"""Shared scraper utilities."""
import ipaddress
import socket
from urllib.parse import urlparse


def is_safe_url(url: str) -> bool:
    """Reject non-http(s) schemes, private/loopback IPs, and internal hostnames (SSRF guard).

    Performs DNS resolution to block hostnames that resolve to private ranges,
    closing the DNS-rebinding bypass present in a pure IP-address check.
    """
    try:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            return False
        host = parsed.hostname or ""
        if not host:
            return False

        # Resolve hostname to catch internal hostnames (e.g. metadata.google.internal)
        try:
            ip_str = socket.getaddrinfo(host, None)[0][4][0]
            addr = ipaddress.ip_address(ip_str)
            if addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_reserved:
                return False
        except (socket.gaierror, ValueError):
            return False  # fail-closed: unresolvable or invalid hosts are denied

        return True
    except Exception:
        return False
