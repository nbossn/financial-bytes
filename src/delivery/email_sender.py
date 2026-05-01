"""Gmail SMTP email delivery for the daily newsletter."""
from __future__ import annotations

import smtplib
from datetime import date
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

from loguru import logger
from tenacity import retry, stop_after_attempt, wait_exponential

from src.config import settings


@retry(
    wait=wait_exponential(multiplier=1, min=2, max=30),
    stop=stop_after_attempt(3),
    reraise=True,
)
def _send_via_smtp(msg: MIMEMultipart) -> None:
    """Open SMTP connection and send message with retry."""
    with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=30) as server:
        server.ehlo()
        server.starttls()
        server.login(settings.smtp_user, settings.smtp_pass)
        server.send_message(msg)


def _build_subject(report_date: date, market_theme: str | None) -> str:
    date_str = report_date.strftime("%b %d")
    if market_theme:
        theme = market_theme.replace("\r", "").replace("\n", " ")
        theme = theme[:60] + "…" if len(theme) > 60 else theme
        return f"Financial Bytes ({date_str}) — {theme}"
    return f"Financial Bytes — Daily Portfolio Brief ({date_str})"


def _build_plain_text(markdown_content: str) -> str:
    """Strip Markdown syntax for the plain-text fallback part."""
    import re
    text = markdown_content
    # Remove headers
    text = re.sub(r"^#{1,6}\s+", "", text, flags=re.MULTILINE)
    # Remove bold/italic
    text = re.sub(r"\*{1,3}(.*?)\*{1,3}", r"\1", text)
    # Remove links
    text = re.sub(r"\[([^\]]+)\]\([^\)]+\)", r"\1", text)
    # Remove table separators
    text = re.sub(r"^\|[-| :]+\|$", "", text, flags=re.MULTILINE)
    # Collapse blank lines
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def send_newsletter(
    report_date: date,
    html_content: str,
    markdown_content: str,
    pdf_path: Path | None = None,
    market_theme: str | None = None,
    recipients: list[str] | None = None,
) -> bool:
    """Send the daily newsletter via Gmail SMTP.

    Returns True on success, False on failure.
    """
    to_list = recipients or [settings.email_recipient]
    subject = _build_subject(report_date, market_theme)

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = f"Financial Bytes <{settings.email_from}>"
    msg["To"] = ", ".join(to_list)

    # Plain text fallback
    plain = _build_plain_text(markdown_content)
    msg.attach(MIMEText(plain, "plain", "utf-8"))

    # HTML part (preferred)
    msg.attach(MIMEText(html_content, "html", "utf-8"))

    # Optional PDF attachment
    if pdf_path and pdf_path.exists():
        with open(pdf_path, "rb") as f:
            pdf_data = f.read()
        attachment = MIMEApplication(pdf_data, _subtype="pdf")
        attachment.add_header(
            "Content-Disposition",
            "attachment",
            filename=f"financial-bytes-{report_date.strftime('%Y-%m-%d')}.pdf",
        )
        # Wrap in a mixed envelope when we have an attachment
        outer = MIMEMultipart("mixed")
        outer["Subject"] = subject
        outer["From"] = msg["From"]
        outer["To"] = msg["To"]
        outer.attach(msg)
        outer.attach(attachment)
        msg = outer  # type: ignore[assignment]

    logger.info(f"Sending newsletter to {len(to_list)} recipient(s) — '{subject}'")

    try:
        _send_via_smtp(msg)
        _update_db_status(report_date, "sent")
        logger.info(f"Newsletter delivered to {len(to_list)} recipient(s)")
        return True
    except smtplib.SMTPAuthenticationError:
        # Do NOT log the exception object — it may echo back SMTP server error
        # strings that include credential fragments in some configurations.
        logger.error("Newsletter delivery failed: SMTP authentication rejected — check SMTP_USER/SMTP_PASS")
        _update_db_status(report_date, "failed")
        return False
    except Exception as exc:
        logger.error(f"Newsletter delivery failed: {type(exc).__name__}")
        _update_db_status(report_date, "failed")
        return False


def _update_db_status(report_date: date, status: str) -> None:
    try:
        from src.db.models import Newsletter
        from src.db.session import get_db

        with get_db() as db:
            rec = db.query(Newsletter).filter_by(report_date=report_date).first()
            if rec:
                rec.status = status
    except Exception as e:
        logger.debug(f"DB status update skipped: {e}")


def send_from_files(
    report_date: date,
    output_dir: Path,
    market_theme: str | None = None,
    recipients: list[str] | None = None,
) -> bool:
    """Convenience wrapper: read rendered files and send.

    Useful when calling from the CLI after generator.generate().
    """
    date_str = report_date.strftime("%Y-%m-%d")
    html_path = output_dir / f"{date_str}.html"
    md_path = output_dir / f"{date_str}.md"
    pdf_path = output_dir / f"{date_str}.pdf"

    if not html_path.exists():
        logger.error(f"HTML newsletter not found: {html_path}")
        return False

    html_content = html_path.read_text(encoding="utf-8")
    md_content = md_path.read_text(encoding="utf-8") if md_path.exists() else ""
    pdf = pdf_path if pdf_path.exists() else None

    return send_newsletter(
        report_date=report_date,
        html_content=html_content,
        markdown_content=md_content,
        pdf_path=pdf,
        market_theme=market_theme,
        recipients=recipients,
    )
