# Ticker Deep-Dive Report

The `ticker-report` command runs a comprehensive, standalone analysis on any publicly traded stock — no portfolio required. It chains four AI agents and multiple data sources into a single markdown + HTML report.

---

## Quick Start

```bash
# Ensure .env is configured (see local setup below)
PYTHONPATH=. .venv/bin/python -m src.cli ticker-report FIG

# Or via Make
make ticker-report TICKER=FIG

# Use cached articles (no re-scrape)
PYTHONPATH=. .venv/bin/python -m src.cli ticker-report FIG --skip-scrape

# Specify a date
PYTHONPATH=. .venv/bin/python -m src.cli ticker-report NVDA --date 2026-04-01
```

**Output:** `newsletters/ticker-reports/<TICKER>-<DATE>.md` and `.html`

---

## Local Setup (no Postgres, no email required)

```bash
git clone https://github.com/nbossn/financial-bytes
cd financial-bytes

# 1. Copy the local env template
cp .env.local .env

# 2. Add your API keys to .env:
#    ANTHROPIC_API_KEY=sk-ant-api03-...
#    MASSIVE_API_KEY=...  (optional — signals gracefully degrade without it)

# 3. Install dependencies + initialize SQLite database
make setup-local

# 4. Run a report
make ticker-report TICKER=AAPL
```

The local setup uses **SQLite** (no Postgres needed) and skips email delivery automatically.

---

## Pipeline Architecture

```
ticker-report FIG
       │
       ▼
[1] Scrape          Finviz + Google News + MarketWatch + CNBC + Yahoo (15 articles)
       │
       ▼
[2] Market Signals  Massive.com (RSI, technicals) + Finviz deep scrape
       │             ├── Full snapshot table (80+ fields: P/S, EV/Sales, margins, ownership)
       │             ├── Analyst rating changes (16 entries for FIG)
       │             └── Insider trades (up to 30 most recent transactions)
       │
       ▼
[3] Quant Metrics   Yahoo Finance 1-year price history → computed locally (no API cost)
       │             ├── Beta vs SPY
       │             ├── Jensen's Alpha (annualized)
       │             ├── Sharpe & Sortino ratios
       │             ├── Annualized return & volatility
       │             ├── Max drawdown & current drawdown
       │             └── 1M / 3M / 6M momentum
       │
       ▼
[4a] Analyst Agent  Claude Haiku — BUY/HOLD/SELL, sentiment, catalysts, risks
       │
       ▼
[4b] Quant Agent    Claude Sonnet — interprets quant metrics, flags anomalies
       │             ├── Risk profile (Very Low → Very High)
       │             ├── Return quality (Exceptional → Poor)
       │             ├── Momentum signal + RSI interpretation
       │             ├── Short squeeze risk (float + days-to-cover)
       │             ├── Insider signal (buying/selling pattern)
       │             └── Fair value note (PEG / P/S vs growth)
       │
       ▼
[5] MD Agent        Claude Sonnet — trade plays with entry/target/stop/structure
       │             ├── Overall stance (Aggressive Long → Aggressive Short)
       │             ├── 2–4 specific trade plays per report
       │             │    ├── Long Equity
       │             │    ├── Bull/Bear Call Spread
       │             │    ├── Cash-Secured Put
       │             │    ├── Put Hedge / Protective Put
       │             │    └── Straddle / Collar
       │             ├── Key support / resistance / breakout levels
       │             ├── Insider warning (if distribution pattern detected)
       │             └── Position management guidance
       │
       ▼
Report             Markdown + HTML written to newsletters/ticker-reports/
```

---

## Data Sources Per Report

| Source | Data Collected | Method |
|--------|---------------|--------|
| **Finviz snapshot** | 80+ fields: P/E, P/S, EV/EBITDA, margins, ROE/ROIC, short float/ratio/interest, insider %, volatility, ATR, SMA, RSI, price targets | requests + BeautifulSoup |
| **Finviz analyst ratings** | Last 16 rating changes: date, analyst, action, rating change, price target | Parsed from `js-table-ratings` table |
| **Finviz insider trades** | Up to 30 recent transactions: name, role, date, type, shares, price, value | Parsed from quote page insider table |
| **Finviz news** | 18 article links with full body extraction | Selenium (headless Chrome) |
| **Google News** | 20 article headlines and snippets | RSS feed via requests |
| **Yahoo Finance** | Historical daily prices (1 year) for quant computation | Public v8 chart API |
| **Massive.com** | RSI, MACD, technicals (requires paid plan for quote/snapshot data) | REST API |

---

## The Four Agents

### 1. Senior Analyst Agent (`claude-haiku-4-5`)
Reads scraped articles + fundamental data and produces:
- **Recommendation:** BUY / HOLD / SELL
- **Confidence:** 0–100%
- **Sentiment:** Very Bullish → Very Bearish with numeric score (-1.0 to +1.0)
- **Summary:** 3–4 sentence investment thesis grounded in news data
- **Key Catalysts** and **Key Risks** (bullet lists)
- **Analyst consensus** and **price target** from Finviz ratings

### 2. Quantitative Analyst Agent (`claude-sonnet-4-6`)
Interprets the computed quant metrics in the context of fundamentals and insider activity:
- **Risk profile** with rationale (cites specific metrics)
- **Return quality** based on Sharpe/Sortino thresholds
- **Beta interpretation** — what the market sensitivity means for a portfolio
- **Alpha interpretation** — is the stock generating return above what its beta justifies?
- **Momentum signal** — Strong Uptrend / Uptrend / Neutral / Downtrend / Strong Downtrend
- **Short squeeze risk** — based on float, days-to-cover, and recent price action
- **Insider signal** — buy/sell pattern assessment
- **Fair value note** — P/S vs growth rate, PEG if available
- **Quant flags** — anomalies like negative alpha, recent IPO data limitations, extreme drawdowns

### 3. Managing Director Agent (`claude-sonnet-4-6`)
Synthesizes all data into actionable trade plays:
- **Overall stance** with conviction level
- **MD thesis** — overarching view from a trading desk perspective
- **Trade plays** (2–4), each with:
  - Play type (equity, options structure)
  - Time horizon (short-term <1mo, swing 1-3mo, position 3-12mo)
  - Specific entry price or trigger condition
  - Price target and stop-loss level
  - Exact options structure if applicable (strikes, expiry, debit/credit)
  - Risk/reward ratio
  - Position sizing recommendation
  - Per-play conviction (High / Medium / Low)
- **Key levels** — support, resistance, breakout trigger
- **Insider warning** — explicit flag if executive distribution is a concern
- **Position management** — when to add, scale, or exit

---

## Report Output

Reports are written to `newsletters/ticker-reports/` as both `.md` and `.html`:

```
newsletters/
└── ticker-reports/
    ├── FIG-2026-04-01.md
    └── FIG-2026-04-01.html
```

### HTML Report Features
- Dark-mode, responsive layout
- Color-coded by MD stance (green for Long, red for Short, amber for Neutral)
- All three agents' output in one scrollable page
- Tables for quant metrics and trade plays
- Blockquote-styled insider warnings

---

## Sample Output Summary (FIG, April 1 2026)

```
Senior Analyst:   BUY  |  57% confidence  |  Bullish (+0.38)
Price Target:     $35.25 (analyst consensus)

Quant:            Beta 1.856  |  Alpha -99.4%  |  Sharpe -1.07
                  Volatility 87.7%  |  Max Drawdown 83.5%
                  Risk: Very High  |  Momentum: Strong Downtrend
                  Short Squeeze: Moderate (15.4% float short)
                  Insider Signal: Bearish (11 sales, 0 buys)

MD Stance:        Cautious Long  |  Conviction: Low

Play 1:  Long Equity (swing)           Entry $20.00–20.50 | Target $28 | Stop $18.75  | R/R 1:2.8
Play 2:  Bull Call Spread $22/$30      90 days, ~$2.00 debit              | R/R 1:3.6
Play 3:  Cash-Secured Put $18 strike   30-35 days, ~$1.05 credit          | 5-6.5% yield
Play 4:  Put Hedge $19 strike          45 days, insurance on long          | Defined risk

⚠  Insider Warning: Zero buys vs 11 sales totaling $19.3M — do not size up
```

---

## Finviz Data Fields Extracted

The snapshot table parser captures all 80+ Finviz fields, including fields that were previously broken due to compound formatting:

| Field | Example Raw Value | Parsed Value |
|-------|-----------------|--------------|
| `52W High` | `142.92-85.71%` | `142.92` |
| `52W Low` | `19.823.03%` | `19.82` |
| `Volatility` | `6.65% 6.77%` | `week=6.65, month=6.77` |
| `Sales past 3/5Y` | `44.61%-` | `44.61` |
| `ROIC` | `-83.89%` | `-83.89` |
| `EV/Sales` | `8.57` | `8.57` |
| `Short Interest` | `28.39M` | `"28.39M"` (string) |
| `Option/Short` | `Yes / Yes` | `"Yes / Yes"` (string) |
| `Analyst Recom` | `2.54` | `2.54` (1=Strong Buy, 5=Strong Sell) |

---

## Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `ANTHROPIC_API_KEY` | **Yes** | Powers all three AI agents |
| `MASSIVE_API_KEY` | Optional | Adds RSI/MACD technicals; degrades gracefully |
| `DATABASE_URL` | **Yes** | `sqlite:///financial_bytes.db` for local dev |
| `EMAIL_RECIPIENT` | No* | Only needed for full `run` pipeline |
| `SMTP_USER` / `SMTP_PASS` | No* | Only needed for full `run` pipeline |

\* `ticker-report` never sends email — these can be set to any placeholder value.

---

## Cost

`ticker-report` uses three model calls per run:

| Agent | Model | Typical Tokens | Est. Cost |
|-------|-------|---------------|-----------|
| Analyst | `claude-haiku-4-5` | ~4k input + 400 output | ~$0.002 |
| Quant | `claude-sonnet-4-6` | ~2k input + 600 output | ~$0.009 |
| MD | `claude-sonnet-4-6` | ~4k input + 1200 output | ~$0.020 |
| **Total per run** | | | **~$0.03** |
