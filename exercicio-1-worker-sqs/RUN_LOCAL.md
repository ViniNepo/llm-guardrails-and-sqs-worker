# Como rodar o Worker SQS localmente

Este guia sobe um **SQS local** (ElasticMQ), injeta mensagens de teste e roda o
worker — sem precisar de conta AWS. Tudo foi testado de ponta a ponta.

## Pré-requisitos

- **Go 1.22+** (`go version`)
- Para a fila local, escolha um: **Docker** (opção A) ou **Java 11+** (opção B)

## Estrutura de apoio

```
exercicio-1-worker-sqs/
├── worker_corrigido.go        # o worker (suporta AWS_ENDPOINT_URL p/ modo local)
├── Makefile                   # atalhos: up / seed / run / demo / down
├── .env.example               # variáveis de ambiente de exemplo
└── local/
    ├── elasticmq.conf         # fila "session-closed" + DLQ "session-closed-dlq"
    ├── docker-compose.yml     # SQS local via Docker
    └── producer/producer.go   # envia mensagens de teste
```

---

## Caminho rápido (com Makefile)

```bash
cd exercicio-1-worker-sqs
make deps          # go mod tidy

# 1) Sobe a fila local (escolha um):
make up            # via Docker   (recomendado)
#   ou, em outro terminal, sem Docker:
make up-jar        # via Java (baixa o ElasticMQ e roda em foreground)

# 2) Em outro terminal: injeta mensagens e roda o worker
make seed          # envia 25 mensagens de teste (ajuste com N_MESSAGES=100)
make run           # roda o worker; Ctrl+C faz shutdown gracioso

# 3) Ao terminar:
make down          # derruba a fila (Docker)
```

`make demo` faz `seed` + `run` de uma vez.

---

## Passo a passo manual (sem Makefile)

### Opção A — fila local via Docker

```bash
cd exercicio-1-worker-sqs
docker compose -f local/docker-compose.yml up -d
# SQS em http://localhost:9324  (UI de estatísticas: http://localhost:9325)
```

### Opção B — fila local via Java (sem Docker)

```bash
cd exercicio-1-worker-sqs
curl -sSL -o /tmp/elasticmq.jar \
  https://s3-eu-west-1.amazonaws.com/softwaremill-public/elasticmq-server-1.6.11.jar
java -Dconfig.file=local/elasticmq.conf -jar /tmp/elasticmq.jar
# deixe rodando neste terminal
```

### Configurar o ambiente e rodar (novo terminal)

```bash
cd exercicio-1-worker-sqs
cp .env.example .env
set -a; source .env; set +a      # carrega as variáveis

go run ./local/producer          # envia 25 mensagens (N_MESSAGES p/ mudar)
go run .                         # roda o worker
```

Você verá logs JSON como:

```json
{"level":"INFO","msg":"usando endpoint SQS customizado (modo local)","endpoint":"http://localhost:9324"}
{"level":"INFO","msg":"sessão processada","session_id":"sess-0007","customer_id":"cust-007","sentiment":"positive","closed_at":"2026-07-09T12:43:26Z"}
```

O worker espera um evento JSON (`session_id`, `customer_id`, `closed_at`,
`sentiment`) — o mesmo formato que o `producer` envia. Um body que não seja esse
JSON (ex.: um `"hello world"` puro) é tratado como malformado: retorna erro, não
é apagado e, após `maxReceiveCount` reentregas, vai para a DLQ.

Pressione **Ctrl+C**: o worker para de buscar, drena o que está em andamento e
encerra com `"worker finalizado com sucesso"`.

---

## Testar manualmente e ver as mensagens

A forma mais direta de "ver" as mensagens é deixar o **worker rodando em um
terminal** (os logs aparecem nele) e **enviar mensagens de outro terminal**.

Sugestão com 3 terminais (todos dentro de `exercicio-1-worker-sqs`):

1. **Terminal 1 — fila local:** `make up` (Docker, libera o terminal) ou
   `make up-jar` (Java, ocupa o terminal).
2. **Terminal 2 — worker:** `make run`. É **aqui que você vê as mensagens**
   sendo processadas (log `"msg":"sessão processada"`). Deixe aberto.
3. **Terminal 3 — enviar mensagens:** `make seed` (25 eventos) ou os `curl`
   abaixo. Assim que enviar, olhe o Terminal 2.

### Enviar uma mensagem específica na mão (curl)

Evento JSON **válido** (aparece como `sessão processada` no worker):

```bash
curl "http://localhost:9324/000000000000/session-closed?Action=SendMessage" \
  --data-urlencode 'MessageBody={"session_id":"sess-teste","customer_id":"cust-1","closed_at":"2026-07-09T12:00:00Z","sentiment":"negative"}'
```

Body **inválido** (mostra o caminho de erro → reentrega → DLQ):

```bash
curl "http://localhost:9324/000000000000/session-closed?Action=SendMessage" \
  --data-urlencode 'MessageBody=hello world'
```

### Ver o estado das filas visualmente

Abra **http://localhost:9325** no navegador (UI de estatísticas do ElasticMQ):
mostra quantas mensagens estão em `session-closed` e na `session-closed-dlq`.

### Inspecionar a fila por linha de comando

```bash
# Quantidade aproximada de mensagens na fila principal
curl -s "http://localhost:9324/000000000000/session-closed?Action=GetQueueAttributes&AttributeName.1=ApproximateNumberOfMessages"

# Quantidade na DLQ
curl -s "http://localhost:9324/000000000000/session-closed-dlq?Action=GetQueueAttributes&AttributeName.1=ApproximateNumberOfMessages"
```

---

## O que este setup demonstra do exercício

- **Long polling / batch**: ajuste `WAIT_TIME_SECONDS` e `MAX_MESSAGES` no `.env`.
- **Concorrência**: `NUM_WORKERS` controla o pool de goroutines.
- **Delete só em sucesso + DLQ**: o `handleMessage` de demo sempre tem sucesso;
  se você fizer ele retornar erro, a mensagem é reentregue e, após
  `maxReceiveCount=5` (configurado no `elasticmq.conf`), vai para a
  `session-closed-dlq`.
- **Shutdown gracioso**: o Ctrl+C aciona o caminho de SIGTERM.

### Ver a DLQ em ação (opcional)

Edite `handleMessage` em `worker_corrigido.go` para retornar um erro
(`return fmt.Errorf("falha simulada")`), rode `make seed && make run` e observe,
após algumas reentregas, as mensagens aparecerem em `session-closed-dlq`
(visível na UI em http://localhost:9325).

---

## Rodar contra a AWS real (produção)

Basta **não** definir `AWS_ENDPOINT_URL` e usar o `QUEUE_URL` real:

```bash
unset AWS_ENDPOINT_URL
export QUEUE_URL="https://sqs.us-east-1.amazonaws.com/<conta>/<fila>"
export AWS_REGION="us-east-1"
export WAIT_TIME_SECONDS=20        # long polling recomendado em produção
# credenciais via IAM role (ECS) ou `aws configure`
go run .
```

O mesmo binário serve para local e produção — a única diferença é o
`AWS_ENDPOINT_URL` (vazio = AWS real).

---

## Validação já executada

Este fluxo foi rodado e verificado: **25 mensagens enviadas → 25 processadas →
fila zerada (`ApproximateNumberOfMessages = 0`) → shutdown gracioso concluído**.
Resultados de `go build`, `go vet` e `gofmt`: todos sem erros.
