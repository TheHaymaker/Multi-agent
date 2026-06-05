.PHONY: help install dev up down test lint health sweep serve triage clean

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-12s\033[0m %s\n",$$1,$$2}'

install: ## Install runtime deps
	uv pip install -e .

dev: ## Install runtime + dev deps (pytest, ruff)
	uv pip install -e '.[dev]'

up: ## Start the local stack (postgres, redis, qdrant, langfuse, app)
	docker compose up -d

down: ## Stop the local stack
	docker compose down

test: ## Run the test suite
	pytest -q

lint: ## Lint with ruff
	ruff check overnight_eng tests

health: ## Check config + which secrets/toolsets are wired
	overnight health

sweep: ## Drain the queue, triage, print the morning digest
	overnight sweep

serve: ## Run the ingress + scheduler daemon
	overnight serve

triage: ## Triage one payload, e.g. make triage P=tests/fixtures/sentry_issue_alert.json
	overnight triage $(P)

clean: ## Remove caches
	rm -rf .pytest_cache **/__pycache__ .ruff_cache
