"""Kafka worker — the only Kafka-aware file on the consuming side.

`verify_kyc_document` returns immediately with {"status": "queued"}; this
process does the slow part and updates the document and any live case when it
finishes. All of the actual behaviour lives in `verification.apply_verification`
so it can be tested without a broker; this file is the loop that feeds it.

    make worker        (or)   python -m src.worker.consumer

Delivery: at-least-once. The offset is committed *after* the handler returns,
so a crash mid-task replays it rather than losing it — and the handler is
idempotent precisely because that replay is expected, not exceptional.

Poison messages: a payload that does not validate is logged and committed
rather than retried. Retrying it forever would wedge the partition behind a
message that can never succeed, and one malformed task must not stop
verification for every other customer.
"""

import asyncio
import json
import logging
import signal

from pydantic import ValidationError

from src.config import get_settings
from src.db import mongo
from src.domain.models import VerificationTask
from src.messaging import broker
from src.worker import verification

logger = logging.getLogger(__name__)

CONSUMER_GROUP = "kyc-verifier"


async def run(stop: asyncio.Event | None = None) -> None:
    from aiokafka import AIOKafkaConsumer

    settings = get_settings()
    stop = stop or asyncio.Event()

    consumer = AIOKafkaConsumer(
        settings.kafka_tasks_topic,
        bootstrap_servers=settings.kafka_bootstrap,
        group_id=CONSUMER_GROUP,
        # Manual commits — see the module docstring. Auto-commit would ack
        # messages the handler has not run yet, turning at-least-once into
        # at-most-once and silently dropping work on a crash.
        enable_auto_commit=False,
        auto_offset_reset="earliest",
        value_deserializer=lambda raw: json.loads(raw.decode("utf-8")),
    )

    await consumer.start()
    logger.info(
        "worker up: topic=%s group=%s broker=%s",
        settings.kafka_tasks_topic,
        CONSUMER_GROUP,
        settings.kafka_bootstrap,
    )

    db = mongo.get_db()
    try:
        while not stop.is_set():
            batch = await consumer.getmany(timeout_ms=1000, max_records=10)
            for messages in batch.values():
                for message in messages:
                    await _handle(db, message.value)
            if batch:
                await consumer.commit()
    finally:
        await consumer.stop()
        await broker.close_publisher()
        logger.info("worker stopped")


async def _handle(db, payload: dict) -> None:
    try:
        task = VerificationTask.model_validate(payload)
    except ValidationError:
        logger.exception("dropping unparseable verification task: %r", payload)
        return

    try:
        await verification.apply_verification(db, task)
    except Exception:  # noqa: BLE001
        # One task failing must not take the worker down; the offset is
        # committed either way, and the document simply stays in `verifying`
        # where a human can see it. Losing the loop would strand every other
        # customer's re-check behind this one.
        logger.exception("verification task %s failed", task.task_id)


async def main() -> None:
    logging.basicConfig(level=get_settings().log_level)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        # Finish the task in hand and commit before exiting, rather than being
        # killed between the Mongo write and the offset commit.
        loop.add_signal_handler(sig, stop.set)

    await run(stop)


if __name__ == "__main__":
    asyncio.run(main())
