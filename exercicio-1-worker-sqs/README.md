# Exercício 1 — Diagnóstico e Correção do Worker SQS

> **Premissas assumidas (ambiguidade documentada):**
> - O worker consome uma fila SQS **Standard** (entrega *at-least-once*, sem
>   ordenação garantida). Se fosse FIFO, o enunciado provavelmente mencionaria
>   ordenação/deduplicação — então assumo Standard, que é o caso comum para
>   eventos de "sessão encerrada".
> - Existe (ou deve existir) uma **DLQ com redrive policy** associada à fila.
> - O worker roda em **ECS Fargate** (o próprio enunciado cita esse cenário no
>   3º follow-up).
> - `handleMessage` faz I/O real (banco, enriquecimento, logging), logo pode
>   falhar e pode ser lento.

Arquivos desta pasta:

- **[`problemas_e_follow_up.md`](./problemas_e_follow_up.md) — diagnóstico completo
  dos problemas do código original e respostas às perguntas de follow-up.**
- [`worker_original.go`](./worker_original.go) — código do enunciado, mantido só para referência (não compila no build, tem `//go:build ignore`).
- [`worker_corrigido.go`](./worker_corrigido.go) — versão corrigida, comentada e pronta para rodar.
- [`go.mod`](./go.mod) / [`go.sum`](./go.sum) — dependências (AWS SDK for Go v2).
- **[`RUN_LOCAL.md`](./RUN_LOCAL.md) — como rodar o worker localmente** (SQS local
  via ElasticMQ), passo a passo. Testado de ponta a ponta.
- [`Makefile`](./Makefile) — atalhos (`make up`, `make seed`, `make run`, `make demo`).
- [`.env.example`](./.env.example) — variáveis de ambiente de exemplo.
- [`local/`](./local) — infra de desenvolvimento: `elasticmq.conf` (fila +
  DLQ), `docker-compose.yml` e `producer/` (gerador de mensagens de teste).

> **▶️ Para executar:** siga o [`RUN_LOCAL.md`](./RUN_LOCAL.md). Em resumo:
> `make up` (sobe o SQS local) → `make seed` (injeta mensagens) → `make run`
> (roda o worker). Já validei o fluxo: **25 mensagens enviadas → 25 processadas
> → fila zerada → shutdown gracioso**, com `go build`/`go vet`/`gofmt` limpos.

---

## 1. Diagnóstico e follow-ups

O **diagnóstico completo dos 8 problemas** do código original (com trechos,
impacto e correção de cada um) e as **respostas às 3 perguntas de follow-up**
foram movidos para um documento dedicado:

➡️ **[`problemas_e_follow_up.md`](./problemas_e_follow_up.md)**

Resumo dos problemas encontrados:

| # | Problema | Impacto | Correção |
|---|----------|---------|----------|
| 1.1 | Delete incondicional | **Perda de mensagens** | Deletar só em sucesso |
| 1.2 | Erro de Receive ignorado | Busy loop, custo, throttle | Tratar erro + backoff |
| 1.3 | Short polling | Lag + custo | `WaitTimeSeconds: 20` |
| 1.4 | 1 msg por chamada | Throughput baixo | `MaxNumberOfMessages: 10` |
| 1.5 | Sequencial | Não escala em pico | Worker pool concorrente |
| 1.6 | `context.Background()` | Travas + shutdown abrupto | Timeout + SIGTERM |
| 1.7 | Delete ignorado/unitário | Falhas invisíveis + custo | Checar erro + batch |
| 1.8 | Sem idempotência | Duplicação de dados | Dedup por `sessionId` |

---

## 2. Versão corrigida

O código completo está em [`worker_corrigido.go`](./worker_corrigido.go). Os
pontos-chave da arquitetura:

- **1 poller + N workers**: o poller faz long polling em lote e publica num
  canal; os workers consomem em paralelo.
- **Delete só em sucesso**, com contexto próprio (para não falhar durante o
  shutdown).
- **Backoff exponencial** em erros de `ReceiveMessage`.
- **Timeout por mensagem** (< VisibilityTimeout) e **shutdown gracioso** por
  SIGTERM.
- **Configuração por variáveis de ambiente** (12-factor), fácil de tunar no ECS.

Trecho central (consumo + delete condicional):

```go
func (w *Worker) consume(id int, in <-chan types.Message) {
    for msg := range in {
        msgCtx, cancel := context.WithTimeout(context.Background(), w.cfg.HandleTimeout)
        err := handleMessage(msgCtx, aws.ToString(msg.Body))
        cancel()

        if err != nil {
            // NÃO apaga → SQS reentrega e, após maxReceiveCount, envia à DLQ.
            slog.Error("falha ao processar; será reentregue", "err", err)
            continue
        }

        // Sucesso → apaga (contexto próprio p/ sobreviver ao shutdown).
        delCtx, delCancel := context.WithTimeout(context.Background(), 5*time.Second)
        _, delErr := w.client.DeleteMessageBatch(delCtx, &sqs.DeleteMessageBatchInput{
            QueueUrl: &w.cfg.QueueURL,
            Entries:  []types.DeleteMessageBatchRequestEntry{{Id: msg.MessageId, ReceiptHandle: msg.ReceiptHandle}},
        })
        delCancel()
        if delErr != nil {
            slog.Error("processada mas não apagada; handleMessage precisa ser idempotente", "err", delErr)
        }
    }
}
```

> **Como validei:** compilei e rodei localmente contra um SQS local (ElasticMQ)
> — ver [`RUN_LOCAL.md`](./RUN_LOCAL.md). `go build`, `go vet` e `gofmt` sem
> erros, e o fluxo de ponta a ponta processou todas as mensagens e encerrou de
> forma graciosa.

> As **perguntas de follow-up** (por que o lag cresce em picos; como configurar
> `VisibilityTimeout`/`WaitTimeSeconds`; múltiplas tasks no ECS) estão
> respondidas em [`problemas_e_follow_up.md`](./problemas_e_follow_up.md#2-perguntas-de-follow-up).

---

### IA utilizada

- **Ferramenta:** Claude (assistente de IA da Anthropic).
- **Onde a IA ajudou (parte significativa):** gerou o rascunho inicial do worker
  concorrente em Go (estrutura poller + worker pool, backoff, shutdown gracioso),
  a primeira redação do diagnóstico dos bugs e o esqueleto da infra local
  (ElasticMQ + producer + Makefile).
- **O que fiz / validei manualmente:**
  - **Compilei e rodei de verdade:** `go build`, `go vet` e `gofmt` sem erros, e
    executei o fluxo ponta a ponta contra um SQS local (mensagens enviadas →
    processadas → fila zerada → shutdown gracioso). Não me baseei só no que a IA
    "achava" que funcionava.
  - **Revisei linha a linha** as assinaturas do AWS SDK for Go v2, os imports e a
    remoção do parâmetro `ctx` não usado em `consume`.
  - **Decisões de engenharia minhas**, checadas contra a documentação da AWS:
    dimensionamento de `VisibilityTimeout`, autoscaling por backlog (não por CPU)
    e a exigência de idempotência em `handleMessage` para um worker I/O-bound.
- **Resumo honesto do balanço:** a IA acelerou a escrita (boilerplate e primeira
  versão); a corretude, os testes de execução, as correções de bugs e as
  decisões arquiteturais foram validadas e assumidas por mim.
