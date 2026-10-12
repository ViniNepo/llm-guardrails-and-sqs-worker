# LLM guardrails, context optimization and a resilient SQS worker

Four hands-on exercises around a production **fintech chatbot built on
generative AI** (AWS Bedrock / Claude), written as a take-home technical
assessment in **Go and Python**:

- a concurrent **SQS worker in Go** that no longer loses messages under load;
- a **context optimizer** that cuts LLM input tokens by ~69% per session;
- an **output guardrail** that blocks answers with made-up deadlines or amounts;
- an **N8n + Lambda** integration design with retries, DLQ, idempotency and
  envelope encryption.

Every exercise has its own folder with the technical reasoning in `README.md`
and **working code** in separate files. The code builds and runs locally (the
Go worker against a local SQS; the Python scripts with the standard library
only).

```
.
├── README.md                        ← you are here (index + general assumptions)
├── 01-sqs-worker-go/                ← diagnosing and fixing the SQS worker (Go)
│   ├── README.md
│   ├── issues_and_follow_up.md      (full diagnosis + follow-up answers)
│   ├── worker_original.go           (buggy code, reference only)
│   ├── worker_fixed.go              (fixed, commented version)
│   ├── RUN_LOCAL.md / Makefile      (run it against ElasticMQ)
│   └── local/                       (docker-compose, queue config, producer)
├── 02-llm-context-cost/             ← FinOps and context optimization (Python)
│   ├── README.md
│   ├── context_optimizer.py         (RAG + sliding window + summary + token budget)
│   └── system_prompt_optimized.txt  (prompt < 300 tokens)
├── 03-hallucination-guardrail/      ← hallucination diagnosis + guardrail (Python)
│   ├── README.md
│   └── guardrail_lambda.py          (detection/blocking guardrail)
└── 04-n8n-lambda-integration/       ← N8n + Lambda integration design
    ├── README.md
    ├── n8n_workflow.json            (workflow sketch)
    └── lambda_session_handler.py    (business logic)
```

---

## Exercise index

| # | Exercise | Focus | Folder |
|---|----------|-------|--------|
| 1 | Diagnosing and fixing the SQS worker | Concurrency, messaging reliability, Go | [`01-sqs-worker-go`](./01-sqs-worker-go/README.md) |
| 2 | FinOps and context optimization | Cost per session, prompt engineering, observability | [`02-llm-context-cost`](./02-llm-context-cost/README.md) |
| 3 | Hallucination diagnosis and prompt evolution | LLMOps, A/B testing, guardrails | [`03-hallucination-guardrail`](./03-hallucination-guardrail/README.md) |
| 4 | N8n + Lambda integration design | Integration architecture, resilience, security | [`04-n8n-lambda-integration`](./04-n8n-lambda-integration/README.md) |

## Quick start

```bash
# Exercise 1: Go worker against a local SQS (needs Go 1.22+ and Docker or Java)
cd 01-sqs-worker-go && make up && make demo

# Exercises 2-4: Python 3.10+, no dependencies needed
python3 02-llm-context-cost/context_optimizer.py
python3 03-hallucination-guardrail/guardrail_lambda.py
python3 04-n8n-lambda-integration/lambda_session_handler.py
```

---

## Scenario (summary)

We are the engineering team of a **fintech** running a **customer support
chatbot built on generative AI** (AWS Bedrock / Claude), handling thousands of
sessions a day about bills, charge disputes and financial products. Three
critical problems are open:

1. **Quality degradation / hallucination** on refund policy questions
   (Ex. 3).
2. **Rising cost per session** (+40% in a month) caused by uncontrolled growth
   of the context sent to the model (Ex. 2).
3. **SQS queue piling up messages**: a worker with growing lag at peaks
   (Ex. 1).

Ex. 4 is an extension: a new automated flow triggered when a session ends with
negative sentiment.

The chatbot serves Brazilian customers, so customer-facing text (chatbot
answers, test cases) stays in Portuguese; everything else is in English.

---

## General assumptions (handling ambiguity)

The brief explicitly asks to document interpretations wherever something is
ambiguous. Exercise-specific assumptions are in each folder's `README.md`; the
cross-cutting ones are:

- **AWS region/services:** a typical AWS stack (SQS, Bedrock, Lambda, SNS, S3,
  CloudWatch) in a single region, with least-privilege IAM.
- **Model:** "Claude via Bedrock". Where I mention prices or tokens, I use
  orders of magnitude to reason about trade-offs, not exact contract values.
- **Scale:** "thousands of sessions a day" means tens of messages per second at
  peak. Enough to justify concurrency and batching, not enough to need exotic
  architecture.
- **Worker runtime:** ECS Fargate with multiple tasks (Ex. 1 mentions this
  scenario explicitly).

---

## AI usage statement

As required by the brief, **each exercise has its own `AI usage` section**
describing the tool, what it was used for and what I validated or changed by
hand. Consolidated summary:

- **Tool:** Claude (Anthropic's AI assistant).
- **Where AI helped (a significant part):** repository structure, the first
  draft of all the code (Go and Python), the first write-up of the READMEs and
  the organization of the diagnoses and tables.
- **What I did and validated myself:** built and **ran** the code (Go:
  `go build`/`go vet`/`gofmt` + an end-to-end test against a local SQS; Python:
  running all three scripts), **fixed bugs the AI introduced**, checked every
  architecture decision against the AWS documentation and tuned the trade-offs
  to a fintech context.
- **Honest summary:** AI sped up writing and boilerplate; correctness, test
  runs and technical decisions were checked and owned by me. Per-exercise
  details are in each `AI usage` section.
- **Translation:** the repository was originally written in Portuguese and
  later translated to English with AI help.
