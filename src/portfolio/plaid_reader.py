"""Plaid Investments integration — fetch Fidelity holdings via OAuth.

Two-phase setup:
  Phase 1 (one-time): Run `financial-bytes plaid-setup --portfolio lilich`
    Opens a local web server, user completes Plaid Link in browser,
    exchanges public_token for access_token, stores in .env as
    PLAID_ACCESS_TOKEN_<PORTFOLIO_NAME_UPPER>.

  Phase 2 (daily): Run `financial-bytes plaid-sync --portfolio lilich`
    Uses stored access_token to fetch current holdings from Plaid.
    Returns list[Holding] — same interface as fidelity_reader and robinhood_reader.

Environment variables (add to .env after setup):
  PLAID_CLIENT_ID       — from Plaid dashboard (https://dashboard.plaid.com)
  PLAID_SECRET          — Development environment secret
  PLAID_ENV             — "sandbox" | "development" | "production" (use "development" for real accounts)
  PLAID_ACCESS_TOKEN_LILICH  — stored after first plaid-setup run (lilich portfolio)
  PLAID_ACCESS_TOKEN_NBOSSN  — stored after first plaid-setup run (nbossn portfolio)

Plaid Data Notes:
  - Holdings endpoint returns average cost basis (not per-lot detail)
  - For per-lot detail, use purchase_history JSON (already exists for lilich)
  - Prices from Plaid may be 15-min delayed; pipeline uses yfinance for live quotes anyway
  - Trust accounts: accessible if they share a Fidelity login; separate logins need separate Items

Plaid Plan:
  Development tier is free, supports real accounts, no call volume limits for personal use.
  Production requires Plaid approval — development tier is sufficient here indefinitely.
"""
from __future__ import annotations

import os
from datetime import date
from decimal import Decimal
from typing import Optional

from loguru import logger


class PlaidConfigError(Exception):
    """Raised when Plaid credentials are missing or misconfigured."""


class PlaidSyncError(Exception):
    """Raised when holdings cannot be fetched from Plaid."""


def _get_plaid_client():
    """Build and return a configured Plaid API client."""
    try:
        import plaid
        from plaid.api import plaid_api
        from plaid.model.products import Products
        from plaid.model.country_code import CountryCode
    except ImportError as e:
        raise PlaidConfigError("plaid-python not installed — run: pip install plaid-python") from e

    client_id = os.getenv("PLAID_CLIENT_ID")
    secret = os.getenv("PLAID_SECRET")
    env_name = os.getenv("PLAID_ENV", "development").lower()

    if not client_id or not secret:
        raise PlaidConfigError(
            "PLAID_CLIENT_ID and PLAID_SECRET must be set in .env.\n"
            "Get them at: https://dashboard.plaid.com/team/keys\n"
            "Use the Development environment for real Fidelity accounts."
        )

    env_map = {
        "sandbox": plaid.Environment.Sandbox,
        "development": plaid.Environment.Development,
        "production": plaid.Environment.Production,
    }
    if env_name not in env_map:
        raise PlaidConfigError(f"PLAID_ENV must be sandbox|development|production, got: {env_name}")

    configuration = plaid.Configuration(
        host=env_map[env_name],
        api_key={
            "clientId": client_id,
            "secret": secret,
        },
    )
    api_client = plaid.ApiClient(configuration)
    return plaid_api.PlaidApi(api_client)


def _access_token_env_key(portfolio_name: str) -> str:
    """Return the .env key for a portfolio's Plaid access token."""
    return f"PLAID_ACCESS_TOKEN_{portfolio_name.upper()}"


def get_access_token(portfolio_name: str) -> str:
    """Read stored Plaid access token for a portfolio. Raises PlaidConfigError if missing."""
    key = _access_token_env_key(portfolio_name)
    token = os.getenv(key)
    if not token:
        raise PlaidConfigError(
            f"{key} not set in .env.\n"
            f"Run: financial-bytes plaid-setup --portfolio {portfolio_name}\n"
            f"Then complete the OAuth flow in your browser."
        )
    return token


def create_link_token(portfolio_name: str) -> str:
    """Create a Plaid Link token to initiate the OAuth flow."""
    from plaid.model.link_token_create_request import LinkTokenCreateRequest
    from plaid.model.link_token_create_request_user import LinkTokenCreateRequestUser
    from plaid.model.products import Products
    from plaid.model.country_code import CountryCode

    client = _get_plaid_client()

    request = LinkTokenCreateRequest(
        products=[Products("investments")],
        client_name="financial-bytes",
        country_codes=[CountryCode("US")],
        language="en",
        user=LinkTokenCreateRequestUser(client_user_id=f"user-{portfolio_name}"),
        redirect_uri=None,
    )

    try:
        response = client.link_token_create(request)
        return response["link_token"]
    except Exception as e:
        raise PlaidConfigError(f"Failed to create Plaid Link token: {e}") from e


def exchange_public_token(public_token: str) -> str:
    """Exchange a Plaid public token for a permanent access token."""
    from plaid.model.item_public_token_exchange_request import ItemPublicTokenExchangeRequest

    client = _get_plaid_client()
    request = ItemPublicTokenExchangeRequest(public_token=public_token)

    try:
        response = client.item_public_token_exchange(request)
        return response["access_token"]
    except Exception as e:
        raise PlaidSyncError(f"Token exchange failed: {e}") from e


def fetch_plaid_holdings(portfolio_name: str) -> list[dict]:
    """
    Fetch raw investment holdings from Plaid for a portfolio.

    Returns list of dicts with keys: ticker, shares, cost_basis, name, type.
    """
    from plaid.model.investments_holdings_get_request import InvestmentsHoldingsGetRequest

    access_token = get_access_token(portfolio_name)
    client = _get_plaid_client()

    try:
        request = InvestmentsHoldingsGetRequest(access_token=access_token)
        response = client.investments_holdings_get(request)
    except Exception as e:
        raise PlaidSyncError(f"Failed to fetch holdings from Plaid: {e}") from e

    # Build security lookup: security_id -> security info
    securities = {s["security_id"]: s for s in response.get("securities", [])}

    results = []
    for holding in response.get("holdings", []):
        security_id = holding.get("security_id")
        security = securities.get(security_id, {})

        ticker = security.get("ticker_symbol") or security.get("name", "")
        if not ticker:
            logger.debug(f"Skipping holding with no ticker: {security}")
            continue

        ticker = ticker.upper().strip()
        shares = holding.get("quantity", 0)
        cost_basis = holding.get("cost_basis")  # May be None for some holdings

        # Skip cash, money market, and non-equity securities
        security_type = security.get("type", "").lower()
        if security_type in ("cash", "derivative") or ticker.startswith("CUR:"):
            logger.debug(f"Skipping non-equity: {ticker} ({security_type})")
            continue

        results.append({
            "ticker": ticker,
            "shares": shares,
            "cost_basis": cost_basis,
            "name": security.get("name", ""),
            "type": security_type,
            "institution_price": holding.get("institution_price"),
        })

    logger.info(f"[plaid] Fetched {len(results)} holdings for {portfolio_name}")
    return results


def read_plaid_holdings(portfolio_name: str) -> list:
    """
    Fetch live Plaid holdings and return as list[Holding].

    Reads access token from env: PLAID_ACCESS_TOKEN_<PORTFOLIO_NAME_UPPER>.
    cost_basis from Plaid is average basis (not per-lot).
    purchase_date is set to None — use purchase_history JSON for lot-level dates.
    """
    from src.portfolio.models import Holding

    raw = fetch_plaid_holdings(portfolio_name)

    holdings = []
    for item in raw:
        ticker = item["ticker"]
        try:
            shares = Decimal(str(round(item["shares"], 6)))
        except Exception:
            logger.warning(f"[plaid] Invalid shares for {ticker}: {item['shares']}")
            continue

        # Cost basis may be None if Plaid doesn't have it (transferred positions)
        if item["cost_basis"] is not None:
            try:
                cost_basis = Decimal(str(round(item["cost_basis"], 4)))
            except Exception:
                logger.warning(f"[plaid] Invalid cost_basis for {ticker}: {item['cost_basis']}")
                cost_basis = Decimal("0")
        else:
            logger.warning(f"[plaid] No cost basis for {ticker} — defaulting to 0 (transferred position?)")
            cost_basis = Decimal("0")

        holdings.append(Holding(
            ticker=ticker,
            shares=shares,
            cost_basis=cost_basis,
            purchase_date=None,  # Use purchase_history JSON for per-lot dates
        ))

    logger.info(f"[plaid] Parsed {len(holdings)} Holding objects for {portfolio_name}")
    return holdings


def save_access_token_to_env(portfolio_name: str, access_token: str, env_path: str) -> None:
    """Write or update the Plaid access token in .env file."""
    from pathlib import Path

    key = _access_token_env_key(portfolio_name)
    env_file = Path(env_path)

    if not env_file.exists():
        env_file.write_text(f"{key}={access_token}\n")
        logger.info(f"Created {env_file} with {key}")
        return

    content = env_file.read_text(encoding="utf-8")
    lines = content.splitlines(keepends=True)

    updated = False
    new_lines = []
    for line in lines:
        if line.startswith(f"{key}="):
            new_lines.append(f"{key}={access_token}\n")
            updated = True
        else:
            new_lines.append(line)

    if not updated:
        new_lines.append(f"\n# Plaid access token — {portfolio_name}\n{key}={access_token}\n")

    env_file.write_text("".join(new_lines), encoding="utf-8")
    logger.info(f"Saved {key} to {env_file}")
