"""Phase 7 — async document verification.

No Kafka here. `queue_verification` and `apply_verification` take a publisher,
so an in-memory one exercises the whole feature: the enqueue, the state machine,
redelivery, case updates and the audit event. What is *not* covered is aiokafka
itself — that lives in `consumer.py`, which is a loop and nothing else.

The behavioural risk this phase carries is one sentence: the agent must say
verification is **in progress**, never that it succeeded. That is what the
"tool never asserts an outcome" tests are for.
"""

from datetime import UTC, datetime
from typing import Any

import pytest
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver
from mongomock_motor import AsyncMongoMockClient

from src.db import mongo as mongo_module
from src.db import repositories
from src.domain.models import VerificationTask
from src.messaging.broker import InMemoryPublisher
from src.worker import verification

NOW = datetime(2026, 7, 1, tzinfo=UTC)
TASKS = "servicing.tasks"
AUDIT = "servicing.audit"

# Deterministic outcomes (sha256 of the id, first byte < 192 passes), picked
# once here so the tests read as "a document that passes" rather than as magic
# ids. Asserted in test_outcome_ids_are_what_the_tests_assume — if the rule
# changes, that test fails loudly instead of these silently testing nothing.
DOC_PASSES = "DOC-014-1"
DOC_FAILS = "DOC-014-4"


@pytest.fixture(autouse=True)
def fake_db(monkeypatch):
    db = AsyncMongoMockClient()["kyc_verification_test"]
    monkeypatch.setattr(mongo_module, "get_db", lambda: db)
    return db


@pytest.fixture(autouse=True)
def no_latency(monkeypatch):
    """The simulated scan delay is real time; tests do not pay for it."""
    from src.config import get_settings

    monkeypatch.setattr(get_settings(), "verification_latency_seconds", 0.0)


@pytest.fixture
def publisher():
    return InMemoryPublisher()


@pytest.fixture
async def seeded(fake_db):
    await fake_db.customers.insert_one(
        {
            "customer_id": "CUST-014",
            "full_name": "Meera Nair",
            "email": "user014@example.invalid",
            "phone": "+91 9000000014",
            "city": "Chennai",
            "onboarding_stage": "additional_info_required",
            "risk_tier": "high",
            "created_at": NOW,
            "updated_at": NOW,
        }
    )
    await fake_db.kyc_documents.insert_many(
        [
            {
                "document_id": DOC_PASSES,
                "customer_id": "CUST-014",
                "doc_type": "address_proof",
                "status": "rejected",
                "rejection_reason": "Document image is blurred; text is not machine-readable.",
                "submitted_at": NOW,
                "reviewed_at": NOW,
            },
            {
                "document_id": DOC_FAILS,
                "customer_id": "CUST-014",
                "doc_type": "pan",
                "status": "rejected",
                "rejection_reason": "Document image is blurred; text is not machine-readable.",
                "submitted_at": NOW,
                "reviewed_at": NOW,
            },
            {
                "document_id": "DOC-014-2",
                "customer_id": "CUST-014",
                "doc_type": "passport",
                "status": "verified",
                "rejection_reason": None,
                "submitted_at": NOW,
                "reviewed_at": NOW,
            },
            {
                "document_id": "DOC-014-9",
                "customer_id": "CUST-014",
                "doc_type": "aadhaar",
                "status": "expired",
                "rejection_reason": None,
                "submitted_at": NOW,
                "reviewed_at": NOW,
            },
        ]
    )
    # The unique index idempotency relies on. mongomock honours it.
    await fake_db.idempotency_keys.create_index("key", unique=True)
    return fake_db


async def queue(db, publisher, document_id: str, scope: str = "corr-1") -> dict[str, Any]:
    return await verification.queue_verification(
        db, document_id=document_id, scope=scope, publisher=publisher
    )


# --------------------------------------------------------------------------- #
# Enqueue
# --------------------------------------------------------------------------- #


async def test_queue_publishes_one_task_and_returns_immediately(seeded, publisher):
    result = await queue(seeded, publisher, DOC_PASSES)

    assert result["status"] == "queued"
    assert result["task_id"].startswith("VER-")

    (task,) = publisher.topic(TASKS)
    assert task["document_id"] == DOC_PASSES
    assert task["customer_id"] == "CUST-014"
    # The correlation id crosses the process boundary, so the async work stays
    # attributable to the turn that asked for it.
    assert task["correlation_id"] == "corr-1"


async def test_queue_never_asserts_an_outcome(seeded, publisher):
    """The one sentence this phase must not get wrong."""
    result = await queue(seeded, publisher, DOC_PASSES)

    assert result["status"] == "queued"
    assert "verified" not in result["message"].lower().replace("not been verified", "")
    assert "IN PROGRESS" in result["message"]
    # And the document itself does not claim success either.
    document = await repositories.find_document(seeded, DOC_PASSES)
    assert document["status"] == "verifying"


async def test_queue_moves_the_document_into_verifying_so_reads_show_it(seeded, publisher):
    """A customer asking again mid-flight must see progress, not the old rejection."""
    await queue(seeded, publisher, DOC_PASSES)

    status = await repositories.build_onboarding_status(seeded, "CUST-014")
    blocker = next(b for b in status["blockers"] if b["document_id"] == DOC_PASSES)

    assert blocker["status"] == "verifying"
    assert blocker["needs_customer_action"] is False
    assert "in progress" in blocker["customer_action"].lower()


async def test_unknown_document_is_data_not_an_exception(seeded, publisher):
    result = await queue(seeded, publisher, "DOC-NOPE")

    assert result["error"] == "document_not_found"
    assert publisher.messages == []


async def test_already_verified_document_is_not_requeued(seeded, publisher):
    result = await queue(seeded, publisher, "DOC-014-2")

    assert result["status"] == "already_verified"
    assert publisher.messages == []
    # And it was not knocked back into `verifying`.
    document = await repositories.find_document(seeded, "DOC-014-2")
    assert document["status"] == "verified"


async def test_second_request_in_one_turn_queues_nothing_new(seeded, publisher):
    first = await queue(seeded, publisher, DOC_PASSES, scope="corr-1")
    second = await queue(seeded, publisher, DOC_PASSES, scope="corr-1")

    assert second["idempotent_replay"] is True
    assert second["task_id"] == first["task_id"]
    assert len(publisher.topic(TASKS)) == 1


async def test_asking_again_in_a_later_turn_does_queue_again(seeded, publisher):
    """Idempotency is scoped to the turn — a fresh request is not a duplicate."""
    first = await queue(seeded, publisher, DOC_PASSES, scope="corr-1")
    second = await queue(seeded, publisher, DOC_PASSES, scope="corr-2")

    assert second["status"] == "queued"
    assert second["task_id"] != first["task_id"]
    assert len(publisher.topic(TASKS)) == 2


class BrokenPublisher:
    """A broker that is down. The `error` decides which failure mode."""

    def __init__(self, error: Exception | None = None) -> None:
        self.error = error or ConnectionError("broker down")

    async def publish(self, topic: str, message: dict) -> None:
        raise self.error

    async def close(self) -> None:
        pass


@pytest.mark.parametrize(
    "error",
    [
        ConnectionError("broker down"),
        # The bounded-publish path: an unreachable broker that never refuses,
        # it just never answers. Indistinguishable from a hang without a bound.
        TimeoutError(),
    ],
)
async def test_broker_outage_changes_nothing_and_says_so(seeded, error):
    result = await queue(seeded, BrokenPublisher(error), DOC_PASSES)

    assert result["error"] == "verification_unavailable"
    assert "Nothing has been submitted" in result["message"]
    # The document must not be left claiming a re-check that was never queued.
    document = await repositories.find_document(seeded, DOC_PASSES)
    assert document["status"] == "rejected"


async def test_retry_after_a_broker_outage_is_not_deduped(seeded, publisher):
    """The failed claim is released, or the retry silently replays a non-write."""
    await queue(seeded, BrokenPublisher(), DOC_PASSES, scope="corr-1")
    result = await queue(seeded, publisher, DOC_PASSES, scope="corr-1")

    assert result["status"] == "queued"
    assert len(publisher.topic(TASKS)) == 1


# --------------------------------------------------------------------------- #
# The worker
# --------------------------------------------------------------------------- #


def test_outcome_ids_are_what_the_tests_assume():
    """Guards the fixture ids against a change in the outcome rule."""
    assert verification.decide_outcome({"document_id": DOC_PASSES, "status": "rejected"}) == (
        "verified",
        None,
    )
    outcome, reason = verification.decide_outcome({"document_id": DOC_FAILS, "status": "rejected"})
    assert outcome == "rejected"
    assert reason


def test_outcome_is_deterministic():
    """A demo should tell the same story twice, and an eval should be assertable."""
    document = {"document_id": DOC_PASSES, "status": "rejected"}
    assert verification.decide_outcome(document) == verification.decide_outcome(document)


def test_an_expired_document_always_fails_and_says_why():
    outcome, reason = verification.decide_outcome({"document_id": DOC_PASSES, "status": "expired"})
    assert outcome == "rejected"
    assert "expired" in reason.lower()
    assert "submit" in reason.lower()  # tells the customer what to actually do


async def task_for(db, publisher, document_id: str, scope: str = "corr-1") -> VerificationTask:
    result = await queue(db, publisher, document_id, scope=scope)
    (message,) = [m for m in publisher.topic(TASKS) if m["task_id"] == result["task_id"]]
    return VerificationTask.model_validate(message)


async def test_a_pass_verifies_the_document_and_clears_the_stale_reason(seeded, publisher):
    task = await task_for(seeded, publisher, DOC_PASSES)

    result = await verification.apply_verification(seeded, task, publisher=publisher)

    assert result["outcome"] == "verified"
    document = await repositories.find_document(seeded, DOC_PASSES)
    assert document["status"] == "verified"
    # A verified document that still carries "image is blurred" would have the
    # read tools explaining a rejection that no longer exists.
    assert document["rejection_reason"] is None
    assert document["verification"]["completed_at"] is not None


async def test_a_failure_records_a_reason_the_customer_can_act_on(seeded, publisher):
    task = await task_for(seeded, publisher, DOC_FAILS)

    result = await verification.apply_verification(seeded, task, publisher=publisher)

    assert result["outcome"] == "rejected"
    document = await repositories.find_document(seeded, DOC_FAILS)
    assert document["status"] == "rejected"
    assert "resubmit" in document["rejection_reason"].lower()


async def test_an_expired_document_stays_expired_through_the_whole_path(seeded, publisher):
    """DOC-014-9 hashes to a pass — only the expiry rule stops it.

    And it only stops it if the worker can still see the *submitted* status:
    the enqueue overwrote it with `verifying`, so this also pins the
    previous_status hand-off between the two halves.
    """
    task = await task_for(seeded, publisher, "DOC-014-9")

    result = await verification.apply_verification(seeded, task, publisher=publisher)

    assert result["outcome"] == "rejected"
    document = await repositories.find_document(seeded, "DOC-014-9")
    assert "expired" in document["rejection_reason"].lower()


async def test_redelivery_is_dropped(seeded, publisher):
    """Kafka is at-least-once; the second delivery must not double-write."""
    task = await task_for(seeded, publisher, DOC_PASSES)
    await verification.apply_verification(seeded, task, publisher=publisher)

    again = await verification.apply_verification(seeded, task, publisher=publisher)

    assert again["status"] == "duplicate_ignored"
    assert len(publisher.topic(AUDIT)) == 1


async def test_a_live_case_gets_the_outcome_appended(seeded, publisher):
    await seeded.servicing_cases.insert_many(
        [
            {
                "case_id": "CASE-OPEN",
                "customer_id": "CUST-014",
                "category": "document_review",
                "summary": "Address proof rejected",
                "status": "open",
                "created_at": NOW,
                "updates": [],
            },
            {
                "case_id": "CASE-DONE",
                "customer_id": "CUST-014",
                "category": "document_review",
                "summary": "An older, closed case",
                "status": "resolved",
                "created_at": NOW,
                "updates": [],
            },
        ]
    )
    task = await task_for(seeded, publisher, DOC_PASSES)

    result = await verification.apply_verification(seeded, task, publisher=publisher)

    assert result["cases_updated"] == 1
    live = await seeded.servicing_cases.find_one({"case_id": "CASE-OPEN"})
    (update,) = live["updates"]
    assert update["outcome"] == "verified"
    assert update["document_id"] == DOC_PASSES
    # History is not rewritten.
    closed = await seeded.servicing_cases.find_one({"case_id": "CASE-DONE"})
    assert closed["updates"] == []


async def test_audit_event_carries_the_correlation_id_but_no_pii(seeded, publisher):
    task = await task_for(seeded, publisher, DOC_PASSES)

    await verification.apply_verification(seeded, task, publisher=publisher)

    (event,) = publisher.topic(AUDIT)
    assert event["event"] == "kyc_document_verified"
    assert event["outcome"] == "verified"
    # Stitches the async work back to the turn that requested it.
    assert event["correlation_id"] == "corr-1"
    # Same rule as the Mongo trail: ids and an outcome, never the customer.
    assert "Meera Nair" not in str(event)
    assert "user014@example.invalid" not in str(event)


async def test_a_deleted_document_does_not_crash_the_worker(seeded, publisher):
    task = await task_for(seeded, publisher, DOC_PASSES)
    await seeded.kyc_documents.delete_one({"document_id": DOC_PASSES})

    result = await verification.apply_verification(seeded, task, publisher=publisher)

    assert result["status"] == "document_missing"
    (event,) = publisher.topic(AUDIT)
    assert event["outcome"] == "document_missing"


async def test_a_failed_audit_emit_does_not_lose_the_verification(seeded, publisher):
    """The document update is the record the customer's answer depends on."""
    task = await task_for(seeded, publisher, DOC_PASSES)

    class AuditOnlyFailure:
        async def publish(self, topic: str, message: dict) -> None:
            raise ConnectionError("broker down")

        async def close(self) -> None:
            pass

    result = await verification.apply_verification(seeded, task, publisher=AuditOnlyFailure())

    assert result["outcome"] == "verified"
    document = await repositories.find_document(seeded, DOC_PASSES)
    assert document["status"] == "verified"


# --------------------------------------------------------------------------- #
# Consumer glue
#
# The aiokafka loop is not covered — it needs a broker. What is covered is what
# the loop does with a message, which is where the interesting decisions are.
# --------------------------------------------------------------------------- #


async def test_a_valid_message_reaches_the_handler(seeded, publisher, monkeypatch):
    from src.messaging import broker
    from src.worker import consumer

    monkeypatch.setattr(broker, "get_publisher", lambda: publisher)
    task = await task_for(seeded, publisher, DOC_PASSES)

    await consumer._handle(seeded, task.model_dump(mode="json"))

    document = await repositories.find_document(seeded, DOC_PASSES)
    assert document["status"] == "verified"


async def test_a_poison_message_is_dropped_rather_than_wedging_the_partition(seeded):
    """One malformed task must not stop verification for every other customer."""
    from src.worker import consumer

    await consumer._handle(seeded, {"nonsense": True})  # must not raise


async def test_one_failing_task_does_not_take_the_worker_down(seeded, publisher, monkeypatch):
    from src.worker import consumer

    async def boom(*_args, **_kwargs):
        raise ConnectionError("mongo is down")

    task = await task_for(seeded, publisher, DOC_PASSES)
    monkeypatch.setattr(consumer.verification, "apply_verification", boom)

    await consumer._handle(seeded, task.model_dump(mode="json"))  # must not raise


# --------------------------------------------------------------------------- #
# Through the agent — the tool as the model actually reaches it
# --------------------------------------------------------------------------- #


class ScriptedChatModel(FakeMessagesListChatModel):
    def bind_tools(self, tools: Any, **kwargs: Any):  # noqa: ARG002
        return self


async def test_agent_can_call_the_tool_end_to_end(seeded, publisher, monkeypatch):
    """Through MCP and the bridge, with no approval gate — verification is not a write."""
    from src.agent.graph import agent_session
    from src.messaging import broker
    from src.obs import audit

    monkeypatch.setattr(broker, "get_publisher", lambda: publisher)
    audit.new_correlation_id()

    model = ScriptedChatModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "verify_kyc_document",
                        "args": {"document_id": DOC_PASSES},
                        "id": "c1",
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(content="I've asked for it to be re-checked; it's in progress."),
        ]
    )

    async with agent_session(model=model, checkpointer=InMemorySaver()) as agent:
        result = await agent.ainvoke(
            {"messages": [("user", f"please recheck {DOC_PASSES}")]},
            config={"configurable": {"thread_id": "sess-verify"}},
        )

    # It ran rather than pausing: an async re-check is not a customer-record write.
    assert not result.get("__interrupt__")
    assert len(publisher.topic(TASKS)) == 1

    # And the call is on the audit trail like any other.
    trail = await audit.read_session_trail("sess-verify")
    assert [row["tool_name"] for row in trail] == ["verify_kyc_document"]
    assert trail[0]["is_error"] is False
