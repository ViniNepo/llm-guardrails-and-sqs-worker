# Exercise 1: Diagnosing and fixing an SQS worker (Go)

> **Assumptions (documented where the brief was ambiguous):**
> - The worker consumes an SQS **Standard** queue (*at-least-once* delivery, no
>   ordering guarantee). A FIFO queue would probably have come with ordering or
>   deduplication requirements, so I assume Standard, the usual choice for
>   "session closed" events.
> - A **DLQ with a redrive policy** is (or should be) attached to the queue.
> - The worker runs on **ECS Fargate** (the brief mentions this scenario in the
>   third follow-up question).
> - `handleMessage` does real I/O (database, enrichment, logging), so it can
>   fail and it can be slow.

Files in this folder:

- **[`issues_and_follow_up.md`](./issues_and_follow_up.md): full diagnosis of
  the original code and answers to the follow-up questions.**
- [`worker_original.go`](./worker_original.go): the code from the brief, kept for reference only (excluded from the build with `//go:build ignore`).
- [`worker_fixed.go`](./worker_fixed.go): the fixed version, commented and ready to run.
- [`go.mod`](./go.mod) / [`go.sum`](./go.sum): dependencies (AWS SDK for Go v2).
- **[`RUN_LOCAL.md`](./RUN_LOCAL.md): how to run the worker locally** (local SQS
  with ElasticMQ), step by step. Tested end to end.
- [`Makefile`](./Makefile): shortcuts (`make up`, `make seed`, `make run`, `make demo`).
- [`.env.example`](./.env.example): sample environment variables.
- [`local/`](./local): development infrastructure: `elasticmq.conf` (queue +
  DLQ), `docker-compose.yml` and `producer/` (test message generator).

> **▶️ To run it:** follow [`RUN_LOCAL.md`](./RUN_LOCAL.md). In short:
> `make up` (starts the local SQS), `make seed` (sends messages), `make run`
> (runs the worker). Validated flow: **25 messages sent, 25 processed, queue
> empty, graceful shutdown**, with clean `go build`/`go vet`/`gofmt`.

---

## 1. Diagnosis and follow-ups

The **full diagnosis of the 8 issues** in the original code (with the snippet,
impact and fix for each) and the **answers to the 3 follow-up questions** are in
a dedicated document:

➡️ **[`issues_and_follow_up.md`](./issues_and_follow_up.md)**

Summary of the issues found:

| # | Issue | Impact | Fix |
|---|-------|--------|-----|
| 1.1 | Unconditional delete | **Message loss** | Delete only on success |
| 1.2 | Receive error ignored | Busy loop, cost, throttling | Handle error + backoff |
| 1.3 | Short polling | Lag + cost | `WaitTimeSeconds: 20` |
| 1.4 | 1 message per call | Low throughput | `MaxNumberOfMessages: 10` |
| 1.5 | Sequential | Does not scale at peaks | Concurrent worker pool |
| 1.6 | `context.Background()` | Hangs + abrupt shutdown | Timeout + SIGTERM |
| 1.7 | Delete ignored / one by one | Invisible failures + cost | Check error + batch |
| 1.8 | No idempotency | Duplicate data | Dedup by `sessionId` |

---

## 2. Fixed version

The full code is in [`worker_fixed.go`](./worker_fixed.go). Key points of the
design:

- **1 poller + N workers**: the poller long-polls in batches and publishes to a
  channel; the workers consume in parallel.
- **Delete only on success**, with its own context (so it does not fail during
  shutdown).
- **Exponential backoff** on `ReceiveMessage` errors.
- **Per-message timeout** (shorter than VisibilityTimeout) and a **graceful
  shutdown** on SIGTERM.
- **Configuration through environment variables** (12-factor), easy to tune on
  ECS.

Core snippet (consume + conditional delete):

```go
func (w *Worker) consume(id int, in <-chan types.Message) {
    for msg := range in {
        msgCtx, cancel := context.WithTimeout(context.Background(), w.cfg.HandleTimeout)
        err := handleMessage(msgCtx, aws.ToString(msg.Body))
        cancel()

        if err != nil {
            // Do NOT delete: SQS redelivers it and, after maxReceiveCount, moves it to the DLQ.
            slog.Error("failed to process message; it will be redelivered", "err", err)
            continue
        }

        // Success: delete it (with its own context so it survives shutdown).
        delCtx, delCancel := context.WithTimeout(context.Background(), 5*time.Second)
        _, delErr := w.client.DeleteMessageBatch(delCtx, &sqs.DeleteMessageBatchInput{
            QueueUrl: &w.cfg.QueueURL,
            Entries:  []types.DeleteMessageBatchRequestEntry{{Id: msg.MessageId, ReceiptHandle: msg.ReceiptHandle}},
        })
        delCancel()
        if delErr != nil {
            slog.Error("processed message could not be deleted; it may be processed again", "err", delErr)
        }
    }
}
```

> **How I validated it:** built and ran it locally against a local SQS
> (ElasticMQ), see [`RUN_LOCAL.md`](./RUN_LOCAL.md). `go build`, `go vet` and
> `gofmt` pass with no errors, and the end-to-end flow processed every message
> and shut down gracefully.

> The **follow-up questions** (why lag grows at peaks; how to set
> `VisibilityTimeout`/`WaitTimeSeconds`; multiple ECS tasks) are answered in
> [`issues_and_follow_up.md`](./issues_and_follow_up.md#2-follow-up-questions).

---

### AI usage

- **Tool:** Claude (Anthropic's AI assistant).
- **Where AI helped (a significant part):** it generated the first draft of the
  concurrent Go worker (poller + worker pool, backoff, graceful shutdown), the
  first write-up of the bug diagnosis and the skeleton of the local setup
  (ElasticMQ + producer + Makefile).
- **What I did and validated myself:**
  - **Built and actually ran it:** `go build`, `go vet` and `gofmt` with no
    errors, and the end-to-end flow against a local SQS (messages sent,
    processed, queue empty, graceful shutdown). I did not rely on what the AI
    "thought" would work.
  - **Reviewed line by line** the AWS SDK for Go v2 signatures, the imports and
    the removal of the unused `ctx` parameter in `consume`.
  - **Engineering decisions were mine**, checked against the AWS
    documentation: sizing `VisibilityTimeout`, autoscaling on backlog (not CPU)
    and requiring idempotency in `handleMessage` for an I/O-bound worker.
- **Honest summary:** AI sped up the writing (boilerplate and first version);
  correctness, test runs, bug fixes and architecture decisions were validated
  and owned by me.
