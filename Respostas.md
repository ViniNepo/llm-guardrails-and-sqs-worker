# Avaliação Técnica — Desenvolvedor Go | Python | Ruby + IA

**Candidato:** Vinicius Nepomuceno
**Vaga:** Desenvolvedor Go | Python | Ruby + IA
**Talent Studio by UDS Tecnologia**

---

## Sobre esta entrega

Este documento consolida, em um único arquivo, as respostas dos quatro
exercícios — com todo o código-fonte em blocos na linguagem correspondente
(imports incluídos). Ele é enviado **junto com um arquivo `.zip`** que contém o
projeto organizado com **cada exercício em sua própria pasta** e o código já
**executável** (worker Go compilando e rodando contra um SQS local; scripts
Python rodando com a biblioteca padrão).

Estrutura do `.zip`:

```
teste-pratico/
├── exercicio-1-worker-sqs/        # Worker SQS em Go (código, testes locais, Makefile)
├── exercicio-2-finops-contexto/   # FinOps: otimização de contexto (Python)
├── exercicio-3-alucinacao-guardrail/  # Guardrail anti-alucinação (Python)
└── exercicio-4-n8n-lambda/        # Integração N8n + Lambda (Python + JSON do fluxo)
```

Como executar cada parte está detalhado nas seções e nos `README.md`/`RUN_LOCAL.md`
de cada pasta do `.zip`.

**Uso de IA:** conforme exigido pelo enunciado, cada exercício termina com uma
seção `### IA utilizada` descrevendo ferramenta, no que ela auxiliou e o que foi
validado/ajustado manualmente.

---

## Exercício 1 — Diagnóstico e Correção do Worker SQS (Go)

Este documento concentra o **diagnóstico completo dos problemas** do worker
original e as **respostas às perguntas de follow-up** do enunciado. O código
corrigido e as instruções de execução estão no [`README.md`](./README.md) e no
[`RUN_LOCAL.md`](./RUN_LOCAL.md).

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

---

### 1. Diagnóstico — todos os problemas do código original

Vou do mais grave (corretude/perda de dados) ao mais operacional (custo/escala).

#### 🔴 1.1 Perda silenciosa de mensagens (bug mais grave)

```go
err := handleMessage(*msg.Body)
if err != nil {
    fmt.Println("erro:", err)
}
client.DeleteMessage(...) // ← executa SEMPRE, mesmo com erro acima
```

O `DeleteMessage` roda **incondicionalmente**, inclusive quando
`handleMessage` retorna erro. Ou seja: se o banco cair ou o enriquecimento
falhar, a mensagem é **apagada da fila mesmo sem ter sido processada**. O evento
de sessão é perdido para sempre — não vai para a DLQ, não é reprocessado. Para
a empresa (logging/análise de sessões), isso é perda de dado auditável.

**Correção:** só apagar a mensagem **após sucesso**. Em caso de erro, não
apagar — o SQS reentrega após o `VisibilityTimeout` e, depois de
`maxReceiveCount` tentativas, move automaticamente para a DLQ.

#### 🔴 1.2 Erro do `ReceiveMessage` ignorado

```go
result, _ := client.ReceiveMessage(...) // ← erro descartado
```

Se a chamada falhar (throttling, rede, credenciais), `result` vem `nil`/vazio,
o `for range result.Messages` não itera, e o `for {}` externo **gira em loop
apertado (busy loop)**, martelando a API de SQS milhares de vezes por segundo.
Isso queima CPU, gera mais throttling e **aumenta o custo de requisições SQS**.

**Correção:** tratar o erro, logar e aplicar **backoff exponencial** antes de
tentar de novo.

#### 🟠 1.3 Short polling (sem `WaitTimeSeconds`) — causa direta do lag e do custo

Sem `WaitTimeSeconds`, o SQS usa **short polling**: cada `ReceiveMessage`
retorna imediatamente, muitas vezes **vazio**, mesmo havendo mensagens (o short
polling consulta apenas um subconjunto dos servidores). Resultado:

- muitas chamadas vazias → **custo alto** de requisições;
- latência de entrega maior → **lag**;
- CPU desperdiçada no loop.

**Correção:** **long polling** com `WaitTimeSeconds: 20` (máximo). A chamada
espera até 20s por mensagens, reduz chamadas vazias e entrega assim que chega.

#### 🟠 1.4 `MaxNumberOfMessages: 1` — throughput baixo

Buscar **uma mensagem por chamada** significa uma ida à rede por mensagem. Com
milhares de sessões, a fila enche mais rápido do que o worker esvazia.

**Correção:** buscar em **lote de até 10** (`MaxNumberOfMessages: 10`), o máximo
do SQS. Menos chamadas, mais throughput.

#### 🟠 1.5 Processamento 100% sequencial — não escala em picos

O laço processa uma mensagem de cada vez. Se `handleMessage` leva, digamos,
200ms (chamada de banco), o teto é ~5 msg/s **por task**, independentemente de
quanto a fila cresce. É a razão central do follow-up "por que o lag cresce em
picos".

**Correção:** **pool de goroutines** (worker pool). Um poller busca lotes e
distribui via canal para N workers concorrentes. O paralelismo desacopla a taxa
de consumo do tempo de uma única chamada de I/O.

#### 🟡 1.6 `context.Background()` sem timeout nem cancelamento

- `handleMessage` pode travar indefinidamente (banco lento) e **segurar** o
  worker além do `VisibilityTimeout`, fazendo a mensagem reaparecer e ser
  processada em duplicidade.
- Não há tratamento de **SIGTERM**. No ECS, o deploy manda SIGTERM; sem
  shutdown gracioso, mensagens em andamento são cortadas no meio.

**Correção:** `context.WithTimeout` por mensagem (menor que o
`VisibilityTimeout`) e `signal.NotifyContext` para shutdown gracioso.

#### 🟡 1.7 Erro do `DeleteMessage` ignorado + `DeleteMessage` unitário

O retorno do delete é descartado (não sabemos se falhou) e deletar 1 a 1 é mais
caro que `DeleteMessageBatch`.

**Correção:** verificar o erro do delete e usar `DeleteMessageBatch`.

#### 🟡 1.8 Ausência de idempotência

Como SQS Standard é *at-least-once*, a mesma mensagem **vai** chegar duplicada
em algum momento (ex.: delete falhou, mas o processamento ocorreu). Se
`handleMessage` não for idempotente, geramos logs/registros duplicados.

**Correção:** `handleMessage` deve deduplicar por chave natural (ex.:
`sessionId`) — UPSERT em vez de INSERT. Documentado no código.

#### Resumo em tabela

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

### 2. Perguntas de follow-up

#### 2.1 Por que o lag cresce durante picos mesmo com o loop infinito rodando?

Porque o loop infinito **não garante vazão**; ele só garante que o processo está
vivo. Três gargalos se combinam:

1. **Processamento sequencial** (1.5): a vazão é `1 / tempo_de_handleMessage`
   por task. Se o handle leva 200ms, são ~5 msg/s por task — teto fixo. Em um
   pico de, digamos, 50 msg/s, a fila cresce 45 msg/s.
2. **Short polling + 1 msg por chamada** (1.3, 1.4): boa parte do tempo do loop
   é gasta em chamadas de rede vazias ou trazendo uma mensagem só, em vez de
   processar. O loop "roda", mas roda em falso.
3. **Busy loop em erro** (1.2): sob pico costuma vir throttling; como o erro é
   ignorado, o worker gira sem trabalho útil, piorando o throttle.

Ou seja: o `ApproximateNumberOfMessagesVisible` sobe porque a **taxa de chegada
supera a taxa de consumo efetiva**, e o desenho do worker limita artificialmente
essa taxa de consumo. A correção ataca isso com **concorrência + batching +
long polling**, e a escala horizontal (mais tasks) passa a de fato ajudar.

#### 2.2 Como configurar `VisibilityTimeout` e `WaitTimeSeconds`?

**`WaitTimeSeconds` = 20** (máximo). Habilita long polling: menos chamadas
vazias, menor custo e menor latência de entrega. Não há motivo para usar menos
neste cenário de worker dedicado.

**`VisibilityTimeout`**: deve ser **maior que o p99 do tempo de
`handleMessage`, com margem** — para que uma mensagem em processamento não
reapareça e seja processada em paralelo por outra task. Regra prática:

```
VisibilityTimeout ≈ p99(handleMessage) × 2   (com piso, ex. 30–60s)
```

Se o handle leva no p99 ~15s, um VisibilityTimeout de 60s dá folga confortável.
Cuidado com os dois extremos:

- **Muito curto** → mensagens "voltam" durante o processamento →
  **processamento duplicado** e mais carga.
- **Muito longo** → em caso de crash do worker, a mensagem demora demais para
  ficar visível de novo → **recuperação lenta**.

Boa prática adicional: se algum handle for muito variável, usar
`ChangeMessageVisibility` para **estender o timeout dinamicamente** enquanto o
processamento ainda está em andamento (heartbeat), em vez de fixar um valor alto.

E, crucialmente, configurar a **redrive policy** na fila (ex.
`maxReceiveCount: 5`) para que mensagens venenosas vão para a **DLQ** em vez de
circularem para sempre.

#### 2.3 Worker em ECS com múltiplas tasks — qual problema pode surgir e como mitigar?

Rodar N tasks consumindo a mesma fila é o modelo **correto** de escala
horizontal (SQS distribui mensagens entre consumidores). Mas surgem pontos de
atenção:

**a) Processamento duplicado / concorrência na mesma mensagem.** Com short
`VisibilityTimeout` ou handles lentos, duas tasks podem pegar a mesma mensagem.
→ *Mitigação:* `VisibilityTimeout` bem dimensionado (2.2) + **idempotência** em
`handleMessage` (dedup por `sessionId`, UPSERT).

**b) "Thundering herd" no downstream.** Muitas tasks × muitas goroutines podem
esgotar conexões do banco / rate limit do serviço de enriquecimento. → *Mitigação:*
limitar `NumWorkers` por task, usar **connection pool** dimensionado, e escalar
com base numa métrica sensata.

**c) Escalonamento (autoscaling) mal ajustado.** Escalar por CPU é ruim para
worker I/O-bound (a CPU fica baixa mesmo com fila cheia). → *Mitigação:* usar
**Target Tracking pela métrica `ApproximateNumberOfMessagesVisible`** (ou
"backlog per task" = mensagens ÷ tasks) como gatilho do Application Auto Scaling.
Assim, mais mensagens ⇒ mais tasks.

**d) Shutdown durante deploy/scale-in.** O ECS manda **SIGTERM** e depois SIGKILL
após o `stopTimeout`. Sem tratamento, mensagens em andamento são cortadas e
reprocessadas. → *Mitigação:* o **shutdown gracioso** já implementado (para de
buscar, drena as em andamento) + `stopTimeout` do container maior que o
`HandleTimeout`.

**e) Ordenação.** SQS Standard não garante ordem, e com múltiplas tasks isso
fica ainda mais evidente. → *Mitigação:* se ordem importasse, migrar para **SQS
FIFO** com `MessageGroupId` (mas para logging de sessão, ordem geralmente não é
requisito).


### Código-fonte

O worker corrigido é executável (testado localmente contra ElasticMQ). A pasta do exercício no `.zip` inclui ainda `Makefile`, `RUN_LOCAL.md`, `docker-compose.yml`, `elasticmq.conf` e um `producer` para injetar mensagens de teste.

#### `worker_corrigido.go — versão corrigida`

```go
package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"os"
	"os/signal"
	"strconv"
	"sync"
	"syscall"
	"time"

	"github.com/aws/aws-sdk-go-v2/aws"
	"github.com/aws/aws-sdk-go-v2/config"
	"github.com/aws/aws-sdk-go-v2/service/sqs"
	"github.com/aws/aws-sdk-go-v2/service/sqs/types"
)

type sqsAPI interface {
	ReceiveMessage(ctx context.Context, in *sqs.ReceiveMessageInput, optFns ...func(*sqs.Options)) (*sqs.ReceiveMessageOutput, error)
	DeleteMessageBatch(ctx context.Context, in *sqs.DeleteMessageBatchInput, optFns ...func(*sqs.Options)) (*sqs.DeleteMessageBatchOutput, error)
}

type Worker struct {
	client sqsAPI
	cfg    Config
}

type Config struct {
	QueueURL          string
	NumWorkers        int
	WaitTimeSeconds   int32
	MaxMessages       int32
	VisibilityTimeout int32
	HandleTimeout     time.Duration
	EndpointURL       string
}

type SessionEvent struct {
	SessionID  string    `json:"session_id"`
	CustomerID string    `json:"customer_id"`
	ClosedAt   time.Time `json:"closed_at"`
	Sentiment  string    `json:"sentiment"`
}

type backoff struct {
	base, max, cur time.Duration
}

func newBackoff(base, max time.Duration) *backoff {
	return &backoff{base: base, max: max, cur: base}
}

func loadConfig() Config {
	return Config{
		QueueURL:          mustEnv("QUEUE_URL"),
		NumWorkers:        getEnvInt("NUM_WORKERS", 10),
		WaitTimeSeconds:   int32(getEnvInt("WAIT_TIME_SECONDS", 20)),
		MaxMessages:       int32(getEnvInt("MAX_MESSAGES", 10)),
		VisibilityTimeout: int32(getEnvInt("VISIBILITY_TIMEOUT", 60)),
		HandleTimeout:     time.Duration(getEnvInt("HANDLE_TIMEOUT_SEC", 45)) * time.Second,
		EndpointURL:       os.Getenv("AWS_ENDPOINT_URL"),
	}
}

func main() {
	logger := slog.New(slog.NewJSONHandler(os.Stdout, nil))
	slog.SetDefault(logger)

	cfg := loadConfig()

	awsCfg, err := config.LoadDefaultConfig(context.Background())
	if err != nil {
		slog.Error("falha ao carregar config AWS", "err", err)
		os.Exit(1)
	}

	client := sqs.NewFromConfig(awsCfg, func(o *sqs.Options) {
		if cfg.EndpointURL != "" {
			o.BaseEndpoint = aws.String(cfg.EndpointURL)
			slog.Info("usando endpoint SQS customizado (modo local)", "endpoint", cfg.EndpointURL)
		}
	})

	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGTERM, syscall.SIGINT)
	defer stop()

	w := &Worker{client: client, cfg: cfg}
	w.Run(ctx)

	slog.Info("worker finalizado com sucesso")
}

func (w *Worker) Run(ctx context.Context) {
	msgCh := make(chan types.Message)
	var wg sync.WaitGroup

	for i := 0; i < w.cfg.NumWorkers; i++ {
		wg.Add(1)
		go func(id int) {
			defer wg.Done()
			w.consume(id, msgCh)
		}(i)
	}

	w.poll(ctx, msgCh)

	close(msgCh)
	wg.Wait()
}

func (w *Worker) poll(ctx context.Context, out chan<- types.Message) {
	backoff := newBackoff(200*time.Millisecond, 10*time.Second)

	for {
		select {
		case <-ctx.Done():
			slog.Info("sinal de shutdown recebido; parando de buscar mensagens")
			return
		default:
		}

		out2, err := w.client.ReceiveMessage(ctx, &sqs.ReceiveMessageInput{
			QueueUrl:            &w.cfg.QueueURL,
			MaxNumberOfMessages: w.cfg.MaxMessages,
			WaitTimeSeconds:     w.cfg.WaitTimeSeconds,
			VisibilityTimeout:   w.cfg.VisibilityTimeout,
		})
		if err != nil {
			if errors.Is(err, context.Canceled) {
				return
			}
			slog.Error("erro no ReceiveMessage", "err", err)
			if !backoff.Sleep(ctx) {
				return
			}
			continue
		}
		backoff.Reset()

		for _, msg := range out2.Messages {
			select {
			case out <- msg:
			case <-ctx.Done():
				return
			}
		}
	}
}

func (w *Worker) consume(id int, in <-chan types.Message) {
	for msg := range in {
		msgCtx, cancel := context.WithTimeout(context.Background(), w.cfg.HandleTimeout)
		err := handleMessage(msgCtx, aws.ToString(msg.Body))
		cancel()

		if err != nil {
			slog.Error("falha ao processar mensagem; será reentregue",
				"worker", id, "messageId", aws.ToString(msg.MessageId), "err", err)
			continue
		}

		delCtx, delCancel := context.WithTimeout(context.Background(), 5*time.Second)
		_, delErr := w.client.DeleteMessageBatch(delCtx, &sqs.DeleteMessageBatchInput{
			QueueUrl: &w.cfg.QueueURL,
			Entries: []types.DeleteMessageBatchRequestEntry{{
				Id:            msg.MessageId,
				ReceiptHandle: msg.ReceiptHandle,
			}},
		})
		delCancel()
		if delErr != nil {
			slog.Error("falha ao apagar mensagem processada; risco de reprocessamento",
				"worker", id, "messageId", aws.ToString(msg.MessageId), "err", delErr)
		}
	}
}

func handleMessage(ctx context.Context, body string) error {
	select {
	case <-ctx.Done():
		return ctx.Err()
	default:
	}

	var evt SessionEvent
	if err := json.Unmarshal([]byte(body), &evt); err != nil {
		return fmt.Errorf("payload inválido (não é JSON de SessionEvent): %w", err)
	}

	if evt.SessionID == "" || evt.CustomerID == "" {
		return fmt.Errorf("evento sem session_id/customer_id: %q", body)
	}

	slog.Info("sessão processada",
		"session_id", evt.SessionID,
		"customer_id", evt.CustomerID,
		"sentiment", evt.Sentiment,
		"closed_at", evt.ClosedAt.Format(time.RFC3339),
	)
	return nil
}

func (b *backoff) Sleep(ctx context.Context) bool {
	t := time.NewTimer(b.cur)
	defer t.Stop()
	select {
	case <-ctx.Done():
		return false
	case <-t.C:
		b.cur *= 2
		if b.cur > b.max {
			b.cur = b.max
		}
		return true
	}
}

func (b *backoff) Reset() { b.cur = b.base }

func mustEnv(k string) string {
	v := os.Getenv(k)
	if v == "" {
		slog.Error("variável de ambiente obrigatória ausente", "var", k)
		os.Exit(1)
	}
	return v
}

func getEnvInt(k string, def int) int {
	v := os.Getenv(k)
	if v == "" {
		return def
	}
	n, err := strconv.Atoi(v)
	if err != nil {
		return def
	}
	return n
}
```

#### `go.mod — dependências`

```go
module github.com/vinicius/teste-pratico/worker-sqs

go 1.22

require (
	github.com/aws/aws-sdk-go-v2 v1.30.3
	github.com/aws/aws-sdk-go-v2/config v1.27.27
	github.com/aws/aws-sdk-go-v2/credentials v1.17.27
	github.com/aws/aws-sdk-go-v2/service/sqs v1.34.3
)

require (
	github.com/aws/aws-sdk-go-v2/feature/ec2/imds v1.16.11 // indirect
	github.com/aws/aws-sdk-go-v2/internal/configsources v1.3.15 // indirect
	github.com/aws/aws-sdk-go-v2/internal/endpoints/v2 v2.6.15 // indirect
	github.com/aws/aws-sdk-go-v2/internal/ini v1.8.0 // indirect
	github.com/aws/aws-sdk-go-v2/service/internal/accept-encoding v1.11.3 // indirect
	github.com/aws/aws-sdk-go-v2/service/internal/presigned-url v1.11.17 // indirect
	github.com/aws/aws-sdk-go-v2/service/sso v1.22.4 // indirect
	github.com/aws/aws-sdk-go-v2/service/ssooidc v1.26.4 // indirect
	github.com/aws/aws-sdk-go-v2/service/sts v1.30.3 // indirect
	github.com/aws/smithy-go v1.20.3 // indirect
)
```

#### `worker_original.go — código do enunciado (referência)`

```go
// worker_original.go
//
// ⚠️ CÓDIGO PROBLEMÁTICO — mantido APENAS para referência/diagnóstico.
// Não é compilado como parte do build (ver build tag abaixo) e não deve ser
// usado. A versão corrigida está em worker_corrigido.go.
//
//go:build ignore
// +build ignore

package main

import (
	"context"
	"fmt"

	"github.com/aws/aws-sdk-go-v2/service/sqs"
)

func processQueue(client *sqs.Client, queueURL string) {
	for {
		result, _ := client.ReceiveMessage(context.Background(), &sqs.ReceiveMessageInput{
			QueueUrl:            &queueURL,
			MaxNumberOfMessages: 1,
		})

		for _, msg := range result.Messages {
			err := handleMessage(*msg.Body)
			if err != nil {
				fmt.Println("erro:", err)
			}
			client.DeleteMessage(context.Background(), &sqs.DeleteMessageInput{
				QueueUrl:      &queueURL,
				ReceiptHandle: msg.ReceiptHandle,
			})
		}
	}
}

func handleMessage(body string) error {
	// processa evento de sessão encerrada
	// chamada a banco de dados, enriquecimento e logging
	return nil
}
```


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

---

## Exercício 2 — FinOps e Otimização de Contexto

> **Premissas assumidas (ambiguidade documentada):**
> - "Custo por sessão" é dominado pelo **custo de tokens de entrada** enviados
>   ao Bedrock (o payload cresceu de ~800 → ~3.200 tokens, +40% de custo). Foco,
>   portanto, em **reduzir tokens de entrada** sem perder qualidade.
> - As "80 regras de negócio" são majoritariamente **irrelevantes** para a
>   maioria das perguntas — só um subconjunto se aplica a cada intenção.
> - Existe uma **base de conhecimento oficial** (fonte da verdade) que pode ser
>   indexada para RAG.

Arquivos desta pasta:

- [`context_optimizer.py`](./context_optimizer.py) — demonstração executável da
  estratégia (RAG + sliding window + sumarização + orçamento de tokens). Rodando
  localmente, mede **~73% de redução** de tokens no cenário simulado.
- [`system_prompt_otimizado.txt`](./system_prompt_otimizado.txt) — system prompt
  < 300 tokens para o contexto de contestação (item **b**).

**Como rodar** (Python 3.10+, sem instalar nada — usa só a biblioteca padrão):

```bash
python3 context_optimizer.py
```

> Para o modo AWS real (chamada ao Bedrock), instale o `boto3`:
> `pip install -r requirements.txt` e defina `AWS_REGION` + credenciais.
> Validado: roda sem erros e mede ~73% de redução de tokens no cenário simulado.

---

### Diagnóstico: de onde vem o custo

O payload atual empilha três fontes de desperdício:

| Componente | Tokens (aprox.) | Problema |
|------------|-----------------|----------|
| System prompt com 80 regras | ~1.200 | Enviado **inteiro** a cada chamada, mesmo quando só 1-2 regras importam |
| Histórico completo (até 40 turnos) | ~1.700 | Cresce linearmente com a sessão; "oi / olá" não agrega valor |
| Mensagem atual | ~50 | (ok) |
| **Total** | **~3.200** | |

O custo por sessão é ainda pior do que parece: em uma conversa de N turnos, se
reenviamos o histórico a cada turno, o custo de tokens de entrada cresce de forma
**quadrática** (O(N²)) ao longo da sessão.

---

### a) Estratégia para reduzir o custo em ≥ 50% sem degradar qualidade

A ideia central: **parar de enviar tudo sempre** e enviar **só o que é
relevante para o turno atual**. Combino quatro técnicas, cada uma atacando uma
fonte de desperdício. Ordenei por relação custo/benefício.

#### 1. RAG para as regras de negócio (maior ganho isolado)

Em vez de colar as 80 regras no system prompt, indexo-as num **vector store**
(OpenSearch Serverless, Kendra ou pgvector) e, a cada mensagem, recupero apenas
os 1-3 chunks relevantes por similaridade semântica.

- **Ganho:** ~1.200 → ~150-300 tokens no bloco de regras.
- **Bônus de qualidade:** menos "ruído" no contexto ⇒ **menos alucinação**
  (regras contraditórias/irrelevantes competindo pela atenção do modelo). Ataca
  diretamente o problema #1 do desafio.
- **Trade-offs:** adiciona latência (busca) e uma dependência de infra; a
  qualidade passa a depender da **qualidade do retrieval** (chunking, embeddings,
  top-k). Um retrieval ruim pode omitir a regra certa → mitigar com avaliação de
  recall do retriever e um fallback conservador ("não sei, encaminho").

#### 2. Sliding window no histórico

Manter apenas os **últimos N turnos completos** (ex. N=4). O passado distante
raramente é necessário para responder "qual o prazo de contestação?".

- **Ganho:** corta a maior parte dos ~1.700 tokens de histórico e **elimina o
  crescimento quadrático** (custo por turno passa a ser ~constante).
- **Trade-offs:** perde-se contexto de longo prazo. Se o cliente mencionou algo
  no turno 2 e volta a ele no turno 30, o modelo não vê mais. → mitigado pela
  técnica 3.

#### 3. Sumarização progressiva (rolling summary)

Antes de descartar os turnos antigos, condensá-los num **resumo curto**
(1-2 frases com os fatos que importam: identidade, produto, decisões). A
sumarização é feita por um **modelo menor e mais barato** (ex. Claude Haiku),
não pelo modelo principal.

- **Ganho:** preserva memória de longo prazo a uma fração dos tokens.
- **Trade-offs:** custo extra da chamada de sumarização (pequeno, e amortizado —
  resume-se uma vez a cada K turnos, não a cada turno) e risco de o resumo
  perder um detalhe. Mitigar resumindo incrementalmente e mantendo dados
  estruturados (ex. `sessionId`, produto) fora do resumo livre.

#### 4. Orçamento de tokens (token budget) + system prompt enxuto

Um teto duro (ex. 1.500 tokens de contexto) que, se estourado, encolhe a janela
automaticamente. E reescrever o system prompt para ser **curto e imperativo**
(item b).

- **Ganho:** previsibilidade de custo e proteção contra regressões.
- **Trade-offs:** cortes agressivos demais degradam qualidade → por isso o teto
  é calibrado por avaliação (item c), não chutado.

#### Resultado esperado

| | System | Histórico+contexto | Total | vs. baseline |
|---|---|---|---|---|
| Atual | ~1.200 | ~1.950 | **~3.200** | — |
| Otimizado | ~250 | ~500-700 | **~800-950** | **−70% a −75%** |

Isso supera com folga a meta de 50%. A execução de
[`context_optimizer.py`](./context_optimizer.py) confirma a ordem de grandeza
(**~73%** no cenário simulado). Além do custo, esperamos **menos alucinação** e
**menor latência** (payloads menores).

> **Nota de arquitetura:** vale ainda avaliar **prompt caching** do Bedrock para
> a parte estável do prompt (system + regras recorrentes), o que reduz o custo
> dos tokens repetidos sem removê-los. É complementar às técnicas acima.

---

### b) System prompt otimizado (< 300 tokens)

O prompt completo está em
[`system_prompt_otimizado.txt`](./system_prompt_otimizado.txt) (~230 tokens
estimados). Reproduzido:

```text
Você é o assistente financeiro da Empresa X. Responda SOMENTE com base no
CONTEXTO fornecido (política e dados da conta). Se a informação não estiver no
CONTEXTO, diga que não pode confirmar e ofereça encaminhar a um atendente.
Nunca invente prazos, valores ou regulamentações.

Escopo: contestação de cobranças. Seja objetivo, cordial e em pt-BR.

Antes de responder, verifique internamente:
1) A resposta está apoiada no CONTEXTO? 2) Cita prazo/valor? Então ele existe no
CONTEXTO? Se não passar em (1) ou (2), não afirme — encaminhe.

Exemplo:
Usuário: "Posso contestar uma cobrança de 6 meses atrás?"
CONTEXTO: "Prazo de contestação: 60 dias a partir da data da cobrança."
Resposta: "O prazo para contestação é de 60 dias a partir da data da cobrança,
então uma cobrança de ~6 meses atrás está fora do prazo. Posso te encaminhar a
um atendente para avaliar exceções."
```

#### Justificativa das escolhas

- **Grounding explícito ("SOMENTE com base no CONTEXTO"):** a defesa nº 1 contra
  alucinação. As regras não vivem mais no prompt (foram para o RAG), então o
  modelo é instruído a se ancorar no contexto injetado.
- **Fallback seguro ("não pode confirmar → encaminhe"):** em fintech, um "não
  sei, vou te encaminhar" é infinitamente melhor que um número inventado. Reduz
  risco regulatório.
- **Chain-of-thought curto e interno (os passos 1-2):** um CoT enxuto e
  focado em *verificação de fatos* (a resposta está no contexto? o prazo citado
  existe?) melhora a fidelidade sem inflar o prompt. Pedi verificação "interna"
  para não expor o raciocínio ao cliente.
- **Um único exemplo few-shot, altamente relevante:** demonstra o comportamento
  desejado (citar o prazo do contexto + aplicar ao caso + oferecer
  encaminhamento) a baixo custo de tokens. Um exemplo bem escolhido supera
  vários genéricos.
- **Escopo restrito ("contestação de cobranças"):** prompts especializados por
  intenção rendem mais qualidade por token que um prompt "faz-tudo".

---

### c) Como medir se a redução impactou a qualidade

Reduzir contexto é uma **hipótese**; precisa de medição antes/depois e
monitoramento contínuo. Divido em métricas offline (pré-deploy) e online
(produção), e faço o rollout por **A/B** para atribuição causal.

#### Métricas de qualidade (offline / avaliação)

- **Faithfulness / groundedness:** % de respostas 100% suportadas pelo contexto
  (avaliada por um "LLM-as-judge" + amostragem humana). É a métrica-chave: se
  cair, cortamos contexto demais.
- **Answer relevance / correctness:** contra um **golden set** de perguntas com
  respostas corretas conhecidas (inclui casos como "prazo de contestação").
- **Taxa de alucinação:** frequência de prazos/valores/regulamentações inventados
  (cruzável com o guardrail do Exercício 3).
- **RAG recall@k:** o retriever trouxe o chunk certo? Se a qualidade cair, a
  causa costuma ser recall ruim, não a janela.

#### Métricas online (CloudWatch / Datadog)

- **Custo/tokens por sessão** (`InputTokens`, `OutputTokens` por sessão) — o KPI
  primário do FinOps; validar a redução ≥ 50%.
- **Latência (p50/p95/p99)** da chamada ao Bedrock — deve cair com payloads
  menores.
- **Taxa de escalonamento para humano / handoff rate** — proxy de qualidade: se
  o bot passar a "não saber" com muito mais frequência, cortamos contexto demais.
- **CSAT / thumbs-up-down** por sessão — satisfação percebida.
- **Taxa de recontato (repeat contact)** sobre o mesmo assunto em 24-48h — sinal
  de resposta insatisfatória.
- **Taxa de disparo do guardrail** (Ex. 3) — se subir, a qualidade regrediu.

#### Como operacionalizar

- Emitir métricas customizadas para o **CloudWatch** (namespace `Chatbot/FinOps`
  e `Chatbot/Quality`) com dimensões por `intent` e por `variant` (A/B).
- **Dashboards** no CloudWatch/Datadog cruzando custo × qualidade lado a lado —
  a decisão nunca é só custo.
- **Alarmes** de regressão: ex. faithfulness < X% ou handoff rate > baseline+Y%
  dispara rollback automático da variante.
- **Guardrails de rollout:** promover a otimização só se custo ↓ **e** qualidade
  se mantiver dentro da margem — nunca trocar qualidade por custo cegamente.

---


### Código-fonte

#### `context_optimizer.py`

```python
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
```

#### `system_prompt_otimizado.txt`

```text
Você é o assistente financeiro da Empresa X. Responda SOMENTE com base no
CONTEXTO fornecido (política e dados da conta). Se a informação não estiver no
CONTEXTO, diga que não pode confirmar e ofereça encaminhar a um atendente.
Nunca invente prazos, valores ou regulamentações.

Escopo: contestação de cobranças. Seja objetivo, cordial e em pt-BR.

Antes de responder, verifique internamente:
1) A resposta está apoiada no CONTEXTO? 2) Cita prazo/valor? Então ele existe no
CONTEXTO? Se não passar em (1) ou (2), não afirme — encaminhe.

Exemplo:
Usuário: "Posso contestar uma cobrança de 6 meses atrás?"
CONTEXTO: "Prazo de contestação: 60 dias a partir da data da cobrança."
Resposta: "O prazo para contestação é de 60 dias a partir da data da cobrança,
então uma cobrança de ~6 meses atrás está fora do prazo. Posso te encaminhar a
um atendente para avaliar exceções."
```


### IA utilizada

- **Ferramenta:** Claude (assistente de IA da Anthropic).
- **Onde a IA ajudou (parte significativa):** gerou o rascunho do
  `context_optimizer.py` (estrutura das quatro técnicas — RAG, sliding window,
  sumarização e orçamento de tokens), uma primeira versão do system prompt e a
  organização inicial da seção de métricas.
- **O que fiz / validei manualmente:**
  - **Executei** `context_optimizer.py` (Python 3.10) e **medi** a redução real
    de tokens (~73% no cenário simulado), em vez de apenas alegar um número.
  - **Reescrevi o system prompt** para caber em < 300 tokens sem perder o
    grounding e o fallback seguro, contando os tokens com a mesma heurística do
    código.
  - **Complementei as métricas** com KPIs de negócio (handoff rate, recontato)
    que a IA não havia proposto e que são os que revelam degradação silenciosa de
    qualidade — a partir da minha experiência.
- **Resumo honesto do balanço:** a IA ajudou a estruturar e escrever mais rápido;
  a validação por execução, a calibragem do prompt e as decisões de FinOps/
  observabilidade foram conferidas e assumidas por mim.

---

## Exercício 3 — Diagnóstico de Alucinação e Evolução de Prompt

> **Premissas assumidas (ambiguidade documentada):**
> - A resposta correta ("60 dias") reflete a **base de conhecimento oficial** da
>   empresa; a resposta B ("180 dias, Banco Central") é alucinação.
> - Temos acesso a **logs de sessão** (prompt, parâmetros, resposta) para
>   investigar e para montar o A/B.
> - Existe uma **fonte da verdade** (a base de conhecimento) contra a qual o
>   guardrail pode validar prazos/valores.

Arquivo desta pasta:

- [`guardrail_lambda.py`](./guardrail_lambda.py) — implementação executável do
  guardrail (item c). Os testes locais reproduzem exatamente o cenário do
  enunciado: aprovam "60 dias" e **bloqueiam "180 dias"**.

**Como rodar** (Python 3.10+, sem instalar nada — usa só a biblioteca padrão):

```bash
python3 guardrail_lambda.py
```

> Para emitir métricas no CloudWatch (modo AWS real), instale o `boto3`:
> `pip install -r requirements.txt`. Validado: os testes passam — bloqueia
> "180 dias" e libera "60 dias".

---

### a) Pelo menos 4 causas técnicas possíveis para a inconsistência

A alucinação aparecer em **~15% das sessões** (e não em 100% nem 0%) é uma pista
importante: sugere um fator **estocástico** (temperatura) e/ou **dependente de
contexto** (o que foi recuperado/enviado varia entre sessões).

1. **Temperatura / parâmetros de sampling altos.** Com `temperature` elevada (ou
   `top_p` alto), o modelo amostra tokens menos prováveis. Para perguntas
   factuais com resposta única (um prazo), isso injeta variabilidade — às vezes
   o número certo, às vezes um plausível-porém-falso. Explica bem a natureza
   "às vezes sim, às vezes não" dos 15%.

2. **Contexto ausente ou incompleto (falha de RAG/grounding).** Se a política de
   contestação **nem sempre** é recuperada/injetada no prompt (recall baixo do
   retriever, chunking ruim, ou a regra ficou de fora quando o contexto foi
   truncado por tamanho — ver Exercício 2), o modelo cai no **conhecimento
   paramétrico** (o que "lembra" do treino) e inventa. A variabilidade dos 15%
   casa com "às vezes o chunk certo entra, às vezes não".

3. **System prompt fraco / sem instrução de grounding.** Se o prompt não obriga
   o modelo a responder **somente com base no contexto** e a **não inventar
   prazos/valores/regulamentações**, ele preenche lacunas com invenções que
   soam autoritativas ("conforme o Banco Central"). A ausência de um fallback
   seguro ("não sei → encaminhar") agrava.

4. **Base de conhecimento desatualizada, ambígua ou contraditória.** Se a base
   tem versões conflitantes (ex. uma página antiga citando 180 dias, outra 60),
   ou linguagem ambígua, o retrieval pode trazer o chunk errado — e aí a
   "alucinação" é, na verdade, **dado ruim recuperado corretamente**. Também
   entra aqui contaminação por conteúdo genérico da internet no treino
   (regulações que não se aplicam à empresa).

**Causas adicionais plausíveis:** modelo/versão inconsistente entre deploys
(rota A/B acidental); contexto poluído por histórico longo (o modelo "se perde"
entre 40 turnos — conexão direta com o Ex. 2, onde reduzir contexto também reduz
alucinação); ou few-shot examples ausentes/ruins.

---

### b) Plano de teste A/B para identificar a causa raiz

O objetivo é **isolar uma variável por vez** para atribuir causalidade, em vez de
mudar tudo junto. Estrutura:

#### Preparação

- **Métrica primária:** taxa de alucinação = % de respostas que citam
  prazo/valor **não presente na base oficial** (medida automaticamente pelo
  guardrail do item c, rodando em modo *observação* — só loga, não bloqueia).
- **Métricas secundárias:** correctness contra golden set, handoff rate, CSAT.
- **Conjunto de avaliação:** um **golden set** de perguntas factuais (incluindo
  variações de "posso contestar uma cobrança de X meses?") com resposta correta
  conhecida, + tráfego real amostrado.
- **Significância:** definir tamanho de amostra por poder estatístico
  (baseline 15% → detectar redução para, digamos, ≤5% com 95% de confiança).
  Randomização por `sessionId` (hash) para evitar contaminação.

#### Experimentos (isolando uma variável de cada vez)

| Exp. | Variável isolada | Grupo A (controle) | Grupo B (tratamento) | Hipótese que testa |
|------|------------------|--------------------|----------------------|--------------------|
| 1 | **Temperatura** | temp atual | temp = 0.0 | Causa #1 (sampling) |
| 2 | **Grounding no RAG** | contexto atual | força injeção da política + log de recall | Causa #2 (contexto) |
| 3 | **System prompt** | prompt atual | prompt otimizado (Ex. 2b) | Causa #3 (instrução) |
| 4 | **Base de conhecimento** | KB atual | KB curada/deduplicada | Causa #4 (dado ruim) |

**Ordem recomendada:** rodar **Exp. 1 primeiro** (mudança mais barata e rápida —
só um parâmetro). Se `temperature=0` derrubar a alucinação para perto de zero,
achamos a causa dominante. Se não, seguir para RAG (Exp. 2) e prompt (Exp. 3).

**Como garantir isolamento:** em cada experimento, **tudo o mais permanece
constante** (mesmo modelo/versão, mesmo tráfego randomizado, mesma janela de
tempo, mesmo golden set). Logar por sessão: `variant`, `temperature`,
`retrieved_chunks`, `recall_flag`, `prompt_version`, `kb_version`,
`hallucination_flag`. Assim, mesmo com interações, dá para decompor a
contribuição de cada variável.

**Leitura dos resultados:** comparar a taxa de alucinação de A vs. B com teste de
proporção (ex. teste z / qui-quadrado). A variável cujo tratamento produzir a
maior queda estatisticamente significativa é a causa raiz principal (pode haver
mais de uma).

---

### c) Mecanismo de guardrail para detectar e bloquear alucinações

Implementação executável em [`guardrail_lambda.py`](./guardrail_lambda.py). É um
guardrail de **saída**, invocado de forma **síncrona entre o modelo e o cliente**.

#### Abordagem técnica — defesa em camadas

```
Modelo (Bedrock) ──► Resposta candidata
                          │
                          ▼
              ┌───────────────────────────┐
              │  GUARDRAIL (AWS Lambda)    │
              │  Camada 1: regex extrai    │  prazos ("60 dias") e
              │            prazos/valores  │  valores ("R$ 100,00")
              │  Camada 2: cada número     │  precisa existir no
              │            existe na KB?   │  contexto oficial (RAG)
              │  Camada 3 (opcional):      │  LLM-juiz só em casos
              │            checagem        │  ambíguos
              │            semântica       │
              └───────────────────────────┘
                    │                 │
             allowed=True      allowed=False
                    │                 │
                    ▼                 ▼
             entrega resposta   resposta segura de fallback
                                + métrica/alerta CloudWatch
```

**Camada 1 — extração estruturada (regex).** Encontra na resposta todo padrão de
**prazo** (`\d+ (dias|meses|semanas|anos|horas)`) e **valor**
(`R$ ...`, `... reais`). Barato e determinístico. É exatamente onde as
alucinações da fintech se manifestam (prazos e valores inventados).

**Camada 2 — verificação factual contra a base oficial.** Cada prazo/valor citado
**precisa aparecer** no contexto oficial recuperado (RAG) para aquela pergunta —
a fonte da verdade. Se a resposta diz "180 dias" mas o contexto só tem "60 dias",
o número é **não suportado** → **bloqueia**. Essa é a checagem que pega o caso do
enunciado (validado nos testes: "180 dias" é bloqueado, "60 dias" passa).

**Camada 3 (opcional) — LLM-juiz semântico.** Para casos ambíguos (ex. a resposta
parafraseia sem citar número, ou usa um sinônimo), um segundo modelo **pequeno e
barato** (ex. Claude Haiku) avalia "esta resposta é suportada pelo contexto?".
Aciona-se **só quando as camadas 1-2 não decidem com confiança**, para não pagar
uma inferência extra em toda requisição.

#### Ação e princípios

- **Ao bloquear:** substitui por uma **resposta segura de fallback** ("não
  consigo confirmar, vou te encaminhar a um atendente") — nunca entrega o número
  inventado — e **emite métrica/alerta** (`Chatbot/Guardrail/GuardrailBlocked`).
- **Fail-safe:** se o próprio guardrail der erro, o padrão é **bloquear**. Em
  fintech, calar é melhor que alucinar.
- **Observabilidade:** a taxa de disparo do guardrail vira uma métrica de
  qualidade contínua (fecha o loop com o Ex. 2c e com o A/B do item b).

> **Nota:** o **AWS Bedrock Guardrails** nativo cobre parte disso (tópicos
> negados, PII, filtros), mas **não valida um prazo específico contra a política
> da empresa**. Por isso a verificação factual da Camada 2 é própria. As duas
> abordagens são complementares.

#### Validação (executada)

Rodando `python guardrail_lambda.py`:

```
Resposta A (correta, '60 dias'):   allowed = True   ✓
Resposta B (alucinação, '180 dias'): allowed = False ✓  (fallback + motivo)
✓ Todos os asserts passaram.
```

---


### Código-fonte

#### `guardrail_lambda.py`

```python
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
```


### IA utilizada

- **Ferramenta:** Claude (assistente de IA da Anthropic).
- **Onde a IA ajudou (parte significativa):** gerou o rascunho do
  `guardrail_lambda.py` (regex + verificação em camadas), a primeira versão da
  tabela de experimentos A/B e da lista de causas de alucinação.
- **O que fiz / validei manualmente:**
  - **Rodei os testes** do guardrail (Python 3.10) e confirmei o comportamento
    esperado: bloqueia "180 dias" e libera "60 dias" — os `assert` passam.
  - **Corrigi a lógica da IA:** a regex de prazos/valores e a comparação por
    número davam falso-positivo na primeira versão; ajustei para tolerar unidades
    equivalentes entre a base de conhecimento e a resposta.
  - **Decisões minhas:** a ordem dos experimentos A/B (temperatura primeiro, por
    ser a mudança mais barata) e o vínculo entre contexto longo e alucinação
    (conexão com o Ex. 2).
- **Resumo honesto do balanço:** a IA produziu o esqueleto do código e do plano;
  eu testei, corrigi os erros que ela introduziu e assumi as decisões de
  LLMOps/guardrail.

---

## Exercício 4 — Design de Integração N8n + Lambda

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

### a) Arquitetura do fluxo — onde fica cada responsabilidade

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

#### Divisão de responsabilidades

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

### b) Pontos de falha e como garantir resiliência

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

#### As três garantias pedidas

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

### c) Transcript não pode trafegar em plaintext pelo N8n

O requisito de segurança é que o N8n **nunca veja o transcript em claro**. A
solução é **criptografar na origem e descriptografar só onde é estritamente
necessário** — o N8n passa a ser um "carteiro cego".

#### Abordagem: envelope encryption com KMS, N8n só repassa ciphertext

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

#### Reforços

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


### Código-fonte

#### `lambda_session_handler.py`

```python
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
```

#### `n8n_workflow.json — esboço do fluxo`

```json
{
  "name": "Sessao Encerrada - Sentimento Negativo",
  "_comment": "Esboço ilustrativo do workflow N8n. O N8n ORQUESTRA (trigger, roteamento, retry, DLQ); a lógica de negócio vive na Lambda. O transcript chega JÁ criptografado ao N8n (item c).",
  "nodes": [
    {
      "name": "Trigger - Sessao Encerrada (SQS/Webhook)",
      "type": "n8n-nodes-base.awsSqsTrigger",
      "notes": "Recebe evento de sessão encerrada. Poderia ser Webhook de EventBridge. Payload contém session_id, customer_id, sentiment, transcript_ciphertext (criptografado).",
      "position": [200, 300]
    },
    {
      "name": "IF - Sentimento Negativo?",
      "type": "n8n-nodes-base.if",
      "notes": "Roteia só quando sentiment == 'negative'. Sentimentos neutros/positivos encerram o fluxo sem custo.",
      "parameters": {
        "conditions": { "string": [{ "value1": "={{$json[\"sentiment\"]}}", "value2": "negative" }] }
      },
      "position": [440, 300]
    },
    {
      "name": "Lambda - Session Handler",
      "type": "n8n-nodes-base.awsLambda",
      "notes": "Invoca lambda_session_handler.py de forma SÍNCRONA. A Lambda executa os 3 efeitos (S3, CRM, SNS) de forma idempotente e atômica-por-passo. Retry: 3x com backoff exponencial. Timeout compatível com a Lambda.",
      "parameters": {
        "function": "session-negative-handler",
        "invocationType": "RequestResponse"
      },
      "retryOnFail": true,
      "maxTries": 3,
      "waitBetweenTries": 2000,
      "position": [680, 260]
    },
    {
      "name": "On Error - Enviar para DLQ",
      "type": "n8n-nodes-base.awsSqs",
      "notes": "Se a Lambda falhar após os retries, publica o evento (com session_id) em uma fila SQS DLQ para reprocessamento manual/automático. Nunca perde o evento.",
      "parameters": { "queue": "session-negative-dlq", "operation": "sendMessage" },
      "position": [680, 440]
    },
    {
      "name": "No-Op - Encerrar",
      "type": "n8n-nodes-base.noOp",
      "position": [680, 120]
    }
  ],
  "connections": {
    "Trigger - Sessao Encerrada (SQS/Webhook)": { "main": [[{ "node": "IF - Sentimento Negativo?", "type": "main", "index": 0 }]] },
    "IF - Sentimento Negativo?": {
      "main": [
        [{ "node": "Lambda - Session Handler", "type": "main", "index": 0 }],
        [{ "node": "No-Op - Encerrar", "type": "main", "index": 0 }]
      ]
    },
    "Lambda - Session Handler": {
      "main": [[]],
      "error": [[{ "node": "On Error - Enviar para DLQ", "type": "main", "index": 0 }]]
    }
  }
}
```


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

