# Financial Bytes

Automated daily stock portfolio newsletter powered by AI agents. DB-first, resumable pipeline that scrapes financial news, pulls fundamentals and SEC filings, analyzes each holding with Claude, and delivers a pre-market brief to your inbox.

Handles portfolios of **300+ tickers** via parallel scraping, DB-backed signal caching, and a crash-resumable analyst phase. Natively imports Fidelity positions exports (manual CSV or live automated scrape), supports multiple named portfolios with grouped email delivery, tracks per-lot capital gains tax exposure, and incorporates fundamental data (P/E, margins, short interest, ROE) and recent SEC filings into every analyst recommendation.

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
- [Fidelity Live Sync](#fidelity-live-sync)
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
| `src/portfolio/fidelity_scraper.py` | Automated live Fidelity sync — DrissionPage + visual state machine |
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

For fully automated Fidelity sync without manual CSV exports, see [Fidelity Live Sync](#fidelity-live-sync).

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

Same-day re-runs are effectively idempotent: zero LLM calls if all summaries are already in DB, zero scrape requests if all articles are fresh.

---

## Installation

### Prerequisites

- Python 3.11+
- [Poetry](https://python-poetry.org/docs/#installation)
- PostgreSQL 14+ (or SQLite: `DATABASE_URL=sqlite:///financial_bytes.db`)
- Google Chrome (Finviz scraper)
- [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code) authenticated with a Claude Code subscription

**For Fidelity live sync only** (optional):
- Windows Chrome installed at `C:\Program Files\Google\Chrome\Application\chrome.exe`
- WSL2 with mirrored networking enabled (see [Fidelity Live Sync](#fidelity-live-sync))
- A Fidelity account with 2FA via authenticator app

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

Define all portfolios in `portfolios.json` at the project root.

### All Fields

| Field | Type | Description |
|-------|------|-------------|
| `name` | string (required) | Identifier used in DB, output paths, and `--portfolio-name` flag |
| `label` | string | Display name in newsletter header |
| `csv_path` | string | Path to a hand-maintained portfolio CSV |
| `fidelity_positions` | string | Path to a Fidelity `Portfolio_Positions_*.csv` export |
| `fidelity_account_filter` | string | Optional substring match on Account Name |
| `fidelity_creds_prefix` | string | Credentials prefix for separate Fidelity logins (e.g. `"LILICH"` → reads `FIDELITY_LILICH_*` from `.env`) |
| `transactions_path` | string | Path to a Robinhood transaction activity CSV |
| `purchase_history` | string | Path to a per-lot JSON file for LTCG tax classification |
| `plaid_access_token_env` | string | Env var name containing a Plaid access token |
| `max_positions` | integer | Cap pipeline to top-N positions by cost-basis value |
| `email_recipients` | string[] | Email addresses for this portfolio's newsletter |
| `email_group` | string | Portfolios sharing the same group name get one combined email |

### Example `portfolios.json`

```json
[
  {
    "name": "nbossn_fidelity",
    "label": "My Portfolio (Fidelity)",
    "fidelity_positions": "/path/to/Portfolio_Positions_Apr-30-2026.csv",
    "purchase_history": "data/nbossn_purchase_history.json",
    "max_positions": 25,
    "email_group": "nick",
    "email_recipients": ["you@example.com"]
  },
  {
    "name": "trust",
    "label": "Family Trust",
    "fidelity_positions": "/path/to/trust_positions.csv",
    "fidelity_account_filter": "Trust",
    "fidelity_creds_prefix": "LILICH",
    "purchase_history": "data/trust_purchase_history.json",
    "email_recipients": ["trustee@example.com"]
  }
]
```

### CSV Format (for `csv_path`)

```csv
ticker,shares,cost_basis,purchase_date
MSFT,100,555.23,2025-08-01
NVDA,5000,78.00,2023-06-15
```

---

## Per-Lot Tax Tracking

```json
{
  "NVDA": [
    {"shares": 1000, "cost_basis": 143.35, "purchase_date": "2025-06-16"},
    {"shares": 4000, "cost_basis": 44.50,  "purchase_date": "2023-06-15"}
  ],
  "MSFT": [
    {"shares": null, "cost_basis": 296.20, "purchase_date": "2021-08-01"}
  ],
  "SPAXX": []
}
```

Use `null` shares to assign all remaining shares to a single-lot holding. Set a ticker to `[]` to skip it from tax calculations (money market funds).

---

## Earnings Calendar

```json
{
  "2026-04-30": [
    {
      "ticker": "LLY",
      "time": "pre-market",
      "prev_close": 851.21,
      "guide": "Mounjaro+Zepbound combined vs. $9-10B threshold"
    }
  ]
}
```

```bash
financial-bytes add-earnings-event --date 2026-05-20 --ticker NVDA --time after-close \
    --guide "Data Center revenue vs. $73-75B guidance"

financial-bytes show-earnings-calendar --days 30
```

---

## Environment Variables

### Required

| Variable | Description |
|----------|-------------|
| `ANTHROPIC_API_KEY` | Anthropic API key |
| `MASSIVE_API_KEY` | massive.com API key |
| `DATABASE_URL` | SQLAlchemy connection string (default: `sqlite:///financial_bytes.db`) |
| `EMAIL_RECIPIENT` | Default email recipient |
| `EMAIL_FROM` | Sender address |
| `SMTP_HOST` | SMTP server (default: `smtp.gmail.com`) |
| `SMTP_USER` | SMTP username |
| `SMTP_PASS` | SMTP password (Gmail: 16-char App Password) |

### Fidelity Live Sync (optional)

| Variable | Description |
|----------|-------------|
| `FIDELITY_USERNAME` | Fidelity username |
| `FIDELITY_PASSWORD` | Fidelity password |
| `FIDELITY_2FA_SECRET` | Base32 TOTP secret from Fidelity Security Center |
| `FIDELITY_LILICH_USERNAME` | Trust portfolio Fidelity username |
| `FIDELITY_LILICH_PASSWORD` | Trust portfolio Fidelity password |
| `FIDELITY_LILICH_2FA_SECRET` | Trust portfolio TOTP secret |
| `FIDELITY_CHROME_BIN` | Windows Chrome binary (default: `C:\Program Files\Google\Chrome\Application\chrome.exe`) |
| `FIDELITY_DEBUG_PORT` | Chrome remote debug port (default: `9222`) |

### Schedule & Parallelism

| Variable | Default | Description |
|----------|---------|-------------|
| `PIPELINE_START_TIME` | `05:30` | Daily pipeline run time (HH:MM, 24h) |
| `NEWSLETTER_TIMEZONE` | `America/New_York` | Timezone for all scheduled jobs |
| `MAX_PARALLEL_TICKERS` | `8` | Concurrent scraper workers |
| `MAX_PARALLEL_ANALYSTS` | `12` | Concurrent Claude analyst calls |
| `SIGNAL_CACHE_TTL_HOURS` | `1` | Signal cache TTL |

### Alerts

| Variable | Description |
|----------|-------------|
| `DISCORD_WEBHOOK_URL` | Discord webhook for stop-loss alerts, earnings checks, reminders |

See `.env.template` for the full variable list.

---

## Scheduled Jobs

### Cron (`Dopple/Scripts/setup-cron.sh`)

| Schedule | Job |
|----------|-----|
| `@reboot` | Start APScheduler daemon (45s delay) |
| `5:50 AM` | Watchdog — restart if dead |
| `7:00 PM` weekdays | Fidelity sync via Plaid |

### APScheduler

| Schedule | Job |
|----------|-----|
| `PIPELINE_START_TIME` (5:30 AM) | Full pipeline for all portfolios |
| `6:00 AM` | Decision reminder Discord check |
| `7:10 AM` | Pre-market earnings check (days with pre-market events only) |

---

## CLI Reference

### `financial-bytes run`

```bash
financial-bytes run [OPTIONS]

Options:
  -p, --portfolio PATH          Portfolio CSV path
  -d, --date YYYY-MM-DD         Report date (default: today)
  --skip-scrape                 Use cached articles from DB
  --skip-email                  Generate newsletter, don't send
  --portfolio-name NAME         Portfolio from portfolios.json
  -r, --email-recipients EMAIL  Override recipients (repeatable)
```

```bash
# Run all portfolios
financial-bytes run

# Named portfolio, skip email
financial-bytes run --portfolio-name trust --skip-email

# Use cached articles
financial-bytes run --skip-scrape --portfolio-name nbossn_fidelity
```

### `financial-bytes fidelity-setup`

One-time manual login to save session cookies (~7 day TTL).

```bash
financial-bytes fidelity-setup --portfolio nbossn_fidelity
```

### `financial-bytes fidelity-sync`

Automated Fidelity positions download.

```bash
# Dry run — print holdings
financial-bytes fidelity-sync --portfolio nbossn_fidelity --dry-run

# Download and write CSV
financial-bytes fidelity-sync --portfolio nbossn_fidelity --output fidelity-holdings.csv

# Show browser window (debug)
financial-bytes fidelity-sync --portfolio nbossn_fidelity --no-headless
```

### Other Commands

| Command | Description |
|---------|-------------|
| `financial-bytes analyse [TICKERS...]` | Run analyst agents only (no scrape, no email) |
| `financial-bytes schedule` | Start APScheduler daemon |
| `financial-bytes ticker-report TICKER` | Deep-dive on any ticker (not in portfolio) |
| `financial-bytes check-stops` | Stop-loss threshold check + Discord alert |
| `financial-bytes check-dividends` | Dividend income projections |
| `financial-bytes add-reminder` | Time-gated decision reminder |
| `financial-bytes add-earnings-event` | Add event to earnings calendar |
| `financial-bytes show-earnings-calendar` | Show upcoming earnings |
| `financial-bytes track-performance` | Record daily P&L snapshot |
| `financial-bytes show-performance` | Historical performance chart |
| `financial-bytes earnings-check` | Map earnings results to portfolio actions |
| `financial-bytes portfolios` | List configured portfolios |
| `financial-bytes audit` | DB health check, cost audit, security scan |

---

## Fidelity Live Sync

`fidelity-setup` and `fidelity-sync` automate Fidelity portfolio exports without manual CSV downloads.

### How It Works

**DrissionPage CDP mode instead of Selenium WebDriver**

Selenium injects `navigator.webdriver = true` and registers a detectable CDP handshake signature that Fidelity's Akamai bot detection catches immediately. DrissionPage connects to Chrome's existing CDP socket directly — no WebDriver protocol, no `chromedriver.exe`. From Akamai's perspective, the traffic is indistinguishable from a developer tools session.

**Visual state machine**

At every navigation step the scraper takes a screenshot and sends it to Claude Haiku for classification into one of 9 states: `LOGIN`, `MFA_TOTP`, `MFA_OTHER`, `PORTFOLIO`, `POSITIONS_LOADING`, `POSITIONS_READY`, `BOT_CHALLENGE`, `ACCESS_DENIED`, `UNKNOWN`. The scraper reacts to what it sees, not what URL it expects. This handles Akamai interstitials, unexpected MFA variants, and UI changes transparently. Fallback: DOM inspection by element ID → URL heuristics.

**Chrome prefs patching**

Chrome 120+ ignores `--download-default-directory` when the profile has a saved download path. The scraper patches `Default/Preferences` directly before each Chrome launch, setting `download.default_directory` and `prompt_for_download = false`. Downloads land silently in `~/Downloads` without a Save dialog.

### WSL2 Mirrored Networking Requirement

Chrome binds its debug port to `127.0.0.1` only. With standard WSL2 NAT, `127.0.0.1` inside WSL is the Linux loopback — Chrome is unreachable. Enable mirrored networking once:

```ini
# C:\Users\<user>\.wslconfig
[wsl2]
networkingMode=mirrored
```

```powershell
wsl --shutdown
```

### Setup

**1. Add to `.env`:**

```bash
FIDELITY_USERNAME=your_username
FIDELITY_PASSWORD=your_password
FIDELITY_2FA_SECRET=BASE32_TOTP_SECRET   # from Fidelity Security Center
```

To get the TOTP secret: Fidelity → Profile → Security → Two-Factor Authentication → Authenticator App → Set Up → "Can't scan?" reveals the Base32 key.

**2. First run (optional manual setup):**

```bash
financial-bytes fidelity-setup --portfolio nbossn_fidelity
```

**3. Sync:**

```bash
financial-bytes fidelity-sync --portfolio nbossn_fidelity
```

### Akamai Rate Limiting

Blocked attempts back off exponentially (45s → 90s → 180s → 360s) and retry up to 4 times automatically. Run `--no-headless` to watch the browser during retries.

### Debug Screenshots

Every failure path saves a labelled PNG to `data/fidelity_debug/`. Open these to see exactly what state Chrome was in at the failure point.

---

## Parallelism & Performance

| Phase | Concurrency | Config |
|-------|-------------|--------|
| Phase 2 — Scrape | `ThreadPoolExecutor(MAX_PARALLEL_TICKERS)` | `MAX_PARALLEL_TICKERS=8` |
| Phase 3 — Signals | 10 outer × 4 inner HTTP calls | hardcoded |
| Phase 4 — Analysts | `asyncio.gather` + `Semaphore(MAX_PARALLEL_ANALYSTS)` | `MAX_PARALLEL_ANALYSTS=12` |

**Wall-clock estimate (25-ticker portfolio, defaults):** ~5–6 minutes total.

---

## Data Sources

| Source | Method | Data |
|--------|--------|------|
| Finviz | Selenium + requests + BeautifulSoup | News, fundamentals (P/E, EPS, margins, ROE, short float), SEC filings |
| Google News RSS | requests + defusedxml | Headlines |
| Yahoo Finance | requests + BeautifulSoup | Full article text |
| CNBC | requests + BeautifulSoup (Queryly API) | Full article text |
| MarketWatch | requests + BeautifulSoup | Snippets |
| Morningstar | requests + BeautifulSoup | Analysis |
| massive.com | REST API | Analyst ratings, price targets, technicals, Benzinga sentiment, real-time quotes |
| DuckDuckGo | requests (DDGS) | Fallback |

---

## Agents

| Agent | Model | Purpose |
|-------|-------|---------|
| Analyst | `claude-haiku-4-5` | Per-ticker BUY/HOLD/SELL with confidence, sentiment, catalysts, risks. Runs via `claude -p` subprocess, DB-cached per day. |
| Director | `claude-sonnet-4-6` | Portfolio synthesis — market theme, 5-min brief, action items. Reads analyst summaries from DB. |
| Fidelity page classifier | `claude-haiku-4-5` | Vision-based classification of Fidelity browser state during live sync. Falls back to DOM inspection. |

All analyst and director calls route through your Claude Code subscription via `claude -p`. The Fidelity classifier uses the Anthropic SDK directly (API key or Claude Code OAuth from `~/.claude/.credentials.json`).

---

## Cost Estimate

| Component | Model | Tokens/call (est.) |
|-----------|-------|-------------------|
| Analyst (per ticker) | Haiku 4.5 | ~4,000 in + ~600 out |
| Director (once) | Sonnet 4.6 | ~8,000 in + ~800 out |
| Fidelity classifier (per nav step) | Haiku 4.5 | ~500 in + ~30 out |
| **25-ticker pipeline** | | **~108K in + ~16K out** |

Run `financial-bytes audit` to see estimated costs from your actual DB call history.

---

## Security

- All secrets in `.env` (gitignored, `chmod 600`)
- Portfolio CSVs and `portfolios.json` gitignored
- Ticker symbols validated against `^[A-Z]{1,5}$` before use in URLs
- Portfolio names validated against `^[A-Za-z0-9_-]{1,64}$`
- **SSRF protection** — `is_safe_url()` resolves hostnames before any outbound request, blocking private IPs; DNS errors fail closed
- **WeasyPrint SSRF** — custom `url_fetcher` runs `is_safe_url()` before every PDF asset fetch (addresses CVE-2025-68616)
- **XML bomb protection** — Google News RSS parsed with `defusedxml`
- Jinja2 `SandboxedEnvironment` — prevents template injection
- GitHub token via `GIT_ASKPASS` — never in command args
- Fidelity session cookies stored at `chmod 600` — never committed
- Debug screenshots stored at `chmod 600` — contain portfolio data

---

## Development

```bash
make test        # Test suite
make lint        # ruff + black check
make format      # Auto-format
make migrate     # Run pending Alembic migrations
make logs        # Tail live log

# Debug run
financial-bytes --log-level DEBUG run --skip-email
```

---

## License

MIT
