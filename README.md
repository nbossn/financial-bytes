# Financial Bytes

Automated daily stock portfolio newsletter powered by AI agents. Scrapes financial news from seven sources, analyzes articles per holding using Claude AI, and delivers a 5-minute executive brief to your inbox before market open.

---

## Contents

- [What It Does](#what-it-does)
- [How Data Is Collected and Used](#how-data-is-collected-and-used)
- [Installation](#installation)
- [Configuration](#configuration)
- [Usage](#usage)
- [Examples](#examples)
- [Architecture](#architecture)
- [Agents](#agents)
- [News Sources](#news-sources)
- [Cost Estimate](#cost-estimate)
- [Security](#security)

---

## What It Does

Each morning before NYSE open, Financial Bytes runs an automated pipeline:

1. **Loads your portfolio** from a local CSV file (ticker, shares, cost basis, purchase date)
2. **Scrapes financial news** from Finviz, Yahoo Finance, CNBC, MarketWatch, Morningstar, Reuters, and Seeking Alpha for every holding
3. **Fetches structured signals** from massive.com — analyst ratings, price targets, technical indicators, and Benzinga news sentiment
4. **Runs an Analyst AI agent** (Claude Haiku) per ticker that reads the articles and produces a BUY/HOLD/SELL recommendation, confidence score, sentiment label, key catalysts, key risks, and a narrative context paragraph
5. **Runs a Director AI agent** (Claude Sonnet) that synthesizes all per-stock reports into a portfolio-level brief with a market theme, 5-minute summary, top opportunities, and prioritized action items
6. **Generates a newsletter** in HTML, Markdown, and PDF formats with portfolio P&L, per-stock analysis cards, and an action checklist
7. **Delivers via Gmail SMTP** to your inbox by 7:30 AM ET
8. **Archives the newsletter** to a GitHub repository (optional)

---

## How Data Is Collected and Used

### Data Sources

| Source | What Is Collected | Method |
|--------|------------------|--------|
| **Finviz** | News headline links for each ticker | Selenium (headless Chrome) |
| **Yahoo Finance** | Full article text, video transcript snippets | Playwright (headless Chrome) |
| **CNBC** | Full article text | Playwright (headless Chrome) |
| **MarketWatch** | Pre-paywall snippets and headlines | Playwright (headless Chrome) |
| **Morningstar** | Analysis page text | requests + BeautifulSoup |
| **Reuters** | Full article text | requests + BeautifulSoup |
| **Seeking Alpha** | Free-tier snippets and headlines | requests |
| **massive.com API** | Analyst ratings, price targets, technical indicators, Benzinga news, real-time quotes | REST API (requires API key) |
| **DuckDuckGo** (fallback) | Web search results when <3 articles found | requests (DDGS library) |

### What Is Stored

All scraped data is stored in a local PostgreSQL database (`financial_bytes`):

- **Articles** — headline, URL, source, body text, snippet, published timestamp, scrape timestamp
- **Summaries** — per-ticker AI analysis (sentiment, recommendation, confidence, catalysts, risks)
- **Recommendations** — director-level portfolio synthesis
- **Newsletters** — delivery status (sent/failed), file paths
- **Scrape logs** — per-source success/failure and article counts

**Your portfolio data (tickers, shares, cost basis) is never sent to external services.** It is read from your local `portfolio.csv` file and stored only in your local database.

### What Is Sent to AI APIs

The Analyst agent sends to Anthropic's API:
- Scraped article headlines and body text (publicly available news)
- Your holding details (ticker, shares, cost basis) for context
- No personally identifiable information

The Director agent sends to Anthropic's API:
- All per-ticker analyst reports
- Portfolio-level aggregates (total value, P&L)

**Anthropic's data usage policy applies.** Review it at [anthropic.com/privacy](https://www.anthropic.com/privacy). For maximum privacy, use API access (not the Claude.ai web interface) — API inputs are not used for model training by default.

### What Is Not Collected

- No account credentials are scraped or stored
- No real-time trade execution or brokerage integration
- No personal financial data beyond what you enter in `portfolio.csv`
- The `.env` file and `portfolio.csv` are gitignored and never leave your machine

---

## Installation

### Prerequisites

- Python 3.11+
- [Poetry](https://python-poetry.org/docs/#installation)
- PostgreSQL 14+ (or use SQLite for testing: `DATABASE_URL=sqlite:///financial_bytes.db`)
- Google Chrome (for Playwright/Selenium scrapers)

### 1. Clone

```bash
git clone https://github.com/nbossn/financial-bytes.git
cd financial-bytes
```

### 2. Install Dependencies

```bash
make install
```

This installs all Python dependencies via Poetry and downloads the Chromium browser for Playwright.

### 3. Configure Environment

```bash
cp .env.template .env
chmod 600 .env   # Restrict file permissions
```

Open `.env` and fill in your values (see [Configuration](#configuration) below).

### 4. Create Your Portfolio File

Create `portfolio.csv` in the project root (this file is gitignored):

```csv
ticker,shares,cost_basis,purchase_date
MSFT,100,555.23,2025-08-01
NVDA,200,206.45,2025-11-01
AAPL,50,178.90,2024-03-15
```

| Column | Format | Description |
|--------|--------|-------------|
| `ticker` | Uppercase, 1–5 letters | Stock ticker symbol |
| `shares` | Decimal | Number of shares held |
| `cost_basis` | Decimal (USD) | Average cost per share |
| `purchase_date` | YYYY-MM-DD | Date of purchase (for holding period context) |

### 5. Initialize the Database

```bash
make migrate
```

Creates all tables in your PostgreSQL database.

### 6. Verify Setup

Generate a test newsletter using synthetic data (no API calls, no email):

```bash
make test-newsletter
```

Output is saved to `newsletters/test/`. Open the `.html` file in your browser to preview the newsletter format.

---

## Configuration

All configuration is via the `.env` file. Copy `.env.template` and fill in:

```bash
# === Required ===

# Anthropic Claude API (analyst + director agents)
ANTHROPIC_API_KEY=sk-ant-...

# massive.com API (market signals, analyst ratings, price targets)
MASSIVE_API_KEY=your_key_here
MASSIVE_BASE_URL=https://api.massive.com

# PostgreSQL connection string
DATABASE_URL=postgresql://user:password@localhost:5432/financial_bytes

# Email recipient
EMAIL_RECIPIENT=your@email.com

# Gmail SMTP (use a Gmail App Password, not your account password)
EMAIL_FROM=your@gmail.com
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USER=your@gmail.com
SMTP_PASS=your_16_char_app_password

# Path to your portfolio CSV
PORTFOLIO_CSV_PATH=portfolio.csv

# === Optional ===

# Newsletter schedule (Eastern Time by default)
NEWSLETTER_TIMEZONE=America/New_York
NEWSLETTER_SEND_TIME=07:20
PIPELINE_START_TIME=05:00

# GitHub (archives each newsletter as a commit)
GITHUB_TOKEN=ghp_...
GITHUB_REPO=your-username/financial-bytes

# Scraper tuning
SCRAPER_DELAY_MIN=2        # Min seconds between requests per scraper
SCRAPER_DELAY_MAX=5        # Max seconds between requests per scraper
MAX_ARTICLES_PER_TICKER=15 # Articles fed to analyst agent per ticker
ARTICLE_LOOKBACK_HOURS=24  # How far back to pull articles from DB

# Search fallback (optional, falls back to DuckDuckGo if not set)
# SERPAPI_KEY=your_serpapi_key_here

# Logging
LOG_LEVEL=INFO
LOG_FILE=logs/financial_bytes.log
```

### Gmail App Password Setup

1. Go to your Google Account → Security → 2-Step Verification (must be enabled)
2. Search for "App passwords"
3. Create a new app password for "Mail"
4. Use the 16-character password as `SMTP_PASS`

---

## Usage

### Start the Daily Scheduler (Recommended)

```bash
make run
```

Starts the APScheduler daemon. Runs the full pipeline daily at `PIPELINE_START_TIME` (default 5:00 AM ET) and delivers by `NEWSLETTER_SEND_TIME` (default 7:20 AM ET). Runs in the foreground — use a process manager (systemd, screen, tmux) for persistent background operation.

### Run the Full Pipeline Now

```bash
make run-once
# or
poetry run python -m src.cli run
```

Runs scrape → analyse → newsletter → email immediately using today's date.

### Available Commands

| Command | Description |
|---------|-------------|
| `make run` | Start the APScheduler daemon (daily pipeline) |
| `make run-once` | Run the full pipeline immediately |
| `make test-newsletter` | Generate a test newsletter with synthetic data |
| `make scrape` | Scrape articles only (no AI, no email) |
| `make analyze` | Run analyst agents on cached articles |
| `make newsletter` | Generate newsletter from existing DB data |
| `make audit` | Security scan + cost audit + DB health check |
| `make test` | Run the test suite |
| `make lint` | Check code style (ruff + black) |
| `make format` | Auto-format code |
| `make logs` | Tail the live log file |
| `make migrate` | Run pending database migrations |
| `make db-shell` | Open a psql shell to the database |

### CLI Reference

```bash
# Full pipeline with options
poetry run python -m src.cli run \
  --portfolio path/to/portfolio.csv \
  --date 2025-12-01 \
  --skip-scrape \       # Use cached articles from DB
  --skip-email \        # Generate but don't send
  --output-dir newsletters/custom

# Scrape specific tickers
poetry run python -m src.cli scrape MSFT NVDA AAPL

# Analyze specific tickers (uses cached articles)
poetry run python -m src.cli analyse MSFT NVDA --date 2025-12-01

# Regenerate newsletter from existing DB data
poetry run python -m src.cli newsletter --skip-email --date 2025-12-01

# Run the maintenance audit
poetry run python -m src.cli audit

# Adjust log verbosity
poetry run python -m src.cli --log-level DEBUG run
```

---

## Examples

### Example: First Run

```bash
# 1. Generate a test newsletter (no API calls required)
make test-newsletter
# → newsletters/test/2025-12-01.html

# 2. Scrape articles for your portfolio tickers
poetry run python -m src.cli scrape MSFT NVDA
# → MSFT: 14 article(s) scraped
# → NVDA: 12 article(s) scraped

# 3. Run analysis on cached articles (uses Claude API)
poetry run python -m src.cli analyse MSFT NVDA
# → MSFT: BUY (81%) — Bullish
# → NVDA: HOLD (88%) — Very Bullish

# 4. Generate newsletter without sending email
poetry run python -m src.cli newsletter --skip-email
# → HTML: newsletters/2025-12-01.html
# → MD:   newsletters/2025-12-01.md
# → PDF:  newsletters/2025-12-01.pdf

# 5. Run the full pipeline (scrape + analyze + email)
make run-once
```

### Example: Using a Custom Portfolio File

```bash
poetry run python -m src.cli run --portfolio /path/to/my-portfolio.csv --skip-email
```

### Example: Regenerating a Past Newsletter

```bash
# Use data already in the database for a specific date
poetry run python -m src.cli newsletter --date 2025-11-28 --skip-email
```

### Example: Monitoring Costs

```bash
make audit
# Logs estimated Claude API costs for the last 7 and 30 days
# based on call counts stored in the database
```

### Example Newsletter Output

The generated newsletter includes:

```
Financial Bytes — Dec 01, 2025
─────────────────────────────────────────
5-Minute Summary
Markets are pricing in a softer Fed path following yesterday's CPI print.
Your portfolio is well-positioned: NVDA continues its AI infrastructure
dominance and MSFT is attractively priced for a re-entry.

Portfolio Overview
  Total Value:   $84,520.00
  Cost Basis:    $82,368.00
  P&L:          +$2,152.00 (+2.61%)

┌─ MSFT ─ BUY ─ 81% confidence ─ Bullish ───────────────────────┐
│ Microsoft continues to show strong momentum driven by Azure AI │
│ adoption. Copilot integration across Office suite is seeing    │
│ higher-than-expected uptake.                                   │
│                                                                │
│ Catalysts: Azure AI revenue, Copilot enterprise, earnings beat │
│ Risks: Antitrust scrutiny, OpenAI partnership uncertainty      │
│ Target: $450 | Consensus: Strong Buy                           │
└────────────────────────────────────────────────────────────────┘

Action Items
  ☐ Monitor NVDA earnings call Thursday — Blackwell supply commentary
  ☐ Consider adding to MSFT on any weakness below $330
```

---

## Pipeline Timeline (Daily, ET)

| Time | Stage |
|------|-------|
| 5:00 AM | Orchestrator starts; portfolio loaded; DB checked |
| 5:05–5:30 AM | Web scrapers + massive.com API signals collected |
| 5:30–6:30 AM | Analyst Agent per ticker (Claude Haiku) |
| 6:30–7:00 AM | Director Agent synthesis (Claude Sonnet) |
| 7:00–7:20 AM | Newsletter rendered (HTML + Markdown + PDF) |
| 7:20–7:30 AM | Email delivered + GitHub archive commit |

---

## Architecture

```
portfolio.csv
     │
     ▼
[Orchestrator / main_pipeline.py]
     │
     ├─▶ [Scrapers] ──────────────────────────────────────────────┐
     │   Finviz (Selenium)                                        │
     │   Yahoo Finance, CNBC, MarketWatch (Playwright)            │
     │   Morningstar, Reuters, Seeking Alpha (requests)           │
     │   DuckDuckGo fallback                                      │
     │                                                            │
     ├─▶ [massive.com API] ──────────────────────────────────────┐│
     │   Quotes, analyst ratings, price targets, signals          ││
     │                                                            ││
     │                            Articles + Signals ◀───────────┘│
     │                                                            │
     ├─▶ [Analyst Agent × N tickers] ◀───────────────────────────┘
     │   Model: claude-haiku-4-5
     │   Output: AnalystReport per ticker
     │
     ├─▶ [Director Agent]
     │   Model: claude-sonnet-4-6
     │   Output: DirectorReport (portfolio synthesis)
     │
     ├─▶ [Newsletter Generator]
     │   Output: HTML + Markdown + PDF
     │
     └─▶ [Email Sender]  +  [GitHub Sync]
         Gmail SMTP           Optional archive commit
```

### Key Modules

| Module | Purpose |
|--------|---------|
| `src/config.py` | Pydantic settings from `.env` |
| `src/portfolio/reader.py` | CSV parser, DB persistence |
| `src/scrapers/` | Per-source scrapers + orchestrator |
| `src/api/massive_client.py` | massive.com REST client |
| `src/agents/analyst_agent.py` | Per-ticker Claude Haiku analysis |
| `src/agents/director_agent.py` | Portfolio-level Claude Sonnet synthesis |
| `src/newsletter/generator.py` | Jinja2 HTML/MD rendering |
| `src/delivery/email_sender.py` | Gmail SMTP delivery |
| `src/agents/fullstack_agent.py` | Weekly maintenance audit |
| `src/scheduler.py` | APScheduler daemon |
| `src/cli.py` | Click CLI entry point |

---

## Agents

| Agent | Model | Runs | Purpose |
|-------|-------|------|---------|
| **Analyst** | claude-haiku-4-5 | Daily, per ticker | Article summarization, sentiment scoring, BUY/HOLD/SELL recommendation |
| **Director** | claude-sonnet-4-6 | Daily, once | Portfolio synthesis, market theme, action items |
| **Fullstack** | — (no AI) | Weekly (via `make audit`) | DB health, cost audit, security scan, GitHub sync |

---

## News Sources

| Source | Method | Data Type |
|--------|--------|-----------|
| Finviz | Selenium | News links per ticker |
| Yahoo Finance | Playwright | Full articles |
| CNBC | Playwright | Full articles |
| MarketWatch | Playwright | Pre-paywall snippets |
| Morningstar | requests + BeautifulSoup | Analysis text |
| Reuters | requests + BeautifulSoup | Full articles |
| Seeking Alpha | requests | Free-tier snippets |
| massive.com | REST API | Structured signals, Benzinga news |
| DuckDuckGo | requests (DDGS) | Fallback when <3 articles found |

---

## Cost Estimate

| Component | Model | Est. Daily Cost |
|-----------|-------|----------------|
| Analyst Agent (per ticker) | claude-haiku-4-5 | ~$0.005–0.05 per ticker |
| Director Agent (once) | claude-sonnet-4-6 | ~$0.05–0.20 |
| massive.com API | — | Varies by plan |
| **Total (5-ticker portfolio)** | | **~$0.07–0.45/day** |

Run `make audit` to see actual estimated costs based on your DB call history.

---

## Security

- All secrets in `.env` (gitignored, `chmod 600`)
- `portfolio.csv` gitignored — never leaves your machine
- Ticker symbols validated against `^[A-Z]{1,5}$` before use in URLs
- Output directory validated against path traversal (`..` rejected)
- SSRF protection on web search fallback — private/loopback IPs blocked
- Jinja2 `SandboxedEnvironment` — prevents template injection via AI-generated content
- GitHub token passed via `GIT_ASKPASS` — never embedded in command args or URLs
- SMTP auth errors logged without credential fragments
- Weekly automated security scan via `make audit`

---

## Development

```bash
# Run tests
make test

# Run with debug logging
poetry run python -m src.cli --log-level DEBUG run --skip-email

# Lint and format
make lint
make format

# Add a new migration after changing models
poetry run alembic revision --autogenerate -m "description"
make migrate
```

### Running Tests

```bash
poetry run pytest tests/ -v
```

Tests use SQLite in-memory and mock all external APIs (Claude, SMTP, scrapers). No real credentials or network access required.

---

## License

MIT
