# Build Plan & Status

Where the project is, what's left, and why the load-bearing decisions went the way they did.

**Shipped:** Phases 0–5 · **57 tests passing** · no Docker or API key required to run them.
The system is functionally complete end to end: a servicing request comes in, the agent reads real
data through MCP, answers, and any write it wants to make pauses for a human.

---

## Status at a glance

| # | Phase | State | What it delivered |
|---|---|---|---|
| 0 | Skeleton | ✅ | Compose (Mongo + Kafka), 12-factor config, `/healthz`, `/metrics`, Makefile |
| 1 | Domain + seed | ✅ | Pydantic models, Mongo collections + indexes, 50 synthetic customers with correlated document states |
| 2 | MCP server | ✅ | 7 tools declared, 4 read tools implemented, prescriptive descriptions |
| 3 | Agent loop | ✅ | LangGraph agent, MCP→LangChain bridge, system prompt, chat endpoint |
| 4 | State + audit | ✅ | Mongo checkpointer, append-only `tool_audit`, `GET /sessions/{id}/audit` |
| 5 | Write gate | ✅ | Both write tools, idempotency, `POST /sessions/{id}/approve` |
| 6 | Presentation | ▫️ | Architecture diagram, demo script, deploy |
| 7 | Async work | ▫️ | Kafka producer + worker, `verify_kyc_document` |
| 8 | Resilience | ▫️ | Per-tool timeout + retry policy |
| 9 | Evals | ▫️ | 15-scenario suite, 4 assertion types, in CI |
| 10 | Observability | ▫️ | Structured logs, PII redaction, Prometheus metrics, token/cost counters |
| 11 | ADK port | ▫️ | Google ADK runtime behind the same MCP tools |

---

## Shipped, in detail

### Phase 0 — Skeleton
Docker Compose brings up MongoDB and Kafka (KRaft mode, no ZooKeeper). Config is 12-factor via
`pydantic-settings`; nothing in the codebase hard-codes a connection string or provider name.
`/healthz` reports Mongo reachability, `/metrics` serves Prometheus.

### Phase 1 — Domain and synthetic data
Six collections: `customers`, `kyc_documents`, `servicing_cases`, `conversations`, `tool_audit`,
`idempotency_keys`. `scripts/seed.py` generates 50 customers whose document states correlate with
their onboarding stage — an approved customer has verified documents, a blocked one has a rejection
with a reason. The seed is deterministic (`random.seed(42)`) so eval scenarios can reference
specific customer ids.

### Phase 2 — MCP server
All seven tools declared; the four read tools implemented. `get_onboarding_status` derives blockers
from stage plus document state and, importantly, distinguishes documents the customer must **act
on** (rejected, expired) from ones they are merely **waiting for** (pending, verifying).

`search_servicing_kb` is lexical term-overlap scoring with tags weighted above body text —
deliberately not a vector store. A few dozen policy articles do not need embeddings, and a lexical
matcher is far easier to reason about when an eval fails.

### Phase 3 — Agent loop
`agent_session()` is an async context manager, because the tools close over a live MCP client and
the agent is only valid while that session is open. `read_only=True` binds just the four read tools
— an agent never given a write tool cannot be prompted into calling one.

The system prompt lives in its own module (`src/agent/prompt.py`) because it is behaviour, not
configuration: it changes what the agent does at least as much as the code does, so it should be
reviewed and diffed like code. Its first and longest section is grounding — every factual claim
about a customer must come from a tool result in the conversation.

### Phase 4 — Conversation state and audit
`MongoDBSaver` persists graph state per thread. This is load-bearing rather than a nicety: a write
parked for human approval must survive the process that parked it, or a restart drops every pending
decision. Tested by resuming a paused write with a brand-new agent instance against the same store.

The audit trail is appended at the MCP→LangChain bridge — the single choke point every tool call
passes through, successes and failures alike.

### Phase 5 — Write gate
Both write tools implemented, idempotent within a correlation scope. `POST /sessions/{id}/approve`
resumes or rejects a paused run; approving with nothing pending is a 409 rather than a silent no-op.

### Phase 6 — Presentation (MVP cut)
Nothing new functionally — this is the phase that makes the work legible to someone who won't read
the source. Rendered diagrams in [`architecture.md`](./architecture.md) (components, the approval
gate as a sequence, the shape of an audit row), and `make demo`, which takes a clean clone to a
working demonstration in one command.

The demo drives the API over HTTP rather than reaching into the agent, so whatever it proves is
proven at the boundary a caller actually sees; and it establishes ground truth by reading Mongo
directly *before* the agent runs, so its claims can be checked from outside the agent's own path.

It is under test too, including a case asserting it can still fail — a walkthrough that prints ✓
unconditionally proves nothing, and one nobody runs until interview day has quietly rotted.

Deploying a public URL was originally scoped here; it moved to Phase 12 alongside the demo video,
since neither changes the system and both are better done once it is finished.

---

## Remaining

### Phase 7 — Async document verification
`verify_kyc_document` currently raises. It should produce to `servicing.tasks` and return
`{"status": "queued", "task_id": ...}` immediately; the worker consumes, simulates verification
latency and a pass/fail outcome, updates the document and any linked case, and emits to
`servicing.audit`.

The behavioural risk worth testing here: the agent must tell the customer verification is **in
progress**, never that it succeeded. The tool description already says so; Phase 9 should assert it.

### Phase 8 — Resilience
Per-tool timeout and retry policy. Today a slow tool blocks the turn indefinitely. Tool errors
already return as data (`TOOL_ERROR from …`) rather than raising, so the agent can recover — but
there is no bound on how long it waits first.

### Phase 9 — Evaluation suite
`evals/scenarios.yaml` has 5 of a planned 15 scenarios and no runner. Four assertion types:

- **tool selection** — was the right tool called? catches over- and under-calling
- **no unapproved writes** — asserted against `tool_audit`, not the transcript; the transcript is
  what the model *said*, the audit log is what actually happened
- **grounded** — every factual claim traces to a tool result
- **refusal** — out-of-scope requests declined rather than hallucinating a capability

This is the phase that tests *judgement* rather than wiring, and it needs a real model. The existing
63 tests deliberately cover only mechanics; conflating the two would make both weaker.

### Phase 10 — Observability
Structured JSON logs carrying the correlation id, PII redaction at the log boundary, Prometheus
counters and histograms per tool, token and cost accounting. `langchain.agents.middleware` ships a
`PIIMiddleware` worth evaluating before hand-rolling redaction.

### Phase 11 — Google ADK port
A second runtime binding the same MCP tools, selected by `AGENT_RUNTIME`. The point is to
demonstrate that the tool surface — not the framework — is the stable interface: both runtimes
should pass the same eval suite unchanged.

---

## Decision log

Things that could reasonably have gone the other way.

**The agent has no direct database access.** Every capability is a typed MCP tool. This is what
makes the audit trail complete (one choke point), the permission model enforceable (the gate lives
at the tool boundary), and the runtime swappable (Phase 11 changes nothing below the tools).

**No money-movement tool exists.** Not disabled — absent. No transfers, no payments, no balance
reads. An agent that can move money is a different risk conversation; the scope here is servicing,
and the tool surface enforces that rather than relying on the prompt to.

**Approval is per tool, not per graph node.** The graph-level `interrupt_before` takes *node* names,
and every tool shares one node — using it would have gated reads too. `HumanInTheLoopMiddleware`
discriminates by tool name.

**Idempotency is scoped to the correlation id.** A retry within one turn collapses onto the first
write; the same request made next week legitimately opens a second case. Keyed on
`(correlation_id, tool, arguments)` and enforced by a unique index, so the database settles the race
rather than the application.

**Audit rows hash results and redact argument values.** Results carry PII, so the log stores a
SHA-256 rather than a second copy of the customer database. The subtler half:
`update_customer_contact(field="email", value=...)` carries personal data *in its arguments*, so
hashing the result while logging raw args would leak precisely the field the write was about.

**An audit write failing does not fail the request.** It is logged and swallowed. A deliberate trade
for a servicing assistant — losing one audit row beats failing a customer's turn. A system that
moved money would make the opposite call and fail closed.

**Contact updates are an allowlist, not a denylist.** `email`, `phone`, `city`. A field added to the
model later is closed by default rather than silently writable.

**No `langchain-mcp-adapters`.** Its latest release (0.3.1) imports `mcp.server.fastmcp.tools`, a
module removed in MCP SDK 2.0, so it cannot be imported against a current SDK — and pip does not
catch it because its `mcp` dependency is unpinned. The choice was to pin this project to a
superseded SDK or own ~50 lines of glue; `src/agent/mcp_tools.py` is the glue.

---

## Known gaps

Being explicit about what has **not** been demonstrated, as distinct from what is unimplemented.

- **Nothing has run against real MongoDB or a real model.** The suite runs against
  `mongomock-motor` and a scripted fake chat model. Query shapes are ordinary and the unique index
  idempotency depends on is honoured by mongomock, but the `MongoDBSaver` path specifically has not
  been exercised against a live server.
- **No test covers model judgement.** By design — see Phase 9.
- **`verify_kyc_document` raises.** Declared and described, implemented in Phase 7.
- **No authentication.** `JWT_SECRET` is in config and unused; every endpoint is open. Fine for a
  local demo, not for anything else.
- **The approver is whoever the caller says it is.** `POST /approve` takes an `approver` string on
  trust. A real deployment needs that bound to an authenticated identity — an audit trail is only
  as good as the identity feeding it.
