"""Domain model for banking onboarding + KYC servicing.

Deliberately small. The point of the project is the agent architecture around
this data, not the data model itself.

All of it is synthetic — see scripts/seed.py.
"""

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class OnboardingStage(StrEnum):
    INITIATED = "initiated"
    DOCS_PENDING = "docs_pending"
    UNDER_REVIEW = "under_review"
    ADDITIONAL_INFO_REQUIRED = "additional_info_required"
    APPROVED = "approved"
    REJECTED = "rejected"


class DocumentStatus(StrEnum):
    PENDING = "pending"
    VERIFYING = "verifying"
    VERIFIED = "verified"
    REJECTED = "rejected"
    EXPIRED = "expired"


class DocumentType(StrEnum):
    PAN = "pan"
    AADHAAR = "aadhaar"
    PASSPORT = "passport"
    ADDRESS_PROOF = "address_proof"
    PHOTO = "photo"


class RiskTier(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class CaseStatus(StrEnum):
    OPEN = "open"
    IN_PROGRESS = "in_progress"
    RESOLVED = "resolved"


class Customer(BaseModel):
    customer_id: str
    full_name: str
    email: str
    phone: str
    city: str
    onboarding_stage: OnboardingStage
    risk_tier: RiskTier
    created_at: datetime
    updated_at: datetime


class KycDocument(BaseModel):
    document_id: str
    customer_id: str
    doc_type: DocumentType
    status: DocumentStatus
    # Populated only when status == REJECTED. This is the field the agent most
    # often needs — it's the answer to "why am I blocked?".
    rejection_reason: str | None = None
    submitted_at: datetime
    reviewed_at: datetime | None = None


class ServicingCase(BaseModel):
    case_id: str
    customer_id: str
    category: str
    summary: str
    status: CaseStatus = CaseStatus.OPEN
    created_at: datetime
    # Which conversation opened this, and who approved the write.
    session_id: str | None = None
    approved_by: str | None = None


class ToolAuditEntry(BaseModel):
    """Append-only. One document per tool invocation — never updated, never deleted.

    This is the artifact that makes the system auditable: given a session_id you
    can reconstruct exactly what the agent did and on what data it based its answer.

    Rows are written by `src.obs.audit.record_tool_call`; this model documents
    and validates the shape. See that module for why results are hashed and
    which arguments are redacted.
    """

    # Shared by every tool call made while handling one inbound request, so the
    # trail can be grouped by turn as well as by session.
    correlation_id: str
    session_id: str
    tool_name: str
    # Personal-data values replaced with a marker; keys retained.
    arguments: dict[str, Any]
    # Hash rather than the payload — results carry PII, and the log must not
    # become a second copy of the customer database.
    result_sha256: str
    result_bytes: int
    latency_ms: int
    is_error: bool = False
    error_message: str | None = None
    # Set for write tools once approved; None for reads.
    approved_by: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
