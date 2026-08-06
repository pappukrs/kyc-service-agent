# Build Plan & Status

Where the project is, what's left, and why the load-bearing decisions went the way they did.

**Shipped:** Phases 0–9 · **183 tests passing** · no Docker, broker or API key required to run them.
The system is functionally complete end to end: a servicing request comes in, the agent reads real
data through MCP, answers, any write it wants to make pauses for a human, work too slow for a turn
goes on a queue and lands back on the customer's record later, and nothing that fails underneath can
hold the turn open indefinitely. As of Phase 9 there is also a suite that asks whether the answers
are any *good* — 16 scenarios against a real model, which the 183 above deliberately do not cover.

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
| 6 | Presentation | ✅ | Rendered diagrams, `make demo`, tested end-to-end walkthrough |
| 7 | Async work | ✅ | Kafka producer + worker, `verify_kyc_document`, at-least-once handling |
| 8 | Resilience | ✅ | Per-tool timeout + deadline, reads retried, turn-level tool-call cap |
| 9 | Evals | ✅ | 16-scenario suite, mechanical + model-judged assertions, in CI |
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

### Phase 7 — Async document verification
`verify_kyc_document` produces a `VerificationTask` to `servicing.tasks` and returns
`{"status": "queued", "task_id": ...}` without waiting. The worker consumes, simulates the check,
writes the outcome to the document, appends it to any live case, and emits `kyc_document_verified`
to `servicing.audit`.

The behavioural risk this phase carries is one sentence — the agent must say verification is **in
progress**, never that it succeeded — so the design removes the opportunity rather than
instructing against it. The tool has no success to return: the outcome is decided in another
process, after the turn has ended.

Both halves live in `src/worker/verification.py`, and neither knows about Kafka — they take a
publisher and a database. That is what makes the whole feature testable without a broker
(25 of the 88 tests), and `consumer.py` is then nothing but a loop.

Three things the queue forced that the synchronous path never had to think about:

- **The document moves to `verifying` at enqueue.** Otherwise a customer asking again two minutes
  later is told their document is still rejected, with nothing to show a re-check is running. It
  also means the *submitted* status has been overwritten by the time the worker decides, so it is
  preserved as `verification.previous_status` — an expired document must stay expired however many
  times it is re-checked.
- **Redelivery is expected, not exceptional.** At-least-once delivery plus a commit after handling
  means the handler will see the same task twice, so it drops one already recorded complete. Without
  that, a replay appends a second note to the customer's case and emits a second audit event.
- **A broker outage must not be reported as success.** The idempotency claim is released, the
  document is untouched, and the tool returns `verification_unavailable` with instructions to say
  so. A queued-but-lost verification the customer has been told about is the worst outcome here.

### Phase 8 — Resilience
Every tool call is bounded — `tool_timeout_seconds` per attempt, `tool_deadline_seconds` for the
whole call — and a turn is capped at `max_tool_calls_per_turn`. Tool errors already returned as
data; what had no handling was *no answer at all*, because it had no end.

The load-bearing decision is which calls may be retried:

- **Reads are retried, writes are not.** A read is a question and the second answer is as good as
  the first. A timed-out write may already have committed — the timeout says the answer did not come
  back, not that the work did not happen. Retrying is a guess about which, and the idempotency claim
  does not settle it: a retry inside the same correlation scope collapses onto a claim whose result
  was never stored and returns `duplicate_in_progress`. So a write is attempted once and reported as
  **unconfirmed**, with the agent told plainly not to say it succeeded *or* that it failed.
  `verify_kyc_document` sits in the same bucket — it claims a key and publishes to Kafka.
- **Retryable means "no answer", not "an answer we didn't like".** An MCP `is_error` result means
  the server ran the tool and said how it went. Some of those would pass on a second attempt, but
  most (unknown document, bad argument) are deterministic, and telling them apart would mean parsing
  the server's error strings. Timeouts and transport failures classify with certainty; those are the
  ones retried.
- **A deadline, not just a timeout.** Three attempts at fifteen seconds is forty-five seconds of a
  customer watching a spinner. Each attempt gets the lesser of its own timeout and what is left of
  the call's budget, and an attempt that cannot finish inside the budget is not started.
- **Failure carries the exception type, never its message.** An exception string can hold a
  connection string, a query fragment, or customer data, and this payload goes into the model's
  context and the conversation transcript.

`ToolCallLimitMiddleware` is used as shipped for the turn cap, in `continue` mode — the model is
told to stop calling tools and answer from what it has, where `end` mode would hand the customer the
framework's own "run limit exceeded (9/8 calls)".

### Phase 9 — Evaluation suite
Sixteen scenarios in `evals/scenarios.yaml`, run against the production agent — the real MCP
server, the real approval gate, the real retry policy — and a real model. The database and the
broker are in-memory; the model emphatically is not. This is the first thing in the project that
tests *judgement*, and every earlier phase deliberately avoided it: a fake model cannot be asked
whether it picked the right tool, because the script and not the model decides.

Coverage tracks the phases. Reading and grounding (rejection reasons explained, every rejection
surfaced rather than just the first, an unknown id not papered over, a follow-up answered from
context rather than re-fetched); scope (money movement refused, ambiguity questioned rather than
guessed at); the gate (a write pauses, an approved one lands, a refused one writes nothing); and
the two failure modes Phases 7 and 8 introduced — a re-check that was *requested* must never be
described as one that *passed*, and a timed-out write must be reported as neither success nor
failure. One scenario is a prompt injection planted in a rejection reason, on a fixture customer
that exists only here.

The load-bearing decisions:

- **Mechanically first, judged second.** A scenario that called the wrong tool has already failed;
  grading the prose it wrote off the back of that spends two API calls to tell you what you know,
  and occasionally reports a "grounded" pass on an answer built from a lookup that should never
  have happened. As much stays on the mechanical side as the questions allow — those failures are
  facts about the run, cost nothing, and cannot be argued with.
- **Tool selection is read from the transcript; write safety from `tool_audit`.** A write the model
  requested and the gate then parked is a correct call *and* a write that did not happen. Asserting
  both against one source makes one of the two questions unanswerable.
- **The judge sees only what the tools returned.** Not the database, not the scenario's intent, not
  the customer id — so it cannot rationalise an unsupported claim as true. That is the same
  standard the agent is held to.
- **A failure must quote the sentence.** A verdict naming no specific claim is downgraded to a
  pass. A deliberate bias towards false negatives: an eval that fails for reasons a human cannot
  check is one people learn to ignore, and a suite nobody trusts catches nothing at all.
- **The harness is itself under test, without a key.** That the scenario file is well-formed, that
  a paused write leaves no audit row and an approved one does, that an unsubstantiated verdict is
  not counted. Including a tripwire on the seed: change it and the scenarios naming CUST-019 and
  CUST-020 do not fail, they quietly become tests of nothing and stay green.

CI runs in two jobs. Lint and the 183 tests gate every push — deterministic, free, no key. The
evals are their own job, gated on the secret being present rather than on the event, because a
fork's pull request cannot see it and the alternative to skipping is sixteen scenarios failing with
an auth error and looking like the agent got everything wrong.

---

## Remaining

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

**Re-verification is not behind the approval gate.** It changes a document's status, so the case
for gating it is real. It is not gated because the agent is asking the verification system to look
again and does not get to choose what it finds — the outcome is produced by another process on the
document's own merits. Approval exists to put a human between the agent's *judgement* and the
customer's record; here the agent exercises none. A tool that let it *set* a document to verified
would be a write and would go through the gate.

**The enqueue preserves the status it overwrites.** Moving the document to `verifying` is what makes
progress visible to the read tools, but it destroys the very field the worker needs to decide —
`verification.previous_status` carries it across. Without it an expired document quietly becomes
verifiable, which is the one outcome re-verification must never produce.

**The worker's audit trail is a Kafka topic, not a Mongo collection.** `tool_audit` records that the
agent *asked* for verification; `servicing.audit` records what the system then *did*, in another
process, after the turn ended. Both carry the correlation id, so the two halves join. Writing the
async half into `tool_audit` would have made "every row is a tool call the agent made" false.

**Reads are retried; writes get one attempt and an honest "unconfirmed".** The alternative — retry
everything and lean on the idempotency key — fails in the exact case it is needed: the first attempt
claimed the key and timed out before storing a result, so the retry returns `duplicate_in_progress`,
which means "something happened, unclear what". Better to attempt once and say so than to attempt
twice and still not know. The customer-facing rule falls out of it: after a failed write the agent
must not claim success *or* failure.

**The retry lives in the MCP bridge, not in `ToolRetryMiddleware`.** The shipped middleware handles
backoff and per-tool selection well, but it has no notion of a time bound — which is the half that
matters, since retrying without a deadline just multiplies the wait. And it retries by re-running
the tool, so each attempt writes its own audit row: the trail would show three `get_customer` calls
with no way to distinguish a retry from the agent asking three times. Retrying inside the bridge
keeps one tool call to one row and records `attempts` on it. The turn cap went the other way —
`ToolCallLimitMiddleware` is exactly right for that and is used as shipped.

**Tool names are classified by blast radius in the MCP server.** `READ_TOOLS` / `ASYNC_TOOLS` /
`WRITE_TOOLS` live beside the tool definitions, because the read-only binding, the approval gate and
the retry policy all classify the same seven names — three private copies would be three chances to
drift. A test asserts the classification still covers exactly what the server advertises.

**Contact updates are an allowlist, not a denylist.** `email`, `phone`, `city`. A field added to the
model later is closed by default rather than silently writable.

**No `langchain-mcp-adapters`.** Its latest release (0.3.1) imports `mcp.server.fastmcp.tools`, a
module removed in MCP SDK 2.0, so it cannot be imported against a current SDK — and pip does not
catch it because its `mcp` dependency is unpinned. The choice was to pin this project to a
superseded SDK or own ~50 lines of glue; `src/agent/mcp_tools.py` is the glue.

---

## Known gaps

Being explicit about what has **not** been demonstrated, as distinct from what is unimplemented.

- **Nothing has run against real MongoDB.** Both suites run against `mongomock-motor`. Query shapes
  are ordinary and the unique index idempotency depends on is honoured by mongomock, but the
  `MongoDBSaver` path specifically has not been exercised against a live server.
- **The eval suite has not been run against a real model.** This is the sharpest gap on the list,
  and it is about this checkout rather than the code: no `MODEL_API_KEY` is configured, so all 16
  scenarios skip. What is proven is everything around them — the harness, the assertions, the
  judge's substantiation rule, the fixture preconditions, all under test with a scripted model. What
  is unproven is the agent's judgement itself, which is the one thing the suite exists to measure.
  Until it runs green against a real model, treat the scenarios as *written*, not as *passing*.
- **The judge defaults to the agent's own model.** Which grades its own output more kindly than it
  should. `EVAL_JUDGE_MODEL` fixes it and any result worth quoting should set it — but the default
  is the weaker arrangement, not the stronger one.
- **The eval suite is sequential and not perfectly deterministic.** Scenarios patch process-global
  state (the database, the publisher, the resilience bounds), so two running at once would evaluate
  a system neither described. The honest fix is to inject rather than patch. And a real model at
  `temperature=0` is still not a guarantee: an occasional flake is a property of the suite, so a
  single red scenario is a prompt to read the reply, not automatically a regression.
- **Nothing has run against a real Kafka broker.** The verification feature is tested end to end
  against an in-memory publisher — the enqueue, the outcomes, redelivery, case updates and the
  audit event. What that does not exercise is aiokafka itself: `consumer.py`'s loop, the manual
  commit, and the group rebalance are unproven outside a live broker.
- **Timeouts are proven against an in-process MCP server.** The bound, the retry, the deadline and
  the audit row are all tested — by making a repository call hang, which is a faithful stand-in for
  a wedged database. What it does not exercise is a timeout across a real transport, where the
  server keeps working on a call the client has already given up on. Nothing downstream depends on
  the client waiting, but it has not been watched happen.
- **The turn cap is soft.** `continue` mode blocks further tool calls and tells the model to answer;
  a model that ignored that and kept asking would loop until LangGraph's recursion limit stopped it.
  That backstop is the framework's default, not something this project sets.
- **No authentication.** `JWT_SECRET` is in config and unused; every endpoint is open. Fine for a
  local demo, not for anything else.
- **The approver is whoever the caller says it is.** `POST /approve` takes an `approver` string on
  trust. A real deployment needs that bound to an authenticated identity — an audit trail is only
  as good as the identity feeding it.
