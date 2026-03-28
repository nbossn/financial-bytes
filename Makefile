.PHONY: install run test lint audit migrate scrape analyze newsletter email audit-fs test-newsletter

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
