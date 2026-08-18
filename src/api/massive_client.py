"""HTTP client for the massive.com REST API.

massive.com exposes a Polygon-compatible surface. This account's plan does not
cover all of it, and one path in this codebase was simply wrong. Probed
2026-08-18:

    /v2/reference/analysts            404  — path does not exist
    /benzinga/v1/ratings              403  — correct path, plan not entitled
    /benzinga/v1/analyst-insights     403  — correct path, plan not entitled
    /v2/snapshot/.../tickers/{t}      403  — plan not entitled
    /v1/indicators/{rsi,macd}/{t}     429  — entitled, rate limited
    /v2/reference/news                429  — entitled, rate limited
    /v3/reference/tickers             200
    /v2/aggs/ticker/{t}/prev          200

Permanent failures (401/403/404) are hopeless until the plan or the path
changes, so the first one disables its endpoint family for the life of the
process: no repeat requests, no repeat ERROR lines. Transient failures
(429/5xx/network) still retry — the caller's backoff handles those.
"""
import re

import httpx
from loguru import logger

from src.config import settings

# Statuses that will not improve by trying again with this key/plan.
_PERMANENT_STATUSES = frozenset({401, 403, 404})

# Trailing path segment that is a ticker rather than part of the route,
# e.g. /v1/indicators/rsi/AAPL, /v2/snapshot/.../tickers/BRK.B
_TICKER_SEGMENT = re.compile(r"^[A-Za-z][A-Za-z.\-]{0,9}$")


class MassiveAPIError(Exception):
    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code

    @property
    def is_permanent(self) -> bool:
        """True when retrying cannot help (bad path, or plan not entitled)."""
        return self.status_code in _PERMANENT_STATUSES


class MassiveClient:
    def __init__(self):
        self._client = httpx.Client(
            base_url=settings.massive_base_url,
            headers={
                "Authorization": f"Bearer {settings.massive_api_key}",
                "Accept": "application/json",
                "User-Agent": "financial-bytes/0.1.0",
            },
            timeout=30.0,
        )
        # endpoint family -> human-readable reason it was disabled
        self._disabled: dict[str, str] = {}

    @staticmethod
    def _endpoint_family(path: str) -> str:
        """Collapse a per-ticker path to the route it belongs to.

        Without this the breaker would key on /v1/indicators/rsi/AAPL and never
        trip for /v1/indicators/rsi/MSFT — one dead route would still cost one
        failed request per ticker.
        """
        segments = [s for s in path.split("/") if s]
        if len(segments) > 1 and _TICKER_SEGMENT.match(segments[-1]):
            # Keep known route verbs that would otherwise look like tickers.
            if segments[-1].lower() not in {"prev", "ratings", "analysts", "news", "tickers"}:
                segments = segments[:-1]
        return "/" + "/".join(segments)

    def get(self, path: str, params: dict | None = None) -> dict:
        """Make authenticated GET request, return parsed JSON."""
        family = self._endpoint_family(path)

        if family in self._disabled:
            raise MassiveAPIError(
                f"{family} disabled this run: {self._disabled[family]}",
                status_code=None,
            )

        try:
            response = self._client.get(path, params=params)
            response.raise_for_status()
            return response.json()
        except httpx.HTTPStatusError as e:
            status = e.response.status_code
            body = e.response.text[:200]

            if status in _PERMANENT_STATUSES:
                # Log once per family, then stay quiet for the rest of the run.
                reason = f"HTTP {status}"
                self._disabled[family] = reason
                logger.warning(
                    f"massive.com {family} disabled for this run ({reason}) — "
                    f"falling back to alternate sources. {body}"
                )
            else:
                logger.error(f"massive.com API error {status}: {path} — {body}")

            raise MassiveAPIError(f"HTTP {status}: {path}", status_code=status) from e
        except httpx.RequestError as e:
            logger.error(f"massive.com request failed: {path} — {e}")
            raise MassiveAPIError(f"Request failed: {path}") from e

    @property
    def disabled_endpoints(self) -> dict[str, str]:
        """Endpoint families switched off this run, for end-of-run reporting."""
        return dict(self._disabled)

    def close(self):
        self._client.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
