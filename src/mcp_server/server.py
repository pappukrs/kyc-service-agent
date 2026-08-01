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

from mcp.server.mcpserver import MCPServer

from src.db import mongo, repositories
from src.obs import audit

# MCP SDK 2.x: MCPServer replaces the 1.x `FastMCP` class. Same decorator
# ergonomics — @mcp.tool() — but imported from mcp.server.mcpserver.
mcp = MCPServer(
    "kyc-servicing",
    instructions=(
        "Servicing tools for retail-banking onboarding and KYC. Read tools are "
        "safe to call freely. Write tools require human approval and must never "
        "be called speculatively. There is no tool for moving money, checking "
        "balances, or altering accounts — if asked, say so plainly."
    ),
)


def _not_found(customer_id: str) -> dict:
    """Uniform not-found result.

    Returned as data rather than raised: the agent should tell the customer the
    record could not be found, not crash the turn — and it must not invent one.
    """
    return {
        "error": "customer_not_found",
        "customer_id": customer_id,
        "message": (
            f"No customer with id {customer_id}. Ask the customer to confirm their "
            f"customer id. Do not guess or substitute another id."
        ),
    }


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
    customer = await repositories.find_customer(mongo.get_db(), customer_id)
    return customer or _not_found(customer_id)


@mcp.tool()
async def get_onboarding_status(customer_id: str) -> dict:
    """Get a customer's onboarding stage plus the list of outstanding blockers.

    Call this when the customer asks how far along their application is, why it
    is taking time, or what they still need to do. If they ask specifically
    about a *document*, use list_kyc_documents instead — it carries the
    rejection reasons.
    """
    status = await repositories.build_onboarding_status(mongo.get_db(), customer_id)
    return status or _not_found(customer_id)


@mcp.tool()
async def list_kyc_documents(customer_id: str) -> list[dict]:
    """List a customer's KYC documents with status and rejection reason.

    Call this whenever the customer asks why a document was rejected, why their
    onboarding is blocked, or what they still need to submit. This is the tool
    that carries rejection_reason — the usual answer to "why am I stuck?".
    """
    db = mongo.get_db()
    if await repositories.find_customer(db, customer_id) is None:
        # Distinguish "no such customer" from "customer with no documents".
        # Returning [] for both would let the agent tell someone their
        # application is fine when it does not exist.
        return [_not_found(customer_id)]
    return await repositories.find_documents(db, customer_id)


@mcp.tool()
async def search_servicing_kb(query: str) -> list[dict]:
    """Search internal servicing policy and FAQ content.

    Call this for questions about process, policy, or timelines that are not
    specific to one customer's record — "how long does verification take?",
    "which address proofs are accepted?". Do not use it to look up customer data.
    """
    return await repositories.search_kb(mongo.get_db(), query)


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
    return await repositories.create_case(
        mongo.get_db(),
        customer_id=customer_id,
        category=category,
        summary=summary,
        approved_by=audit.get_approver(),
        # Scoping idempotency to the correlation id means a retry *within this
        # turn* collapses onto the first write, while the same request made
        # again tomorrow legitimately opens a second case.
        scope=audit.get_correlation_id(),
    )


@mcp.tool()
async def update_customer_contact(customer_id: str, field: str, value: str) -> dict:
    """Update a customer's email, phone, or city. WRITE — needs approval.

    Call this only when the customer explicitly asks to change one of these
    fields and has stated the new value. `field` must be one of: email, phone,
    city. Never infer a new value from context — ask for it.
    """
    return await repositories.update_contact(
        mongo.get_db(),
        customer_id=customer_id,
        field=field,
        value=value,
        approved_by=audit.get_approver(),
        scope=audit.get_correlation_id(),
    )


if __name__ == "__main__":
    mcp.run()
