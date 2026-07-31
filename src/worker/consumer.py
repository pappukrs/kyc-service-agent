"""Kafka worker — async KYC document verification.

verify_kyc_document returns immediately with {"status": "queued"}; this worker
does the slow part and updates the document + case when it finishes.
"""

import asyncio


async def main() -> None:
    # TODO(Phase 7):
    #   - aiokafka consumer on KAFKA_TASKS_TOPIC, consumer group "kyc-verifier"
    #   - verify the document (simulate latency + a pass/fail outcome)
    #   - update kyc_documents.status and the linked servicing case
    #   - emit an audit event to KAFKA_AUDIT_TOPIC
    raise NotImplementedError("Phase 7")


if __name__ == "__main__":
    asyncio.run(main())
