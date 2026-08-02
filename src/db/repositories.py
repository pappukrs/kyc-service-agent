"""Data access for the servicing tools.

Deliberately separate from the MCP layer: these are plain async functions over a
Motor database, so they can be unit-tested without standing up an MCP session,
and the MCP tool bodies stay thin. The database handle is passed in rather than
imported, which is what makes the tests below able to run against a fake Mongo.
"""

import hashlib
import json
import re
from datetime import UTC, datetime
from typing import Any

from motor.motor_asyncio import AsyncIOMotorDatabase
from pymongo.errors import DuplicateKeyError

from src.domain.models import CaseStatus, DocumentStatus, OnboardingStage

# Statuses that stop a customer progressing, mapped to what the customer must do.
_BLOCKING_STATUSES: dict[str, str] = {
    DocumentStatus.REJECTED.value: "Resubmit this document.",
    DocumentStatus.EXPIRED.value: "Submit a current, unexpired document.",
    DocumentStatus.PENDING.value: "Awaiting review — no action needed yet.",
    DocumentStatus.VERIFYING.value: "Verification in progress — no action needed yet.",
}

# What the customer should expect next, per stage.
_STAGE_NEXT_ACTION: dict[str, str] = {
    OnboardingStage.INITIATED.value: "Submit your KYC documents to continue.",
    OnboardingStage.DOCS_PENDING.value: "Submit the outstanding documents listed below.",
    OnboardingStage.UNDER_REVIEW.value: "Your documents are under review; no action needed.",
    OnboardingStage.ADDITIONAL_INFO_REQUIRED.value: (
        "Action needed — resolve the blockers listed below."
    ),
    OnboardingStage.APPROVED.value: "Onboarding is complete. Nothing is pending.",
    OnboardingStage.REJECTED.value: (
        "The application was rejected. A servicing agent must review it before it can reopen."
    ),
}

_PROJECTION = {"_id": 0}


async def find_customer(db: AsyncIOMotorDatabase, customer_id: str) -> dict[str, Any] | None:
    return await db.customers.find_one({"customer_id": customer_id}, _PROJECTION)


async def find_document(db: AsyncIOMotorDatabase, document_id: str) -> dict[str, Any] | None:
    return await db.kyc_documents.find_one({"document_id": document_id}, _PROJECTION)


async def find_documents(db: AsyncIOMotorDatabase, customer_id: str) -> list[dict[str, Any]]:
    cursor = db.kyc_documents.find({"customer_id": customer_id}, _PROJECTION).sort("document_id", 1)
    return await cursor.to_list(length=None)


async def build_onboarding_status(
    db: AsyncIOMotorDatabase, customer_id: str
) -> dict[str, Any] | None:
    """Derive stage + blockers + next action from the customer and their documents.

    Returns None when the customer does not exist, so the caller can distinguish
    "no such customer" from "customer with nothing outstanding".
    """
    customer = await find_customer(db, customer_id)
    if customer is None:
        return None

    documents = await find_documents(db, customer_id)
    stage = customer["onboarding_stage"]

    blockers: list[dict[str, Any]] = []
    for doc in documents:
        status = doc["status"]
        if status not in _BLOCKING_STATUSES:
            continue
        blockers.append(
            {
                "document_id": doc["document_id"],
                "doc_type": doc["doc_type"],
                "status": status,
                # Only rejections carry a reason; surface it verbatim — it is
                # almost always the real answer to "why am I stuck?".
                "reason": doc.get("rejection_reason"),
                "customer_action": _BLOCKING_STATUSES[status],
                # Pending/verifying are waiting states, not things to act on.
                "needs_customer_action": status
                in (DocumentStatus.REJECTED.value, DocumentStatus.EXPIRED.value),
            }
        )

    actionable = [b for b in blockers if b["needs_customer_action"]]

    return {
        "customer_id": customer_id,
        "full_name": customer["full_name"],
        "onboarding_stage": stage,
        "risk_tier": customer["risk_tier"],
        "next_action": _STAGE_NEXT_ACTION.get(stage, "Contact servicing."),
        "blockers": blockers,
        "blocker_count": len(blockers),
        "awaiting_customer_action": len(actionable) > 0,
        "documents_total": len(documents),
        "documents_verified": sum(
            1 for d in documents if d["status"] == DocumentStatus.VERIFIED.value
        ),
    }


_TOKEN = re.compile(r"[a-z0-9]+")


def _tokens(text: str) -> set[str]:
    return set(_TOKEN.findall(text.lower()))


async def search_kb(db: AsyncIOMotorDatabase, query: str, limit: int = 3) -> list[dict[str, Any]]:
    """Keyword search over the servicing knowledge base.

    Plain term-overlap scoring, with tags weighted above body text. This is
    deliberately not a vector store — the project is about agent architecture,
    and a lexical search over a few dozen policy articles is both sufficient
    and easier to reason about when an eval fails.
    """
    terms = _tokens(query)
    if not terms:
        return []

    articles = await db.kb_articles.find({}, _PROJECTION).to_list(length=None)

    scored: list[tuple[int, dict[str, Any]]] = []
    for article in articles:
        title_hits = len(terms & _tokens(article.get("title", "")))
        tag_hits = len(terms & {t.lower() for t in article.get("tags", [])})
        body_hits = len(terms & _tokens(article.get("body", "")))
        score = title_hits * 3 + tag_hits * 2 + body_hits
        if score:
            scored.append((score, {**article, "score": score}))

    scored.sort(key=lambda pair: (-pair[0], pair[1]["title"]))
    return [article for _, article in scored[:limit]]


# --------------------------------------------------------------------------- #
# Writes
# --------------------------------------------------------------------------- #

# Fields a customer is allowed to change through servicing. Anything touching
# identity (name, date of birth) requires a fresh KYC cycle and a human — an
# allowlist rather than a denylist, so a new field is closed by default.
UPDATABLE_CONTACT_FIELDS: frozenset[str] = frozenset({"email", "phone", "city"})


def idempotency_key(scope: str, tool: str, arguments: dict[str, Any]) -> str:
    """Derive a stable key for one logical write.

    Same scope + same tool + same arguments ⇒ same key, so a retried or
    duplicated call collapses onto the first one instead of creating a second
    case. Scope is the correlation id: retrying *within a turn* is a duplicate,
    but a customer legitimately asking again tomorrow is not.
    """
    canonical = json.dumps(arguments, sort_keys=True, default=str)
    digest = hashlib.sha256(f"{scope}|{tool}|{canonical}".encode()).hexdigest()
    return digest[:32]


async def _claim_idempotency_key(
    db: AsyncIOMotorDatabase, key: str, tool: str
) -> dict[str, Any] | None:
    """Claim a key, or return the result of the write that claimed it first.

    Relies on the unique index on `idempotency_keys.key` — the database decides
    the race, not the application. Returns None when the claim succeeded and
    the caller should proceed.
    """
    try:
        await db.idempotency_keys.insert_one(
            {"key": key, "tool": tool, "created_at": datetime.now(UTC), "result": None}
        )
        return None
    except DuplicateKeyError:
        existing = await db.idempotency_keys.find_one({"key": key}, {"_id": 0})
        return (existing or {}).get("result") or {"status": "duplicate_in_progress"}


async def _store_idempotent_result(
    db: AsyncIOMotorDatabase, key: str, result: dict[str, Any]
) -> None:
    await db.idempotency_keys.update_one({"key": key}, {"$set": {"result": result}})


async def claim_write(
    db: AsyncIOMotorDatabase, *, scope: str, tool: str, arguments: dict[str, Any]
) -> tuple[str, dict[str, Any] | None]:
    """Public form of the claim: returns (key, prior result or None).

    Used by writes that live outside this module — the verification enqueue in
    `src.worker.verification` needs the same dedupe as `create_case`, and there
    is no reason for it to own a second copy of the rule.
    """
    key = idempotency_key(scope, tool, arguments)
    return key, await _claim_idempotency_key(db, key, tool)


async def release_write(db: AsyncIOMotorDatabase, key: str) -> None:
    """Drop an unfulfilled claim so a retry is not permanently deduped.

    Only ever called when the write it guarded did *not* happen — a broker that
    refused the message, say. A claim whose write succeeded is never released;
    that is the whole point of it.
    """
    await db.idempotency_keys.delete_one({"key": key, "result": None})


async def store_write_result(db: AsyncIOMotorDatabase, key: str, result: dict[str, Any]) -> None:
    await _store_idempotent_result(db, key, result)


async def create_case(
    db: AsyncIOMotorDatabase,
    *,
    customer_id: str,
    category: str,
    summary: str,
    session_id: str | None = None,
    approved_by: str | None = None,
    scope: str = "",
) -> dict[str, Any]:
    """Open a servicing case. Idempotent within a correlation scope."""
    if await find_customer(db, customer_id) is None:
        return {"error": "customer_not_found", "customer_id": customer_id}

    args = {"customer_id": customer_id, "category": category, "summary": summary}
    key = idempotency_key(scope, "create_servicing_case", args)
    if prior := await _claim_idempotency_key(db, key, "create_servicing_case"):
        # Report the original outcome rather than silently doing nothing — the
        # agent needs to tell the customer a case exists, not that it failed.
        return prior | {"idempotent_replay": True}

    case_id = f"CASE-{key[:8].upper()}"
    case = {
        "case_id": case_id,
        "customer_id": customer_id,
        "category": category,
        "summary": summary,
        "status": CaseStatus.OPEN.value,
        "created_at": datetime.now(UTC),
        "session_id": session_id,
        "approved_by": approved_by,
    }
    await db.servicing_cases.insert_one(case)

    result = {
        "status": "created",
        "case_id": case_id,
        "customer_id": customer_id,
        "case_status": CaseStatus.OPEN.value,
        "approved_by": approved_by,
    }
    await _store_idempotent_result(db, key, result)
    return result


async def mark_document_verifying(
    db: AsyncIOMotorDatabase, *, document_id: str, task_id: str, queued_at: datetime
) -> None:
    """Move a document into the `verifying` state the moment work is queued.

    Without this a customer who asks again two minutes later is told their
    document is still rejected, with nothing to indicate a re-check is running.
    `verifying` already reads as "in progress, no action needed" in
    build_onboarding_status, so the whole read path picks this up for free.
    """
    document = await find_document(db, document_id)
    await db.kyc_documents.update_one(
        {"document_id": document_id},
        {
            "$set": {
                "status": DocumentStatus.VERIFYING.value,
                "verification": {
                    "task_id": task_id,
                    "queued_at": queued_at,
                    # The status this replaced. Load-bearing, not diagnostic:
                    # `verifying` overwrites the very field the worker needs to
                    # decide the outcome, and an expired document must stay
                    # expired however many times it is re-checked.
                    "previous_status": (document or {}).get("status"),
                    "completed_at": None,
                    "outcome": None,
                },
            }
        },
    )


async def record_verification_outcome(
    db: AsyncIOMotorDatabase,
    *,
    document_id: str,
    task_id: str,
    outcome: str,
    reason: str | None,
    completed_at: datetime,
) -> None:
    """Write the worker's decision onto the document.

    A pass clears `rejection_reason`: leaving the old one behind would have the
    read tools telling a customer with a now-verified document why it was once
    rejected, which is exactly the sort of stale-field bug that erodes trust in
    the answers.
    """
    await db.kyc_documents.update_one(
        {"document_id": document_id},
        {
            "$set": {
                "status": outcome,
                "rejection_reason": reason,
                "reviewed_at": completed_at,
                "verification.task_id": task_id,
                "verification.completed_at": completed_at,
                "verification.outcome": outcome,
            }
        },
    )


async def append_case_update(
    db: AsyncIOMotorDatabase, *, customer_id: str, update: dict[str, Any]
) -> int:
    """Append a note to the customer's live cases. Returns how many were touched.

    Live means open or in progress — a resolved case is history and does not get
    rewritten. `$push` rather than `$set` because a case accumulates events and
    the human who picks it up needs the sequence, not the latest one.
    """
    result = await db.servicing_cases.update_many(
        {
            "customer_id": customer_id,
            "status": {"$in": [CaseStatus.OPEN.value, CaseStatus.IN_PROGRESS.value]},
        },
        {"$push": {"updates": update}},
    )
    return result.modified_count


async def update_contact(
    db: AsyncIOMotorDatabase,
    *,
    customer_id: str,
    field: str,
    value: str,
    approved_by: str | None = None,
    scope: str = "",
) -> dict[str, Any]:
    """Update one contact field. Idempotent within a correlation scope."""
    if field not in UPDATABLE_CONTACT_FIELDS:
        return {
            "error": "field_not_updatable",
            "field": field,
            "message": (
                f"{field!r} cannot be changed through servicing. Updatable fields are: "
                f"{', '.join(sorted(UPDATABLE_CONTACT_FIELDS))}. Changes to name or date of "
                f"birth require a fresh KYC cycle."
            ),
        }

    if await find_customer(db, customer_id) is None:
        return {"error": "customer_not_found", "customer_id": customer_id}

    args = {"customer_id": customer_id, "field": field, "value": value}
    key = idempotency_key(scope, "update_customer_contact", args)
    if prior := await _claim_idempotency_key(db, key, "update_customer_contact"):
        return prior | {"idempotent_replay": True}

    await db.customers.update_one(
        {"customer_id": customer_id},
        {"$set": {field: value, "updated_at": datetime.now(UTC)}},
    )

    # Note: the new value is NOT echoed back. It is personal data, it is already
    # known to whoever supplied it, and returning it would put it in the model's
    # context and the conversation transcript for no benefit.
    result = {
        "status": "updated",
        "customer_id": customer_id,
        "field": field,
        "approved_by": approved_by,
    }
    await _store_idempotent_result(db, key, result)
    return result
