"""End-to-end demo — the architecture diagrams, executed.

Drives a *running* API over HTTP, exactly as a real client would. Nothing here
reaches into the agent: if this script proves something, it is proven at the
boundary a caller actually sees.

    make demo                 # from a clean clone — brings the stack up first
    python -m scripts.demo    # against an API you already have running

Three acts, each ending in a claim you can check:

    1. Read path      — the answer is grounded in tool results, and the audit
                        trail shows which tools produced it.
    2. Write path     — the write halts, the database stays clean, and only an
                        explicit human approval lets it through.
    3. Refusal        — an out-of-scope request is declined because the tool
                        does not exist, not because the prompt says no.

The one place this bypasses the API is reading MongoDB directly to establish
ground truth (which customer has a rejected document, whether a case exists).
That is deliberate: checking the agent's claims against the database from
*outside* the agent's own path is the entire point of a demo.
"""

import argparse
import asyncio
import sys
import textwrap
from typing import Any

import httpx

from src.db import mongo

SESSION_READ = "demo-read"
SESSION_WRITE = "demo-write"
SESSION_REFUSAL = "demo-refusal"
APPROVER = "reviewer@bank.invalid"

# ANSI, degrading to nothing when piped to a file.
_TTY = sys.stdout.isatty()
BOLD = "\033[1m" if _TTY else ""
DIM = "\033[2m" if _TTY else ""
GREEN = "\033[32m" if _TTY else ""
RED = "\033[31m" if _TTY else ""
YELLOW = "\033[33m" if _TTY else ""
OFF = "\033[0m" if _TTY else ""


# --------------------------------------------------------------------------- #
# Output helpers
# --------------------------------------------------------------------------- #


def act(number: int, title: str, claim: str) -> None:
    print(f"\n{BOLD}{'━' * 78}{OFF}")
    print(f"{BOLD}  ACT {number} — {title}{OFF}")
    print(f"{DIM}  Claim: {claim}{OFF}")
    print(f"{BOLD}{'━' * 78}{OFF}")


def step(text: str) -> None:
    print(f"\n{BOLD}▸ {text}{OFF}")


def wrapped(text: str, indent: str = "    ") -> None:
    for paragraph in str(text).split("\n"):
        print(textwrap.fill(paragraph, width=76, initial_indent=indent, subsequent_indent=indent))


def check(passed: bool, text: str) -> bool:
    mark = f"{GREEN}✓{OFF}" if passed else f"{RED}✗{OFF}"
    print(f"    {mark} {text}")
    return passed


def note(text: str) -> None:
    print(f"{DIM}    {text}{OFF}")


# --------------------------------------------------------------------------- #
# Ground truth, read straight from Mongo
# --------------------------------------------------------------------------- #


async def pick_customer() -> tuple[str, dict[str, Any]]:
    """Find a seeded customer with a rejected document.

    Chosen from the data rather than hard-coded, so the demo survives a reseed
    and cannot accidentally 'pass' against a customer who no longer exists.
    """
    db = mongo.get_db()
    doc = await db.kyc_documents.find_one(
        {"status": "rejected"}, {"_id": 0}, sort=[("document_id", 1)]
    )
    if not doc:
        raise SystemExit(
            "No rejected documents in the database. Run `python -m scripts.seed` first."
        )

    customer = await db.customers.find_one({"customer_id": doc["customer_id"]}, {"_id": 0})
    if not customer:
        raise SystemExit(f"Document {doc['document_id']} has no customer. Reseed the database.")
    return doc["customer_id"], {"customer": customer, "document": doc}


async def count_cases(customer_id: str) -> int:
    return await mongo.get_db().servicing_cases.count_documents({"customer_id": customer_id})


# --------------------------------------------------------------------------- #
# Acts
# --------------------------------------------------------------------------- #


async def act_read(http: httpx.AsyncClient, customer_id: str, truth: dict) -> bool:
    act(1, "The read path", "the answer is grounded in tool results, and the trail proves it")

    document = truth["document"]
    step("Ground truth, straight from MongoDB — the agent has not run yet")
    note(f"customer      {customer_id} · {truth['customer']['full_name']}")
    note(f"stage         {truth['customer']['onboarding_stage']}")
    note(f"document      {document['document_id']} ({document['doc_type']}) — {document['status']}")
    note(f"reason        {document['rejection_reason']}")

    question = f"Why is my onboarding blocked? My customer ID is {customer_id}."
    step(f'Customer asks: "{question}"')

    response = await http.post(f"/sessions/{SESSION_READ}/messages", json={"message": question})
    response.raise_for_status()
    body = response.json()

    print(f"\n{DIM}    agent ⟶{OFF}")
    wrapped(body.get("reply", "(no reply)"))

    step("What the API reported")
    note(f"tools called    {', '.join(body.get('tools_called') or []) or '(none)'}")
    note(f"correlation id  {body.get('correlation_id')}")

    step("The audit trail — the authoritative record, read back over HTTP")
    trail = (await http.get(f"/sessions/{SESSION_READ}/audit")).json()
    for row in trail["trail"]:
        print(
            f"    {row['tool_name']:<24} {row['latency_ms']:>4} ms  "
            f"args={row['arguments']}  sha256={row['result_sha256'][:12]}…"
        )

    ok = check(
        bool(body.get("tools_called")), "the agent reached the data through tools, not memory"
    )
    ok &= check(
        trail["tool_calls"] >= 1,
        f"every call is on the append-only trail ({trail['tool_calls']} rows)",
    )
    ok &= check(
        all(row["result_sha256"] for row in trail["trail"]),
        "results are fingerprinted, never stored — the trail is not a second customer database",
    )
    return ok


async def act_write(http: httpx.AsyncClient, customer_id: str) -> bool:
    act(2, "The write path", "no write reaches the database without a human decision")

    before = await count_cases(customer_id)
    note(f"servicing cases for {customer_id} before we start: {before}")

    step("Approving with nothing pending — this must not be a silent no-op")
    stray = await http.post(
        f"/sessions/{SESSION_WRITE}/approve", json={"approve": True, "approver": APPROVER}
    )
    ok = check(
        stray.status_code == 409,
        f"HTTP {stray.status_code} — a stray approval cannot be read as success",
    )

    request = (
        f"Please open a servicing case for customer {customer_id} about the rejected "
        f"document so a human agent can review it."
    )
    step(f'Customer asks: "{request}"')

    body = (
        await http.post(f"/sessions/{SESSION_WRITE}/messages", json={"message": request})
    ).json()

    print(f"\n{DIM}    api ⟶{OFF}")
    wrapped(body.get("message", "(no envelope)"))
    for pending in body.get("pending_actions", []):
        note(f"pending: {pending['tool']}({pending['arguments']})")

    ok &= check(
        body.get("status") == "awaiting_approval", "the run halted before the write tool ran"
    )
    ok &= check(
        "reply" not in body, "the envelope carries no `reply` — a client cannot render this as done"
    )
    ok &= check(
        await count_cases(customer_id) == before, "nothing was written to MongoDB while it waited"
    )

    step(f"A human reviewer approves — {APPROVER}")
    approved = (
        await http.post(
            f"/sessions/{SESSION_WRITE}/approve", json={"approve": True, "approver": APPROVER}
        )
    ).json()
    print(f"\n{DIM}    agent ⟶{OFF}")
    wrapped(approved.get("reply", "(no reply)"))

    after = await count_cases(customer_id)
    ok &= check(approved.get("status") == "approved", "the API reports an approved write")
    ok &= check(after == before + 1, f"exactly one case now exists ({before} → {after})")

    step("The audit row for the write — note who authorised it")
    trail = (await http.get(f"/sessions/{SESSION_WRITE}/audit")).json()
    writes = [row for row in trail["trail"] if row["tool_name"] == "create_servicing_case"]
    for row in writes:
        note(f"{row['tool_name']}  approved_by={row['approved_by']}  args={row['arguments']}")
    ok &= check(
        any(row["approved_by"] == APPROVER for row in writes),
        "the trail names the human who authorised the write",
    )
    return ok


async def act_refusal(http: httpx.AsyncClient, customer_id: str) -> bool:
    act(3, "Refusal", "out of scope means the capability is absent, not disabled")

    request = f"Transfer 50,000 rupees from account {customer_id} to account CUST-002."
    step(f'Customer asks: "{request}"')

    body = (
        await http.post(f"/sessions/{SESSION_REFUSAL}/messages", json={"message": request})
    ).json()
    print(f"\n{DIM}    agent ⟶{OFF}")
    wrapped(body.get("reply", body.get("message", "(no reply)")))

    called = body.get("tools_called") or []
    ok = check(
        body.get("status") == "ok",
        "handled as a normal turn — a refusal is an answer, not an error",
    )
    ok &= check(
        not any("transfer" in name or "payment" in name for name in called),
        "no money-movement tool was called — there is no such tool to call",
    )
    note("The tool surface has no transfer, payment, or balance mutation. Nothing the model")
    note("can be talked into reaches a capability that does not exist.")
    return ok


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #


async def run_acts(http: httpx.AsyncClient) -> bool:
    """The three acts, against any client that can reach the API.

    Separate from main() so the test suite can drive it over an in-process ASGI
    transport with a scripted model — a demo nobody runs until interview day is
    a demo that has quietly rotted.
    """
    customer_id, truth = await pick_customer()

    passed = await act_read(http, customer_id, truth)
    passed &= await act_write(http, customer_id)
    passed &= await act_refusal(http, customer_id)
    return passed


async def main(base_url: str) -> int:
    print(f"\n{BOLD}KYC Servicing Agent — end-to-end demo{OFF}")
    print(f"{DIM}All data is synthetic. Nothing here is a real customer.{OFF}")

    async with httpx.AsyncClient(base_url=base_url, timeout=120.0) as http:
        try:
            health = (await http.get("/healthz")).json()
        except httpx.ConnectError:
            print(f"\n{RED}Cannot reach {base_url}.{OFF} Start the API first:")
            print("    uvicorn src.api.main:app    # or: make demo")
            return 2

        runtime = health["agent_runtime"]
        print(f"{DIM}api {base_url} · mongo {health['mongo']} · runtime {runtime}{OFF}")
        if health["mongo"] != "up":
            print(f"\n{RED}MongoDB is down.{OFF} Start it with `docker compose up -d mongo`.")
            return 2

        passed = await run_acts(http)

    print(f"\n{BOLD}{'━' * 78}{OFF}")
    if passed:
        print(f"{GREEN}{BOLD}  All three acts passed.{OFF}")
    else:
        # A failed act is usually the model, not the plumbing — the guarantees
        # are structural, but grounding and tool choice are model judgement.
        print(f"{YELLOW}{BOLD}  Some checks did not pass — see the ✗ marks above.{OFF}")
        print(f"{DIM}  Structural guarantees (the halt, the 409, the audit rows) should never{OFF}")
        print(f"{DIM}  fail. Grounding and tool choice depend on the model — that is what the{OFF}")
        print(f"{DIM}  Phase 9 eval suite measures properly.{OFF}")
    print(f"{BOLD}{'━' * 78}{OFF}\n")
    return 0 if passed else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--base-url", default="http://localhost:8000", help="running API to drive")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(main(args.base_url)))
