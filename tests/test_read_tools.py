"""Phase 2 — the four read tools, against an in-memory Mongo.

These run without Docker: mongomock-motor gives a real Motor-shaped async API
over an in-memory store, so the query logic is genuinely exercised rather than
mocked out.
"""

from datetime import UTC, datetime

import pytest
from mongomock_motor import AsyncMongoMockClient

from src.db import repositories

NOW = datetime(2026, 7, 1, tzinfo=UTC)


@pytest.fixture
async def db():
    database = AsyncMongoMockClient()["kyc_test"]

    await database.customers.insert_many(
        [
            {
                "customer_id": "CUST-001",
                "full_name": "Aarav Sharma",
                "email": "user001@example.invalid",
                "phone": "+91 9000000001",
                "city": "Bengaluru",
                "onboarding_stage": "additional_info_required",
                "risk_tier": "medium",
                "created_at": NOW,
                "updated_at": NOW,
            },
            {
                "customer_id": "CUST-002",
                "full_name": "Diya Iyer",
                "email": "user002@example.invalid",
                "phone": "+91 9000000002",
                "city": "Pune",
                "onboarding_stage": "approved",
                "risk_tier": "low",
                "created_at": NOW,
                "updated_at": NOW,
            },
        ]
    )

    await database.kyc_documents.insert_many(
        [
            {
                "document_id": "DOC-001-1",
                "customer_id": "CUST-001",
                "doc_type": "address_proof",
                "status": "rejected",
                "rejection_reason": "Address proof is older than 3 months.",
                "submitted_at": NOW,
                "reviewed_at": NOW,
            },
            {
                "document_id": "DOC-001-2",
                "customer_id": "CUST-001",
                "doc_type": "pan",
                "status": "verified",
                "rejection_reason": None,
                "submitted_at": NOW,
                "reviewed_at": NOW,
            },
            {
                "document_id": "DOC-001-3",
                "customer_id": "CUST-001",
                "doc_type": "photo",
                "status": "pending",
                "rejection_reason": None,
                "submitted_at": NOW,
                "reviewed_at": None,
            },
            {
                "document_id": "DOC-002-1",
                "customer_id": "CUST-002",
                "doc_type": "pan",
                "status": "verified",
                "rejection_reason": None,
                "submitted_at": NOW,
                "reviewed_at": NOW,
            },
        ]
    )

    await database.kb_articles.insert_many(
        [
            {
                "title": "Accepted address proofs",
                "body": "Utility bills dated within the last 3 months are accepted.",
                "tags": ["address", "documents", "kyc"],
            },
            {
                "title": "KYC verification timelines",
                "body": "Standard KYC verification completes within 2 business days.",
                "tags": ["timeline", "kyc", "verification"],
            },
        ]
    )
    return database


# --------------------------------------------------------------------------- #
# find_customer
# --------------------------------------------------------------------------- #


async def test_find_customer_returns_profile_without_mongo_id(db):
    customer = await repositories.find_customer(db, "CUST-001")
    assert customer["full_name"] == "Aarav Sharma"
    # _id must never reach the agent — it is noise the model will try to use.
    assert "_id" not in customer


async def test_find_customer_returns_none_for_unknown_id(db):
    assert await repositories.find_customer(db, "CUST-999") is None


# --------------------------------------------------------------------------- #
# find_documents
# --------------------------------------------------------------------------- #


async def test_find_documents_scopes_to_one_customer(db):
    docs = await repositories.find_documents(db, "CUST-001")
    assert len(docs) == 3
    assert {d["customer_id"] for d in docs} == {"CUST-001"}


async def test_find_documents_surfaces_rejection_reason(db):
    docs = await repositories.find_documents(db, "CUST-001")
    rejected = [d for d in docs if d["status"] == "rejected"]
    assert len(rejected) == 1
    assert rejected[0]["rejection_reason"] == "Address proof is older than 3 months."


# --------------------------------------------------------------------------- #
# build_onboarding_status
# --------------------------------------------------------------------------- #


async def test_status_flags_rejected_document_as_actionable(db):
    status = await repositories.build_onboarding_status(db, "CUST-001")

    assert status["onboarding_stage"] == "additional_info_required"
    assert status["awaiting_customer_action"] is True

    rejected = next(b for b in status["blockers"] if b["status"] == "rejected")
    assert rejected["reason"] == "Address proof is older than 3 months."
    assert rejected["needs_customer_action"] is True


async def test_status_does_not_ask_customer_to_act_on_pending_docs(db):
    """A pending document is a waiting state, not a to-do.

    Telling a customer to act on a document that is simply queued for review is
    the single most common way a servicing bot wastes someone's time.
    """
    status = await repositories.build_onboarding_status(db, "CUST-001")
    pending = next(b for b in status["blockers"] if b["status"] == "pending")
    assert pending["needs_customer_action"] is False


async def test_status_counts_verified_documents(db):
    status = await repositories.build_onboarding_status(db, "CUST-001")
    assert status["documents_total"] == 3
    assert status["documents_verified"] == 1


async def test_approved_customer_has_no_blockers(db):
    status = await repositories.build_onboarding_status(db, "CUST-002")
    assert status["blockers"] == []
    assert status["awaiting_customer_action"] is False
    assert "complete" in status["next_action"].lower()


async def test_status_returns_none_for_unknown_customer(db):
    assert await repositories.build_onboarding_status(db, "CUST-999") is None


# --------------------------------------------------------------------------- #
# search_kb
# --------------------------------------------------------------------------- #


async def test_kb_search_ranks_title_and_tag_matches_first(db):
    results = await repositories.search_kb(db, "what address proof is accepted?")
    assert results[0]["title"] == "Accepted address proofs"


async def test_kb_search_matches_on_tags(db):
    results = await repositories.search_kb(db, "verification timeline")
    assert results[0]["title"] == "KYC verification timelines"


async def test_kb_search_returns_empty_for_no_match(db):
    assert await repositories.search_kb(db, "cryptocurrency margin trading") == []


async def test_kb_search_handles_punctuation_only_query(db):
    assert await repositories.search_kb(db, "???") == []
