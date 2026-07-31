"""MongoDB access.

Collections:
    customers          — synthetic customer profiles
    kyc_documents      — documents with status + rejection reason
    servicing_cases    — cases opened by the agent (after human approval)
    conversations      — agent session state
    tool_audit         — APPEND-ONLY record of every tool invocation
    idempotency_keys   — write-tool dedupe
"""

from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase

from src.config import get_settings

_client: AsyncIOMotorClient | None = None


def get_client() -> AsyncIOMotorClient:
    global _client
    if _client is None:
        _client = AsyncIOMotorClient(get_settings().mongo_uri)
    return _client


def get_db() -> AsyncIOMotorDatabase:
    return get_client()[get_settings().mongo_db]


async def ensure_indexes() -> None:
    """Idempotent — safe to call on every startup."""
    db = get_db()
    await db.customers.create_index("customer_id", unique=True)
    await db.kyc_documents.create_index("document_id", unique=True)
    await db.kyc_documents.create_index("customer_id")
    await db.servicing_cases.create_index("case_id", unique=True)
    await db.servicing_cases.create_index("customer_id")
    await db.conversations.create_index("session_id", unique=True)
    await db.tool_audit.create_index([("session_id", 1), ("created_at", 1)])
    await db.idempotency_keys.create_index("key", unique=True)


async def ping() -> bool:
    try:
        await get_client().admin.command("ping")
        return True
    except Exception:
        return False
