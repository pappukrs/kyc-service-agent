"""Async KYC document verification — the enqueue and the handler, together.

These are the two halves of one contract across a process boundary: the MCP
tool produces a `VerificationTask`, the worker consumes it. Keeping them in one
module means the payload, the state machine and the idempotency rules are read
and changed in one place; splitting them across `mcp_server/` and `worker/` is
how the producer and consumer drift apart.

Neither function knows about Kafka. Both take a publisher and a database, so
the entire feature is testable — outcomes, redelivery, case updates — without a
broker or a container. `consumer.py` is the only Kafka-aware file, and it is
nothing but a loop.

The state machine
-----------------
    (any non-verified state)  --queue-->  verifying  --worker-->  verified
                                                                  rejected

Three properties worth being explicit about:

**The tool never asserts an outcome.** It returns `{"status": "queued"}` and
moves the document to `verifying`. The agent can truthfully say "it is being
re-checked" and has nothing available to it that would let it say "it passed".

**The handler is idempotent.** Kafka is at-least-once and redelivery is normal,
not exceptional. A task whose `task_id` is already recorded complete on the
document is dropped rather than re-run — otherwise a redelivered message would
emit a second audit event and append a second note to the customer's case.

**Verification is simulated, and deterministically so.** There is no document
scanner behind this. The outcome is a function of the document, so a demo tells
the same story twice and an eval can assert on it — a coin flip here would make
every downstream test flaky for no gain in realism.
"""

import asyncio
import hashlib
import logging
from datetime import UTC, datetime
from typing import Any

from motor.motor_asyncio import AsyncIOMotorDatabase

from src.config import get_settings
from src.db import repositories
from src.domain.models import DocumentStatus, VerificationOutcome, VerificationTask
from src.messaging import broker

logger = logging.getLogger(__name__)

TOOL_NAME = "verify_kyc_document"

# An expired document is expired. Re-verification is for suspected processing
# errors, and no amount of re-checking makes a lapsed passport current — saying
# so plainly beats sending the customer round the loop again.
_EXPIRED_REASON = (
    "This document has expired. Re-verification cannot change that — please submit "
    "a current, unexpired document."
)

_FAILED_REASON = (
    "Re-verification did not pass: the document image is still not machine-readable. "
    "Please resubmit a clear, uncropped photo or scan of the original."
)


# --------------------------------------------------------------------------- #
# Enqueue — runs inside the MCP tool, must return immediately
# --------------------------------------------------------------------------- #


async def queue_verification(
    db: AsyncIOMotorDatabase,
    *,
    document_id: str,
    scope: str,
    publisher: broker.Publisher | None = None,
) -> dict[str, Any]:
    """Queue a document for re-verification and return without waiting for it.

    Idempotent within a correlation scope, on the same rule as the write tools:
    asking twice in one turn queues one task, asking again next week queues a
    new one.
    """
    settings = get_settings()
    publisher = publisher or broker.get_publisher()

    document = await repositories.find_document(db, document_id)
    if document is None:
        # Data, not an exception: the agent should ask the customer to confirm
        # the document id, not have the turn blow up underneath it.
        return {
            "error": "document_not_found",
            "document_id": document_id,
            "message": (
                f"No document with id {document_id}. Call list_kyc_documents to get the "
                f"customer's real document ids. Do not guess one."
            ),
        }

    if document["status"] == DocumentStatus.VERIFIED.value:
        # Nothing to re-check. Queuing anyway would burn a worker slot and,
        # worse, move a good document back into `verifying` for a while.
        return {
            "status": "already_verified",
            "document_id": document_id,
            "customer_id": document["customer_id"],
            "message": "This document is already verified. No re-check is needed.",
        }

    key, prior = await repositories.claim_write(
        db, scope=scope, tool=TOOL_NAME, arguments={"document_id": document_id}
    )
    if prior:
        return prior | {"idempotent_replay": True}

    task = VerificationTask(
        # Derived from the idempotency key, so the id of the task is the
        # identity of the work — a replay of the same request can only ever
        # name the same task.
        task_id=f"VER-{key[:8].upper()}",
        document_id=document_id,
        customer_id=document["customer_id"],
        correlation_id=scope,
    )

    try:
        await publisher.publish(settings.kafka_tasks_topic, task.model_dump())
    except Exception as exc:  # noqa: BLE001 — a broker outage is not this turn's problem
        # Release the claim: nothing was queued, so a retry must not be deduped
        # against a request that never happened. Then say so honestly rather
        # than letting the agent tell a customer work is under way.
        await repositories.release_write(db, key)
        logger.warning("could not queue verification for %s: %s", document_id, exc)
        return {
            "error": "verification_unavailable",
            "document_id": document_id,
            "message": (
                "Re-verification could not be queued because the processing system is "
                "unavailable. Nothing has been submitted. Tell the customer it could not "
                "be requested and offer to open a servicing case instead."
            ),
        }

    await repositories.mark_document_verifying(
        db, document_id=document_id, task_id=task.task_id, queued_at=task.requested_at
    )

    result = {
        "status": "queued",
        "task_id": task.task_id,
        "document_id": document_id,
        "customer_id": task.customer_id,
        "document_status": DocumentStatus.VERIFYING.value,
        # Said in the tool result, not only in the system prompt: the sentence
        # the agent is most likely to echo is the one in front of it.
        "message": (
            "Re-verification is IN PROGRESS. It has not completed and the document has "
            "not been verified. Tell the customer it is being re-checked and that the "
            "result will land on their application; do not say it passed."
        ),
    }
    await repositories.store_write_result(db, key, result)
    return result


# --------------------------------------------------------------------------- #
# Handler — runs on the worker, allowed to be slow
# --------------------------------------------------------------------------- #


def _status_under_review(document: dict[str, Any]) -> str:
    """The status the document had when it was queued.

    Not `document["status"]` — that is `verifying` by the time the worker sees
    it, because the enqueue moved it there so the read tools would show progress.
    The decision has to be made against what was actually submitted.
    """
    verification = document.get("verification") or {}
    return verification.get("previous_status") or document["status"]


def decide_outcome(document: dict[str, Any]) -> tuple[str, str | None]:
    """Simulated verification. Deterministic on the document id.

    Returns (outcome, rejection_reason). Roughly three in four pass, which is
    the useful shape for a demo: re-verification usually clears a processing
    error, and sometimes it does not — and both paths get exercised without
    anyone having to hand-pick a document id.
    """
    if _status_under_review(document) == DocumentStatus.EXPIRED.value:
        return VerificationOutcome.REJECTED.value, _EXPIRED_REASON

    digest = hashlib.sha256(document["document_id"].encode()).digest()
    if digest[0] < 192:  # 192/256 ≈ 75%
        return VerificationOutcome.VERIFIED.value, None
    return VerificationOutcome.REJECTED.value, _FAILED_REASON


async def apply_verification(
    db: AsyncIOMotorDatabase,
    task: VerificationTask,
    *,
    publisher: broker.Publisher | None = None,
    latency_seconds: float | None = None,
) -> dict[str, Any]:
    """Do the slow part: verify the document, update it and any live case.

    Safe to call twice with the same task — see the module docstring.
    """
    settings = get_settings()
    publisher = publisher or broker.get_publisher()

    document = await repositories.find_document(db, task.document_id)
    if document is None:
        # The document was deleted between queue and consume. Nothing to update,
        # but the attempt is still part of the trail.
        await _emit_audit(publisher, task, outcome="document_missing", document=None)
        logger.warning("verification task %s: document %s is gone", task.task_id, task.document_id)
        return {"status": "document_missing", "task_id": task.task_id}

    verification = document.get("verification") or {}
    if verification.get("task_id") == task.task_id and verification.get("completed_at"):
        logger.info("verification task %s already applied; dropping redelivery", task.task_id)
        return {"status": "duplicate_ignored", "task_id": task.task_id}

    delay = settings.verification_latency_seconds if latency_seconds is None else latency_seconds
    if delay:
        await asyncio.sleep(delay)

    outcome, reason = decide_outcome(document)
    completed_at = datetime.now(UTC)

    await repositories.record_verification_outcome(
        db,
        document_id=task.document_id,
        task_id=task.task_id,
        outcome=outcome,
        reason=reason,
        completed_at=completed_at,
    )

    cases_updated = await repositories.append_case_update(
        db,
        customer_id=task.customer_id,
        update={
            "at": completed_at,
            "source": "kyc_verification",
            "task_id": task.task_id,
            "document_id": task.document_id,
            "outcome": outcome,
            "note": reason or f"{document['doc_type']} passed re-verification.",
        },
    )

    await _emit_audit(publisher, task, outcome=outcome, document=document, reason=reason)

    logger.info(
        "verification task %s: document %s -> %s (%d case(s) updated)",
        task.task_id,
        task.document_id,
        outcome,
        cases_updated,
    )
    return {
        "status": "completed",
        "task_id": task.task_id,
        "document_id": task.document_id,
        "outcome": outcome,
        "cases_updated": cases_updated,
    }


async def _emit_audit(
    publisher: broker.Publisher,
    task: VerificationTask,
    *,
    outcome: str,
    document: dict[str, Any] | None,
    reason: str | None = None,
) -> None:
    """Emit the async half of the audit trail.

    `tool_audit` in Mongo records that the agent *asked* for verification; this
    records what the verification system then *did*, minutes later and in
    another process. Both carry the correlation id, so a turn and its
    consequences can still be stitched together after the fact.

    No PII: an outcome and two ids. Same rule as the Mongo trail — an audit log
    must not become a second copy of the customer database.
    """
    settings = get_settings()
    try:
        await publisher.publish(
            settings.kafka_audit_topic,
            {
                "event": "kyc_document_verified",
                "task_id": task.task_id,
                "document_id": task.document_id,
                "customer_id": task.customer_id,
                "correlation_id": task.correlation_id,
                "doc_type": (document or {}).get("doc_type"),
                "outcome": outcome,
                "reason": reason,
                "at": datetime.now(UTC),
            },
        )
    except Exception:  # noqa: BLE001
        # Same trade as the Mongo audit trail: losing a row beats failing the
        # work. The document update has already been committed and is the
        # record that the customer's answer depends on.
        logger.exception("failed to emit audit event for task %s", task.task_id)
