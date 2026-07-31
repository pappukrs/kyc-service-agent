"""Data access for the servicing tools.

Deliberately separate from the MCP layer: these are plain async functions over a
Motor database, so they can be unit-tested without standing up an MCP session,
and the MCP tool bodies stay thin. The database handle is passed in rather than
imported, which is what makes the tests below able to run against a fake Mongo.
"""

import re
from typing import Any

from motor.motor_asyncio import AsyncIOMotorDatabase

from src.domain.models import DocumentStatus, OnboardingStage

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
