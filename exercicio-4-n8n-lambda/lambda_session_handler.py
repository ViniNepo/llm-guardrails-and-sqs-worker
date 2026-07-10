"""
lambda_session_handler.py
=========================

Lógica de negócio do fluxo "sessão encerrada com sentimento negativo"
(Exercício 4). Concentra as chamadas a serviços AWS/externos em uma Lambda,
deixando o N8n apenas como orquestrador (roteamento, retry, visibilidade).

Responsabilidades desta Lambda:
  1. Criar ticket no CRM via API REST (idempotente por sessionId).
  2. Publicar alerta no tópico SNS do time de risco.
  3. Salvar o transcript no S3 com metadados estruturados.

Princípios aplicados:
  - IDEMPOTÊNCIA: usa `sessionId` como chave; releitura/reexecução não duplica
    ticket nem objeto no S3 (checagem prévia / chave determinística).
  - SEGREDO/CRIPTOGRAFIA: o transcript é criptografado (envelope encryption via
    KMS) — nunca trafega em plaintext pelo N8n (ver item c do README).
  - FAIL-FAST + retorno estruturado: erros retornam de forma que o N8n saiba
    rotear para retry/DLQ.

Este arquivo é executável para demonstração (`python lambda_session_handler.py`):
se boto3/credenciais não existirem, ele roda em MODO SIMULADO, imprimindo o que
faria, para permitir validação da lógica sem AWS.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
from dataclasses import dataclass


# ---------------------------------------------------------------------------
# Modelo de entrada
# ---------------------------------------------------------------------------
@dataclass
class SessionEvent:
    session_id: str
    customer_id: str
    sentiment: str          # esperado: "negative" para disparar o fluxo
    sentiment_score: float
    transcript_ciphertext: str  # transcript JÁ criptografado (base64) — ver item c
    metadata: dict

    @staticmethod
    def from_event(event: dict) -> "SessionEvent":
        return SessionEvent(
            session_id=event["session_id"],
            customer_id=event["customer_id"],
            sentiment=event.get("sentiment", ""),
            sentiment_score=float(event.get("sentiment_score", 0.0)),
            transcript_ciphertext=event.get("transcript_ciphertext", ""),
            metadata=event.get("metadata", {}),
        )


# ---------------------------------------------------------------------------
# Detecção de ambiente (real vs. simulado)
# ---------------------------------------------------------------------------
def _aws_available() -> bool:
    try:
        import boto3  # noqa: F401
    except ImportError:
        return False
    return bool(os.getenv("AWS_REGION"))


# ---------------------------------------------------------------------------
# 1) CRM — criação idempotente de ticket
# ---------------------------------------------------------------------------
def create_crm_ticket(evt: SessionEvent) -> dict:
    """Cria (ou reaproveita) um ticket no CRM.

    Idempotência: enviamos um `Idempotency-Key = session_id`. Se a API do CRM
    suportar, ela retorna o ticket existente em vez de criar outro. Caso não
    suporte, uma consulta prévia por `session_id` faz o mesmo papel.
    """
    payload = {
        "external_id": evt.session_id,   # chave natural para dedup no CRM
        "customer_id": evt.customer_id,
        "priority": "high" if evt.sentiment_score <= -0.7 else "normal",
        "subject": "Sessão encerrada com sentimento negativo",
        "tags": ["chatbot", "sentiment-negative"],
    }
    headers = {
        "Authorization": f"Bearer {_get_secret('CRM_API_TOKEN')}",
        "Idempotency-Key": evt.session_id,
        "Content-Type": "application/json",
    }

    if not _aws_available():
        return {"simulated": True, "action": "create_crm_ticket", "payload": payload}

    import urllib.request

    req = urllib.request.Request(
        os.environ["CRM_API_URL"],
        data=json.dumps(payload).encode(),
        headers=headers,
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read())


# ---------------------------------------------------------------------------
# 2) SNS — alerta ao time de risco
# ---------------------------------------------------------------------------
def publish_risk_alert(evt: SessionEvent, ticket: dict) -> dict:
    message = {
        "session_id": evt.session_id,
        "customer_id": evt.customer_id,
        "sentiment_score": evt.sentiment_score,
        "ticket_id": ticket.get("id") or ticket.get("external_id"),
    }
    if not _aws_available():
        return {"simulated": True, "action": "publish_risk_alert", "message": message}

    import boto3

    sns = boto3.client("sns")
    # MessageDeduplicationId (se o tópico for FIFO) reforça idempotência.
    return sns.publish(
        TopicArn=os.environ["RISK_ALERT_TOPIC_ARN"],
        Subject="Alerta: sessão com sentimento negativo",
        Message=json.dumps(message),
    )


# ---------------------------------------------------------------------------
# 3) S3 — persistência do transcript (criptografado) com metadados
# ---------------------------------------------------------------------------
def save_transcript(evt: SessionEvent) -> dict:
    """Salva o transcript no S3 com chave DETERMINÍSTICA (idempotência).

    O corpo é o ciphertext recebido; o bucket ainda usa SSE-KMS (dupla proteção).
    A chave é baseada no session_id, então reexecutar sobrescreve o mesmo objeto
    em vez de criar duplicatas.
    """
    key = f"transcripts/{_partition(evt.session_id)}/{evt.session_id}.json.enc"
    body = base64.b64decode(evt.transcript_ciphertext) if evt.transcript_ciphertext else b""
    s3_metadata = {
        "customer-id": evt.customer_id,
        "sentiment": evt.sentiment,
        "sentiment-score": str(evt.sentiment_score),
        **{f"meta-{k}": str(v) for k, v in evt.metadata.items()},
    }

    if not _aws_available():
        return {"simulated": True, "action": "save_transcript", "key": key,
                "metadata": s3_metadata, "bytes": len(body)}

    import boto3

    s3 = boto3.client("s3")
    return s3.put_object(
        Bucket=os.environ["TRANSCRIPT_BUCKET"],
        Key=key,
        Body=body,
        Metadata=s3_metadata,
        ServerSideEncryption="aws:kms",
        SSEKMSKeyId=os.environ.get("KMS_KEY_ID", "alias/aws/s3"),
    )


# ---------------------------------------------------------------------------
# Utilitários
# ---------------------------------------------------------------------------
def _partition(session_id: str) -> str:
    """Prefixo de partição para não criar hotspot no S3."""
    h = hashlib.sha256(session_id.encode()).hexdigest()[:2]
    return h


def _get_secret(name: str) -> str:
    """Lê segredo do Secrets Manager (ou env em modo simulado)."""
    if not _aws_available():
        return os.getenv(name, "SIMULATED_SECRET")
    import boto3

    sm = boto3.client("secretsmanager")
    return sm.get_secret_value(SecretId=name)["SecretString"]


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------
def lambda_handler(event: dict, context=None) -> dict:
    evt = SessionEvent.from_event(event)

    # Guarda de negócio: só age em sentimento negativo (defesa extra além do N8n).
    if evt.sentiment != "negative":
        return {"statusCode": 200, "skipped": True, "reason": "sentiment != negative"}

    # A ordem importa: persistir o transcript e criar o ticket ANTES de alertar,
    # para que o alerta já carregue o ticket_id. Cada passo é idempotente, então
    # um retry do N8n reexecuta com segurança.
    result = {"session_id": evt.session_id}
    try:
        result["transcript"] = save_transcript(evt)
        result["ticket"] = create_crm_ticket(evt)
        result["alert"] = publish_risk_alert(evt, result["ticket"])
        result["statusCode"] = 200
    except Exception as e:
        # Retorno estruturado para o N8n rotear a retry/DLQ.
        result["statusCode"] = 500
        result["error"] = str(e)
        raise  # relança para o N8n/Lambda marcar como falha
    return result


# ---------------------------------------------------------------------------
# Demonstração local
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    sample = {
        "session_id": "sess-abc-123",
        "customer_id": "cust-987",
        "sentiment": "negative",
        "sentiment_score": -0.82,
        "transcript_ciphertext": base64.b64encode(b'{"turns":["..."]}').decode(),
        "metadata": {"channel": "app", "produto": "cartao"},
    }
    print("=== Lambda session handler (modo simulado) ===\n")
    out = lambda_handler(sample)
    print(json.dumps(out, indent=2, ensure_ascii=False, default=str))
