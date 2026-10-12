# Running the SQS worker locally

This guide starts a **local SQS** (ElasticMQ), sends test messages and runs the
worker, with no AWS account needed. The whole flow was tested end to end.

## Prerequisites

- **Go 1.22+** (`go version`)
- For the local queue, pick one: **Docker** (option A) or **Java 11+** (option B)

## Supporting files

```
01-sqs-worker-go/
├── worker_fixed.go            # the worker (supports AWS_ENDPOINT_URL for local mode)
├── Makefile                   # shortcuts: up / seed / run / demo / down
├── .env.example               # sample environment variables
└── local/
    ├── elasticmq.conf         # "session-closed" queue + "session-closed-dlq" DLQ
    ├── docker-compose.yml     # local SQS with Docker
    └── producer/producer.go   # sends test messages
```

---

## Quick path (with the Makefile)

```bash
cd 01-sqs-worker-go
make deps          # go mod tidy

# 1) Start the local queue (pick one):
make up            # with Docker (recommended)
#   or, in another terminal, without Docker:
make up-jar        # with Java (downloads ElasticMQ and runs it in the foreground)

# 2) In another terminal: send messages and run the worker
make seed          # sends 25 test messages (change with N_MESSAGES=100)
make run           # runs the worker; Ctrl+C triggers a graceful shutdown

# 3) When you are done:
make down          # stops the queue (Docker)
```

`make demo` runs `seed` + `run` in one go.

---

## Manual steps (without the Makefile)

### Option A: local queue with Docker

```bash
cd 01-sqs-worker-go
docker compose -f local/docker-compose.yml up -d
# SQS at http://localhost:9324  (stats UI: http://localhost:9325)
```

### Option B: local queue with Java (no Docker)

```bash
cd 01-sqs-worker-go
curl -sSL -o /tmp/elasticmq.jar \
  https://s3-eu-west-1.amazonaws.com/softwaremill-public/elasticmq-server-1.6.11.jar
java -Dconfig.file=local/elasticmq.conf -jar /tmp/elasticmq.jar
# leave it running in this terminal
```

### Set up the environment and run (new terminal)

```bash
cd 01-sqs-worker-go
cp .env.example .env
set -a; source .env; set +a      # load the variables

go run ./local/producer          # sends 25 messages (change with N_MESSAGES)
go run .                         # runs the worker
```

You will see JSON logs like:

```json
{"level":"INFO","msg":"using custom SQS endpoint (local mode)","endpoint":"http://localhost:9324"}
{"level":"INFO","msg":"session processed","session_id":"sess-0007","customer_id":"cust-007","sentiment":"positive","closed_at":"2026-07-09T12:43:26Z"}
```

The worker expects a JSON event (`session_id`, `customer_id`, `closed_at`,
`sentiment`), the same format the `producer` sends. A body that is not this JSON
(for example a plain `"hello world"`) is treated as malformed: it returns an
error, it is not deleted, and after `maxReceiveCount` redeliveries it goes to the
DLQ.

Press **Ctrl+C**: the worker stops polling, drains the messages in flight and
exits with `"worker stopped cleanly"`.

---

## Testing by hand and watching the messages

The easiest way to see the messages is to keep the **worker running in one
terminal** (the logs show up there) and **send messages from another terminal**.

Suggested setup with 3 terminals (all inside `01-sqs-worker-go`):

1. **Terminal 1, local queue:** `make up` (Docker, frees the terminal) or
   `make up-jar` (Java, keeps the terminal busy).
2. **Terminal 2, worker:** `make run`. **This is where you see the messages**
   being processed (log `"msg":"session processed"`). Keep it open.
3. **Terminal 3, send messages:** `make seed` (25 events) or the `curl` commands
   below. Right after sending, look at Terminal 2.

### Sending a specific message by hand (curl)

**Valid** JSON event (shows up as `session processed` in the worker):

```bash
curl "http://localhost:9324/000000000000/session-closed?Action=SendMessage" \
  --data-urlencode 'MessageBody={"session_id":"sess-test","customer_id":"cust-1","closed_at":"2026-07-09T12:00:00Z","sentiment":"negative"}'
```

**Invalid** body (shows the error path: redelivery, then DLQ):

```bash
curl "http://localhost:9324/000000000000/session-closed?Action=SendMessage" \
  --data-urlencode 'MessageBody=hello world'
```

### Viewing the queues in the browser

Open **http://localhost:9325** (ElasticMQ stats UI). It shows how many messages
are in `session-closed` and in `session-closed-dlq`.

### Inspecting the queue from the command line

```bash
# Approximate number of messages in the main queue
curl -s "http://localhost:9324/000000000000/session-closed?Action=GetQueueAttributes&AttributeName.1=ApproximateNumberOfMessages"

# Number of messages in the DLQ
curl -s "http://localhost:9324/000000000000/session-closed-dlq?Action=GetQueueAttributes&AttributeName.1=ApproximateNumberOfMessages"
```

---

## What this setup demonstrates

- **Long polling / batching**: tune `WAIT_TIME_SECONDS` and `MAX_MESSAGES` in `.env`.
- **Concurrency**: `NUM_WORKERS` controls the goroutine pool.
- **Delete only on success + DLQ**: the demo `handleMessage` always succeeds;
  if you make it return an error, the message is redelivered and, after
  `maxReceiveCount=5` (set in `elasticmq.conf`), it moves to
  `session-closed-dlq`.
- **Graceful shutdown**: Ctrl+C goes through the same path as SIGTERM.

### Seeing the DLQ in action (optional)

Edit `handleMessage` in `worker_fixed.go` to return an error
(`return fmt.Errorf("simulated failure")`), run `make seed && make run` and
watch the messages land in `session-closed-dlq` after a few redeliveries
(visible in the UI at http://localhost:9325).

---

## Running against real AWS (production)

Just **don't** set `AWS_ENDPOINT_URL` and use the real `QUEUE_URL`:

```bash
unset AWS_ENDPOINT_URL
export QUEUE_URL="https://sqs.us-east-1.amazonaws.com/<account>/<queue>"
export AWS_REGION="us-east-1"
export WAIT_TIME_SECONDS=20        # long polling is recommended in production
# credentials from an IAM role (ECS) or `aws configure`
go run .
```

The same binary works locally and in production. The only difference is
`AWS_ENDPOINT_URL` (empty means real AWS).

---

## Validation already done

This flow was run and checked: **25 messages sent, 25 processed, queue empty
(`ApproximateNumberOfMessages = 0`), graceful shutdown completed**.
`go build`, `go vet` and `gofmt` all pass with no errors.
