# Exercício 4 — Design de Integração N8n + Lambda

> **Premissas assumidas (ambiguidade documentada):**
> - O sentimento negativo já é **detectado pelo modelo** e vem no evento de
>   sessão encerrada (não é responsabilidade deste fluxo classificá-lo).
> - O **N8n** é a camada de orquestração (trigger, roteamento, retry, DLQ,
>   visibilidade); a **lógica de negócio** e o acesso a dados sensíveis ficam na
>   **Lambda** (superfície de segurança menor, mais fácil de auditar e testar).
> - CRM exposto via **API REST**; alertas via **SNS**; transcript em **S3**.

Arquivos desta pasta:

- [`lambda_session_handler.py`](./lambda_session_handler.py) — lógica de negócio
  (CRM + SNS + S3), idempotente e executável em modo simulado.
- [`n8n_workflow.json`](./n8n_workflow.json) — esboço do workflow N8n
  (trigger → filtro de sentimento → Lambda → DLQ em erro). JSON validado.

**Como rodar** (Python 3.10+, sem instalar nada — roda em modo simulado com a
biblioteca padrão):

```bash
python3 lambda_session_handler.py
```

> Para executar de fato contra a AWS (S3/SNS/Secrets), instale o `boto3`:
> `pip install -r requirements.txt` e defina `AWS_REGION` + credenciais.
> Validado: a Lambda roda em modo simulado e o `n8n_workflow.json` é JSON válido.

---

## a) Arquitetura do fluxo — onde fica cada responsabilidade

O princípio de design: **N8n orquestra, Lambda executa**. O N8n é ótimo para
roteamento visual, trigger e política de retry; mas colocar credenciais de CRM,
chave KMS e acesso ao S3 dentro dele aumenta a superfície de ataque e dificulta
teste unitário. Então a lógica sensível é concentrada em **uma** Lambda.

```
┌──────────────────────────────────────────────────────────────────┐
│                              N8N (orquestrador)                    │
│                                                                    │
│  [Trigger: sessão encerrada]                                       │
│   (SQS/EventBridge webhook; payload traz transcript CRIPTOGRAFADO) │
│            │                                                        │
│            ▼                                                        │
│  [IF: sentiment == "negative"?] ──não──► [encerra sem custo]       │
│            │ sim                                                    │
│            ▼                                                        │
│  [Invoke Lambda: session-negative-handler]  (síncrono, retry 3x)   │
│            │                         │                              │
│         sucesso                    erro (após retries)             │
│            │                         ▼                              │
│         [fim]                 [envia p/ SQS DLQ]                    │
└──────────────────────────────────────────────────────────────────┘
                             │  (invoca)
                             ▼
┌──────────────────────────────────────────────────────────────────┐
│                    LAMBDA (lógica de negócio)                      │
│                                                                    │
│  1) save_transcript()  ─► S3 (SSE-KMS) + metadados estruturados    │
│  2) create_crm_ticket()─► CRM REST (Idempotency-Key = session_id)  │
│  3) publish_risk_alert()─► SNS (tópico do time de risco)           │
│                                                                    │
│  Credenciais via Secrets Manager · KMS decrypt · IAM least-priv.   │
└──────────────────────────────────────────────────────────────────┘
```

### Divisão de responsabilidades

| Camada | Responsabilidade | Por quê |
|--------|------------------|---------|
| **N8n** | Trigger, filtro `sentiment==negative`, invocar a Lambda, política de retry, rota de erro → DLQ, visibilidade/observabilidade do fluxo | É a cola/orquestração; muda com frequência e ganha com o editor visual |
| **Lambda** | Os 3 efeitos (S3, CRM, SNS), idempotência, KMS/segredos, validação de negócio | Código versionado e testável; concentra o acesso a dados sensíveis |
| **AWS gerenciado** | S3 (SSE-KMS), SNS, Secrets Manager, KMS, SQS DLQ, CloudWatch | Durabilidade, escala e auditoria sem código |

> **Por que uma Lambda e não três?** Poderíamos ter um nó N8n por efeito, mas
> isso espalharia credenciais e lógica de idempotência pelo N8n e criaria
> **estados parciais** difíceis de reconciliar (ticket criado, mas transcript
> não salvo). Uma Lambda única encapsula a transação lógica, é testável e mantém
> os segredos fora do N8n. Se um dos efeitos precisasse escalar de forma
> independente, aí sim faria sentido quebrar em Step Functions.

**Pseudocódigo da Lambda** (implementação real em `lambda_session_handler.py`):

```python
def lambda_handler(event, ctx):
    evt = SessionEvent.from_event(event)
    if evt.sentiment != "negative":
        return {"skipped": True}          # guarda de negócio (defesa extra)

    save_transcript(evt)                  # 1) S3 + metadados (chave determinística)
    ticket = create_crm_ticket(evt)       # 2) CRM (Idempotency-Key = session_id)
    publish_risk_alert(evt, ticket)       # 3) SNS (com ticket_id)
    return {"statusCode": 200}
```

---

## b) Pontos de falha e como garantir resiliência

Mapeando o fluxo, cada salto é um ponto de falha potencial:

| Ponto de falha | Risco | Mitigação |
|----------------|-------|-----------|
| Trigger perde o evento | Sessão negativa não tratada | Trigger sobre **SQS** (persistente) em vez de webhook fire-and-forget; SQS já dá durabilidade |
| CRM indisponível/timeout | Ticket não criado | **Retry com backoff exponencial** (3x no N8n) + idempotência para não duplicar |
| SNS falha | Time de risco não alertado | Retry; SNS é altamente disponível; em último caso, o evento vai à DLQ |
| S3 falha | Transcript perdido | Retry; S3 tem 11 noves de durabilidade após gravado |
| Lambda falha no meio (ticket ok, S3 não) | **Estado parcial** | **Idempotência por passo** → o retry reexecuta tudo com segurança; ordem (S3 → CRM → SNS) escolhida para o alerta já ter o ticket_id |
| Falha após todos os retries | Evento perdido | **DLQ (SQS)**: o N8n roteia o erro para uma fila de dead-letter para reprocessamento |
| Evento duplicado (retry, redelivery) | Ticket/alerta/objeto duplicados | **Idempotência** (ver abaixo) |

### As três garantias pedidas

**Retries.** No N8n, o nó da Lambda usa `retryOnFail` (3 tentativas,
`waitBetweenTries` com backoff). Internamente, chamadas de rede da Lambda têm
timeout curto para falhar rápido e deixar o retry agir.

**DLQ.** A rota de **erro** do nó Lambda no N8n publica o evento numa **SQS
DLQ** (`session-negative-dlq`). Nada é descartado silenciosamente; um consumidor
ou reprocessamento manual drena a DLQ. (A própria Lambda também pode ter uma
`DeadLetterConfig` nativa como segunda rede de proteção.)

**Idempotência** (o ponto mais importante num fluxo com múltiplos efeitos):
- **CRM:** header `Idempotency-Key = session_id` (e/ou consulta prévia por
  `external_id`) → reexecutar retorna o mesmo ticket, não cria outro.
- **S3:** chave **determinística** `transcripts/<hash>/<session_id>.json.enc` →
  reexecutar **sobrescreve** o mesmo objeto, sem duplicata.
- **SNS:** se o tópico for FIFO, usar `MessageDeduplicationId = session_id`;
  senão, o consumidor do alerta deduplica por `session_id`.

Com isso, um retry do fluxo inteiro é **seguro**: no pior caso, reexecuta os três
efeitos sem gerar duplicidade.

---

## c) Transcript não pode trafegar em plaintext pelo N8n

O requisito de segurança é que o N8n **nunca veja o transcript em claro**. A
solução é **criptografar na origem e descriptografar só onde é estritamente
necessário** — o N8n passa a ser um "carteiro cego".

### Abordagem: envelope encryption com KMS, N8n só repassa ciphertext

1. **Criptografar na origem** (no serviço que encerra a sessão, antes de emitir
   o evento): gerar/usar uma data key do **KMS** e criptografar o transcript.
   O evento carrega apenas o **ciphertext** (base64) — que é o que a Lambda
   recebe no campo `transcript_ciphertext`.
2. **N8n só repassa.** O ciphertext trafega pelo N8n, é logado (se for) como
   bytes ininteligíveis, e é encaminhado à Lambda. O N8n **não tem permissão de
   `kms:Decrypt`**, então mesmo comprometido não abre o conteúdo.
3. **Descriptografia só onde precisa.** Se algum passo precisar do plaintext, é a
   **Lambda** (com IAM `kms:Decrypt` restrito) quem descriptografa, em memória,
   pelo menor tempo possível. No fluxo atual, nem isso é necessário: o transcript
   vai **cifrado** direto para o S3, com **SSE-KMS** por cima (dupla proteção,
   defense-in-depth).

### Reforços

- **N8n sem permissão de decrypt** (IAM least-privilege): a role do N8n só pode
  invocar a Lambda e publicar na DLQ — nada de KMS/S3/Secrets.
- **Segredos fora do N8n:** token do CRM e chave KMS ficam no **Secrets
  Manager**, lidos pela Lambda em runtime — nunca em variáveis do N8n.
- **TLS em todos os saltos** (já é padrão nas APIs AWS/HTTPS) — protege em
  trânsito; a criptografia de aplicação protege **do próprio N8n**.
- **Logs saneados:** garantir que o N8n não faça log do payload em nível DEBUG;
  como o campo já é ciphertext, mesmo um vazamento de log não expõe o conteúdo.

> **Alternativa considerada:** manter o transcript **fora do N8n por completo** —
> a origem grava direto no S3 e o evento carrega só um **ponteiro** (`s3://...`).
> A Lambda lê pelo ponteiro. É ainda mais seguro (o transcript nunca toca o
> N8n), ao custo de uma escrita extra na origem. Se a política de segurança for
> estrita, essa é a opção preferível; deixei a de envelope encryption como
> desenho principal por ser o que o enunciado mais diretamente descreve ("não
> trafegar em plaintext pelo N8n").

---

### IA utilizada

- **Ferramenta:** Claude (assistente de IA da Anthropic).
- **Onde a IA ajudou (parte significativa):** gerou o rascunho do
  `lambda_session_handler.py`, o esboço do `n8n_workflow.json` e a primeira
  versão das tabelas de pontos de falha e resiliência.
- **O que fiz / validei manualmente:**
  - **Executei** a Lambda em modo simulado (Python 3.10) e **validei o JSON** do
    workflow (`json.load`) — ambos rodam sem erro.
  - **Decisões de design minhas:** a **ordem dos efeitos** (S3 → CRM → SNS) para
    o alerta já carregar o `ticket_id`, e a **idempotência por passo**
    (Idempotency-Key no CRM, chave S3 determinística, dedup no SNS).
  - **Refinei a arquitetura:** "uma Lambda em vez de três nós N8n" e o critério
    de quando eu migraria para Step Functions.
- **Resumo honesto do balanço:** a IA acelerou o rascunho de código e diagramas;
  a validação por execução e as decisões de arquitetura, resiliência e segurança
  foram conferidas e assumidas por mim.
