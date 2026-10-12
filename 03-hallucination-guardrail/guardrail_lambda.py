"""
guardrail_lambda.py
===================

Output guardrail for the fintech chatbot (Exercise 3, item c).

Goal: detect and block model answers that cite DEADLINES or AMOUNTS that are
NOT present in the official knowledge base, the main source of the reported
hallucinations (e.g. "180 days, according to the Central Bank").

Layered strategy (defense in depth):
  Layer 1 - Structured extraction (regex): finds every number that behaves
            like a deadline (e.g. "60 days" / "60 dias") or an amount
            (e.g. "R$ 100,00") in the model's answer.
  Layer 2 - Fact check: every extracted number MUST exist in the official
            knowledge base retrieved for that question. If the answer cites a
            number that is not in the source of truth, it is BLOCKED.
  Layer 3 - (optional) Semantic check by a second model (LLM as judge), used
            only for ambiguous cases so we don't pay for it on every request.

On block: replace the answer with a safe fallback and emit a metric/alert
(CloudWatch). Fail-safe: if the guardrail itself errors, the default is to
BLOCK (in fintech, staying silent beats hallucinating).

The chatbot serves Brazilian customers, so the patterns understand Portuguese
(the production language) as well as English.

Usage: can run as an AWS Lambda (handler `lambda_handler`) invoked
synchronously between the model and the customer, or be imported as a library.

Local tests: `python guardrail_lambda.py`
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field


DEADLINE_RE = re.compile(
    r"(\d{1,4})\s*("
    r"dias?|meses?|m[eê]s|semanas?|anos?|horas?"          # Portuguese
    r"|days?|months?|weeks?|years?|hours?"                # English
    r")\b",
    re.IGNORECASE,
)

AMOUNT_RE = re.compile(
    r"(?:R\$|US\$|\$)\s?(\d{1,3}(?:[.,]\d{3})*(?:[.,]\d{2})?)|(\d+)\s*(?:reais|dollars)",
    re.IGNORECASE,
)


def _norm_num(raw: str) -> str:
    """Normalizes '1.234,56' -> '1234.56', '1,234.56' -> '1234.56', '60' -> '60'."""
    raw = raw.strip()
    if "," in raw and (raw.rfind(",") > raw.rfind(".")):
        # Brazilian format: '.' groups thousands, ',' marks decimals.
        raw = raw.replace(".", "").replace(",", ".")
    else:
        raw = raw.replace(",", "")
    return raw


@dataclass
class Claims:
    deadlines: set[str] = field(default_factory=set)  # e.g. {"60 dias"}
    amounts: set[str] = field(default_factory=set)    # e.g. {"100.00"}


def extract_claims(text: str) -> Claims:
    c = Claims()
    for num, unit in DEADLINE_RE.findall(text):
        c.deadlines.add(f"{num} {unit.lower()}")
    for currency_amount, word_amount in AMOUNT_RE.findall(text):
        raw = currency_amount or word_amount
        if raw:
            c.amounts.add(_norm_num(raw))
    return c


@dataclass
class GuardrailResult:
    allowed: bool
    reason: str
    unsupported_deadlines: list[str] = field(default_factory=list)
    unsupported_amounts: list[str] = field(default_factory=list)


def verify_against_kb(response_text: str, kb_context: str) -> GuardrailResult:
    resp_claims = extract_claims(response_text)
    kb_claims = extract_claims(kb_context)

    # Compare by number, so equivalent units ("60 dias" / "60 days") match.
    kb_deadline_nums = {d.split()[0] for d in kb_claims.deadlines}
    resp_deadline_nums = {d.split()[0]: d for d in resp_claims.deadlines}

    unsupported_deadlines = [
        original for num, original in resp_deadline_nums.items() if num not in kb_deadline_nums
    ]
    unsupported_amounts = [
        a for a in resp_claims.amounts if a not in kb_claims.amounts
    ]

    if unsupported_deadlines or unsupported_amounts:
        return GuardrailResult(
            allowed=False,
            reason="Answer cites a deadline or amount that is not in the official knowledge base.",
            unsupported_deadlines=unsupported_deadlines,
            unsupported_amounts=unsupported_amounts,
        )
    return GuardrailResult(allowed=True, reason="OK")


# Customer-facing text stays in Portuguese (the chatbot's production language):
# "I can't safely confirm this information right now. I'll transfer you to an
# agent who can check your account details and the applicable policy."
SAFE_FALLBACK = (
    "Não consigo confirmar essa informação com segurança agora. "
    "Vou te encaminhar para um atendente que pode verificar os detalhes "
    "da sua conta e a política aplicável."
)


def _emit_metric(blocked: bool, result: GuardrailResult) -> None:
    try:
        import boto3

        cw = boto3.client("cloudwatch")
        cw.put_metric_data(
            Namespace="Chatbot/Guardrail",
            MetricData=[
                {"MetricName": "GuardrailBlocked", "Value": 1 if blocked else 0, "Unit": "Count"},
            ],
        )
    except Exception:
        # Metrics must never break the request path.
        pass


def check_response(model_response: str, kb_context: str) -> dict:
    try:
        result = verify_against_kb(model_response, kb_context)
    except Exception as e:  # fail-safe: when in doubt, block.
        _emit_metric(True, GuardrailResult(False, f"internal error: {e}"))
        return {
            "allowed": False,
            "final_response": SAFE_FALLBACK,
            "reason": f"guardrail_error: {e}",
        }

    if not result.allowed:
        _emit_metric(True, result)
        return {
            "allowed": False,
            "final_response": SAFE_FALLBACK,
            "reason": result.reason,
            "unsupported_deadlines": result.unsupported_deadlines,
            "unsupported_amounts": result.unsupported_amounts,
        }

    _emit_metric(False, result)
    return {"allowed": True, "final_response": model_response, "reason": result.reason}


def lambda_handler(event: dict, context=None) -> dict:
    model_response = event.get("model_response", "")
    kb_context = event.get("kb_context", "")
    return check_response(model_response, kb_context)


def _run_tests():
    # Scenario from the brief, in Portuguese as the chatbot answers in production.
    # KB: "Dispute policy: the deadline to dispute is 60 days from the charge date."
    kb = "Política de contestação: o prazo para contestar é de 60 dias a partir da data da cobrança."

    # Answer A (correct): "The dispute deadline is 60 days from the charge date."
    answer_a = "O prazo para contestação é de 60 dias a partir da data da cobrança."
    # Answer B (hallucination): "Yes, you have up to 180 days to dispute any charge,
    # according to Central Bank regulations."
    answer_b = ("Sim, você tem até 180 dias para contestar qualquer cobrança, "
                "conforme regulamentação do Banco Central.")
    # Extra cases: English answer and a made-up amount.
    answer_c = "You can dispute a charge within 60 days of the charge date."
    answer_d = "O estorno de R$ 150,00 cai em 60 dias."

    r_a = check_response(answer_a, kb)
    r_b = check_response(answer_b, kb)
    r_c = check_response(answer_c, kb)
    r_d = check_response(answer_d, kb)

    print("=== Guardrail test ===\n")
    print("Answer A (correct, '60 dias'):")
    print(f"  allowed = {r_a['allowed']}  -> expected: True")
    print(f"  final   = {r_a['final_response'][:60]}...\n")

    print("Answer B (hallucination, '180 dias'):")
    print(f"  allowed = {r_b['allowed']}  -> expected: False")
    print(f"  reason  = {r_b['reason']}")
    print(f"  unsupported deadlines = {r_b.get('unsupported_deadlines')}")
    print(f"  final (fallback) = {r_b['final_response'][:60]}...\n")

    print("Answer C (English, '60 days'):")
    print(f"  allowed = {r_c['allowed']}  -> expected: True\n")

    print("Answer D (made-up amount, 'R$ 150,00'):")
    print(f"  allowed = {r_d['allowed']}  -> expected: False")
    print(f"  unsupported amounts = {r_d.get('unsupported_amounts')}\n")

    assert r_a["allowed"] is True, "A correct answer must not be blocked"
    assert r_b["allowed"] is False, "A hallucination must be blocked"
    assert r_c["allowed"] is True, "Equivalent units in English must match"
    assert r_d["allowed"] is False, "An amount missing from the KB must be blocked"
    print("✓ All assertions passed.")


if __name__ == "__main__":
    _run_tests()
