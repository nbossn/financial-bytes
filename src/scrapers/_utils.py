"""Shared scraper utilities."""
import ipaddress
from urllib.parse import urlparse


def is_safe_url(url: str) -> bool:
    """Reject non-http(s) schemes and private/loopback IP ranges (SSRF guard)."""
    try:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            return False
        host = parsed.hostname or ""
        try:
            addr = ipaddress.ip_address(host)
            if addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_reserved:
                return False
        except ValueError:
            pass  # hostname, not a bare IP — allow
        return True
    except Exception:
        return False
