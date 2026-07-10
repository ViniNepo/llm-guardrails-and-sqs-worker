"""
context_optimizer.py
====================

Demonstração executável da estratégia de redução de custo por sessão do
Exercício 2. Monta o payload enviado ao modelo (AWS Bedrock / Claude) aplicando:

  1. System prompt enxuto (carregado de system_prompt_otimizado.txt)
  2. RAG: injeta APENAS as políticas relevantes à mensagem atual
     (aqui, um "retriever" simulado por palavras-chave; em produção seria uma
     busca vetorial em OpenSearch/Kendra/pgvector).
  3. Sliding window: mantém só os últimos N turnos completos.
  4. Sumarização: turnos mais antigos viram um resumo curto (stub — em produção,
     uma chamada barata a um modelo menor, ex. Claude Haiku).
  5. Orçamento de tokens (token budget): corta o histórico até caber no limite.

O objetivo é mostrar a mecânica e permitir medir a redução de tokens antes/depois.
Não requer credenciais AWS: a chamada ao Bedrock é encapsulada e só roda se
boto3 + credenciais estiverem presentes (ver `build_and_estimate`).

Execução:
    python context_optimizer.py
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass, field
from pathlib import Path


# ---------------------------------------------------------------------------
# Estimativa de tokens
# ---------------------------------------------------------------------------
# Em produção, use o tokenizer do provedor (ex.: contagem retornada pela API do
# Bedrock, ou `anthropic.count_tokens`). Aqui usamos a heurística amplamente
# aceita de ~4 caracteres por token para texto em pt/en — suficiente para
# raciocínio de custo e para os testes deste exercício.
def estimate_tokens(text: str) -> int:
    return math.ceil(len(text) / 4)


@dataclass
class Turn:
    role: str  # "user" | "assistant"
    content: str

    def tokens(self) -> int:
        return estimate_tokens(f"{self.role}: {self.content}")


# ---------------------------------------------------------------------------
# 1) System prompt otimizado
# ---------------------------------------------------------------------------
def load_system_prompt() -> str:
    path = Path(__file__).parent / "system_prompt_otimizado.txt"
    return path.read_text(encoding="utf-8").strip()


# ---------------------------------------------------------------------------
# 2) RAG — recupera só as políticas relevantes à pergunta atual
# ---------------------------------------------------------------------------
# Base de conhecimento (trecho ilustrativo). Em produção viveria fora do prompt,
# indexada em um vector store, e cada item seria um chunk recuperável.
KNOWLEDGE_BASE = {
    "contestacao": (
        "Política de contestação de cobrança: o prazo para contestar é de 60 "
        "dias a partir da data da cobrança. Contestações fora do prazo exigem "
        "análise manual pelo time de atendimento."
    ),
    "reembolso": (
        "Política de reembolso: reembolsos aprovados são creditados em até 10 "
        "dias úteis no mesmo meio de pagamento."
    ),
    "saldo": (
        "Consulta de saldo: o saldo disponível reflete transações liquidadas; "
        "valores em processamento aparecem como 'pendentes'."
    ),
}


def retrieve_policies(user_message: str, top_k: int = 1) -> list[str]:
    """Retriever simulado por palavras-chave.

    Substitua por busca vetorial real (embeddings + similaridade de cosseno).
    Retorna só os chunks relevantes, evitando despejar as 80 regras no prompt.
    """
    msg = user_message.lower()
    keywords = {
        "contestacao": ["contest", "cobran", "prazo"],
        "reembolso": ["reembols", "estorn", "devolu"],
        "saldo": ["saldo", "extrato"],
    }
    scored: list[tuple[int, str]] = []
    for key, kws in keywords.items():
        score = sum(1 for kw in kws if kw in msg)
        if score:
            scored.append((score, KNOWLEDGE_BASE[key]))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [chunk for _, chunk in scored[:top_k]]


# ---------------------------------------------------------------------------
# 3) + 4) Sliding window + sumarização do histórico antigo
# ---------------------------------------------------------------------------
def summarize_old_turns(old_turns: list[Turn]) -> str:
    """Stub de sumarização.

    Em produção: uma chamada barata a um modelo pequeno (ex. Claude Haiku) para
    condensar os turnos antigos em 1-2 frases com os fatos relevantes
    (identidade, produto discutido, decisões). Aqui apenas concatenamos os
    pedidos do usuário de forma comprimida para a demonstração ser executável
    sem rede.
    """
    if not old_turns:
        return ""
    user_points = [t.content for t in old_turns if t.role == "user"]
    joined = "; ".join(user_points)
    return f"Resumo da conversa anterior (assuntos do cliente): {joined}."


def apply_sliding_window(
    history: list[Turn], keep_last: int
) -> tuple[list[Turn], str]:
    """Mantém os últimos `keep_last` turnos e resume o restante."""
    if len(history) <= keep_last:
        return history, ""
    old = history[:-keep_last]
    recent = history[-keep_last:]
    return recent, summarize_old_turns(old)


# ---------------------------------------------------------------------------
# 5) Orçamento de tokens — garante que o payload cabe no limite
# ---------------------------------------------------------------------------
@dataclass
class OptimizerConfig:
    keep_last_turns: int = 4          # sliding window
    max_context_tokens: int = 1500    # teto do payload (fora a resposta)
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
        """Formato de mensagens estilo Bedrock/Anthropic."""
        ctx_block = "\n".join(self.retrieved)
        if self.summary:
            ctx_block = f"{self.summary}\n{ctx_block}"
        return [
            {"role": "user", "content": f"[CONTEXTO]\n{ctx_block}"},
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

    # Se ainda estourar o orçamento, encolhe a janela progressivamente.
    while built.total_tokens > cfg.max_context_tokens and len(turns) > 1:
        dropped = turns[0]
        turns = turns[1:]
        summary = summarize_old_turns([dropped]) if not summary else summary
        built = BuiltContext(system_prompt, summary, retrieved, turns)

    return built


# ---------------------------------------------------------------------------
# Baseline (payload ATUAL, sem otimização) para comparação
# ---------------------------------------------------------------------------
BLOATED_SYSTEM_PROMPT = "Você é um assistente financeiro da empresa X. Regras: " + " ".join(
    f"Regra {i}: texto extenso descrevendo uma política de negócio detalhada." for i in range(1, 81)
)


def build_baseline_context(history: list[Turn], user_message: str) -> BuiltContext:
    """Reproduz o payload inchado descrito no enunciado (~3.200 tokens):
    system prompt gigante + histórico completo (até 40 turnos)."""
    full_history = history + [Turn("user", user_message)]
    # Sem RAG e sem window: tudo entra.
    b = BuiltContext(BLOATED_SYSTEM_PROMPT, "", [], full_history)
    return b


# ---------------------------------------------------------------------------
# (Opcional) chamada real ao Bedrock — só roda se boto3+credenciais existirem
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
# Demonstração
# ---------------------------------------------------------------------------
def _demo():
    # Simula uma sessão longa (40 turnos, como no enunciado).
    history: list[Turn] = []
    for i in range(20):
        history.append(Turn("user", f"pergunta genérica número {i} do cliente"))
        history.append(Turn("assistant", f"resposta genérica número {i}"))

    user_message = "qual o prazo para contestar uma cobrança?"

    baseline = build_baseline_context(history, user_message)
    optimized = build_optimized_context(history, user_message, OptimizerConfig())

    print("=== FinOps: comparação de tokens por sessão ===\n")
    print(f"Baseline  (atual): {baseline.total_tokens:>5} tokens")
    print(f"Otimizado (novo) : {optimized.total_tokens:>5} tokens")
    reducao = 100 * (1 - optimized.total_tokens / baseline.total_tokens)
    print(f"Redução          : {reducao:>5.1f}%\n")

    print("Políticas recuperadas via RAG:")
    for c in optimized.retrieved:
        print(f"  - {c[:80]}...")
    print(f"\nTurnos mantidos (sliding window): {len(optimized.turns)}")
    print(f"Resumo do histórico antigo: {optimized.summary[:80]}...")

    answer = invoke_bedrock(optimized.as_messages(), optimized.system_prompt)
    if answer:
        print(f"\nResposta do Bedrock:\n{answer}")
    else:
        print("\n(Chamada ao Bedrock ignorada: boto3/credenciais ausentes.)")


if __name__ == "__main__":
    _demo()
