# Finviz Bulk Screener — Diagnosis + Plan

> Status: **row parsing fixed and validated end-to-end (2026-09-04, later block).
> Not wired into anything nightly — still local dev/test only.**
> Nick's ask: stop scripting single-ticker requests to Finviz for things the
> *screener* already answers for free — short-float/RSI/unusual-volume/earnings-date
> style cross-sectional filters — and make that capability available to the
> stock-picker instead of hand-picking candidate tickers.

## 0. Resolved (2026-09-04, later block): §2's third hypothesis was correct

Captured a fresh post-Cloudflare page and grepped it directly for `ORCL` (per
this file's own instruction) instead of guessing at a new regex. Finding: the
screener's row links changed from `quote.ashx?t=TICKER` to
`stock?t=TICKER&ty=c&p=d&b=1` — a real markup change, not a render-timing or
filter-code problem (the other two hypotheses in §2 are ruled out; Cloudflare
clears fine and `earningsdate_thismonth` returns genuine matches).

Fixed in `finviz_screener.py`:
- `_wait_for_rows`'s detection regex and `_parse_result_page`'s row selector
  now match both the old and new href forms (`_ROW_LINK_RE`), so a partial
  rollout or future revert on Finviz's side doesn't retrigger this.
- A second, separate bug surfaced once real rows parsed: the ticker `<td>`
  also contains a one-letter logo-fallback span (e.g. "O" for ORCL) inside a
  `class="company-ticker"` anchor, which would have glued onto the ticker
  text via the generic `td.get_text()` cell mapping. That anchor is now
  stripped from the row before reading cell text.

Validated live: `run_screen(filters=["earningsdate_thismonth"],
sort="-marketcap", max_rows=45)` returns 45 unique, correctly-parsed tickers
across 3 paginated pages (20+20+5) — this also confirms pagination, which §3
Phase 2 had listed as "untested." Non-headless Chrome via the existing
stealth recipe still clears Cloudflare instantly; headless is still untested
(§3 Phase 1's open item stands as-is).

---

## 1. What's already proven, not guessed

Three real, reproducible facts, each found by testing the actual mechanism
rather than assuming a cause:

1. **Plain `requests` (what `finviz_data.py` already uses for per-ticker
   quote pages) gets a real 200 OK screener page — with zero result rows.**
   The screener's result table is populated by client-side JavaScript after
   load; a static HTTP fetch never triggers it. This is why the per-ticker
   path has worked flawlessly for months and the bulk path returned nothing —
   they need fundamentally different fetch mechanisms, not a better filter code.
2. **A plain headless Chrome hitting the same URL gets Cloudflare's
   "Just a moment... Performing security verification" challenge page
   instead of Finviz's HTML at all** (confirmed via `--dump-dom`: the page's
   own `_cf_chl_opt.cType` is `'non-interactive'`). It sat on that page for
   30+ seconds without clearing.
3. **A real (non-headless) window clears the same challenge instantly** —
   confirmed live: `title` was already the real screener title at the first
   check, versus headless which never cleared in 20 checks over 30s. This
   matches `fidelity_scraper.py`'s own default (`headless: bool = False`) —
   that wasn't an arbitrary choice, it's a real, working constraint against
   this exact class of bot detection, on a harder target (Akamai, gating a
   live brokerage login).

A fourth, smaller bug was found and already fixed in the new module
(`src/stockpicker/finviz_screener.py`, written but not yet validated
end-to-end): `ChromiumOptions()` defaults to expecting a non-headless
browser, and when the real browser doesn't match, DrissionPage "fixes" the
mismatch by calling `self.quit()` on the browser before reconnecting — which
kills the only browser `existing_only()` mode is allowed to talk to, so the
reconnect fails with a message ("confirm the browser has started") that
doesn't describe the actual cause. Fix: call `opts.headless(headless)`
explicitly so the declared and actual state always match.

## 2. What's still open (the actual next step, not yet done)

With Cloudflare cleared (non-headless) and the connection bug fixed, the
**last** test run returned a real ~430KB page (vs. the ~29KB Cloudflare
holding page and the ~207KB static-request page) but **still showed zero
`quote.ashx?t=TICKER` links** by the same regex that works fine on Finviz's
per-ticker quote pages. Three live hypotheses, not yet distinguished:

- The filter code `earningsdate_thismonth` doesn't exist / isn't what's
  echoed back into the page correctly, so the *real*, cleared page is
  showing zero genuine matches (a data question, not a scraping question).
- The results table markup changed to something that doesn't use a plain
  `quote.ashx?t=` href at all anymore (needs inspecting the real rendered
  HTML directly — grep for a known ticker symbol expected to be in the
  results, not for the URL pattern).
- The page cleared Cloudflare but the results table itself is a *separate*
  slower client-side render that hadn't finished by the time the HTML was
  captured (same class of problem as failure #1, one layer further in).

None of these are guesses to resolve by reasoning — the next session should
grep the actual captured HTML for a specific ticker known to report this
month (e.g. `ORCL`, confirmed from yesterday's per-ticker screen) and see
literally what markup surrounds it, before writing a new parser regex.

## 3. Plan — configure, implement, execute (for a later session)

**Phase 1 — Configure (mostly done)**
- [x] New, isolated module: `src/stockpicker/finviz_screener.py` — separate
  Chrome profile (`stockpicker_automation`) and debug port (`9223`, vs.
  Fidelity's `9222`) so this can never collide with or interfere with the
  live Fidelity brokerage automation.
- [x] Stealth launch recipe adapted from `fidelity_scraper.py::_make_driver`
  (same JS patches that already clear Akamai on a harder target).
- [x] `opts.headless(...)` fix applied (see §1).
- [ ] Confirm non-headless is genuinely required, or whether a longer wait /
  different stealth patch set lets headless clear it too — non-headless
  means a visible Chrome window opens on Nick's desktop each run, which is
  fine for an on-demand screen but worth knowing about before this becomes a
  nightly cron step.

**Phase 2 — Implement**
- [x] Inspect the real post-Cloudflare HTML directly (grep for a known ticker
  string, not the URL pattern) to find the actual current row markup, then
  fix `_parse_result_page()` to match it. Done — see §0.
- [x] Validate pagination (`&r=` offset) actually advances through result
  pages once single-page parsing works. Done — see §0 (45 rows, 3 pages, no
  dupes, no gaps).
- Decide the concurrency/session model: launch-and-teardown per screen (safe,
  slower, current default) vs. one long-lived browser reused across several
  screens in a single stock-picker run (faster, more state to manage). The
  `run_screen(..., page=...)` parameter already exists for the second mode;
  not exercised yet.
- Wire real filter presets the picker would actually use beyond earnings —
  Nick's own framing was "great ticker-based information," not just earnings
  dates. Concrete candidates already worth building as named presets:
  short-squeeze setups (short float + days-to-cover + relative volume —
  currently computed per-ticker in `finviz_data.squeeze_score` from a
  hand-picked candidate list; the screener could generate that candidate list
  instead of requiring one), unusual volume, new 52-week highs/lows, insider
  buying clusters, analyst upgrades this week.
- Decide where this plugs into the nightly picker: as a new, earlier stage in
  `sectors.py`'s candidate funnel (screener output feeds the ~55-candidate
  pool alongside or instead of the current yfinance sector-deviation scan),
  or as a separate on-demand tool (`/stock-picker screen ...`) for ad-hoc
  asks like the earnings-timing one from yesterday. These are not mutually
  exclusive, but the nightly-cron path needs the headless question in Phase 1
  resolved first — a visible Chrome window popping up during an unattended
  3 AM run is a real operational problem the Fidelity scraper doesn't have to
  solve (it runs on a scheduled weekday sync, not deep overnight).

**Phase 3 — Execute (once Phase 2 lands)**
- Re-run the September earnings screen through the new mechanism and diff
  against yesterday's hand-picked 39-ticker list — this is a real check on
  whether the screener catches names the manual list missed (it should catch
  `DOCU`/`LULU`, which yesterday's report flagged as "plausible but
  unconfirmed" precisely because per-ticker `yfinance` didn't have a
  confirmed forward date yet — Finviz's own earnings-date filter may resolve
  that gap directly).
  the fresh way and compare quality/hit-rate against the current
  sector-deviation-only candidate pool.
- Document the finished mechanism in `Projects/stock-picker/DATA-SOURCES.md`
  and `METHODOLOGY.md` (both already reference Finviz; this closes a gap
  those docs currently don't mention) and in this file's own status header.

## 4. Risk / cost notes for whoever picks this up

- Non-headless means a visible browser window — fine interactively, needs a
  decision before any unattended/nightly use (headless retest, a virtual
  display, or accepting the window and just not minimizing it).
- Each screen launch-and-teardown takes ~15-20s of overhead (Chrome cold
  start + Cloudflare clearing) even before parsing — cheap for an occasional
  ad-hoc ask, worth batching (one browser, several screens) if this becomes
  a regular multi-filter nightly step.
- This is a second, independent browser-automation surface next to the
  Fidelity one. Worth keeping genuinely isolated (separate profile/port, as
  built) rather than sharing state, given the Fidelity automation gates a
  live brokerage login and any instability there has real consequences.

---

## 5. Scope expansion, 2026-09-05: Nick's Finviz walkthrough

Nick walked through six Finviz sections live over Discord (screenshots +
his own description of why each is useful), asking for "deep research...
understand the website flow and full capabilities" before building further.
This is a scoping input, not a build order for all six — summarized here so
a later session doesn't have to re-derive it from the Discord log.

| Section | Nick's stated interest | Mapped opportunity |
|---|---|---|
| **Map** | "similar visuals in the daily reports specific to the stocks that I have in my portfolio and their relative sizes to portfolio percentage" | **Explicit, buildable now — no scraping.** Treemap of `portfolio.csv` positions sized by portfolio weight (not market cap), colored by day change. This is the one direct ask on the list. |
| **Insider → Managers** | Drilling into a specific manager (demoed live with BlackRock) surfaced a real idea — "makes me think the MRVL and KLAC plays are interesting" | Real signal feed: which large managers are rotating into names Nick holds/watches (sector allocation + top-buys/sells per manager), not insider disclosures in the abstract. |
| **Groups** | Sector overview/valuation/financial/performance in one view | Strictly richer than `sectors.py`'s current yfinance-only sector-deviation scan — same consumer, better input. |
| **News** | Market/stock/ETF/crypto feeds, viewable by time/source | Feed aggregation for the daily report's context section. Lower build cost, no unique data. |
| **Home** | Modular dashboard (S&P treemap, major news, futures, forex/bonds, earnings, econ releases) | Same category as News — aggregation/polish, not a new data source. |
| **Screener** | Already in progress | See §0-§4 above — presets + generalized parsing landed 2026-09-05. |

Proposed sequencing given each page type carries its own DOM/Cloudflare cost
(learned the hard way building the screener — this is realistically a
multi-night project, not one sitting): **Map treemap first** (fastest,
zero new scraping surface, directly requested), then **Insider/Managers**
(real trading signal, drill-down page confirmed to work with plain
navigation — no bot-check hit seen in Nick's own screenshots), then Groups,
then News/Home last as polish. Proposed to Nick via Discord 2026-09-05
00:12 EDT; awaiting his priority call before starting new scraper work
beyond the treemap.

**Map treemap: done and delivered**, same night — see
`src/charts/portfolio_treemap.py`. Sizes by portfolio $ weight (not market
cap), colors by day change %, reuses the Plotly-embedding convention from
`src/charts/ohlcv_chart.py`. Validated live against Nick's actual
`portfolio.csv` (all 39 positions, including BTC-USD, priced via one batched
`yf.download` call) and sent to him as a standalone interactive HTML file.
Not wired into the newsletter pipeline — that's a separate decision (where
in `generator.py`/`daily.html.j2` it plugs in) that hasn't been made.

**Insider/Managers: reconnaissance done, build not started.** Real findings,
not guessed, using the same driver helpers as the screener
(`_make_driver`/`_quit_driver`/`_wait_past_cloudflare` from
`finviz_screener.py` — reused directly, no new browser-automation surface):

- Both pages clear Cloudflare cleanly with the existing non-headless recipe —
  `insidertrading.ashx` (the Insiders tab) and `/insidertrading/managers`
  (the Managers tab) each returned real content on the first navigation, no
  retry needed. This is a *lower*-friction target than the screener was.
- Insiders table real URL is `finviz.com/insidertrading.ashx` (not a REST-
  style path like the Managers tab) with query params for filter/sort —
  `tc=7` was observed live producing "Latest Insider Trading." Real header
  row: `Ticker, Owner, Relationship, Date, Transaction, Cost, #Shares,
  Value ($), #Shares Total, SEC Form 4` — a different column set from the
  screener, so `finviz_screener.py`'s existing `_parse_header_columns` genuinely
  generalizes here rather than needing a rewrite (same `<thead>`-driven
  approach would work), but this is a distinct page/row markup, not the
  screener's table, so it needs its own parse function, not a shared one.
- Real row markup: `<tr class="fv-insider-row is-{type}-{n} cursor-pointer">`
  — the transaction type (buy/sale/proposedSale/option) is encoded directly
  in the row's own CSS class, not just in a text cell. That's a cleaner
  signal than text-matching "Sale" vs "Proposed Sale" would have been.
- Managers tab: 24 manager-card links found on one page load, pattern
  `/insidertrading/managers/{slug}-{cik}` (e.g.
  `/insidertrading/managers/susquehanna-international-group-llp-1446194`) —
  confirms the drill-down Nick demoed (BlackRock) is a normal navigable URL,
  not a JS-only interaction, so it's scrapable the same way.
- Not yet done: parsing the drill-down page itself (sector allocation chart,
  top-buys/sells table, general-statistics block) — Nick's screenshots show
  the layout but the real markup hasn't been captured live yet. Given the
  page cleared Cloudflare with zero friction, this is expected to be
  meaningfully cheaper than the screener build was, not another multi-hour
  diagnosis.
- Real product decision still open, not a scraping question: what "signal"
  actually means here. Nick's own framing was reactive ("looking at this one
  today makes me think MRVL and KLAC are interesting") — a useful feed would
  need either (a) per-manager top-buys pulled for managers Nick already
  follows, cross-referenced against his portfolio/watchlist, or (b) an
  aggregate view ("which names are multiple top managers buying this
  quarter"). Neither is decided; worth asking Nick directly rather than
  guessing which one he'd actually read.

## 6. Per-ticker deep dive (Nick's walkthrough continued, 04:17-04:19 UTC)

Nick kept going past the six site-section overview and walked through a
single ticker's page (NVDA) tab by tab, flagging what he actually reads.
Two things stood out as genuinely new — not duplicating what
`finviz_data.py` already scrapes off the Overview tab:

- **Short Interest tab** (`/stock?t=NVDA&ty=si`) — a real time series (chart
  + table) of short interest, short float %, and short ratio across ~2 years
  of settlement dates, not just the Overview tab's single current-value
  fields. Overview already has `short_float`/`short_ratio`
  (`finviz_data.py`), but not the *trend* — whether short interest is
  building or unwinding, which is the actually decision-relevant read.
- **Financials tab → Earnings sub-tab** (`&ty=ea`) — EPS/GAAP EPS/Revenue,
  each with per-quarter **estimate vs. reported vs. surprise %** AND
  **# of analysts** contributing the estimate, covering 8 quarters back and
  4 quarters of forward estimates. This is Finviz's own consensus data,
  materially more structured than scraping analyst PT commentary out of
  articles (last night's per-ticker earnings work used `yfinance` for beat
  history — this may be a cleaner source for the same thing, worth a direct
  comparison before switching). Balance Sheet / Cash Flow / income statement
  (seen on the ticker Overview tab itself, not a separate tab) are already
  noted in the Overview screenshots — 3 years free, older years paywalled
  behind Finviz Elite (a real, hard limit, not a scraping gap).

Nick kept going one more round on the Financials tab specifically:

- **Earnings sub-tab, beat-rate summary** — Finviz computes and displays
  "100% EPS beats the estimate, 8/8 last quarters" (same for GAAP EPS and
  Revenue) directly, plus an estimate-revisions panel (up/down revision
  counts per quarter, by analyst count) and a chart overlaying price against
  the estimate band over time. This is the exact beat-consistency check the
  September earnings research already did by hand via `yfinance` — Finviz
  computes it natively and may be cheaper/more reliable to read directly
  than to re-derive.
- **Forecast sub-tab** — analyst consensus rating as one number ("Strong
  Buy", from 68 analysts: 59 Strong Buy / 6 Buy / 2 Hold / 0 Sell / 1 Strong
  Sell), low/avg/high price targets with % upside/downside, and a full
  target-price-history chart. This is a materially richer analyst-consensus
  view than `finviz_data.py`'s current single consensus rating + target
  price fields.

**Options tab** (`&ty=oc`) was the last thing Nick showed ("Lastly for a
specific ticker is options... this would be a great resource" — his own
framing was speculative, he's explicitly not an options trader): a full
calls/puts chain by strike and expiry (last close, change, bid/ask, volume,
open interest). Real, structured data, but lowest-priority of everything in
this walkthrough by Nick's own words.

Not assessed: Latest Filings tab, Compare tab (visible in NVDA's tab bar,
never opened). The walkthrough ended after Options — confirmed by silence
in the Discord log, not assumed.

This reinforces §5's sequencing rather than changing it: none of this is
urgent-scraping-worthy yet on its own, but the Financials/Earnings tab is a
strong candidate to fold into whichever future session builds out
`finviz_data.py`'s per-ticker coverage, since the module already exists and
this would be an extension of it, not a new browser-automation surface (no
Cloudflare fight expected — the ticker quote page already works fine via
plain `requests`, unlike the screener and unlike the Insider pages, which
needed the browser route).

---
*Written 2026-09-04, updated 2026-09-05. Code so far:
`src/stockpicker/finviz_screener.py` — Cloudflare-clearing, browser
connection, generalized column parsing (6 views confirmed live), and 5 named
presets (short_squeeze, unusual_volume, new_52w_high, new_52w_low,
insider_buying, analyst_upgrades), all validated live 2026-09-05. Nothing
wired into the nightly picker yet — this is diagnosis + a module, not a
shipped feature. §5 above scopes a broader multi-page expansion Nick asked
to explore; not started beyond the screener itself.*
