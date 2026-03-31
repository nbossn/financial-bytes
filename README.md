# Financial Bytes

Automated daily stock portfolio newsletter powered by AI agents. Scrapes financial news from six sources (including Google News RSS), pulls Finviz fundamentals and SEC filings, analyzes articles per holding using Claude AI, and delivers a 5-minute executive brief to your inbox before market open.

Supports portfolios of **15+ tickers** via a parallel scraping, signal-fetching, and AI analysis pipeline. Natively imports Fidelity positions exports, supports multiple named portfolios, tracks per-lot capital gains tax exposure, and incorporates fundamental data (P/E, margins, short interest, ROE) and recent SEC filings into every analyst recommendation.

---

## Contents

- [What It Does](#what-it-does)
- [How Data Is Collected and Used](#how-data-is-collected-and-used)
- [Installation](#installation)
- [Configuration](#configuration)
- [Multi-Portfolio Setup](#multi-portfolio-setup)
- [Per-Lot Tax Tracking](#per-lot-tax-tracking)
- [Parallelism & Performance](#parallelism--performance)
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

1. **Loads your portfolio** from one of four sources: a hand-maintained `portfolio.csv`, a Robinhood transaction export, a Fidelity positions export, or a named portfolio definition in `portfolios.json`
2. **Scrapes financial news** from Finviz, Google News RSS, Yahoo Finance, CNBC, MarketWatch, and Morningstar for every holding — **tickers scraped in parallel**. Also scrapes Finviz for full fundamental data (valuation ratios, margins, ownership, short interest) and recent SEC filings, which are passed directly to the Analyst agent
3. **Fetches structured signals** from massive.com — analyst ratings, price targets, technical indicators, and Benzinga news sentiment — **all 4 endpoint calls per ticker run concurrently, and all tickers run in parallel**
4. **Runs an Analyst AI agent** (Claude Haiku) per ticker — reads articles and produces a BUY/HOLD/SELL recommendation, confidence score, sentiment label, key catalysts, and risk factors — **all tickers analyzed concurrently** (bounded semaphore to respect rate limits)
5. **Runs a Director AI agent** (Claude Sonnet) that synthesizes all per-stock reports into a portfolio-level brief with a market theme, 5-minute summary, top opportunities, and prioritized action items
6. **Generates a newsletter** in HTML, Markdown, and PDF formats with portfolio P&L, a tax efficiency section (per-lot capital gains estimates and tax-loss harvesting candidates), collapsible per-stock analysis cards, and an action checklist
7. **Delivers via Gmail SMTP** to your inbox by 7:30 AM ET
8. **Archives the newsletter** to a GitHub repository (optional)

---

## How Data Is Collected and Used

### Data Sources

| Source | What Is Collected | Method |
|--------|------------------|--------|
| **Finviz** | News headline links, full snapshot fundamentals (P/E, EPS, margins, ROE, short float, etc.), and recent SEC filings | Selenium (headless Chrome) + requests + BeautifulSoup |
| **Google News RSS** | News headlines from hundreds of sources — no auth required | requests + defusedxml |
| **Yahoo Finance** | Full article text | requests + BeautifulSoup |
| **CNBC** | Full article text via Queryly search API | requests + BeautifulSoup |
| **MarketWatch** | Pre-paywall snippets and headlines | requests + BeautifulSoup |
| **Morningstar** | Analysis page text | requests + BeautifulSoup |
| **massive.com API** | Analyst ratings, price targets, technical indicators, Benzinga news, real-time quotes | REST API (requires API key) |
| **DuckDuckGo** (fallback) | Web search results when <3 articles found | requests (DDGS library) |

### What Is Stored

All scraped data is stored in a local PostgreSQL database (`financial_bytes`):

- **Articles** — headline, URL, source, body text, snippet, published timestamp, scrape timestamp
- **Summaries** — per-ticker AI analysis (sentiment, recommendation, confidence, catalysts, risks)
- **Recommendations** — director-level portfolio synthesis
- **Newsletters** — delivery status (sent/failed), file paths
- **Scrape logs** — per-source success/failure and article counts

**Your portfolio data (tickers, shares, cost basis, purchase dates) is never sent to external services.** It is read from local files and stored only in your local database.

### What Is Sent to AI APIs

The Analyst agent sends to Anthropic's API (via `claude -p` subprocess using your Claude Code subscription):
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
- Google Chrome (for Selenium — used by Finviz scraper only; all other scrapers are requests-based)
- [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code) — authenticated with a Claude Code subscription (used for AI agent calls without consuming API credits)

### 1. Clone

```bash
git clone https://github.com/nbossn/financial-bytes.git
cd financial-bytes
```

### 2. Install Dependencies

```bash
make install
```

This installs all Python dependencies via Poetry. Selenium uses your system Chrome install; no separate Playwright browser download is required.

### 3. Configure Environment

```bash
cp .env.template .env
chmod 600 .env   # Restrict file permissions
```

Open `.env` and fill in your values (see [Configuration](#configuration) below).

### 4. Create Your Portfolio File

**Option A — Hand-maintained CSV:**

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

**Option B — Derived from Robinhood transaction export:**

Export your Robinhood activity CSV (`Account > Statements > CSV`) and import it:

```bash
# Preview derived holdings (no files written)
poetry run python -m src.cli import-transactions ~/Downloads/robinhood_activity.csv --dry-run

# Write to portfolio.csv
poetry run python -m src.cli import-transactions ~/Downloads/robinhood_activity.csv

# Or specify a custom output path
poetry run python -m src.cli import-transactions ~/Downloads/robinhood_activity.csv --output my-portfolio.csv
```

The importer parses all `Buy`/`Sell` rows, computes net shares and weighted average cost basis per ticker, and writes a standard `portfolio.csv` ready for `run`.

**Option C — Fidelity positions export (recommended for Fidelity accounts):**

Export your positions from Fidelity (`Accounts & Trade > Portfolio > Download`). The file is named `Portfolio_Positions_<date>.csv`. Point your portfolio definition at it in `portfolios.json` (see [Multi-Portfolio Setup](#multi-portfolio-setup)):

```json
{
  "name": "my-fidelity",
  "label": "My Fidelity Account",
  "fidelity_positions": "/path/to/Portfolio_Positions_Mar-28-2026.csv",
  "email_recipients": ["you@example.com"]
}
```

Money market funds (SPAXX, FZDXX, FZAXX) are detected automatically and handled at $1.00 NAV — no manual adjustment needed. Run the pipeline with:

```bash
poetry run python -m src.cli run --portfolio-name my-fidelity --skip-email
```

**Option D — Multi-portfolio via `portfolios.json`:**

For multiple accounts or named portfolios, define them all in `portfolios.json` at the project root (see [Multi-Portfolio Setup](#multi-portfolio-setup)). Each portfolio can have its own data source, email recipients, and purchase history file.

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

# Anthropic Claude API (used by claude CLI for agent calls)
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

# Parallelism (see "Parallelism & Performance" section)
MAX_PARALLEL_TICKERS=3     # Parallel Selenium browser instances for scraping
MAX_PARALLEL_ANALYSTS=5    # Concurrent claude -p (Haiku) calls for analysis

# Search fallback (optional, falls back to DuckDuckGo if not set)
# SERPAPI_KEY=your_serpapi_key_here

# CNBC / Queryly search API (public key provided; override if requests get blocked)
QUERYLY_API_KEY=31a35d40a9a64ab3

# Claude agent permissions (false = require manual approval in claude CLI; true = auto-approve)
CLAUDE_SKIP_PERMISSIONS=true

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

## Multi-Portfolio Setup

Financial Bytes supports multiple named portfolios via `portfolios.json` at the project root. Each entry can use a different data source, email recipients, and purchase history file.

### `portfolios.json` Format

```json
[
  {
    "name": "default",
    "label": "My Portfolio",
    "csv_path": "portfolio.csv",
    "email_recipients": ["you@example.com"]
  },
  {
    "name": "trust",
    "label": "Family Trust",
    "fidelity_positions": "/path/to/Portfolio_Positions_Mar-28-2026.csv",
    "purchase_history": "data/trust_purchase_history.json",
    "email_recipients": ["trustee@example.com"]
  },
  {
    "name": "robinhood",
    "label": "Robinhood Account",
    "transactions_path": "/path/to/robinhood_activity.csv",
    "email_recipients": []
  }
]
```

| Field | Required | Description |
|-------|----------|-------------|
| `name` | Yes | Identifier used with `--portfolio-name` CLI flag |
| `label` | No | Human-readable name shown in newsletter header |
| `csv_path` | One of these | Path to a hand-maintained `portfolio.csv` |
| `fidelity_positions` | One of these | Path to a Fidelity positions export CSV |
| `transactions_path` | One of these | Path to a Robinhood transactions CSV |
| `email_recipients` | No | Override the default `EMAIL_RECIPIENT` from `.env` |
| `purchase_history` | No | Path to a per-lot purchase history JSON (see below) |

### Running a Specific Portfolio

```bash
# Run the pipeline for a named portfolio
poetry run python -m src.cli run --portfolio-name trust --skip-email

# List all configured portfolios
poetry run python -m src.cli portfolios
```

---

## Per-Lot Tax Tracking

Financial Bytes can track your holdings at the individual tax lot level, giving you accurate short-term vs. long-term capital gains classification and flagging tax-loss harvesting opportunities in the newsletter.

### Why It Matters

When you buy the same stock at different times and prices, each purchase is a separate tax lot. The IRS taxes gains on lots held **less than one year** as ordinary income (22–37%), while lots held **one year or more** qualify for the lower long-term capital gains rate (15–20%). Mixing these up can significantly overstate or understate your tax exposure.

### Tax Efficiency Section in the Newsletter

The newsletter includes a **Tax Efficiency** section before the Analyst Ratings Desk that shows:

- Per-lot unrealized gains/losses with holding period labels (Short-Term / Long-Term)
- Estimated tax range at your bracket (e.g., `$12,400–$18,500`)
- **Tax-loss harvesting candidates** — positions with unrealized losses you could realize to offset gains

### Setting Up Purchase History

Create a JSON file at `data/<portfolio-name>_purchase_history.json` and reference it in `portfolios.json` via the `purchase_history` field:

```json
{
  "_comment": "Keys starting with _ are ignored. Empty arrays are skipped.",
  "MSFT": [
    {"shares": null, "cost_basis": 296.20, "purchase_date": "2021-08-01"}
  ],
  "NVDA": [
    {"shares": 1000, "cost_basis": 143.35, "purchase_date": "2025-06-16"},
    {"shares": 4000, "cost_basis": 44.50, "purchase_date": "2023-06-15"}
  ],
  "AAPL": [
    {"shares": 50, "cost_basis": 178.90, "purchase_date": "2024-03-15"}
  ],
  "SPAXX": []
}
```

| Field | Type | Description |
|-------|------|-------------|
| `shares` | number or `null` | Shares in this lot. Use `null` to assign all remaining shares (useful for single-lot holdings) |
| `cost_basis` | number | Per-share cost basis for this lot |
| `purchase_date` | YYYY-MM-DD | Date of purchase — determines short vs. long-term classification |

**Tips:**
- A ticker can have multiple lot entries — list them in any order
- Tickers omitted from this file fall back to the aggregated holding's purchase date from the portfolio CSV
- Set a ticker to `[]` (empty array) to explicitly skip it from tax calculations (useful for money markets like SPAXX)
- Fidelity exports don't include individual lot dates — use this file to supply accurate dates from your brokerage statements

The earliest lot date for each ticker is also passed to the Analyst agent, so it can provide accurate holding period context in its analysis.

---

## Parallelism & Performance

Financial Bytes uses four layers of parallelism to handle 15+ ticker portfolios in reasonable wall-clock time. Each layer is independently tunable.

### Overview

| Pipeline Phase | Concurrency Strategy | Default | Config Key |
|---------------|---------------------|---------|------------|
| Phase 2 — Scraping | `ThreadPoolExecutor` across tickers | 3 workers | `MAX_PARALLEL_TICKERS` |
| Phase 3 — Market signals | `ThreadPoolExecutor` across tickers (up to 10) + inner `ThreadPoolExecutor` of 4 per ticker | auto | (hardcoded, see below) |
| Phase 4 — Analyst agents | `asyncio.gather` with `asyncio.Semaphore` | 5 concurrent | `MAX_PARALLEL_ANALYSTS` |
| Phase 5 — Director agent | Sequential (single call) | n/a | n/a |

### Phase 2: Parallel Scraping

**File:** `src/scrapers/scraper_orchestrator.py` — `scrape_tickers_parallel()`
**Called from:** `src/pipeline/main_pipeline.py` — Phase 2

Each ticker is scraped by a pool of up to `MAX_PARALLEL_TICKERS` worker threads. Within each ticker, Finviz (Selenium) runs first to collect news links, full fundamentals, and SEC filings; then the remaining scrapers (Google News, Yahoo, CNBC, MarketWatch, Morningstar — all requests-based) run in their own inner pool of 3.

```
MAX_PARALLEL_TICKERS=3   # hard ceiling; each worker spawns a Chrome instance
                          # Values above 3 risk RAM exhaustion on a typical VPS
                          # (each Selenium instance uses ~200-400 MB)
```

**Where to change:** `.env` → `MAX_PARALLEL_TICKERS`
**Code location:** `src/config.py:45` (`max_parallel_tickers` field)

### Phase 3: Parallel Market Signals

**File:** `src/api/endpoints.py` — `MassiveEndpoints.get_ticker_signals()`
**Orchestrator:** `src/pipeline/main_pipeline.py` — `_fetch_signals_for_ticker()`, Phase 3

Two layers of parallelism:

1. **Across tickers** — `ThreadPoolExecutor(min(len(holdings), 10))` in `main_pipeline.py`. Each worker creates its own `MassiveClient` instance (thread-safe by design — no shared state).
2. **Within each ticker** — quote, news, analyst ratings, and technicals are 4 separate HTTP calls dispatched to a `ThreadPoolExecutor(max_workers=4)` inside `get_ticker_signals()`.

Combined effect: 15 tickers × 4 HTTP calls = 60 requests, all in-flight concurrently (bounded at 10 outer workers × 4 inner workers).

**Where to change:** The outer worker count is calculated dynamically as `min(len(holdings), 10)` in `main_pipeline.py:119`. Increase or decrease the cap there if needed.
**Code location:** `src/pipeline/main_pipeline.py:119`, `src/api/endpoints.py:169`

### Phase 4: Concurrent Analyst Agents

**File:** `src/agents/analyst_agent.py` — `run_analysts_parallel()`, `_call_claude_async()`
**Called from:** `src/pipeline/main_pipeline.py` — Phase 4

All analyst calls run concurrently via `asyncio.gather`. A `asyncio.Semaphore(MAX_PARALLEL_ANALYSTS)` caps how many `claude -p` subprocesses are active at once to prevent hitting Claude Code rate limits.

Each analyst call:
- Spawns `claude -p <prompt> --model claude-haiku-4-5-20251001 --dangerously-skip-permissions`
- Input: ~2,000–5,000 tokens (articles + holding data)
- Output: ~500–800 tokens (JSON analysis)
- Typical duration: 8–15 seconds

#### Tuning MAX_PARALLEL_ANALYSTS for your Claude Code plan

| Plan | Safe range | Notes |
|------|-----------|-------|
| Claude Code Pro | 3–5 | Standard Pro is limited; start at 3, increase if no 429s |
| Claude Code Max | 5–8 | Higher limits; 5 is the default sweet-spot |
| Claude Code Enterprise | 8+ | Consult your org's rate limit allocation |

Rate-limit errors (`429`, `529`, `overloaded`) are detected in stderr and trigger an extended backoff (3× longer than normal errors, up to 60 seconds between retries, with 5 total attempts).

**Where to change:** `.env` → `MAX_PARALLEL_ANALYSTS`
**Code location:** `src/config.py:46` (`max_parallel_analysts` field), `src/agents/analyst_agent.py:251` (semaphore)

### Wall-Clock Time Estimates (15 tickers)

| Phase | Sequential | Parallel (defaults) |
|-------|-----------|---------------------|
| Phase 2 — Scrape | ~45 min | ~8 min (3 workers; most scrapers now requests-based) |
| Phase 3 — Signals | ~90 s | ~10 s (10 outer × 4 inner) |
| Phase 4 — Analysts | ~150 s | ~35 s (5 concurrent Haiku) |
| Phase 5 — Director | ~20 s | ~20 s (single Sonnet call) |
| **Total** | **~50 min** | **~20 min** |

### SQLite Concurrent Write Safety

If you use SQLite (`DATABASE_URL=sqlite:///...`) instead of PostgreSQL, the session layer automatically enables WAL mode and a 10-second busy timeout so multi-threaded writes from parallel scrapers and agents don't deadlock.

**Code location:** `src/db/session.py:20–30`

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

### Multi-Portfolio Commands

```bash
# List all portfolios defined in portfolios.json
poetry run python -m src.cli portfolios

# Run a specific named portfolio
poetry run python -m src.cli run --portfolio-name trust
poetry run python -m src.cli run --portfolio-name trust --skip-email

# Import a Fidelity positions file (one-shot preview)
poetry run python -m src.cli fidelity-import /path/to/Portfolio_Positions_Mar-28-2026.csv --dry-run
```

### CLI Reference

```bash
# Full pipeline — hand-maintained portfolio CSV
poetry run python -m src.cli run \
  --portfolio path/to/portfolio.csv \
  --date 2025-12-01 \
  --skip-scrape \       # Use cached articles from DB
  --skip-email \        # Generate but don't send
  --output-dir newsletters/custom

# Full pipeline — named portfolio from portfolios.json
poetry run python -m src.cli run --portfolio-name trust --skip-email

# Full pipeline — Fidelity positions export (via portfolios.json definition)
poetry run python -m src.cli run --portfolio-name my-fidelity --skip-email

# Full pipeline — derive portfolio from Robinhood transaction CSV
poetry run python -m src.cli run \
  --transactions ~/Downloads/robinhood_activity.csv \
  --skip-email

# List all configured portfolios
poetry run python -m src.cli portfolios

# Import a Fidelity positions file (preview)
poetry run python -m src.cli fidelity-import /path/to/Portfolio_Positions_Mar-28-2026.csv --dry-run

# Import transactions and write portfolio.csv (one-time setup or refresh)
poetry run python -m src.cli import-transactions ~/Downloads/robinhood_activity.csv
poetry run python -m src.cli import-transactions ~/Downloads/robinhood_activity.csv --dry-run
poetry run python -m src.cli import-transactions ~/Downloads/robinhood_activity.csv --output my-portfolio.csv

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

# 3. Run analysis on cached articles (uses Claude via claude -p)
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

### Example: Import from Robinhood

```bash
# Preview what will be derived (no files written)
poetry run python -m src.cli import-transactions ~/Downloads/robinhood_activity.csv --dry-run
# → Derived 8 open holdings:
#
#   TICKER         SHARES     AVG COST     TOTAL COST  FIRST BUY
#   ──────────────────────────────────────────────────────────────
#   MSFT          100.0000     555.2300     55,523.00  2025-08-01
#   NVDA          200.0000     206.4500     41,290.00  2025-11-05
#   ...

# Write derived portfolio and run the full pipeline
poetry run python -m src.cli import-transactions ~/Downloads/robinhood_activity.csv
make run-once

# Or derive and run in a single step (no file written to disk)
poetry run python -m src.cli run --transactions ~/Downloads/robinhood_activity.csv --skip-email
```

### Example: Using a Custom Portfolio File

```bash
poetry run python -m src.cli run --portfolio /path/to/my-portfolio.csv --skip-email
```

### Example: Fidelity Import

```bash
# 1. Download your Fidelity positions (Accounts & Trade > Portfolio > Download)
#    File name: Portfolio_Positions_Mar-28-2026.csv

# 2. Add to portfolios.json:
#    { "name": "fidelity", "fidelity_positions": "/path/to/Portfolio_Positions_Mar-28-2026.csv" }

# 3. Preview what will be loaded (no pipeline run)
poetry run python -m src.cli fidelity-import /path/to/Portfolio_Positions_Mar-28-2026.csv --dry-run
# → MSFT: 100.000 shares @ $296.20
# → NVDA: 5000.000 shares @ $78.00 (blended avg)
# → SPAXX: 12500.000 shares @ $1.00 [money market]

# 4. Run the full pipeline
poetry run python -m src.cli run --portfolio-name fidelity --skip-email
```

### Example: Multi-Portfolio with Named Recipients

```bash
# portfolios.json excerpt:
# { "name": "trust", "label": "Family Trust", "fidelity_positions": "...",
#   "purchase_history": "data/trust_purchase_history.json",
#   "email_recipients": ["trustee@example.com", "advisor@example.com"] }

# Run the trust portfolio — sends to its own recipients
poetry run python -m src.cli run --portfolio-name trust

# Run both portfolios (e.g., in a cron job)
poetry run python -m src.cli run --portfolio-name default
poetry run python -m src.cli run --portfolio-name trust
```

### Example: Setting Up Per-Lot Tax Tracking

```bash
# 1. Create data/trust_purchase_history.json (see Per-Lot Tax Tracking section)
# 2. Reference it in portfolios.json via "purchase_history" field
# 3. Run the pipeline — newsletter will include per-lot Tax Efficiency section:

#   Tax Efficiency
#   ─────────────────────────────────────────
#   NVDA  4000 sh  Long-Term (≥1yr)   +$342,000  est. tax $51,300–$68,400
#   NVDA  1000 sh  Short-Term (<1yr)  -$79,250   harvesting candidate ✓
#   GOOG  2000 sh  Long-Term (≥1yr)   +$198,400  est. tax $29,760–$39,680
#   GOOG   500 sh  Short-Term (<1yr)  +$5,120    est. tax $1,126–$1,894
#   ─────────────────────────────────────────
#   Total unrealized gain:  $466,270
#   Estimated tax (low):    $82,186
#   Estimated tax (high):   $109,974
#   Harvestable losses:     -$79,250
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

With default parallelism settings on a 15-ticker portfolio:

| Time | Stage | Parallelism |
|------|-------|-------------|
| 5:00 AM | Portfolio loaded; DB checked | — |
| 5:00–5:08 AM | Web scraping across all tickers (Finviz + fundamentals + SEC filings; Google News, Yahoo, CNBC, MarketWatch, Morningstar via requests) | 3 parallel workers |
| 5:15–5:16 AM | massive.com market signals | Up to 10 tickers × 4 HTTP calls concurrently |
| 5:16–5:22 AM | Analyst Agent per ticker (Claude Haiku) | 5 concurrent `claude -p` subprocesses |
| 5:22–5:27 AM | Director Agent synthesis (Claude Sonnet) | Single call |
| 5:27–5:35 AM | Newsletter rendered (HTML + Markdown + PDF) | — |
| 7:20–7:30 AM | Email delivered + GitHub archive commit | — |

---

## Architecture

```
portfolio.csv  ─or─  robinhood_transactions.csv  ─or─  fidelity_positions.csv  ─or─  portfolios.json
     │                        │                               │                            │
     │                  transaction_reader.py          fidelity_reader.py          portfolio_config.py
     │                  (net shares, avg cost)         (money market auto-detect)  (named portfolios)
     ▼                        │                               │                            │
[main_pipeline.py] ◀──────────┴───────────────────────────────┴────────────────────────────┘
     │    ↑
     │  purchase_history.json (per-lot dates → tax_calculator.py + analyst context)
     │
     ├─▶ Phase 2: Scraping (parallel across tickers) ─────────────────┐
     │   ThreadPoolExecutor(MAX_PARALLEL_TICKERS=3)                   │
     │   Each ticker: Finviz (news + fundamentals + SEC filings)      │
     │            → [Google News/Yahoo/CNBC/MarketWatch/...] in pool  │
     │   Fallback: DuckDuckGo if <3 articles found                    │
     │                                                                │
     ├─▶ Phase 3: Market Signals (parallel across + within tickers) ──┤
     │   Outer: ThreadPoolExecutor(min(N,10)) across tickers          │
     │   Inner: ThreadPoolExecutor(4) per ticker                      │
     │   ├── get_quote()                                              │
     │   ├── get_news()              ← all 4 run concurrently         │
     │   ├── get_analyst_ratings()                                    │
     │   └── get_technicals()                                         │
     │                                       Articles + Signals ◀─────┘
     │
     ├─▶ Phase 4: Analyst Agents (asyncio.gather + Semaphore) ────────┐
     │   asyncio.Semaphore(MAX_PARALLEL_ANALYSTS=5)                   │
     │   Each: claude -p --model claude-haiku-4-5-20251001            │
     │   Rate-limit backoff: detects 429/529/overloaded               │
     │   Output: AnalystReport per ticker                             │
     │                                                                │
     ├─▶ Phase 5: Director Agent ──────────────────────────────────────┘
     │   claude -p --model claude-sonnet-4-6
     │   Input: all AnalystReports + portfolio snapshot
     │   Output: DirectorReport (market theme, action items)
     │
     ├─▶ Newsletter Generator
     │   Jinja2 → HTML + Markdown + PDF (WeasyPrint)
     │
     └─▶ Email Sender  +  GitHub Sync
         Gmail SMTP         Optional archive commit
```

### Key Modules

| Module | Purpose |
|--------|---------|
| `src/config.py` | All settings (Pydantic) — reads from `.env` |
| `src/pipeline/main_pipeline.py` | Pipeline orchestrator — all phases + purchase history loading |
| `src/portfolio/reader.py` | CSV parser, DB persistence |
| `src/portfolio/models.py` | `Holding`, `PortfolioSnapshot` dataclasses (incl. `lot_overrides`) |
| `src/portfolio/portfolio_config.py` | `portfolios.json` loader — multi-portfolio definitions |
| `src/portfolio/transaction_reader.py` | Robinhood activity CSV parser |
| `src/portfolio/fidelity_reader.py` | Fidelity positions CSV parser — money market auto-detection |
| `src/portfolio/tax_calculator.py` | Per-lot capital gains estimator — short/long-term classification |
| `src/scrapers/scraper_orchestrator.py` | Multi-source scraping + parallel ticker dispatch |
| `src/api/endpoints.py` | massive.com REST client — parallel signal fetching |
| `src/agents/analyst_agent.py` | Analyst agent — async subprocess pool + semaphore |
| `src/agents/director_agent.py` | Director agent — portfolio synthesis |
| `src/newsletter/generator.py` | Jinja2 HTML/MD rendering + PDF |
| `src/delivery/email_sender.py` | Gmail SMTP delivery |
| `src/agents/fullstack_agent.py` | Weekly maintenance audit |
| `src/scheduler.py` | APScheduler daemon |
| `src/cli.py` | Click CLI entry point |

---

## Agents

| Agent | Model | Runs | Purpose |
|-------|-------|------|---------|
| **Analyst** | `claude-haiku-4-5-20251001` | Daily, per ticker (parallel) | Article summarization, sentiment scoring, BUY/HOLD/SELL recommendation. Prompt includes scraped articles, technical signals, analyst ratings, **Finviz fundamentals** (P/E, EPS, margins, ROE, short float, etc.), and **recent SEC filings** |
| **Director** | `claude-sonnet-4-6` | Daily, once | Portfolio synthesis, market theme, action items |
| **Fullstack** | — (no AI) | Weekly (via `make audit`) | DB health, cost audit, security scan, GitHub sync |

All AI calls are made via `claude -p` subprocesses, routing through your Claude Code subscription rather than consuming Anthropic API credits.

---

## News Sources

| Source | Method | Data Type |
|--------|--------|-----------|
| Finviz | Selenium + requests + BeautifulSoup | News links, full fundamentals snapshot, SEC filings |
| Google News RSS | requests + defusedxml | Headlines from hundreds of outlets — no auth |
| Yahoo Finance | requests + BeautifulSoup | Full articles |
| CNBC | requests + BeautifulSoup (Queryly API) | Full articles |
| MarketWatch | requests + BeautifulSoup | Pre-paywall snippets |
| Morningstar | requests + BeautifulSoup | Analysis text |
| massive.com | REST API | Structured signals, Benzinga news |
| DuckDuckGo | requests (DDGS) | Fallback when <3 articles found |

> **Note:** Reuters and Seeking Alpha scrapers were retired. Yahoo Finance, CNBC, and MarketWatch were migrated from Playwright (headless Chrome) to requests-based scrapers, eliminating the browser dependency for the article-fetching phase.

---

## Cost Estimate

All AI calls use `claude -p` via the Claude Code CLI, which routes through your Claude Code subscription — no separate Anthropic API credits consumed.

| Component | Model | Tokens per call (est.) |
|-----------|-------|------------------------|
| Analyst Agent (per ticker) | Haiku 4.5 | ~4,000 in + ~600 out (includes fundamentals + SEC filings) |
| Director Agent (once) | Sonnet 4.6 | ~8,000 in + ~800 out |
| **15-ticker pipeline total** | | ~68,000 in + ~9,800 out |

### massive.com API

| Component | Calls per pipeline run |
|-----------|----------------------|
| Quotes | 1 per ticker |
| News articles | 1 per ticker |
| Analyst ratings | 1 per ticker |
| Technical indicators | 1 per ticker |
| **15-ticker total** | **60 API calls** |

Costs vary by massive.com plan. Run `make audit` to see actual estimated costs based on your DB call history.

---

## Security

- All secrets in `.env` (gitignored, `chmod 600`)
- `portfolio.csv` gitignored — never leaves your machine
- Ticker symbols validated against `^[A-Z]{1,5}$` before use in URLs
- Output directory enforced within `newsletters/` via `Path.relative_to()` — absolute paths (e.g., `/etc/`) rejected at the boundary
- **SSRF protection with DNS resolution** — `is_safe_url()` resolves hostnames via `socket.getaddrinfo()` before making any outbound request, blocking internal hostnames (e.g., `metadata.google.internal`) that resolve to private IPs; fails closed on DNS errors
- **WeasyPrint SSRF mitigation** — custom `url_fetcher` runs `is_safe_url()` before every asset fetch during PDF rendering; WeasyPrint upgraded to ≥68 (addresses CVE-2025-68616)
- **XML bomb protection** — Google News RSS parsed with `defusedxml` to prevent billion-laughs DoS on malicious feeds
- **`--dangerously-skip-permissions` gated** — claude CLI subprocesses only pass the flag when `CLAUDE_SKIP_PERMISSIONS=true` (default); set to `false` for supervised/audit runs
- **CNBC Queryly API key configurable** — moved from hardcoded value to `QUERYLY_API_KEY` env var
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
