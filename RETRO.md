# Sprint Retrospectives

Two sprints. Estimate before starting; record actuals honestly — estimate-vs-actual is the
part that's worth talking about in an interview.

## Sprint 1 — Phases 0–6 (MVP cut) — ✅ complete

- **Estimated:** _[ your number — 7 phases planned at ~3 h/day ]_
- **Actual:** _[ your number ]_
- **Shipped:** the whole spine. Compose + config + health (0), domain model and a 50-customer
  synthetic seed (1), MCP server with 4 read tools (2), LangGraph agent + the MCP→LangChain bridge
  + chat endpoint (3), Mongo conversation state + append-only audit trail (4), write tools with
  idempotency and the human-approval gate (5), and the MVP cut — rendered architecture diagrams,
  `make demo`, and a tested end-to-end walkthrough (6). 63 tests, none needing Docker or an API key.
- **Slipped, and why:** `verify_kyc_document` is declared on the MCP server but raises
  `NotImplementedError`. It is the async path and belongs with Kafka in Phase 7, so it was left as
  a typed stub rather than faked synchronously — and the README says so rather than implying seven
  working tools.
- **Estimate vs. actual, the honest part:** the time went to SDK churn, not to design. MCP 2.0
  renamed `FastMCP` and `inputSchema`, LangGraph 1.0 moved the prebuilt agent, and
  `langchain-mcp-adapters` turned out to be unimportable against a current MCP SDK. None of that
  was in the estimate, because none of it is visible until you import the thing.
- **One change for next sprint:** import-test every third-party dependency against the pinned SDK
  versions *before* designing around it. The adapter cost the most time and a five-minute spike
  would have caught it — finding it early is what would have made writing the ~50-line bridge an
  obvious upfront call rather than a mid-phase rescue.

## Sprint 2 — Phases 7–12

- **Estimated:** _[ ]_
- **Actual:** _[ ]_
- **Shipped:** _[ ]_
- **Slipped, and why:** _[ ]_
- **What I'd do differently if I rebuilt this:** _[ ]_
