# Financial Bytes

Automated daily stock portfolio newsletter powered by AI agents. DB-first, resumable pipeline that scrapes financial news, pulls fundamentals and SEC filings, analyzes each holding with Claude, and delivers a pre-market brief to your inbox.

Handles portfolios of **300+ tickers** via parallel scraping, DB-backed signal caching, and a crash-resumable analyst phase. Natively imports Fidelity positions exports, supports multiple named portfolios with grouped email delivery, tracks per-lot capital gains tax exposure, and incorporates fundamental data (P/E, margins, short interest, ROE) and recent SEC filings into every analyst recommendation.

---

## Contents

- [Architecture](#architecture)
- [Pipeline Phases](#pipeline-phases)
- [Resume Behavior](#resume-behavior)
- [Installation](#installation)
- [Portfolio Configuration](#portfolio-configuration)
- [Per-Lot Tax Tracking](#per-lot-tax-tracking)
- [Earnings Calendar](#earnings-calendar)
- [Environment Variables](#environment-variables)
- [Scheduled Jobs](#scheduled-jobs)
- [CLI Reference](#cli-reference)
- [Parallelism & Performance](#parallelism--performance)
- [Data Sources](#data-sources)
- [Agents](#agents)
- [Cost Estimate](#cost-estimate)
- [Security](#security)

---

## Architecture

```
portfolios.json  →  portfolio_config.py  →  load_portfolio_defs()
                                                     │
                              ┌──────────────────────┘
                              │
                     [main_pipeline.py]
                              │
          ┌───────────────────┼───────────────────────────┐
          │                   │                           │
   Phase 1: Portfolio  Phase 2: Scrape           Phase 3: Signals
   CSV / Fidelity      articles table            api_signals table
   export / Plaid      (DB-first)                (TTL cache)
                              │                           │
          ┌───────────────────┴───────────────────────────┘
          │
   Phase 4: Analysts
   summaries table (DB-first)
   asyncio.gather + Semaphore
   crash-resumable
          │
   Phase 5: Director
   reads summaries from DB (no in-memory payload)
   prompt size bounded regardless of portfolio size
          │
   Phase 6: Newsletter
   newsletters/YYYY-MM-DD/<portfolio_name>/
   HTML + Markdown + PDF
          │
   Email Sender  (per-portfolio or combined group email)
```

### Key Modules

| Module | Purpose |
|--------|---------|
| `src/config.py` | All settings (Pydantic) — reads from `.env` |
| `src/pipeline/main_pipeline.py` | Orchestrator — all phases, purchase history, pipeline_runs tracking |
| `src/portfolio/portfolio_config.py` | `portfolios.json` loader — multi-portfolio definitions |
| `src/portfolio/reader.py` | CSV parser and DB persistence |
| `src/portfolio/fidelity_reader.py` | Fidelity positions CSV parser — money market auto-detection |
| `src/portfolio/transaction_reader.py` | Robinhood activity CSV parser |
| `src/portfolio/tax_calculator.py` | Per-lot capital gains — short/long-term classification |
| `src/scrapers/scraper_orchestrator.py` | Multi-source parallel scraping |
| `src/api/endpoints.py` | massive.com REST client — parallel signal fetching |
| `src/agents/analyst_agent.py` | Analyst agent — async subprocess pool + semaphore + DB cache |
| `src/agents/director_agent.py` | Director agent — reads from `summaries` DB, portfolio synthesis |
| `src/newsletter/generator.py` | Jinja2 HTML/MD rendering + WeasyPrint PDF |
| `src/delivery/email_sender.py` | SMTP delivery + group email combining |
| `src/scheduler.py` | APScheduler daemon |
| `src/cli.py` | Click CLI entry point |

---

## Pipeline Phases

### Phase 1 — Portfolio

Reads holdings from one of four sources:

- **`csv_path`** — hand-maintained CSV (`ticker, shares, cost_basis, purchase_date`)
- **`fidelity_positions`** — Fidelity `Portfolio_Positions_*.csv` export. Money market funds (SPAXX, FZDXX, FZAXX) are auto-detected and handled at $1.00 NAV. `fidelity_account_filter` filters a multi-account export by account name substring.
- **`transactions_path`** — Robinhood activity CSV; net shares and weighted average cost basis are computed on the fly.
- **`plaid_access_token_env`** — env var name containing a Plaid access token for live Fidelity position sync.

If `max_positions` is set, only the top-N positions by cost-basis value (shares × cost_basis) are kept. Useful for accounts with 300+ holdings.

### Phase 2 — Scrape (DB-first)

Checks the `articles` table for each ticker. Fresh articles (within `ARTICLE_LOOKBACK_HOURS`) are returned directly from DB; only stale tickers trigger live scraping. Sources: Finviz (news + full fundamentals + SEC filings), Google News RSS, Yahoo Finance, CNBC, MarketWatch, Morningstar. DuckDuckGo fallback if fewer than 3 articles found.

Scraping runs via `ThreadPoolExecutor(MAX_PARALLEL_TICKERS)` — each worker handles one ticker at a time.

### Phase 3 — Signals (DB-first, TTL cache)

Checks the `api_signals` table. Signals cached within `SIGNAL_CACHE_TTL_HOURS` (default: 1h) are served from DB. Only stale tickers trigger live massive.com API calls. Live fetching runs two levels of concurrency: up to 10 tickers in parallel, each making 4 endpoint calls concurrently (quote, news, analyst ratings, technicals).

### Phase 4 — Analysts (DB-first, crash-resumable)

Checks the `summaries` table before calling Claude. If today's summary for a ticker already exists, the Claude call is skipped entirely. All analyst calls run via `asyncio.gather` with a `asyncio.Semaphore(MAX_PARALLEL_ANALYSTS)` cap.

Resume behavior: if a 345-ticker run crashes at ticker 200, restarting picks up from ticker 201. The 200 completed summaries in DB are served without any Claude calls.

### Phase 5 — Director

Reads all analyst summaries directly from the `summaries` DB table — no in-memory analyst payload is passed. Prompt size is bounded regardless of portfolio size. Single Claude Sonnet call that synthesizes a market theme, portfolio brief, and action items.

### Phase 6 — Newsletter

Generates HTML, Markdown, and PDF output in `newsletters/YYYY-MM-DD/<portfolio_name>/`. Includes portfolio P&L, per-lot tax efficiency section, collapsible per-stock analyst cards, and action checklist.

---

## Resume Behavior

Every pipeline run creates or updates a row in the `pipeline_runs` table keyed on `(portfolio_name, report_date)`. Each phase writes its completion status:

```
pipeline_runs
├── run_id
├── portfolio_name
├── report_date
├── status          (running | complete | failed)
├── phase           (portfolio | scrape | signals | analysts | director | newsletter)
├── total_tickers
├── tickers_complete
└── completed_at
```

On restart for the same portfolio and date:

- Phase 2: articles already in DB are skipped — only missing tickers are scraped
- Phase 3: signals within TTL are served from DB — only stale tickers call massive.com
- Phase 4: tickers with summaries in DB are skipped — only incomplete tickers call Claude
- Phase 5: runs only if director report is not yet in DB for today

Same-day re-runs are effectively idempotent: zero LLM calls if all summaries are already in DB, zero scrape requests if all articles are fresh. This also means manually re-running the pipeline to regenerate the newsletter costs nothing.

---

## Installation

### Prerequisites

- Python 3.11+
- [Poetry](https://python-poetry.org/docs/#installation)
- PostgreSQL 14+ (or SQLite: `DATABASE_URL=sqlite:///financial_bytes.db`)
- Google Chrome (Selenium — Finviz scraper only; all other scrapers are requests-based)
- [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code) authenticated with a Claude Code subscription

### 1. Clone

```bash
git clone https://github.com/nbossn/financial-bytes.git
cd financial-bytes
```

### 2. Install Dependencies

```bash
make install
```

### 3. Configure Environment

```bash
cp .env.template .env
chmod 600 .env
```

Fill in `.env` — see [Environment Variables](#environment-variables).

### 4. Initialize the Database

```bash
make migrate
```

### 5. Verify Setup

```bash
make test-newsletter
# → newsletters/test/  (open the .html file to preview)
```

---

## Portfolio Configuration

Define all portfolios in `portfolios.json` at the project root. If the file does not exist, a single `default` portfolio is loaded from `PORTFOLIO_CSV_PATH`.

### All Fields

| Field | Type | Description |
|-------|------|-------------|
| `name` | string (required) | Identifier used in DB, output paths, and `--portfolio-name` flag |
| `label` | string | Display name in newsletter header |
| `csv_path` | string | Path to a hand-maintained portfolio CSV |
| `fidelity_positions` | string | Path to a Fidelity `Portfolio_Positions_*.csv` export |
| `fidelity_account_filter` | string | Optional substring match on Account Name (filters multi-account Fidelity exports) |
| `transactions_path` | string | Path to a Robinhood transaction activity CSV |
| `purchase_history` | string | Path to a per-lot JSON file for LTCG tax classification (see [Per-Lot Tax Tracking](#per-lot-tax-tracking)) |
| `plaid_access_token_env` | string | Env var name containing a Plaid access token for live Fidelity sync |
| `max_positions` | integer | Cap pipeline to top-N positions by cost-basis value. For accounts with 300+ holdings. |
| `email_recipients` | string[] | Email addresses to send this portfolio's newsletter to |
| `email_group` | string | Portfolios sharing the same group name get one combined email. e.g. `nbossn` + `nbossn_fidelity` both set `"email_group": "nick"` → one email with both portfolios. |

Exactly one of `csv_path`, `fidelity_positions`, `transactions_path`, or `plaid_access_token_env` must be set as the data source.

### Example `portfolios.json`

```json
[
  {
    "name": "nbossn",
    "label": "My Portfolio",
    "csv_path": "portfolio.csv",
    "plaid_access_token_env": "PLAID_ACCESS_TOKEN_NBOSSN",
    "email_group": "nick",
    "email_recipients": ["you@example.com"]
  },
  {
    "name": "nbossn_fidelity",
    "label": "My Portfolio (Fidelity)",
    "fidelity_positions": "/path/to/Portfolio_Positions_Apr-30-2026.csv",
    "fidelity_account_filter": null,
    "purchase_history": "data/nbossn_purchase_history.json",
    "max_positions": 25,
    "email_group": "nick",
    "email_recipients": ["you@example.com"]
  },
  {
    "name": "trust",
    "label": "Family Trust",
    "fidelity_positions": "/path/to/trust_Portfolio_Positions_Apr-30-2026.csv",
    "fidelity_account_filter": "Trust",
    "purchase_history": "data/trust_purchase_history.json",
    "plaid_access_token_env": "PLAID_ACCESS_TOKEN_TRUST",
    "email_recipients": ["trustee@example.com"]
  }
]
```

In this example, `nbossn` and `nbossn_fidelity` share `"email_group": "nick"` — both newsletters are run independently, then combined into a single email sent to `you@example.com`. `trust` has no group and sends its own email to `trustee@example.com`.

### CSV Format (for `csv_path`)

```csv
ticker,shares,cost_basis,purchase_date
MSFT,100,555.23,2025-08-01
NVDA,5000,78.00,2023-06-15
AAPL,50,178.90,2024-03-15
```

---

## Per-Lot Tax Tracking

Create a JSON file and reference it via the `purchase_history` field in `portfolios.json`. This enables accurate short-term vs. long-term capital gains classification in the newsletter's Tax Efficiency section, and passes the earliest lot date to the Analyst agent for holding period context.

### Format

```json
{
  "_comment": "Keys starting with _ are ignored. Empty arrays are skipped.",
  "NVDA": [
    {"shares": 1000, "cost_basis": 143.35, "purchase_date": "2025-06-16", "notes": "Short-term lot"},
    {"shares": 4000, "cost_basis": 44.50,  "purchase_date": "2023-06-15", "notes": "Long-term lot"}
  ],
  "MSFT": [
    {"shares": null, "cost_basis": 296.20, "purchase_date": "2021-08-01"}
  ],
  "SPAXX": []
}
```

| Field | Type | Description |
|-------|------|-------------|
| `shares` | number or `null` | Shares in this lot. Use `null` to assign all remaining shares for single-lot holdings. |
| `cost_basis` | number | Per-share cost basis for this lot |
| `purchase_date` | YYYY-MM-DD | Date of purchase — determines short vs. long-term classification |
| `notes` | string (optional) | Free-text label (not used in calculations) |

**Important:** The Fidelity "History" CSV (transaction activity) does NOT contain original lot purchase dates. To populate this file accurately, export "Cost Basis" from Fidelity: Accounts → Cost Basis → Download CSV. That export includes the acquisition date per lot.

Tickers omitted from this file fall back to the aggregated purchase date from the portfolio CSV. Set a ticker to `[]` to explicitly skip it from tax calculations (e.g. money market funds).

---

## Earnings Calendar

Upcoming earnings events are stored in `data/earnings_calendar.json`. The APScheduler daemon checks this file at 7:10 AM ET and runs premarket analysis only on days with pre-market events.

### Format

```json
{
  "2026-04-30": [
    {
      "ticker": "LLY",
      "time": "pre-market",
      "prev_close": 851.21,
      "guide": "Mounjaro+Zepbound combined vs. $9-10B threshold"
    },
    {
      "ticker": "AAPL",
      "time": "after-close",
      "prev_close": 270.17,
      "guide": "Services revenue vs. $30.4B and iPhone vs. $56.5B"
    }
  ],
  "2026-05-20": [
    {
      "ticker": "NVDA",
      "time": "after-close",
      "guide": "Data Center revenue vs. $73-75B guidance — Beat >75B, Severe Miss <58B"
    }
  ]
}
```

| Field | Values | Description |
|-------|--------|-------------|
| `ticker` | string | Ticker symbol |
| `time` | `pre-market` or `after-close` | When results are released. Only `pre-market` events trigger the 7:10 AM Discord alert. |
| `prev_close` | number (optional) | Previous session close — used to compute premarket % move. If omitted, fetched live via yfinance. |
| `guide` | string (optional) | Decision guide or threshold to watch — included in the Discord alert. |

### Adding Events

```bash
# After-close event (no prev_close needed)
financial-bytes add-earnings-event --date 2026-05-20 --ticker NVDA --time after-close \
    --guide "Data Center revenue vs. $73-75B guidance"

# Pre-market event with prev_close and guide
financial-bytes add-earnings-event --date 2026-04-30 --ticker LLY --time pre-market \
    --prev-close 851.21 --guide "Mounjaro+Zepbound vs. $9-10B threshold"

# View upcoming events
financial-bytes show-earnings-calendar --days 30
```

---

## Environment Variables

All configuration is read from `.env` (Pydantic `BaseSettings`). Copy `.env.template` and fill in.

### Required

| Variable | Description |
|----------|-------------|
| `ANTHROPIC_API_KEY` | Anthropic API key (used by `claude -p` subprocess calls) |
| `MASSIVE_API_KEY` | massive.com API key (market signals, analyst ratings, price targets) |
| `DATABASE_URL` | SQLAlchemy connection string. Default: `sqlite:///financial_bytes.db`. PostgreSQL recommended for production: `postgresql://user:pass@localhost:5432/financial_bytes` |
| `EMAIL_RECIPIENT` | Default email recipient (used when `email_recipients` is not set in portfolios.json) |
| `EMAIL_FROM` | Sender address |
| `SMTP_HOST` | SMTP server. Default: `smtp.gmail.com` |
| `SMTP_USER` | SMTP username |
| `SMTP_PASS` | SMTP password (Gmail: use a 16-char App Password, not account password) |

### Schedule & Timezone

| Variable | Default | Description |
|----------|---------|-------------|
| `PIPELINE_START_TIME` | `05:30` | Time to run daily pipeline (HH:MM, 24h, in `NEWSLETTER_TIMEZONE`) |
| `NEWSLETTER_TIMEZONE` | `America/New_York` | Timezone for all scheduled jobs |

### Parallelism & Cache

| Variable | Default | Description |
|----------|---------|-------------|
| `MAX_PARALLEL_TICKERS` | `8` | Concurrent scraper workers (each spawns a Chrome instance for Finviz) |
| `MAX_PARALLEL_ANALYSTS` | `12` | Concurrent `claude -p` analyst calls (bounded by asyncio Semaphore) |
| `SIGNAL_CACHE_TTL_HOURS` | `1` | How long massive.com signals are considered fresh in `api_signals` table |
| `ANALYST_CACHE_ENABLED` | `true` | Whether to skip Claude calls for tickers that already have today's summary in DB |

### Alerts

| Variable | Description |
|----------|-------------|
| `DISCORD_WEBHOOK_URL` | Required for Discord alerts (decision reminders, pre-market earnings check). If unset, alerts log a warning and are skipped. |

### Optional

| Variable | Default | Description |
|----------|---------|-------------|
| `MASSIVE_BASE_URL` | `https://api.massive.com` | massive.com base URL |
| `SMTP_PORT` | `587` | SMTP port |
| `PORTFOLIO_CSV_PATH` | `portfolio.csv` | Default portfolio CSV path (used when portfolios.json absent) |
| `PORTFOLIOS_CONFIG` | `portfolios.json` | Path to portfolios config file |
| `GITHUB_TOKEN` | — | GitHub token for newsletter archive commits |
| `GITHUB_REPO` | — | Target repo for archive (e.g. `username/financial-bytes`) |
| `QUERYLY_API_KEY` | `31a35d40a9a64ab3` | CNBC Queryly search API key (public key provided; override if blocked) |
| `CLAUDE_SKIP_PERMISSIONS` | `true` | Pass `--dangerously-skip-permissions` to `claude -p`. Set `false` for supervised runs. |
| `SCRAPER_DELAY_MIN` | `2.0` | Min seconds between requests per scraper |
| `SCRAPER_DELAY_MAX` | `5.0` | Max seconds between requests per scraper |
| `MAX_ARTICLES_PER_TICKER` | `15` | Max articles fed to analyst agent per ticker |
| `ARTICLE_LOOKBACK_HOURS` | `24` | How far back to pull articles from DB |
| `LOG_LEVEL` | `INFO` | Log verbosity |
| `LOG_FILE` | `logs/financial_bytes.log` | Log output path |

---

## Scheduled Jobs

Scheduling is split between the Dopple cron daemon (`setup-cron.sh`) and the financial-bytes APScheduler daemon (`financial-bytes schedule`). All times are Eastern Time.

### Cron (installed via `Dopple/Scripts/setup-cron.sh`)

| Schedule | Job | Description |
|----------|-----|-------------|
| `@reboot` | `financial-bytes schedule` | Start APScheduler daemon on WSL startup (45s delay) |
| `5:50 AM` daily | watchdog | Restart APScheduler daemon if dead (`pgrep` check — noop if running) |
| `5:00 PM` daily | `dopple-daemon.sh check-handoff` | Dopple: check for handoff, parse daily notes |
| `7:00 PM` daily (weekdays) | `fidelity-sync.py` | Sync Fidelity portfolios via Plaid after market close |
| `10:00 PM` daily | `dopple-daemon.sh infer-handoff` | Dopple: auto-infer tasks if no handoff written |
| `11:05 PM` daily | watchdog | Restart Dopple continuous loop if dead |

### APScheduler (runs inside `financial-bytes schedule` daemon)

| Schedule | Job | Description |
|----------|-----|-------------|
| `PIPELINE_START_TIME` (default 5:30 AM) | `_run_all_portfolios()` | Full pipeline for all portfolios defined in `portfolios.json` |
| `6:00 AM` daily | `_run_reminder_check()` | Send Discord alert if any decision reminders are due within 24h |
| `7:10 AM` daily | `_run_premarket_earnings_check()` | Fires only on days with `pre-market` events in `data/earnings_calendar.json`. Fetches premarket prices via yfinance and posts directional signal to Discord. |

### Starting the Daemon Manually

```bash
# Start (runs in foreground — use screen/tmux for persistence)
financial-bytes schedule

# Or via Make
make run

# Logs
tail -f logs/scheduler.log
```

---

## CLI Reference

All commands are available as `financial-bytes <command>` (installed via Poetry) or `poetry run python -m src.cli <command>`.

### `financial-bytes run`

Run the full pipeline: scrape → analyse → newsletter → email.

```bash
financial-bytes run [OPTIONS]

Options:
  -p, --portfolio PATH          Path to portfolio CSV
  -t, --transactions PATH       Activity export CSV (Robinhood/Fidelity)
  -d, --date YYYY-MM-DD         Report date (default: today)
  --skip-scrape                 Use cached articles from DB (skip Phase 2)
  --skip-email                  Generate newsletter but do not send email
  --output-dir PATH             Output directory (default: newsletters)
  --portfolio-name NAME         Portfolio identifier from portfolios.json
  --portfolio-label LABEL       Display name for newsletter title
  -r, --email-recipients EMAIL  Override recipients (repeatable)
```

Examples:

```bash
# Run default portfolio from portfolios.json
financial-bytes run

# Run specific named portfolio, skip email
financial-bytes run --portfolio-name trust --skip-email

# Run with inline CSV, override date
financial-bytes run --portfolio /path/to/my.csv --date 2025-12-01 --skip-email

# Use cached articles — no scraping, fast analyst re-run
financial-bytes run --skip-scrape --portfolio-name nbossn_fidelity
```

### `financial-bytes analyse`

Run analyst agents only on cached articles. Does not scrape or run the Director.

```bash
financial-bytes analyse [TICKERS...] [--date YYYY-MM-DD]

# Examples
financial-bytes analyse MSFT NVDA AAPL
financial-bytes analyse NVDA --date 2025-12-01
```

### `financial-bytes fidelity-import`

Preview or import a Fidelity positions CSV.

```bash
financial-bytes fidelity-import POSITIONS_CSV [OPTIONS]

Options:
  -o, --output PATH             Write derived holdings to this CSV
  --account-filter TEXT         Filter by account name substring
  --dry-run                     Print derived holdings without writing files
```

### `financial-bytes add-earnings-event`

Add an event to `data/earnings_calendar.json`.

```bash
financial-bytes add-earnings-event [OPTIONS]

Options:
  --date YYYY-MM-DD     Earnings date (required)
  --ticker TEXT         Ticker symbol (required)
  --time [pre-market|after-close]  When results are released (required)
  --prev-close FLOAT    Previous close price (for premarket % calculation)
  --guide TEXT          Decision guide or threshold to watch

# Examples
financial-bytes add-earnings-event --date 2026-05-20 --ticker NVDA --time after-close \
    --guide "Data Center vs. $73-75B guidance"

financial-bytes add-earnings-event --date 2026-04-30 --ticker LLY --time pre-market \
    --prev-close 851.21 --guide "Mounjaro+Zepbound vs. $9-10B threshold"
```

### `financial-bytes add-reminder`

Add a time-gated decision reminder. The scheduler sends a Discord alert when the deadline is within `--hours-before` hours.

```bash
financial-bytes add-reminder [OPTIONS]

Options:
  -c, --context TEXT         Decision context (required)
  --deadline YYYY-MM-DD      Deadline date (required)
  --hours-before INT         Alert lead time in hours (default: 24)
  --id TEXT                  Custom reminder ID (auto-generated if omitted)

# Examples
financial-bytes add-reminder -c "AMD trim: 5sh before earnings" --deadline 2026-05-02
financial-bytes add-reminder -c "AMZN trim 512sh (trust): execute this week" --deadline 2026-05-05

# List pending reminders
financial-bytes list-reminders

# Remove a reminder
financial-bytes remove-reminder <id>
```

### `financial-bytes check-stops`

Check portfolio positions against stop-loss thresholds and alert via Discord.

```bash
financial-bytes check-stops [OPTIONS]

Options:
  -p, --portfolio PATH        Portfolio CSV (default: portfolio.csv)
  --portfolio-name NAME       Portfolio identifier
  --no-alert                  Print results without sending Discord alert
  --mode [static|dynamic|hybrid]  Stop mode (default: hybrid)
  --atr-multiplier FLOAT      ATR14 multiplier for dynamic/hybrid (default: 5.0)
```

### `financial-bytes check-dividends`

Show dividend income projections and upcoming ex-dividend dates for the portfolio.

```bash
financial-bytes check-dividends [OPTIONS]

Options:
  -p, --portfolio PATH        Portfolio CSV (default: portfolio.csv)
```

### Other Commands

| Command | Description |
|---------|-------------|
| `financial-bytes schedule` | Start APScheduler daemon (blocking) |
| `financial-bytes scrape [TICKERS...]` | Scrape articles only — no AI, no email |
| `financial-bytes newsletter` | Generate newsletter from existing DB data |
| `financial-bytes import-transactions CSV` | Derive holdings from Robinhood transaction CSV |
| `financial-bytes portfolios` | List all configured portfolios |
| `financial-bytes show-earnings-calendar` | Show upcoming earnings events |
| `financial-bytes premarket-check` | Manual premarket price check for a ticker |
| `financial-bytes suggest-stops` | Show ATR-based stop-loss recommendations |
| `financial-bytes plaid-setup` | One-time Plaid OAuth setup for a portfolio |
| `financial-bytes plaid-sync` | Sync live positions from Plaid |
| `financial-bytes track-performance` | Record daily P&L snapshot to DB |
| `financial-bytes show-performance` | Show historical performance chart |
| `financial-bytes audit` | DB health check, cost audit, security scan |

---

## Parallelism & Performance

| Phase | Concurrency | Config |
|-------|-------------|--------|
| Phase 2 — Scrape | `ThreadPoolExecutor(MAX_PARALLEL_TICKERS)` across tickers | `MAX_PARALLEL_TICKERS` (default: 8) |
| Phase 3 — Signals | `ThreadPoolExecutor(min(N, 10))` across tickers + inner `ThreadPoolExecutor(4)` per ticker | hardcoded at 10 outer |
| Phase 4 — Analysts | `asyncio.gather` + `asyncio.Semaphore(MAX_PARALLEL_ANALYSTS)` | `MAX_PARALLEL_ANALYSTS` (default: 12) |
| Phase 5 — Director | Single call | n/a |

### Wall-Clock Estimates (25-ticker portfolio, defaults)

| Phase | Time |
|-------|------|
| Phase 2 — Scrape | ~3–4 min (8 workers; most scrapers are requests-based) |
| Phase 3 — Signals | ~10 s (10 outer × 4 inner HTTP calls) |
| Phase 4 — Analysts | ~20–30 s (12 concurrent Haiku) |
| Phase 5 — Director | ~20 s (single Sonnet call) |
| **Total** | **~5–6 min** |

### Tuning `MAX_PARALLEL_ANALYSTS`

| Plan | Safe range | Notes |
|------|-----------|-------|
| Claude Code Pro | 5–8 | Start at 5; increase if no 429s |
| Claude Code Max | 8–12 | 12 is the default |
| Claude Code Enterprise | 12+ | Consult org rate limit allocation |

Rate-limit errors (429, 529, overloaded) trigger extended backoff: 3× longer than normal errors, up to 60s between retries, 5 total attempts.

### SQLite Concurrent Write Safety

When using SQLite, the session layer automatically enables WAL mode and a 10-second busy timeout so multi-threaded writes from parallel scrapers don't deadlock. For PostgreSQL this is not needed.

---

## Data Sources

| Source | Method | Data |
|--------|--------|------|
| Finviz | Selenium + requests + BeautifulSoup | News links, full fundamentals (P/E, EPS, margins, ROE, short float), SEC filings |
| Google News RSS | requests + defusedxml | Headlines from hundreds of outlets — no auth required |
| Yahoo Finance | requests + BeautifulSoup | Full article text |
| CNBC | requests + BeautifulSoup (Queryly API) | Full article text |
| MarketWatch | requests + BeautifulSoup | Pre-paywall snippets and headlines |
| Morningstar | requests + BeautifulSoup | Analysis text |
| massive.com | REST API | Analyst ratings, price targets, technical indicators, Benzinga news sentiment, real-time quotes |
| DuckDuckGo | requests (DDGS) | Fallback when fewer than 3 articles found |

---

## Agents

| Agent | Model | Runs | Purpose |
|-------|-------|------|---------|
| Analyst | `claude-haiku-4-5-20251001` | Daily, per ticker (parallel, DB-cached) | BUY/HOLD/SELL recommendation, confidence, sentiment, catalysts, risks. Prompt includes articles, Finviz fundamentals, SEC filings, technical signals, holding details. |
| Director | `claude-sonnet-4-6` | Daily, once | Reads analyst summaries from DB. Synthesizes market theme, 5-minute brief, action items. |

All AI calls are made via `claude -p` subprocesses, routing through your Claude Code subscription rather than consuming Anthropic API credits directly.

---

## Cost Estimate

| Component | Model | Tokens per call (est.) |
|-----------|-------|------------------------|
| Analyst (per ticker) | Haiku 4.5 | ~4,000 in + ~600 out |
| Director (once) | Sonnet 4.6 | ~8,000 in + ~800 out |
| **25-ticker pipeline total** | | ~108,000 in + ~15,800 out |

massive.com: 4 API calls per ticker (quote, news, analyst ratings, technicals). A 25-ticker pipeline makes 100 calls per run.

Run `financial-bytes audit` to see estimated costs based on actual call counts in your DB.

---

## Security

- All secrets in `.env` (gitignored, `chmod 600`)
- `portfolio.csv` and `portfolios.json` gitignored — never leave your machine
- Ticker symbols validated against `^[A-Z]{1,5}$` before use in URLs
- Output directory enforced within `newsletters/` via `Path.relative_to()` — absolute paths rejected at the boundary
- **SSRF protection with DNS resolution** — `is_safe_url()` resolves hostnames via `socket.getaddrinfo()` before making any outbound request, blocking internal hostnames (e.g. `metadata.google.internal`) that resolve to private IPs; fails closed on DNS errors
- **WeasyPrint SSRF mitigation** — custom `url_fetcher` runs `is_safe_url()` before every asset fetch during PDF rendering; WeasyPrint ≥68 (addresses CVE-2025-68616)
- **XML bomb protection** — Google News RSS parsed with `defusedxml` to prevent billion-laughs DoS
- **`--dangerously-skip-permissions` gated** — only passed to `claude -p` when `CLAUDE_SKIP_PERMISSIONS=true`; set to `false` for supervised runs
- Jinja2 `SandboxedEnvironment` — prevents template injection via AI-generated content
- GitHub token passed via `GIT_ASKPASS` — never embedded in command args or URLs
- SMTP auth errors logged without credential fragments
- Weekly automated security scan via `financial-bytes audit`

---

## Development

```bash
make test        # Run test suite
make lint        # ruff + black check
make format      # Auto-format
make migrate     # Run pending alembic migrations
make db-shell    # Open psql shell
make logs        # Tail live log

# Debug run
financial-bytes --log-level DEBUG run --skip-email

# Add a migration after changing DB models
poetry run alembic revision --autogenerate -m "description"
make migrate
```

Tests use SQLite in-memory and mock all external APIs (Claude, SMTP, scrapers). No real credentials or network access required.

---

## License

MIT
