.PHONY: setup up down migrate dev-api dev-web test lint typecheck smoke

UV ?= uv
PNPM ?= pnpm

setup: ## Install deps and create .env
	@test -f .env || (cp .env.example .env && sed -i "s|^BETTER_AUTH_SECRET=.*|BETTER_AUTH_SECRET=$$(openssl rand -base64 32)|" .env && echo "created .env")
	$(PNPM) install
	cd apps/api && $(UV) sync

up: ## Start Postgres, Redis, Mailpit
	docker compose up -d --wait postgres redis mailpit

down:
	docker compose down

migrate: ## Apply database migrations
	cd apps/api && $(UV) run alembic upgrade head

dev-api:
	cd apps/api && $(UV) run uvicorn tacit_api.main:app --reload --port 8000

dev-web:
	$(PNPM) --filter web dev

test:
	cd apps/api && $(UV) run pytest -q

lint:
	cd apps/api && $(UV) run ruff check . && $(UV) run ruff format --check .
	$(PNPM) --filter web lint

typecheck:
	$(PNPM) --filter web typecheck
