# Exercício 1 — Problemas encontrados e Perguntas de follow-up

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

## 1. Diagnóstico — todos os problemas do código original

Vou do mais grave (corretude/perda de dados) ao mais operacional (custo/escala).

### 🔴 1.1 Perda silenciosa de mensagens (bug mais grave)

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

### 🔴 1.2 Erro do `ReceiveMessage` ignorado

```go
result, _ := client.ReceiveMessage(...) // ← erro descartado
```

Se a chamada falhar (throttling, rede, credenciais), `result` vem `nil`/vazio,
o `for range result.Messages` não itera, e o `for {}` externo **gira em loop
apertado (busy loop)**, martelando a API de SQS milhares de vezes por segundo.
Isso queima CPU, gera mais throttling e **aumenta o custo de requisições SQS**.

**Correção:** tratar o erro, logar e aplicar **backoff exponencial** antes de
tentar de novo.

### 🟠 1.3 Short polling (sem `WaitTimeSeconds`) — causa direta do lag e do custo

Sem `WaitTimeSeconds`, o SQS usa **short polling**: cada `ReceiveMessage`
retorna imediatamente, muitas vezes **vazio**, mesmo havendo mensagens (o short
polling consulta apenas um subconjunto dos servidores). Resultado:

- muitas chamadas vazias → **custo alto** de requisições;
- latência de entrega maior → **lag**;
- CPU desperdiçada no loop.

**Correção:** **long polling** com `WaitTimeSeconds: 20` (máximo). A chamada
espera até 20s por mensagens, reduz chamadas vazias e entrega assim que chega.

### 🟠 1.4 `MaxNumberOfMessages: 1` — throughput baixo

Buscar **uma mensagem por chamada** significa uma ida à rede por mensagem. Com
milhares de sessões, a fila enche mais rápido do que o worker esvazia.

**Correção:** buscar em **lote de até 10** (`MaxNumberOfMessages: 10`), o máximo
do SQS. Menos chamadas, mais throughput.

### 🟠 1.5 Processamento 100% sequencial — não escala em picos

O laço processa uma mensagem de cada vez. Se `handleMessage` leva, digamos,
200ms (chamada de banco), o teto é ~5 msg/s **por task**, independentemente de
quanto a fila cresce. É a razão central do follow-up "por que o lag cresce em
picos".

**Correção:** **pool de goroutines** (worker pool). Um poller busca lotes e
distribui via canal para N workers concorrentes. O paralelismo desacopla a taxa
de consumo do tempo de uma única chamada de I/O.

### 🟡 1.6 `context.Background()` sem timeout nem cancelamento

- `handleMessage` pode travar indefinidamente (banco lento) e **segurar** o
  worker além do `VisibilityTimeout`, fazendo a mensagem reaparecer e ser
  processada em duplicidade.
- Não há tratamento de **SIGTERM**. No ECS, o deploy manda SIGTERM; sem
  shutdown gracioso, mensagens em andamento são cortadas no meio.

**Correção:** `context.WithTimeout` por mensagem (menor que o
`VisibilityTimeout`) e `signal.NotifyContext` para shutdown gracioso.

### 🟡 1.7 Erro do `DeleteMessage` ignorado + `DeleteMessage` unitário

O retorno do delete é descartado (não sabemos se falhou) e deletar 1 a 1 é mais
caro que `DeleteMessageBatch`.

**Correção:** verificar o erro do delete e usar `DeleteMessageBatch`.

### 🟡 1.8 Ausência de idempotência

Como SQS Standard é *at-least-once*, a mesma mensagem **vai** chegar duplicada
em algum momento (ex.: delete falhou, mas o processamento ocorreu). Se
`handleMessage` não for idempotente, geramos logs/registros duplicados.

**Correção:** `handleMessage` deve deduplicar por chave natural (ex.:
`sessionId`) — UPSERT em vez de INSERT. Documentado no código.

### Resumo em tabela

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

## 2. Perguntas de follow-up

### 2.1 Por que o lag cresce durante picos mesmo com o loop infinito rodando?

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

### 2.2 Como configurar `VisibilityTimeout` e `WaitTimeSeconds`?

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

### 2.3 Worker em ECS com múltiplas tasks — qual problema pode surgir e como mitigar?

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
