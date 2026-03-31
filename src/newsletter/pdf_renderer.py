"""PDF renderer using WeasyPrint."""
from __future__ import annotations

from pathlib import Path

from src.scrapers._utils import is_safe_url


def _safe_url_fetcher(url: str, **kwargs):
    """WeasyPrint url_fetcher that blocks SSRF via is_safe_url() before each fetch."""
    if not is_safe_url(url):
        raise ValueError(f"Blocked unsafe URL during PDF render: {url}")
    from weasyprint import default_url_fetcher
    return default_url_fetcher(url, **kwargs)


def render_pdf(html_content: str, output_path: Path) -> None:
    """Render HTML to PDF using WeasyPrint.

    Raises ImportError if WeasyPrint is not installed.
    Raises OSError / WeasyPrint exceptions on rendering failure.
    """
    try:
        from weasyprint import HTML
    except ImportError as exc:
        raise ImportError(
            "WeasyPrint is not installed. Run: pip install weasyprint"
        ) from exc

    output_path.parent.mkdir(parents=True, exist_ok=True)
    HTML(string=html_content, url_fetcher=_safe_url_fetcher).write_pdf(str(output_path))
