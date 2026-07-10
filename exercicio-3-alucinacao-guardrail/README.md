# Exercício 3 — Diagnóstico de Alucinação e Evolução de Prompt

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

## a) Pelo menos 4 causas técnicas possíveis para a inconsistência

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

## b) Plano de teste A/B para identificar a causa raiz

O objetivo é **isolar uma variável por vez** para atribuir causalidade, em vez de
mudar tudo junto. Estrutura:

### Preparação

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

### Experimentos (isolando uma variável de cada vez)

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

## c) Mecanismo de guardrail para detectar e bloquear alucinações

Implementação executável em [`guardrail_lambda.py`](./guardrail_lambda.py). É um
guardrail de **saída**, invocado de forma **síncrona entre o modelo e o cliente**.

### Abordagem técnica — defesa em camadas

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

### Ação e princípios

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

### Validação (executada)

Rodando `python guardrail_lambda.py`:

```
Resposta A (correta, '60 dias'):   allowed = True   ✓
Resposta B (alucinação, '180 dias'): allowed = False ✓  (fallback + motivo)
✓ Todos os asserts passaram.
```

---

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
