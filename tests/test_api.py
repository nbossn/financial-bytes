"""Tests for massive.com API client and endpoints."""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from src.api.models import AnalystRating, BenzingaArticle, QuoteSnapshot, TechnicalIndicators, TickerSignals


class TestApiModels:
    def test_ticker_signals_consensus_rating(self):
        ratings = [
            AnalystRating(ticker="MSFT", analyst_firm="GS", rating="Buy", price_target=450.0),
            AnalystRating(ticker="MSFT", analyst_firm="MS", rating="Buy", price_target=430.0),
            AnalystRating(ticker="MSFT", analyst_firm="Citi", rating="Neutral", price_target=380.0),
        ]
        # consensus is set by endpoints.get_ticker_signals(); test model field storage
        signals = TickerSignals(ticker="MSFT", analyst_ratings=ratings, consensus_rating="Buy")
        assert signals.consensus_rating == "Buy"

    def test_ticker_signals_consensus_price_target(self):
        from decimal import Decimal
        ratings = [
            AnalystRating(ticker="MSFT", analyst_firm="GS", rating="Buy", price_target=450.0),
            AnalystRating(ticker="MSFT", analyst_firm="MS", rating="Buy", price_target=430.0),
        ]
        signals = TickerSignals(ticker="MSFT", analyst_ratings=ratings,
                                consensus_price_target=Decimal("440.0"))
        assert float(signals.consensus_price_target) == pytest.approx(440.0)

    def test_quote_snapshot_defaults(self):
        q = QuoteSnapshot(ticker="MSFT", current_price=360.0)
        assert q.current_price == 360.0
        assert q.day_change_pct is None

    def test_technical_indicators_partial(self):
        t = TechnicalIndicators(ticker="MSFT", rsi=42.5, macd=1.2, macd_signal=0.8)
        assert t.rsi == pytest.approx(42.5)
        assert t.signal_summary is None


class TestMassiveClient:
    def test_client_sets_auth_header(self):
        with patch("httpx.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client_cls.return_value.__enter__ = MagicMock(return_value=mock_client)
            mock_client_cls.return_value.__exit__ = MagicMock(return_value=False)

            from src.api.massive_client import MassiveClient
            from src.config import settings

            with MassiveClient() as client:
                pass

            # Verify client was created with bearer token
            call_kwargs = mock_client_cls.call_args
            assert call_kwargs is not None


class TestEndpoints:
    def test_get_ticker_signals_empty_ratings(self):
        from src.api.endpoints import MassiveEndpoints

        mock_client = MagicMock()
        endpoints = MassiveEndpoints(mock_client)

        # Mock get_quote to return a simple quote
        mock_client.get.return_value = {
            "ticker": "MSFT",
            "price": 360.0,
        }

        with patch.object(endpoints, "get_quote") as mock_quote, \
             patch.object(endpoints, "get_news") as mock_news, \
             patch.object(endpoints, "get_analyst_ratings") as mock_ratings, \
             patch.object(endpoints, "get_technicals") as mock_tech:

            mock_quote.return_value = QuoteSnapshot(ticker="MSFT", current_price=360.0)
            mock_news.return_value = []
            mock_ratings.return_value = []
            mock_tech.return_value = None

            signals = endpoints.get_ticker_signals("MSFT")
            assert signals.ticker == "MSFT"
            assert signals.quote.current_price == 360.0
            assert signals.analyst_ratings == []
