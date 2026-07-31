"""FastAPI entrypoint — request intake, session routing, health, metrics.

Phase 0 gives you a working service with health + metrics. The chat and
approval endpoints are stubbed with their contracts defined; fill them in at
Phase 3 and Phase 5.
"""

from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel
from starlette.responses import Response

from src.config import get_settings
from src.db import mongo

settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    await mongo.ensure_indexes()
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


@app.post("/sessions/{session_id}/messages", tags=["agent"])
async def send_message(session_id: str, body: MessageIn) -> dict:
    """Send a servicing request to the agent.

    Returns either the agent's answer, or a pending-approval envelope when the
    agent wants to call a write tool:

        {"status": "awaiting_approval",
         "pending_tool": "create_servicing_case",
         "arguments": {...}}
    """
    # TODO(Phase 3): run the LangGraph agent, persist conversation state.
    # TODO(Phase 5): surface interrupt() as the awaiting_approval envelope.
    raise HTTPException(status_code=501, detail="Phase 3: agent loop not implemented yet")


@app.post("/sessions/{session_id}/approve", tags=["agent"])
async def approve(session_id: str, body: ApprovalIn) -> dict:
    """Approve or deny a pending write, then resume the agent."""
    # TODO(Phase 5): resume the interrupted graph; record approver in tool_audit.
    raise HTTPException(status_code=501, detail="Phase 5: approval flow not implemented yet")
