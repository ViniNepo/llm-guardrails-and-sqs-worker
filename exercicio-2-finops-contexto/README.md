# Exercício 2 — FinOps e Otimização de Contexto

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

## Diagnóstico: de onde vem o custo

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

## a) Estratégia para reduzir o custo em ≥ 50% sem degradar qualidade

A ideia central: **parar de enviar tudo sempre** e enviar **só o que é
relevante para o turno atual**. Combino quatro técnicas, cada uma atacando uma
fonte de desperdício. Ordenei por relação custo/benefício.

### 1. RAG para as regras de negócio (maior ganho isolado)

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

### 2. Sliding window no histórico

Manter apenas os **últimos N turnos completos** (ex. N=4). O passado distante
raramente é necessário para responder "qual o prazo de contestação?".

- **Ganho:** corta a maior parte dos ~1.700 tokens de histórico e **elimina o
  crescimento quadrático** (custo por turno passa a ser ~constante).
- **Trade-offs:** perde-se contexto de longo prazo. Se o cliente mencionou algo
  no turno 2 e volta a ele no turno 30, o modelo não vê mais. → mitigado pela
  técnica 3.

### 3. Sumarização progressiva (rolling summary)

Antes de descartar os turnos antigos, condensá-los num **resumo curto**
(1-2 frases com os fatos que importam: identidade, produto, decisões). A
sumarização é feita por um **modelo menor e mais barato** (ex. Claude Haiku),
não pelo modelo principal.

- **Ganho:** preserva memória de longo prazo a uma fração dos tokens.
- **Trade-offs:** custo extra da chamada de sumarização (pequeno, e amortizado —
  resume-se uma vez a cada K turnos, não a cada turno) e risco de o resumo
  perder um detalhe. Mitigar resumindo incrementalmente e mantendo dados
  estruturados (ex. `sessionId`, produto) fora do resumo livre.

### 4. Orçamento de tokens (token budget) + system prompt enxuto

Um teto duro (ex. 1.500 tokens de contexto) que, se estourado, encolhe a janela
automaticamente. E reescrever o system prompt para ser **curto e imperativo**
(item b).

- **Ganho:** previsibilidade de custo e proteção contra regressões.
- **Trade-offs:** cortes agressivos demais degradam qualidade → por isso o teto
  é calibrado por avaliação (item c), não chutado.

### Resultado esperado

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

## b) System prompt otimizado (< 300 tokens)

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

### Justificativa das escolhas

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

## c) Como medir se a redução impactou a qualidade

Reduzir contexto é uma **hipótese**; precisa de medição antes/depois e
monitoramento contínuo. Divido em métricas offline (pré-deploy) e online
(produção), e faço o rollout por **A/B** para atribuição causal.

### Métricas de qualidade (offline / avaliação)

- **Faithfulness / groundedness:** % de respostas 100% suportadas pelo contexto
  (avaliada por um "LLM-as-judge" + amostragem humana). É a métrica-chave: se
  cair, cortamos contexto demais.
- **Answer relevance / correctness:** contra um **golden set** de perguntas com
  respostas corretas conhecidas (inclui casos como "prazo de contestação").
- **Taxa de alucinação:** frequência de prazos/valores/regulamentações inventados
  (cruzável com o guardrail do Exercício 3).
- **RAG recall@k:** o retriever trouxe o chunk certo? Se a qualidade cair, a
  causa costuma ser recall ruim, não a janela.

### Métricas online (CloudWatch / Datadog)

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

### Como operacionalizar

- Emitir métricas customizadas para o **CloudWatch** (namespace `Chatbot/FinOps`
  e `Chatbot/Quality`) com dimensões por `intent` e por `variant` (A/B).
- **Dashboards** no CloudWatch/Datadog cruzando custo × qualidade lado a lado —
  a decisão nunca é só custo.
- **Alarmes** de regressão: ex. faithfulness < X% ou handoff rate > baseline+Y%
  dispara rollback automático da variante.
- **Guardrails de rollout:** promover a otimização só se custo ↓ **e** qualidade
  se mantiver dentro da margem — nunca trocar qualidade por custo cegamente.

---

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
