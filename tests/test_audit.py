"""Phase 4 — the append-only tool audit trail.

The tests that matter most here are the PII ones. A tool call is easy to log;
logging it without quietly building a second copy of the customer database is
the part that takes care.
"""

from datetime import UTC, datetime
from typing import Any

import pytest
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver
from mongomock_motor import AsyncMongoMockClient

from src.db import mongo as mongo_module
from src.obs import audit

NOW = datetime(2026, 7, 1, tzinfo=UTC)


@pytest.fixture(autouse=True)
def fake_db(monkeypatch):
    db = AsyncMongoMockClient()["kyc_audit_test"]
    monkeypatch.setattr(mongo_module, "get_db", lambda: db)
    return db


class ScriptedChatModel(FakeMessagesListChatModel):
    def bind_tools(self, tools: Any, **kwargs: Any):  # noqa: ARG002
        return self


def tool_call(name: str, args: dict) -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[{"name": name, "args": args, "id": "c1", "type": "tool_call"}],
    )


@pytest.fixture
async def seeded(fake_db):
    await fake_db.customers.insert_one(
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
    await fake_db.kyc_documents.insert_one(
        {
            "document_id": "DOC-014-1",
            "customer_id": "CUST-014",
            "doc_type": "address_proof",
            "status": "rejected",
            "rejection_reason": "Document image is blurred; text is not machine-readable.",
            "submitted_at": NOW,
            "reviewed_at": NOW,
        }
    )
    return fake_db


# --------------------------------------------------------------------------- #
# Redaction — the PII surface
# --------------------------------------------------------------------------- #


def test_redaction_hides_the_value_but_keeps_the_key():
    """Knowing `value` was supplied is audit signal; its content is not."""
    out = audit.redact_arguments(
        {"customer_id": "CUST-014", "field": "email", "value": "real.person@example.com"}
    )
    assert out["customer_id"] == "CUST-014"  # an id is not personal data here
    assert out["field"] == "email"  # *which* field changed is auditable
    assert out["value"] == audit.REDACTED
    assert "real.person@example.com" not in str(out)


@pytest.mark.parametrize(
    "key",
    ["email", "new_email", "phone", "contact_phone", "address", "full_name", "dob", "aadhaar"],
)
def test_redaction_matches_sensitive_keys_by_substring(key):
    assert audit.redact_arguments({key: "sensitive"})[key] == audit.REDACTED


def test_hash_is_stable_and_discriminating():
    assert audit.hash_result("same") == audit.hash_result("same")
    assert audit.hash_result("a") != audit.hash_result("b")


# --------------------------------------------------------------------------- #
# Writing rows
# --------------------------------------------------------------------------- #


async def test_record_stores_hash_not_payload(fake_db):
    secret = '{"full_name": "Meera Nair", "phone": "+91 9000000014"}'
    await audit.record_tool_call(
        session_id="s1",
        tool_name="get_customer",
        arguments={"customer_id": "CUST-014"},
        result=secret,
        latency_ms=12,
    )

    row = await fake_db.tool_audit.find_one({"session_id": "s1"})
    assert row["result_sha256"] == audit.hash_result(secret)
    # The payload itself must not be anywhere in the row.
    assert "Meera Nair" not in str(row)
    assert "9000000014" not in str(row)
    assert row["result_bytes"] == len(secret.encode())


async def test_audit_failure_does_not_break_the_turn(monkeypatch, fake_db):
    """A broken audit sink must not fail a customer's request.

    Deliberate trade for a servicing assistant. A system that moved money
    would fail closed instead — worth saying out loud, not hiding.
    """

    class Exploding:
        async def insert_one(self, *_a, **_k):
            raise RuntimeError("mongo is down")

    class DB:
        tool_audit = Exploding()

    monkeypatch.setattr(mongo_module, "get_db", lambda: DB())

    # Must not raise.
    await audit.record_tool_call(
        session_id="s1", tool_name="get_customer", arguments={}, result="{}", latency_ms=1
    )


async def test_correlation_id_is_shared_within_a_scope(fake_db):
    cid = audit.new_correlation_id()
    for tool in ("get_customer", "list_kyc_documents"):
        await audit.record_tool_call(
            session_id="s2", tool_name=tool, arguments={}, result="{}", latency_ms=1
        )

    rows = await audit.read_session_trail("s2")
    assert {r["correlation_id"] for r in rows} == {cid}


# --------------------------------------------------------------------------- #
# End to end through the agent
# --------------------------------------------------------------------------- #


async def test_agent_tool_calls_are_audited(seeded):
    """Every call through the bridge lands in the trail, with real timings."""
    from src.agent.graph import agent_session

    audit.new_correlation_id()
    model = ScriptedChatModel(
        responses=[
            tool_call("list_kyc_documents", {"customer_id": "CUST-014"}),
            AIMessage(content="Your address proof was rejected."),
        ]
    )

    async with agent_session(model=model, checkpointer=InMemorySaver()) as agent:
        await agent.ainvoke(
            {"messages": [("user", "why blocked?")]},
            config={"configurable": {"thread_id": "sess-42"}},
        )

    trail = await audit.read_session_trail("sess-42")
    assert len(trail) == 1
    row = trail[0]
    assert row["tool_name"] == "list_kyc_documents"
    assert row["session_id"] == "sess-42"  # picked up from RunnableConfig
    assert row["arguments"] == {"customer_id": "CUST-014"}
    assert row["is_error"] is False
    assert row["latency_ms"] >= 0
    # The rejection reason came back in the result but must not be in the log.
    assert "blurred" not in str(row)


async def test_failed_tool_calls_are_audited_too(seeded, monkeypatch):
    """Failures are the calls you most want a record of."""
    from src.agent.graph import agent_session
    from src.db import repositories

    async def unavailable(*_args, **_kwargs):
        raise ConnectionError("mongo is down")

    # A tool that raises inside the MCP server — the shape of any genuine
    # infrastructure failure underneath a tool.
    monkeypatch.setattr(repositories, "find_documents", unavailable)

    model = ScriptedChatModel(
        responses=[
            tool_call("list_kyc_documents", {"customer_id": "CUST-014"}),
            AIMessage(content="I couldn't read those documents."),
        ]
    )

    async with agent_session(model=model, checkpointer=InMemorySaver()) as agent:
        await agent.ainvoke(
            {"messages": [("user", "recheck my doc")]},
            config={"configurable": {"thread_id": "sess-err"}},
        )

    trail = await audit.read_session_trail("sess-err")
    assert len(trail) == 1
    assert trail[0]["is_error"] is True
    assert trail[0]["error_message"]


async def test_trail_is_ordered_oldest_first(seeded):
    from src.agent.graph import agent_session

    model = ScriptedChatModel(
        responses=[
            tool_call("get_customer", {"customer_id": "CUST-014"}),
            tool_call("list_kyc_documents", {"customer_id": "CUST-014"}),
            AIMessage(content="Here's the position."),
        ]
    )

    async with agent_session(model=model, checkpointer=InMemorySaver()) as agent:
        await agent.ainvoke(
            {"messages": [("user", "status?")]},
            config={"configurable": {"thread_id": "sess-order"}},
        )

    trail = await audit.read_session_trail("sess-order")
    assert [r["tool_name"] for r in trail] == ["get_customer", "list_kyc_documents"]
