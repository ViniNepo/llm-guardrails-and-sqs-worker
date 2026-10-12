# Exercise 1: Issues found and follow-up questions

This document holds the **full diagnosis of the issues** in the original worker
and the **answers to the follow-up questions** from the brief. The fixed code
and how to run it are in [`README.md`](./README.md) and
[`RUN_LOCAL.md`](./RUN_LOCAL.md).

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

---

## 1. Diagnosis: every issue in the original code

Ordered from the most severe (correctness, data loss) to the most operational
(cost, scale).

### 🔴 1.1 Silent message loss (the most severe bug)

```go
err := handleMessage(*msg.Body)
if err != nil {
    fmt.Println("error:", err)
}
client.DeleteMessage(...) // ← ALWAYS runs, even after the error above
```

`DeleteMessage` runs **unconditionally**, even when `handleMessage` returns an
error. If the database goes down or enrichment fails, the message is **deleted
from the queue without being processed**. The session event is lost for good:
it never reaches the DLQ and is never retried. For the business (session
logging and analytics), that is loss of auditable data.

**Fix:** delete the message **only after success**. On error, leave it alone:
SQS redelivers it after the `VisibilityTimeout` and, after `maxReceiveCount`
attempts, moves it to the DLQ automatically.

### 🔴 1.2 `ReceiveMessage` error is ignored

```go
result, _ := client.ReceiveMessage(...) // ← error discarded
```

If the call fails (throttling, network, credentials), `result` comes back
`nil`/empty, `for range result.Messages` does nothing, and the outer `for {}`
**spins in a tight busy loop**, hitting the SQS API thousands of times per
second. That burns CPU, causes more throttling and **raises the SQS request
bill**.

**Fix:** handle the error, log it and apply **exponential backoff** before
trying again.

### 🟠 1.3 Short polling (no `WaitTimeSeconds`): a direct cause of lag and cost

Without `WaitTimeSeconds`, SQS uses **short polling**: each `ReceiveMessage`
returns immediately, often **empty** even when there are messages (short
polling only queries a subset of the servers). The result:

- many empty calls, so a **high request cost**;
- higher delivery latency, so **lag**;
- wasted CPU in the loop.

**Fix:** **long polling** with `WaitTimeSeconds: 20` (the maximum). The call
waits up to 20s for messages, cuts empty calls and delivers as soon as a
message arrives.

### 🟠 1.4 `MaxNumberOfMessages: 1`: low throughput

Fetching **one message per call** means one network round trip per message.
With thousands of sessions, the queue fills faster than the worker drains it.

**Fix:** fetch in **batches of up to 10** (`MaxNumberOfMessages: 10`), the SQS
maximum. Fewer calls, more throughput.

### 🟠 1.5 Fully sequential processing: it does not scale at peaks

The loop handles one message at a time. If `handleMessage` takes, say, 200ms
(a database call), the ceiling is ~5 msg/s **per task**, no matter how much the
queue grows. This is the core reason behind the follow-up question "why does
lag grow at peaks".

**Fix:** a **goroutine pool** (worker pool). One poller fetches batches and
hands them through a channel to N concurrent workers. Parallelism decouples the
consumption rate from the duration of a single I/O call.

### 🟡 1.6 `context.Background()` with no timeout or cancellation

- `handleMessage` can hang forever (slow database) and **hold** the worker past
  the `VisibilityTimeout`, so the message reappears and gets processed twice.
- There is no **SIGTERM** handling. On ECS, a deploy sends SIGTERM; without a
  graceful shutdown, in-flight messages are cut off halfway.

**Fix:** a per-message `context.WithTimeout` (shorter than the
`VisibilityTimeout`) and `signal.NotifyContext` for a graceful shutdown.

### 🟡 1.7 `DeleteMessage` error ignored, and one delete per message

The delete result is discarded (we never know if it failed), and deleting one
by one costs more than `DeleteMessageBatch`.

**Fix:** check the delete error and use `DeleteMessageBatch`.

### 🟡 1.8 No idempotency

SQS Standard is *at-least-once*, so the same message **will** arrive twice at
some point (for example, processing succeeded but the delete failed). If
`handleMessage` is not idempotent, we write duplicate logs or records.

**Fix:** `handleMessage` must deduplicate by a natural key (such as
`sessionId`), using UPSERT instead of INSERT. Documented in the code.

### Summary table

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

## 2. Follow-up questions

### 2.1 Why does lag grow during peaks even with the infinite loop running?

Because an infinite loop **does not guarantee throughput**; it only guarantees
the process is alive. Three bottlenecks add up:

1. **Sequential processing** (1.5): throughput is `1 / handleMessage_time` per
   task. If a handle takes 200ms, that is ~5 msg/s per task, a fixed ceiling.
   During a peak of, say, 50 msg/s, the queue grows by 45 msg/s.
2. **Short polling + 1 message per call** (1.3, 1.4): much of the loop time is
   spent on empty network calls or calls that bring back a single message,
   instead of processing. The loop "runs", but mostly idles.
3. **Busy loop on errors** (1.2): peaks usually bring throttling; since the
   error is ignored, the worker spins without doing useful work, which makes
   the throttling worse.

So `ApproximateNumberOfMessagesVisible` climbs because the **arrival rate is
higher than the effective consumption rate**, and the worker design caps that
consumption rate artificially. The fix tackles this with **concurrency +
batching + long polling**, and horizontal scaling (more tasks) then actually
helps.

### 2.2 How should `VisibilityTimeout` and `WaitTimeSeconds` be set?

**`WaitTimeSeconds` = 20** (the maximum). It enables long polling: fewer empty
calls, lower cost and lower delivery latency. There is no reason to use less
for a dedicated worker like this one.

**`VisibilityTimeout`** must be **greater than the p99 of `handleMessage`, with
headroom**, so a message being processed does not reappear and get picked up
in parallel by another task. Rule of thumb:

```
VisibilityTimeout ≈ p99(handleMessage) × 2   (with a floor, e.g. 30–60s)
```

If the p99 handle time is ~15s, a 60s VisibilityTimeout leaves comfortable
headroom. Watch both extremes:

- **Too short**: messages come back while still being processed, causing
  **duplicate processing** and extra load.
- **Too long**: if a worker crashes, the message takes too long to become
  visible again, so **recovery is slow**.

Another good practice: if handle time varies a lot, use
`ChangeMessageVisibility` to **extend the timeout dynamically** while the work
is still in progress (a heartbeat), instead of hard-coding a high value.

And, crucially, set a **redrive policy** on the queue (e.g.
`maxReceiveCount: 5`) so poison messages go to the **DLQ** instead of cycling
forever.

### 2.3 Worker on ECS with multiple tasks: what can go wrong and how to mitigate it?

Running N tasks against the same queue is the **right** horizontal scaling
model (SQS spreads messages across consumers). It does raise a few concerns:

**a) Duplicate processing / two tasks on the same message.** With a short
`VisibilityTimeout` or slow handles, two tasks can pick up the same message.
→ *Mitigation:* a well-sized `VisibilityTimeout` (2.2) + **idempotency** in
`handleMessage` (dedup by `sessionId`, UPSERT).

**b) Thundering herd on downstream services.** Many tasks × many goroutines can
exhaust database connections or the enrichment service's rate limit.
→ *Mitigation:* cap `NumWorkers` per task, use a properly sized **connection
pool**, and scale on a sensible metric.

**c) Badly tuned autoscaling.** Scaling on CPU is a poor fit for an I/O-bound
worker (CPU stays low even with a full queue). → *Mitigation:* use **Target
Tracking on `ApproximateNumberOfMessagesVisible`** (or "backlog per task" =
messages ÷ tasks) as the Application Auto Scaling trigger. More messages means
more tasks.

**d) Shutdown during deploys or scale-in.** ECS sends **SIGTERM**, then SIGKILL
after `stopTimeout`. Without handling it, in-flight messages are cut off and
reprocessed. → *Mitigation:* the **graceful shutdown** already implemented
(stop polling, drain in-flight work) + a container `stopTimeout` longer than
`HandleTimeout`.

**e) Ordering.** SQS Standard does not guarantee order, and multiple tasks make
that even more visible. → *Mitigation:* if order mattered, move to **SQS FIFO**
with `MessageGroupId` (for session logging, order is usually not a
requirement).
