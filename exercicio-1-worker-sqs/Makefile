# Makefile — atalhos para rodar o worker localmente.
# Requer: Go 1.22+, e (para a fila local) Docker OU Java.

SHELL := /bin/bash
ENDPOINT ?= http://localhost:9324
QUEUE_URL ?= http://localhost:9324/000000000000/session-closed
ELASTICMQ_VERSION ?= 1.6.11
ELASTICMQ_JAR ?= /tmp/elasticmq-server-$(ELASTICMQ_VERSION).jar

export AWS_ENDPOINT_URL := $(ENDPOINT)
export QUEUE_URL := $(QUEUE_URL)
export AWS_REGION := us-east-1
export AWS_ACCESS_KEY_ID := local
export AWS_SECRET_ACCESS_KEY := local

.PHONY: help deps build vet up up-jar down seed run demo clean

help: ## Lista os comandos disponíveis
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
	  awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-10s\033[0m %s\n", $$1, $$2}'

deps: ## Baixa as dependências Go
	go mod tidy

build: ## Compila o worker
	go build -o bin/worker .

vet: ## Roda go vet
	go vet ./...

up: ## Sobe a fila SQS local via Docker (recomendado)
	docker compose -f local/docker-compose.yml up -d
	@echo "SQS local em $(ENDPOINT) (UI: http://localhost:9325)"

up-jar: ## Sobe a fila SQS local via Java (sem Docker)
	@test -f $(ELASTICMQ_JAR) || curl -sSL -o $(ELASTICMQ_JAR) \
	  https://s3-eu-west-1.amazonaws.com/softwaremill-public/elasticmq-server-$(ELASTICMQ_VERSION).jar
	java -Dconfig.file=local/elasticmq.conf -jar $(ELASTICMQ_JAR)

down: ## Derruba a fila SQS local (Docker)
	docker compose -f local/docker-compose.yml down

seed: ## Envia N_MESSAGES mensagens de teste (default 25)
	N_MESSAGES=$${N_MESSAGES:-25} go run ./local/producer

run: ## Roda o worker (Ctrl+C para shutdown gracioso)
	NUM_WORKERS=$${NUM_WORKERS:-5} WAIT_TIME_SECONDS=$${WAIT_TIME_SECONDS:-1} go run .

demo: ## Fluxo completo: injeta mensagens e roda o worker
	@$(MAKE) seed
	@$(MAKE) run

clean: ## Remove binários
	rm -rf bin
