# Architecture

Three diagrams and the reasoning behind them. The short version: **the agent has no direct
database access.** Every capability it has is a typed MCP tool with an explicit contract, which is
what makes the audit trail complete, the permission model enforceable, and the agent framework
replaceable.

---

## 1. Components

```mermaid
flowchart TB
    client([Servicing UI / API client])

    subgraph api["FastAPI — src/api/main.py"]
        intake["POST /sessions/{id}/messages<br/>intake + session routing"]
        approve["POST /sessions/{id}/approve<br/>the only write path"]
        trail["GET /sessions/{id}/audit<br/>reconstruction"]
        ops["/healthz · /metrics"]
    end

    subgraph agent["Agent — src/agent/"]
        loop["LangGraph loop<br/>plan → call tool → observe → repeat"]
        gate{{"HumanInTheLoopMiddleware<br/>interrupts per write tool"}}
        llm["build_llm — the one place<br/>that knows the provider"]
    end

    bridge["mcp_tools.py — MCP → LangChain bridge<br/>(~50 lines, no langchain-mcp-adapters)"]

    subgraph mcp["MCP server — src/mcp_server/server.py"]
        reads["4 read tools<br/>get_customer · get_onboarding_status<br/>list_kyc_documents · search_servicing_kb"]
        async_tool["1 async tool<br/>verify_kyc_document"]
        writes["2 write tools ⚠️<br/>create_servicing_case<br/>update_customer_contact"]
    end

    repos["repositories.py<br/>the only module that touches Mongo"]

    mongo[("MongoDB<br/>customers · kyc_documents<br/>servicing_cases · conversations<br/>tool_audit · idempotency_keys")]
    kafka[["Kafka<br/>servicing.tasks"]]
    worker["Worker — async<br/>document verification"]

    client --> intake & approve & trail
    intake --> loop
    approve -->|"Command(resume=…)"| loop
    loop <--> llm
    loop --> gate
    gate -->|"reads: straight through"| bridge
    gate -.->|"writes: halt, return envelope"| intake
    bridge --> reads & async_tool & writes
    reads & writes --> repos
    repos --> mongo
    async_tool --> kafka --> worker --> mongo
    loop -.->|"every call, append-only"| mongo
    trail --> mongo

    classDef danger stroke:#c0392b,stroke-width:2px
    class writes,gate danger
```

**Read the diagram for what is *missing*.** There is no arrow from the agent to MongoDB. There is
no money-movement tool. An agent that can move money is a different risk conversation; the scope
here is servicing, and the tool surface enforces that rather than the prompt asking nicely.

The dotted line from the gate back to intake is the whole write story: a write does not become a
slower write, it becomes an *envelope* the caller has to act on.

---

## 2. The approval gate, as a sequence

The security-critical path. Note that the write tool is not called until after the human decides —
this is a halt, not a rollback.

```mermaid
sequenceDiagram
    autonumber
    participant C as Client
    participant A as FastAPI
    participant G as LangGraph agent
    participant M as MCP server
    participant DB as MongoDB

    C->>A: POST /sessions/s1/messages<br/>"open a case about my rejected document"
    A->>G: ainvoke(thread_id=s1)
    G->>M: list_kyc_documents(CUST-014)
    M->>DB: find documents
    DB-->>M: 3 docs, 1 rejected
    M-->>G: result
    G->>DB: append tool_audit row (args redacted, result hashed)
    Note over G: model asks for create_servicing_case
    G--xM: HALT — write tool, not called
    G-->>A: __interrupt__ with the pending action
    A-->>C: 202-shaped envelope: status=awaiting_approval<br/>no `reply` field — a client cannot render this as done

    Note over C,DB: nothing has been written

    C->>A: POST /sessions/s1/approve {approve: true, approver: "…"}
    A->>G: Command(resume={decisions:[approve]})
    G->>M: create_servicing_case(…)
    M->>DB: check idempotency key (unique index)
    DB-->>M: not seen → insert case
    M-->>G: {case_id: CASE-…}
    G->>DB: append tool_audit row incl. approver
    G-->>A: final message
    A-->>C: {status: "approved", reply: "…"}
```

Four properties make this a gate rather than a suggestion:

| Property | Why it holds |
|---|---|
| The pause is **structural** | `HumanInTheLoopMiddleware` interrupts the graph per tool. It does not depend on the model choosing to comply. Note *per tool* — all tools share one graph node, so a graph-level `interrupt_before` would have gated reads too. |
| Approval is the **only** write path | No endpoint performs a write directly. Approving with nothing pending is a **409**, not a silent no-op, so a stray call cannot be read as success. |
| The envelope carries **no `reply`** | A client physically cannot render a queued write as a completed one. |
| Writes are **idempotent** | Keyed on `(correlation_id, tool, arguments)` and enforced by a unique index — the database settles the race, not the application. A retry within the turn collapses; the same request next week legitimately opens a second case. |

---

## 3. What one turn writes to the audit trail

```mermaid
flowchart LR
    call["tool call"] --> row["tool_audit row"]
    row --> f1["session_id + correlation_id"]
    row --> f2["tool name"]
    row --> f3["arguments — keys kept,<br/>values redacted"]
    row --> f4["result — SHA-256 only,<br/>never the payload"]
    row --> f5["latency_ms"]
    row --> f6["approver — writes only"]

    classDef note fill:none,stroke-dasharray:3 3
    class f3,f4 note
```

Three rules keep this an audit log rather than a second copy of the customer database:

- **Append-only.** `insert_one` and nothing else. There is no update or delete path.
- **Results are hashed, not stored.** The digest proves *what* the agent saw without persisting it.
- **Argument values are redacted.** The subtle case: `update_customer_contact(field="email",
  value=…)` carries the new address *in its arguments*, so hashing the result while logging raw
  args would leak precisely the field the write was about. There is a test asserting an approved
  email change lands in the database while the address appears nowhere in the trail.

An audit write failing never breaks a customer's turn — it is logged and swallowed. That is a
deliberate trade for a servicing assistant; a system that moved money would fail closed instead.

---

## 4. Why MCP, and not just functions bound to the agent

Binding Python functions straight into the agent is fewer moving parts, and for a single-framework
prototype it would be the right call. The MCP indirection buys three things that matter here:

1. **One audit point.** Every capability crosses the same boundary, so "log every tool call" is one
   place rather than a decorator someone forgets on the eighth tool.
2. **A contract the agent cannot bypass.** Tools are typed and discovered, not imported. The agent
   has no reference to a repository, so there is no path from a clever prompt to raw Mongo.
3. **The framework becomes swappable.** The Phase 11 Google ADK port binds the same MCP server and
   needs no change below the agent layer — the same eval suite runs against both runtimes.

The cost is real and worth stating: an extra process boundary, a serialization hop per call, and —
because `langchain-mcp-adapters` does not work against MCP SDK 2.x — about fifty lines of bridge
code this project owns. That tradeoff (own the glue vs. pin to a superseded SDK) is written up in
the README.

---

## Related

- [`PLAN.md`](./PLAN.md) — phase-by-phase build plan with acceptance tests
- [`../README.md`](../README.md) — quick start, tool surface, SDK notes
- [`../scripts/demo.py`](../scripts/demo.py) — the diagrams above, executed end to end
