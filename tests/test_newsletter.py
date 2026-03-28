"""Tests for newsletter generator and email sender."""
from __future__ import annotations

import tempfile
from datetime import date
from pathlib import Path

import pytest


class TestNewsletterGenerator:
    def test_generate_html(self, sample_snapshot, sample_analyst_report, sample_director_report, tmp_path):
        from src.newsletter.generator import generate

        with __import__("unittest.mock", fromlist=["patch"]).patch("src.newsletter.generator._save_newsletter"):
            paths = generate(
                report=sample_director_report,
                analyst_reports=[sample_analyst_report],
                snapshot=sample_snapshot,
                output_dir=tmp_path,
            )

        assert "html" in paths
        assert paths["html"].exists()
        html = paths["html"].read_text()
        assert "Financial Bytes" in html
        assert "MSFT" in html
        assert "Director" in html

    def test_generate_markdown(self, sample_snapshot, sample_analyst_report, sample_director_report, tmp_path):
        from src.newsletter.generator import generate

        with __import__("unittest.mock", fromlist=["patch"]).patch("src.newsletter.generator._save_newsletter"):
            paths = generate(
                report=sample_director_report,
                analyst_reports=[sample_analyst_report],
                snapshot=sample_snapshot,
                output_dir=tmp_path,
            )

        assert "md" in paths
        assert paths["md"].exists()
        md = paths["md"].read_text()
        assert "# Financial Bytes" in md
        assert "MSFT" in md

    def test_generate_creates_dated_files(self, sample_snapshot, sample_analyst_report,
                                          sample_director_report, tmp_path):
        from src.newsletter.generator import generate

        with __import__("unittest.mock", fromlist=["patch"]).patch("src.newsletter.generator._save_newsletter"):
            paths = generate(
                report=sample_director_report,
                analyst_reports=[sample_analyst_report],
                snapshot=sample_snapshot,
                report_date=date(2026, 3, 27),
                output_dir=tmp_path,
            )

        assert "2026-03-27" in str(paths["html"])
        assert "2026-03-27" in str(paths["md"])

    def test_html_contains_sentiment_bar(self, sample_snapshot, sample_analyst_report,
                                          sample_director_report, tmp_path):
        from src.newsletter.generator import generate

        with __import__("unittest.mock", fromlist=["patch"]).patch("src.newsletter.generator._save_newsletter"):
            paths = generate(
                report=sample_director_report,
                analyst_reports=[sample_analyst_report],
                snapshot=sample_snapshot,
                output_dir=tmp_path,
            )

        html = paths["html"].read_text()
        assert "sentiment-bar" in html

    def test_html_contains_action_items(self, sample_snapshot, sample_analyst_report,
                                        sample_director_report, tmp_path):
        from src.newsletter.generator import generate

        with __import__("unittest.mock", fromlist=["patch"]).patch("src.newsletter.generator._save_newsletter"):
            paths = generate(
                report=sample_director_report,
                analyst_reports=[sample_analyst_report],
                snapshot=sample_snapshot,
                output_dir=tmp_path,
            )

        html = paths["html"].read_text()
        assert "Monitor NVDA earnings" in html

    def test_html_contains_disclaimer(self, sample_snapshot, sample_analyst_report,
                                      sample_director_report, tmp_path):
        from src.newsletter.generator import generate

        with __import__("unittest.mock", fromlist=["patch"]).patch("src.newsletter.generator._save_newsletter"):
            paths = generate(
                report=sample_director_report,
                analyst_reports=[sample_analyst_report],
                snapshot=sample_snapshot,
                output_dir=tmp_path,
            )

        html = paths["html"].read_text()
        assert "financial advice" in html.lower()


class TestEmailSender:
    def test_build_subject_with_theme(self):
        from src.delivery.email_sender import _build_subject
        subject = _build_subject(date(2026, 3, 27), "AI dominates markets")
        assert "Mar 27" in subject
        assert "AI dominates" in subject

    def test_build_subject_without_theme(self):
        from src.delivery.email_sender import _build_subject
        subject = _build_subject(date(2026, 3, 27), None)
        assert "Financial Bytes" in subject
        assert "Mar 27" in subject

    def test_build_plain_text_strips_markdown(self):
        from src.delivery.email_sender import _build_plain_text
        md = "# Header\n**Bold text** and *italic*\n[Link](https://example.com)"
        plain = _build_plain_text(md)
        assert "#" not in plain
        assert "**" not in plain
        assert "[Link]" not in plain
        assert "Header" in plain
        assert "Bold text" in plain

    def test_send_newsletter_smtp_called(self, tmp_path):
        from src.delivery.email_sender import send_newsletter
        from unittest.mock import patch, MagicMock

        with patch("src.delivery.email_sender._send_via_smtp") as mock_smtp, \
             patch("src.delivery.email_sender._update_db_status"):
            mock_smtp.return_value = None
            result = send_newsletter(
                report_date=date(2026, 3, 27),
                html_content="<html><body>Test</body></html>",
                markdown_content="# Test Newsletter",
                pdf_path=None,
                market_theme="Test theme",
                recipients=["test@example.com"],
            )

        assert result is True
        mock_smtp.assert_called_once()

    def test_send_newsletter_handles_smtp_error(self):
        from src.delivery.email_sender import send_newsletter
        from unittest.mock import patch

        with patch("src.delivery.email_sender._send_via_smtp") as mock_smtp, \
             patch("src.delivery.email_sender._update_db_status"):
            mock_smtp.side_effect = Exception("SMTP connection refused")
            result = send_newsletter(
                report_date=date(2026, 3, 27),
                html_content="<html><body>Test</body></html>",
                markdown_content="# Test",
                recipients=["test@example.com"],
            )

        assert result is False

    def test_send_newsletter_with_pdf(self, tmp_path):
        from src.delivery.email_sender import send_newsletter
        from unittest.mock import patch

        pdf_file = tmp_path / "test.pdf"
        pdf_file.write_bytes(b"%PDF fake pdf content")

        with patch("src.delivery.email_sender._send_via_smtp") as mock_smtp, \
             patch("src.delivery.email_sender._update_db_status"):
            mock_smtp.return_value = None
            result = send_newsletter(
                report_date=date(2026, 3, 27),
                html_content="<html><body>Test</body></html>",
                markdown_content="# Test",
                pdf_path=pdf_file,
                recipients=["test@example.com"],
            )

        assert result is True
        # Message should be a mixed MIME (with PDF attachment)
        call_args = mock_smtp.call_args[0][0]
        assert call_args.get_content_type() == "multipart/mixed"
