"""Phase 3 — the chat endpoint, end to end through the real agent + MCP stack.

Only the model is faked. The request goes through FastAPI → agent → MCP →
repository → in-memory Mongo and back, so the wiring is genuinely exercised.
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

NOW = datetime(2026, 7, 1, tzinfo=UTC)


class ScriptedChatModel(FakeMessagesListChatModel):
    def bind_tools(self, tools: Any, **kwargs: Any):  # noqa: ARG002
        return self


def tool_call(name: str, args: dict) -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[{"name": name, "args": args, "id": "call_1", "type": "tool_call"}],
    )


@asynccontextmanager
async def client_with(model, monkeypatch):
    """Build an ASGI client whose app is wired to a scripted model + fake Mongo."""
    db = AsyncMongoMockClient()["kyc_api_test"]
    monkeypatch.setattr(mongo_module, "get_db", lambda: db)
    monkeypatch.setattr(mongo_module, "ensure_indexes", lambda: _noop())

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
    await db.kyc_documents.insert_one(
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


@pytest.mark.asyncio
async def test_message_endpoint_answers_from_tool_data(monkeypatch):
    model = ScriptedChatModel(
        responses=[
            tool_call("list_kyc_documents", {"customer_id": "CUST-014"}),
            AIMessage(content="Your address proof was rejected: the image was blurred."),
        ]
    )

    async with client_with(model, monkeypatch) as http:
        resp = await http.post("/sessions/s1/messages", json={"message": "Why am I blocked?"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert "blurred" in body["reply"]
    assert body["tools_called"] == ["list_kyc_documents"]


@pytest.mark.asyncio
async def test_message_endpoint_returns_approval_envelope_for_writes(monkeypatch):
    """A pending write must never be reported as done."""
    model = ScriptedChatModel(
        responses=[
            tool_call(
                "create_servicing_case",
                {"customer_id": "CUST-014", "category": "kyc", "summary": "escalation"},
            ),
            AIMessage(content="Sent for review."),
        ]
    )

    async with client_with(model, monkeypatch) as http:
        resp = await http.post("/sessions/s2/messages", json={"message": "Please escalate."})

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "awaiting_approval"
    assert body["pending_actions"] == [
        {
            "tool": "create_servicing_case",
            "arguments": {
                "customer_id": "CUST-014",
                "category": "kyc",
                "summary": "escalation",
            },
        }
    ]
    assert "Nothing has been written yet" in body["message"]
    # No answer field — the caller must not be able to render this as a result.
    assert "reply" not in body
