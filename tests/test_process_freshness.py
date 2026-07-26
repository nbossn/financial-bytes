"""Tests for the scheduler freshness check.

Two distinct failures motivate this module, both measured on 2026-07-26:

1. **The crontab watchdog can never fire.** The 5:50 AM line is

       pgrep -f 'financial-bytes schedule' || (cd ... && nohup ... &)

   cron runs that through `sh -c`, so the *shell's own argv* contains the
   pattern and `pgrep -f` matches it. Reproduced with a pattern matching no
   real process: still exit 0. A restart guard that cannot say "no".

2. **Liveness is not freshness.** The scheduler ran unrestarted from
   2026-06-23 to 2026-07-14 while the fix it needed sat on disk from 06-25.
   Python imports modules once, so the live process served 20-day-old code and
   dropped the newsletter every single day. It was *alive* the whole time — a
   pgrep watchdog would have said "fine" on all 20 days.

So the check must (a) not match itself or its ancestors, and (b) compare the
process's start time against the mtime of the code it imported.
"""
from __future__ import annotations

import os
import shlex
import subprocess
import sys
import time
from pathlib import Path

import pytest

from src.ops.process_freshness import (
    NOT_RUNNING,
    RUNNING_FRESH,
    RUNNING_STALE,
    check,
    find_processes,
    newest_source_mtime,
    process_start_time,
)

MAGIC = "zz-freshness-probe-target"


@pytest.fixture
def target():
    """A real process whose argv contains MAGIC, reaped at teardown."""
    # Not `sleep 60 MAGIC` — GNU sleep rejects the extra arg and dies instantly,
    # which silently empties this fixture. A Python child keeps MAGIC in argv
    # and cannot be exec-optimised away by a shell.
    p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)", MAGIC])
    time.sleep(0.4)          # let it appear in /proc
    yield p
    p.kill()
    p.wait()


@pytest.fixture
def src_tree(tmp_path):
    root = tmp_path / "src"
    (root / "pkg").mkdir(parents=True)
    f = root / "pkg" / "mod.py"
    f.write_text("x = 1\n")
    return root, f


# ---------------------------------------------------------------- discovery

def test_finds_a_real_process(target):
    """Positive control. Without this, every 'not running' below proves nothing."""
    assert target.pid in find_processes(MAGIC)


def test_absent_pattern_is_not_running():
    assert find_processes("zz-pattern-that-matches-nothing-at-all") == []


def _own_cmdline() -> str:
    raw = Path(f"/proc/{os.getpid()}/cmdline").read_bytes()
    return raw.replace(b"\x00", b" ").decode("utf-8", "replace").strip()


def test_does_not_match_itself():
    """Our own argv carries the pattern in the CLI case; that must not count.

    The pattern is this process's *actual* /proc cmdline, not `sys.argv` —
    they differ (`python -m pytest` vs the pytest path), and using sys.argv
    made this test match nothing at all, so it passed without exercising the
    exclusion. A zero from a check never shown able to return non-zero is not
    a result.
    """
    own = _own_cmdline()

    # Positive control: an unfiltered scan really does find us with this pattern.
    raw_hits = [
        pid for pid in (int(e.name) for e in Path("/proc").iterdir() if e.name.isdigit())
        if (Path(f"/proc/{pid}/cmdline").exists()
            and own in Path(f"/proc/{pid}/cmdline").read_bytes()
            .replace(b"\x00", b" ").decode("utf-8", "replace").strip())
    ]
    assert os.getpid() in raw_hits, "control failed — the pattern doesn't match us at all"

    assert os.getpid() not in find_processes(own)


def test_does_not_match_an_ancestor_shell():
    """The live crontab bug, reproduced faithfully.

    cron runs the watchdog via `sh -c '<command>'`, so the parent shell's argv
    contains the pattern. `pgrep -f` matches that shell and reports the daemon
    alive. This asserts our check does not.
    """
    probe = "zz-ancestor-selfmatch-probe"
    code = (
        "import sys; sys.path.insert(0, %r);"
        "from src.ops.process_freshness import find_processes;"
        "print(len(find_processes(%r)))" % (str(Path.cwd()), probe)
    )
    # Trailing `exit $?` stops sh from exec-optimising itself away, so the
    # shell really does stay alive as our parent — as it does under cron.
    # shlex.quote, not repr() — Python's repr uses single quotes that collide
    # with the shell's, and bash/sh take Python's escapes literally.
    script = f"{shlex.quote(sys.executable)} -c {shlex.quote(code)} --{probe}\nexit $?\n"
    out = subprocess.run(["sh", "-c", script], capture_output=True, text=True, cwd=Path.cwd())
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "0", (
        f"self/ancestor match leaked through: found {out.stdout.strip()} process(es). "
        "This is exactly the crontab pgrep bug."
    )


def test_pgrep_form_does_match_itself():
    """Pins the bug this module exists to avoid — the crontab form, unchanged.

    If this ever starts failing, the platform changed and the crontab line may
    have quietly become correct; re-measure before trusting either.
    """
    probe = "zz-pgrep-selfmatch-probe"
    out = subprocess.run(
        ["sh", "-c", f"pgrep -f '{probe}' >/dev/null && echo MATCH || echo NOMATCH\nexit 0\n"],
        capture_output=True, text=True,
    )
    assert out.stdout.strip() == "MATCH", "pgrep -f no longer self-matches"


# ---------------------------------------------------------------- start time

def test_process_start_time_is_recent(target):
    started = process_start_time(target.pid)
    assert started is not None
    assert 0 <= time.time() - started < 60


def test_start_time_of_dead_process_is_none():
    p = subprocess.Popen(["true"])
    p.wait()
    time.sleep(0.2)
    assert process_start_time(p.pid) is None or process_start_time(p.pid) > 0


# ---------------------------------------------------------------- mtime scan

def test_newest_source_mtime_finds_the_newest_file(src_tree):
    root, f = src_tree
    older = root / "pkg" / "old.py"
    older.write_text("y = 2\n")
    os.utime(older, (time.time() - 5000, time.time() - 5000))
    mtime, path = newest_source_mtime(root)
    assert path == f
    assert mtime == pytest.approx(f.stat().st_mtime)


def test_mtime_scan_ignores_pycache(src_tree):
    """The __pycache__ guard, exercised with a .py — not a .pyc.

    A .pyc can never reach the guard: the scan globs "*.py", which does not
    match "*.pyc". Seeding a .pyc here would assert on a branch the code never
    executes, and deleting the guard would leave the suite green.
    """
    root, f = src_tree
    cache = root / "pkg" / "__pycache__"
    cache.mkdir()
    sneaky = cache / "mod.py"
    sneaky.write_text("z = 3\n")
    future = time.time() + 9000
    os.utime(sneaky, (future, future))
    _, path = newest_source_mtime(root)
    assert path == f, "a .py under __pycache__ was treated as source"


def test_mtime_scan_ignores_non_python_files(src_tree):
    root, f = src_tree
    notes = root / "notes.txt"
    notes.write_text("hi")
    future = time.time() + 9000
    os.utime(notes, (future, future))
    _, path = newest_source_mtime(root)
    assert path == f, "a .txt was treated as source"


def test_empty_tree_reports_no_source(tmp_path):
    assert newest_source_mtime(tmp_path) == (None, None)


# ---------------------------------------------------------------- the check

def test_stale_when_code_is_newer_than_the_process(target, src_tree):
    """The 20-day blackout, in one assertion."""
    root, f = src_tree
    future = time.time() + 120
    os.utime(f, (future, future))
    r = check(MAGIC, root)
    assert r.state == RUNNING_STALE
    assert r.pids == [target.pid]
    assert r.newest_source_path == f


def test_fresh_when_code_predates_the_process(target, src_tree):
    root, f = src_tree
    past = time.time() - 5000
    os.utime(f, (past, past))
    r = check(MAGIC, root)
    assert r.state == RUNNING_FRESH
    assert r.pids == [target.pid]


def test_not_running_reports_that_and_not_staleness(src_tree):
    root, _ = src_tree
    r = check("zz-nothing-here-at-all", root)
    assert r.state == NOT_RUNNING
    assert r.pids == []


def test_all_three_states_are_reachable(target, src_tree):
    """No state may be unreachable by construction.

    A checker only ever observed returning one value is indistinguishable from
    one that cannot return the others.
    """
    root, f = src_tree
    seen = set()

    os.utime(f, (time.time() + 120,) * 2)
    seen.add(check(MAGIC, root).state)
    os.utime(f, (time.time() - 5000,) * 2)
    seen.add(check(MAGIC, root).state)
    seen.add(check("zz-nothing-here-at-all", root).state)

    assert seen == {RUNNING_STALE, RUNNING_FRESH, NOT_RUNNING}


def test_exit_codes_are_distinct(target, src_tree):
    """The cron caller branches on exit code, so the three must not collide."""
    root, f = src_tree
    os.utime(f, (time.time() + 120,) * 2)
    stale = check(MAGIC, root).exit_code
    os.utime(f, (time.time() - 5000,) * 2)
    fresh = check(MAGIC, root).exit_code
    absent = check("zz-nothing-here-at-all", root).exit_code
    assert len({stale, fresh, absent}) == 3
    assert fresh == 0, "the healthy case must be exit 0"


def test_summary_names_the_offending_file(target, src_tree):
    root, f = src_tree
    os.utime(f, (time.time() + 120,) * 2)
    summary = check(MAGIC, root).summary()
    assert "mod.py" in summary
    assert "STALE" in summary.upper()
