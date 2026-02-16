.PHONY: up down build migrate test lint format frontend-dev backend-dev e2e

up:
	docker compose up -d

down:
	docker compose down

build:
	docker compose build

migrate:
	docker compose exec backend alembic upgrade head

migrate-local:
	cd backend && alembic upgrade head

revision:
	docker compose exec backend alembic revision --autogenerate -m "$(msg)"

test:
	cd backend && python -m pytest tests/ -v --tb=short

test-cov:
	cd backend && python -m pytest tests/ -v --cov=app --cov-report=term-missing

lint:
	cd backend && ruff check app/
	cd frontend && npm run lint

format:
	cd backend && ruff format app/

backend-dev:
	cd backend && uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

frontend-dev:
	cd frontend && npm run dev

install:
	cd backend && pip install -e ".[dev]"
	cd frontend && npm install

logs:
	docker compose logs -f

e2e:
	cd frontend && npx playwright test

clean:
	docker compose down -v
	rm -rf frontend/.next frontend/node_modules
