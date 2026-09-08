"""
finviz_driver.py — shared Cloudflare-clearing browser helpers for Finviz.

Extracted 2026-09-05 out of `finviz_screener.py` (where this was first built
and proven against the bulk screener) so every browser-based Finviz module
(`finviz_screener.py`, `finviz_insider.py`, and any future one) reuses one
implementation instead of copy-pasting the stealth-Chrome recipe. Behavior is
unchanged from the pre-extraction `finviz_screener.py` — same profile name
(`stockpicker_automation`), same debug port (9223, never Fidelity's 9222),
same stealth JS patch set, same Cloudflare-clear polling logic.

Recipe origin: `src/portfolio/fidelity_scraper.py::_make_driver`, adapted for
a harder-to-detect but lower-stakes target (Finviz's Cloudflare
non-interactive challenge vs. Fidelity's Akamai bot detection gating a live
brokerage login).

## Usage

    from src.stockpicker.finviz_driver import _make_driver, _quit_driver, _wait_past_cloudflare

    page = _make_driver(headless=False)
    try:
        page.get("https://finviz.com/some-page")
        _wait_past_cloudflare(page)
        html = page.html
    finally:
        _quit_driver(page)
"""
from __future__ import annotations

import atexit
import os
import subprocess
import time
from pathlib import Path

from loguru import logger

# ── Config — deliberately separate from fidelity_scraper.py's constants ─────
# Different profile dir AND different debug port: this must be able to run
# concurrently with (or independently of) the Fidelity automation without
# either one killing or confusing the other's Chrome process.
_WIN_CHROME_BIN = os.getenv(
    "FIDELITY_CHROME_BIN",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
)
_WSL_CHROME_BIN = _WIN_CHROME_BIN.replace("C:\\", "/mnt/c/").replace("\\", "/")

try:
    _result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-Command", "$env:USERNAME"],
        capture_output=True, text=True, timeout=5,
    )
    _WIN_USERNAME = _result.stdout.strip() or os.getenv("USERNAME", "nicky")
except Exception:
    _WIN_USERNAME = os.getenv("USERNAME", "nicky")

_WIN_PROFILE_DIR = rf"C:\Users\{_WIN_USERNAME}\AppData\Local\stockpicker_automation\chrome_profile"
_DEBUG_PORT = int(os.getenv("STOCKPICKER_CHROME_DEBUG_PORT", "9223"))  # 9222 is Fidelity's

_STEALTH_INIT_SCRIPT = """
(function() {
    try { delete Object.getPrototypeOf(navigator).webdriver; } catch(e) {}
    Object.defineProperty(navigator, 'languages', {
        get: () => ['en-US', 'en'], configurable: true, enumerable: true
    });
    Object.defineProperty(window, 'outerHeight', {
        get: () => window.innerHeight + 87, configurable: true
    });
    Object.defineProperty(window, 'outerWidth', {
        get: () => window.innerWidth, configurable: true
    });
    const _artifacts = [
        'cdc_adoQpoasnfa76pfcZLmcfl_Array',
        'cdc_adoQpoasnfa76pfcZLmcfl_Promise',
        'cdc_adoQpoasnfa76pfcZLmcfl_Symbol',
        '__webdriver_script_fn', '__playwright', '__pw_manual',
        '$chrome_asyncScriptInfo',
    ];
    _artifacts.forEach(k => { try { delete window[k]; } catch(e) {} });
})();
"""

_chrome_proc: subprocess.Popen | None = None


def _atexit_cleanup() -> None:
    global _chrome_proc
    if _chrome_proc is not None:
        try:
            _chrome_proc.terminate()
        except Exception:
            pass
        _chrome_proc = None


atexit.register(_atexit_cleanup)


def _kill_existing() -> None:
    """Kill only OUR debug-port Chrome (never touches Fidelity's 9222 or the
    user's real browser windows) and clear a stale profile lockfile."""
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
    try:
        subprocess.run(["fuser", "-k", f"{_DEBUG_PORT}/tcp"], capture_output=True, timeout=5)
    except Exception:
        pass
    time.sleep(1.0)
    wsl_profile = Path(f"/mnt/c/Users/{_WIN_USERNAME}/AppData/Local/stockpicker_automation/chrome_profile")
    for lf in [wsl_profile / "lockfile", wsl_profile / "Default" / "lockfile"]:
        try:
            lf.unlink(missing_ok=True)
        except Exception:
            pass


def _wait_for_port(host: str, port: int, timeout: float = 20.0) -> None:
    """Poll the DevTools HTTP endpoint itself, not just the TCP socket.

    A raw `socket.create_connection` succeeds the instant Chrome's listener
    binds the port, which is measurably before the DevTools HTTP server is
    actually answering requests — DrissionPage's own connect then fails with
    a connection error despite the port having "opened." Confirmed by hand:
    a bare TCP check passed while `curl .../json/version` still refused.
    """
    import urllib.request
    deadline = time.time() + timeout
    url = f"http://{host}:{port}/json/version"
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1) as resp:
                if resp.status == 200:
                    return
        except Exception:
            time.sleep(0.3)
    raise RuntimeError(f"Chrome DevTools endpoint {url} did not respond within {timeout}s")


def _make_driver(headless: bool = True):
    """Launch a stealth Windows Chrome scoped to the stock-picker's own
    profile/port and connect DrissionPage to it. Mirrors
    fidelity_scraper.py::_make_driver but with no download-prefs patching
    (this module only reads pages) and its own profile/port."""
    global _chrome_proc
    from DrissionPage import ChromiumPage, ChromiumOptions

    _kill_existing()

    chrome_args = [
        _WSL_CHROME_BIN,
        f"--remote-debugging-port={_DEBUG_PORT}",
        f"--user-data-dir={_WIN_PROFILE_DIR}",
        "--profile-directory=Default",
        "--disable-blink-features=AutomationControlled",
        "--window-size=1536,864",
        "--lang=en-US",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-notifications",
        "--use-angle=default",
        "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
        "--disable-extensions",
        "--no-service-autorun",
    ]
    if headless:
        chrome_args.append("--headless=new")

    logger.info(f"[finviz_driver] Launching Windows Chrome on port {_DEBUG_PORT}...")
    _proc = subprocess.Popen(chrome_args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    try:
        _wait_for_port("127.0.0.1", _DEBUG_PORT, timeout=30)
    except RuntimeError:
        _proc.terminate()
        raise

    _chrome_proc = _proc

    opts = ChromiumOptions()
    opts.existing_only()
    opts.set_address(f"127.0.0.1:{_DEBUG_PORT}")
    # Must match the real Chrome process's headless state exactly. DrissionPage
    # compares this against what it detects on the live browser and, on a
    # mismatch, calls self.quit() to "fix" it before reconnecting — which
    # kills the only browser `existing_only()` is allowed to talk to, and the
    # reconnect then fails outright. Diagnosed 2026-09-04 by tracing
    # Chromium.__init__ directly; not documented anywhere in DrissionPage's
    # own error message, which just says "confirm the browser has started."
    opts.headless(headless)
    page = ChromiumPage(addr_or_opts=opts)
    page.add_init_js(_STEALTH_INIT_SCRIPT)
    logger.info("[finviz_driver] DrissionPage connected")
    return page


def _quit_driver(page) -> None:
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


def _wait_past_cloudflare(page, timeout: float = 20.0) -> bool:
    """Poll until the Cloudflare challenge title is gone. Returns False (not
    raises) on timeout — the caller decides whether that's fatal, since a
    slow-but-real page load looks the same for the first few seconds."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            title = page.title or ""
        except Exception:
            title = ""
        if "just a moment" not in title.lower():
            return True
        time.sleep(0.5)
    return False


def _wait_for_content(page, marker: str, timeout: float = 12.0, poll: float = 0.3) -> bool:
    """Poll `page.html` until a substring/marker actually appears, instead of
    sleeping a fixed duration and hoping the client-side widget finished by
    then.

    🔴 Replaces a real, measured bug in this function's own previous version:
    a fixed `time.sleep(3.0)` after Cloudflare cleared, on the theory that 3s
    is enough for the widget to render. It wasn't, reliably — re-running
    `finviz_insider.fetch_insiders()` three times in a row against the exact
    same URL returned 200, 0, 200 rows. The 0-row run was not a parsing bug
    and not a Cloudflare failure (both confirmed separately): `page.html`
    was captured a moment before the `fv-insider-row` elements existed, so
    the row-selector correctly found nothing in a real, complete HTML
    document — which then surfaces as a silently empty result, not an
    exception. `finviz_screener.py`'s own `_wait_for_rows` already used a
    poll-until-present pattern for exactly this reason; this generalizes
    that pattern for any caller's own content marker instead of hardcoding
    the screener's specific ticker-link regex."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            html = page.html
        except Exception:
            html = ""
        if marker in html:
            return True
        time.sleep(poll)
    return False


def _get_rendered_html(url: str, page: object | None = None, wait: float = 3.0,
                        retries: int = 2, content_marker: str | None = None) -> str | None:
    """Fetch a URL through a real browser and return the fully client-rendered
    HTML. Shared by any Finviz page whose content is populated by a
    client-side widget after load (confirmed live 2026-09-05 for the
    per-ticker Short Interest/Earnings/Forecast/Options tabs in
    `finviz_data.py`, and for the Insider/Managers pages in
    `finviz_insider.py`) — as opposed to `finviz_data.py`'s plain-`requests`
    Overview scrape, which doesn't need this. Pass an already-open `page` to
    reuse one browser across several calls; otherwise one is launched and
    torn down here. Returns None if Cloudflare doesn't clear within `timeout`
    (20s) — never a truncated/partial page.

    `content_marker`: a substring guaranteed to be present only once the
    real content has rendered (e.g. `"fv-insider-row"` for the Insiders feed).
    When given, this polls for it via `_wait_for_content` instead of a fixed
    sleep — see that function's docstring for the exact failure this fixes
    (a real 0-rows-of-203 result, reproduced 1-in-3 runs, from sleeping a
    fixed 3s that wasn't always enough). When omitted (no single marker
    applies, e.g. a page whose content shape varies by ticker), falls back to
    the fixed `wait` sleep — still better than nothing, but callers with a
    real marker should pass one.

    Retries on `DrissionPage.errors.PageDisconnectedError` — observed live
    2026-09-05, repeatedly, on a `page` reused across 2-3+ navigations in a
    row (a real, reproducible CDP-connection drop on this WSL2/Windows-Chrome
    setup, not a hypothetical). A bare re-`page.get(url)` after a short pause
    recovered every time it was tried by hand this session, so that's what
    this does automatically rather than surfacing a transient disconnect as
    a hard failure for every caller to handle itself."""
    owns_page = page is None
    if owns_page:
        page = _make_driver(headless=False)
    try:
        last_err = None
        for attempt in range(1, retries + 2):
            try:
                page.get(url)
                if not _wait_past_cloudflare(page, timeout=20):
                    return None
                if content_marker:
                    _wait_for_content(page, content_marker, timeout=max(wait * 4, 12.0))
                else:
                    time.sleep(wait)  # no marker given — best-effort fixed wait
                return page.html
            except Exception as e:
                last_err = e
                if attempt <= retries:
                    logger.warning(f"[finviz_driver] {type(e).__name__} fetching {url} "
                                    f"(attempt {attempt}); retrying")
                    time.sleep(1.5)
                    continue
                raise
        raise last_err  # unreachable, satisfies type checkers
    finally:
        if owns_page:
            _quit_driver(page)
