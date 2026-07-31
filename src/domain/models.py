"""Domain model for banking onboarding + KYC servicing.

Deliberately small. The point of the project is the agent architecture around
this data, not the data model itself.

All of it is synthetic — see scripts/seed.py.
"""

from datetime import datetime
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
    """

    correlation_id: str
    session_id: str
    tool_name: str
    arguments: dict[str, Any]
    # Hash rather than the payload — results can contain PII.
    result_sha256: str
    latency_ms: int
    is_error: bool = False
    error_message: str | None = None
    # Set for write tools; None for reads.
    approved_by: str | None = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
