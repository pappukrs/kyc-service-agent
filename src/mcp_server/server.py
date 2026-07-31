"""MCP server — the ONLY path between the agent and banking data.

Seven tools, split by blast radius:

    read   get_customer, get_onboarding_status, list_kyc_documents, search_servicing_kb
    async  verify_kyc_document          (enqueues Kafka work, returns immediately)
    write  create_servicing_case, update_customer_contact   ← human approval required

There is deliberately NO money-movement tool. No transfers, no payments, no
balance mutation. An agent that can move money is a different risk conversation;
the scope here is servicing, and the tool surface enforces that rather than
relying on the prompt to.

Note on docstrings: each tool's description says **when to call it**, not just
what it does. That is the single highest-leverage lever on tool-selection
accuracy — the model reads these to decide.

Run standalone:  python -m src.mcp_server.server
"""

from mcp.server.fastmcp import FastMCP

from src.db import mongo

mcp = FastMCP("kyc-servicing")


# --------------------------------------------------------------------------- #
# Read tools
# --------------------------------------------------------------------------- #


@mcp.tool()
async def get_customer(customer_id: str) -> dict:
    """Fetch a customer's profile, onboarding stage, and risk tier.

    Call this first whenever a request concerns a specific customer and you do
    not yet have their details in context. Prefer get_onboarding_status when
    the question is specifically about progress or blockers.
    """
    doc = await mongo.get_db().customers.find_one({"customer_id": customer_id}, {"_id": 0})
    return doc or {"error": "not_found", "customer_id": customer_id}


@mcp.tool()
async def get_onboarding_status(customer_id: str) -> dict:
    """Get a customer's onboarding stage plus the list of outstanding blockers.

    Call this when the customer asks how far along their application is, why it
    is taking time, or what they still need to do. If they ask specifically
    about a *document*, use list_kyc_documents instead — it carries the
    rejection reasons.
    """
    # TODO(Phase 2): derive blockers from stage + pending/rejected documents.
    raise NotImplementedError("Phase 2")


@mcp.tool()
async def list_kyc_documents(customer_id: str) -> list[dict]:
    """List a customer's KYC documents with status and rejection reason.

    Call this whenever the customer asks why a document was rejected, why their
    onboarding is blocked, or what they still need to submit. This is the tool
    that carries rejection_reason — the usual answer to "why am I stuck?".
    """
    # TODO(Phase 2): query kyc_documents by customer_id.
    raise NotImplementedError("Phase 2")


@mcp.tool()
async def search_servicing_kb(query: str) -> list[dict]:
    """Search internal servicing policy and FAQ content.

    Call this for questions about process, policy, or timelines that are not
    specific to one customer's record — "how long does verification take?",
    "which address proofs are accepted?". Do not use it to look up customer data.
    """
    # TODO(Phase 2): keyword search over a seeded kb collection.
    # Deliberately not a vector store — this is an agent project, not a RAG project.
    raise NotImplementedError("Phase 2")


# --------------------------------------------------------------------------- #
# Async tool — returns immediately, work happens on the Kafka worker
# --------------------------------------------------------------------------- #


@mcp.tool()
async def verify_kyc_document(document_id: str) -> dict:
    """Queue a KYC document for re-verification.

    Call this only when the customer explicitly asks for a document to be
    re-checked, or when a rejection looks like it was a processing error.
    Verification is asynchronous: this returns {"status": "queued"} and the
    result lands on the case later. Tell the customer it is in progress — do
    not claim it has been verified.
    """
    # TODO(Phase 7): produce to KAFKA_TASKS_TOPIC, return the task_id.
    raise NotImplementedError("Phase 7")


# --------------------------------------------------------------------------- #
# Write tools — human approval required (LangGraph interrupt)
# --------------------------------------------------------------------------- #


@mcp.tool()
async def create_servicing_case(customer_id: str, category: str, summary: str) -> dict:
    """Open a servicing case for a human agent to action. WRITE — needs approval.

    Call this when the customer's problem cannot be resolved by explaining their
    current state — a document needs manual review, a policy exception is being
    requested, or they have asked to escalate. Do not open a case for questions
    you can answer from the read tools.
    """
    # TODO(Phase 5): write to servicing_cases, guarded by an idempotency key,
    # and only after the approval event has been recorded.
    raise NotImplementedError("Phase 5")


@mcp.tool()
async def update_customer_contact(customer_id: str, field: str, value: str) -> dict:
    """Update a customer's email, phone, or city. WRITE — needs approval.

    Call this only when the customer explicitly asks to change one of these
    fields and has stated the new value. `field` must be one of: email, phone,
    city. Never infer a new value from context — ask for it.
    """
    # TODO(Phase 5): validate field against an allowlist, then update + audit.
    raise NotImplementedError("Phase 5")


if __name__ == "__main__":
    mcp.run()
