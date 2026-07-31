"""LangGraph agent loop.

Shape:
    plan -> call tool -> observe -> repeat, with interrupt() before any write.

Tools are loaded from the MCP server via langchain-mcp-adapters, so the agent
never imports a repository or touches Mongo directly. That indirection is what
makes the audit trail complete and the runtime swappable (see src/agent/adk.py).
"""


async def build_agent():
    """Build the compiled LangGraph agent with MCP tools attached."""
    # TODO(Phase 3):
    #   1. start/connect the MCP server session
    #   2. load tools via langchain_mcp_adapters.tools.load_mcp_tools
    #   3. create_react_agent(build_llm(), tools, checkpointer=<mongo saver>)
    # TODO(Phase 5): interrupt_before the write tools so the API can surface
    #   an approval envelope and resume on POST /sessions/{id}/approve.
    raise NotImplementedError("Phase 3")
