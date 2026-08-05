#!/usr/bin/env python3
"""Mutation harness for the reminder-delivery fix.

Each mutation is applied to a pristine copy of the tree, the new suite is run
against that copy, and the mutation is "caught" only if the suite goes red.

Two guards, both learned by getting them wrong before:

  * Every mutation asserts its anchor actually applied. A no-op edit runs the
    unmutated tree and reports "all pass", which reads exactly like a caught
    mutation from the outside.
  * The mutated copy is imported from its own directory and that is verified,
    because this venv carries a .pth pointing unconditionally at the live
    checkout — a mutant that silently imports the fixed source is a false
    "caught".
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PY = REPO / ".venv" / "bin" / "python"
SUITE = "tests/test_reminder_delivery.py"

# (id, relative path, find, replace, why it must be caught)
MUTATIONS = [
    ("M01", "src/scheduler.py",
     '            "— reminder alert NOT sent; reminders stay pending"\n        )\n        return False',
     '            "— reminder alert NOT sent; reminders stay pending"\n        )\n        return True',
     "no webhook reported as delivered — the original production failure"),

    ("M02", "src/scheduler.py",
     'logger.warning(f"Reminder Discord alert failed: {e} — reminders stay pending")\n        return False',
     'logger.warning(f"Reminder Discord alert failed: {e} — reminders stay pending")\n        return True',
     "transport failure reported as delivered"),

    ("M03", "src/scheduler.py",
     '    if not _send_reminder_discord(due):',
     '    if False and not _send_reminder_discord(due):',
     "restores the bug: mark sent regardless of delivery"),

    ("M04", "src/scheduler.py",
     '            f"Reminder check: {len(due)} reminder(s) left PENDING — delivery failed"\n        )\n        return',
     '            f"Reminder check: {len(due)} reminder(s) left PENDING — delivery failed"\n        )',
     "falls through to mark_sent after reporting the failure"),

    ("M05", "src/config.py",
     '    return (settings.discord_webhook_url or "").strip()',
     '    return ""',
     "drops the .env fallback — the daemon case goes dark again"),

    ("M06", "src/config.py",
     '    exported = (os.environ.get("DISCORD_WEBHOOK_URL") or "").strip()\n    if exported:\n        return exported',
     '    exported = ""\n    if exported:\n        return exported',
     "drops process-env precedence, so an explicit override is ignored"),

    ("M07", "src/config.py",
     '    exported = (os.environ.get("DISCORD_WEBHOOK_URL") or "").strip()',
     '    exported = os.environ.get("DISCORD_WEBHOOK_URL") or ""',
     "whitespace-only export treated as configured"),

    ("M08", "src/config.py",
     '    return (settings.discord_webhook_url or "").strip()',
     '    return settings.discord_webhook_url or ""',
     "whitespace-only .env value treated as configured"),

    ("M09", "src/portfolio/reminders.py",
     '        if dl < today:\n            expired.append(r)',
     '        if dl <= today:\n            expired.append(r)',
     "boundary: a reminder due TODAY reported as expired"),

    ("M10", "src/portfolio/reminders.py",
     '    for r in _load()["reminders"]:\n        if r.get("sent"):\n            continue',
     '    for r in _load()["reminders"]:\n        if False:\n            continue',
     "already-delivered reminders reported as lost"),

    ("M11", "src/portfolio/reminders.py",
     '        if dl < today:\n            expired.append(r)',
     '        if dl > today:\n            expired.append(r)',
     "inverts the window — future reminders reported as expired"),

    ("M12", "src/scheduler.py",
     '    expired = get_expired_unsent_reminders()\n    if expired:',
     '    expired = get_expired_unsent_reminders()\n    if False:',
     "expiry computed and never reported — silent loss returns"),

    ("M13", "src/scheduler.py",
     '            "— premarket alert NOT sent"\n        )\n        return False',
     '            "— premarket alert NOT sent"\n        )\n        return True',
     "premarket: no webhook reported as delivered"),

    ("M14", "src/scheduler.py",
     '    logger.info("Premarket earnings Discord alert sent")\n    return True',
     '    logger.info("Premarket earnings Discord alert sent")\n    return None',
     "premarket success reports no outcome"),

    ("M15", "src/scheduler.py",
     "        lines.append(f\"• **Deadline {r['deadline']}:** {r['context']}\")",
     '        lines.append("• a reminder is due")',
     "the reminder's own text never reaches the message"),

    ("M16", "src/alerts/stop_loss.py",
     'from src.config import discord_webhook',
     'from src.config import discord_webhook  # noqa\nimport os as _os\ndiscord_webhook = lambda: _os.getenv("DISCORD_WEBHOOK_URL")',
     "reintroduces a bare env lookup at a posting site"),

    ("M17", "src/scheduler.py",
     '    logger.info(f"Reminder Discord alert sent ({len(reminders)} reminder(s))")\n    return True',
     '    logger.info(f"Reminder Discord alert sent ({len(reminders)} reminder(s))")\n    return False',
     "successful delivery reported as failed — reminders never clear"),
]


def run_one(mid: str, relpath: str, find: str, repl: str, why: str) -> tuple[str, str]:
    with tempfile.TemporaryDirectory(prefix=f"mut-{mid}-") as td:
        dest = Path(td) / "tree"
        dest.mkdir(parents=True, exist_ok=True)
        proc = subprocess.run(
            ["bash", "-c", f"git archive HEAD | tar -x -C '{dest}'"],
            cwd=REPO, capture_output=True, text=True,
        )
        if proc.returncode != 0:
            return "ERROR", f"archive failed: {proc.stderr[:200]}"

        # Overlay the *working tree* versions of the files under test, since the
        # fix is not committed yet: the mutant must be the fixed source minus
        # one property, not the pre-fix source.
        for f in {m[1] for m in MUTATIONS} | {SUITE}:
            src_f = REPO / f
            dst_f = dest / f
            dst_f.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src_f, dst_f)

        target = dest / relpath
        text = target.read_text()
        if find not in text:
            return "REFUSED", "anchor not found — mutation did not apply"
        mutated = text.replace(find, repl, 1)
        if mutated == text:
            return "REFUSED", "replacement was a no-op"
        target.write_text(mutated)

        # Prove the mutant tree is the one imported.
        pin = subprocess.run(
            [str(PY), "-c",
             "import sys; sys.path.insert(0,''); import src.config as c; print(c.__file__)"],
            cwd=dest, capture_output=True, text=True,
            env={"PATH": "/usr/bin:/bin", "HOME": str(Path.home()),
                 "ANTHROPIC_API_KEY": "x", "MASSIVE_API_KEY": "x",
                 "DATABASE_URL": "sqlite:///:memory:", "EMAIL_RECIPIENT": "a@b.c",
                 "EMAIL_FROM": "a@b.c", "SMTP_USER": "a@b.c", "SMTP_PASS": "x"},
        )
        if str(dest) not in pin.stdout:
            return "ERROR", f"pin failed, imported: {pin.stdout.strip()[:120]}"

        res = subprocess.run(
            [str(PY), "-m", "pytest", SUITE, "-q", "--no-cov", "-p", "no:cacheprovider"],
            cwd=dest, capture_output=True, text=True,
        )
        tail = [l for l in res.stdout.strip().splitlines() if "passed" in l or "failed" in l]
        summary = tail[-1] if tail else res.stdout.strip()[-120:]
        return ("CAUGHT" if res.returncode != 0 else "SURVIVED"), summary


def main() -> int:
    caught = survived = refused = errored = 0
    print(f"Mutation run — {len(MUTATIONS)} mutations against {SUITE}\n")
    for mid, relpath, find, repl, why in MUTATIONS:
        verdict, detail = run_one(mid, relpath, find, repl, why)
        mark = {"CAUGHT": "✅", "SURVIVED": "🔴", "REFUSED": "⚠️", "ERROR": "⚠️"}[verdict]
        print(f"{mark} {mid} {verdict:9s} {relpath:32s} {why}")
        if verdict != "CAUGHT":
            print(f"      -> {detail}")
        caught += verdict == "CAUGHT"
        survived += verdict == "SURVIVED"
        refused += verdict == "REFUSED"
        errored += verdict == "ERROR"
    print(f"\n{caught} caught · {survived} survived · {refused} refused · {errored} errored")
    return 0 if (survived == 0 and refused == 0 and errored == 0) else 1


if __name__ == "__main__":
    sys.exit(main())
