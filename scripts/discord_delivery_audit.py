#!/usr/bin/env python3
"""Can the Discord alert channel actually open in the environment it runs in?

The scheduler daemon is launched by cron and @reboot. On 2026-08-05 the live
process (PID 264) had EIGHT environment variables and DISCORD_WEBHOOK_URL was
not one of them, while `.env` had held the value since May. Every Discord
posting site read `os.getenv`, so the whole channel reported itself
"not configured" — and `_run_reminder_check` marked reminders sent anyway.

The pipeline only *logs* that, and a log line is not something that can fail.
This is the consumer that can.

    exit 0  — the channel resolves under a cron-like environment, and no
              reminder has expired undelivered
    exit 1  — findings
    exit 2  — could not measure (controls did not behave), so no verdict

Usage:
    scripts/discord_delivery_audit.py [--repo PATH]

--repo pins the tree under audit. It defaults to this script's own repository
rather than the current directory, because running as `python scripts/x.py`
puts `scripts/` on sys.path — not the repo root — and this venv carries a .pth
that then resolves `src.*` to the installed checkout. A tool that audits a tree
must pin the tree it audits.
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

# The environment a cron-launched daemon actually gets. Measured from
# /proc/<pid>/environ of the running scheduler, not assumed.
CRON_ENV = {
    "HOME": os.path.expanduser("~"),
    "LANG": "C.UTF-8",
    "LOGNAME": os.environ.get("LOGNAME", "nboss"),
    "PATH": "/usr/bin:/bin",
    "SHELL": "/bin/sh",
    "TZ": "America/New_York",
}

_RESOLVE = (
    "import sys; sys.path.insert(0, '');"
    "from src.config import discord_webhook;"
    "w = discord_webhook();"
    "print('RESOLVED' if w else 'EMPTY')"
)

BARE_LOOKUP = re.compile(
    r"""(os\.getenv|os\.environ\.get|os\.environ\[)\s*\(?\s*['"]DISCORD_WEBHOOK_URL"""
)


def _resolve_under_cron_env(repo: Path, env_file: Path | None) -> tuple[str, str]:
    """Run the real resolver in a subprocess with a cron-like environment."""
    env = dict(CRON_ENV)
    # Settings reads `.env` relative to cwd, so cwd selects which file is seen.
    cwd = repo if env_file is None else env_file.parent
    proc = subprocess.run(
        [sys.executable, "-c", _RESOLVE],
        cwd=str(cwd), capture_output=True, text=True, env=env, timeout=60,
    )
    return proc.stdout.strip(), proc.stderr.strip()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default=str(Path(__file__).resolve().parent.parent))
    args = ap.parse_args()
    repo = Path(args.repo).resolve()

    print(f"Discord delivery audit — tree under audit: {repo}")
    if not (repo / "src" / "config.py").exists():
        print(f"  CANNOT MEASURE: {repo}/src/config.py does not exist")
        return 2

    # ── controls first, before any verdict ────────────────────────
    stub_env = {
        "ANTHROPIC_API_KEY": "x", "MASSIVE_API_KEY": "x",
        "DATABASE_URL": "sqlite:///:memory:", "EMAIL_RECIPIENT": "a@b.c",
        "EMAIL_FROM": "a@b.c", "SMTP_USER": "a@b.c", "SMTP_PASS": "x",
    }
    with tempfile.TemporaryDirectory() as td:
        good = Path(td) / "good"
        bad = Path(td) / "bad"
        for d in (good, bad):
            d.mkdir()
            (d / "src").symlink_to(repo / "src")
        base = "\n".join(f"{k}={v}" for k, v in stub_env.items())
        (good / ".env").write_text(base + "\nDISCORD_WEBHOOK_URL=https://discord.test/control\n")
        (bad / ".env").write_text(base + "\n")

        pos, pos_err = _resolve_under_cron_env(repo, good / ".env")
        neg, neg_err = _resolve_under_cron_env(repo, bad / ".env")

    if pos != "RESOLVED":
        print(f"  CANNOT MEASURE: positive control did not resolve ({pos!r}) {pos_err[:200]}")
        return 2
    if neg != "EMPTY":
        print(f"  CANNOT MEASURE: negative control did not come back empty ({neg!r}) {neg_err[:200]}")
        return 2
    print("  controls: a configured webhook resolves, an absent one does not — "
          "this check can say both yes and no")

    findings: list[str] = []

    # ── 1. the production invariant ───────────────────────────────
    live, live_err = _resolve_under_cron_env(repo, None)
    if live == "RESOLVED":
        print("  [OK]   webhook resolves under a cron-like environment "
              f"({len(CRON_ENV)} env vars, none of them DISCORD_WEBHOOK_URL)")
    elif live == "EMPTY":
        findings.append(
            "the Discord webhook does NOT resolve under a cron-like environment. "
            "Every scheduled alert — reminders, pre-market — is silently dead in "
            "the daemon even if an interactive shell can see the value."
        )
    else:
        print(f"  CANNOT MEASURE: live resolution returned {live!r} {live_err[:300]}")
        return 2

    # ── 2. no posting site may bypass the resolver ────────────────
    posting, offenders = [], []
    for py in (repo / "src").rglob("*.py"):
        text = py.read_text(encoding="utf-8", errors="replace")
        if "DISCORD_WEBHOOK_URL" not in text and "discord_webhook" not in text:
            continue
        if "def discord_webhook(" in text:  # the resolver itself
            continue
        posting.append(py)
        if BARE_LOOKUP.search(text):
            offenders.append(str(py.relative_to(repo)))

    if len(posting) < 4:
        print(f"  CANNOT MEASURE: discovery found only {len(posting)} Discord posting "
              "file(s); a sweep that passes on zero inputs is not a sweep")
        return 2
    if offenders:
        findings.append(
            f"{len(offenders)} posting site(s) read the process environment directly "
            f"instead of src.config.discord_webhook(): {', '.join(offenders)}"
        )
    else:
        print(f"  [OK]   all {len(posting)} Discord posting site(s) go through the resolver")

    # ── 3. reminders that expired undelivered ─────────────────────
    proc = subprocess.run(
        [sys.executable, "-c",
         "import sys, json; sys.path.insert(0, '');"
         "from src.portfolio.reminders import get_expired_unsent_reminders as g;"
         "print(json.dumps([{'id': r.get('id'), 'deadline': r.get('deadline')} for r in g()]))"],
        cwd=str(repo), capture_output=True, text=True, timeout=60,
    )
    if proc.returncode != 0:
        print(f"  CANNOT MEASURE: expiry query failed — {proc.stderr.strip()[:300]}")
        return 2
    import json
    expired = json.loads(proc.stdout.strip().splitlines()[-1])
    if expired:
        detail = ", ".join(f"{r['id']} (deadline {r['deadline']})" for r in expired)
        findings.append(f"{len(expired)} reminder(s) expired UNDELIVERED: {detail}")
    else:
        print("  [OK]   no reminder has expired undelivered")

    print()
    if findings:
        print(f"RESULT: FAIL — {len(findings)} finding(s)")
        for f in findings:
            print(f"  - {f}")
        return 1
    print("RESULT: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
