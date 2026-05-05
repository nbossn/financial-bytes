"""Fidelity automated positions scraper — Playwright-based.

Downloads current portfolio positions from Fidelity's web interface by logging
in with stored credentials and triggering the built-in CSV export. Works for
all account types: brokerage, IRA, trust (lilich), 401k, etc.

Setup — add to .env:
  FIDELITY_USERNAME=your_fidelity_username
  FIDELITY_PASSWORD=your_fidelity_password
  FIDELITY_2FA_SECRET=BASE32_TOTP_SECRET   # from Fidelity Security Center → 2FA setup

  # If lilich trust uses a SEPARATE Fidelity login:
  FIDELITY_LILICH_USERNAME=trust_username
  FIDELITY_LILICH_PASSWORD=trust_password
  FIDELITY_LILICH_2FA_SECRET=BASE32_TOTP_SECRET

Setup — portfolios.json:
  Add "fidelity_creds_prefix": "LILICH"   # uses FIDELITY_LILICH_* vars
  "fidelity_account_filter" already supported — filters by account name substring

ToS note: Automated Fidelity access is against their ToS for non-RIA use.
Personal use risk accepted — identical risk profile to Plaid credential-based
aggregation (Yodlee, Finicity, etc. screen-scrape Fidelity under the hood too).

2FA notes:
  - TOTP (Google Authenticator / Authy): store the Base32 setup key as
    FIDELITY_2FA_SECRET — pyotp generates codes automatically.
  - If no TOTP secret is stored, the scraper will pause and prompt
    interactively (requires headless=False or terminal access).
  - SMS / push notification 2FA cannot be automated — enroll a TOTP app.
"""
from __future__ import annotations

import csv
import io
import os
import tempfile
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from loguru import logger

_PROJECT_ROOT = Path(__file__).parent.parent.parent
load_dotenv(_PROJECT_ROOT / ".env", override=True)


# ── Exceptions ────────────────────────────────────────────────────────────────

class FidelityScraperError(Exception):
    """Raised when Fidelity scraping fails for any reason."""


class FidelityAuthError(FidelityScraperError):
    """Raised when login or 2FA fails."""


class FidelityCredentialError(FidelityScraperError):
    """Raised when required credentials are missing from .env."""


# ── Credentials ───────────────────────────────────────────────────────────────

def _get_credentials(creds_prefix: str = "") -> tuple[str, str, str | None]:
    """
    Read Fidelity credentials from env.

    creds_prefix="" → FIDELITY_USERNAME, FIDELITY_PASSWORD, FIDELITY_2FA_SECRET
    creds_prefix="LILICH" → FIDELITY_LILICH_USERNAME, …
    """
    prefix = f"FIDELITY_{creds_prefix.upper()}_" if creds_prefix else "FIDELITY_"
    username = os.getenv(f"{prefix}USERNAME", "").strip()
    password = os.getenv(f"{prefix}PASSWORD", "").strip()
    totp_secret = os.getenv(f"{prefix}2FA_SECRET", "").strip() or None

    if not username or not password:
        var_u = f"{prefix}USERNAME"
        var_p = f"{prefix}PASSWORD"
        raise FidelityCredentialError(
            f"{var_u} and {var_p} must be set in .env\n"
            "Credentials are your Fidelity.com login (username + password).\n"
            "For TOTP 2FA, also set "
            f"{prefix}2FA_SECRET with the Base32 key from Fidelity Security Center."
        )
    return username, password, totp_secret


def _totp_code(secret: str) -> str:
    """Generate current 6-digit TOTP code from Base32 secret."""
    try:
        import pyotp
        return pyotp.TOTP(secret).now()
    except ImportError:
        raise FidelityScraperError("pyotp not installed — run: pip install pyotp")


# ── Core scraper ──────────────────────────────────────────────────────────────

def _download_positions_csv(
    username: str,
    password: str,
    totp_secret: str | None,
    headless: bool = True,
) -> str:
    """
    Log into Fidelity and return the full positions CSV as a string.

    Raises FidelityAuthError on login/2FA failure.
    Raises FidelityScraperError on navigation or download failure.
    """
    try:
        from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
    except ImportError:
        raise FidelityScraperError(
            "playwright not installed — run:\n"
            "  pip install playwright\n"
            "  playwright install chromium"
        )

    csv_content: str | None = None

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        context = browser.new_context(
            viewport={"width": 1280, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
        )
        page = context.new_page()

        try:
            # ── 1. Login ───────────────────────────────────────────────────
            logger.debug("[fidelity] Opening login page")
            page.goto(
                "https://digital.fidelity.com/prgw/digital/login/full-page",
                timeout=30_000,
            )
            page.wait_for_load_state("networkidle", timeout=20_000)

            # Username
            user_sel = "#userId-input, input[name='username'], input[id*='user']"
            page.locator(user_sel).first.fill(username)

            # "Continue" / "Next" button (some flows split username + password)
            try:
                cont = page.locator(
                    "button:has-text('Continue'), button:has-text('Next'), "
                    "#username-submit, button[type='submit']"
                ).first
                cont.click(timeout=4_000)
                page.wait_for_load_state("networkidle", timeout=10_000)
            except PWTimeout:
                pass  # single-page login, no intermediate step

            # Password
            pass_sel = "#password, input[name='password'], input[type='password']"
            pw_field = page.locator(pass_sel).first
            pw_field.wait_for(state="visible", timeout=12_000)
            pw_field.fill(password)

            login_btn = page.locator(
                "#fs-login-button, button[type='submit'], "
                "button:has-text('Log In'), button:has-text('Sign In')"
            ).first
            login_btn.click()
            page.wait_for_load_state("networkidle", timeout=25_000)

            # ── 2. 2FA ────────────────────────────────────────────────────
            mfa_indicators = [
                "input[name*='code']",
                "input[id*='code']",
                "input[placeholder*='code']",
                "input[aria-label*='code']",
                "#security-code",
            ]
            mfa_visible = any(
                page.locator(sel).is_visible(timeout=2_000)
                for sel in mfa_indicators
            )

            if mfa_visible or "mfa" in page.url.lower() or "2fa" in page.url.lower():
                logger.info("[fidelity] 2FA prompt detected")

                if totp_secret:
                    code = _totp_code(totp_secret)
                    logger.debug("[fidelity] Generated TOTP code")
                else:
                    if headless:
                        raise FidelityAuthError(
                            "Fidelity is asking for a 2FA code but FIDELITY_2FA_SECRET is not set.\n"
                            "Options:\n"
                            "  1. Set FIDELITY_2FA_SECRET in .env (TOTP Base32 key from Fidelity Security Center)\n"
                            "  2. Run fidelity-sync --no-headless to enter the code manually"
                        )
                    code = input("Fidelity 2FA code: ").strip()

                code_field = page.locator(", ".join(mfa_indicators)).first
                code_field.fill(code)

                submit = page.locator(
                    "button[type='submit'], button:has-text('Submit'), "
                    "button:has-text('Verify'), button:has-text('Continue')"
                ).first
                submit.click()
                page.wait_for_load_state("networkidle", timeout=20_000)

            # Confirm we're not back on login
            if "login" in page.url.lower() or "ftgw/Fas" in page.url:
                raise FidelityAuthError(
                    "Login failed — check FIDELITY_USERNAME and FIDELITY_PASSWORD.\n"
                    f"Landed on: {page.url}"
                )

            logger.info("[fidelity] Login successful")

            # ── 3. Navigate to Positions ───────────────────────────────────
            page.goto(
                "https://digital.fidelity.com/prgw/digital/portfolio/positions",
                timeout=30_000,
            )
            page.wait_for_load_state("networkidle", timeout=25_000)

            if "login" in page.url.lower():
                raise FidelityAuthError("Redirected to login after navigation — session may have expired")

            # Wait for the positions table to render
            try:
                page.wait_for_selector(
                    "table[aria-label*='Position'], [data-testid*='position'], "
                    ".positions-table, table.ag-header",
                    timeout=15_000,
                )
            except PWTimeout:
                logger.warning("[fidelity] Positions table selector not found — proceeding anyway")

            # ── 4. Download CSV ────────────────────────────────────────────
            # Fidelity's download button is sometimes behind a "..." menu.
            # Strategy: try direct button first, then look for overflow menu.

            download_selectors = [
                "button[aria-label*='Download'], button[aria-label*='download']",
                "button[aria-label*='Export'], button[aria-label*='export']",
                "[data-testid='download-button'], [data-testid='export-button']",
                "button:has-text('Download'), a:has-text('Download')",
                "li:has-text('Download CSV'), [role='menuitem']:has-text('Download')",
            ]

            overflow_selectors = [
                "button[aria-label*='more options'], button[aria-label*='More']",
                "button[aria-label*='...'], button:has-text('...')",
                "[data-testid='overflow-menu'], button[aria-haspopup='menu']",
            ]

            def _click_download() -> None:
                for sel in download_selectors:
                    try:
                        btn = page.locator(sel).first
                        if btn.is_visible(timeout=2_000):
                            btn.click()
                            return
                    except PWTimeout:
                        continue

                # Try overflow menu → download item
                for sel in overflow_selectors:
                    try:
                        btn = page.locator(sel).first
                        if btn.is_visible(timeout=2_000):
                            btn.click()
                            page.wait_for_timeout(600)
                            for dl_sel in download_selectors:
                                try:
                                    dl = page.locator(dl_sel).first
                                    if dl.is_visible(timeout=1_500):
                                        dl.click()
                                        return
                                except PWTimeout:
                                    continue
                    except PWTimeout:
                        continue

                raise FidelityScraperError(
                    "Could not locate the Download button on the Positions page.\n"
                    "Fidelity may have updated their UI. Run with --no-headless to debug."
                )

            with page.expect_download(timeout=30_000) as dl_info:
                _click_download()

            download = dl_info.value
            with tempfile.NamedTemporaryFile(suffix=".csv", delete=False) as tmp:
                tmp_path = tmp.name
            download.save_as(tmp_path)
            csv_content = Path(tmp_path).read_text(encoding="utf-8", errors="replace")
            Path(tmp_path).unlink(missing_ok=True)

            logger.info(f"[fidelity] Downloaded positions CSV ({len(csv_content):,} bytes)")

        except FidelityScraperError:
            raise
        except PWTimeout as e:
            raise FidelityScraperError(f"Page timed out during scrape: {e}") from e
        except Exception as e:
            raise FidelityScraperError(f"Unexpected error during Fidelity scrape: {e}") from e
        finally:
            context.close()
            browser.close()

    if not csv_content:
        raise FidelityScraperError("Download completed but CSV content is empty")

    return csv_content


# ── Public API ────────────────────────────────────────────────────────────────

def sync_fidelity_raw(
    portfolio_name: str,
    creds_prefix: str = "",
    account_filter: str | None = None,
    headless: bool = True,
) -> list[dict]:
    """
    Download Fidelity positions and return as list of raw CSV row dicts.

    Args:
        portfolio_name: Used for logging only.
        creds_prefix: Env prefix for credentials (e.g. "LILICH" → FIDELITY_LILICH_*)
        account_filter: If set, only rows where 'Account Name' contains this string.
        headless: Run browser headless (set False to debug interactively).
    """
    username, password, totp_secret = _get_credentials(creds_prefix)
    logger.info(f"[fidelity] Syncing positions for {portfolio_name} (user: {username[:3]}***)")

    csv_text = _download_positions_csv(username, password, totp_secret, headless=headless)

    rows: list[dict] = []
    reader = csv.DictReader(io.StringIO(csv_text))
    for row in reader:
        account_name = row.get("Account Name", row.get("Account Name ", "")).strip()
        if account_filter and account_filter.lower() not in account_name.lower():
            continue
        rows.append(dict(row))

    logger.info(f"[fidelity] Parsed {len(rows)} raw rows for {portfolio_name}")
    return rows


def read_fidelity_live(
    portfolio_name: str,
    creds_prefix: str = "",
    account_filter: str | None = None,
    headless: bool = True,
) -> list:
    """
    Download live Fidelity positions and return as list[Holding].

    Delegates CSV parsing to the existing fidelity_reader module.
    """
    from src.portfolio.fidelity_reader import read_fidelity_positions

    # Download and write to temp CSV, then parse with the existing reader
    username, password, totp_secret = _get_credentials(creds_prefix)
    csv_text = _download_positions_csv(username, password, totp_secret, headless=headless)

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".csv", delete=False, encoding="utf-8"
    ) as tmp:
        tmp.write(csv_text)
        tmp_path = tmp.name

    try:
        holdings = read_fidelity_positions(tmp_path, account_filter=account_filter)
    finally:
        Path(tmp_path).unlink(missing_ok=True)

    logger.info(f"[plaid] Parsed {len(holdings)} Holding objects for {portfolio_name}")
    return holdings
