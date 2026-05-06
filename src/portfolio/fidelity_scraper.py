"""Fidelity automated positions scraper — DrissionPage + Windows Chrome.

Visual-first architecture: at every navigation step a screenshot is taken and
classified by Claude Haiku. The scraper knows exactly where it is in the
Fidelity flow at all times — login, MFA, positions loaded, bot challenge —
and reacts to what it *sees*, not to what URL it expects.

Key improvements over Selenium:
  1. DrissionPage CDP mode — no WebDriver protocol, no Runtime.enable, minimal
     CDP footprint that Akamai cannot distinguish from a real DevTools session.
  2. Visual state machine — screenshot → Claude Haiku → PageState → act.
     Handles unexpected Akamai interstitials, UI changes, MFA variants, and
     anything else that doesn't match a hardcoded URL pattern.
  3. Re-enabled automated login — TOTP generated from .env, entered when
     MFA_TOTP state is detected visually.
  4. Hardened download — visual confirmation that positions table is fully
     loaded before attempting download click.
  5. Debug screenshots saved on every failure for post-mortem analysis.

Setup — add to .env:
  FIDELITY_USERNAME=your_fidelity_username
  FIDELITY_PASSWORD=your_fidelity_password
  FIDELITY_2FA_SECRET=BASE32_TOTP_SECRET   # from Fidelity Security Center
  ANTHROPIC_API_KEY=...                    # for visual page classification

  # lilich trust (separate login):
  FIDELITY_LILICH_USERNAME=trust_username
  FIDELITY_LILICH_PASSWORD=trust_password
  FIDELITY_LILICH_2FA_SECRET=BASE32_TOTP_SECRET

Windows binary (auto-detected, override via env):
  FIDELITY_CHROME_BIN  — default: C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe
  FIDELITY_DEBUG_PORT  — default: 9222 (Chrome remote debugging port)
"""
from __future__ import annotations

import atexit
import base64
import csv
import glob
import io
import json
import os
import random
import subprocess
import tempfile
import time
from enum import Enum
from pathlib import Path

from dotenv import load_dotenv
from loguru import logger

_PROJECT_ROOT = Path(__file__).parent.parent.parent
load_dotenv(_PROJECT_ROOT / ".env", override=True)

# ── Constants ─────────────────────────────────────────────────────────────────

_BACKOFF_BASE_SECS = 45
_MAX_LOGIN_ATTEMPTS = 4

# Windows paths (Chrome is a Windows process, paths must be Windows format)
try:
    _result = subprocess.run(
        ["cmd.exe", "/c", "echo", "%USERNAME%"],
        capture_output=True, text=True, timeout=5,
    )
    _WIN_USERNAME = _result.stdout.strip()
except Exception:
    _WIN_USERNAME = os.getenv("USERNAME", "nicky")
if not _WIN_USERNAME or "%" in _WIN_USERNAME:
    _WIN_USERNAME = os.getenv("USERNAME", "nicky")

_WIN_CHROME_BIN = os.getenv(
    "FIDELITY_CHROME_BIN",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
)
# WSL path for subprocess.Popen (transparent Windows EXE execution)
_WSL_CHROME_BIN = _WIN_CHROME_BIN.replace("C:\\", "/mnt/c/").replace("\\", "/")

_WIN_PROFILE_DIR = rf"C:\Users\{_WIN_USERNAME}\AppData\Local\fidelity_automation\chrome_profile"
_WIN_DL_DIR = rf"C:\Users\{_WIN_USERNAME}\AppData\Local\Temp\fidelity_dl"
_WSL_DL_DIR = _WIN_DL_DIR.replace("C:\\", "/mnt/c/").replace("\\", "/")

_COOKIE_DIR = _PROJECT_ROOT / "data" / "fidelity_sessions"
_DEBUG_DIR = _PROJECT_ROOT / "data" / "fidelity_debug"
_DEBUG_PORT = int(os.getenv("FIDELITY_DEBUG_PORT", "9222"))


# ── Page state ────────────────────────────────────────────────────────────────

class PageState(str, Enum):
    LOGIN             = "login"             # username/password form
    MFA_TOTP          = "mfa_totp"          # 6-digit authenticator code entry
    MFA_OTHER         = "mfa_other"         # SMS / push / security question
    PORTFOLIO         = "portfolio"          # portfolio summary (no positions table)
    POSITIONS_LOADING = "positions_loading" # positions page, data still loading
    POSITIONS_READY   = "positions_ready"   # positions table fully loaded
    BOT_CHALLENGE     = "bot_challenge"     # Akamai CAPTCHA / challenge page
    ACCESS_DENIED     = "access_denied"     # 403 / security block
    UNKNOWN           = "unknown"           # unrecognised state


# ── Exceptions ────────────────────────────────────────────────────────────────

class FidelityScraperError(Exception):
    """Raised when Fidelity scraping fails for any reason."""


class FidelityAuthError(FidelityScraperError):
    """Raised when login fails after all retries."""


class FidelityCredentialError(FidelityScraperError):
    """Raised when required credentials are missing from .env."""


# ── Credentials ───────────────────────────────────────────────────────────────

def _get_credentials(creds_prefix: str = "") -> tuple[str, str, str | None]:
    prefix = f"FIDELITY_{creds_prefix.upper()}_" if creds_prefix else "FIDELITY_"
    username = os.getenv(f"{prefix}USERNAME", "").strip()
    password = os.getenv(f"{prefix}PASSWORD", "").strip()
    totp_secret = os.getenv(f"{prefix}2FA_SECRET", "").strip() or None
    if not username or not password:
        var_u, var_p = f"{prefix}USERNAME", f"{prefix}PASSWORD"
        raise FidelityCredentialError(
            f"{var_u} and {var_p} must be set in .env\n"
            "These are your Fidelity.com login credentials.\n"
            f"For TOTP 2FA, also set {prefix}2FA_SECRET (Base32 key from Fidelity Security Center)."
        )
    return username, password, totp_secret


def _totp_code(secret: str) -> str:
    try:
        import pyotp
        return pyotp.TOTP(secret).now()
    except ImportError:
        raise FidelityScraperError("pyotp not installed — run: pip install pyotp")


# ── Cookie persistence ────────────────────────────────────────────────────────

def _cookie_path(creds_prefix: str) -> Path:
    key = creds_prefix.upper() if creds_prefix else "DEFAULT"
    return _COOKIE_DIR / f"fidelity_{key}_cookies.json"


def _save_cookies(page, creds_prefix: str) -> None:
    _COOKIE_DIR.mkdir(parents=True, exist_ok=True)
    path = _cookie_path(creds_prefix)
    cookies = page.cookies(all_info=True)
    path.write_text(json.dumps(cookies, indent=2), encoding="utf-8")
    logger.info(f"[fidelity] Cookies saved → {path.name} ({len(cookies)} cookies)")


def _load_cookies(page, creds_prefix: str) -> bool:
    """Load saved cookies into DrissionPage. Returns True if file existed."""
    path = _cookie_path(creds_prefix)
    if not path.exists():
        return False
    try:
        cookies = json.loads(path.read_text(encoding="utf-8"))
        # Navigate to domain first so DrissionPage can set cookies
        page.get("https://digital.fidelity.com")
        time.sleep(2)
        page.set.cookies(cookies)
        logger.info(f"[fidelity] Loaded {len(cookies)} cookies from {path.name}")
        return True
    except Exception as e:
        logger.warning(f"[fidelity] Could not load cookies ({e}) — will re-authenticate")
        return False


# ── Stealth script ────────────────────────────────────────────────────────────

# Injected via DrissionPage's add_init_js() before any page JS runs.
# Key change from old Selenium approach: delete webdriver from the Navigator
# *prototype* (not just the instance), which survives property-descriptor checks.
# Plugin spoof removed — real Windows Chrome reports correct plugins natively;
# our old 2-plugin spoof was a known Akamai fingerprint marker.
_STEALTH_INIT_SCRIPT = """
(function() {
    // 1. Delete navigator.webdriver from prototype — survives descriptor checks
    try { delete Object.getPrototypeOf(navigator).webdriver; } catch(e) {}

    // 2. Realistic language list
    Object.defineProperty(navigator, 'languages', {
        get: () => ['en-US', 'en'], configurable: true, enumerable: true
    });

    // 3. Correct Chrome frame height for Windows 11 (title bar + tab strip + URL bar)
    Object.defineProperty(window, 'outerHeight', {
        get: () => window.innerHeight + 87, configurable: true
    });
    Object.defineProperty(window, 'outerWidth', {
        get: () => window.innerWidth, configurable: true
    });

    // 4. Remove automation artifacts from window
    const _artifacts = [
        'cdc_adoQpoasnfa76pfcZLmcfl_Array',
        'cdc_adoQpoasnfa76pfcZLmcfl_Promise',
        'cdc_adoQpoasnfa76pfcZLmcfl_Symbol',
        '__webdriver_script_fn',
        '__playwright', '__pw_manual',
        '$chrome_asyncScriptInfo',
    ];
    _artifacts.forEach(k => { try { delete window[k]; } catch(e) {} });

    // 5. Permissions API — ensure notifications returns 'default' not 'denied'
    //    (headless Chrome sometimes returns 'denied', which Akamai checks)
    const _origPermsQuery = window.Permissions && window.Permissions.prototype.query;
    if (_origPermsQuery) {
        window.Permissions.prototype.query = function(perm) {
            return _origPermsQuery.call(this, perm).then(r => {
                if (perm && perm.name === 'notifications') {
                    try {
                        Object.defineProperty(r, 'state', {
                            value: 'default', configurable: true
                        });
                    } catch(e) {}
                }
                return r;
            });
        };
    }
})();
"""


# ── Windows host IP ───────────────────────────────────────────────────────────

def _get_windows_host_ip() -> str:
    """Return the Windows host IP reachable from WSL2 (via default gateway)."""
    try:
        result = subprocess.run(
            ["ip", "route", "show", "default"],
            capture_output=True, text=True, timeout=5,
        )
        for line in result.stdout.splitlines():
            parts = line.split()
            if "via" in parts:
                return parts[parts.index("via") + 1]
    except Exception:
        pass
    return "172.31.32.1"


# ── Chrome launcher + DrissionPage driver ────────────────────────────────────

_chrome_proc: subprocess.Popen | None = None


def _atexit_chrome_cleanup() -> None:
    """Terminate the Chrome subprocess on interpreter exit (prevents orphans)."""
    global _chrome_proc
    if _chrome_proc is not None:
        try:
            _chrome_proc.terminate()
        except Exception:
            pass
        _chrome_proc = None


atexit.register(_atexit_chrome_cleanup)


def _kill_existing_chrome_debug() -> None:
    """Kill any Chrome instance holding our debug port."""
    try:
        subprocess.run(
            ["fuser", "-k", f"{_DEBUG_PORT}/tcp"],
            capture_output=True, timeout=5,
        )
        time.sleep(1)
    except Exception:
        pass


def _make_driver(headless: bool = False):
    """
    Launch Windows Chrome from WSL2 with debugging port on 0.0.0.0,
    then connect DrissionPage in existing_only mode via the Windows host IP.

    Why self-launch instead of DrissionPage's built-in launch:
      - Chrome must bind on 0.0.0.0 (--remote-debugging-address=0.0.0.0) so
        WSL2 Linux can reach it via the Windows gateway IP.
      - We pass Windows-format paths (user-data-dir, download dir) which Chrome
        interprets correctly since it runs as a Windows process.
      - This matches the architecture of the old Selenium approach but without
        the chromedriver.exe layer.
    """
    global _chrome_proc
    from DrissionPage import ChromiumPage, ChromiumOptions

    win_ip = _get_windows_host_ip()
    logger.info(f"[fidelity] Windows host IP: {win_ip}")

    # Ensure download and debug dirs exist
    Path(_WSL_DL_DIR).mkdir(parents=True, exist_ok=True)
    _DEBUG_DIR.mkdir(parents=True, exist_ok=True)

    # Kill any stale Chrome on our debug port
    _kill_existing_chrome_debug()

    # Chrome launch args (Windows process — uses Windows path format)
    chrome_args = [
        _WSL_CHROME_BIN,
        f"--remote-debugging-port={_DEBUG_PORT}",
        "--remote-debugging-address=0.0.0.0",        # WSL2 accessibility
        f"--user-data-dir={_WIN_PROFILE_DIR}",        # Windows path
        "--disable-blink-features=AutomationControlled",  # PRIMARY stealth fix
        "--window-size=1536,864",                     # common real-user size
        "--lang=en-US",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-notifications",
        "--use-angle=default",                        # real GPU, NOT SwiftShader
        # Block WebRTC from leaking WSL2 172.x.x.x internal IP to Akamai
        "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
        # Download directory (Windows path for Chrome's file system)
        f"--download-default-directory={_WIN_DL_DIR}",
        # Remove automation extension flags
        "--disable-extensions",
        "--no-service-autorun",
    ]
    if headless:
        chrome_args.append("--headless=new")

    logger.info(f"[fidelity] Launching Windows Chrome on port {_DEBUG_PORT}…")
    _proc = subprocess.Popen(
        chrome_args,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    # Wait for Chrome to bind its debugging port.
    # Assign _chrome_proc AFTER the port is confirmed open so that if the
    # wait times out (Chrome failed to start), we still terminate the process
    # and don't leave a global pointing at an orphan.
    try:
        _wait_for_port(win_ip, _DEBUG_PORT, timeout=15)
    except FidelityScraperError:
        _proc.terminate()
        raise

    _chrome_proc = _proc

    # Connect DrissionPage (existing_only — don't re-launch Chrome)
    opts = ChromiumOptions()
    opts.existing_only()
    opts.set_address(f"{win_ip}:{_DEBUG_PORT}")

    page = ChromiumPage(addr_or_opts=opts)

    # Inject stealth patches before any page navigation
    page.add_init_js(_STEALTH_INIT_SCRIPT)

    logger.info("[fidelity] DrissionPage connected to Windows Chrome")
    return page


def _wait_for_port(host: str, port: int, timeout: float = 15.0) -> None:
    """Poll until host:port is accepting connections."""
    import socket
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=1):
                return
        except (ConnectionRefusedError, OSError):
            time.sleep(0.5)
    raise FidelityScraperError(
        f"Chrome debug port {host}:{port} did not open within {timeout}s"
    )


def _quit_driver(page) -> None:
    """Close DrissionPage and the Chrome subprocess."""
    global _chrome_proc
    try:
        page.quit()
    except Exception:
        pass
    if _chrome_proc is not None:
        try:
            _chrome_proc.terminate()
            _chrome_proc.wait(timeout=5)
        except Exception:
            pass
        _chrome_proc = None


# ── Screenshot + visual state machine ────────────────────────────────────────

def _take_screenshot(page) -> bytes:
    """Capture the current browser state as PNG bytes."""
    try:
        data = page.get_screenshot(as_bytes=True)
        return data if isinstance(data, bytes) else b""
    except Exception as e:
        logger.warning(f"[fidelity] Screenshot failed: {e}")
        return b""


def _resize_screenshot(png_bytes: bytes, max_width: int = 1024) -> bytes:
    """Resize PNG to reduce Claude API token usage."""
    try:
        from PIL import Image
        import io as _io
        img = Image.open(_io.BytesIO(png_bytes))
        if img.width > max_width:
            ratio = max_width / img.width
            img = img.resize((max_width, int(img.height * ratio)), Image.LANCZOS)
        buf = _io.BytesIO()
        img.save(buf, format="PNG", optimize=True)
        return buf.getvalue()
    except Exception:
        return png_bytes


def _save_debug_screenshot(png_bytes: bytes, label: str) -> None:
    """Save a labelled screenshot to data/fidelity_debug/ for post-mortem."""
    if not png_bytes:
        return
    ts = time.strftime("%Y%m%d_%H%M%S")
    path = _DEBUG_DIR / f"{ts}_{label}.png"
    try:
        _DEBUG_DIR.mkdir(parents=True, exist_ok=True)
        path.write_bytes(png_bytes)
        logger.info(f"[fidelity] Debug screenshot → {path.name}")
    except Exception:
        pass


def _identify_page_state(png_bytes: bytes, url: str) -> tuple[PageState, str]:
    """
    Use Claude Haiku vision to classify the current browser page state.

    Falls back to URL-based heuristics if ANTHROPIC_API_KEY is not set or
    if the Haiku call fails — so the scraper degrades gracefully.
    """
    api_key = os.getenv("ANTHROPIC_API_KEY", "").strip()

    if not api_key or not png_bytes:
        state = _url_fallback_state(url)
        return state, "url-heuristic (no API key)"

    try:
        from anthropic import Anthropic
        client = Anthropic(api_key=api_key)

        img_b64 = base64.standard_b64encode(
            _resize_screenshot(png_bytes, max_width=1024)
        ).decode()

        prompt = (
            "Classify this Fidelity financial website screenshot into EXACTLY ONE state.\n"
            f"URL: {url[:120]}\n\n"
            "States:\n"
            "LOGIN — username/password login form is visible\n"
            "MFA_TOTP — 6-digit authenticator app code entry field is visible\n"
            "MFA_OTHER — SMS, email, push notification, or security question MFA\n"
            "PORTFOLIO — portfolio summary page (no detailed positions table)\n"
            "POSITIONS_LOADING — positions page but data still loading (spinner/skeleton)\n"
            "POSITIONS_READY — positions table fully loaded with ticker symbols and values\n"
            "BOT_CHALLENGE — CAPTCHA, slider, or Akamai bot verification challenge\n"
            "ACCESS_DENIED — access denied, security block, or error page\n"
            "UNKNOWN — none of the above\n\n"
            'Respond with JSON only: {"state": "STATE_NAME", "confidence": 0-100, "detail": "one sentence"}'
        )

        response = client.messages.create(
            model="claude-haiku-4-5",
            max_tokens=120,
            messages=[{
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/png",
                            "data": img_b64,
                        },
                    },
                    {"type": "text", "text": prompt},
                ],
            }],
        )

        result = json.loads(response.content[0].text.strip())
        raw = result.get("state", "UNKNOWN").upper()
        state_map = {
            "LOGIN":             PageState.LOGIN,
            "MFA_TOTP":          PageState.MFA_TOTP,
            "MFA_OTHER":         PageState.MFA_OTHER,
            "PORTFOLIO":         PageState.PORTFOLIO,
            "POSITIONS_LOADING": PageState.POSITIONS_LOADING,
            "POSITIONS_READY":   PageState.POSITIONS_READY,
            "BOT_CHALLENGE":     PageState.BOT_CHALLENGE,
            "ACCESS_DENIED":     PageState.ACCESS_DENIED,
        }
        state = state_map.get(raw, PageState.UNKNOWN)
        detail = result.get("detail", "")
        logger.info(
            f"[fidelity] State: {state.value} "
            f"({result.get('confidence', '?')}%) — {detail}"
        )
        return state, detail

    except Exception as e:
        logger.warning(f"[fidelity] Visual classification error ({e}) — URL fallback")
        return _url_fallback_state(url), f"fallback: {e}"


def _url_fallback_state(url: str) -> PageState:
    """Best-effort page state from URL when Haiku is unavailable."""
    u = url.lower()
    if "prgw/digital/login" in u:
        return PageState.LOGIN
    if "signin" in u or "/mfa" in u or "/2fa" in u:
        return PageState.MFA_TOTP
    if "ftgw/digital/portfolio/positions" in u:
        return PageState.POSITIONS_LOADING
    if "ftgw/digital/portfolio" in u:
        return PageState.PORTFOLIO
    return PageState.UNKNOWN


def _wait_for_stable_state(
    page,
    timeout: float = 30.0,
    poll_interval: float = 2.5,
    target_states: set[PageState] | None = None,
) -> tuple[PageState, str, bytes]:
    """
    Poll via screenshot + Haiku until the page reaches a recognisable,
    non-loading state (or times out).

    Returns (state, detail, screenshot_bytes).
    """
    if target_states is None:
        target_states = {
            PageState.LOGIN, PageState.MFA_TOTP, PageState.MFA_OTHER,
            PageState.PORTFOLIO, PageState.POSITIONS_READY,
            PageState.BOT_CHALLENGE, PageState.ACCESS_DENIED,
            # UNKNOWN intentionally excluded — a transient misclassification
            # shouldn't short-circuit the poll loop prematurely.
        }

    deadline = time.time() + timeout
    last: tuple[PageState, str, bytes] = (PageState.UNKNOWN, "", b"")

    while time.time() < deadline:
        time.sleep(poll_interval)
        screenshot = _take_screenshot(page)
        state, detail = _identify_page_state(screenshot, page.url)
        last = (state, detail, screenshot)
        if state in target_states:
            return last
        logger.debug(f"[fidelity] Waiting for stable state (current: {state.value})…")

    logger.warning(f"[fidelity] State wait timed out — last: {last[0].value}")
    return last


# ── Human simulation ──────────────────────────────────────────────────────────

# Common English letter-pair timing (milliseconds → seconds here).
# Fast bigrams type closer together; slow ones apart.
_FAST_BIGRAMS = {"th", "er", "in", "re", "en", "on", "he", "at", "es", "an",
                 "st", "or", "is", "it", "al", "ar", "nd", "to"}
_SLOW_BIGRAMS = {"qu", "xz", "zx", "qj", "jq", "vx", "xv", "bz", "zb"}


def _human_type(page, element, text: str) -> None:
    """
    Type text into an element character by character using Actions.type().
    Actions.type() dispatches OS-level key events (isTrusted=True).
    Timing follows a bigram model with occasional burst/pause patterns.
    """
    from DrissionPage.common import Actions
    actions = Actions(page)

    # Click to focus
    _human_click(page, element)
    time.sleep(random.uniform(0.3, 0.6))

    prev_char = ""
    for char in text:
        pair = (prev_char + char).lower()
        if pair in _FAST_BIGRAMS:
            delay = random.uniform(0.05, 0.12)
        elif pair in _SLOW_BIGRAMS:
            delay = random.uniform(0.20, 0.42)
        else:
            delay = random.uniform(0.09, 0.19)

        # Occasional thinking pause (word boundary / end of a chunk)
        if random.random() < 0.07:
            delay += random.uniform(0.15, 0.50)

        actions.type(char)
        time.sleep(delay)
        prev_char = char


def _human_browse(page) -> None:
    """
    Simulate a human settling onto a freshly loaded page before interacting.
    Hover over a few elements, do a small scroll, then settle.
    """
    from DrissionPage.common import Actions

    # Initial reading pause — let Akamai sensor_data scripts finish collecting
    time.sleep(random.uniform(1.5, 2.5))

    try:
        actions = Actions(page)
        elements = page.eles("css:a") + page.eles("css:button") + page.eles("css:p")
        visible = [e for e in elements[:25] if e.states.is_displayed]
        targets = random.sample(visible, min(random.randint(2, 5), len(visible)))

        for el in targets:
            try:
                # Small random offset — real users don't hover dead-centre
                ox = random.randint(-4, 4)
                oy = random.randint(-3, 3)
                actions.move_to(el, offset_x=ox, offset_y=oy)
                time.sleep(random.uniform(0.12, 0.38))
            except Exception:
                pass

        # Brief scroll and return
        scroll_amt = random.randint(60, 160)
        actions.scroll(scroll_amt)
        time.sleep(random.uniform(0.4, 0.9))
        actions.scroll(-scroll_amt)
        time.sleep(random.uniform(0.3, 0.6))

    except Exception as e:
        logger.debug(f"[fidelity] Human browse error (non-fatal): {e}")
        time.sleep(random.uniform(1.0, 2.0))


def _human_click(page, element) -> None:
    """Move to element with realistic offset then click."""
    from DrissionPage.common import Actions
    try:
        ox = random.randint(-3, 3)
        oy = random.randint(-2, 2)
        Actions(page).move_to(element, offset_x=ox, offset_y=oy)
        time.sleep(random.uniform(0.15, 0.38))
        element.click.left()
        time.sleep(random.uniform(0.08, 0.22))
    except Exception:
        try:
            element.click.left()
        except Exception:
            pass


# ── Auth check ────────────────────────────────────────────────────────────────

def _is_authenticated(page, settle_secs: float = 6.0) -> bool:
    """Navigate to positions page and visually confirm we are authenticated."""
    try:
        page.get("https://digital.fidelity.com/ftgw/digital/portfolio/positions")
        time.sleep(settle_secs)
        screenshot = _take_screenshot(page)
        state, detail = _identify_page_state(screenshot, page.url)
        logger.info(f"[fidelity] Auth check → {state.value}: {detail}")
        return state in {PageState.PORTFOLIO, PageState.POSITIONS_LOADING, PageState.POSITIONS_READY}
    except Exception as e:
        logger.warning(f"[fidelity] Auth check error: {e}")
        return False


# ── CSV download (hardened) ───────────────────────────────────────────────────

def _download_csv_from_positions(page) -> str:
    """
    Visually confirm the positions table is fully loaded, then click Download.
    Polls for the CSV file with an extended timeout.
    Saves a debug screenshot if download fails.
    """
    # Clear any leftover CSVs from previous runs
    for old in glob.glob(f"{_WSL_DL_DIR}/*.csv"):
        try:
            Path(old).unlink()
        except Exception:
            pass

    # Wait for positions table — not just the page shell
    state, detail, screenshot = _wait_for_stable_state(
        page,
        timeout=50.0,
        poll_interval=3.0,
        target_states={
            PageState.POSITIONS_READY,
            PageState.BOT_CHALLENGE,
            PageState.ACCESS_DENIED,
            PageState.UNKNOWN,
        },
    )

    if state != PageState.POSITIONS_READY:
        _save_debug_screenshot(screenshot, f"positions_not_ready_{state.value}")
        raise FidelityScraperError(
            f"Positions page not ready for download — state: {state.value} ({detail})\n"
            f"Debug screenshot saved to data/fidelity_debug/"
        )

    logger.info("[fidelity] Positions table loaded — locating Download button")

    # Locate the Download button via multiple fallback strategies
    download_el = None
    for locator in [
        "css:button[aria-label*='Download']",
        "css:button[aria-label*='download']",
        "css:a[href*='.csv']",
        "css:[role='menuitem']",
    ]:
        try:
            el = page.ele(locator, timeout=4)
            if el and el.states.is_displayed:
                download_el = el
                break
        except Exception:
            continue

    # Last-resort: scan all buttons for "download" text
    if not download_el:
        try:
            for btn in page.eles("css:button"):
                try:
                    if "download" in (btn.text or "").lower() and btn.states.is_displayed:
                        download_el = btn
                        break
                except Exception:
                    continue
        except Exception:
            pass

    if not download_el:
        screenshot = _take_screenshot(page)
        _save_debug_screenshot(screenshot, "download_button_not_found")
        raise FidelityScraperError(
            "Download button not found on positions page.\n"
            "Debug screenshot saved to data/fidelity_debug/"
        )

    _human_click(page, download_el)
    logger.info("[fidelity] Download button clicked — waiting for CSV file")

    # Poll for the CSV file (extended timeout vs old 30s)
    deadline = time.time() + 50
    csv_path = None
    while time.time() < deadline:
        time.sleep(1.5)
        csvs = [
            f for f in glob.glob(f"{_WSL_DL_DIR}/*.csv")
            if not f.endswith(".crdownload")
        ]
        if csvs:
            csv_path = max(csvs, key=os.path.getmtime)
            break

    if not csv_path:
        screenshot = _take_screenshot(page)
        _save_debug_screenshot(screenshot, "download_timeout")
        raise FidelityScraperError(
            f"Download timed out — no CSV appeared in {_WSL_DL_DIR} after 50s\n"
            "Debug screenshot saved to data/fidelity_debug/"
        )

    content = Path(csv_path).read_text(encoding="utf-8", errors="replace")
    logger.info(f"[fidelity] Downloaded positions CSV ({len(content):,} bytes)")
    return content


# ── Single login attempt ──────────────────────────────────────────────────────

def _single_login_attempt(creds_prefix: str) -> str | None:
    """
    One complete visual-guided login → CSV download cycle.

    Returns CSV text on success, None if Akamai blocked or flow failed.
    Raises FidelityAuthError/FidelityCredentialError for hard errors.
    Debug screenshots are saved at every failure point.
    """
    username, password, totp_secret = _get_credentials(creds_prefix)
    page = _make_driver(headless=False)

    try:
        # ── Step 1: Navigate to login ────────────────────────────────────────
        logger.info("[fidelity] Navigating to Fidelity login…")
        page.get("https://digital.fidelity.com/prgw/digital/login/full-page")

        # Long settle — Akamai's sensor_data collection scripts need time to run.
        # Touching the form before they finish is a strong bot signal.
        time.sleep(random.uniform(5.0, 8.0))

        # ── Step 2: Visually confirm login page ──────────────────────────────
        screenshot = _take_screenshot(page)
        state, detail = _identify_page_state(screenshot, page.url)
        logger.info(f"[fidelity] Initial state: {state.value} — {detail}")

        if state == PageState.BOT_CHALLENGE:
            _save_debug_screenshot(screenshot, "bot_challenge_initial")
            logger.warning("[fidelity] Akamai challenge on initial page load")
            return None

        if state == PageState.ACCESS_DENIED:
            _save_debug_screenshot(screenshot, "access_denied_initial")
            logger.warning("[fidelity] Access denied before login")
            return None

        # Already authenticated (e.g. cookies in profile)
        if state in {PageState.PORTFOLIO, PageState.POSITIONS_LOADING, PageState.POSITIONS_READY}:
            logger.info("[fidelity] Already authenticated from profile — skipping login")
        else:
            # ── Step 3: Human browsing simulation ───────────────────────────
            logger.info("[fidelity] Simulating human browsing before form interaction…")
            _human_browse(page)

            # ── Step 4: Fill username ────────────────────────────────────────
            username_el = page.ele("#dom-username-input", timeout=15)
            if not username_el:
                _save_debug_screenshot(_take_screenshot(page), "username_field_missing")
                logger.warning("[fidelity] Username field not found")
                return None
            _human_type(page, username_el, username)
            time.sleep(random.uniform(0.8, 1.6))

            # ── Step 5: Fill password ────────────────────────────────────────
            password_el = page.ele("#dom-pswd-input", timeout=8)
            if not password_el:
                _save_debug_screenshot(_take_screenshot(page), "password_field_missing")
                logger.warning("[fidelity] Password field not found")
                return None
            _human_type(page, password_el, password)
            time.sleep(random.uniform(1.0, 2.2))

            # ── Step 6: Submit ───────────────────────────────────────────────
            submit_el = page.ele("css:button[type='submit']", timeout=5)
            if submit_el:
                _human_click(page, submit_el)
            else:
                logger.warning("[fidelity] Submit button not found — trying Enter key")
                from DrissionPage.common import Actions, Keys
                Actions(page).key_down(Keys.ENTER)
            logger.info("[fidelity] Login form submitted — awaiting evaluation…")

        # ── Step 7: Wait for stable post-submit state ────────────────────────
        state, detail, screenshot = _wait_for_stable_state(
            page,
            timeout=22.0,
            poll_interval=2.5,
            target_states={
                PageState.MFA_TOTP, PageState.MFA_OTHER,
                PageState.PORTFOLIO, PageState.POSITIONS_LOADING,
                PageState.POSITIONS_READY, PageState.BOT_CHALLENGE,
                PageState.ACCESS_DENIED, PageState.LOGIN,
            },
        )
        logger.info(f"[fidelity] Post-submit state: {state.value}")

        # ── Step 8: Handle each possible outcome ─────────────────────────────
        if state in {PageState.BOT_CHALLENGE, PageState.ACCESS_DENIED}:
            _save_debug_screenshot(screenshot, f"{state.value}_post_submit")
            logger.warning(f"[fidelity] Akamai blocked at login: {state.value} — {detail}")
            return None

        if state == PageState.LOGIN:
            _save_debug_screenshot(screenshot, "still_on_login_post_submit")
            logger.warning("[fidelity] Still on login page — bad credentials or silent block")
            return None

        if state == PageState.MFA_TOTP:
            # ── Step 9: Handle TOTP ──────────────────────────────────────────
            if not totp_secret:
                raise FidelityAuthError(
                    "Fidelity is requesting TOTP but no 2FA secret is configured.\n"
                    f"Add FIDELITY_{(creds_prefix.upper() + '_') if creds_prefix else ''}2FA_SECRET to .env\n"
                    "(Base32 key from Fidelity Security Center → Security → 2FA setup)"
                )

            code = _totp_code(totp_secret)
            expires_in = int(30 - (time.time() % 30))
            logger.info(f"[fidelity] Entering TOTP code (valid for ~{expires_in}s)…")

            totp_el = None
            for sel in [
                "#dom-totp-security-code-input",
                "css:input[name*='totp']",
                "css:input[name*='code']",
                "css:input[placeholder*='code']",
                "css:input[type='tel']",
            ]:
                totp_el = page.ele(sel, timeout=5)
                if totp_el:
                    break

            if totp_el:
                _human_type(page, totp_el, code)
                time.sleep(random.uniform(0.5, 1.0))
            else:
                _save_debug_screenshot(_take_screenshot(page), "totp_field_missing")
                logger.warning("[fidelity] TOTP input field not found — continuing anyway")

            # Trust this device
            trust_el = page.ele("#dom-trust-device-checkbox", timeout=3)
            if trust_el and trust_el.states.is_displayed:
                try:
                    _human_click(page, trust_el)
                    time.sleep(random.uniform(0.3, 0.6))
                except Exception:
                    pass

            totp_submit = page.ele("css:button[type='submit']", timeout=5)
            if totp_submit:
                _human_click(page, totp_submit)

            # Wait for post-MFA stable state
            state, detail, screenshot = _wait_for_stable_state(
                page,
                timeout=18.0,
                poll_interval=2.0,
                target_states={
                    PageState.PORTFOLIO, PageState.POSITIONS_LOADING,
                    PageState.POSITIONS_READY, PageState.BOT_CHALLENGE,
                    PageState.ACCESS_DENIED, PageState.UNKNOWN,
                },
            )
            logger.info(f"[fidelity] Post-TOTP state: {state.value}")

        if state == PageState.MFA_OTHER:
            _save_debug_screenshot(screenshot, "mfa_other_cannot_automate")
            raise FidelityAuthError(
                "Fidelity is requesting a non-TOTP MFA method (SMS/push/security question).\n"
                "Run `financial-bytes fidelity-setup` to log in manually and save session cookies."
            )

        if state in {PageState.BOT_CHALLENGE, PageState.ACCESS_DENIED}:
            _save_debug_screenshot(screenshot, f"{state.value}_post_mfa")
            logger.warning(f"[fidelity] Akamai block post-MFA: {state.value}")
            return None

        # ── Step 10: Navigate to positions page ──────────────────────────────
        if state not in {PageState.POSITIONS_LOADING, PageState.POSITIONS_READY}:
            logger.info("[fidelity] Navigating to positions page…")
            time.sleep(random.uniform(1.5, 2.5))  # brief post-auth browse pause
            page.get("https://digital.fidelity.com/ftgw/digital/portfolio/positions")
            time.sleep(random.uniform(3.5, 5.5))

            state, detail, screenshot = _wait_for_stable_state(
                page,
                timeout=35.0,
                poll_interval=3.0,
                target_states={
                    PageState.POSITIONS_READY, PageState.POSITIONS_LOADING,
                    PageState.BOT_CHALLENGE, PageState.ACCESS_DENIED, PageState.UNKNOWN,
                },
            )
            logger.info(f"[fidelity] Positions nav state: {state.value}")

        if state in {PageState.BOT_CHALLENGE, PageState.ACCESS_DENIED}:
            _save_debug_screenshot(screenshot, f"{state.value}_on_positions_nav")
            logger.warning(f"[fidelity] Akamai block on positions navigation: {state.value}")
            return None

        # ── Step 11: Save cookies + download ─────────────────────────────────
        logger.info("[fidelity] Authenticated — saving cookies and downloading CSV")
        _save_cookies(page, creds_prefix)
        csv_text = _download_csv_from_positions(page)
        return csv_text

    except (FidelityScraperError, FidelityAuthError):
        raise
    except Exception as e:
        logger.warning(f"[fidelity] Login attempt error: {type(e).__name__}: {e}")
        try:
            _save_debug_screenshot(_take_screenshot(page), f"exception_{type(e).__name__}")
        except Exception:
            pass
        return None
    finally:
        _quit_driver(page)


# ── Core orchestrator ─────────────────────────────────────────────────────────

def _download_positions_csv(
    creds_prefix: str = "",
    max_attempts: int = _MAX_LOGIN_ATTEMPTS,
) -> str:
    """
    Return Fidelity positions CSV text.

    Flow:
      1. Try saved cookies (headless DrissionPage).
      2. On expiry/miss: automated visual-guided login (up to max_attempts).
      3. All attempts fail → raise FidelityAuthError → manual fidelity-setup.
    """
    # ── 1. Try saved session cookies ──────────────────────────────────────────
    page = None
    try:
        page = _make_driver(headless=True)
        had_cookies = _load_cookies(page, creds_prefix)
        if had_cookies:
            logger.info("[fidelity] Testing saved session cookies…")
            if _is_authenticated(page):
                logger.info("[fidelity] Session valid — downloading CSV")
                csv_text = _download_csv_from_positions(page)
                _save_cookies(page, creds_prefix)
                return csv_text
            logger.warning("[fidelity] Saved cookies expired — clearing, will re-login")
            _cookie_path(creds_prefix).unlink(missing_ok=True)
    except FidelityScraperError:
        raise
    except Exception as e:
        logger.warning(f"[fidelity] Cookie check error: {e}")
    finally:
        if page:
            _quit_driver(page)

    # ── 2. Automated login ────────────────────────────────────────────────────
    for attempt in range(1, max_attempts + 1):
        logger.info(f"[fidelity] Automated login attempt {attempt}/{max_attempts}…")
        csv_text = _single_login_attempt(creds_prefix)
        if csv_text is not None:
            return csv_text

        if attempt < max_attempts:
            delay = _BACKOFF_BASE_SECS * (2 ** (attempt - 1))
            delay *= random.uniform(0.75, 1.25)
            logger.warning(f"[fidelity] Attempt {attempt} failed — retrying in {delay:.0f}s")
            time.sleep(delay)

    # ── 3. All automated attempts failed ─────────────────────────────────────
    portfolio = "lilich" if creds_prefix else "nbossn_fidelity"
    raise FidelityAuthError(
        f"All {max_attempts} automated login attempts failed.\n\n"
        "Establish a manual session:\n"
        f"  financial-bytes fidelity-setup --portfolio {portfolio}\n\n"
        "A Chrome window will open — log in manually. "
        "Cookies are saved automatically and reused for ~7 days.\n"
        "Debug screenshots saved in: data/fidelity_debug/"
    )


# ── Manual session setup ──────────────────────────────────────────────────────

def setup_fidelity_cookies(creds_prefix: str = "", timeout_secs: int = 300) -> None:
    """
    Open Windows Chrome and wait for manual login. Saves cookies on success.
    Uses visual state detection to confirm portfolio page is reached.
    """
    print("\n" + "=" * 62)
    print("  FIDELITY MANUAL SESSION SETUP")
    print("=" * 62)
    print("  A Chrome window is opening. Log in to Fidelity normally.")
    print("  The script detects when you reach the portfolio page")
    print("  and automatically saves your session cookies.")
    print(f"  Timeout: {timeout_secs // 60} minutes.")
    print("=" * 62 + "\n")

    page = _make_driver(headless=False)
    try:
        page.get("https://digital.fidelity.com/prgw/digital/login/full-page")

        deadline = time.time() + timeout_secs
        while time.time() < deadline:
            time.sleep(3)
            screenshot = _take_screenshot(page)
            state, _ = _identify_page_state(screenshot, page.url)
            if state in {PageState.PORTFOLIO, PageState.POSITIONS_LOADING, PageState.POSITIONS_READY}:
                break
        else:
            raise FidelityAuthError(
                f"Timed out waiting for manual login ({timeout_secs}s). "
                "Please run fidelity-setup again."
            )

        if "portfolio/positions" not in page.url.lower():
            page.get("https://digital.fidelity.com/ftgw/digital/portfolio/positions")
            time.sleep(5)

        _save_cookies(page, creds_prefix)
        print(f"\n✅ Session cookies saved → {_cookie_path(creds_prefix)}")
        print("   fidelity-sync will reuse these automatically for ~7 days.")

    finally:
        _quit_driver(page)


# ── Public API ────────────────────────────────────────────────────────────────

def sync_fidelity_raw(
    portfolio_name: str,
    creds_prefix: str = "",
    account_filter: str | None = None,
    headless: bool = True,  # kept for API compat; login is always non-headless
) -> list[dict]:
    """Download Fidelity positions and return as list of raw CSV row dicts."""
    username, _, _ = _get_credentials(creds_prefix)
    logger.info(f"[fidelity] Syncing {portfolio_name} (user: {username[:3]}***)")
    csv_text = _download_positions_csv(creds_prefix=creds_prefix)
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
    headless: bool = True,  # kept for API compat
) -> list:
    """Download live Fidelity positions and return as list[Holding]."""
    from src.portfolio.fidelity_reader import read_fidelity_positions

    username, _, _ = _get_credentials(creds_prefix)
    logger.info(f"[fidelity] Live sync for {portfolio_name} (user: {username[:3]}***)")
    csv_text = _download_positions_csv(creds_prefix=creds_prefix)

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".csv", delete=False, encoding="utf-8"
    ) as tmp:
        tmp.write(csv_text)
        tmp_path = tmp.name

    try:
        holdings = read_fidelity_positions(tmp_path, account_filter=account_filter)
    finally:
        Path(tmp_path).unlink(missing_ok=True)

    logger.info(f"[fidelity] Parsed {len(holdings)} Holding objects for {portfolio_name}")
    return holdings
