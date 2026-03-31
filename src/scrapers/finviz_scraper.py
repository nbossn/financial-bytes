"""Finviz scraper using requests + BeautifulSoup.

Finviz quote pages are server-rendered — no JS engine required.
"""
from datetime import datetime, timezone
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from loguru import logger

from src.scrapers._utils import is_safe_url
from src.scrapers.base_scraper import BaseScraper, ScrapedArticle
from src.scrapers.user_agents import random_user_agent

FINVIZ_BASE = "https://finviz.com"
FINVIZ_QUOTE_URL = "https://finviz.com/quote.ashx?t={ticker}&p=d"
FINVIZ_SEC_URL = "https://finviz.com/sec.ashx?t={ticker}"
FINVIZ_CHART_DAILY = "https://charts2.finviz.com/chart.ashx?t={ticker}&ty=c&ta=1&p=d"
FINVIZ_CHART_WEEKLY = "https://charts2.finviz.com/chart.ashx?t={ticker}&ty=c&ta=1&p=w"

# ── Snapshot table: label → field name ──────────────────────────────────────
# Finviz snapshot table has ~70 cells arranged in label/value pairs.
_SNAPSHOT_LABEL_MAP = {
    # Technicals
    "RSI (14)":       "rsi",
    "MACD":           "macd",
    "SMA20":          "sma_20",
    "SMA50":          "sma_50",
    "SMA200":         "sma_200",
    "Beta":           "beta",
    # Valuation
    "P/E":            "pe_ratio",
    "Forward P/E":    "forward_pe",
    "PEG":            "peg_ratio",
    "P/S":            "ps_ratio",
    "P/B":            "pb_ratio",
    "P/C":            "pc_ratio",
    "P/FCF":          "pfcf_ratio",
    "EPS (ttm)":      "eps_ttm",
    "EPS next Y":     "eps_next_year",
    "EPS next Q":     "eps_next_quarter",
    "EPS this Y":     "eps_this_year",
    "EPS past 5Y":    "eps_past_5y",
    "EPS next 5Y":    "eps_next_5y",
    # Growth
    "Sales past 5Y":  "sales_past_5y",
    "Sales Q/Q":      "sales_qoq",
    "EPS Q/Q":        "eps_qoq",
    # Profitability
    "Profit Margin":  "profit_margin",
    "Oper. Margin":   "oper_margin",
    "Gross Margin":   "gross_margin",
    # Financial strength
    "Current Ratio":  "current_ratio",
    "Quick Ratio":    "quick_ratio",
    "LT Debt/Eq":     "lt_debt_eq",
    "Debt/Eq":        "debt_eq",
    "ROA":            "roa",
    "ROE":            "roe",
    "ROI":            "roi",
    # Market data
    "Market Cap":     "market_cap_text",
    "Income":         "income_text",
    "Sales":          "sales_text",
    "Book/sh":        "book_per_share",
    "Cash/sh":        "cash_per_share",
    "Dividend":       "dividend",
    "Dividend %":     "dividend_pct",
    # Ownership / float
    "Shs Outstand":   "shares_outstanding_text",
    "Shs Float":      "shares_float_text",
    "Short Float":    "short_float",
    "Short Ratio":    "short_ratio",
    "Insider Own":    "insider_own",
    "Inst Own":       "inst_own",
    "Insider Trans":  "insider_trans",
    "Inst Trans":     "inst_trans",
    "Avg Volume":     "avg_volume_text",
    "Rel Volume":     "rel_volume",
    # Price / target
    "52W High":       "high_52w",
    "52W Low":        "low_52w",
    "52W Range":      "range_52w",
    "Target Price":   "target_price",
    "Perf Week":      "perf_week",
    "Perf Month":     "perf_month",
    "Perf Quarter":   "perf_quarter",
    "Perf Half Y":    "perf_half_year",
    "Perf Year":      "perf_year",
    "Perf YTD":       "perf_ytd",
    "Volatility W":   "volatility_week",
    "Volatility M":   "volatility_month",
    "ATR (14)":       "atr",
}

# Fields that are percentages (stored as strings with % sign, keep as float)
_PCT_FIELDS = {
    "rsi", "short_float", "insider_own", "inst_own", "insider_trans", "inst_trans",
    "profit_margin", "oper_margin", "gross_margin", "roa", "roe", "roi",
    "perf_week", "perf_month", "perf_quarter", "perf_half_year", "perf_year", "perf_ytd",
    "volatility_week", "volatility_month", "eps_this_year", "eps_next_year", "eps_past_5y",
    "eps_next_5y", "sales_past_5y", "sales_qoq", "eps_qoq", "dividend_pct",
}

# Fields that should stay as plain string (market cap notation like "1.23T")
_STRING_FIELDS = {
    "market_cap_text", "income_text", "sales_text", "shares_outstanding_text",
    "shares_float_text", "avg_volume_text", "range_52w",
}


def _get_page_html(url: str) -> str | None:
    """Fetch Finviz quote page HTML via plain requests (server-rendered)."""
    headers = {
        "User-Agent": random_user_agent(),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://finviz.com/",
    }
    try:
        resp = requests.get(url, headers=headers, timeout=20)
        resp.raise_for_status()
        return resp.text
    except Exception as e:
        logger.warning(f"[finviz] Request failed for {url}: {e}")
        return None


def _parse_snapshot(soup: BeautifulSoup) -> dict:
    """Extract all fields from the Finviz snapshot table.

    Returns a flat dict with keys matching _SNAPSHOT_LABEL_MAP values.
    Numeric fields stored as float; string fields (market cap notation) stored as str.
    """
    result: dict = {}
    snapshot = soup.find("table", class_="snapshot-table2")
    if not snapshot:
        return result

    cells = snapshot.find_all("td")
    for i in range(0, len(cells) - 1, 2):
        label = cells[i].get_text(strip=True)
        value_text = cells[i + 1].get_text(strip=True)
        field = _SNAPSHOT_LABEL_MAP.get(label)
        if not field or not value_text or value_text == "-":
            continue

        if field in _STRING_FIELDS:
            result[field] = value_text
            continue

        # Convert to float — strip %, commas, B/M/T notation where applicable
        clean = value_text.replace("%", "").replace(",", "")
        try:
            result[field] = float(clean)
        except ValueError:
            # Keep as string for values like "N/A" or complex ranges
            result[field] = value_text

    return result


def _extract_article_text(url: str, headers: dict) -> str | None:
    """Fetch article URL and extract body text."""
    if not is_safe_url(url):
        logger.warning(f"[finviz] Blocked unsafe URL: {url[:80]}")
        return None
    try:
        resp = requests.get(url, headers=headers, timeout=15, allow_redirects=True)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "lxml")

        for tag in soup(["script", "style", "nav", "header", "footer", "aside", "iframe"]):
            tag.decompose()

        for selector in [
            "article", ".article-body", ".article__body", "#article-body",
            ".story-body", ".entry-content", ".post-content", "main",
        ]:
            element = soup.select_one(selector)
            if element:
                text = element.get_text(separator=" ", strip=True)
                if len(text) > 200:
                    return text[:3000]

        paragraphs = soup.find_all("p")
        text = " ".join(p.get_text(strip=True) for p in paragraphs if len(p.get_text(strip=True)) > 50)
        return text[:3000] if text else None
    except Exception as e:
        logger.debug(f"[finviz] Could not extract text from {url}: {e}")
        return None


class FinvizScraper(BaseScraper):
    source_name = "finviz"

    def _scrape(self, ticker: str) -> list[ScrapedArticle]:
        url = FINVIZ_QUOTE_URL.format(ticker=ticker)
        articles = []

        html = _get_page_html(url)
        if not html:
            return []

        soup = BeautifulSoup(html, "lxml")

        news_table = (
            soup.find("table", class_="fullview-news-outer")
            or soup.find(class_="news-table")
        )
        if not news_table:
            logger.warning(f"[finviz] No news table found for {ticker}")
            return []

        headers = self._get_headers()
        rows = news_table.find_all("tr")

        for row in rows[:20]:
            cells = row.find_all("td")
            if len(cells) < 2:
                continue

            link_cell = cells[-1]
            a_tag = link_cell.find("a")
            if not a_tag or not a_tag.get("href"):
                continue

            article_url = a_tag["href"]
            if not article_url.startswith("http"):
                article_url = urljoin(FINVIZ_BASE, article_url)

            headline = a_tag.get_text(strip=True)
            source = link_cell.find("span") or link_cell.find(class_="news-link-source")
            source_name = source.get_text(strip=True) if source else "finviz"

            time_cell = cells[0].get_text(strip=True) if len(cells) > 1 else ""
            published_at = _parse_finviz_time(time_cell)

            self._sleep()
            body = _extract_article_text(article_url, headers)

            articles.append(
                ScrapedArticle(
                    ticker=ticker,
                    headline=headline,
                    url=article_url,
                    source=source_name or "finviz",
                    body=body,
                    published_at=published_at,
                )
            )

        logger.info(f"[finviz] {len(articles)} articles for {ticker}")
        return articles

    def scrape_technicals(self, ticker: str) -> dict:
        """Scrape technicals + chart URLs from Finviz snapshot page.

        Returns dict suitable for merging into a TechnicalIndicators instance.
        """
        url = FINVIZ_QUOTE_URL.format(ticker=ticker)
        html = _get_page_html(url)
        if not html:
            return {}

        soup = BeautifulSoup(html, "lxml")
        snapshot = _parse_snapshot(soup)

        # Extract technical subset
        result = {
            k: snapshot[k]
            for k in ("rsi", "macd", "sma_20", "sma_50", "sma_200", "beta")
            if k in snapshot
        }
        result["chart_daily_url"] = FINVIZ_CHART_DAILY.format(ticker=ticker)
        result["chart_weekly_url"] = FINVIZ_CHART_WEEKLY.format(ticker=ticker)
        logger.info(
            f"[finviz] technicals for {ticker}: "
            f"RSI={result.get('rsi')}, SMA20={result.get('sma_20')}, Beta={result.get('beta')}"
        )
        return result

    def scrape_fundamentals(self, ticker: str) -> dict:
        """Scrape the full Finviz snapshot table — valuation, profitability, ownership, etc.

        Returns flat dict with all parsed fields from _SNAPSHOT_LABEL_MAP.
        """
        url = FINVIZ_QUOTE_URL.format(ticker=ticker)
        html = _get_page_html(url)
        if not html:
            return {}

        soup = BeautifulSoup(html, "lxml")
        data = _parse_snapshot(soup)

        logger.info(
            f"[finviz] fundamentals for {ticker}: "
            f"P/E={data.get('pe_ratio')}, Margin={data.get('profit_margin')}, "
            f"ShortFloat={data.get('short_float')}, MarketCap={data.get('market_cap_text')}"
        )
        return data

    def scrape_sec_filings(self, ticker: str) -> list[dict]:
        """Scrape recent SEC filings from finviz.com/sec.ashx?t={ticker}.

        Returns list of dicts with keys: date, form_type, description, url.
        """
        url = FINVIZ_SEC_URL.format(ticker=ticker)
        html = _get_page_html(url)
        if not html:
            return []

        soup = BeautifulSoup(html, "lxml")
        filings = []

        # Finviz SEC page has a table with class "body-table" or similar
        table = soup.find("table", class_="body-table") or soup.find("table", id="news-table")
        if not table:
            # Fallback: any table with SEC form type columns
            tables = soup.find_all("table")
            for t in tables:
                headers_row = t.find("tr")
                if headers_row and "Form" in headers_row.get_text():
                    table = t
                    break

        if not table:
            logger.debug(f"[finviz] No SEC filings table found for {ticker}")
            return []

        rows = table.find_all("tr")
        for row in rows[1:15]:  # skip header, cap at 14 filings
            cells = row.find_all("td")
            if len(cells) < 3:
                continue

            date_text = cells[0].get_text(strip=True)
            form_type = cells[1].get_text(strip=True)
            desc_cell = cells[2]
            a_tag = desc_cell.find("a")
            description = desc_cell.get_text(strip=True)
            filing_url = ""
            if a_tag and a_tag.get("href"):
                filing_url = a_tag["href"]
                if not filing_url.startswith("http"):
                    filing_url = urljoin(FINVIZ_BASE, filing_url)

            if form_type and date_text:
                filings.append({
                    "date": date_text,
                    "form_type": form_type,
                    "description": description,
                    "url": filing_url,
                })

        logger.info(f"[finviz] {len(filings)} SEC filings for {ticker}")
        return filings


def _parse_finviz_time(time_str: str) -> datetime | None:
    """Parse Finviz time strings like 'Mar-27-26 08:30AM' or 'Today 08:30AM'."""
    if not time_str:
        return None
    try:
        now = datetime.now(timezone.utc)
        if "today" in time_str.lower():
            time_part = time_str.lower().replace("today", "").strip()
            return datetime.strptime(
                f"{now.strftime('%Y-%m-%d')} {time_part}", "%Y-%m-%d %I:%M%p"
            ).replace(tzinfo=timezone.utc)
        return datetime.strptime(time_str, "%b-%d-%y %I:%M%p").replace(tzinfo=timezone.utc)
    except (ValueError, AttributeError):
        return None
