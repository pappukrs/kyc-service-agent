"""Seed the database with SYNTHETIC customers, documents, and KB articles.

Nothing here is real. Names are generated, IDs are sequential, and every
document is fictional. Run:  python -m scripts.seed
"""

import asyncio
import random
from datetime import UTC, datetime, timedelta

from src.db import mongo
from src.domain.models import DocumentStatus, DocumentType, OnboardingStage, RiskTier

random.seed(42)  # reproducible dataset — the eval suite depends on it

FIRST = ["Aarav", "Diya", "Kabir", "Isha", "Rohan", "Meera", "Arjun", "Sana", "Vikram", "Neha"]
LAST = ["Sharma", "Iyer", "Nair", "Bose", "Reddy", "Kulkarni", "Menon", "Chopra", "Das", "Rao"]
CITIES = ["Bengaluru", "Mumbai", "Delhi", "Hyderabad", "Pune", "Chennai"]

REJECTION_REASONS = [
    "Document image is blurred; text is not machine-readable.",
    "Name on document does not match the name on the application.",
    "Document has expired.",
    "Address proof is older than 3 months.",
    "Photograph does not meet the required dimensions.",
]

KB_ARTICLES = [
    {
        "title": "Accepted address proofs",
        "body": "Utility bills (electricity, gas, water) dated within the last 3 months, "
        "bank statements within 3 months, registered rent agreements, and passports "
        "are accepted as address proof.",
        "tags": ["address", "documents", "kyc"],
    },
    {
        "title": "KYC verification timelines",
        "body": "Standard KYC verification completes within 2 business days. Cases flagged "
        "for enhanced due diligence may take up to 7 business days.",
        "tags": ["timeline", "kyc", "verification"],
    },
    {
        "title": "Resubmitting a rejected document",
        "body": "A rejected document can be resubmitted at any time from the onboarding "
        "portal. Resubmission resets the review clock; there is no limit on attempts.",
        "tags": ["rejected", "resubmit", "documents"],
    },
    {
        "title": "Changing registered contact details",
        "body": "Email, phone, and city can be updated by the customer at any stage. "
        "Changes to name or date of birth require a fresh KYC cycle.",
        "tags": ["contact", "update", "profile"],
    },
]

N_CUSTOMERS = 50


async def main() -> None:
    db = mongo.get_db()
    await mongo.ensure_indexes()

    print("Clearing synthetic collections…")
    for coll in ("customers", "kyc_documents", "kb_articles", "servicing_cases"):
        await db[coll].delete_many({})

    now = datetime.now(UTC)
    customers, documents = [], []

    for i in range(1, N_CUSTOMERS + 1):
        cid = f"CUST-{i:03d}"
        stage = random.choice(list(OnboardingStage))
        created = now - timedelta(days=random.randint(1, 90))

        customers.append(
            {
                "customer_id": cid,
                "full_name": f"{random.choice(FIRST)} {random.choice(LAST)}",
                "email": f"user{i:03d}@example.invalid",
                "phone": f"+91 9{random.randint(100000000, 999999999)}",
                "city": random.choice(CITIES),
                "onboarding_stage": stage.value,
                "risk_tier": random.choice(list(RiskTier)).value,
                "created_at": created,
                "updated_at": created + timedelta(days=random.randint(0, 5)),
            }
        )

        # 2–4 documents each, with status correlated to the onboarding stage
        for j, doc_type in enumerate(random.sample(list(DocumentType), random.randint(2, 4)), 1):
            if stage == OnboardingStage.APPROVED:
                status = DocumentStatus.VERIFIED
            elif stage in (OnboardingStage.REJECTED, OnboardingStage.ADDITIONAL_INFO_REQUIRED):
                status = random.choice([DocumentStatus.REJECTED, DocumentStatus.VERIFIED])
            else:
                status = random.choice(list(DocumentStatus))

            documents.append(
                {
                    "document_id": f"DOC-{i:03d}-{j}",
                    "customer_id": cid,
                    "doc_type": doc_type.value,
                    "status": status.value,
                    "rejection_reason": (
                        random.choice(REJECTION_REASONS)
                        if status == DocumentStatus.REJECTED
                        else None
                    ),
                    "submitted_at": created + timedelta(days=1),
                    "reviewed_at": (
                        created + timedelta(days=2)
                        if status in (DocumentStatus.VERIFIED, DocumentStatus.REJECTED)
                        else None
                    ),
                }
            )

    await db.customers.insert_many(customers)
    await db.kyc_documents.insert_many(documents)
    await db.kb_articles.insert_many(KB_ARTICLES)

    rejected = sum(1 for d in documents if d["status"] == DocumentStatus.REJECTED.value)
    print(f"  {len(customers)} customers")
    print(f"  {len(documents)} documents ({rejected} rejected — useful for eval scenarios)")
    print(f"  {len(KB_ARTICLES)} KB articles")
    print("Done. All data is synthetic.")


if __name__ == "__main__":
    asyncio.run(main())
