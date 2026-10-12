# Makefile: shortcuts to run the worker locally.
# Requires Go 1.22+ and, for the local queue, Docker or Java.

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

help: ## List available commands
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
	  awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-10s\033[0m %s\n", $$1, $$2}'

deps: ## Download Go dependencies
	go mod tidy

build: ## Build the worker
	go build -o bin/worker .

vet: ## Run go vet
	go vet ./...

up: ## Start the local SQS queue with Docker (recommended)
	docker compose -f local/docker-compose.yml up -d
	@echo "Local SQS at $(ENDPOINT) (UI: http://localhost:9325)"

up-jar: ## Start the local SQS queue with Java (no Docker)
	@test -f $(ELASTICMQ_JAR) || curl -sSL -o $(ELASTICMQ_JAR) \
	  https://s3-eu-west-1.amazonaws.com/softwaremill-public/elasticmq-server-$(ELASTICMQ_VERSION).jar
	java -Dconfig.file=local/elasticmq.conf -jar $(ELASTICMQ_JAR)

down: ## Stop the local SQS queue (Docker)
	docker compose -f local/docker-compose.yml down

seed: ## Send N_MESSAGES test messages (default 25)
	N_MESSAGES=$${N_MESSAGES:-25} go run ./local/producer

run: ## Run the worker (Ctrl+C for a graceful shutdown)
	NUM_WORKERS=$${NUM_WORKERS:-5} WAIT_TIME_SECONDS=$${WAIT_TIME_SECONDS:-1} go run .

demo: ## Full flow: send messages, then run the worker
	@$(MAKE) seed
	@$(MAKE) run

clean: ## Remove binaries
	rm -rf bin
