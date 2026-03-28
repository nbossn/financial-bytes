# Financial Bytes

Automated daily stock portfolio newsletter powered by AI agents. Scrapes financial news, analyzes articles per holding, and delivers a 5-minute executive brief to your inbox before market open.

## What It Does

1. **Scrapes** financial news from Finviz, Yahoo Finance, CNBC, MarketWatch, Morningstar, Reuters, and Seeking Alpha for each stock in your portfolio
2. **Fetches** structured news, analyst ratings, technical indicators, and price targets from massive.com
3. **Analyzes** articles per ticker using Claude Haiku (cost-optimized) to produce summaries and BUY/HOLD/SELL recommendations with narrative context
4. **Synthesizes** all findings using Claude Sonnet into a portfolio-level director's report with prioritized action items
5. **Generates** a formatted HTML/Markdown newsletter with portfolio P&L, market pulse, and per-stock analysis cards
6. **Delivers** via Gmail to your inbox by 7:30 AM ET, before NYSE open

## Pipeline Timeline (Daily, ET)

| Time | Stage |
|------|-------|
| 5:00 AM | Orchestrator starts, portfolio + prices loaded |
| 5:05–5:30 AM | Web scrapers + massive.com API data collection |
| 5:30–6:30 AM | Financial Analyst Agent (per ticker, haiku-4-5) |
| 6:30–7:00 AM | Financial Director Agent (synthesis, sonnet-4-6) |
| 7:00–7:20 AM | Newsletter generation (HTML + MD + PDF) |
| 7:20–7:30 AM | Email delivery + GitHub archive commit |

## Setup

### 1. Clone & Install

```bash
git clone https://github.com/nbossn/financial-bytes.git
cd financial-bytes
make install
```

### 2. Configure Environment

```bash
cp .env.template .env
# Edit .env with your API keys and email credentials
```

Required keys:
- `ANTHROPIC_API_KEY` — [console.anthropic.com](https://console.anthropic.com)
- `MASSIVE_API_KEY` — [massive.com](https://massive.com)
- `DATABASE_URL` — PostgreSQL connection string
- Gmail SMTP credentials (use an App Password, not your main password)

### 3. Create Portfolio File

Create `portfolio.csv` (gitignored — stays local):

```csv
ticker,shares,cost_basis,purchase_date
MSFT,100,555.23,2025-08-01
NVDA,200,206.45,2025-11-01
```

### 4. Initialize Database

```bash
make migrate
```

### 5. Test Run

```bash
make test-newsletter
```

Generates a test newsletter using your portfolio without sending email.

### 6. Start Scheduler

```bash
make run
```

Runs the daily 5 AM ET pipeline in the background.

## Manual Commands

```bash
make run-once        # Run full pipeline now
make scrape          # Scrape articles only
make analyze         # Run analyst agent on existing articles
make newsletter      # Generate newsletter from existing analysis
make audit           # Run fullstack security + cost audit
make test            # Run test suite
make logs            # Tail live logs
```

## Architecture

```
Portfolio CSV → Orchestrator → [Scrapers + massive.com API] → DB
    → Analyst Agent (per ticker, haiku-4-5)
        → Director Agent (synthesis, sonnet-4-6)
            → Newsletter Generator (HTML + MD + PDF)
                → Email + GitHub Archive
```

## News Sources

| Source | Method | Notes |
|--------|--------|-------|
| Finviz | Selenium | News links per ticker |
| Yahoo Finance | Playwright | Articles + video transcripts |
| CNBC | Playwright | Full articles |
| MarketWatch | Playwright | Pre-paywall snippets |
| Morningstar | requests + BeautifulSoup | Full analysis pages |
| Reuters | requests + BeautifulSoup | Replaces Bloomberg/Barrons |
| Seeking Alpha | requests | Free tier snippets |
| massive.com | REST API | Benzinga news + sentiment |

## Cost Estimate

| Component | Model | Est. Daily Cost |
|-----------|-------|----------------|
| Analyst Agent | claude-haiku-4-5 | ~$0.02–0.15 |
| Director Agent | claude-sonnet-4-6 | ~$0.05–0.20 |
| massive.com API | — | Varies by plan |
| **Total** | | **~$0.07–0.35/day** |

## Security

- All API keys stored in `.env` (gitignored)
- `portfolio.csv` gitignored — stays local
- Weekly automated security scan via fullstack agent
- No hardcoded credentials anywhere in source

## Agents

| Agent | Model | Frequency | Purpose |
|-------|-------|-----------|---------|
| Analyst | claude-haiku-4-5 | Daily per ticker | Article summarization + per-stock recommendation |
| Director | claude-sonnet-4-6 | Daily once | Portfolio synthesis + executive brief |
| Fullstack | claude-haiku-4-5 | Weekly | DB audit, cost review, security scan, GitHub sync |
