"""Phase 8 — time bounds and retries on tool calls.

Two things are being asserted here, and they are deliberately asymmetric:

*Every* tool call is bounded, so no tool can hold a customer's turn open
indefinitely. Only *reads* are retried, because a write that timed out may
already have committed — and the agent has to be told that, rather than being
allowed to pick a story.

Timeouts are set in the tens of milliseconds throughout. A resilience suite that
takes 45 seconds to prove a 15-second bound is a suite nobody runs.
"""

import asyncio
import json
from datetime import UTC, datetime
from typing import Any

import pytest
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver
from mongomock_motor import AsyncMongoMockClient

from src.agent import resilience
from src.agent.mcp_tools import connect, load_mcp_tools
from src.agent.resilience import ToolPolicy, call_with_policy, policy_for
from src.config import get_settings
from src.db import mongo as mongo_module
from src.db import repositories
from src.mcp_server.server import ALL_TOOLS, READ_TOOLS, WRITE_TOOLS, mcp
from src.obs import audit

NOW = datetime(2026, 7, 1, tzinfo=UTC)

CONFIG = {"configurable": {"thread_id": "sess-resilience"}}


@pytest.fixture(autouse=True)
def fast_bounds(monkeypatch):
    """Real policies, sub-second numbers.

    Overriding the settings rather than hand-building policies keeps the
    production wiring — `policy_for` — inside the test rather than beside it.
    """
    settings = get_settings().model_copy(
        update={
            "tool_timeout_seconds": 0.05,
            "tool_deadline_seconds": 0.5,
            "tool_retry_attempts": 3,
            "tool_retry_backoff_seconds": 0.01,
        }
    )
    monkeypatch.setattr(resilience, "get_settings", lambda: settings)
    return settings


@pytest.fixture(autouse=True)
def fake_db(monkeypatch):
    db = AsyncMongoMockClient()["kyc_resilience_test"]
    monkeypatch.setattr(mongo_module, "get_db", lambda: db)
    return db


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


def hangs(seconds: float = 30.0):
    """A repository call that never usefully returns."""

    async def _hang(*_args, **_kwargs):
        await asyncio.sleep(seconds)
        raise AssertionError("the timeout did not fire")

    return _hang


# --------------------------------------------------------------------------- #
# The policy itself
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("tool_name", READ_TOOLS)
def test_reads_are_retried(tool_name):
    assert policy_for(tool_name).attempts == 3


@pytest.mark.parametrize("tool_name", [*WRITE_TOOLS, "verify_kyc_document"])
def test_writes_and_enqueues_are_attempted_once(tool_name):
    """A timed-out write may already have committed; a second attempt would be a
    guess about which. See the module docstring in src/agent/resilience.py."""
    assert policy_for(tool_name).attempts == 1


def test_every_tool_is_bounded():
    """Whatever the classification says, nothing gets an unbounded call."""
    assert all(policy_for(name).timeout_seconds > 0 for name in ALL_TOOLS)


async def test_classification_covers_the_whole_tool_surface():
    """The constants and the server cannot drift apart unnoticed.

    A tool added to the server but not classified would silently inherit the
    write policy — safe, but by accident rather than by decision.
    """
    async with connect(mcp) as client:
        advertised = {tool.name for tool in (await client.list_tools()).tools}

    assert advertised == set(ALL_TOOLS)


# --------------------------------------------------------------------------- #
# The retry loop
# --------------------------------------------------------------------------- #

FAST = ToolPolicy(attempts=3, timeout_seconds=0.05, deadline_seconds=0.5, backoff_seconds=0.01)
ONCE = ToolPolicy(attempts=1, timeout_seconds=0.05, deadline_seconds=0.5, backoff_seconds=0.01)


async def test_success_is_returned_on_the_first_attempt():
    outcome = await call_with_policy(FAST, lambda: asyncio.sleep(0, result="fine"))
    assert outcome.ok and outcome.result == "fine"
    assert outcome.attempts == 1


async def test_a_transient_failure_is_retried():
    calls = 0

    async def flaky():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ConnectionError("mongo blinked")
        return "second time lucky"

    outcome = await call_with_policy(FAST, flaky)
    assert outcome.ok and outcome.result == "second time lucky"
    assert outcome.attempts == 2


async def test_a_hanging_call_is_cut_off_at_the_per_attempt_timeout():
    started = asyncio.get_running_loop().time()
    outcome = await call_with_policy(ONCE, lambda: asyncio.sleep(30))
    elapsed = asyncio.get_running_loop().time() - started

    assert not outcome.ok and outcome.timed_out
    assert outcome.attempts == 1
    assert elapsed < 1.0, "the call was not bounded"


async def test_exhaustion_is_returned_not_raised():
    """Failure has to arrive as something the agent can read."""
    outcome = await call_with_policy(FAST, lambda: asyncio.sleep(30))
    assert outcome.attempts == 3
    assert isinstance(outcome.error, TimeoutError)


async def test_the_deadline_caps_the_total_wait():
    """Retries must not multiply the wait they were meant to bound.

    Per-attempt 0.2s × 5 attempts would be a second; the deadline stops it at
    roughly 0.3s, so some of those attempts never get to run.
    """
    policy = ToolPolicy(attempts=5, timeout_seconds=0.2, deadline_seconds=0.3, backoff_seconds=0.01)

    started = asyncio.get_running_loop().time()
    outcome = await call_with_policy(policy, lambda: asyncio.sleep(30))
    elapsed = asyncio.get_running_loop().time() - started

    assert outcome.attempts < policy.attempts, "the deadline did not stop the retries"
    assert elapsed < 0.6


async def test_a_single_attempt_policy_does_not_retry():
    calls = 0

    async def failing():
        nonlocal calls
        calls += 1
        raise ConnectionError("down")

    outcome = await call_with_policy(ONCE, failing)
    assert calls == 1
    assert not outcome.ok


# --------------------------------------------------------------------------- #
# What the agent is told
# --------------------------------------------------------------------------- #


async def test_failure_payload_does_not_leak_the_exception_message():
    """Exception strings carry connection strings, queries, sometimes customer
    data — and this payload goes into the model's context and the transcript."""
    outcome = resilience.Outcome(
        result=None,
        attempts=1,
        error=ConnectionError("mongodb://user:hunter2@10.0.0.4/kyc_servicing timed out"),
        timed_out=False,
    )

    payload = resilience.failure_payload("get_customer", outcome, elapsed_ms=12)

    assert payload["detail"] == "ConnectionError"
    assert "hunter2" not in json.dumps(payload)


async def test_a_failed_read_tells_the_agent_it_knows_nothing():
    outcome = resilience.Outcome(None, 3, TimeoutError(), True)
    message = resilience.failure_payload("list_kyc_documents", outcome, elapsed_ms=1)["message"]

    assert "do not have this information" in message
    assert "Do not guess" in message


async def test_a_failed_write_is_reported_as_unconfirmed():
    """The important one. After a timed-out write the agent knows less than
    nothing: not whether the customer's record changed."""
    outcome = resilience.Outcome(None, 1, TimeoutError(), True)
    message = resilience.failure_payload("create_servicing_case", outcome, elapsed_ms=1)["message"]

    assert "may or may not have taken effect" in message
    assert "not tell the customer it succeeded" in message.replace("Do NOT", "not")
    assert "Do not retry" in message


# --------------------------------------------------------------------------- #
# Through the bridge — policy, audit row and tool result together
# --------------------------------------------------------------------------- #


async def call_tool(name: str, **kwargs: Any) -> str:
    """Invoke one bridged tool the way the agent's tool node would."""
    async with connect(mcp) as client:
        tools = {tool.name: tool for tool in await load_mcp_tools(client)}
        return await tools[name].ainvoke(kwargs, config=CONFIG)


async def test_a_hanging_read_reaches_the_agent_as_a_timeout(seeded, monkeypatch):
    audit.new_correlation_id()
    monkeypatch.setattr(repositories, "find_documents", hangs())

    rendered = await call_tool("list_kyc_documents", customer_id="CUST-014")

    assert rendered.startswith("TOOL_ERROR from list_kyc_documents")
    payload = json.loads(rendered.split(": ", 1)[1])
    assert payload["error"] == "tool_timeout"
    assert payload["attempts"] == 3


async def test_a_timed_out_call_is_audited_as_one_row_carrying_the_attempts(seeded, monkeypatch):
    """One tool call, one row — otherwise a reader of the trail cannot tell a
    retry from the agent asking the same question three times."""
    audit.new_correlation_id()
    monkeypatch.setattr(repositories, "find_documents", hangs())

    await call_tool("list_kyc_documents", customer_id="CUST-014")

    trail = await audit.read_session_trail("sess-resilience")
    assert len(trail) == 1
    assert trail[0]["attempts"] == 3
    assert trail[0]["timed_out"] is True
    assert trail[0]["is_error"] is True


async def test_a_read_that_recovers_returns_real_data(seeded, monkeypatch):
    calls = 0
    real = repositories.find_documents

    async def hangs_once(db, customer_id):
        nonlocal calls
        calls += 1
        if calls == 1:
            await asyncio.sleep(30)
        return await real(db, customer_id)

    monkeypatch.setattr(repositories, "find_documents", hangs_once)
    audit.new_correlation_id()

    rendered = await call_tool("list_kyc_documents", customer_id="CUST-014")

    assert "blurred" in rendered, "the retry did not produce the customer's real documents"
    assert not rendered.startswith("TOOL_ERROR")

    trail = await audit.read_session_trail("sess-resilience")
    assert len(trail) == 1
    assert trail[0]["attempts"] == 2
    assert trail[0]["is_error"] is False


async def test_a_hanging_write_is_attempted_once_and_reported_as_unconfirmed(seeded, monkeypatch):
    monkeypatch.setattr(repositories, "create_case", hangs())
    audit.new_correlation_id()

    rendered = await call_tool(
        "create_servicing_case",
        customer_id="CUST-014",
        category="kyc",
        summary="escalate",
    )

    payload = json.loads(rendered.split(": ", 1)[1])
    assert payload["attempts"] == 1, "a write must not be retried"
    assert "may or may not have taken effect" in payload["message"]

    trail = await audit.read_session_trail("sess-resilience")
    assert trail[0]["attempts"] == 1
    assert trail[0]["timed_out"] is True


# --------------------------------------------------------------------------- #
# The turn-level bound
# --------------------------------------------------------------------------- #


class ScriptedChatModel(FakeMessagesListChatModel):
    def bind_tools(self, tools: Any, **kwargs: Any):  # noqa: ARG002
        return self


def tool_call(name: str, args: dict, call_id: str = "c1") -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[{"name": name, "args": args, "id": call_id, "type": "tool_call"}],
    )


async def test_a_turn_cannot_call_tools_forever(seeded, monkeypatch):
    """A model that keeps calling tools is the same failure as a tool that never
    returns: the customer gets no answer. The cap is soft — the model is told to
    stop and answer from what it has, rather than being handed a framework
    string to read out."""
    from src.agent import graph

    capped = get_settings().model_copy(update={"max_tool_calls_per_turn": 1})
    monkeypatch.setattr(graph, "get_settings", lambda: capped)

    model = ScriptedChatModel(
        responses=[
            tool_call("get_customer", {"customer_id": "CUST-014"}, "c1"),
            tool_call("list_kyc_documents", {"customer_id": "CUST-014"}, "c2"),
            AIMessage(content="Here is what I have."),
        ]
    )

    async with graph.agent_session(model=model, checkpointer=InMemorySaver()) as agent:
        result = await agent.ainvoke(
            {"messages": [("user", "status?")]},
            config={"configurable": {"thread_id": "sess-capped"}},
        )

    tool_messages = [m for m in result["messages"] if m.type == "tool"]
    blocked = [m for m in tool_messages if "limit exceeded" in m.content.lower()]

    assert len(blocked) == 1, "the second tool call was not blocked"
    # Only the allowed call actually ran, so only it is in the trail.
    trail = await audit.read_session_trail("sess-capped")
    assert [row["tool_name"] for row in trail] == ["get_customer"]
