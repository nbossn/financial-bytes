"""Regression test for the unicode crash that silently killed 20 days of newsletters.

History this pins down (measured from logs/financial_bytes.log + archives):

    2026-06-25 -> 2026-07-14   newsletter generated every day, DELIVERED ZERO TIMES
    every one of those days    ERROR src.scheduler:_run_all_portfolios:209
                               "Combined group email failed for group 'nick': bad escape \\u"

`_send_group_email` splices each portfolio's <body> into the first portfolio's
HTML with `re.sub`. When the replacement is passed as a *string*, `re.sub`
parses it as a replacement TEMPLATE, so a literal backslash-u anywhere in the
newsletter body (interactive-chart JSON is full of them) raises
`re.error: bad escape \\u` and the whole group send dies.

The fix (92c0e6b, 2026-06-25) passes a callable instead — callables are handed
back verbatim and never template-parsed. It shipped with no test, which is why
nothing noticed that the crash continued for another 20 days.

These tests assert the *behaviour* (unicode-bearing content survives the
splice), not the implementation, so they stay honest if the splice is ever
rewritten without `re.sub`.
"""
from __future__ import annotations

import re

import pytest

from src.scheduler import _send_group_email


class _Pdef:
    """Minimal stand-in for a portfolio definition."""

    def __init__(self, name: str, label: str, recipients: list[str], group: str = "nick"):
        self.name = name
        self.label = label
        self.email_recipients = recipients
        self.email_group = group


def _html(body: str) -> str:
    return f"<html><head><title>t</title></head><body>{body}</body></html>"


def _run(tmp_path, bodies: list[str], monkeypatch) -> dict:
    """Drive _send_group_email over `bodies`, capturing what send_newsletter got."""
    captured: dict = {}

    def _fake_send(**kwargs):
        captured.update(kwargs)

    monkeypatch.setattr("src.delivery.email_sender.send_newsletter", _fake_send)

    runs = []
    for i, body in enumerate(bodies):
        html_path = tmp_path / f"p{i}.html"
        md_path = tmp_path / f"p{i}.md"
        html_path.write_text(_html(body), encoding="utf-8")
        md_path.write_text(f"# portfolio {i}", encoding="utf-8")
        runs.append((
            _Pdef(f"p{i}", f"Portfolio {i}", ["nick.bossn@gmail.com"]),
            {"paths": {"html": str(html_path), "md": str(md_path)},
             "report_date": "2026-07-26"},
        ))

    _send_group_email("nick", runs)
    return captured


# The exact payload shape that broke it: chart JSON carrying a \u escape.
UNICODE_BODY = '<script>var d={"t":"Alphabet \\u2014 AI capex repricing"};</script>'


def test_unicode_body_is_delivered_not_crashed(tmp_path, monkeypatch):
    """The bug, stated as behaviour: a \\u in the body must not stop the send."""
    captured = _run(tmp_path, [UNICODE_BODY, "<p>second portfolio</p>"], monkeypatch)

    assert captured, "send_newsletter was never called — the group send died"
    assert "second portfolio" in captured["html_content"], "second portfolio was dropped"
    # The escape must survive verbatim, not be interpreted or mangled.
    assert "\\u2014" in captured["html_content"]


def test_every_backslash_escape_class_survives(tmp_path, monkeypatch):
    """\\u is the one that bit us; \\g and \\1 are the other template landmines."""
    nasty = r'<script>a="\u2014"; b="\g<0>"; c="\1"; d="\\";</script>'
    captured = _run(tmp_path, [nasty, "<p>tail</p>"], monkeypatch)

    assert captured, "send_newsletter was never called"
    for token in (r"\u2014", r"\g<0>", r"\1"):
        assert token in captured["html_content"], f"{token} was consumed by the splice"


def test_plain_content_still_works(tmp_path, monkeypatch):
    """Positive control: the ordinary path must pass, or the tests above prove nothing."""
    captured = _run(tmp_path, ["<p>alpha</p>", "<p>beta</p>"], monkeypatch)

    assert captured
    assert "alpha" in captured["html_content"]
    assert "beta" in captured["html_content"]
    assert captured["recipients"] == ["nick.bossn@gmail.com"]


def test_string_replacement_form_really_does_crash():
    """Negative control — proves the tests above are guarding a real failure mode.

    Without this, a green suite could mean 'the bug is fixed' OR 'this content
    was never dangerous in the first place'. This pins it to the former.
    """
    with pytest.raises(re.error, match="bad escape"):
        re.sub(
            r"<body[^>]*>.*?</body>",
            "<body>" + UNICODE_BODY + "</body>",   # main's form: string replacement
            _html("x"),
            count=1,
            flags=re.DOTALL | re.IGNORECASE,
        )
