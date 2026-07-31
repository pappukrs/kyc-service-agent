"""Conversation state persistence.

LangGraph calls this a checkpointer: it stores the graph state per thread, so a
session survives a process restart and — the part that matters here — a graph
paused at an approval can be resumed by a *different* process than the one that
paused it. Without persistence, restarting the API would silently drop every
write awaiting a human decision.

`InMemorySaver` remains the right choice in tests: deterministic, no I/O, and a
test that needed durable state would be testing the library rather than us.
"""

from contextlib import asynccontextmanager

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.mongodb import MongoDBSaver

from src.config import get_settings


@asynccontextmanager
async def checkpointer(*, in_memory: bool = False):
    """Yield a checkpointer, Mongo-backed unless told otherwise.

    Writes to its own collections (`checkpoints`, `checkpoint_writes`) in the
    application database — deliberately separate from `tool_audit`. They answer
    different questions and have opposite lifecycles: checkpoints are mutable
    working state that can be pruned; the audit trail is immutable and kept.
    """
    if in_memory:
        yield InMemorySaver()
        return

    settings = get_settings()
    with MongoDBSaver.from_conn_string(
        settings.mongo_uri,
        db_name=settings.mongo_db,
    ) as saver:
        yield saver
