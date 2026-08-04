"""LangGraph agent loop.

Shape:
    plan → call tool → observe → repeat, pausing before any write.

Tools come from the MCP server via `src.agent.mcp_tools`, so the agent never
imports a repository and never touches Mongo directly. That indirection is what
makes the Phase 4 audit trail complete and the runtime swappable — the Phase 11
Google ADK port binds the same MCP tools and needs no change below this line.
"""

from contextlib import asynccontextmanager
from typing import Any

# LangGraph 1.0 moved the prebuilt agent to langchain.agents.create_agent;
# langgraph.prebuilt.create_react_agent still imports but emits a deprecation
# warning and goes away in 2.0. Note the kwarg renamed too: prompt → system_prompt.
from langchain.agents import create_agent
from langchain.agents.middleware import HumanInTheLoopMiddleware, ToolCallLimitMiddleware

from src.agent.llm import build_llm
from src.agent.mcp_tools import connect, load_mcp_tools, load_read_tools
from src.agent.prompt import SYSTEM_PROMPT
from src.config import get_settings
from src.mcp_server.server import WRITE_TOOLS, mcp

# WRITE_TOOLS — the tools that mutate state — is declared next to the tools
# themselves in src/mcp_server/server.py, so the gate and the tool surface
# cannot disagree about what a write is. Re-exported here because the gate is
# what most readers come looking for.
__all__ = ["WRITE_TOOLS", "agent_session"]


@asynccontextmanager
async def agent_session(
    *,
    model: Any = None,
    read_only: bool = False,
    checkpointer: Any = None,
    server: Any = None,
):
    """Yield a compiled agent bound to a live MCP session.

    The MCP client is a context manager, and the tools close over it — so the
    agent is only valid inside this block. Hence a context manager rather than a
    plain `build_agent()`: it makes the tool session's lifetime explicit instead
    of leaving a half-dead agent object lying around.

    Args:
        model: a LangChain chat model. Defaults to the configured provider;
            tests pass a fake so the graph is exercised without an API key.
        read_only: bind only the four read tools. An agent that was never given
            a write tool cannot be talked into calling one — defence in depth
            alongside the approval gate.
        checkpointer: LangGraph checkpointer for conversation state (Phase 4).
        server: MCPServer to connect to. Defaults to this project's server.
    """
    async with connect(server or mcp) as client:
        tools = await (load_read_tools(client) if read_only else load_mcp_tools(client))

        # A bound on the turn, alongside the per-call bound in
        # src/agent/resilience.py: a model that keeps calling tools and a tool
        # that never returns are the same thing to the customer, which is no
        # answer. Unlike the timeout, the shipped middleware is exactly right
        # here, so it is used as-is.
        #
        # `continue` rather than `end`: the model is told it may not call any
        # more tools and has to answer from what it already has, which produces
        # a real reply. `end` would hand the customer the framework's own
        # "run limit exceeded (9/8 calls)". This is the soft cap; LangGraph's
        # recursion limit is the hard one underneath it.
        middleware: list[Any] = [
            ToolCallLimitMiddleware(
                run_limit=get_settings().max_tool_calls_per_turn,
                exit_behavior="continue",
            )
        ]

        # The write gate. Execution halts *before* a write runs, so the API can
        # surface an approval envelope and resume only on an explicit decision.
        # The prompt tells the agent to expect the pause, but the pause does not
        # depend on the model obeying anything — a guardrail that lives only in
        # a prompt is not a guardrail.
        #
        # Per tool, not per node: every tool shares one graph node, so the
        # graph-level `interrupt_before` would have gated reads too. This
        # middleware is the mechanism that discriminates by tool name.
        #
        # Nothing to guard in read-only mode — no write tool is even bound.
        if not read_only:
            middleware.append(
                HumanInTheLoopMiddleware(
                    interrupt_on={name: True for name in WRITE_TOOLS},
                    description_prefix="This action changes customer records and needs approval",
                )
            )

        yield create_agent(
            model=model if model is not None else build_llm(),
            tools=tools,
            system_prompt=SYSTEM_PROMPT,
            checkpointer=checkpointer,
            middleware=middleware,
        )
