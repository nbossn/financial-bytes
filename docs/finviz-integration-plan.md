# Finviz Integration — Site Map, Gap Analysis, Build Plan

> Supersedes the scope of `finviz-screener-plan.md` (kept for its diagnosis —
> the Cloudflare/DrissionPage findings there still hold and are the mechanism
> this whole plan builds on). That file was "how do we run the screener."
> This one is "what is actually on this site, and what should we build."
>
> Built from a guided tour Nick gave 2026-09-05 — 15 screenshots across every
> major section of financial-bytes' own DATA-SOURCES.md and METHODOLOGY.md
> reference "recommended feed / not yet built" for several signals; this
> catalogs which of those Finviz already answers for free, so nothing gets
> built twice.

---

## 1. Site map (confirmed live, 2026-09-05)

**Top nav:** Home · Screener · Maps · Charts · News · Groups · Insider ·
Futures · Forex · Crypto · Portfolio · Calendar

| Section | Sub-pages | What's there |
|---|---|---|
| **Home** | — | Index charts (S&P/NASDAQ/DOW/Russell), breadth (advancing/declining, new hi/lo, above/below SMA50/200), a **Signal** table (Top Gainers/Losers, New High/Low, Overbought/Oversold, Unusual Volume, Most Active, Upgrades/Downgrades, Earnings Before/After, Insider Buying/Selling), S&P 500 1-day treemap, technical pattern lists (trendlines/wedges/triangles/H&S), aggregated news + blogs |
| **Screener** | filter tabs: Descriptive/Fundamental/Technical/News/ETF/All; view tabs: Overview/Valuation/Financial/Ownership/Performance/Technical/Custom/Charts/Tickers/Basic/TA/News/Snapshot/Maps/Stats | The bulk cross-sectional query engine. 11,647 tickers, ~20/page. `Signal` dropdown surfaces the same Home-page lists (Unusual Volume, New High, etc.) as a first-class filter. Export link present (untested — likely Elite-gated on click). "Trades" filter explicitly marked Elite-only. |
| **Maps** | Map / Bubbles / Matrix; size-by Market Cap or Equal; filter by S&P500/Dow30/Nasdaq100/Russell2000/All/ETFs/Crypto/Futures/Themes | Treemap, sized/colored per the above. Nick's separate ask: a **portfolio-weighted** version of this in the newsletter — doesn't need Finviz at all, see §4. |
| **News** | Market News / Market Pulse / Stocks News / ETF News / Crypto News | Two-column aggregation (wire services + a blogs column) across many outlets in one feed, view-by-time-or-source. |
| **Groups** | Group-by: Sector / Industry / Industry-per-sector / Country / Cap-size. Order-by: ~25 fundamental/growth/valuation metrics. Same view-tab system as Screener, applied to aggregates. | Cap-weighted sector/industry fundamentals + multi-period performance (1M/3M/1Y charts shown). |
| **Insider** | Insiders / Managers / Funds | Insiders = live Form 4 feed (buy/sale/proposed-sale, minutes-fresh). Managers = 13F institutional holdings, drillable to one firm (sector allocation over time, quarterly-change treemap, top buys/sells, general stats). **13F lag caveat: report end date is quarter-end, filed up to 45 days later** — this is a 1-3 month old position, not a live signal. |
| **Futures** | Prices/Performance/Charts/Maps × Indices/Rates/Energy/Metals/Grains/Softs&Meats/Currencies | Full macro cross-section: Treasury yields, oil/gas, gold/silver/copper, ags, USD/majors — cleaner than the picker's current index-ETF-proxy overlay. |
| **Forex** | Prices/Performance/Charts | Major pairs + gold/crude/BTC, with a 1-day relative-USD-strength cross table. |
| **Calendar** | Economic / Earnings / Dividends | Economic: actual/expected/prior with impact-severity marker (ISM, JOLTS, ADP, Fed speeches, auctions). **Earnings sub-tab is the direct, clean fix for the Sept-2026 earnings-screen gap** (§4). |
| **Portfolio** | (not toured) | Finviz's own portfolio tracker — relevant only if we want Finviz to know Nick's holdings; separate decision from anything below. |
| **Per-ticker page** (`/stock?t=TICKER`) | Overview / Compare / Short Interest / Financials (Dividends/Earnings/Revenue/Forecast) / Options (Prices/Volatility&Greeks) / Latest Filings | See §2 — this is the single richest page on the site. |

**Confirmed: Nick is logged into a Finviz account** (`nick.bossn`, FREE tier,
Alerts bell, "My Presets" dropdown on Screener). Not yet decided whether the
scraper should run as this authenticated session — see §5.

## 2. Per-ticker page, in full (NVDA walkthrough)

- **Overview**: chart (candle, SMA20/50/200, draw tools) + a ~50-field stat
  grid (P/E, Fwd P/E, PEG, EPS this/next Y, ROE/ROIC, margins, short float,
  perf 1W→10Y, beta, ATR, RSI...) — this is close to what `finviz_data.py`
  already captures via the quote page's `requests`-based scrape, confirming
  that scrape doesn't need the browser-automation treatment.
- Below the fold, still on Overview: **GAAP EPS / Sales / Shares-outstanding**
  annual bar charts, an **analyst rating-change history** table (date, firm,
  action, old→new rating, old→new price target), a live scrolling **news
  feed** with per-article source attribution, business description,
  **institutional ownership** (top 10 holders, Managers/Funds toggle),
  **executive team**, and full **Income Statement / Balance Sheet / Cash
  Flow** (TTM + up to ~7 fiscal years annual, or quarterly toggle — **history
  beyond ~3 years is Elite-gated**, confirmed by the paywall lock shown in
  the FY2023-and-earlier columns).
- **Short Interest tab**: a genuine **time series** — short interest shares,
  short float %, and short ratio (days-to-cover), each charted over ~10
  months, plus a bi-monthly settlement-date history table. `finviz_data.py`'s
  own docstring already names this as "Phase 2 (documented, not yet built):
  scrape the short-interest HISTORY page (ty=si)" — it's confirmed to exist
  exactly as described.
- **Financials > Earnings**: **consensus EPS estimate + # of analysts +
  reported + surprise%**, per quarter, for both adjusted and GAAP EPS, plus a
  **Revenue** forecast section, plus beat-rate stats (100%/100%/100% for
  NVDA's last 8 quarters across EPS/GAAP EPS/Revenue) and a **Latest
  Revisions** panel (per-period estimate + up-revision count + down-revision
  count + revision date) with a chart of the estimate band vs. price over
  time.
- **Financials > Forecast**: analyst consensus (Strong Buy/Buy/Hold/Sell/
  Strong Sell counts + a 1.0-5.0 numeric score), Low/Avg/High price targets
  with % upside, and a historical chart of how the target band and price have
  moved together, plus a stacked-area view of the ratings mix over time.
- **Options tab**: full chain (calls/puts side by side, per-strike bid/ask/
  volume/OI, expiry selector) plus a separate "Volatility & Greeks" view (not
  toured in detail).
- **Compare** and **Latest Filings** tabs: not toured.

## 3. Gap analysis — what this closes in the existing picker

| Existing gap (source: `DATA-SOURCES.md` / `finviz_data.py` docstring) | Finviz page that answers it | Verdict |
|---|---|---|
| *"Consensus EPS / estimates... thin via yfinance... best fill: Finnhub or FMP"* (`DATA-SOURCES.md:30`) | Per-ticker **Financials > Earnings** | **Closed, free, no key.** Richer than what Finnhub's free tier was scoped to provide (adjusted AND GAAP, plus a revenue forecast in the same view). |
| *"analyst recommendation trends (revision momentum)... directly upgrades revision_proxy"* (`DATA-SOURCES.md:52,54`) | Per-ticker **Financials > Earnings**, "Latest Revisions" panel | **Closed, free, no key.** Actual up/down revision counts per period, not just a coarse trend line. |
| Squeeze score built on a **point-in-time** short float/ratio snapshot | Per-ticker **Short Interest** tab | **Upgradeable.** Trend (rising/falling short interest) is a different, likely stronger signal than a static number — this was already flagged as unbuilt in the module's own docstring. |
| Global macro overlay = yfinance index-ETF proxies only | **Futures** + **Forex** pages | **Upgradeable.** Direct Treasury yields, energy, metals, FX majors instead of proxying through index ETFs. |
| Sector overlay = yfinance sector-ETF returns | **Groups** page | **Upgradeable.** Real cap-weighted sector/industry fundamentals + multi-period performance, not just a price return. |
| Candidate universe = yfinance sector-deviation scan only | **Screener** (already being built, `finviz_screener.py`) + Home page **Signal** lists | **In progress.** Screener module works end-to-end for at least one filter (see `finviz-screener-plan.md` §0). Home-page signal lists (Unusual Volume, New High, Insider Buying, Upgrades) are a cheaper alternative to a custom screener filter for the common cases — worth checking if they're reachable without the full screener machinery. |
| September earnings-date screen (2026-09-03 ask) needed manual ticker curation | **Calendar > Earnings** | **Closed, pending scrape.** A real earnings calendar, no more piecing dates together per-ticker via `yfinance.get_earnings_dates`. |
| No insider-13F / institutional-flow signal anywhere in the picker | **Insider > Insiders / Managers / Funds** | **New capability**, not a gap-fill. Ship with the 13F-lag caveat attached wherever it's surfaced (see §1's Insider row) — a same-quarter accumulation pattern is real, "smart money bought this today" is not a claim this data can support. |
| Options signal = ATM IV / put-call / event premium only (`options_data.py`) | Per-ticker **Options** tab, full chain | **Upgradeable**, lower priority — current signal is already reasonable; a full chain mainly helps if a real options strategy (spreads, skew) gets built later. |
| Multi-scraper news gathering (`src/scrapers/`) | **News** page, aggregated | **Possible consolidation**, not urgent. Worth a look only if the per-site scrapers are proving fragile; not a signal-quality gap today. |

## 4. Separate from all of the above: the portfolio treemap

Nick's Maps-page ask is a **build**, not a Finviz integration — a treemap
sized by **portfolio weight** (not market cap), colored by day change,
grouped by sector, in the daily brief. Every input already exists in
`financial-bytes`: holdings + weights (Fidelity export → `PortfolioSnapshot`),
day-change per ticker (already fetched for the newsletter), sector groupings
(already computed in `src/stockpicker/sectors.py`). Needs a treemap renderer
(e.g. `squarify` + matplotlib, or a small D3/Plotly embed if the newsletter
already supports embedded HTML) and a slot in the newsletter template. No
Cloudflare, no browser automation, no dependency on anything else in this
document — could ship independently and first if a quick visual win is
wanted.

## 5. Open decisions before building further

1. **Authenticated vs. anonymous scraping session.** Nick's screenshots show
   a logged-in Finviz account. A persisted, authenticated session (cookie jar
   in the stealth Chrome profile, same pattern as the Fidelity scraper's
   per-credential cookie storage) may clear Cloudflare more reliably and
   unlocks "My Presets" / Alerts. Needs Nick's call on whether to store his
   Finviz credentials the way `fidelity_scraper.py` stores brokerage ones
   (`.env`, gitignored, TOTP if Finviz has 2FA) — not assuming that
   unilaterally.
2. **Per-page Cloudflare behavior is unverified beyond the Screener.** The
   diagnosis in `finviz-screener-plan.md` (plain `requests` OK for quote
   pages, challenged for the screener) doesn't tell us what Groups, Insider,
   Calendar, Futures, Forex, or the per-ticker Financials/Short-Interest/
   Options tabs do — some may render fine with plain `requests` (extending
   `finviz_data.py`'s existing approach, cheap), others may need the
   `finviz_screener.py` browser (expensive, one Chrome launch per page-type).
   This needs a quick per-page check before committing engineering effort.
3. **Priority order** — proposed, pending Nick's sign-off:
   1. Finish `finviz_screener.py` per the existing plan (parser done, decide
      headless-vs-window, wire named presets) — already in flight.
   2. Consensus EPS + revision momentum (per-ticker Financials > Earnings) —
      highest signal value, directly closes two named methodology gaps.
   3. Calendar > Earnings — replaces the manual/yfinance-guesswork earnings
      screen entirely.
   4. Short-interest trend — upgrades an existing signal cheaply once the
      per-ticker page mechanism exists from #2.
   5. Groups + Futures/Forex macro overlay — real upgrade, lower urgency than
      signal-level data.
   6. Portfolio treemap (§4) — independent, can happen any time, possibly
      first as a quick standalone win.
   7. Insider (13F + Form 4 feed) — new capability, needs its own signal
      design (how does a lagged 13F actually enter a composite score?)
      before it's worth building the scrape.
   8. Options full chain, News aggregation — lowest priority; current
      coverage is adequate.

---

## 7. 2026-09-05 overnight build — what actually got shipped

Per Nick's live Discord authorization (2026-09-05 00:26 EDT) for full
autonomy to build a Finviz scraping framework. All of this is additive and
uncommitted (untracked/new files, or edits that only add functions) — nothing
in `run.py`/`sectors.py`/`generator.py`/the nightly call path was touched.

**Correction, block 18: block 17's correction (below, struck) was itself
wrong, and it deleted real work because of it.** The dev-agent that flagged
"a second, independent process" was right. That process is Nick's own live
interactive Claude Code terminal (`tmux` session `dopple-interactive`,
running continuously since 2026-09-02, still attached) — it doesn't appear
in `daemon-2026-09-05.log` or as a `dopple-overnight` pane because it isn't
a daemon block, so block 17's check answered a different question than the
one that mattered. Recovered from that session's own transcript:
`finviz_ticker_deep.py` was written and edited 04:33:52–04:36 UTC and was
still that session's live, self-validated (5 tickers, per its own Discord
report) work when block 17 deleted it at 04:56:59 UTC. Restored
byte-faithfully in `Projects/stock-picker/finviz-exploration-2026-09-05.md`'s
block-18 correction section; full account and the still-open
consolidation decision (Nick's, not a block's) are there. ~~Struck below:~~

~~the dev-agent that ran this task reported "a second, independent process
worked this same mandate concurrently tonight," citing a duplicate module
(`finviz_ticker_deep.py`) as evidence. Checked against the daemon log and
`tmux list-sessions`: only one session block ran tonight (block 17, single
`dopple-overnight` pane) — there was no second process. Every file's mtime
is consistent with one continuous run that built `finviz_ticker_deep.py`
first, then re-derived the same functionality directly in `finviz_data.py`
later without noticing the earlier file. `finviz_ticker_deep.py` has since
been deleted (confirmed unimported anywhere first); `finviz_data.py`'s
version is the surviving, canonical implementation.~~

### `src/stockpicker/finviz_driver.py` (new)

Extracted `_make_driver`/`_quit_driver`/`_wait_past_cloudflare`/the stealth
JS script/profile-port config out of `finviz_screener.py` (same port 9223,
same profile name, Fidelity's 9222 untouched). `finviz_screener.py` now
imports these instead of defining them locally. Verified behavior unchanged:
`run_preset("new_52w_high", max_rows=20, headless=False)` still returns the
same 5 sample tickers already on record (NX, PDEX, TITN, MUG, HSCS), 20/20
rows parsed.

### `src/stockpicker/finviz_data.py` (extended)

Added `fetch_short_interest_history`, `fetch_earnings_history`,
`fetch_forecast`, `fetch_options_chain`, `fetch_home_signals`. First four are
browser-based (via `finviz_driver.py`) — **correcting an assumption in §6
above and in this session's own architecture proposal**: the Short
Interest/Earnings/Forecast/Options tabs on the modern `/stock?t=...` page
are React-rendered client-side, not server-rendered like the Overview tab.
Confirmed by grepping a plain-`requests` response for "Latest Revisions" /
"Settlement Date" / "Strong Buy" / "Open Int." — zero hits every time,
despite a real 200 OK page. Validated live:
- Short interest: NVDA and MSFT both return 159 settlement-date rows
  (biweekly, ~6yr — deeper history than §2's "~2 years" estimate).
- Earnings: NVDA 12 quarters (8 reported + 4 forward), 100% beat rate on
  EPS/GAAP EPS/Revenue (8/8), Latest Revisions panel (Q3 '26: est 2.47,
  36/40 analysts revised up, 2 down).
- Forecast: NVDA 68 analysts (59 Strong Buy/6 Buy/2 Hold/0 Sell/1 Strong
  Sell, consensus score 1.21), targets $180.00/$334.32/$710.29 low/avg/high
  — matches §6's numbers from Nick's own screenshot walkthrough exactly.
- Options: NVDA nearest expiry (09/09/2026), 63 real strike rows both sides.
- Home signals (plain `requests`, no browser at all — see §7's breadth-first
  table below): 38 rows across all 13 Signal categories in one fetch.

One real bug caught and fixed before trusting output: the Forecast ratings
regex for `"Buy: (\d+)"` matched *inside* `"Strong Buy: 59"` (a real
substring, not a false anchor), returning `buy: 59` instead of the correct
`6`. Fixed with a negative lookbehind; caught by checking the number against
this doc's own §6 record (Buy: 6), not by the code running without error.

### `src/stockpicker/finviz_insider.py` (new)

Insiders feed (`fetch_insiders`), Managers list (`fetch_managers`), one
manager's drill-down (`fetch_manager_detail`) — reusing §5's exact recon
(real URLs, real row markup) rather than re-discovering it, plus the
drill-down page's markup, captured live for the first time tonight. Validated
live: 200 insider rows, 12 manager cards, and General Statistics/Top
Buys/Top Sells for Susquehanna International Group ($1,284.00B AUM, +43.73%
last quarter; top buy MU Call +$30.51B, top sell SPY Put -$11.74B). Sector
Allocation is a `recharts` SVG with no legend/table in the DOM — not
scrapable as structured data, logged as not built rather than faked.
**BlackRock (Nick's own demoed example) was NOT among the 12 manager cards
this session's page load returned** — the parser is generic to any
`{slug}-{cik}` URL, but this specific name wasn't the one used to validate
it; may need pagination/search to reach.

### Breadth-first pass: Groups, Calendar, Futures, Forex, News, Home

| Section | Fetch mechanism | Status |
|---|---|---|
| Groups | Browser, needs `v=110` (or another explicit view) — bare `/groups` never populates | Real markup captured (11 sector rows, `styled-row` class, same as the bulk screener), not built into a parser |
| Calendar (economic) | Browser required | Real markup captured (Date/Release/Impact/For/Actual/Expected/Prior/Alerts + a real event row), not built |
| Futures | Browser required, tile/heatmap layout not a table | Real values confirmed (Crude Oil WTI 91.22), not built |
| Forex | Browser required, same tile layout | Real values confirmed (EUR/USD tile, clean ticker via `data-boxover-ticker`), not built |
| News | Browser required, feed did not populate this session | Genuinely incomplete — DOM contained only unrendered JS templates after a 5s wait |
| Home | **Plain `requests`, no browser** | **Built** (`fetch_home_signals`) — the single biggest surprise of the night: this is the ONLY section on the entire site tonight that needed no Cloudflare/browser handling at all, yet contains 13 of the same signal categories (Unusual Volume, New High/Low, Insider Buying, Upgrades, ...) that `finviz_screener.py`'s presets pay the full browser cost to reach. Worth comparing coverage before deciding which path the picker should actually call. |

### Open, unresolved

1. **`finviz_data.py` vs `finviz_ticker_deep.py`** — 🔴 **STILL OPEN, this
   "resolved" line was wrong.** Block 17's deletion of `finviz_ticker_deep.py`
   (described above) rested on a false premise — it checked for a second
   *daemon* process and found none, but missed that Nick's own live
   interactive session (a separate, human-driven tmux pane) had independently
   built and validated that exact file minutes earlier, and the daemon's
   later write to a *different* file (`finviz_insider.py`) had already
   silently raced and overwritten part of that session's work. Block 18
   (2026-09-05, see the vault's `finviz-exploration-2026-09-05.md`
   "CORRECTION" section for the full incident) restored
   `finviz_ticker_deep.py` byte-faithfully from that session's own tool-call
   transcript and reopened this as Nick's decision — it is **not** resolved,
   and both files are still on disk as of 2026-09-07.
   **New evidence for the decision** (2026-09-07,
   `Projects/stock-picker/finviz-testset-validation-2026-09-07.md`):
   `finviz_ticker_deep.py`'s forecast parser has a real bug —  its
   dollar-anchored regex breaks on Finviz's `K`-notation for high-priced
   tickers (confirmed on EME, $754/share) and silently returns `None` for
   `avg_target`/`high_target`. `finviz_data.py`'s parallel implementation
   does not have this bug (uses a suffix-generic numeric parser already used
   for market cap). Not fixed in either file — reported as one more data
   point favoring `finviz_data.py`, not a unilateral resolution.
2. Authenticated vs. anonymous Finviz session — still undecided, per §5.
3. Groups/Calendar/Futures/Forex/News — real markup captured, no parser
   written; a reasonable next-session starting point given the markup is
   now on record rather than needing rediscovery.
4. Sector Allocation on a manager's drill-down page — chart-only, not
   scrapable without simulating hover/reverse-engineering chart data props.

---
*Written 2026-09-05, from a live guided tour (15 screenshots) plus a direct
re-check of `DATA-SOURCES.md`'s exact wording before citing it as a gap this
closes. §3's "Closed" verdicts are now partially shipped (see §7) rather than
purely a confirmed-available inventory. §5's authenticated-session question
remains Nick's call.*
