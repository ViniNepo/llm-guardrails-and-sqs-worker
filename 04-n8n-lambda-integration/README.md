# Exercise 4: N8n + Lambda integration design

> **Assumptions (documented where the brief was ambiguous):**
> - Negative sentiment is already **detected by the model** and arrives in the
>   "session closed" event (classifying it is not this flow's job).
> - **N8n** is the orchestration layer (trigger, routing, retry, DLQ,
>   visibility); the **business logic** and access to sensitive data live in
>   the **Lambda** (smaller security surface, easier to audit and test).
> - The CRM is exposed through a **REST API**; alerts go through **SNS**; the
>   transcript is stored in **S3**.

Files in this folder:

- [`lambda_session_handler.py`](./lambda_session_handler.py): business logic
  (CRM + SNS + S3), idempotent and runnable in simulated mode.
- [`n8n_workflow.json`](./n8n_workflow.json): sketch of the N8n workflow
  (trigger, sentiment filter, Lambda, DLQ on error). Valid JSON.

**How to run** (Python 3.10+, nothing to install: it runs in simulated mode
with the standard library):

```bash
python3 lambda_session_handler.py
```

> To actually run against AWS (S3/SNS/Secrets Manager), install `boto3` with
> `pip install -r requirements.txt` and set `AWS_REGION` + credentials.
> Validated: the Lambda runs in simulated mode and `n8n_workflow.json` is valid
> JSON.

---

## a) Flow architecture: where each responsibility lives

The design principle: **N8n orchestrates, Lambda executes**. N8n is great for
visual routing, triggers and retry policy, but putting CRM credentials, the KMS
key and S3 access inside it widens the attack surface and makes unit testing
harder. So the sensitive logic is concentrated in **one** Lambda.

```
┌──────────────────────────────────────────────────────────────────┐
│                           N8N (orchestrator)                       │
│                                                                    │
│  [Trigger: session closed]                                         │
│   (SQS/EventBridge webhook; payload carries the ENCRYPTED          │
│    transcript)                                                     │
│            │                                                        │
│            ▼                                                        │
│  [IF: sentiment == "negative"?] ──no──► [end at no cost]           │
│            │ yes                                                    │
│            ▼                                                        │
│  [Invoke Lambda: session-negative-handler]  (sync, retry 3x)       │
│            │                         │                              │
│         success                  error (after retries)             │
│            │                         ▼                              │
│         [end]                 [send to SQS DLQ]                     │
└──────────────────────────────────────────────────────────────────┘
                             │  (invokes)
                             ▼
┌──────────────────────────────────────────────────────────────────┐
│                     LAMBDA (business logic)                        │
│                                                                    │
│  1) save_transcript()  ─► S3 (SSE-KMS) + structured metadata       │
│  2) create_crm_ticket()─► CRM REST (Idempotency-Key = session_id)  │
│  3) publish_risk_alert()─► SNS (risk team topic)                   │
│                                                                    │
│  Credentials via Secrets Manager · KMS decrypt · least-priv. IAM   │
└──────────────────────────────────────────────────────────────────┘
```

### Split of responsibilities

| Layer | Responsibility | Why |
|-------|----------------|-----|
| **N8n** | Trigger, `sentiment==negative` filter, invoking the Lambda, retry policy, error route to the DLQ, flow visibility/observability | It is the glue/orchestration; it changes often and benefits from the visual editor |
| **Lambda** | The 3 side effects (S3, CRM, SNS), idempotency, KMS/secrets, business validation | Versioned, testable code; it concentrates access to sensitive data |
| **Managed AWS** | S3 (SSE-KMS), SNS, Secrets Manager, KMS, SQS DLQ, CloudWatch | Durability, scale and auditing without code |

> **Why one Lambda and not three?** We could have one N8n node per side effect,
> but that would spread credentials and idempotency logic across N8n and create
> **partial states** that are hard to reconcile (ticket created, transcript not
> saved). A single Lambda wraps the logical transaction, is testable and keeps
> secrets out of N8n. If one of the effects needed to scale on its own, then
> splitting it into Step Functions would make sense.

**Lambda pseudocode** (real implementation in `lambda_session_handler.py`):

```python
def lambda_handler(event, ctx):
    evt = SessionEvent.from_event(event)
    if evt.sentiment != "negative":
        return {"skipped": True}          # business guard (extra defense)

    save_transcript(evt)                  # 1) S3 + metadata (deterministic key)
    ticket = create_crm_ticket(evt)       # 2) CRM (Idempotency-Key = session_id)
    publish_risk_alert(evt, ticket)       # 3) SNS (with ticket_id)
    return {"statusCode": 200}
```

---

## b) Failure points and how to stay resilient

Walking through the flow, every hop is a potential failure point:

| Failure point | Risk | Mitigation |
|---------------|------|------------|
| Trigger loses the event | Negative session never handled | Trigger on **SQS** (persistent) instead of a fire-and-forget webhook; SQS already provides durability |
| CRM down / timeout | Ticket not created | **Retry with exponential backoff** (3x in N8n) + idempotency so nothing is duplicated |
| SNS fails | Risk team not alerted | Retry; SNS is highly available; as a last resort the event goes to the DLQ |
| S3 fails | Transcript lost | Retry; once written, S3 has eleven nines of durability |
| Lambda fails halfway (ticket ok, S3 not) | **Partial state** | **Per-step idempotency**, so a retry safely re-runs everything; the order (S3, CRM, SNS) was chosen so the alert already has the ticket_id |
| Failure after all retries | Event lost | **DLQ (SQS)**: N8n routes the error to a dead-letter queue for reprocessing |
| Duplicate event (retry, redelivery) | Duplicate ticket/alert/object | **Idempotency** (see below) |

### The three guarantees requested

**Retries.** In N8n, the Lambda node uses `retryOnFail` (3 attempts,
`waitBetweenTries` with backoff). Inside the Lambda, network calls have short
timeouts so they fail fast and let the retry do its job.

**DLQ.** The **error** route of the Lambda node in N8n publishes the event to an
**SQS DLQ** (`session-negative-dlq`). Nothing is silently dropped; a consumer or
a manual reprocess drains the DLQ. (The Lambda itself can also have a native
`DeadLetterConfig` as a second safety net.)

**Idempotency** (the most important point in a flow with several side effects):
- **CRM:** `Idempotency-Key = session_id` header (and/or a prior lookup by
  `external_id`), so a re-run returns the same ticket instead of creating
  another.
- **S3:** **deterministic** key `transcripts/<hash>/<session_id>.json.enc`, so a
  re-run **overwrites** the same object with no duplicate.
- **SNS:** if the topic is FIFO, use `MessageDeduplicationId = session_id`;
  otherwise the alert consumer deduplicates by `session_id`.

With this, retrying the whole flow is **safe**: at worst it re-runs the three
side effects without creating duplicates.

---

## c) The transcript must not travel through N8n in plaintext

The security requirement is that N8n **never sees the transcript in the
clear**. The solution is to **encrypt at the source and decrypt only where
strictly needed**, so N8n becomes a "blind courier".

### Approach: envelope encryption with KMS, N8n only forwards ciphertext

1. **Encrypt at the source** (in the service that closes the session, before
   emitting the event): generate or use a **KMS** data key and encrypt the
   transcript. The event carries only the **ciphertext** (base64), which is
   what the Lambda receives in the `transcript_ciphertext` field.
2. **N8n only forwards it.** The ciphertext passes through N8n, is logged (if at
   all) as unreadable bytes, and is handed to the Lambda. N8n **has no
   `kms:Decrypt` permission**, so even if compromised it cannot open the
   content.
3. **Decrypt only where needed.** If a step needs the plaintext, it is the
   **Lambda** (with a narrowly scoped `kms:Decrypt` IAM permission) that
   decrypts it, in memory, for as short a time as possible. In the current flow
   even that is unnecessary: the transcript goes **encrypted** straight to S3,
   with **SSE-KMS** on top (double protection, defense in depth).

### Reinforcements

- **No decrypt permission for N8n** (least-privilege IAM): the N8n role can only
  invoke the Lambda and publish to the DLQ; no KMS, S3 or Secrets Manager.
- **Secrets outside N8n:** the CRM token and the KMS key live in **Secrets
  Manager**, read by the Lambda at runtime, never in N8n variables.
- **TLS on every hop** (already standard for AWS/HTTPS APIs) protects data in
  transit; application-level encryption protects it **from N8n itself**.
- **Sanitized logs:** make sure N8n does not log the payload at DEBUG level;
  since the field is already ciphertext, even a log leak does not expose the
  content.

> **Alternative considered:** keep the transcript **out of N8n entirely**: the
> source writes it straight to S3 and the event carries only a **pointer**
> (`s3://...`). The Lambda reads it through the pointer. It is even safer (the
> transcript never touches N8n), at the cost of an extra write at the source.
> With a strict security policy, that is the preferable option; I kept envelope
> encryption as the main design because it is what the brief describes most
> directly ("must not travel through N8n in plaintext").

---

### AI usage

- **Tool:** Claude (Anthropic's AI assistant).
- **Where AI helped (a significant part):** it drafted
  `lambda_session_handler.py`, the `n8n_workflow.json` sketch and the first
  version of the failure-point and resilience tables.
- **What I did and validated myself:**
  - **Ran** the Lambda in simulated mode (Python 3.10) and **validated the
    workflow JSON** (`json.load`); both run without errors.
  - **My design decisions:** the **order of the side effects** (S3, CRM, SNS) so
    the alert already carries the `ticket_id`, and **per-step idempotency**
    (Idempotency-Key in the CRM, deterministic S3 key, SNS dedup).
  - **Refined the architecture:** "one Lambda instead of three N8n nodes" and
    the criteria for when I would move to Step Functions.
- **Honest summary:** AI sped up the code and diagram drafts; validation by
  running the code and the architecture, resilience and security decisions were
  checked and owned by me.
