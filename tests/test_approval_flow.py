"""Phase 5 — write tools and the human-approval gate.

The security-critical path in this project. The claim being tested is narrow
and absolute: **no write reaches the database without a human decision**, and
the guarantee does not depend on the model cooperating.
"""

from contextlib import AsyncExitStack, asynccontextmanager
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from httpx import ASGITransport
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver
from mongomock_motor import AsyncMongoMockClient

from src.agent.graph import agent_session
from src.db import mongo as mongo_module
from src.db import repositories
from src.obs import audit

NOW = datetime(2026, 7, 1, tzinfo=UTC)


class ScriptedChatModel(FakeMessagesListChatModel):
    def bind_tools(self, tools: Any, **kwargs: Any):  # noqa: ARG002
        return self


def tool_call(name: str, args: dict) -> AIMessage:
    return AIMessage(
        content="", tool_calls=[{"name": name, "args": args, "id": "c1", "type": "tool_call"}]
    )


async def seed(db):
    await db.customers.insert_one(
        {
            "customer_id": "CUST-014",
            "full_name": "Meera Nair",
            "email": "user014@example.invalid",
            "phone": "+91 9000000014",
            "city": "Chennai",
            "onboarding_stage": "additional_info_required",
            "risk_tier": "high",
            "created_at": NOW,
            "updated_at": NOW,
        }
    )
    # The unique index is what makes idempotency a database guarantee rather
    # than an application convention — mongomock honours it.
    await db.idempotency_keys.create_index("key", unique=True)
    return db


@pytest.fixture
async def db(monkeypatch):
    database = AsyncMongoMockClient()["kyc_approval_test"]
    monkeypatch.setattr(mongo_module, "get_db", lambda: database)
    return await seed(database)


@asynccontextmanager
async def api(model, monkeypatch, db):
    monkeypatch.setattr(mongo_module, "ensure_indexes", _noop)
    from src.api.main import app

    async with AsyncExitStack() as stack:
        app.state.agent = await stack.enter_async_context(
            agent_session(model=model, checkpointer=InMemorySaver())
        )
        transport = ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
            yield http


async def _noop():
    return None


# --------------------------------------------------------------------------- #
# Repository-level write semantics
# --------------------------------------------------------------------------- #


async def test_create_case_writes_and_records_approver(db):
    result = await repositories.create_case(
        db,
        customer_id="CUST-014",
        category="kyc",
        summary="Document rejected twice",
        approved_by="reviewer@bank.invalid",
        scope="corr-1",
    )
    assert result["status"] == "created"

    stored = await db.servicing_cases.find_one({"case_id": result["case_id"]})
    assert stored["approved_by"] == "reviewer@bank.invalid"
    assert stored["status"] == "open"


async def test_create_case_is_idempotent_within_a_scope(db):
    args = dict(customer_id="CUST-014", category="kyc", summary="same request", scope="corr-1")

    first = await repositories.create_case(db, **args)
    second = await repositories.create_case(db, **args)

    assert second.get("idempotent_replay") is True
    assert second["case_id"] == first["case_id"]
    assert await db.servicing_cases.count_documents({}) == 1


async def test_same_request_in_a_later_turn_opens_a_second_case(db):
    """Idempotency must not swallow a genuine repeat request.

    Retrying inside one turn is a duplicate; the customer asking again next
    week is not. Scoping the key to the correlation id draws that line.
    """
    args = dict(customer_id="CUST-014", category="kyc", summary="same request")

    await repositories.create_case(db, **args, scope="corr-1")
    await repositories.create_case(db, **args, scope="corr-2")

    assert await db.servicing_cases.count_documents({}) == 2


async def test_create_case_rejects_unknown_customer(db):
    result = await repositories.create_case(
        db, customer_id="CUST-999", category="kyc", summary="x", scope="c"
    )
    assert result["error"] == "customer_not_found"
    assert await db.servicing_cases.count_documents({}) == 0


async def test_update_contact_applies_the_change(db):
    result = await repositories.update_contact(
        db, customer_id="CUST-014", field="city", value="Mumbai", scope="c"
    )
    assert result["status"] == "updated"
    assert (await db.customers.find_one({"customer_id": "CUST-014"}))["city"] == "Mumbai"


async def test_update_contact_does_not_echo_the_new_value(db):
    """The new value is personal data and must not re-enter the transcript."""
    result = await repositories.update_contact(
        db, customer_id="CUST-014", field="email", value="private@example.invalid", scope="c"
    )
    assert "private@example.invalid" not in str(result)


@pytest.mark.parametrize("field", ["full_name", "date_of_birth", "risk_tier", "onboarding_stage"])
async def test_update_contact_refuses_fields_outside_the_allowlist(db, field):
    """An allowlist, so a newly added field is closed by default."""
    before = await db.customers.find_one({"customer_id": "CUST-014"})

    result = await repositories.update_contact(
        db, customer_id="CUST-014", field=field, value="tampered", scope="c"
    )

    assert result["error"] == "field_not_updatable"
    assert await db.customers.find_one({"customer_id": "CUST-014"}) == before


# --------------------------------------------------------------------------- #
# The gate, end to end
# --------------------------------------------------------------------------- #


async def test_pending_write_touches_nothing_until_approved(monkeypatch, db):
    model = ScriptedChatModel(
        responses=[
            tool_call(
                "create_servicing_case",
                {"customer_id": "CUST-014", "category": "kyc", "summary": "escalate"},
            ),
            AIMessage(content="I've opened a case for you."),
        ]
    )

    async with api(model, monkeypatch, db) as http:
        pending = await http.post("/sessions/s1/messages", json={"message": "escalate"})
        assert pending.json()["status"] == "awaiting_approval"
        # Nothing written while it waits.
        assert await db.servicing_cases.count_documents({}) == 0

        approved = await http.post(
            "/sessions/s1/approve", json={"approve": True, "approver": "reviewer@bank.invalid"}
        )

    assert approved.status_code == 200
    assert approved.json()["status"] == "approved"
    assert await db.servicing_cases.count_documents({}) == 1

    case = await db.servicing_cases.find_one({})
    assert case["approved_by"] == "reviewer@bank.invalid"


async def test_rejection_leaves_the_database_untouched(monkeypatch, db):
    model = ScriptedChatModel(
        responses=[
            tool_call(
                "create_servicing_case",
                {"customer_id": "CUST-014", "category": "kyc", "summary": "escalate"},
            ),
            AIMessage(content="A reviewer declined that; here's what you can do instead."),
        ]
    )

    async with api(model, monkeypatch, db) as http:
        await http.post("/sessions/s2/messages", json={"message": "escalate"})
        rejected = await http.post(
            "/sessions/s2/approve",
            json={
                "approve": False,
                "approver": "reviewer@bank.invalid",
                "reason": "Read tools already answered this.",
            },
        )

    assert rejected.json()["status"] == "rejected"
    assert await db.servicing_cases.count_documents({}) == 0


async def test_approving_when_nothing_is_pending_is_a_conflict(monkeypatch, db):
    """No pending action means no write — approval cannot conjure one."""
    model = ScriptedChatModel(responses=[AIMessage(content="Nothing to do.")])

    async with api(model, monkeypatch, db) as http:
        await http.post("/sessions/s3/messages", json={"message": "hello"})
        resp = await http.post(
            "/sessions/s3/approve", json={"approve": True, "approver": "reviewer@bank.invalid"}
        )

    assert resp.status_code == 409
    assert await db.servicing_cases.count_documents({}) == 0


async def test_approved_write_is_audited_with_the_approver(monkeypatch, db):
    model = ScriptedChatModel(
        responses=[
            tool_call(
                "create_servicing_case",
                {"customer_id": "CUST-014", "category": "kyc", "summary": "escalate"},
            ),
            AIMessage(content="Opened."),
        ]
    )

    async with api(model, monkeypatch, db) as http:
        await http.post("/sessions/s4/messages", json={"message": "escalate"})
        await http.post(
            "/sessions/s4/approve", json={"approve": True, "approver": "reviewer@bank.invalid"}
        )

    trail = await audit.read_session_trail("s4")
    write_rows = [r for r in trail if r["tool_name"] == "create_servicing_case"]
    assert len(write_rows) == 1
    assert write_rows[0]["approved_by"] == "reviewer@bank.invalid"


async def test_approver_does_not_leak_into_a_later_write(monkeypatch, db):
    """One human's authorisation must not attach to somebody else's write."""
    audit.set_approver("reviewer@bank.invalid")
    audit.clear_approver()

    await audit.record_tool_call(
        session_id="s5",
        tool_name="create_servicing_case",
        arguments={},
        result="{}",
        latency_ms=1,
    )

    row = (await audit.read_session_trail("s5"))[0]
    assert row["approved_by"] is None


async def test_contact_update_value_is_redacted_in_the_audit_trail(monkeypatch, db):
    """The write whose *arguments* are the personal data — the leak-prone case."""
    model = ScriptedChatModel(
        responses=[
            tool_call(
                "update_customer_contact",
                {"customer_id": "CUST-014", "field": "email", "value": "private@example.invalid"},
            ),
            AIMessage(content="Updated."),
        ]
    )

    async with api(model, monkeypatch, db) as http:
        await http.post("/sessions/s6/messages", json={"message": "change my email"})
        await http.post(
            "/sessions/s6/approve", json={"approve": True, "approver": "reviewer@bank.invalid"}
        )

    # The write landed…
    assert (await db.customers.find_one({"customer_id": "CUST-014"}))["email"] == (
        "private@example.invalid"
    )
    # …but the address appears nowhere in the audit trail.
    trail = await audit.read_session_trail("s6")
    assert "private@example.invalid" not in str(trail)
    row = next(r for r in trail if r["tool_name"] == "update_customer_contact")
    assert row["arguments"]["value"] == audit.REDACTED
    assert row["arguments"]["field"] == "email"  # which field changed is auditable
