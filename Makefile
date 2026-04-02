.PHONY: install run test lint audit migrate scrape analyze newsletter email audit-fs test-newsletter \
        setup-local install-local migrate-local ticker-report

install:
	poetry install
	poetry run playwright install chromium
	poetry run playwright install-deps chromium

migrate:
	poetry run alembic upgrade head

run:
	poetry run python -m src.scheduler

run-once:
	poetry run python -m src.cli run

test:
	poetry run pytest tests/ -v

test-newsletter:
	poetry run python -m src.cli test-newsletter

scrape:
	poetry run python -m src.cli scrape

analyze:
	poetry run python -m src.cli analyze

newsletter:
	poetry run python -m src.cli newsletter

lint:
	poetry run ruff check src/ tests/
	poetry run black --check src/ tests/

format:
	poetry run ruff check --fix src/ tests/
	poetry run black src/ tests/

audit:
	poetry run python -m src.cli audit
	poetry run pip-audit

db-shell:
	psql $$DATABASE_URL

logs:
	tail -f logs/financial_bytes.log

# ── Local dev (no poetry, no postgres) ───────────────────────────
# 1. Edit .env (fill in ANTHROPIC_API_KEY at minimum)
# 2. Run: make setup-local
# 3. Run: make ticker-report TICKER=FIG

setup-local: install-local migrate-local
	@echo ""
	@echo "Local setup complete. Run a ticker report with:"
	@echo "  make ticker-report TICKER=FIG"
	@echo ""

install-local:
	@if [ ! -f .env ]; then \
	  cp .env.local .env; \
	  echo "Created .env from .env.local — edit it and add your ANTHROPIC_API_KEY"; \
	fi
	@if [ ! -d .venv ]; then \
	  python3.10 -m venv .venv; \
	fi
	.venv/bin/pip install --quiet --upgrade pip
	.venv/bin/pip install --quiet \
	  anthropic pydantic "pydantic-settings>=2.3" sqlalchemy alembic \
	  "psycopg2-binary>=2.9" jinja2 click loguru tenacity \
	  python-dotenv httpx "beautifulsoup4>=4.12" lxml selenium \
	  webdriver-manager apscheduler pytz python-dateutil defusedxml \
	  weasyprint markdown
	@echo "Dependencies installed."

migrate-local:
	@mkdir -p logs newsletters
	PYTHONPATH=. .venv/bin/alembic upgrade head
	@echo "Database migrated."

ticker-report:
	PYTHONPATH=. .venv/bin/python -m src.cli --log-level INFO ticker-report $(TICKER) --skip-email 2>/dev/null || \
	PYTHONPATH=. .venv/bin/python -m src.cli --log-level INFO ticker-report $(TICKER)
