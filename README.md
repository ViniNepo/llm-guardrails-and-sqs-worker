# Avaliação Técnica — Desenvolvedor Go | Python | Ruby + IA

**Candidato:** Vinicius Nepomuceno
**Vaga:** Desenvolvedor Go | Python | Ruby + IA
**Talent Studio by UDS Tecnologia**

---

## Como este repositório está organizado

O enunciado permite entregar tudo em um único arquivo Markdown, mas optei por
organizar **cada exercício em sua própria pasta**, com o raciocínio técnico
documentado em `README.md` e o **código funcional** em arquivos separados
(`.go`, `.py`, `.json`). A intenção é demonstrar não só a resposta, mas também
que o código realmente compila/roda e que a solução foi pensada como algo que
entraria em produção.

```
teste pratico/
├── README.md                          ← você está aqui (índice + premissas gerais)
├── exercicio-1-worker-sqs/            ← Diagnóstico e correção do worker SQS (Go)
│   ├── README.md
│   ├── go.mod
│   ├── worker_original.go             (código problemático, apenas p/ referência)
│   └── worker_corrigido.go            (versão corrigida e comentada)
├── exercicio-2-finops-contexto/       ← FinOps e otimização de contexto
│   ├── README.md
│   ├── context_optimizer.py           (sliding window + sumarização + orçamento de tokens)
│   └── system_prompt_otimizado.txt    (prompt < 300 tokens)
├── exercicio-3-alucinacao-guardrail/  ← Diagnóstico de alucinação + guardrail
│   ├── README.md
│   └── guardrail_lambda.py            (guardrail de detecção/bloqueio)
└── exercicio-4-n8n-lambda/            ← Design de integração N8n + Lambda
    ├── README.md
    ├── n8n_workflow.json              (esboço do workflow)
    └── lambda_session_handler.py      (lógica de negócio)
```

> **Observação sobre a stack:** a vaga menciona Go, Python e Ruby. Os exercícios
> do enunciado pedem explicitamente Go (Ex. 1) e sugerem Python nas Lambdas
> (Ex. 3 e 4). Segui essas indicações. O Ruby não aparece em nenhum enunciado,
> então não o forcei artificialmente — priorizei resolver bem o que foi pedido.

---

## Índice dos exercícios

| # | Exercício | Foco | Pasta |
|---|-----------|------|-------|
| 1 | Diagnóstico e correção do worker SQS | Concorrência, confiabilidade de mensageria, Go | [`exercicio-1-worker-sqs`](./exercicio-1-worker-sqs/README.md) |
| 2 | FinOps e otimização de contexto | Custo por sessão, engenharia de prompt, observabilidade | [`exercicio-2-finops-contexto`](./exercicio-2-finops-contexto/README.md) |
| 3 | Diagnóstico de alucinação e evolução de prompt | LLMOps, testes A/B, guardrails | [`exercicio-3-alucinacao-guardrail`](./exercicio-3-alucinacao-guardrail/README.md) |
| 4 | Design de integração N8n + Lambda | Arquitetura de integração, resiliência, segurança | [`exercicio-4-n8n-lambda`](./exercicio-4-n8n-lambda/README.md) |

---

## Contexto do desafio (resumo)

Somos o time de engenharia de uma **fintech** que opera um **chatbot de
atendimento baseado em IA generativa** (AWS Bedrock / Claude), lidando com
milhares de sessões diárias sobre faturas, contestações de cobrança e produtos
financeiros. Três problemas críticos estão em aberto:

1. **Degradação de qualidade / alucinação** em perguntas sobre políticas de
   reembolso (tratado no Ex. 3).
2. **Custo crescente por sessão** (+40% no mês) por crescimento descontrolado do
   contexto enviado ao modelo (tratado no Ex. 2).
3. **Fila SQS acumulando mensagens** — worker com lag crescente em picos
   (tratado no Ex. 1).

O Ex. 4 é uma evolução: um novo fluxo automatizado disparado quando uma sessão
encerra com sentimento negativo.

---

## Premissas gerais (tratamento de ambiguidade)

O enunciado pede explicitamente para documentar interpretações quando algo for
ambíguo. As premissas específicas de cada exercício estão no `README.md`
correspondente; as transversais são:

- **Região/serviços AWS:** assumo uma stack AWS típica (SQS, Bedrock, Lambda,
  SNS, S3, CloudWatch) em uma única região, com IAM por least-privilege.
- **Modelo:** "Claude via Bedrock". Onde cito preços/tokens, uso ordens de
  grandeza para raciocínio de trade-off, não valores contratuais exatos.
- **Escala:** "milhares de sessões diárias" → dezenas de mensagens/segundo em
  pico. Suficiente para justificar concorrência e batching, mas não exige
  arquitetura exótica.
- **Ambiente de execução do worker:** ECS Fargate com múltiplas tasks (o Ex. 1
  cita esse cenário explicitamente).

---

## Declaração de uso de IA

Conforme exigido pelo enunciado, **cada exercício contém sua própria seção
`### IA utilizada`** detalhando ferramenta, uso específico e o que foi validado
ou modificado manualmente. Um resumo consolidado:

- **Ferramenta:** Claude (assistente de IA da Anthropic).
- **Onde a IA ajudou (parte significativa):** estruturação do repositório,
  rascunho inicial de todo o código (Go e Python), primeira redação dos `README`
  e organização dos diagnósticos e tabelas.
- **O que fiz / validei manualmente:** compilei e **executei** o código (Go:
  `go build`/`go vet`/`gofmt` + teste ponta a ponta com SQS local; Python:
  execução dos três scripts), **corrigi bugs introduzidos pela IA**, checei cada
  decisão de arquitetura contra a documentação da AWS e ajustei os trade-offs ao
  contexto de fintech.
- **Balanço honesto:** a IA acelerou a escrita e o boilerplate; a corretude, os
  testes de execução e as decisões técnicas foram conferidas e assumidas por mim.
  O detalhamento por exercício está na seção `### IA utilizada` de cada um.
