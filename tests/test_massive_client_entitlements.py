"""massive.com client: permanent-failure handling and endpoint circuit breaking.

Probing the live API (2026-08-18) established what this plan actually supports:

    /v2/reference/analysts            404  — path does not exist, never did
    /benzinga/v1/ratings              403  — real path, plan not entitled
    /benzinga/v1/analyst-insights     403  — real path, plan not entitled
    /v2/snapshot/.../tickers/{t}      403  — plan not entitled
    /v1/indicators/{rsi,macd}/{t}     429  — entitled, rate limited
    /v2/reference/news                429  — entitled, rate limited
    /v3/reference/tickers             200
    /v2/aggs/ticker/{t}/prev          200

Two bugs followed from treating all of these the same way:

1. `_retry_decorator` retried *any* MassiveAPIError 5x with exponential backoff,
   so every permanently-dead endpoint burned 5 requests and up to ~30s of sleep
   per ticker. Across 51 tickers this dominated the 1,405s runtime.
2. Every retry logged at ERROR, producing thousands of lines per run (10,350
   analysts-404s in scheduler.log alone) and burying genuine new failures.

Permanent errors (401/403/404) must fail fast and disable their endpoint for the
process. Transient ones (429/5xx) must still retry.
"""
from __future__ import annotations

import httpx
import pytest

from src.api.massive_client import MassiveAPIError, MassiveClient


def _http_error(status: int, path: str = "/v2/reference/analysts") -> httpx.HTTPStatusError:
    request = httpx.Request("GET", f"https://api.massive.com{path}")
    response = httpx.Response(status, text="boom", request=request)
    return httpx.HTTPStatusError("err", request=request, response=response)


class TestPermanentClassification:
    @pytest.mark.parametrize("status", [401, 403, 404])
    def test_permanent_statuses(self, status):
        err = MassiveAPIError("x", status_code=status)
        assert err.is_permanent is True

    @pytest.mark.parametrize("status", [429, 500, 502, 503])
    def test_transient_statuses(self, status):
        err = MassiveAPIError("x", status_code=status)
        assert err.is_permanent is False

    def test_unknown_status_is_transient(self):
        """A network error with no status must stay retryable."""
        assert MassiveAPIError("x").is_permanent is False


class TestEndpointFamily:
    """Ticker-bearing paths must collapse to one family, or the breaker never trips."""

    @pytest.mark.parametrize(
        "path,expected",
        [
            ("/v1/indicators/rsi/AAPL", "/v1/indicators/rsi"),
            ("/v1/indicators/macd/BRK.B", "/v1/indicators/macd"),
            (
                "/v2/snapshot/locale/us/markets/stocks/tickers/NVDA",
                "/v2/snapshot/locale/us/markets/stocks/tickers",
            ),
            ("/v2/reference/analysts", "/v2/reference/analysts"),
            ("/benzinga/v1/ratings", "/benzinga/v1/ratings"),
        ],
    )
    def test_family_strips_trailing_ticker(self, path, expected):
        assert MassiveClient._endpoint_family(path) == expected


class TestCircuitBreaker:
    def test_permanent_failure_disables_endpoint(self, monkeypatch):
        calls = []

        def fake_get(self, path, params=None):
            calls.append(path)
            raise _http_error(404)

        monkeypatch.setattr(httpx.Client, "get", fake_get)
        client = MassiveClient()

        with pytest.raises(MassiveAPIError):
            client.get("/v2/reference/analysts", params={"ticker": "AAPL"})
        # Second call must short-circuit without touching the network.
        with pytest.raises(MassiveAPIError):
            client.get("/v2/reference/analysts", params={"ticker": "MSFT"})

        assert len(calls) == 1, "disabled endpoint must not be requested again"

    def test_breaker_covers_whole_family(self, monkeypatch):
        calls = []

        def fake_get(self, path, params=None):
            calls.append(path)
            raise _http_error(403, path)

        monkeypatch.setattr(httpx.Client, "get", fake_get)
        client = MassiveClient()

        base = "/v2/snapshot/locale/us/markets/stocks/tickers"
        with pytest.raises(MassiveAPIError):
            client.get(f"{base}/NVDA")
        with pytest.raises(MassiveAPIError):
            client.get(f"{base}/AAPL")
        with pytest.raises(MassiveAPIError):
            client.get(f"{base}/MSFT")

        assert len(calls) == 1

    def test_transient_failure_does_not_disable(self, monkeypatch):
        calls = []

        def fake_get(self, path, params=None):
            calls.append(path)
            raise _http_error(429, path)

        monkeypatch.setattr(httpx.Client, "get", fake_get)
        client = MassiveClient()

        for _ in range(3):
            with pytest.raises(MassiveAPIError):
                client.get("/v1/indicators/rsi/AAPL")

        assert len(calls) == 3, "rate limits are transient — keep trying"

    def test_success_keeps_endpoint_enabled(self, monkeypatch):
        def fake_get(self, path, params=None):
            request = httpx.Request("GET", f"https://api.massive.com{path}")
            return httpx.Response(200, json={"results": []}, request=request)

        monkeypatch.setattr(httpx.Client, "get", fake_get)
        client = MassiveClient()

        assert client.get("/v2/aggs/ticker/PANW/prev") == {"results": []}
        assert client.get("/v2/aggs/ticker/AAPL/prev") == {"results": []}

    def test_error_carries_status_code(self, monkeypatch):
        def fake_get(self, path, params=None):
            raise _http_error(403, path)

        monkeypatch.setattr(httpx.Client, "get", fake_get)
        client = MassiveClient()

        with pytest.raises(MassiveAPIError) as exc:
            client.get("/benzinga/v1/ratings")

        assert exc.value.status_code == 403
        assert exc.value.is_permanent is True


class TestRetryPredicate:
    """The endpoints layer must not retry permanently-dead calls."""

    def test_permanent_errors_are_not_retried(self):
        from src.api.endpoints import _should_retry

        assert _should_retry(MassiveAPIError("x", status_code=404)) is False
        assert _should_retry(MassiveAPIError("x", status_code=403)) is False

    def test_transient_errors_are_retried(self):
        from src.api.endpoints import _should_retry

        assert _should_retry(MassiveAPIError("x", status_code=429)) is True
        assert _should_retry(MassiveAPIError("x", status_code=503)) is True
        assert _should_retry(MassiveAPIError("x")) is True
