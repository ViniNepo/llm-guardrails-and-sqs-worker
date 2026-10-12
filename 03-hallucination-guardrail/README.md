# Exercise 3: Hallucination diagnosis and an output guardrail

> **Assumptions (documented where the brief was ambiguous):**
> - The correct answer ("60 days") reflects the company's **official knowledge
>   base**; answer B ("180 days, per the Central Bank") is a hallucination.
> - We have access to **session logs** (prompt, parameters, answer) to
>   investigate and to set up the A/B test.
> - There is a **source of truth** (the knowledge base) the guardrail can check
>   deadlines and amounts against.

File in this folder:

- [`guardrail_lambda.py`](./guardrail_lambda.py): runnable implementation of
  the guardrail (item c). The local tests reproduce the exact scenario from the
  brief: they allow "60 days" and **block "180 days"**.

**How to run** (Python 3.10+, nothing to install: it only uses the standard
library):

```bash
python3 guardrail_lambda.py
```

> To emit CloudWatch metrics (real AWS mode), install `boto3` with
> `pip install -r requirements.txt`. Validated: the tests pass, blocking
> "180 days" and allowing "60 days".

The chatbot serves Brazilian customers, so its answers (and the test cases)
are in Portuguese. The patterns understand both Portuguese and English
("60 dias" and "60 days" count as the same deadline).

---

## a) At least 4 possible technical causes of the inconsistency

The hallucination showing up in **~15% of sessions** (not 100%, not 0%) is an
important clue: it points to a **stochastic** factor (temperature) and/or a
**context-dependent** one (what was retrieved or sent varies between sessions).

1. **High temperature / sampling parameters.** With a high `temperature` (or a
   high `top_p`), the model samples less likely tokens. For factual questions
   with a single correct answer (a deadline), that adds variability: sometimes
   the right number, sometimes a plausible but false one. It fits the "sometimes
   yes, sometimes no" nature of the 15% well.

2. **Missing or incomplete context (RAG/grounding failure).** If the dispute
   policy is **not always** retrieved and injected into the prompt (low
   retriever recall, poor chunking, or the rule was dropped when the context
   was truncated for size, see Exercise 2), the model falls back on its
   **parametric knowledge** (what it "remembers" from training) and makes
   things up. The 15% variability matches "sometimes the right chunk gets in,
   sometimes it doesn't".

3. **Weak system prompt with no grounding instruction.** If the prompt does not
   force the model to answer **only from the context** and to **never invent
   deadlines, amounts or regulations**, it fills gaps with authoritative-sounding
   inventions ("according to the Central Bank"). The lack of a safe fallback
   ("don't know, transfer to an agent") makes it worse.

4. **Outdated, ambiguous or contradictory knowledge base.** If the knowledge
   base has conflicting versions (e.g. an old page citing 180 days, another
   citing 60) or ambiguous wording, retrieval can bring back the wrong chunk.
   In that case the "hallucination" is really **bad data retrieved
   correctly**. Training contamination by generic internet content
   (regulations that do not apply to this company) also belongs here.

**Other plausible causes:** inconsistent model/version across deploys (an
accidental A/B route); context polluted by a long history (the model "gets
lost" across 40 turns, directly connected to Ex. 2, where cutting context also
cuts hallucination); or missing or poor few-shot examples.

---

## b) A/B test plan to find the root cause

The goal is to **isolate one variable at a time** so we can attribute cause,
instead of changing everything at once. Structure:

### Preparation

- **Primary metric:** hallucination rate = % of answers that cite a deadline or
  amount **not present in the official base** (measured automatically by the
  guardrail from item c, running in *observe* mode: it only logs, it does not
  block).
- **Secondary metrics:** correctness against a golden set, handoff rate, CSAT.
- **Evaluation set:** a **golden set** of factual questions (including variants
  of "can I dispute a charge from X months ago?") with known correct answers,
  plus sampled real traffic.
- **Significance:** size the sample for statistical power (baseline 15%, aiming
  to detect a drop to, say, ≤5% at 95% confidence). Randomize by `sessionId`
  (hash) to avoid contamination.

### Experiments (one variable at a time)

| Exp. | Isolated variable | Group A (control) | Group B (treatment) | Hypothesis tested |
|------|-------------------|-------------------|---------------------|-------------------|
| 1 | **Temperature** | current temp | temp = 0.0 | Cause #1 (sampling) |
| 2 | **RAG grounding** | current context | force policy injection + log recall | Cause #2 (context) |
| 3 | **System prompt** | current prompt | optimized prompt (Ex. 2b) | Cause #3 (instructions) |
| 4 | **Knowledge base** | current KB | curated/deduplicated KB | Cause #4 (bad data) |

**Recommended order:** run **Exp. 1 first** (cheapest and fastest change, just
one parameter). If `temperature=0` brings hallucination close to zero, we found
the dominant cause. If not, move on to RAG (Exp. 2) and the prompt (Exp. 3).

**Keeping it isolated:** in each experiment **everything else stays constant**
(same model/version, same randomized traffic, same time window, same golden
set). Log per session: `variant`, `temperature`, `retrieved_chunks`,
`recall_flag`, `prompt_version`, `kb_version`, `hallucination_flag`. That way,
even with interactions, we can break down each variable's contribution.

**Reading the results:** compare the hallucination rate of A vs. B with a test
of proportions (e.g. z-test / chi-square). The variable whose treatment gives
the largest statistically significant drop is the main root cause (there may be
more than one).

---

## c) Guardrail to detect and block hallucinations

Runnable implementation in [`guardrail_lambda.py`](./guardrail_lambda.py). It is
an **output** guardrail, called **synchronously between the model and the
customer**.

### Technical approach: defense in depth

```
Model (Bedrock) ──► Candidate answer
                          │
                          ▼
              ┌───────────────────────────┐
              │  GUARDRAIL (AWS Lambda)    │
              │  Layer 1: regex extracts   │  deadlines ("60 days") and
              │           deadlines and    │  amounts ("R$ 100,00")
              │           amounts          │
              │  Layer 2: does each number │  must exist in the
              │           exist in the KB? │  official context (RAG)
              │  Layer 3 (optional):       │  LLM judge only for
              │           semantic check   │  ambiguous cases
              └───────────────────────────┘
                    │                 │
             allowed=True      allowed=False
                    │                 │
                    ▼                 ▼
             deliver answer     safe fallback answer
                                + CloudWatch metric/alert
```

**Layer 1: structured extraction (regex).** Finds every **deadline** pattern in
the answer (`\d+ (days|months|weeks|years|hours)` and the Portuguese
equivalents) and every **amount** (`R$ ...`, `US$ ...`, `... reais`). Cheap
and deterministic. This is exactly where fintech hallucinations show up
(made-up deadlines and amounts).

**Layer 2: fact check against the official base.** Every cited deadline or
amount **must appear** in the official context retrieved (RAG) for that
question, the source of truth. If the answer says "180 days" but the context
only has "60 days", the number is **unsupported**, so it is **blocked**. This
is the check that catches the case from the brief (validated in the tests:
"180 days" is blocked, "60 days" passes).

**Layer 3 (optional): semantic LLM judge.** For ambiguous cases (e.g. the answer
paraphrases without citing a number, or uses a synonym), a second, **small and
cheap** model (e.g. Claude Haiku) checks "is this answer supported by the
context?". It runs **only when layers 1-2 cannot decide with confidence**, so
we don't pay for an extra inference on every request.

### Action and principles

- **On block:** replace the answer with a **safe fallback** ("I can't confirm
  this, let me transfer you to an agent"), never deliver the made-up number,
  and **emit a metric/alert** (`Chatbot/Guardrail/GuardrailBlocked`).
- **Fail-safe:** if the guardrail itself errors, the default is to **block**. In
  fintech, staying silent beats hallucinating.
- **Observability:** the guardrail trigger rate becomes a continuous quality
  metric (closing the loop with Ex. 2c and the A/B test in item b).

> **Note:** native **AWS Bedrock Guardrails** covers part of this (denied
> topics, PII, filters), but it **does not check a specific deadline against
> the company's policy**. That is why the Layer 2 fact check is custom. The two
> approaches complement each other.

### Validation (run)

Running `python guardrail_lambda.py`:

```
Answer A (correct, '60 dias'):          allowed = True   ✓
Answer B (hallucination, '180 dias'):   allowed = False  ✓  (fallback + reason)
Answer C (English, '60 days'):          allowed = True   ✓
Answer D (made-up amount, 'R$ 150,00'): allowed = False  ✓
✓ All assertions passed.
```

---

### AI usage

- **Tool:** Claude (Anthropic's AI assistant).
- **Where AI helped (a significant part):** it drafted `guardrail_lambda.py`
  (regex + layered checks) and the first versions of the A/B experiment table
  and the list of hallucination causes.
- **What I did and validated myself:**
  - **Ran the guardrail tests** (Python 3.10) and confirmed the expected
    behavior: it blocks "180 days" and allows "60 days"; the `assert`s pass.
  - **Fixed the AI's logic:** the deadline/amount regex and the number
    comparison gave false positives in the first version; I changed it to
    accept equivalent units between the knowledge base and the answer.
  - **My decisions:** the order of the A/B experiments (temperature first, as
    the cheapest change) and the link between long context and hallucination
    (connection to Ex. 2).
- **Honest summary:** AI produced the skeleton of the code and the plan; I
  tested it, fixed the errors it introduced and owned the LLMOps/guardrail
  decisions.
- **Later update:** when translating the repository to English, the patterns
  were extended to understand English as well as Portuguese and `US$` amounts,
  and two test cases were added (an English answer and a made-up amount).
