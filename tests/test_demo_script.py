"""Phase 6 — the demo script itself is under test.

`scripts/demo.py` is what a stranger runs first and what gets screen-shared in
an interview, so it is the worst possible thing to leave unverified: it breaks
silently, and you find out at the moment it matters. These tests drive all
three acts over an in-process ASGI transport with a scripted model, so the
whole narration path runs without Docker, Mongo, or an API key.

What this covers is the plumbing — endpoints, payload shapes, the ground-truth
queries, the printed output. What it cannot cover is model judgement, which is
exactly the split the demo's own closing message describes.
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

from scripts import demo
from src.agent.graph import agent_session
from src.db import mongo as mongo_module

NOW = datetime(2026, 7, 1, tzinfo=UTC)
CUSTOMER_ID = "CUST-014"


class ScriptedChatModel(FakeMessagesListChatModel):
    def bind_tools(self, tools: Any, **kwargs: Any):  # noqa: ARG002
        return self


def tool_call(name: str, args: dict) -> AIMessage:
    return AIMessage(
        content="", tool_calls=[{"name": name, "args": args, "id": "c1", "type": "tool_call"}]
    )


async def _noop():
    return None


@pytest.fixture
async def db(monkeypatch):
    database = AsyncMongoMockClient()["kyc_demo_test"]
    monkeypatch.setattr(mongo_module, "get_db", lambda: database)

    await database.customers.insert_one(
        {
            "customer_id": CUSTOMER_ID,
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
    await database.kyc_documents.insert_one(
        {
            "document_id": "DOC-014-1",
            "customer_id": CUSTOMER_ID,
            "doc_type": "address_proof",
            "status": "rejected",
            "rejection_reason": "Address proof is older than 3 months.",
            "submitted_at": NOW,
            "reviewed_at": NOW,
        }
    )
    await database.idempotency_keys.create_index("key", unique=True)
    return database


@asynccontextmanager
async def api(model, monkeypatch, db):  # noqa: ARG001 — db fixture ordering
    monkeypatch.setattr(mongo_module, "ensure_indexes", _noop)
    from src.api.main import app

    async with AsyncExitStack() as stack:
        app.state.agent = await stack.enter_async_context(
            agent_session(model=model, checkpointer=InMemorySaver())
        )
        transport = ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
            yield http


def demo_model() -> ScriptedChatModel:
    """One script covering every model turn the three acts trigger, in order.

    Act 1: read a tool, then answer.       Act 2: ask for a write, then confirm
    after approval.                        Act 3: refuse, calling nothing.
    """
    return ScriptedChatModel(
        responses=[
            # Act 1 — read path
            tool_call("list_kyc_documents", {"customer_id": CUSTOMER_ID}),
            AIMessage(
                content=(
                    "Your address proof was rejected because it is older than 3 months. "
                    "Submit one dated within the last 3 months to continue."
                )
            ),
            # Act 2 — write path (the tool call is interrupted before it runs)
            tool_call(
                "create_servicing_case",
                {
                    "customer_id": CUSTOMER_ID,
                    "category": "kyc",
                    "summary": "Rejected address proof — customer requested review",
                },
            ),
            AIMessage(content="A reviewer approved it and the case is now open."),
            # Act 3 — refusal
            AIMessage(
                content=(
                    "I can't move money — I only handle onboarding and KYC servicing. "
                    "For transfers, please use the banking app."
                )
            ),
        ]
    )


# --------------------------------------------------------------------------- #
# Ground truth helpers
# --------------------------------------------------------------------------- #


async def test_pick_customer_finds_one_with_a_rejected_document(db):
    """The demo chooses its subject from the data, so a reseed cannot break it."""
    customer_id, truth = await demo.pick_customer()

    assert customer_id == CUSTOMER_ID
    assert truth["document"]["status"] == "rejected"
    assert truth["customer"]["full_name"] == "Meera Nair"


async def test_pick_customer_says_what_to_do_when_unseeded(db):
    """An empty database must produce an instruction, not a stack trace."""
    await db.kyc_documents.delete_many({})

    with pytest.raises(SystemExit, match="scripts.seed"):
        await demo.pick_customer()


# --------------------------------------------------------------------------- #
# The acts
# --------------------------------------------------------------------------- #


async def test_all_three_acts_pass_end_to_end(monkeypatch, db, capsys):
    async with api(demo_model(), monkeypatch, db) as http:
        passed = await demo.run_acts(http)

    assert passed, capsys.readouterr().out

    # The demo's claims must correspond to real state, not just printed text.
    assert await db.servicing_cases.count_documents({"customer_id": CUSTOMER_ID}) == 1
    assert await db.tool_audit.count_documents({"session_id": demo.SESSION_READ}) >= 1


async def test_the_write_act_reports_the_approver_from_the_audit_trail(monkeypatch, db):
    async with api(demo_model(), monkeypatch, db) as http:
        await demo.run_acts(http)

    row = await db.tool_audit.find_one({"tool_name": "create_servicing_case"})
    assert row["approved_by"] == demo.APPROVER


async def test_a_failed_check_is_reported_rather_than_swallowed(monkeypatch, db, capsys):
    """The demo must be capable of failing.

    A script that prints ✓ unconditionally proves nothing. Here the model
    answers act 1 with no tool call at all, which is precisely the ungrounded
    behaviour the first check exists to catch.
    """
    ungrounded = ScriptedChatModel(
        responses=[AIMessage(content="Your document was rejected, I think.")] * 5
    )

    async with api(ungrounded, monkeypatch, db) as http:
        passed = await demo.run_acts(http)

    assert passed is False
    assert "✗" in capsys.readouterr().out


async def test_narration_never_prints_a_raw_contact_value(monkeypatch, db, capsys):
    """Redaction has to hold on the screen-shared path too.

    The audit rows the demo prints go through the same redaction as everything
    else; this asserts it end to end, because a demo is the one context where
    the output is guaranteed to be looked at by someone else.
    """
    model = ScriptedChatModel(
        responses=[
            tool_call(
                "update_customer_contact",
                {"customer_id": CUSTOMER_ID, "field": "email", "value": "private@example.invalid"},
            ),
            AIMessage(content="Updated."),
        ]
        * 3
    )

    async with api(model, monkeypatch, db) as http:
        await demo.run_acts(http)

    assert "private@example.invalid" not in capsys.readouterr().out
