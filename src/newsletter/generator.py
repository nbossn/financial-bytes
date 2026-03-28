"""Newsletter generator — renders HTML, Markdown, and PDF from director + analyst reports."""
from __future__ import annotations

import re
from datetime import date
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape
from loguru import logger

from src.agents.analyst_agent import AnalystReport
from src.agents.director_agent import DirectorReport
from src.portfolio.models import PortfolioSnapshot

TEMPLATE_DIR = Path(__file__).parent / "templates"
OUTPUT_DIR = Path("newsletters")


def _make_env() -> Environment:
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATE_DIR)),
        autoescape=select_autoescape(["html", "xml"]),
    )

    def format_number(value) -> str:
        try:
            return f"{float(value):,.2f}"
        except (TypeError, ValueError):
            return str(value)

    env.filters["format_number"] = format_number
    env.filters["abs"] = abs
    return env


def _collect_sources(analyst_reports: list[AnalystReport]) -> list[str]:
    """Return deduplicated source list for the newsletter footer."""
    sources = ["Finviz", "Yahoo Finance", "CNBC", "Reuters", "MarketWatch",
               "Seeking Alpha", "massive.com (Benzinga)"]
    return sources


def _render_html(
    report: DirectorReport,
    analyst_reports: list[AnalystReport],
    snapshot: PortfolioSnapshot,
) -> str:
    env = _make_env()
    template = env.get_template("daily.html.j2")
    return template.render(
        report=report,
        analyst_reports=analyst_reports,
        snapshot=snapshot,
        sources=_collect_sources(analyst_reports),
    )


def _render_markdown(
    report: DirectorReport,
    analyst_reports: list[AnalystReport],
    snapshot: PortfolioSnapshot,
) -> str:
    # Markdown template uses raw Jinja (no HTML autoescape)
    from jinja2 import Environment as JinjaEnv, FileSystemLoader as JFL

    env = JinjaEnv(loader=JFL(str(TEMPLATE_DIR)), autoescape=False)

    def format_number(value) -> str:
        try:
            return f"{float(value):,.2f}"
        except (TypeError, ValueError):
            return str(value)

    env.filters["format_number"] = format_number
    env.filters["abs"] = abs

    template = env.get_template("daily.md.j2")
    return template.render(
        report=report,
        analyst_reports=analyst_reports,
        snapshot=snapshot,
        sources=_collect_sources(analyst_reports),
    )


def generate(
    report: DirectorReport,
    analyst_reports: list[AnalystReport],
    snapshot: PortfolioSnapshot,
    report_date: date | None = None,
    output_dir: Path | None = None,
) -> dict[str, Path]:
    """Render HTML + Markdown (+ PDF if WeasyPrint available).

    Returns a dict: {"html": Path, "md": Path, "pdf": Path | None}
    """
    today = report_date or date.today()
    date_str = today.strftime("%Y-%m-%d")
    out = output_dir or OUTPUT_DIR
    out.mkdir(parents=True, exist_ok=True)

    paths: dict[str, Path | None] = {}

    # ── HTML ──────────────────────────────────────────────
    html_content = _render_html(report, analyst_reports, snapshot)
    html_path = out / f"{date_str}.html"
    html_path.write_text(html_content, encoding="utf-8")
    paths["html"] = html_path
    logger.info(f"Newsletter HTML written → {html_path}")

    # ── Markdown ──────────────────────────────────────────
    md_content = _render_markdown(report, analyst_reports, snapshot)
    md_path = out / f"{date_str}.md"
    md_path.write_text(md_content, encoding="utf-8")
    paths["md"] = md_path
    logger.info(f"Newsletter Markdown written → {md_path}")

    # ── PDF ───────────────────────────────────────────────
    pdf_path = out / f"{date_str}.pdf"
    try:
        from src.newsletter.pdf_renderer import render_pdf
        render_pdf(html_content, pdf_path)
        paths["pdf"] = pdf_path
        logger.info(f"Newsletter PDF written → {pdf_path}")
    except Exception as e:
        logger.warning(f"PDF rendering skipped: {e}")
        paths["pdf"] = None

    # ── DB ────────────────────────────────────────────────
    _save_newsletter(report.report_date, html_content, md_content, paths.get("pdf"))

    return paths  # type: ignore[return-value]


def _save_newsletter(
    report_date: date,
    html_content: str,
    md_content: str,
    pdf_path: Path | None,
) -> None:
    try:
        from src.db.models import Newsletter
        from src.db.session import get_db

        with get_db() as db:
            existing = db.query(Newsletter).filter_by(report_date=report_date).first()
            data = dict(
                html_content=html_content,
                markdown_content=md_content,
                pdf_path=str(pdf_path) if pdf_path else None,
                status="generated",
            )
            if existing:
                for k, v in data.items():
                    setattr(existing, k, v)
            else:
                db.add(Newsletter(report_date=report_date, **data))
    except Exception as e:
        logger.warning(f"Newsletter DB save failed: {e}")
