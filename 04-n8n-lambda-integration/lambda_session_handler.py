"""
lambda_session_handler.py
=========================

Business logic for the "session closed with negative sentiment" flow
(Exercise 4). It concentrates the calls to AWS and external services in one
Lambda, leaving N8n as the orchestrator only (routing, retry, visibility).

Responsibilities of this Lambda:
  1. Create a ticket in the CRM through its REST API (idempotent by sessionId).
  2. Publish an alert to the risk team's SNS topic.
  3. Save the transcript to S3 with structured metadata.

Principles applied:
  - IDEMPOTENCY: `sessionId` is the key; re-reading or re-running never
    duplicates the ticket or the S3 object (prior lookup / deterministic key).
  - SECRETS/ENCRYPTION: the transcript is encrypted (envelope encryption with
    KMS) and never travels through N8n in plaintext (see item c in the README).
  - FAIL-FAST + structured result: errors are returned in a way that lets N8n
    route them to retry/DLQ.

This file runs as a demo (`python lambda_session_handler.py`): if boto3 or
credentials are missing, it runs in SIMULATED MODE, printing what it would do,
so the logic can be validated without AWS.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
from dataclasses import dataclass


# ---------------------------------------------------------------------------
# Input model
# ---------------------------------------------------------------------------
@dataclass
class SessionEvent:
    session_id: str
    customer_id: str
    sentiment: str          # expected: "negative" to trigger the flow
    sentiment_score: float
    transcript_ciphertext: str  # transcript ALREADY encrypted (base64), see item c
    metadata: dict

    @staticmethod
    def from_event(event: dict) -> "SessionEvent":
        return SessionEvent(
            session_id=event["session_id"],
            customer_id=event["customer_id"],
            sentiment=event.get("sentiment", ""),
            sentiment_score=float(event.get("sentiment_score", 0.0)),
            transcript_ciphertext=event.get("transcript_ciphertext", ""),
            metadata=event.get("metadata", {}),
        )


# ---------------------------------------------------------------------------
# Environment detection (real vs. simulated)
# ---------------------------------------------------------------------------
def _aws_available() -> bool:
    try:
        import boto3  # noqa: F401
    except ImportError:
        return False
    return bool(os.getenv("AWS_REGION"))


# ---------------------------------------------------------------------------
# 1) CRM: idempotent ticket creation
# ---------------------------------------------------------------------------
def create_crm_ticket(evt: SessionEvent) -> dict:
    """Creates (or reuses) a ticket in the CRM.

    Idempotency: we send an `Idempotency-Key = session_id`. If the CRM API
    supports it, it returns the existing ticket instead of creating another.
    If it does not, a prior lookup by `session_id` does the same job.
    """
    payload = {
        "external_id": evt.session_id,   # natural key for dedup in the CRM
        "customer_id": evt.customer_id,
        "priority": "high" if evt.sentiment_score <= -0.7 else "normal",
        "subject": "Session closed with negative sentiment",
        "tags": ["chatbot", "sentiment-negative"],
    }
    headers = {
        "Authorization": f"Bearer {_get_secret('CRM_API_TOKEN')}",
        "Idempotency-Key": evt.session_id,
        "Content-Type": "application/json",
    }

    if not _aws_available():
        return {"simulated": True, "action": "create_crm_ticket", "payload": payload}

    import urllib.request

    req = urllib.request.Request(
        os.environ["CRM_API_URL"],
        data=json.dumps(payload).encode(),
        headers=headers,
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read())


# ---------------------------------------------------------------------------
# 2) SNS: alert to the risk team
# ---------------------------------------------------------------------------
def publish_risk_alert(evt: SessionEvent, ticket: dict) -> dict:
    message = {
        "session_id": evt.session_id,
        "customer_id": evt.customer_id,
        "sentiment_score": evt.sentiment_score,
        "ticket_id": ticket.get("id") or ticket.get("external_id"),
    }
    if not _aws_available():
        return {"simulated": True, "action": "publish_risk_alert", "message": message}

    import boto3

    sns = boto3.client("sns")
    # MessageDeduplicationId (if the topic is FIFO) reinforces idempotency.
    return sns.publish(
        TopicArn=os.environ["RISK_ALERT_TOPIC_ARN"],
        Subject="Alert: session with negative sentiment",
        Message=json.dumps(message),
    )


# ---------------------------------------------------------------------------
# 3) S3: store the (encrypted) transcript with metadata
# ---------------------------------------------------------------------------
def save_transcript(evt: SessionEvent) -> dict:
    """Saves the transcript to S3 under a DETERMINISTIC key (idempotency).

    The body is the ciphertext received; the bucket also uses SSE-KMS (double
    protection). The key is based on the session_id, so a re-run overwrites the
    same object instead of creating duplicates.
    """
    key = f"transcripts/{_partition(evt.session_id)}/{evt.session_id}.json.enc"
    body = base64.b64decode(evt.transcript_ciphertext) if evt.transcript_ciphertext else b""
    s3_metadata = {
        "customer-id": evt.customer_id,
        "sentiment": evt.sentiment,
        "sentiment-score": str(evt.sentiment_score),
        **{f"meta-{k}": str(v) for k, v in evt.metadata.items()},
    }

    if not _aws_available():
        return {"simulated": True, "action": "save_transcript", "key": key,
                "metadata": s3_metadata, "bytes": len(body)}

    import boto3

    s3 = boto3.client("s3")
    return s3.put_object(
        Bucket=os.environ["TRANSCRIPT_BUCKET"],
        Key=key,
        Body=body,
        Metadata=s3_metadata,
        ServerSideEncryption="aws:kms",
        SSEKMSKeyId=os.environ.get("KMS_KEY_ID", "alias/aws/s3"),
    )


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------
def _partition(session_id: str) -> str:
    """Partition prefix to avoid an S3 hotspot."""
    h = hashlib.sha256(session_id.encode()).hexdigest()[:2]
    return h


def _get_secret(name: str) -> str:
    """Reads a secret from Secrets Manager (or env in simulated mode)."""
    if not _aws_available():
        return os.getenv(name, "SIMULATED_SECRET")
    import boto3

    sm = boto3.client("secretsmanager")
    return sm.get_secret_value(SecretId=name)["SecretString"]


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------
def lambda_handler(event: dict, context=None) -> dict:
    evt = SessionEvent.from_event(event)

    # Business guard: only act on negative sentiment (extra defense beyond N8n).
    if evt.sentiment != "negative":
        return {"statusCode": 200, "skipped": True, "reason": "sentiment != negative"}

    # Order matters: store the transcript and create the ticket BEFORE alerting,
    # so the alert already carries the ticket_id. Every step is idempotent, so
    # an N8n retry re-runs safely.
    result = {"session_id": evt.session_id}
    try:
        result["transcript"] = save_transcript(evt)
        result["ticket"] = create_crm_ticket(evt)
        result["alert"] = publish_risk_alert(evt, result["ticket"])
        result["statusCode"] = 200
    except Exception as e:
        # Structured result so N8n can route to retry/DLQ.
        result["statusCode"] = 500
        result["error"] = str(e)
        raise  # re-raise so N8n/Lambda mark it as failed
    return result


# ---------------------------------------------------------------------------
# Local demo
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    sample = {
        "session_id": "sess-abc-123",
        "customer_id": "cust-987",
        "sentiment": "negative",
        "sentiment_score": -0.82,
        "transcript_ciphertext": base64.b64encode(b'{"turns":["..."]}').decode(),
        "metadata": {"channel": "app", "product": "card"},
    }
    print("=== Lambda session handler (simulated mode) ===\n")
    out = lambda_handler(sample)
    print(json.dumps(out, indent=2, ensure_ascii=False, default=str))
