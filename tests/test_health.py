"""Phase 0 smoke test — the service starts and reports health."""

import httpx
import pytest
from httpx import ASGITransport

from src.api.main import app


@pytest.mark.asyncio
async def test_healthz_returns_ok_shape():
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/healthz")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] in {"ok", "degraded"}
    assert "agent_runtime" in body
