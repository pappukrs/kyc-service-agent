"""Phase 3 — the agent loop, driven by a fake model.

No API key, no network, no Docker. A scripted fake chat model emits the
tool_calls a real model would, which lets us assert on the mechanics we
actually care about: that the loop runs, that tools are reached through MCP,
and that write tools are interrupted before they execute.

Model *judgement* (did it pick the right tool for this question?) is not
testable this way — that is what the Phase 9 eval suite is for, and it needs a
real model. These tests cover the wiring; the evals cover the behaviour.
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


class ScriptedChatModel(FakeMessagesListChatModel):
    """A fake model that replays a fixed script of assistant turns.

    The stock FakeMessagesListChatModel raises NotImplementedError on
    bind_tools, so an agent cannot be built from it. Overriding bind_tools to
    accept-and-ignore is what we want here: the script decides which tools get
    called, not the model, which is the whole point of a deterministic test.
    """

    def bind_tools(self, tools: Any, **kwargs: Any):  # noqa: ARG002
        return self


@pytest.fixture(autouse=True)
def fake_db(monkeypatch):
    """Point the MCP tools at an in-memory Mongo for the whole module."""
    db = AsyncMongoMockClient()["kyc_agent_test"]
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


def scripted_model(*messages: AIMessage) -> ScriptedChatModel:
    return ScriptedChatModel(responses=list(messages))


def tool_call(name: str, args: dict, call_id: str = "call_1") -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[{"name": name, "args": args, "id": call_id, "type": "tool_call"}],
    )


# --------------------------------------------------------------------------- #
# Tool binding
# --------------------------------------------------------------------------- #


async def test_agent_binds_all_seven_mcp_tools(seeded):
    from src.agent.graph import agent_session

    async with agent_session(model=scripted_model(AIMessage(content="done"))) as agent:
        # The tools node is where the bound tools live.
        bound = agent.nodes["tools"].bound
        names = {t.name for t in bound.tools_by_name.values()}

    assert names == {
        "get_customer",
        "get_onboarding_status",
        "list_kyc_documents",
        "search_servicing_kb",
        "verify_kyc_document",
        "create_servicing_case",
        "update_customer_contact",
    }


async def test_read_only_agent_has_no_write_tools(seeded):
    """An agent never given a write tool cannot be prompted into calling one."""
    from src.agent.graph import WRITE_TOOLS, agent_session

    async with agent_session(
        model=scripted_model(AIMessage(content="done")), read_only=True
    ) as agent:
        names = {t.name for t in agent.nodes["tools"].bound.tools_by_name.values()}

    assert names.isdisjoint(WRITE_TOOLS)
    assert len(names) == 4


# --------------------------------------------------------------------------- #
# The loop
# --------------------------------------------------------------------------- #


async def test_loop_calls_tool_and_returns_its_data(seeded):
    """plan → call tool → observe → answer, with real data flowing through MCP."""
    from src.agent.graph import agent_session

    model = scripted_model(
        tool_call("list_kyc_documents", {"customer_id": "CUST-014"}),
        AIMessage(content="Your address proof was rejected because the image was blurred."),
    )

    async with agent_session(model=model) as agent:
        result = await agent.ainvoke(
            {"messages": [("user", "Why is CUST-014 blocked?")]},
            config={"configurable": {"thread_id": "t1"}},
        )

    kinds = [m.type for m in result["messages"]]
    assert "tool" in kinds, "the tool node never ran"

    tool_msg = next(m for m in result["messages"] if m.type == "tool")
    # The real rejection reason came back through MCP → repository → Mongo.
    assert "blurred" in tool_msg.content


async def test_unknown_customer_reaches_agent_as_structured_not_found(seeded):
    """A missing record must arrive as data the agent can act on, not an exception."""
    from src.agent.graph import agent_session

    model = scripted_model(
        tool_call("get_customer", {"customer_id": "CUST-999"}),
        AIMessage(content="I couldn't find that customer id."),
    )

    async with agent_session(model=model) as agent:
        result = await agent.ainvoke(
            {"messages": [("user", "Look up CUST-999")]},
            config={"configurable": {"thread_id": "t2"}},
        )

    tool_msg = next(m for m in result["messages"] if m.type == "tool")
    assert "customer_not_found" in tool_msg.content
    assert "Do not guess" in tool_msg.content


# --------------------------------------------------------------------------- #
# The write guard — the security-relevant test in this file
# --------------------------------------------------------------------------- #


async def test_write_tool_is_interrupted_before_it_runs(seeded):
    """The graph must halt *before* a write executes, not after.

    This is the structural guarantee behind the approval gate: even a model
    that decides to open a case cannot cause a write on its own.
    """
    from src.agent.graph import agent_session

    model = scripted_model(
        tool_call(
            "create_servicing_case",
            {"customer_id": "CUST-014", "category": "kyc", "summary": "escalation"},
        ),
        AIMessage(content="I've sent that for review."),
    )
    config = {"configurable": {"thread_id": "t3"}}

    async with agent_session(model=model, checkpointer=InMemorySaver()) as agent:
        result = await agent.ainvoke({"messages": [("user", "Please escalate this.")]}, config)

        # The write never executed: the model asked for the tool, but no tool
        # message came back.
        assert not any(m.type == "tool" for m in result["messages"])

        # An interrupt is pending, carrying what the API needs to render an
        # approval envelope: which tool, and with what arguments.
        assert "__interrupt__" in result, "expected the graph to pause for approval"
        requests = result["__interrupt__"][0].value["action_requests"]
        assert [r["name"] for r in requests] == ["create_servicing_case"]
        assert requests[0]["args"]["customer_id"] == "CUST-014"

        # The graph is parked mid-run, not finished — it can be resumed with a
        # decision. (Asserting on truthiness rather than the middleware's node
        # name, which is a library internal.)
        state = await agent.aget_state(config)
        assert state.next, "graph should be paused, not complete"


async def test_read_tool_is_not_interrupted(seeded):
    """The gate must be specific to writes — reads should flow straight through."""
    from src.agent.graph import agent_session

    model = scripted_model(
        tool_call("get_onboarding_status", {"customer_id": "CUST-014"}),
        AIMessage(content="You have one outstanding document."),
    )

    async with agent_session(model=model, checkpointer=InMemorySaver()) as agent:
        result = await agent.ainvoke(
            {"messages": [("user", "What's my status?")]},
            config={"configurable": {"thread_id": "t4"}},
        )

    assert any(m.type == "tool" for m in result["messages"])
    assert result["messages"][-1].content == "You have one outstanding document."
