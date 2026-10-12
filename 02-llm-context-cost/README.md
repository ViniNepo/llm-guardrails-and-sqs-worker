# Exercise 2: LLM cost per session (FinOps) and context optimization

> **Assumptions (documented where the brief was ambiguous):**
> - "Cost per session" is dominated by the **input tokens** sent to Bedrock (the
>   payload grew from ~800 to ~3,200 tokens, +40% cost). So the focus is on
>   **cutting input tokens** without losing quality.
> - Most of the "80 business rules" are **irrelevant** to most questions; only
>   a subset applies to each intent.
> - There is an **official knowledge base** (the source of truth) that can be
>   indexed for RAG.

Files in this folder:

- [`context_optimizer.py`](./context_optimizer.py): runnable demo of the
  strategy (RAG + sliding window + summarization + token budget). Run locally,
  it measures a **~69% token reduction** in the simulated scenario.
- [`system_prompt_optimized.txt`](./system_prompt_optimized.txt): a system
  prompt under 300 tokens for the charge dispute context (item **b**).

**How to run** (Python 3.10+, nothing to install: it only uses the standard
library):

```bash
python3 context_optimizer.py
```

> For real AWS mode (calling Bedrock), install `boto3` with
> `pip install -r requirements.txt` and set `AWS_REGION` + credentials.
> Validated: it runs without errors and measures a ~69% token reduction in the
> simulated scenario.

---

## Diagnosis: where the cost comes from

The current payload stacks three sources of waste:

| Component | Tokens (approx.) | Problem |
|-----------|------------------|---------|
| System prompt with 80 rules | ~1,200 | Sent **in full** on every call, even when only 1-2 rules matter |
| Full history (up to 40 turns) | ~1,700 | Grows linearly with the session; "hi / hello" adds nothing |
| Current message | ~50 | (fine) |
| **Total** | **~3,200** | |

The cost per session is even worse than it looks: in an N-turn conversation, if
we resend the history on every turn, the input token cost grows
**quadratically** (O(N²)) over the session.

---

## a) Strategy to cut cost by ≥ 50% without hurting quality

The core idea: **stop sending everything every time** and send **only what is
relevant to the current turn**. I combine four techniques, each targeting one
source of waste, ordered by cost/benefit.

### 1. RAG for the business rules (largest single gain)

Instead of pasting the 80 rules into the system prompt, I index them in a
**vector store** (OpenSearch Serverless, Kendra or pgvector) and, for each
message, retrieve only the 1-3 relevant chunks by semantic similarity.

- **Gain:** ~1,200 to ~150-300 tokens in the rules block.
- **Quality bonus:** less noise in the context means **fewer hallucinations**
  (contradictory or irrelevant rules competing for the model's attention). This
  directly addresses problem #1 in the brief.
- **Trade-offs:** adds latency (the search) and an infrastructure dependency;
  quality now depends on **retrieval quality** (chunking, embeddings, top-k). Bad
  retrieval can leave out the right rule, so mitigate with recall evaluation of
  the retriever and a conservative fallback ("I don't know, let me transfer
  you").

### 2. Sliding window over the history

Keep only the **last N complete turns** (e.g. N=4). The distant past is rarely
needed to answer "what is the dispute deadline?".

- **Gain:** removes most of the ~1,700 history tokens and **eliminates the
  quadratic growth** (cost per turn becomes roughly constant).
- **Trade-offs:** long-range context is lost. If the customer mentioned
  something in turn 2 and comes back to it in turn 30, the model no longer sees
  it. Mitigated by technique 3.

### 3. Rolling summary

Before dropping the older turns, condense them into a **short summary**
(1-2 sentences with the facts that matter: identity, product, decisions). The
summary is written by a **smaller, cheaper model** (e.g. Claude Haiku), not by
the main model.

- **Gain:** keeps long-range memory at a fraction of the tokens.
- **Trade-offs:** the extra summarization call costs a little (amortized: it
  runs once every K turns, not on every turn), and the summary can drop a
  detail. Mitigate by summarizing incrementally and keeping structured data
  (e.g. `sessionId`, product) outside the free-text summary.

### 4. Token budget + lean system prompt

A hard ceiling (e.g. 1,500 context tokens) that automatically shrinks the
window when exceeded. And rewrite the system prompt to be **short and
imperative** (item b).

- **Gain:** predictable cost and protection against regressions.
- **Trade-offs:** cutting too aggressively hurts quality, which is why the
  ceiling is calibrated through evaluation (item c), not guessed.

### Expected result

| | System | History + context | Total | vs. baseline |
|---|---|---|---|---|
| Current | ~1,200 | ~1,950 | **~3,200** | — |
| Optimized | ~250 | ~500-700 | **~800-950** | **−70% to −75%** |

That comfortably beats the 50% target. Running
[`context_optimizer.py`](./context_optimizer.py) confirms the order of
magnitude (**~69%** in the simulated scenario). Beyond cost, we expect **fewer
hallucinations** and **lower latency** (smaller payloads).

> **Architecture note:** Bedrock **prompt caching** is also worth evaluating for
> the stable part of the prompt (system prompt + recurring rules). It lowers the
> cost of repeated tokens without removing them, and it complements the
> techniques above.

---

## b) Optimized system prompt (< 300 tokens)

The full prompt is in
[`system_prompt_optimized.txt`](./system_prompt_optimized.txt) (~220 tokens
estimated). The chatbot serves Brazilian customers, so the prompt tells the
model to reply in Brazilian Portuguese. Reproduced here:

```text
You are Company X's financial assistant. Answer ONLY from the CONTEXT provided
(policy and account data). If the information is not in the CONTEXT, say you
cannot confirm it and offer to transfer the customer to a human agent.
Never make up deadlines, amounts or regulations.

Scope: charge disputes. Be brief and polite, and reply in Brazilian Portuguese (pt-BR).

Before answering, check silently:
1) Is the answer supported by the CONTEXT? 2) Does it cite a deadline or amount?
Then does it appear in the CONTEXT? If (1) or (2) fails, do not state it; transfer.

Example:
User: "Can I dispute a charge from 6 months ago?"
CONTEXT: "Dispute deadline: 60 days from the charge date."
Answer: "You can dispute a charge within 60 days of the charge date, so a charge
from about 6 months ago is past the deadline. I can transfer you to an agent to
review possible exceptions."
```

### Why these choices

- **Explicit grounding ("ONLY from the CONTEXT"):** the first line of defense
  against hallucination. The rules no longer live in the prompt (they moved to
  RAG), so the model is told to anchor on the injected context.
- **Safe fallback ("cannot confirm, transfer"):** in fintech, "I don't know,
  let me transfer you" is far better than a made-up number. It lowers
  regulatory risk.
- **Short, silent chain of thought (steps 1-2):** a lean check focused on *fact
  verification* (is the answer in the context? does the cited deadline exist?)
  improves faithfulness without inflating the prompt. I asked for a "silent"
  check so the reasoning is not shown to the customer.
- **One highly relevant few-shot example:** shows the desired behavior (cite
  the deadline from the context, apply it to the case, offer a transfer) at low
  token cost. One well-chosen example beats several generic ones.
- **Narrow scope ("charge disputes"):** prompts specialized per intent give more
  quality per token than a do-everything prompt.

---

## c) Measuring whether the reduction hurt quality

Cutting context is a **hypothesis**; it needs before/after measurement and
continuous monitoring. I split this into offline metrics (pre-deploy) and
online metrics (production), and roll out through an **A/B test** for causal
attribution.

### Quality metrics (offline / evaluation)

- **Faithfulness / groundedness:** % of answers fully supported by the context
  (scored by an LLM-as-judge plus human sampling). This is the key metric: if it
  drops, we cut too much context.
- **Answer relevance / correctness:** against a **golden set** of questions with
  known correct answers (including cases like "dispute deadline").
- **Hallucination rate:** how often deadlines, amounts or regulations are made
  up (cross-checked with the guardrail from Exercise 3).
- **RAG recall@k:** did the retriever bring back the right chunk? When quality
  drops, the cause is usually poor recall, not the window.

### Online metrics (CloudWatch / Datadog)

- **Cost / tokens per session** (`InputTokens`, `OutputTokens` per session): the
  primary FinOps KPI, used to confirm the ≥ 50% reduction.
- **Latency (p50/p95/p99)** of the Bedrock call: should drop with smaller
  payloads.
- **Escalation to a human / handoff rate:** a quality proxy. If the bot starts
  "not knowing" much more often, we cut too much context.
- **CSAT / thumbs up-down** per session: perceived satisfaction.
- **Repeat contact rate** on the same topic within 24-48h: a sign of an
  unsatisfying answer.
- **Guardrail trigger rate** (Ex. 3): if it rises, quality regressed.

### Putting it into practice

- Emit custom metrics to **CloudWatch** (namespaces `Chatbot/FinOps` and
  `Chatbot/Quality`) with dimensions per `intent` and per `variant` (A/B).
- **Dashboards** in CloudWatch/Datadog showing cost and quality side by side;
  the decision is never about cost alone.
- **Regression alarms:** e.g. faithfulness below X% or handoff rate above
  baseline + Y% triggers an automatic rollback of the variant.
- **Rollout guardrails:** promote the optimization only if cost goes down **and**
  quality stays within the margin. Never trade quality for cost blindly.

---

### AI usage

- **Tool:** Claude (Anthropic's AI assistant).
- **Where AI helped (a significant part):** it drafted `context_optimizer.py`
  (the structure of the four techniques: RAG, sliding window, summarization and
  token budget), a first version of the system prompt and the initial outline
  of the metrics section.
- **What I did and validated myself:**
  - **Ran** `context_optimizer.py` (Python 3.10) and **measured** the actual
    token reduction (~69% in the simulated scenario) instead of just claiming a
    number.
  - **Rewrote the system prompt** to fit under 300 tokens without losing the
    grounding and the safe fallback, counting tokens with the same heuristic as
    the code.
  - **Added business KPIs to the metrics** (handoff rate, repeat contact) that
    the AI had not suggested; from my experience, these are the ones that reveal
    silent quality degradation.
- **Honest summary:** AI helped structure and write faster; validation by
  running the code, prompt calibration and the FinOps/observability decisions
  were checked and owned by me.
