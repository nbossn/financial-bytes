"""PDF renderer using WeasyPrint."""
from __future__ import annotations

from pathlib import Path


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
    HTML(string=html_content).write_pdf(str(output_path))
