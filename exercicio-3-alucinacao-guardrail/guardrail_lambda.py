"""
guardrail_lambda.py
===================

Guardrail de saída para o chatbot da fintech (Exercício 3, item c).

Objetivo: detectar e bloquear respostas do modelo que citem PRAZOS ou VALORES
que NÃO estejam presentes na base de conhecimento oficial — a principal fonte
das alucinações reportadas (ex.: "180 dias, conforme o Banco Central").

Estratégia em camadas (defense-in-depth):
  Camada 1 — Extração estruturada (regex): encontra todo número que se
             comporte como prazo (ex. "60 dias") ou valor (ex. "R$ 100,00")
             na resposta do modelo.
  Camada 2 — Verificação factual: cada número extraído PRECISA existir na base
             de conhecimento oficial recuperada para aquela pergunta. Se citar
             um número que não está na fonte da verdade → BLOQUEIA.
  Camada 3 — (opcional) Verificação semântica por um segundo modelo (LLM juiz),
             acionada só em casos ambíguos, para não pagar por ela sempre.

Ação em caso de bloqueio: substituir por uma resposta segura de fallback e
emitir métrica/alerta (CloudWatch). Fail-safe: em erro interno do guardrail,
o comportamento padrão é BLOQUEAR (melhor calar do que alucinar em fintech).

Uso: pode rodar como AWS Lambda (handler `lambda_handler`) invocada de forma
síncrona entre o modelo e o cliente, ou ser importada como biblioteca.

Execução dos testes locais: `python guardrail_lambda.py`
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field


PRAZO_RE = re.compile(
    r"(\d{1,4})\s*(dias?|dia|meses?|m[eê]s|semanas?|anos?|horas?)",
    re.IGNORECASE,
)

VALOR_RE = re.compile(
    r"(?:R\$\s?)(\d{1,3}(?:\.\d{3})*(?:,\d{2})?)|(\d+)\s*reais",
    re.IGNORECASE,
)


def _norm_num(raw: str) -> str:
    """Normaliza '1.234,56' -> '1234.56' e '60' -> '60' para comparação."""
    raw = raw.strip()
    if "," in raw:
        raw = raw.replace(".", "").replace(",", ".")
    return raw


@dataclass
class Claims:
    prazos: set[str] = field(default_factory=set)   # ex. {"60 dias"}
    valores: set[str] = field(default_factory=set)  # ex. {"100.00"}


def extract_claims(text: str) -> Claims:
    c = Claims()
    for num, unidade in PRAZO_RE.findall(text):
        c.prazos.add(f"{num} {unidade.lower()}")
    for brl, reais in VALOR_RE.findall(text):
        raw = brl or reais
        if raw:
            c.valores.add(_norm_num(raw))
    return c


@dataclass
class GuardrailResult:
    allowed: bool
    reason: str
    unsupported_prazos: list[str] = field(default_factory=list)
    unsupported_valores: list[str] = field(default_factory=list)


def verify_against_kb(response_text: str, kb_context: str) -> GuardrailResult:
    resp_claims = extract_claims(response_text)
    kb_claims = extract_claims(kb_context)

    kb_prazo_nums = {p.split()[0] for p in kb_claims.prazos}
    resp_prazo_nums = {p.split()[0]: p for p in resp_claims.prazos}

    unsupported_prazos = [
        original for num, original in resp_prazo_nums.items() if num not in kb_prazo_nums
    ]
    unsupported_valores = [
        v for v in resp_claims.valores if v not in kb_claims.valores
    ]

    if unsupported_prazos or unsupported_valores:
        return GuardrailResult(
            allowed=False,
            reason="Resposta cita prazo/valor ausente da base de conhecimento oficial.",
            unsupported_prazos=unsupported_prazos,
            unsupported_valores=unsupported_valores,
        )
    return GuardrailResult(allowed=True, reason="OK")


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
        pass


def check_response(model_response: str, kb_context: str) -> dict:
    try:
        result = verify_against_kb(model_response, kb_context)
    except Exception as e:  # fail-safe: em dúvida, bloqueia.
        _emit_metric(True, GuardrailResult(False, f"erro interno: {e}"))
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
            "unsupported_prazos": result.unsupported_prazos,
            "unsupported_valores": result.unsupported_valores,
        }

    _emit_metric(False, result)
    return {"allowed": True, "final_response": model_response, "reason": result.reason}


def lambda_handler(event: dict, context=None) -> dict:
    model_response = event.get("model_response", "")
    kb_context = event.get("kb_context", "")
    return check_response(model_response, kb_context)


def _run_tests():
    kb = "Política de contestação: o prazo para contestar é de 60 dias a partir da data da cobrança."

    resposta_A = "O prazo para contestação é de 60 dias a partir da data da cobrança."
    resposta_B = ("Sim, você tem até 180 dias para contestar qualquer cobrança, "
                  "conforme regulamentação do Banco Central.")

    r_a = check_response(resposta_A, kb)
    r_b = check_response(resposta_B, kb)

    print("=== Teste do guardrail ===\n")
    print("Resposta A (correta, '60 dias'):")
    print(f"  allowed = {r_a['allowed']}  -> esperado: True")
    print(f"  final   = {r_a['final_response'][:60]}...\n")

    print("Resposta B (alucinação, '180 dias'):")
    print(f"  allowed = {r_b['allowed']}  -> esperado: False")
    print(f"  motivo  = {r_b['reason']}")
    print(f"  prazos não suportados = {r_b.get('unsupported_prazos')}")
    print(f"  final (fallback) = {r_b['final_response'][:60]}...\n")

    assert r_a["allowed"] is True, "Resposta correta não deveria ser bloqueada"
    assert r_b["allowed"] is False, "Alucinação deveria ser bloqueada"
    print("✓ Todos os asserts passaram.")


if __name__ == "__main__":
    _run_tests()
