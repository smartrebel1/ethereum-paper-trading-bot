# ETHUSDT Paper Trading Engine — phase 1 convenience targets.
# Every target is a thin wrapper: no hidden magic, same commands as the README.
PY ?= .venv/bin/python
PORT ?= 8000

.PHONY: help install check-env init-db verify-schema ingest replay dashboard-file run test test-unit test-integration lint fmt guard-check clean

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

install: ## Create the venv and install the package with dev extras
	python3 -m venv .venv
	$(PY) -m pip install --upgrade pip
	$(PY) -m pip install -e ".[dev]"

check-env: ## Print effective configuration and run the paper-only safety checks
	$(PY) scripts/check_env.py

init-db: ## Apply migrations (alembic upgrade head)
	$(PY) scripts/init_db.py

verify-schema: ## Detect drift between the database and the ORM metadata
	$(PY) scripts/verify_schema.py

ingest: ## Ingest the Binance archive into the database (idempotent, offline)
	$(PY) scripts/ingest_archive.py

replay: ## Replay the whole stored series through the paper engine
	$(PY) scripts/replay.py

backtest: ## From empty database to a full backtest in one command
	$(PY) scripts/replay.py --rebuild-db --report data/replay_report.json

dashboard-file: ## Render the Arabic dashboard to dashboard/dashboard_ar.html
	$(PY) scripts/render_dashboard.py

run: ## Run the API (Arabic dashboard at http://127.0.0.1:$(PORT)/dashboard)
	$(PY) -m uvicorn app.main:app --host 127.0.0.1 --port $(PORT)

test: ## Run the full test suite
	$(PY) -m pytest

test-unit: ## Run unit tests only
	$(PY) -m pytest tests/unit

test-integration: ## Run integration tests only
	$(PY) -m pytest tests/integration

lint: ## Ruff lint
	$(PY) -m ruff check .

fmt: ## Ruff format
	$(PY) -m ruff format .

guard-check: ## Prove the process refuses to boot in live mode (expects exit code 2)
	@TRADING_MODE=live $(PY) -c "import app.main" ; \
	code=$$? ; \
	if [ $$code -eq 2 ]; then echo "GUARD OK: refused to start (exit 2)"; else echo "GUARD FAILED: exit code $$code"; exit 1; fi

clean: ## Remove caches and the local database
	rm -rf .pytest_cache .ruff_cache .mypy_cache
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
	rm -f data/trading.db data/trading.db-wal data/trading.db-shm
