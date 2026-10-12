"""
context_optimizer.py
====================

Runnable demo of the cost-per-session reduction strategy from Exercise 2.
It builds the payload sent to the model (AWS Bedrock / Claude) by applying:

  1. A lean system prompt (loaded from system_prompt_optimized.txt)
  2. RAG: injects ONLY the policies relevant to the current message
     (here a keyword-based "retriever" stands in; in production it would be a
     vector search on OpenSearch/Kendra/pgvector).
  3. Sliding window: keeps only the last N complete turns.
  4. Summarization: older turns become a short summary (stubbed here; in
     production, a cheap call to a smaller model such as Claude Haiku).
  5. Token budget: trims the history until it fits under the limit.

The goal is to show the mechanics and measure the token reduction before and
after. No AWS credentials are needed: the Bedrock call is wrapped and only runs
when boto3 and credentials are available (see `invoke_bedrock`).

Run:
    python context_optimizer.py
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass, field
from pathlib import Path


# ---------------------------------------------------------------------------
# Token estimation
# ---------------------------------------------------------------------------
# In production, use the provider's tokenizer (e.g. the token counts returned
# by the Bedrock API, or `anthropic.count_tokens`). Here we use the common
# ~4 characters per token heuristic, which is enough to reason about cost and
# for this exercise's comparison.
def estimate_tokens(text: str) -> int:
    return math.ceil(len(text) / 4)


@dataclass
class Turn:
    role: str  # "user" | "assistant"
    content: str

    def tokens(self) -> int:
        return estimate_tokens(f"{self.role}: {self.content}")


# ---------------------------------------------------------------------------
# 1) Optimized system prompt
# ---------------------------------------------------------------------------
def load_system_prompt() -> str:
    path = Path(__file__).parent / "system_prompt_optimized.txt"
    return path.read_text(encoding="utf-8").strip()


# ---------------------------------------------------------------------------
# 2) RAG: retrieve only the policies relevant to the current question
# ---------------------------------------------------------------------------
# Knowledge base (illustrative excerpt). In production it would live outside
# the prompt, indexed in a vector store, with each entry as a retrievable chunk.
KNOWLEDGE_BASE = {
    "dispute": (
        "Charge dispute policy: customers can dispute a charge within 60 days "
        "of the charge date. Disputes filed after that require a manual review "
        "by the support team."
    ),
    "refund": (
        "Refund policy: approved refunds are credited within 10 business days "
        "to the original payment method."
    ),
    "balance": (
        "Balance inquiry: the available balance reflects settled transactions; "
        "amounts still processing show up as 'pending'."
    ),
}


def retrieve_policies(user_message: str, top_k: int = 1) -> list[str]:
    """Keyword-based stand-in for a retriever.

    Replace with a real vector search (embeddings + cosine similarity).
    Returns only the relevant chunks instead of dumping all 80 rules into the
    prompt.
    """
    msg = user_message.lower()
    keywords = {
        "dispute": ["disput", "charge", "deadline"],
        "refund": ["refund", "chargeback", "money back"],
        "balance": ["balance", "statement"],
    }
    scored: list[tuple[int, str]] = []
    for key, kws in keywords.items():
        score = sum(1 for kw in kws if kw in msg)
        if score:
            scored.append((score, KNOWLEDGE_BASE[key]))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [chunk for _, chunk in scored[:top_k]]


# ---------------------------------------------------------------------------
# 3) + 4) Sliding window + summary of the older history
# ---------------------------------------------------------------------------
def summarize_old_turns(old_turns: list[Turn]) -> str:
    """Summarization stub.

    In production: a cheap call to a small model (e.g. Claude Haiku) that
    condenses the older turns into 1-2 sentences with the facts that matter
    (identity, product discussed, decisions). Here we just join the user's
    requests in compressed form so the demo runs without network access.
    """
    if not old_turns:
        return ""
    user_points = [t.content for t in old_turns if t.role == "user"]
    joined = "; ".join(user_points)
    return f"Summary of the earlier conversation (customer topics): {joined}."


def apply_sliding_window(
    history: list[Turn], keep_last: int
) -> tuple[list[Turn], str]:
    """Keeps the last `keep_last` turns and summarizes the rest."""
    if len(history) <= keep_last:
        return history, ""
    old = history[:-keep_last]
    recent = history[-keep_last:]
    return recent, summarize_old_turns(old)


# ---------------------------------------------------------------------------
# 5) Token budget: make sure the payload fits under the limit
# ---------------------------------------------------------------------------
@dataclass
class OptimizerConfig:
    keep_last_turns: int = 4          # sliding window
    max_context_tokens: int = 1500    # payload ceiling (excluding the answer)
    rag_top_k: int = 1


@dataclass
class BuiltContext:
    system_prompt: str
    summary: str
    retrieved: list[str]
    turns: list[Turn]
    total_tokens: int = field(init=False)

    def __post_init__(self):
        self.total_tokens = (
            estimate_tokens(self.system_prompt)
            + estimate_tokens(self.summary)
            + sum(estimate_tokens(c) for c in self.retrieved)
            + sum(t.tokens() for t in self.turns)
        )

    def as_messages(self) -> list[dict]:
        """Bedrock/Anthropic-style messages."""
        ctx_block = "\n".join(self.retrieved)
        if self.summary:
            ctx_block = f"{self.summary}\n{ctx_block}"
        return [
            {"role": "user", "content": f"[CONTEXT]\n{ctx_block}"},
            *[{"role": t.role, "content": t.content} for t in self.turns],
        ]


def build_optimized_context(
    history: list[Turn], user_message: str, cfg: OptimizerConfig
) -> BuiltContext:
    system_prompt = load_system_prompt()
    retrieved = retrieve_policies(user_message, top_k=cfg.rag_top_k)

    full_history = history + [Turn("user", user_message)]
    turns, summary = apply_sliding_window(full_history, cfg.keep_last_turns)

    built = BuiltContext(system_prompt, summary, retrieved, turns)

    # If it still exceeds the budget, shrink the window step by step.
    while built.total_tokens > cfg.max_context_tokens and len(turns) > 1:
        dropped = turns[0]
        turns = turns[1:]
        summary = summarize_old_turns([dropped]) if not summary else summary
        built = BuiltContext(system_prompt, summary, retrieved, turns)

    return built


# ---------------------------------------------------------------------------
# Baseline (CURRENT payload, no optimization) for comparison
# ---------------------------------------------------------------------------
BLOATED_SYSTEM_PROMPT = "You are a financial assistant for Company X. Rules: " + " ".join(
    f"Rule {i}: long text describing a detailed business policy." for i in range(1, 81)
)


def build_baseline_context(history: list[Turn], user_message: str) -> BuiltContext:
    """Reproduces the bloated payload described in the brief (~3,200 tokens):
    a huge system prompt + the full history (up to 40 turns)."""
    full_history = history + [Turn("user", user_message)]
    # No RAG and no window: everything goes in.
    b = BuiltContext(BLOATED_SYSTEM_PROMPT, "", [], full_history)
    return b


# ---------------------------------------------------------------------------
# (Optional) real Bedrock call: only runs when boto3 + credentials exist
# ---------------------------------------------------------------------------
def invoke_bedrock(messages: list[dict], system_prompt: str) -> str | None:
    try:
        import json
        import boto3  # type: ignore
    except ImportError:
        return None
    if not os.getenv("AWS_REGION"):
        return None
    client = boto3.client("bedrock-runtime")
    body = {
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": 512,
        "temperature": 0.0,
        "system": system_prompt,
        "messages": messages,
    }
    resp = client.invoke_model(
        modelId="anthropic.claude-3-haiku-20240307-v1:0",
        body=json.dumps(body),
    )
    payload = json.loads(resp["body"].read())
    return payload["content"][0]["text"]


# ---------------------------------------------------------------------------
# Demo
# ---------------------------------------------------------------------------
def _demo():
    # Simulates a long session (40 turns, as in the brief).
    history: list[Turn] = []
    for i in range(20):
        history.append(Turn("user", f"generic customer question number {i}"))
        history.append(Turn("assistant", f"generic answer number {i}"))

    user_message = "what is the deadline to dispute a charge?"

    baseline = build_baseline_context(history, user_message)
    optimized = build_optimized_context(history, user_message, OptimizerConfig())

    print("=== FinOps: tokens per session ===\n")
    print(f"Baseline  (current): {baseline.total_tokens:>5} tokens")
    print(f"Optimized (new)    : {optimized.total_tokens:>5} tokens")
    reduction = 100 * (1 - optimized.total_tokens / baseline.total_tokens)
    print(f"Reduction          : {reduction:>5.1f}%\n")

    print("Policies retrieved via RAG:")
    for c in optimized.retrieved:
        print(f"  - {c[:80]}...")
    print(f"\nTurns kept (sliding window): {len(optimized.turns)}")
    print(f"Summary of the older history: {optimized.summary[:80]}...")

    answer = invoke_bedrock(optimized.as_messages(), optimized.system_prompt)
    if answer:
        print(f"\nBedrock answer:\n{answer}")
    else:
        print("\n(Bedrock call skipped: boto3/credentials not available.)")


if __name__ == "__main__":
    _demo()
