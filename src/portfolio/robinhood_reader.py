"""
Robinhood live position reader — uses robin_stocks (unofficial, personal use only).

Pulls current holdings directly from Robinhood's internal API.
Returns list[Holding] compatible with the rest of the financial-bytes pipeline.

Credentials: ROBINHOOD_EMAIL, ROBINHOOD_PASSWORD, ROBINHOOD_MFA_SECRET in .env
MFA:         ROBINHOOD_MFA_SECRET is the Base32 TOTP secret from Robinhood 2FA setup.
             Not your 6-digit code — the secret you scan or copy during 2FA enrollment.
             If you can't find it, re-enroll 2FA in Robinhood settings and copy the secret.

Warning:     robin_stocks reverse-engineers Robinhood's internal OAuth API.
             Use is technically against Robinhood ToS. Nick has accepted this risk (2026-04-25).
"""
from __future__ import annotations

import os
from datetime import date
from decimal import Decimal, InvalidOperation

from loguru import logger

from src.portfolio.models import Holding


class RobinhoodAuthError(Exception):
    """Raised when login fails — credentials missing, MFA wrong, or Robinhood changed their API."""


class RobinhoodReadError(Exception):
    """Raised when holdings can't be parsed after a successful login."""


def _login(email: str, password: str, mfa_secret: str | None) -> None:
    """Authenticate with Robinhood. Raises RobinhoodAuthError on failure."""
    try:
        import robin_stocks.robinhood as rh
    except ImportError as e:
        raise RobinhoodAuthError("robin_stocks not installed — run: pip install robin_stocks") from e

    mfa_code = None
    if mfa_secret:
        try:
            import pyotp
            mfa_code = pyotp.TOTP(mfa_secret).now()
            logger.debug("Generated TOTP MFA code from secret")
        except ImportError:
            logger.warning("pyotp not installed — attempting login without MFA code")
        except Exception as e:
            logger.warning(f"TOTP generation failed: {e} — attempting login without MFA code")

    try:
        if mfa_code:
            rh.login(email, password, mfa_code=mfa_code, store_session=False)
        else:
            rh.login(email, password, store_session=False)
        logger.info("[robinhood] Login successful")
    except Exception as e:
        raise RobinhoodAuthError(f"Robinhood login failed: {e}") from e


def _logout() -> None:
    """Logout and clean up session tokens."""
    try:
        import robin_stocks.robinhood as rh
        rh.logout()
        logger.debug("[robinhood] Logged out")
    except Exception as e:
        logger.debug(f"[robinhood] Logout error (non-critical): {e}")


def read_robinhood_holdings() -> list[Holding]:
    """
    Fetch live Robinhood holdings and return as list[Holding].

    Reads credentials from env: ROBINHOOD_EMAIL, ROBINHOOD_PASSWORD, ROBINHOOD_MFA_SECRET.
    purchase_date is set to None — Robinhood's holdings endpoint doesn't expose per-position
    open dates; use the transactions import path if you need accurate date history.

    Raises:
        RobinhoodAuthError: credential issue or API change
        RobinhoodReadError: holdings parse failure after successful login
    """
    email = os.environ.get("ROBINHOOD_EMAIL") or ""
    password = os.environ.get("ROBINHOOD_PASSWORD") or ""
    mfa_secret = os.environ.get("ROBINHOOD_MFA_SECRET") or ""

    if not email or not password:
        raise RobinhoodAuthError(
            "ROBINHOOD_EMAIL and ROBINHOOD_PASSWORD must be set in .env. "
            "ROBINHOOD_MFA_SECRET is required if 2FA is enabled (recommended)."
        )

    _login(email, password, mfa_secret or None)

    try:
        import robin_stocks.robinhood as rh

        raw = rh.account.build_holdings()
        if not raw:
            raise RobinhoodReadError("build_holdings() returned empty — no positions found")

        holdings: list[Holding] = []
        skipped = 0

        for ticker, data in raw.items():
            ticker = ticker.strip().upper()
            if not ticker:
                skipped += 1
                continue

            # Skip options, crypto, and non-equity positions the pipeline can't handle
            position_type = (data.get("type") or "stock").lower()
            if position_type not in ("stock", "adr", "etp"):
                logger.debug(f"[robinhood] Skipping {ticker} (type={position_type})")
                skipped += 1
                continue

            try:
                shares = Decimal(str(data.get("quantity") or "0"))
                if shares <= 0:
                    logger.debug(f"[robinhood] Skipping {ticker} — zero quantity")
                    skipped += 1
                    continue

                cost_basis_str = data.get("average_buy_price") or "0"
                cost_basis = Decimal(str(cost_basis_str))
                if cost_basis <= 0:
                    logger.warning(f"[robinhood] {ticker} has zero cost basis — skipping to avoid division errors")
                    skipped += 1
                    continue

                holdings.append(
                    Holding(
                        ticker=ticker,
                        shares=shares,
                        cost_basis=cost_basis,
                        purchase_date=None,  # not available from holdings endpoint
                    )
                )
                logger.debug(f"[robinhood] {ticker}: {shares} shares @ ${cost_basis}")

            except (InvalidOperation, ValueError, TypeError) as e:
                logger.warning(f"[robinhood] Could not parse {ticker}: {e} — skipping")
                skipped += 1
                continue

        if not holdings:
            raise RobinhoodReadError(
                f"No valid equity holdings parsed (skipped {skipped} positions). "
                "Crypto, options, and zero-quantity positions are excluded."
            )

        logger.info(
            f"[robinhood] {len(holdings)} holdings loaded, {skipped} skipped: "
            f"{[h.ticker for h in holdings]}"
        )
        return holdings

    except RobinhoodReadError:
        raise
    except Exception as e:
        raise RobinhoodReadError(f"Failed to parse Robinhood holdings: {e}") from e
    finally:
        _logout()


def export_robinhood_to_csv(holdings: list[Holding], output_path: str | None = None) -> str:
    """
    Write robinhood holdings to a portfolio CSV, compatible with read_portfolio().
    Returns the path written.
    """
    import csv
    from pathlib import Path

    path = Path(output_path or "portfolio-robinhood.csv")
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["ticker", "shares", "cost_basis", "purchase_date"])
        writer.writeheader()
        for h in holdings:
            writer.writerow({
                "ticker": h.ticker,
                "shares": str(h.shares),
                "cost_basis": str(h.cost_basis),
                "purchase_date": str(h.purchase_date) if h.purchase_date else "",
            })

    logger.info(f"[robinhood] Portfolio written to {path} ({len(holdings)} holdings)")
    return str(path)
