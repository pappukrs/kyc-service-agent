"""FastAPI entrypoint — request intake, session routing, health, metrics.

Phase 0 gives you a working service with health + metrics. The chat and
approval endpoints are stubbed with their contracts defined; fill them in at
Phase 3 and Phase 5.
"""

from contextlib import AsyncExitStack, asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel
from starlette.responses import Response

from src.agent.checkpoint import checkpointer
from src.agent.graph import agent_session
from src.config import get_settings
from src.db import mongo
from src.obs import audit

settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Own the MCP session and the checkpointer for the process lifetime.

    The agent's tools close over a live MCP client, so the session has to
    outlive a single request — building an agent per request would reconnect
    MCP on every message. One session, opened at startup, closed at shutdown.
    """
    await mongo.ensure_indexes()

    async with AsyncExitStack() as stack:
        saver = await stack.enter_async_context(checkpointer())
        app.state.agent = await stack.enter_async_context(agent_session(checkpointer=saver))
        yield


app = FastAPI(
    title="KYC Servicing Agent",
    description="AI-assisted servicing for retail-banking onboarding and KYC (synthetic data).",
    version="0.1.0",
    lifespan=lifespan,
)


# --------------------------------------------------------------------------- #
# Health & metrics
# --------------------------------------------------------------------------- #


@app.get("/healthz", tags=["ops"])
async def healthz() -> dict:
    mongo_ok = await mongo.ping()
    return {
        "status": "ok" if mongo_ok else "degraded",
        "mongo": "up" if mongo_ok else "down",
        "agent_runtime": settings.agent_runtime,
    }


@app.get("/metrics", tags=["ops"])
async def metrics() -> Response:
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


# --------------------------------------------------------------------------- #
# Read endpoints (Phase 1)
# --------------------------------------------------------------------------- #


@app.get("/customers/{customer_id}", tags=["customers"])
async def get_customer(customer_id: str) -> dict:
    doc = await mongo.get_db().customers.find_one({"customer_id": customer_id}, {"_id": 0})
    if not doc:
        raise HTTPException(status_code=404, detail=f"No customer {customer_id}")
    return doc


# --------------------------------------------------------------------------- #
# Agent endpoints
# --------------------------------------------------------------------------- #


class MessageIn(BaseModel):
    message: str


class ApprovalIn(BaseModel):
    approve: bool
    approver: str
    reason: str | None = None


def _approval_envelope(interrupts: list[Any]) -> dict:
    """Render a paused write as something a caller can act on.

    Deliberately explicit that nothing has happened yet — the most damaging
    thing this API could do is let a client render "case created" for a write
    still sitting in a queue awaiting a human.
    """
    requests = [req for interrupt in interrupts for req in interrupt.value["action_requests"]]
    return {
        "status": "awaiting_approval",
        "message": (
            "This action changes customer records and is waiting for a human reviewer. "
            "Nothing has been written yet."
        ),
        "pending_actions": [{"tool": r["name"], "arguments": r["args"]} for r in requests],
    }


@app.post("/sessions/{session_id}/messages", tags=["agent"])
async def send_message(session_id: str, body: MessageIn) -> dict:
    """Send a servicing request to the agent.

    Returns either the agent's answer, or a pending-approval envelope when the
    agent has asked to call a write tool. The write does not execute until
    POST /sessions/{id}/approve.
    """
    # One correlation id for this request; every tool call the agent makes
    # while handling it shares the id, so the trail can be grouped by turn as
    # well as by session.
    correlation_id = audit.new_correlation_id()

    result = await app.state.agent.ainvoke(
        {"messages": [("user", body.message)]},
        config={"configurable": {"thread_id": session_id}},
    )

    if interrupts := result.get("__interrupt__"):
        return _approval_envelope(interrupts) | {"correlation_id": correlation_id}

    return {
        "status": "ok",
        "reply": result["messages"][-1].content,
        "correlation_id": correlation_id,
        # Convenience for the caller and for debugging. The authoritative
        # record is the tool_audit collection — see GET .../audit below.
        "tools_called": [
            call["name"]
            for message in result["messages"]
            for call in (getattr(message, "tool_calls", None) or [])
        ],
    }


@app.get("/sessions/{session_id}/audit", tags=["agent"])
async def session_audit(session_id: str) -> dict:
    """Every tool call made in this session, oldest first.

    The reconstruction path: this is what you show someone asking "why did it
    tell the customer that?". Results are hashed, not stored, and personal-data
    arguments are redacted — the trail proves what happened without becoming a
    second copy of the customer database.
    """
    trail = await audit.read_session_trail(session_id)
    return {"session_id": session_id, "tool_calls": len(trail), "trail": trail}


@app.post("/sessions/{session_id}/approve", tags=["agent"])
async def approve(session_id: str, body: ApprovalIn) -> dict:
    """Approve or deny a pending write, then resume the agent."""
    # TODO(Phase 5): resume the interrupted graph; record approver in tool_audit.
    raise HTTPException(status_code=501, detail="Phase 5: approval flow not implemented yet")
