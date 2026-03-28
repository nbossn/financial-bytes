"""Web search fallback — triggered when article count < 3 for a ticker."""
import requests
from bs4 import BeautifulSoup
from loguru import logger

from src.scrapers.base_scraper import BaseScraper, ScrapedArticle


class WebSearchFallback(BaseScraper):
    """DuckDuckGo HTML search fallback for tickers with insufficient article coverage."""
    source_name = "web_search"

    DDGO_URL = "https://html.duckduckgo.com/html/"

    def _scrape(self, ticker: str) -> list[ScrapedArticle]:
        query = f"{ticker} stock news financial analysis {self._today_str()}"
        return self._search_ddgo(ticker, query)

    def _search_ddgo(self, ticker: str, query: str) -> list[ScrapedArticle]:
        articles = []
        headers = self._get_headers()
        headers["Content-Type"] = "application/x-www-form-urlencoded"

        try:
            self._sleep()
            resp = requests.post(
                self.DDGO_URL,
                data={"q": query, "b": "", "kl": "us-en"},
                headers=headers,
                timeout=20,
            )
            resp.raise_for_status()
            soup = BeautifulSoup(resp.text, "lxml")

            results = soup.select(".result__title a, .result a.result__url")
            seen_urls = set()

            for a_tag in results[:8]:
                href = a_tag.get("href", "")
                if not href or href in seen_urls:
                    continue
                # DDG wraps links — extract actual URL
                if "duckduckgo.com" in href:
                    continue
                seen_urls.add(href)

                headline = a_tag.get_text(strip=True)
                if not headline or len(headline) < 10:
                    continue

                # Skip known low-quality or paywalled domains
                skip_domains = ["wsj.com", "ft.com"]
                if any(d in href for d in skip_domains):
                    continue

                snippet = self._get_snippet(soup, href)
                body = self._fetch_article(href, headers)

                articles.append(
                    ScrapedArticle(
                        ticker=ticker,
                        headline=headline,
                        url=href,
                        source="web_search",
                        body=body,
                        snippet=snippet,
                    )
                )

        except Exception as e:
            logger.warning(f"[web_search] Fallback failed for {ticker}: {e}")

        logger.info(f"[web_search] Found {len(articles)} fallback articles for {ticker}")
        return articles

    def _get_snippet(self, soup, url: str) -> str | None:
        """Try to find the search result snippet for a URL."""
        try:
            result_divs = soup.select(".result__snippet")
            for div in result_divs:
                text = div.get_text(strip=True)
                if len(text) > 20:
                    return text[:500]
            return None
        except Exception:
            return None

    @staticmethod
    def _is_safe_url(url: str) -> bool:
        """Reject non-http(s) schemes and private/loopback IP ranges (SSRF guard)."""
        import ipaddress
        from urllib.parse import urlparse
        try:
            parsed = urlparse(url)
            if parsed.scheme not in ("http", "https"):
                return False
            host = parsed.hostname or ""
            # Block bare IP addresses that are private/loopback/link-local
            try:
                addr = ipaddress.ip_address(host)
                if addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_reserved:
                    return False
            except ValueError:
                pass  # hostname, not an IP — allow
            return True
        except Exception:
            return False

    def _fetch_article(self, url: str, headers: dict) -> str | None:
        if not self._is_safe_url(url):
            logger.warning(f"[web_search] Blocked unsafe URL: {url[:80]}")
            return None
        try:
            self._sleep()
            resp = requests.get(url, headers=headers, timeout=15, allow_redirects=True)
            resp.raise_for_status()
            soup = BeautifulSoup(resp.text, "lxml")

            for tag in soup(["script", "style", "nav", "header", "footer", "aside"]):
                tag.decompose()

            for selector in ["article", ".article-body", ".post-content", ".entry-content", "main"]:
                el = soup.select_one(selector)
                if el:
                    text = el.get_text(separator=" ", strip=True)
                    if len(text) > 150:
                        return text[:2500]

            paragraphs = soup.find_all("p")
            text = " ".join(p.get_text(strip=True) for p in paragraphs if len(p.get_text(strip=True)) > 40)
            return text[:2500] if len(text) > 150 else None
        except Exception:
            return None

    @staticmethod
    def _today_str() -> str:
        from datetime import date
        return date.today().strftime("%B %Y")
