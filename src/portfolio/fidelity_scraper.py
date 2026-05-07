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
_WIN_DL_DIR = rf"C:\Users\{_WIN_USERNAME}\Downloads"
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
    """Kill all Chrome processes and clear stale profile lock files.

    We kill ALL Chrome (not just debug-port holders) because a Chrome window
    from a previous run that exited without cleanup leaves a lockfile in the
    profile directory. Chrome exits immediately with code 21 if the lockfile
    is present, even if no process currently holds it.
    """
    # 1. Kill ONLY the Chrome process holding our specific debug port.
    #    We do NOT kill all chrome.exe — that would close the user's personal
    #    Chrome tabs.  Only the automation Chrome (launched with
    #    --remote-debugging-port=_DEBUG_PORT) needs to be stopped.
    try:
        subprocess.run(
            ["powershell.exe", "-NoProfile", "-Command",
             f"Get-WmiObject Win32_Process | "
             f"Where-Object {{ $_.Name -eq 'chrome.exe' -and "
             f"$_.CommandLine -like '*remote-debugging-port={_DEBUG_PORT}*' }} | "
             f"ForEach-Object {{ Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }}"],
            capture_output=True, timeout=10,
        )
    except Exception:
        pass

    # 2. Belt-and-suspenders: kill any Linux-visible process holding the debug port
    try:
        subprocess.run(
            ["fuser", "-k", f"{_DEBUG_PORT}/tcp"],
            capture_output=True, timeout=5,
        )
    except Exception:
        pass

    time.sleep(1.5)

    # 3. Remove stale profile lockfile (Chrome won't start if this exists)
    lockfile = Path(_WSL_DL_DIR.replace("Temp\\fidelity_dl", "")) / \
        "Local" / "fidelity_automation" / "chrome_profile" / "lockfile"
    # Build directly from known Windows user path
    wsl_profile = Path(f"/mnt/c/Users/{_WIN_USERNAME}/AppData/Local/fidelity_automation/chrome_profile")
    for lf in [wsl_profile / "lockfile", wsl_profile / "Default" / "lockfile"]:
        try:
            lf.unlink(missing_ok=True)
        except Exception:
            pass


def _patch_chrome_download_prefs() -> None:
    """
    Patch Chrome's profile Preferences JSON to auto-download to _WIN_DL_DIR.

    Why: Chrome 120+ ignores --download-default-directory if the profile already
    has a saved download path.  Patching the Preferences file before Chrome
    starts is the only reliable fix — it sets BOTH the directory AND disables
    the "Ask where to save each file" dialog, so downloads land silently in our
    configured folder.

    Must be called AFTER _kill_existing_chrome_debug() (Chrome must not be
    running when we write the Preferences file — concurrent writes corrupt it).
    """
    prefs_path = Path(
        f"/mnt/c/Users/{_WIN_USERNAME}/AppData/Local"
        f"/fidelity_automation/chrome_profile/Default/Preferences"
    )
    if not prefs_path.exists():
        logger.debug("[fidelity] Chrome Preferences not found — will be created on first launch")
        return
    try:
        prefs = json.loads(prefs_path.read_text(encoding="utf-8"))
        dl = prefs.setdefault("download", {})
        dl["default_directory"]   = _WIN_DL_DIR   # Windows path — Chrome is a Windows process
        dl["prompt_for_download"]  = False          # never ask where to save
        dl["directory_upgrade"]    = True           # allow changing dir without asking
        prefs_path.write_text(json.dumps(prefs), encoding="utf-8")
        logger.info(f"[fidelity] Chrome prefs patched — auto-download → {_WIN_DL_DIR}")
    except Exception as e:
        logger.warning(f"[fidelity] Could not patch Chrome prefs ({e}) — save dialog may appear")


def _make_driver(headless: bool = False):
    """
    Launch Windows Chrome from WSL2, then connect DrissionPage via 127.0.0.1.

    Networking note:
      Chrome 120+ ignores --remote-debugging-address=0.0.0.0 and always binds
      the debug port to 127.0.0.1 only. With WSL2 mirrored networking
      (networkingMode=mirrored in ~/.wslconfig) the Linux and Windows network
      stacks share a single loopback, so 127.0.0.1 in WSL reaches Chrome
      directly — no gateway IP or port-proxy required.

      Prerequisites: C:\\Users\\<user>\\.wslconfig with [wsl2] networkingMode=mirrored,
      then `wsl --shutdown` once to apply.
    """
    global _chrome_proc
    from DrissionPage import ChromiumPage, ChromiumOptions

    # Ensure download and debug dirs exist
    Path(_WSL_DL_DIR).mkdir(parents=True, exist_ok=True)
    _DEBUG_DIR.mkdir(parents=True, exist_ok=True)

    # Kill any stale Chrome on our debug port
    _kill_existing_chrome_debug()

    # Patch Chrome profile preferences BEFORE launch.
    # Chrome ignores --download-default-directory when the profile already has
    # a saved download directory.  Writing to the Preferences JSON before Chrome
    # starts is the only reliable way to set both the directory AND suppress the
    # "Ask where to save" dialog.
    _patch_chrome_download_prefs()

    # Chrome launch args (Windows process — uses Windows path format)
    chrome_args = [
        _WSL_CHROME_BIN,
        f"--remote-debugging-port={_DEBUG_PORT}",
        # NOTE: --remote-debugging-address=0.0.0.0 is silently ignored by Chrome
        # 120+. With mirrored WSL2 networking we don't need it — Chrome binds to
        # 127.0.0.1 and WSL reaches it directly at 127.0.0.1.
        f"--user-data-dir={_WIN_PROFILE_DIR}",        # Windows path
        "--profile-directory=Default",                # skip profile picker on first run
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
        _wait_for_port("127.0.0.1", _DEBUG_PORT, timeout=30)
    except FidelityScraperError:
        _proc.terminate()
        raise

    _chrome_proc = _proc

    # Connect DrissionPage (existing_only — don't re-launch Chrome)
    opts = ChromiumOptions()
    opts.existing_only()
    opts.set_address(f"127.0.0.1:{_DEBUG_PORT}")

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


def _dom_inspect_state(page) -> PageState | None:
    """
    Inspect DOM element presence to identify page state without Haiku.

    More reliable than URL pattern matching because element IDs are stable
    Fidelity-specific identifiers, whereas URLs vary with redirects.

    Returns None if DOM inspection can't determine the state conclusively.
    """
    # Short timeouts — we want to check presence, not wait for appearance.
    # Page is already loaded when this is called; 0.5s is enough for DOM readiness.
    _T = 0.5
    try:
        def _visible(el) -> bool:
            """Return True only if the element exists and is displayed."""
            if not el:
                return False
            try:
                return bool(el.states.is_displayed)
            except Exception:
                return True  # DrissionPage versions without .states — assume visible

        # Bot/error interstitials — check these first so they aren't mistaken for login
        # Akamai "can't complete this action" and similar blocks have a "Go back to login"
        # link on an otherwise blank page — the login form itself is absent/hidden.
        body_text = ""
        try:
            body = page.ele("tag:body", timeout=_T)
            if body:
                body_text = (body.text or "").lower()
        except Exception:
            pass
        if "can't complete this action" in body_text or "go back to login" in body_text:
            return PageState.ACCESS_DENIED

        # Login page: username input must be visible (not just present in DOM)
        el = page.ele("#dom-username-input", timeout=_T)
        if _visible(el):
            return PageState.LOGIN

        # TOTP page: 6-digit authenticator code input (must be visible)
        for sel in [
            "#dom-totp-security-code-input",
            "css:input[name*='totp']",
            "css:input[autocomplete='one-time-code']",
        ]:
            el = page.ele(sel, timeout=_T)
            if _visible(el):
                return PageState.MFA_TOTP

        # Positions page: download/export button appears only when table is loaded
        for sel in [
            "css:button[aria-label*='Download']",
            "css:a[aria-label*='Download']",
            "css:[data-testid*='export']",
        ]:
            el = page.ele(sel, timeout=_T)
            if _visible(el):
                return PageState.POSITIONS_READY

        # Portfolio summary (positions sub-nav absent)
        for sel in ["css:.portfolio-summary", "css:.acct-selector"]:
            el = page.ele(sel, timeout=_T)
            if _visible(el):
                return PageState.PORTFOLIO

    except Exception:
        pass
    return None


_CLAUDE_CREDS_PATH = Path.home() / ".claude" / ".credentials.json"


def _get_anthropic_client():
    """
    Return an authenticated Anthropic client.

    Priority:
      1. ANTHROPIC_API_KEY env var (explicit API key)
      2. Claude Code OAuth token from ~/.claude/.credentials.json
         (uses the active Claude.ai subscription — no separate API key needed)

    Returns None if no authentication is available.
    """
    from anthropic import Anthropic

    # 1. Explicit API key — must look like a real key (sk-ant-api...), not a placeholder
    api_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    if api_key and api_key.startswith("sk-ant-api"):
        return Anthropic(api_key=api_key)

    # 2. Claude Code OAuth token
    try:
        creds = json.loads(_CLAUDE_CREDS_PATH.read_text())
        oauth = creds.get("claudeAiOauth", {})
        access_token = oauth.get("accessToken", "").strip()
        expires_at_ms = oauth.get("expiresAt", 0)

        if not access_token:
            return None

        if expires_at_ms and expires_at_ms < time.time() * 1000:
            logger.warning(
                "[fidelity] Claude Code OAuth token is expired — "
                "reopen Claude Code to refresh it"
            )
            return None

        expires_in_h = (expires_at_ms / 1000 - time.time()) / 3600
        logger.info(
            f"[fidelity] Using Claude Code OAuth token "
            f"(valid for {expires_in_h:.1f}h)"
        )
        # Temporarily clear ANTHROPIC_API_KEY from the environment so the SDK
        # doesn't send it alongside the Bearer token — the placeholder value
        # "your-anthropic-api-key" (from .env) causes a 401 if it leaks through.
        _old_key = os.environ.pop("ANTHROPIC_API_KEY", None)
        try:
            client = Anthropic(auth_token=access_token)
        finally:
            if _old_key is not None:
                os.environ["ANTHROPIC_API_KEY"] = _old_key
        return client

    except Exception as e:
        logger.debug(f"[fidelity] Could not load Claude Code OAuth token: {e}")
        return None


def _identify_page_state(
    png_bytes: bytes,
    url: str,
    page=None,
) -> tuple[PageState, str]:
    """
    Use Claude Haiku vision to classify the current browser page state.

    Auth priority (automatic — no config needed):
      1. ANTHROPIC_API_KEY env var
      2. Claude Code OAuth token (~/.claude/.credentials.json)

    Fallback order when Haiku is unavailable or fails:
      1. DOM inspection via page object (most reliable — element IDs are stable)
      2. URL-based heuristics (least reliable — URLs vary with redirects)
    """
    client = _get_anthropic_client()

    if client is None or not png_bytes:
        if page is not None:
            dom_state = _dom_inspect_state(page)
            if dom_state is not None:
                return dom_state, "dom-inspect (no auth)"
        state = _url_fallback_state(url)
        return state, "url-heuristic (no auth)"

    try:

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

        raw_text = response.content[0].text.strip()
        # Strip markdown code fences if Haiku wraps the JSON (```json ... ```)
        if raw_text.startswith("```"):
            raw_text = raw_text.split("```", 2)[1]
            if raw_text.startswith("json"):
                raw_text = raw_text[4:]
            raw_text = raw_text.strip()
        result = json.loads(raw_text)
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
        logger.warning(f"[fidelity] Visual classification error ({e}) — DOM/URL fallback")
        if page is not None:
            dom_state = _dom_inspect_state(page)
            if dom_state is not None:
                logger.info(f"[fidelity] DOM inspection resolved state: {dom_state.value}")
                return dom_state, f"dom-inspect fallback: {e}"
        return _url_fallback_state(url), f"url fallback: {e}"


def _url_fallback_state(url: str) -> PageState:
    """Best-effort page state from URL when Haiku and DOM inspection are unavailable.

    Key fix: Fidelity's login page redirects to a URL containing 'signin', so
    'signin' must be treated as LOGIN — not MFA_TOTP. Only explicit /mfa or /2fa
    paths are classified as TOTP.
    """
    u = url.lower()
    # TOTP/MFA paths are explicit sub-paths — check these first (more specific)
    if "/mfa" in u or "/2fa" in u or "totp" in u or "authenticator" in u:
        return PageState.MFA_TOTP
    # Login / sign-in pages: prgw/digital/login OR any signin redirect URL
    if "prgw/digital/login" in u or "signin" in u or "/login" in u:
        return PageState.LOGIN
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
        state, detail = _identify_page_state(screenshot, page.url, page)
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


def _clear_field(page, element) -> None:
    """
    Robustly clear a form field before typing.

    Chrome autofill (and browser-saved passwords) pre-populate fields on page
    load.  element.clear() / element.input('', clear=True) do NOT touch these
    values reliably — the autofill value is held separately from the DOM input
    value and only syncs on focus.  Three strategies in sequence:

      1. JS reset via native setter — triggers React/Angular synthetic events
         so framework-bound state tracks the clear.
      2. Ctrl+A → Delete — visual selection delete; catches any characters that
         survived the JS clear (e.g. password managers that re-inject on blur).
      3. Verification pass — read back the field value; if still non-empty,
         do a final triple-click → Backspace sweep.

    This function leaves the field focused and empty, ready for _human_type.
    """
    from DrissionPage.common import Actions

    _human_click(page, element)
    time.sleep(random.uniform(0.25, 0.45))

    # ── Strategy 1: native-setter JS clear (React-safe) ──────────────────────
    try:
        element.run_js(
            "var setter = Object.getOwnPropertyDescriptor("
            "    window.HTMLInputElement.prototype, 'value').set;"
            "setter.call(this, '');"
            "this.dispatchEvent(new Event('input',  {bubbles: true}));"
            "this.dispatchEvent(new Event('change', {bubbles: true}));"
        )
    except Exception:
        pass
    time.sleep(0.08)

    # ── Strategy 2: Ctrl+A → Delete (visual sweep) ───────────────────────────
    try:
        actions = Actions(page)
        actions.key_down("ctrl")
        actions.type("a")
        actions.key_up("ctrl")
        time.sleep(0.06)
        actions.key_down("delete")
        actions.key_up("delete")
    except Exception:
        pass
    time.sleep(random.uniform(0.12, 0.22))

    # ── Strategy 3: verify + fallback triple-click sweep ─────────────────────
    try:
        remaining = element.run_js("return this.value;") or ""
        if remaining:
            logger.debug(
                f"[fidelity] Field still has {len(remaining)} chars after clear — triple-click sweep"
            )
            try:
                element.click.triple()          # select all via triple-click
                time.sleep(0.08)
                actions = Actions(page)
                actions.key_down("backspace")
                actions.key_up("backspace")
            except Exception:
                pass
    except Exception:
        pass

    time.sleep(random.uniform(0.1, 0.2))


# ── Auth check ────────────────────────────────────────────────────────────────

def _is_authenticated(page, settle_secs: float = 6.0) -> bool:
    """Navigate to positions page and visually confirm we are authenticated."""
    try:
        page.get("https://digital.fidelity.com/ftgw/digital/portfolio/positions")
        time.sleep(settle_secs)
        screenshot = _take_screenshot(page)
        state, detail = _identify_page_state(screenshot, page.url, page)
        logger.info(f"[fidelity] Auth check → {state.value}: {detail}")
        return state in {PageState.PORTFOLIO, PageState.POSITIONS_LOADING, PageState.POSITIONS_READY}
    except Exception as e:
        logger.warning(f"[fidelity] Auth check error: {e}")
        return False


# ── CSV download (hardened) ───────────────────────────────────────────────────

# _WSL_DL_DIR is ~/Downloads — the standard user download location.
# Chrome's profile Preferences are patched to point here before each launch.
_WSL_DOWNLOADS_DIR = _WSL_DL_DIR  # alias kept for _poll_for_csv compatibility


def _cdp_set_download_path(page) -> None:
    """
    Override Chrome's saved download directory via CDP.

    Chrome 120+ silently ignores --download-default-directory when the profile
    already has a saved download path.  Browser.setDownloadBehavior is the only
    reliable way to redirect downloads at runtime without touching the profile.
    """
    try:
        page.run_cdp(
            "Browser.setDownloadBehavior",
            behavior="allow",
            downloadPath=_WIN_DL_DIR,
            eventsEnabled=True,
        )
        logger.info(f"[fidelity] CDP download path → {_WIN_DL_DIR}")
    except Exception as e:
        logger.warning(
            f"[fidelity] CDP setDownloadBehavior failed ({e}) — "
            "will also poll ~/Downloads as fallback"
        )


def _click_download_trigger(page) -> bool:
    """
    Find and click the CSV download trigger on Fidelity's positions page.

    Returns True if a click was dispatched, False if no trigger found.

    Key design constraint: the ⋮ more-options menu auto-closes on any mouse
    movement or blur event.  The old approach returned the element to the caller
    (which then called _human_click with mouse movement) — this always closed the
    menu before the click could land.

    This function handles the entire sequence atomically:
      1. Find and _human_click the ⋮ button (opens menu)
      2. Immediately (no mouse movement, no multi-second selector loops) find
         the Download item using DOM text scan with short per-attempt timeouts
      3. Click the item directly (element.click.left()) — no mouse movement that
         would close the menu

    All "more button" searches use short (0.5s) timeouts and are done BEFORE
    opening the menu, so we don't burn time post-open.
    """
    def _visible(el) -> bool:
        if not el:
            return False
        try:
            return bool(el.states.is_displayed)
        except Exception:
            return True

    def _direct_click(el) -> bool:
        """Click an element without mouse movement (preserves menu open state)."""
        try:
            el.click.left()
            return True
        except Exception:
            try:
                el.run_js("this.click();")
                return True
            except Exception:
                return False

    # ── A. Direct download button (no menu needed) ────────────────────────────
    for sel in [
        "css:button[aria-label*='Download']",
        "css:button[aria-label*='download']",
        "css:a[aria-label*='Download']",
        "css:a[href*='.csv']",
    ]:
        try:
            el = page.ele(sel, timeout=0.5)
            if _visible(el):
                logger.info(f"[fidelity] Direct download button: {sel}")
                _human_click(page, el)
                return True
        except Exception:
            continue

    # ── B. ⋮ more-options menu — find button BEFORE opening ───────────────────
    # Locate the more-button first (all lookups while menu is CLOSED).
    more_btn = None

    # Targeted selector search (fast — short timeouts)
    for sel in [
        "css:button[aria-label*='More']",
        "css:button[aria-label*='more']",
        "css:button[aria-label*='Options']",
        "css:button[aria-label*='Menu']",
        "css:button[data-testid*='more']",
        "css:button[data-testid*='menu']",
        "css:button[data-testid*='overflow']",
    ]:
        try:
            el = page.ele(sel, timeout=0.5)
            if _visible(el):
                more_btn = el
                break
        except Exception:
            continue

    # Fallback: button text/title/aria-label heuristic
    if not more_btn:
        try:
            for btn in page.eles("css:button"):
                try:
                    label = (
                        (btn.attr("aria-label") or "") +
                        (btn.attr("title") or "") +
                        (btn.text or "")
                    ).lower()
                    if (
                        any(k in label for k in ("more", "option", "menu", "overflow"))
                        and _visible(btn)
                    ):
                        more_btn = btn
                        break
                except Exception:
                    continue
        except Exception:
            pass

    if more_btn:
        logger.info("[fidelity] Opening ⋮ more-options menu for Download…")
        _human_click(page, more_btn)
        time.sleep(0.5)  # brief render wait — keep short so menu stays open

        # ── Immediately find and click the Download item ──────────────────────
        # Use ONE fast DOM scan — no multi-selector loops with per-selector delays.
        # Menu closes on mouse movement, so we use direct element.click.left()
        # (not _human_click) once the item is found.
        all_candidates: list = []
        for sel in [
            "css:[role='menuitem']",
            "css:[role='option']",
            "css:li",
        ]:
            try:
                all_candidates.extend(page.eles(sel, timeout=0.3))
            except Exception:
                continue

        for item in all_candidates:
            try:
                text = (item.text or item.attr("aria-label") or "").strip()
                if text.lower() == "download" and _visible(item):
                    logger.info(f"[fidelity] Download menu item found: '{text}' — clicking")
                    if _direct_click(item):
                        return True
            except Exception:
                continue

        # Looser match (text contains "download") if exact match failed
        for item in all_candidates:
            try:
                text = (item.text or item.attr("aria-label") or "").lower()
                if "download" in text and _visible(item):
                    logger.info(f"[fidelity] Download menu item (loose): '{text}' — clicking")
                    if _direct_click(item):
                        return True
            except Exception:
                continue

        logger.warning("[fidelity] ⋮ menu opened but no Download item found inside")

    # ── C. Last-resort: any visible button with "download" text ───────────────
    try:
        for btn in page.eles("css:button"):
            try:
                if "download" in (btn.text or "").lower() and _visible(btn):
                    logger.info(f"[fidelity] Download button (text scan): '{btn.text}'")
                    _human_click(page, btn)
                    return True
            except Exception:
                continue
    except Exception:
        pass

    return False


def _poll_for_csv(download_start_ts: float, timeout: float = 55.0) -> str | None:
    """
    Poll for a newly created CSV file.

    Checks two locations (belt-and-suspenders for Chrome's download-directory
    quirk):
      1. _WSL_DL_DIR  — the CDP-overridden path (preferred)
      2. _WSL_DOWNLOADS_DIR — Windows ~/Downloads (fallback when CDP is ignored)

    Only accepts files created AFTER download_start_ts to avoid stale files.
    Returns the WSL path of the CSV, or None on timeout.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(1.5)
        for search_dir in [_WSL_DL_DIR, _WSL_DOWNLOADS_DIR]:
            candidates = [
                f for f in glob.glob(f"{search_dir}/*.csv")
                if not f.endswith(".crdownload")
                and os.path.getmtime(f) >= download_start_ts
            ]
            if candidates:
                path = max(candidates, key=os.path.getmtime)
                logger.info(f"[fidelity] CSV found → {path}")
                return path
    return None


def _download_csv_from_positions(page) -> str:
    """
    Confirm positions table is fully loaded, click Download, return CSV text.

    Key improvements over v1:
      • CDP download-path override (Chrome ignores --download-default-directory
        when profile has a saved setting; CDP fixes this at runtime).
      • Two-step download flow: direct button OR ⋮ menu → Download item.
      • Dual-directory poll: configured dir + ~/Downloads fallback.
      • Timestamp filter on poll so stale CSVs never match.
    """
    # ── 0. Ensure download dir exists ────────────────────────────────────────
    # Download path is set via Chrome profile prefs in _patch_chrome_download_prefs()
    # (called inside _make_driver before Chrome launches).  CDP setDownloadBehavior
    # is a browser-level command and doesn't work reliably from a page-level CDP
    # target — profile prefs are the correct fix.
    Path(_WSL_DL_DIR).mkdir(parents=True, exist_ok=True)

    # ── 1. Wait for positions table (with portfolio→positions auto-nav) ─────
    # Include PORTFOLIO so we detect it fast instead of spinning until timeout.
    # When detected, navigate to the positions URL and wait again.
    state, detail, screenshot = _wait_for_stable_state(
        page,
        timeout=30.0,
        poll_interval=2.5,
        target_states={
            PageState.POSITIONS_READY,
            PageState.POSITIONS_LOADING,
            PageState.PORTFOLIO,
            PageState.BOT_CHALLENGE,
            PageState.ACCESS_DENIED,
            PageState.UNKNOWN,
        },
    )

    if state in {PageState.PORTFOLIO, PageState.POSITIONS_LOADING}:
        logger.info(
            f"[fidelity] On {state.value} — navigating directly to positions page…"
        )
        page.get("https://digital.fidelity.com/ftgw/digital/portfolio/positions")
        time.sleep(random.uniform(3.0, 5.0))
        state, detail, screenshot = _wait_for_stable_state(
            page,
            timeout=40.0,
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

    # ── 2. Find and click the download trigger atomically ────────────────────
    # _click_download_trigger handles the ⋮ menu open+click in one shot so the
    # menu never auto-closes between finding and clicking the Download item.
    download_start_ts = time.time()
    clicked = _click_download_trigger(page)

    if not clicked:
        screenshot = _take_screenshot(page)
        _save_debug_screenshot(screenshot, "download_button_not_found")
        raise FidelityScraperError(
            "Download button not found on positions page.\n"
            "Debug screenshot saved to data/fidelity_debug/"
        )

    # ── 3. Wait for file ─────────────────────────────────────────────────────
    logger.info("[fidelity] Download triggered — polling for CSV file…")

    csv_path = _poll_for_csv(download_start_ts, timeout=55.0)

    if not csv_path:
        screenshot = _take_screenshot(page)
        _save_debug_screenshot(screenshot, "download_timeout")
        raise FidelityScraperError(
            f"Download timed out — no CSV appeared in {_WSL_DL_DIR} or "
            f"{_WSL_DOWNLOADS_DIR} after 55s\n"
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
        state, detail = _identify_page_state(screenshot, page.url, page)
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
            # Clear before typing — Chrome autofill / saved passwords may have
            # pre-filled both fields.  _clear_field uses three strategies to
            # guarantee the field is empty before _human_type runs.
            _clear_field(page, username_el)
            _human_type(page, username_el, username)
            time.sleep(random.uniform(0.8, 1.6))

            # ── Step 5: Fill password ────────────────────────────────────────
            password_el = page.ele("#dom-pswd-input", timeout=8)
            if not password_el:
                _save_debug_screenshot(_take_screenshot(page), "password_field_missing")
                logger.warning("[fidelity] Password field not found")
                return None
            _clear_field(page, password_el)
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
            state, _ = _identify_page_state(screenshot, page.url, page)
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
