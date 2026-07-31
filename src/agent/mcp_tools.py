"""Bridge: MCP tools → LangChain tools.

Why this file exists instead of `langchain-mcp-adapters`
--------------------------------------------------------
The obvious choice is `langchain-mcp-adapters`. As of 0.3.1 (its latest release)
it imports `mcp.server.fastmcp.tools`, a module removed in **MCP SDK 2.0** — so
`import langchain_mcp_adapters.tools` raises ModuleNotFoundError against a
current SDK. pip does not catch this: the package depends on an unpinned `mcp`.

The options were to pin the project to a superseded SDK, or to own ~50 lines of
glue. This is the glue. It is small, it has no third-party dependency beyond the
two SDKs themselves, and it means the project tracks the current MCP spec.

What it does
------------
Reads the tool list off a live MCP session and wraps each entry as a LangChain
`StructuredTool`, carrying the name, the description (which is load-bearing —
the model reads it to decide *when* to call), and the JSON Schema for arguments.

The important property: the agent still cannot reach the database. It calls a
LangChain tool, which calls MCP, which calls the repository. Every hop is
observable, which is what makes the Phase 4 audit trail complete.
"""

import json
from typing import Any

from langchain_core.tools import StructuredTool
from mcp.client.client import Client
from mcp.server.mcpserver import MCPServer


def _flatten_result(result: Any) -> str:
    """Turn an MCP CallToolResult into text the model can read.

    MCP returns a list of typed content blocks. Models reason over text, so
    concatenate the text blocks. Structured content, when the server provides
    it, is preferred — it round-trips types more faithfully than a rendering.
    """
    structured = getattr(result, "structured_content", None)
    if structured is not None:
        return json.dumps(structured, default=str)

    parts: list[str] = []
    for block in getattr(result, "content", []) or []:
        text = getattr(block, "text", None)
        if text is not None:
            parts.append(text)
    return "\n".join(parts) if parts else "(no content)"


def _make_tool(
    client: Client, name: str, description: str, schema: dict[str, Any]
) -> StructuredTool:
    async def _call(**kwargs: Any) -> str:
        result = await client.call_tool(name, kwargs)
        # A tool-level error is data, not an exception: the agent should read it
        # and recover (ask a clarifying question, try another tool) rather than
        # having the turn blow up underneath it.
        if getattr(result, "is_error", False):
            return f"TOOL_ERROR from {name}: {_flatten_result(result)}"
        return _flatten_result(result)

    return StructuredTool(
        name=name,
        description=description,
        args_schema=schema,
        coroutine=_call,
    )


async def load_mcp_tools(client: Client) -> list[StructuredTool]:
    """Load every tool advertised by the MCP server as a LangChain tool."""
    listing = await client.list_tools()
    return [
        _make_tool(
            client,
            name=tool.name,
            description=tool.description or "",
            # MCP 2.x renamed this from `inputSchema`; 1.x examples still use the
            # camelCase form and will AttributeError against a current SDK.
            schema=tool.input_schema,
        )
        for tool in listing.tools
    ]


async def load_read_tools(client: Client) -> list[StructuredTool]:
    """Load only the read tools.

    Useful for a read-only agent variant and for evals that assert a write can
    never happen — if the tool is not bound, no prompt can talk the model into
    calling it. Defence in depth alongside the Phase 5 approval gate.
    """
    read_only = {
        "get_customer",
        "get_onboarding_status",
        "list_kyc_documents",
        "search_servicing_kb",
    }
    return [t for t in await load_mcp_tools(client) if t.name in read_only]


def connect(server: MCPServer) -> Client:
    """Open a client against an MCPServer instance.

    MCP 2.x lets a Client take a server object directly, which connects
    in-process — no subprocess, no stdio pipe. That keeps tests fast and
    deterministic. Swap this for a transport/URL to run the server out of
    process without changing anything downstream.
    """
    return Client(server)
