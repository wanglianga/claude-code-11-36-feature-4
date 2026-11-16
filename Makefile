PY := .venv/bin/python
PIP := .venv/bin/pip

# Local PostgreSQL used by the dev/demo workflow.
PG_BIN ?= /opt/postgresql-18/bin
DB_NAME ?= cohorthub
DB_TEST ?= cohorthub_test
DB_USER ?= ccdev
DB_PASS ?= ccdev
DB_HOST ?= 127.0.0.1
DB_PORT ?= 5432
URL := postgresql+psycopg://$(DB_USER):$(DB_PASS)@$(DB_HOST):$(DB_PORT)

export COHORT_DATABASE_URL ?= $(URL)/$(DB_NAME)
export COHORT_SUBJECT_SALT ?= dev-only-salt-change-me

.PHONY: venv install install-dev db db-test serve demo demo-lineage test samples openapi docker-up docker-down

venv:
	python3 -m venv .venv

install: venv
	$(PIP) install -q -r requirements.txt

install-dev: install
	$(PIP) install -q -r requirements-dev.txt

db:
	PGPASSWORD=$(DB_PASS) $(PG_BIN)/createdb -h $(DB_HOST) -U $(DB_USER) $(DB_NAME) 2>/dev/null || true

db-test:
	PGPASSWORD=$(DB_PASS) $(PG_BIN)/createdb -h $(DB_HOST) -U $(DB_USER) $(DB_TEST) 2>/dev/null || true

serve: db
	$(PY) -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload

demo: db
	$(PY) -m scripts.seed_demo

demo-lineage: db
	$(PY) -m scripts.seed_lineage_demo

test: db-test
	COHORT_DATABASE_URL=$(URL)/$(DB_TEST) $(PY) -m pytest tests -q

samples:
	$(PY) -m scripts.export_samples

openapi:
	$(PY) -m scripts.dump_openapi

docker-up:
	docker compose up --build -d

docker-down:
	docker compose down
