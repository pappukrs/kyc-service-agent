"""Phase 4 — conversation state across agent instances.

The checkpointer is what lets a session outlive the agent object holding it.
That matters twice over: a follow-up question should not lose its context, and
— more importantly — a graph paused at an approval must be resumable by a
different process than the one that paused it. Without persistence, restarting
the API would silently drop every write awaiting a human decision.

These use InMemorySaver held *outside* the agent, which is exactly the
restart scenario in miniature: new agent, same store.
"""

from datetime import UTC, datetime
from typing import Any

import pytest
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver
from mongomock_motor import AsyncMongoMockClient

from src.db import mongo as mongo_module

NOW = datetime(2026, 7, 1, tzinfo=UTC)


@pytest.fixture(autouse=True)
def fake_db(monkeypatch):
    db = AsyncMongoMockClient()["kyc_state_test"]
    monkeypatch.setattr(mongo_module, "get_db", lambda: db)
    return db


@pytest.fixture(autouse=True)
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
    return fake_db


class ScriptedChatModel(FakeMessagesListChatModel):
    def bind_tools(self, tools: Any, **kwargs: Any):  # noqa: ARG002
        return self


async def test_history_survives_a_new_agent_instance():
    """Two agents, one store: the second sees the first's conversation."""
    from src.agent.graph import agent_session

    saver = InMemorySaver()
    config = {"configurable": {"thread_id": "sess-persist"}}

    async with agent_session(
        model=ScriptedChatModel(responses=[AIMessage(content="Hello, how can I help?")]),
        checkpointer=saver,
    ) as agent:
        await agent.ainvoke({"messages": [("user", "Hi, I'm CUST-014.")]}, config)

    # The first agent (and its MCP session) is gone — stand up a fresh one.
    async with agent_session(
        model=ScriptedChatModel(responses=[AIMessage(content="You mentioned CUST-014.")]),
        checkpointer=saver,
    ) as agent:
        result = await agent.ainvoke({"messages": [("user", "What was my id again?")]}, config)

    contents = [m.content for m in result["messages"]]
    assert "Hi, I'm CUST-014." in contents, "earlier turn was lost"
    assert len(result["messages"]) >= 4  # two turns, both sides


async def test_separate_sessions_do_not_share_history():
    """Thread isolation — one customer must never see another's conversation."""
    from src.agent.graph import agent_session

    saver = InMemorySaver()

    async with agent_session(
        model=ScriptedChatModel(responses=[AIMessage(content="ok")]), checkpointer=saver
    ) as agent:
        await agent.ainvoke(
            {"messages": [("user", "My id is CUST-014 and my phone is +91 9000000014.")]},
            {"configurable": {"thread_id": "customer-a"}},
        )
        result = await agent.ainvoke(
            {"messages": [("user", "Hello?")]},
            {"configurable": {"thread_id": "customer-b"}},
        )

    joined = " ".join(str(m.content) for m in result["messages"])
    assert "CUST-014" not in joined
    assert "9000000014" not in joined


async def test_paused_write_is_resumable_by_a_new_agent():
    """The reason persistence is load-bearing, not a nicety.

    A write parked for human approval has to survive the process that parked
    it — otherwise a restart drops every pending decision on the floor.
    """
    from src.agent.graph import agent_session

    saver = InMemorySaver()
    config = {"configurable": {"thread_id": "sess-pending"}}

    pending_write = AIMessage(
        content="",
        tool_calls=[
            {
                "name": "create_servicing_case",
                "args": {"customer_id": "CUST-014", "category": "kyc", "summary": "escalate"},
                "id": "c1",
                "type": "tool_call",
            }
        ],
    )

    async with agent_session(
        model=ScriptedChatModel(responses=[pending_write]), checkpointer=saver
    ) as agent:
        result = await agent.ainvoke({"messages": [("user", "escalate please")]}, config)
        assert "__interrupt__" in result

    # Fresh agent, same store — the pending approval is still there.
    async with agent_session(
        model=ScriptedChatModel(responses=[AIMessage(content="done")]), checkpointer=saver
    ) as agent:
        state = await agent.aget_state(config)

    assert state.next, "the paused write did not survive the new agent instance"
    assert state.interrupts, "no pending approval found after restart"
    requests = state.interrupts[0].value["action_requests"]
    assert requests[0]["name"] == "create_servicing_case"
