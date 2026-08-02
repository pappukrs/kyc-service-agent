"""Publishing side of the async path.

One small interface — `publish(topic, message)` — with two implementations: a
Kafka producer for the running system, and an in-memory recorder that lets the
whole verification feature be tested without a broker. Every test in this
project runs without Docker; a hard dependency on aiokafka at import time would
have ended that, so the import lives inside the Kafka implementation.

The process-wide accessor mirrors `src.db.mongo.get_db`: callers ask for a
publisher rather than constructing one, so a test (or the worker, or a script)
can swap the transport underneath them without any tool code knowing.

Delivery guarantee: at-least-once. `acks="all"` on the producer and a commit
after handling on the consumer means a message can be delivered twice and must
never be lost. Everything downstream is written to tolerate the duplicate —
see `src.worker.verification.apply_verification`.
"""

import asyncio
import json
import logging
from typing import Any, Protocol

from src.config import get_settings

logger = logging.getLogger(__name__)


def serialize(message: dict[str, Any]) -> bytes:
    """Encode a message for the wire. `default=str` carries datetimes as ISO-8601."""
    return json.dumps(message, default=str).encode("utf-8")


class Publisher(Protocol):
    """What the rest of the codebase needs from a broker. Deliberately tiny."""

    async def publish(self, topic: str, message: dict[str, Any]) -> None: ...

    async def close(self) -> None: ...


class KafkaPublisher:
    """aiokafka-backed producer, started lazily on first publish.

    Lazy because the MCP server, the API and the worker all import this module,
    but only some of them ever produce — connecting a broker at import time
    would make an unused dependency into a startup failure.
    """

    def __init__(self, bootstrap_servers: str) -> None:
        self._bootstrap = bootstrap_servers
        self._producer: Any = None

    async def _ensure_started(self) -> Any:
        if self._producer is None:
            from aiokafka import AIOKafkaProducer

            producer = AIOKafkaProducer(
                bootstrap_servers=self._bootstrap,
                value_serializer=serialize,
                # Wait for the full in-sync replica set. A queued verification
                # the customer has been told about must not evaporate because a
                # leader died between the ack and the replication.
                acks="all",
                enable_idempotence=True,
            )
            await producer.start()
            self._producer = producer
        return self._producer

    async def publish(self, topic: str, message: dict[str, Any]) -> None:
        # Bounded, because this runs inside a customer's turn. aiokafka retries
        # bootstrap for tens of seconds against an unreachable broker, which the
        # caller experiences as a hang rather than as the outage it is. Raising
        # is correct here — the caller decides what to tell the customer.
        async with asyncio.timeout(get_settings().kafka_publish_timeout_seconds):
            producer = await self._ensure_started()
            # send_and_wait, not send: the caller is about to tell a customer
            # their request is queued, and that claim should be true when made.
            await producer.send_and_wait(topic, message)

    async def close(self) -> None:
        if self._producer is not None:
            await self._producer.stop()
            self._producer = None


class InMemoryPublisher:
    """Records messages instead of sending them.

    Used by the test suite, and usable as a no-broker mode for a demo box: the
    tool still behaves correctly, the work simply never runs.
    """

    def __init__(self) -> None:
        self.messages: list[tuple[str, dict[str, Any]]] = []

    async def publish(self, topic: str, message: dict[str, Any]) -> None:
        # Round-trip through the real serializer so a message that Kafka could
        # not encode fails in tests too, rather than only in production.
        self.messages.append((topic, json.loads(serialize(message))))

    async def close(self) -> None:
        self.messages.clear()

    def topic(self, topic: str) -> list[dict[str, Any]]:
        return [message for sent_topic, message in self.messages if sent_topic == topic]


_publisher: Publisher | None = None


def get_publisher() -> Publisher:
    global _publisher
    if _publisher is None:
        _publisher = KafkaPublisher(get_settings().kafka_bootstrap)
    return _publisher


def set_publisher(publisher: Publisher | None) -> None:
    """Install a publisher (tests, scripts) or reset to the default with None."""
    global _publisher
    _publisher = publisher


async def close_publisher() -> None:
    global _publisher
    if _publisher is not None:
        await _publisher.close()
        _publisher = None
