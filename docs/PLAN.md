# Project Plan — Agentic Servicing Assistant

> **Why this project:** `README.md` §1 lists four hard gaps against the Wells Fargo JD — **Python**,
> **Google ADK/LangChain**, **agentic environments**, and **MCP**. This one project closes all four,
> plus reinforces MongoDB and Kafka, in a **banking-servicing domain that mirrors their exact
> product**. It is also the highest-leverage thing you can build right now for the whole category:
> every Bengaluru fintech is hiring for agentic platforms, and almost none of those applicants have
> your KYC/onboarding background to pair with it.
>
> **Build it, then paste the pre-written bullets from `resume-content.md` §APPENDIX.**
> ⚠️ Nothing goes on the résumé until it exists and runs.

---

## 1. What you're building

**An AI-assisted customer-servicing assistant for retail banking onboarding and KYC.**

A servicing request comes in — *"my KYC document was rejected, what now?"*, *"what's the status of my
onboarding?"*, *"update my registered address"* — and an agent resolves it end to end: looks up the
customer, reads their onboarding and KYC state, explains the blocker, and either answers or opens a
servicing case. Write actions pause for human approval. Every tool call is audited.

That is, deliberately, a plain-English restatement of the JD:

| JD phrase | Where it lands in this project |
|---|---|
| "design and build an AI-Assisted servicing platform" | the whole thing |
| "Design and Develop AI Agents using Google ADK/Langchain" | Phase 3 (LangChain/LangGraph) + Phase 11 (ADK port) |
| "Implement production grade agents" | guardrails, audit trail, evals, observability |
| "Build robust APIs and workflows" | FastAPI + the approval workflow |
| "Implement workflow and task-based processing" | Kafka worker for async document verification |
| "Strong MCP" | the MCP server is how the agent gets every tool |
| "Strong MongoDB or any NoSQL" | conversation state, case records, audit log |
| "Kafka/Queues, Workflows" | Phase 7 |
| "Exposure to Banking domain (KYC, Onboarding)" | the domain model — and you have real SenseGrass experience behind it |
| "Basic understanding of cloud-native concepts" | Docker Compose, health checks, 12-factor config |
| "Hands-on experience working in Agile" | §6 — run it as two real sprints |

⚠️ **Synthetic data only.** Seed fake customers and fake documents. Never put real banking data,
real PII, or anything from Chesa into this repo. Say "synthetic dataset" in the README and in
interviews — a candidate who is careless with data is a non-starter at a bank.

---

## 2. Architecture

```
                    ┌──────────────────────────────────────────┐
   HTTP  ─────────▶ │  FastAPI  —  auth (JWT), request intake,  │
                    │  session routing, /healthz, /metrics      │
                    └────────────────────┬─────────────────────┘
                                         │
                    ┌────────────────────▼─────────────────────┐
                    │  Agent service  —  LangGraph agent loop   │
                    │  plan → call tool → observe → repeat      │
                    │  interrupt() on any write action          │
                    └────────────────────┬─────────────────────┘
                                         │ tools loaded over MCP
                    ┌────────────────────▼─────────────────────┐
                    │  MCP server  —  the ONLY way the agent    │
                    │  touches banking data. 7 typed tools.     │
                    └───────┬───────────────────────┬──────────┘
                            │                       │
              ┌─────────────▼──────────┐   ┌────────▼─────────────┐
              │  MongoDB               │   │  Kafka               │
              │  • customers           │   │  • servicing.tasks   │
              │  • kyc_documents       │   │  • servicing.audit   │
              │  • servicing_cases     │   └────────┬─────────────┘
              │  • conversations       │            │
              │  • tool_audit (append) │   ┌────────▼─────────────┐
              │  • idempotency_keys    │   │  Worker — async doc  │
              └────────────────────────┘   │  verification, then  │
                                           │  updates the case    │
                                           └──────────────────────┘
```

**The one design decision to lead with in interviews:** *the agent has no direct database access.*
Every capability is a typed MCP tool with an explicit contract. That's what makes the audit trail
complete, the permission model enforceable, and the tool surface swappable between LangChain and ADK
without touching business logic.

### Stack

| Layer | Choice | Why |
|---|---|---|
| Language | **Python 3.12** | the gap you're closing |
| API | **FastAPI** + Pydantic | async, typed, the Python default |
| Agent | **LangGraph** (LangChain) | you need state + human-in-the-loop; LangGraph has `interrupt`/resume built in |
| Tools | **MCP** (`mcp` Python SDK, `FastMCP`) | the JD's "Strong MCP" — and it's an open standard, not vendor lock-in |
| Bridge | **`langchain-mcp-adapters`** | loads MCP tools straight into a LangChain/LangGraph agent |
| Store | **MongoDB** (Motor async driver) | reinforces a ✅ you already have |
| Queue | **Kafka** (`aiokafka`) — RabbitMQ acceptable | JD says "Kafka/Queues"; you know RabbitMQ, so Kafka is the new bit |
| Runtime | **Docker Compose** | whole stack up with one command |

**On the model provider:** LangChain and LangGraph are provider-agnostic — keep the model behind one
config value (`MODEL_PROVIDER` / `MODEL_NAME`) and a single factory function. Use whatever you have
credits for during development; a smaller/cheaper model is fine for everything except the final demo
run. Google ADK is Google's own framework and defaults to Gemini, which is what the Phase 11 port
will use. **Do not hard-code a provider anywhere but that factory** — being able to say *"the model
is one config line, because the agent only ever talks to tools"* is itself a good architecture
answer.

---

## 3. The tool surface (design this before you write code)

Seven tools, split by blast radius. **The read/write split is the security boundary** — say it that
way in interviews.

| Tool | Kind | Contract |
|---|---|---|
| `get_customer` | read | `customer_id` → profile, onboarding stage, risk tier |
| `get_onboarding_status` | read | `customer_id` → stage, blockers, pending items |
| `list_kyc_documents` | read | `customer_id` → docs with status + rejection reason |
| `search_servicing_kb` | read | `query` → policy/FAQ snippets (plain keyword search is fine — this is not a RAG project) |
| `verify_kyc_document` | **async** | `document_id` → enqueues a Kafka task, returns `{status: "queued", task_id}` |
| `create_servicing_case` | **write ⚠️** | `customer_id, category, summary` → needs human approval |
| `update_customer_contact` | **write ⚠️** | `customer_id, field, value` → needs human approval |

**Write prescriptive tool descriptions.** The single highest-leverage prompt-engineering lever in an
agentic system is that each tool's own description says **when to call it**, not just what it does:

```python
@mcp.tool()
async def list_kyc_documents(customer_id: str) -> list[dict]:
    """List a customer's KYC documents with status and rejection reasons.

    Call this whenever the customer asks why their onboarding is blocked, why a
    document was rejected, or what they still need to submit. Prefer this over
    get_onboarding_status when the question is specifically about documents.
    """
```

**There is deliberately no money-movement tool.** No transfers, no payments, no balance mutation.
Say so out loud in the README and the interview — *"an agent that can move money is a different
risk conversation; the scope here is servicing, and the tool surface enforces that."* That single
sentence signals more banking maturity than any feature you could add.

---

## 4. Build plan — 12 days, ~3 h/day

Each phase has an **acceptance test**. Don't move on until it passes.
**Day 6 is an MVP cut** — if you run out of time, stop there and you still have a shippable,
résumé-able project that closes Python + LangChain + MCP + agentic. Everything after Day 6 is upside.

### Week 1 — the spine

| Day | Build | Acceptance test |
|---|---|---|
| **0** | Repo, Python 3.12, `uv`/Poetry, Docker Compose (Mongo + Kafka), `.env.example`, `/healthz` | `docker compose up` → `curl /healthz` returns 200 |
| **1** | Domain model (Pydantic), Mongo collections, `seed.py` generating ~50 synthetic customers with varied onboarding states | `GET /customers/{id}` returns a seeded customer; a rejected-KYC customer exists |
| **2** | **MCP server** with the 4 read tools, stdio transport, prescriptive descriptions | MCP Inspector lists all 4 and calls `list_kyc_documents` successfully |
| **3** | **LangGraph agent** + `langchain-mcp-adapters`; chat endpoint `POST /sessions/{id}/messages` | *"Why is CUST-014 blocked?"* → agent calls 2 tools and answers correctly from the data |
| **4** | Conversation state + **append-only `tool_audit`** collection (one doc per call: tool, args, result hash, latency, correlation id) | Restart the process, resume `session_id`, context intact; audit count matches tool calls |
| **5** | Write tools + **human-in-the-loop approval** via LangGraph `interrupt` → `POST /sessions/{id}/approve` | `create_servicing_case` pauses; approve → case created; deny → no write, agent explains |
| **6** | 🎯 **MVP CUT** — README, architecture diagram, one-command startup, demo script | A stranger clones the repo and gets a working demo in under 5 minutes |

### Week 2 — what makes it "production grade"

| Day | Build | Acceptance test |
|---|---|---|
| **7** | **Kafka** producer + worker; `verify_kyc_document` enqueues, worker processes and updates the case | Tool returns `queued`; worker logs consumption; case reflects the result |
| **8** | **Idempotency** keys on writes, per-tool **timeout + retry policy**, structured error results the agent can recover from | Replaying the same write twice creates one case; a forced tool timeout produces a graceful agent response, not a crash |
| **9** | **Eval harness** — 15 scenarios in YAML, run under pytest | All 15 pass; assertions in §5 below |
| **10** | **Observability** — JSON logs with correlation id, PII redaction, Prometheus counters/histograms, `/metrics`, token+cost counter | `/metrics` shows per-tool call counts and latency; no raw PII in any log line |
| **11** | **Google ADK port** of the agent layer, same MCP tools, selected by config | Both runtimes pass the same eval suite |
| **12** | Architecture diagram, README rewrite, 3-minute demo video, deploy (Cloud Run / EC2), pin the repo | Public URL or video link in the README; repo pinned on your GitHub profile |

**Day 11 matters more than it looks.** The JD says "Google ADK**/**Langchain". Having both behind one
tool layer lets you say: *"the tools are MCP, so the agent framework is swappable — I ran the same
eval suite against a LangGraph agent and a Google ADK agent."* That is a senior-sounding sentence
backed by a real artifact, and it converts a 🔴 gap into a strength.

---

## 5. The eval harness (Day 9) — this is your differentiator

Most "I built an agent" projects have no tests. Fifteen scenarios and four assertion types put you
in a different bracket:

1. **Tool selection** — for this question, was the right tool called? (Catches over- and under-calling.)
2. **No unapproved writes** — no write tool ever executes without an approval event. Assert on the audit log, not the transcript.
3. **Grounding** — every factual claim about a customer traces to a tool result. Reject answers that invent an account number or a status.
4. **Refusal** — out-of-scope requests ("transfer ₹50,000") are declined and explained. There is no such tool; the agent must handle that gracefully rather than hallucinate one.

Run it in CI. You already know how to gate deploys on tests — that's a ✅ from `Knowledgebase.md` §12,
so reuse the GitHub Actions pattern from `expense-portal`.

---

## 6. Run it as two Agile sprints (free credibility)

The JD lists Agile as a separate requirement with three sub-bullets: **estimation, sprint planning,
reviews and retrospectives**. Don't just claim it — do it, in the repo:

- **Sprint 1 = Days 0–6**, **Sprint 2 = Days 7–12.**
- A GitHub Projects board with estimated issues (story points or hours) — estimate *before* you start.
- At each sprint end write a 5-line `RETRO.md`: what shipped, what slipped, estimate vs. actual, one change for next sprint.

Cost: about 30 minutes total. It makes "I estimate work and run sprint ceremonies" a thing you can
show rather than assert, and estimate-vs-actual is exactly the sort of concrete answer interviewers
remember.

---

## 7. When you're done — updating the résumé

1. Open `resume-content.md` §APPENDIX — the skills lines and project entry are pre-written with
   `[[ slots ]]`.
2. **Verify each line is literally true of what you built.** Delete anything you didn't do.
   Especially: don't claim Kafka if you used RabbitMQ, and don't claim an ADK port you skipped.
3. Fill the slots, move the block into the main résumé body, re-export the PDF from the HTML.
4. Update `cover-letter.md` — the `[[ Optional, only once true ]]` clause about building agentic
   services in Python becomes true; unbracket it. That single clause changes the letter's whole pitch.
5. Re-apply to Wells Fargo **and** to every other agentic-platform JD in the category.

---

## 8. Interview questions this project earns you — prepare answers

You will be asked these. Have the answer ready before you apply.

- **"Why MCP instead of just wiring functions into the agent?"** — a typed tool contract the agent can't bypass, one audit point, and the framework becomes swappable (which you proved with the ADK port).
- **"How do you stop the agent doing something destructive?"** — read/write split at the tool boundary; writes are `interrupt`-gated on human approval; there is no money-movement tool at all; every call is in an append-only audit log with a correlation id.
- **"What happens when a tool fails or times out?"** — per-tool timeout and retry policy, structured error result fed back to the agent so it can recover or escalate, idempotency keys so a retried write doesn't double-create.
- **"How do you know the agent works?"** — 15-scenario eval suite, four assertion types, running in CI. Then explain the grounding assertion — it's the one that shows you understand hallucination as an engineering problem, not a vibe.
- **"How would you scale it?"** — stateless API and agent services behind a load balancer; Kafka consumer groups for the workers; Mongo indexes on `customer_id` and `session_id`; the real ceiling is model latency and rate limits, so cache read-tool results and batch where possible.
- **"What would you do differently?"** — have a real answer. Candidates who can critique their own design read as senior; candidates who say "nothing" read as junior.

---

## 9. Related

- [`README.md`](./README.md) — the Wells Fargo JD gap analysis this project exists to close
- [`resume-content.md`](./resume-content.md) §APPENDIX — the bullets to paste once it's built
- [`cover-letter.md`](./cover-letter.md) §3 — the *"do you have Python and LangChain?"* answer, which this project rewrites
- [`../Knowledgebase.md`](../Knowledgebase.md) §12 — "GitHub showcase: turn a project into a documented, deployed showcase with an architecture diagram" was already on your backlog. This is that item.
