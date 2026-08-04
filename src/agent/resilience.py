"""Time bounds and retries for tool calls.

Before this, a tool that stopped answering stopped the turn: the bridge awaited
`client.call_tool` with no bound, so a wedged Mongo connection or a broker
sitting on a socket became a customer waiting forever for a reply that was never
coming. Tool *errors* were already handled — they come back as data the agent
can read (`TOOL_ERROR from …`) — but "no answer at all" had no handling because
it had no end.

Two rules, and the asymmetry between them is the whole design:

**Everything gets a time bound.** Reads, the enqueue, and writes alike. A bound
is a promise about how long the customer waits, and it costs nothing to keep.

**Only reads get retried.** A read is a question; asking it twice is free and
the second answer is as good as the first. A write is not: if `create_case`
times out, the call may have reached Mongo and committed — the timeout says the
answer did not come back, not that the work did not happen. Retrying then risks
a second case, and the idempotency claim does not save us, because a retry
inside the same correlation scope collapses onto a claim whose result was never
stored and comes back `duplicate_in_progress` — "something happened, unclear
what". So a write is attempted once, and an unconfirmed write is reported as
exactly that. `verify_kyc_document` is in the same bucket: it claims an
idempotency key and publishes to Kafka, so a retry has the same ambiguity, and
it already carries its own broker timeout underneath.

**What counts as retryable is a failure to get an answer, not an answer we did
not like.** An MCP `is_error` result means the server ran the tool and told us
how it went. Some of those (Mongo blinked) would pass on a second attempt, but
most (unknown document, bad argument) are deterministic, and the bridge cannot
tell the two apart without parsing the server's error strings — which would tie
the retry policy to error text. Timeouts and transport failures we can classify
with certainty, so those are the ones we retry.

Why here and not `ToolRetryMiddleware`
--------------------------------------
LangChain ships one, and it does backoff, per-tool selection and exception
filtering well. Two things kept it out. It has no notion of a time bound, which
is the half of this that actually matters — retrying is the cheap part, and a
retry policy with no deadline just multiplies the wait. And it retries by
re-running the tool through the graph, so each attempt writes its own audit row:
a reader of the trail would see two `get_customer` calls and have no way to tell
a retry from the agent asking twice. Retrying *inside* the bridge keeps the
Phase 4 invariant — one row per tool call the agent made — and puts the attempt
count on the row instead.

The turn-level bound is the opposite call: `ToolCallLimitMiddleware` is exactly
right for it and is used as-is in `src.agent.graph`.
"""

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from src.config import get_settings
from src.mcp_server.server import READ_TOOLS


@dataclass(frozen=True)
class ToolPolicy:
    """How long one tool may take, and how often it may be tried."""

    attempts: int  # total attempts, including the first — 1 means no retry
    timeout_seconds: float  # bound on a single attempt
    deadline_seconds: float  # bound on the whole call, retries and backoff included
    backoff_seconds: float  # pause before the second attempt; doubles thereafter

    @property
    def retried(self) -> bool:
        return self.attempts > 1


def policy_for(tool_name: str) -> ToolPolicy:
    """The policy for one tool. Read tools retry; everything else gets one shot."""
    settings = get_settings()
    return ToolPolicy(
        attempts=max(1, settings.tool_retry_attempts) if tool_name in READ_TOOLS else 1,
        timeout_seconds=settings.tool_timeout_seconds,
        deadline_seconds=settings.tool_deadline_seconds,
        backoff_seconds=settings.tool_retry_backoff_seconds,
    )


@dataclass
class Outcome:
    """What happened across all attempts at one tool call."""

    result: Any | None
    attempts: int
    error: Exception | None
    timed_out: bool

    @property
    def ok(self) -> bool:
        return self.error is None


async def call_with_policy(policy: ToolPolicy, operation: Callable[[], Awaitable[Any]]) -> Outcome:
    """Run `operation` under `policy`, returning how it went rather than raising.

    Failure is returned, not raised, for the same reason tool errors are: the
    agent should get something it can read and act on, and the caller decides
    what the customer is told.
    """
    deadline = time.monotonic() + policy.deadline_seconds
    backoff = policy.backoff_seconds
    error: Exception | None = None
    attempts = 0

    while attempts < policy.attempts:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            # Out of budget. Starting an attempt we cannot afford to finish
            # would burn the tool's time without being able to use its answer.
            break

        attempts += 1
        try:
            # Per attempt, but never past the deadline for the call as a whole.
            async with asyncio.timeout(min(policy.timeout_seconds, remaining)):
                result = await operation()
                return Outcome(result=result, attempts=attempts, error=None, timed_out=False)
        except Exception as exc:  # noqa: BLE001 — classified below, then returned as data
            # CancelledError is a BaseException and deliberately not caught: a
            # cancelled turn is the caller giving up, not a failure to retry.
            error = exc

        if attempts < policy.attempts:
            pause = min(backoff, max(0.0, deadline - time.monotonic()))
            if pause > 0:
                await asyncio.sleep(pause)
            backoff *= 2

    return Outcome(
        result=None,
        attempts=attempts,
        # No attempt ran at all only if the deadline was already spent, which is
        # a timeout by any other name.
        error=error or TimeoutError("tool deadline exhausted before any attempt"),
        timed_out=error is None or isinstance(error, TimeoutError),
    )


# What the agent is told when a call runs out of time. The distinction between
# these two is the point: after a failed read the agent knows nothing, and after
# a failed write it knows *less than nothing* — it cannot even say whether the
# customer's record changed.
_READ_GUIDANCE = (
    "The servicing system did not answer in time, so you do not have this information. "
    "Do not guess it, recall it from earlier in the conversation, or infer it. Tell the "
    "customer their record could not be read just now, and offer to try again or to have "
    "a servicing agent follow up."
)

_UNCONFIRMED_GUIDANCE = (
    "This action was attempted once and did not confirm. It may or may not have taken "
    "effect — that is genuinely unknown. Do NOT tell the customer it succeeded and do NOT "
    "tell them it failed. Say the request could not be confirmed and that a servicing "
    "agent will check it. Do not retry this action."
)


def failure_payload(tool_name: str, outcome: Outcome, *, elapsed_ms: int) -> dict[str, Any]:
    """Render an exhausted call as data the agent can act on.

    Keyed on what the tool *is*, not on how it was configured: a read tool run
    with retries turned off is still a read, and still leaves the customer's
    record untouched.
    """
    return {
        "error": "tool_timeout" if outcome.timed_out else "tool_unavailable",
        "tool": tool_name,
        "attempts": outcome.attempts,
        "elapsed_ms": elapsed_ms,
        # The exception *type*, never its message: an exception string can carry
        # connection strings, query fragments, or a customer's data, and this
        # payload goes into the model's context and the conversation transcript.
        "detail": type(outcome.error).__name__,
        "message": _READ_GUIDANCE if tool_name in READ_TOOLS else _UNCONFIRMED_GUIDANCE,
    }
