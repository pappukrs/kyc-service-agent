"""Append-only audit of every tool invocation.

This is the artifact that makes the system auditable rather than merely
logged: given a session id you can reconstruct exactly what the agent did,
in what order, and on what data it based its answer.

Three rules, and they are the whole design:

1. **Append-only.** `insert_one` and nothing else. No update path, no delete
   path, not even for corrections. An audit log you can edit is not an audit
   log, and a bank will ask.

2. **Hash results, do not store them.** Tool results carry customer PII. The
   hash proves *what* the agent saw without the log itself becoming a second
   copy of the customer database.

3. **Redact sensitive arguments.** Less obvious than (2) and easier to get
   wrong: `update_customer_contact(customer_id, field="email", value=...)`
   carries the new email address *in the arguments*. Hashing the result but
   storing raw args would leak exactly the field the write was about.

Writing an audit row must never break a customer's turn — a failure here is
logged and swallowed. That is a deliberate trade: for a servicing assistant,
losing one audit row is preferable to failing the request. A system that
moved money would make the opposite call and fail closed.
"""

import hashlib
import logging
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from src.db import mongo

logger = logging.getLogger(__name__)

# One correlation id per inbound request, shared by every tool call the agent
# makes while handling it. A contextvar rather than a threaded-through
# parameter so the tool layer stays unaware of the API layer.
_correlation_id: ContextVar[str] = ContextVar("correlation_id", default="")

# Who authorised the write currently executing. Set by the approval endpoint
# just before it resumes a paused graph, so the audit row records *which human*
# let the write through — the question an auditor actually asks. Empty for
# reads, which need no approval.
_approver: ContextVar[str] = ContextVar("approver", default="")

# Argument names whose *values* are personal data. Matched as substrings so
# `new_email`, `contact_phone`, and `value` are all covered.
_SENSITIVE_ARG_HINTS: tuple[str, ...] = (
    "value",
    "email",
    "phone",
    "address",
    "name",
    "dob",
    "date_of_birth",
    "pan",
    "aadhaar",
    "passport",
)

REDACTED = "[redacted]"


def new_correlation_id() -> str:
    """Start a new correlation scope and return its id."""
    cid = uuid4().hex
    _correlation_id.set(cid)
    return cid


def get_correlation_id() -> str:
    """Current correlation id, minting one if nothing set it.

    The fallback matters: a tool called outside a request (a script, a test)
    still produces a well-formed audit row rather than an empty field.
    """
    return _correlation_id.get() or new_correlation_id()


def set_approver(approver: str) -> None:
    """Record who authorised the write about to execute."""
    _approver.set(approver)


def clear_approver() -> None:
    """Drop the approver once the approved write has run.

    Not housekeeping — an approver left set would attach one human's
    authorisation to a later, unapproved write in the same context.
    """
    _approver.set("")


def get_approver() -> str | None:
    return _approver.get() or None


def redact_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    """Replace personal-data argument values with a marker.

    Keys are kept — knowing that `value` was supplied is part of the audit
    trail. Only the value is dropped.
    """
    return {
        key: (REDACTED if any(hint in key.lower() for hint in _SENSITIVE_ARG_HINTS) else value)
        for key, value in arguments.items()
    }


def hash_result(result: str) -> str:
    """Stable fingerprint of a tool result.

    Two calls returning identical data produce identical hashes, so you can
    tell a repeated read from a changed one without storing either payload.
    """
    return hashlib.sha256(result.encode("utf-8")).hexdigest()


async def record_tool_call(
    *,
    session_id: str,
    tool_name: str,
    arguments: dict[str, Any],
    result: str,
    latency_ms: int,
    is_error: bool = False,
    error_message: str | None = None,
    approved_by: str | None = None,
    attempts: int = 1,
    timed_out: bool = False,
) -> None:
    """Append one immutable row describing a tool invocation."""
    entry = {
        "correlation_id": get_correlation_id(),
        "session_id": session_id,
        "tool_name": tool_name,
        "arguments": redact_arguments(arguments),
        "result_sha256": hash_result(result),
        "result_bytes": len(result.encode("utf-8")),
        "latency_ms": latency_ms,
        # One row per tool call the agent made, however many attempts it took —
        # so the trail never reads as though the agent asked the same question
        # twice when it was the bridge retrying. `latency_ms` is the total the
        # customer waited, retries and backoff included.
        "attempts": attempts,
        "timed_out": timed_out,
        "is_error": is_error,
        "error_message": error_message,
        # Set for approved writes, None for reads. Falls back to the ambient
        # approver so the tool layer does not have to thread it through.
        "approved_by": approved_by or get_approver(),
        "created_at": datetime.now(UTC),
    }

    try:
        await mongo.get_db().tool_audit.insert_one(entry)
    except Exception:  # noqa: BLE001 — see module docstring
        logger.exception("failed to write audit row for tool=%s session=%s", tool_name, session_id)


async def read_session_trail(session_id: str) -> list[dict[str, Any]]:
    """Every tool call made in a session, oldest first.

    The reconstruction path — this is what you show someone who asks "why did
    it tell the customer that?".
    """
    cursor = (
        mongo.get_db().tool_audit.find({"session_id": session_id}, {"_id": 0}).sort("created_at", 1)
    )
    return await cursor.to_list(length=None)
