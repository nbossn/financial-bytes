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


# ------------------------------------------------- the clock this host runs on
#
# `process_start_time` is `/proc/stat` btime + the process's starttime ticks.
# BOTH terms come from the monotonic clock, and on this host the monotonic
# clock runs fast, so the answer slides steadily *earlier* the longer a process
# lives. Measured 2026-07-26: three unrelated pids' reported start times each
# moved -19.00s over 341s of wall clock, perfectly linearly (-5.57%), and the
# daemon whose own `date`-stamped log says it launched at 23:00:01 reported
# 22:58:19 — 102s early at 30 minutes of age.
#
# There is no exact correction: the rate varies (4.4%-6.1% across windows), so
# no fixed factor recovers the true instant. What these tests pin instead is
# the property the freshness verdict actually depends on — the error only ever
# points one way, and that way is safe.
#
# Deliberately NOT pinned with a timing test. The skew is bursty, not
# continuous: over a 3s window monotonic/realtime measured 1.0000009 (no skew
# at all), while over 90s it measured 1.06127 — WSL resyncs realtime to the
# Windows host in steps. A pin short enough to run every time would pass or
# fail on where the window landed, and a guard that cries wolf is the failure
# mode this module already documents elsewhere. Reproduce on demand instead:
#
#   python3 -c "
#   import time
#   u=lambda: float(open('/proc/uptime').read().split()[0])
#   a=time.time()-u(); time.sleep(90); print('btime drift:', time.time()-u()-a)"
#   # -> about -5s per 85s of realtime; must be 0 for a stable start time

def test_reported_start_is_never_later_than_the_truth(target):
    """The safety invariant the whole check rests on.

    A start time reported *later* than the truth would let
    `newest_source_mtime <= started` short-circuit to FRESH for a file written
    after launch — the exact silent failure that cost 20 days. Reported
    *earlier* only costs precision. Assert the direction, not the magnitude.
    """
    ceiling = time.time()          # the process already exists (fixture waited)
    started = process_start_time(target.pid)
    assert started is not None
    assert started <= ceiling + 0.05, (
        f"reported start {started} is later than now {ceiling} — a false FRESH "
        "is reachable"
    )


def test_a_start_time_that_slid_later_would_be_caught():
    """Positive control for the invariant above.

    Without this, the assertion is satisfied by any implementation at all and
    could not distinguish a correct bound from a broken one.
    """
    ceiling = time.time()
    pretend_started = ceiling + 30.0
    assert not (pretend_started <= ceiling + 0.05)


def test_summary_does_not_claim_an_exact_start_instant(target):
    """The reported string is the thing that gets copied into BACKLOG.

    Block 6 recorded 'pid 298 started 07-24 16:06' from this output. The same
    never-restarted process reported 15:05:43 on 07-26 and reads earlier every
    minute, so that timestamp decayed into a wrong fact in a document people
    act on. The number cannot be made exact; it must stop presenting itself as
    exact.
    """
    result = check(pattern=MAGIC, source_root=Path("src"))
    text = result.summary()
    assert "started" in text
    assert "no later than" in text, (
        f"summary states a bare start instant and invites it being quoted: {text!r}"
    )


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

def _baseline(root, target):
    """A content snapshot recorded before `target` launched, in a temp manifest.

    Staleness is a claim about content diverging from what the process loaded,
    so establishing it needs a from-before-launch baseline. Tests that only
    move mtime were asserting the false positive fixed on 2026-07-26.
    """
    man = root.parent / "manifest.json"
    start = process_start_time(find_processes(MAGIC)[0])
    save_manifest(man, source_hashes(root), start - 60)
    return man


def test_stale_when_code_is_newer_than_the_process(target, src_tree):
    """The 20-day blackout, in one assertion."""
    root, f = src_tree
    man = _baseline(root, target)
    f.write_text("x = 'the fix that never shipped'\n")   # real content change
    future = time.time() + 120
    os.utime(f, (future, future))
    r = check(MAGIC, root, manifest_path=man)
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


def test_all_four_states_are_reachable(target, src_tree):
    """No state may be unreachable by construction.

    A checker only ever observed returning one value is indistinguishable from
    one that cannot return the others. UNKNOWN is included deliberately: it was
    added to stop a false STALE, and an escape hatch that swallows the real
    STALE would be worse than the bug it replaced.
    """
    root, f = src_tree
    man = _baseline(root, target)
    seen = set()

    f.write_text("x = 2\n")                              # content differs
    os.utime(f, (time.time() + 120,) * 2)
    seen.add(check(MAGIC, root, manifest_path=man).state)

    os.utime(f, (time.time() - 5000,) * 2)               # predates launch
    seen.add(check(MAGIC, root, manifest_path=man).state)

    os.utime(f, (time.time() + 120,) * 2)                # touched, no baseline
    seen.add(check(MAGIC, root, manifest_path=root.parent / "absent.json").state)

    seen.add(check("zz-nothing-here-at-all", root).state)

    assert seen == {RUNNING_STALE, RUNNING_FRESH, NOT_RUNNING, RUNNING_UNKNOWN}


def test_exit_codes_are_distinct(target, src_tree):
    """The cron caller branches on exit code, so the four must not collide."""
    root, f = src_tree
    man = _baseline(root, target)
    f.write_text("x = 2\n")
    os.utime(f, (time.time() + 120,) * 2)
    stale = check(MAGIC, root, manifest_path=man).exit_code
    unknown = check(MAGIC, root, manifest_path=root.parent / "absent.json").exit_code
    os.utime(f, (time.time() - 5000,) * 2)
    fresh = check(MAGIC, root, manifest_path=man).exit_code
    absent = check("zz-nothing-here-at-all", root).exit_code
    assert len({stale, fresh, absent, unknown}) == 4
    assert fresh == 0, "the healthy case must be exit 0"


def test_summary_names_the_offending_file(target, src_tree):
    root, f = src_tree
    man = _baseline(root, target)
    f.write_text("x = 2\n")
    os.utime(f, (time.time() + 120,) * 2)
    summary = check(MAGIC, root, manifest_path=man).summary()
    assert "mod.py" in summary
    assert "STALE" in summary.upper()


# ------------------------------------------------- content vs mtime (block 6)
#
# The first live run of this checker reported STALE for a scheduler that was
# running byte-identical code, and named its own source file as the culprit.
# Two independent causes, both of which produce a false alarm:
#
#   1. mtime is not content. Block 5's mutation testing rewrote src/scheduler.py
#      back to identical bytes; its mtime moved to "tonight" while its content
#      had not changed since 2026-06-25 — before the process even started.
#   2. An added module cannot make already-running code stale. src/ops/ was
#      created after the process launched and nothing imports it.
#
# A guard whose first live output is a false alarm is a guard that gets ignored,
# which is precisely how the 20-day blackout stayed invisible. So STALE now
# requires a *content* change to a file that already existed. mtime is kept as
# the trigger, because it is the one signal that survives a branch checkout —
# dropping it would trade a false positive for a false negative, and the false
# negative is the one that cost 20 days.

from src.ops.process_freshness import (  # noqa: E402
    RUNNING_UNKNOWN,
    changed_files,
    load_manifest,
    save_manifest,
    source_hashes,
)


def test_source_hashes_are_content_not_mtime(src_tree):
    root, f = src_tree
    before = source_hashes(root)
    os.utime(f, (time.time() + 500, time.time() + 500))   # touch, do not edit
    assert source_hashes(root) == before, "hash must not move when only mtime does"


def test_source_hashes_change_when_content_changes(src_tree):
    root, f = src_tree
    before = source_hashes(root)
    f.write_text("x = 2\n")
    assert source_hashes(root) != before


def test_source_hashes_skip_pycache(src_tree):
    root, _ = src_tree
    cache = root / "pkg" / "__pycache__"
    cache.mkdir()
    (cache / "mod.cpython-312.pyc").write_bytes(b"\x00\x01")
    assert all("__pycache__" not in k for k in source_hashes(root))


def test_changed_files_reports_modifications(src_tree):
    base = {"a.py": "h1", "b.py": "h2"}
    assert changed_files(base, {"a.py": "h1", "b.py": "CHANGED"}) == ["b.py"]


def test_changed_files_ignores_additions(src_tree):
    """A module that did not exist at launch cannot be code the process imported."""
    base = {"a.py": "h1"}
    assert changed_files(base, {"a.py": "h1", "new.py": "h9"}) == []


def test_changed_files_reports_deletions(src_tree):
    """A deleted module IS a divergence — the process may still hold it."""
    assert changed_files({"a.py": "h1", "b.py": "h2"}, {"a.py": "h1"}) == ["b.py"]


def test_manifest_roundtrips(tmp_path):
    p = tmp_path / "m.json"
    save_manifest(p, {"a.py": "h1"}, 1000.0)
    at, hashes = load_manifest(p)
    assert at == 1000.0 and hashes == {"a.py": "h1"}


def test_manifest_keeps_history_and_picks_newest_before(tmp_path):
    """Choosing the newest snapshot at-or-before the process start needs history."""
    p = tmp_path / "m.json"
    save_manifest(p, {"a.py": "old"}, 100.0)
    save_manifest(p, {"a.py": "mid"}, 200.0)
    save_manifest(p, {"a.py": "new"}, 300.0)
    assert load_manifest(p, before=250.0) == (200.0, {"a.py": "mid"})
    assert load_manifest(p, before=50.0) == (None, None)   # nothing old enough


def test_missing_manifest_is_none_not_empty(tmp_path):
    """An empty dict would read as 'nothing changed' — a fail-open. Must be None."""
    assert load_manifest(tmp_path / "absent.json") == (None, None)


def test_corrupt_manifest_is_none_not_empty(tmp_path):
    p = tmp_path / "m.json"
    p.write_text("{not json")
    assert load_manifest(p) == (None, None)


# ------------------------------------------------------------ check() verdicts

def test_touched_but_unchanged_is_fresh_not_stale(target, src_tree):
    """The exact false positive observed live on 2026-07-26."""
    root, f = src_tree
    man = root.parent / "m.json"
    start = process_start_time(find_processes(MAGIC)[0])
    save_manifest(man, source_hashes(root), start - 60)    # baseline predates launch
    os.utime(f, (time.time() + 500, time.time() + 500))    # mtime moves, content does not
    r = check(MAGIC, root, manifest_path=man)
    assert r.state == RUNNING_FRESH, r.summary()


def test_real_content_change_is_still_stale(target, src_tree):
    """The false positive fix must not cost the true positive."""
    root, f = src_tree
    man = root.parent / "m.json"
    start = process_start_time(find_processes(MAGIC)[0])
    save_manifest(man, source_hashes(root), start - 60)
    f.write_text("x = 999\n")
    os.utime(f, (time.time() + 500, time.time() + 500))
    r = check(MAGIC, root, manifest_path=man)
    assert r.state == RUNNING_STALE, r.summary()
    assert "mod.py" in r.summary()


def test_added_module_alone_is_not_stale(target, src_tree):
    root, _ = src_tree
    man = root.parent / "m.json"
    start = process_start_time(find_processes(MAGIC)[0])
    save_manifest(man, source_hashes(root), start - 60)
    new = root / "pkg" / "brand_new.py"
    new.write_text("y = 1\n")
    os.utime(new, (time.time() + 500, time.time() + 500))
    r = check(MAGIC, root, manifest_path=man)
    assert r.state == RUNNING_FRESH, r.summary()


def test_no_usable_baseline_is_unknown_not_stale(target, src_tree):
    """No snapshot from before launch => we cannot know. Say so; do not guess."""
    root, f = src_tree
    man = root.parent / "m.json"
    start = process_start_time(find_processes(MAGIC)[0])
    save_manifest(man, source_hashes(root), start + 60)    # baseline is TOO NEW
    f.write_text("x = 3\n")
    os.utime(f, (time.time() + 500, time.time() + 500))
    r = check(MAGIC, root, manifest_path=man)
    assert r.state == RUNNING_UNKNOWN, r.summary()
    assert "mod.py" in r.summary(), "UNKNOWN must still name what moved"


def test_nothing_touched_since_launch_needs_no_manifest(target, src_tree):
    """The common healthy case must not degrade to UNKNOWN."""
    root, f = src_tree
    os.utime(f, (time.time() - 99999, time.time() - 99999))
    r = check(MAGIC, root, manifest_path=root.parent / "absent.json")
    assert r.state == RUNNING_FRESH, r.summary()


def test_unknown_has_its_own_exit_code(target, src_tree):
    root, f = src_tree
    man = root.parent / "m.json"
    start = process_start_time(find_processes(MAGIC)[0])
    save_manifest(man, source_hashes(root), start + 60)
    f.write_text("x = 4\n")
    os.utime(f, (time.time() + 500, time.time() + 500))
    codes = {check(MAGIC, root, manifest_path=man).exit_code}
    assert codes == {3}, "UNKNOWN must be distinguishable from fresh(0)/down(1)/stale(2)"


def test_the_checker_does_not_report_its_own_source(target, tmp_path):
    """A guard that names itself as the fault is shape-matching, not detection.

    Positive control included: the same scan must still see a sibling file, or
    'found nothing' would be indistinguishable from 'excluded everything'.
    """
    root = tmp_path / "src"
    (root / "ops").mkdir(parents=True)
    me = root / "ops" / "process_freshness.py"
    me.write_text("# stand-in for this module\n")
    sibling = root / "ops" / "other.py"
    sibling.write_text("z = 1\n")
    hashes = source_hashes(root, exclude={"ops/process_freshness.py"})
    assert "ops/other.py" in hashes, "positive control: scan must still find siblings"
    assert "ops/process_freshness.py" not in hashes


def test_check_itself_excludes_this_module(target, tmp_path):
    """Closing a gap: the exclusion test above only exercised source_hashes().

    Asserting a helper honours `exclude` says nothing about whether check()
    passes it — that is a test of a mechanism the caller may never read. This
    drives it through the real entry point, with a positive control so that
    "reported FRESH" cannot be confused with "scanned nothing".
    """
    root = tmp_path / "src"
    (root / "ops").mkdir(parents=True)
    me = root / "ops" / "process_freshness.py"
    me.write_text("# stand-in\n")
    sibling = root / "ops" / "other.py"
    sibling.write_text("z = 1\n")

    man = root.parent / "manifest.json"
    start = process_start_time(find_processes(MAGIC)[0])
    save_manifest(man, source_hashes(root, exclude={"ops/process_freshness.py"}), start - 60)

    future = (time.time() + 300,) * 2
    me.write_text("# stand-in, edited\n")          # only the checker changed
    os.utime(me, future)
    assert check(MAGIC, root, manifest_path=man).state == RUNNING_FRESH

    sibling.write_text("z = 2\n")                  # positive control: a real one
    os.utime(sibling, future)
    r = check(MAGIC, root, manifest_path=man)
    assert r.state == RUNNING_STALE, "control: a non-self change must still be caught"
    assert "ops/other.py" in r.changed and "ops/process_freshness.py" not in r.changed


def test_a_legacy_manifest_containing_this_module_is_still_ignored(target, tmp_path):
    """The case that makes the self-exclusion load-bearing.

    A mutation removing `exclude={SELF_RELPATH}` from the scan left all 35
    tests green, because check() writes the manifest already-excluded so the
    key is never in the baseline. A hand-written or older manifest can carry
    it — this builds exactly that and asserts an edit to the guard does not
    indict the process it guards. Positive control at the end.
    """
    root = tmp_path / "src"
    (root / "ops").mkdir(parents=True)
    me = root / "ops" / "process_freshness.py"
    me.write_text("# v1\n")
    sibling = root / "ops" / "other.py"
    sibling.write_text("z = 1\n")

    man = root.parent / "manifest.json"
    start = process_start_time(find_processes(MAGIC)[0])
    save_manifest(man, source_hashes(root), start - 60)   # NOT excluded — legacy shape
    assert "ops/process_freshness.py" in load_manifest(man, before=start)[1], (
        "control: the baseline must really contain the key under test"
    )

    future = (time.time() + 300,) * 2
    me.write_text("# v2 — the guard itself was edited\n")
    os.utime(me, future)
    assert check(MAGIC, root, manifest_path=man).state == RUNNING_FRESH

    sibling.write_text("z = 2\n")                         # control: a real change
    os.utime(sibling, future)
    assert check(MAGIC, root, manifest_path=man).state == RUNNING_STALE


def test_unknown_report_does_not_name_this_module(target, tmp_path):
    """The cosmetic half of the original false alarm.

    The live 2026-07-26 run said "STALE ... process_freshness.py changed" —
    the guard naming itself. Even in UNKNOWN, where no verdict is claimed, the
    file list must not point at the guard. Positive control: a sibling that was
    genuinely touched still has to appear, or an empty list would pass.
    """
    root = tmp_path / "src"
    (root / "ops").mkdir(parents=True)
    me = root / "ops" / "process_freshness.py"
    me.write_text("# v1\n")
    sibling = root / "ops" / "other.py"
    sibling.write_text("z = 1\n")

    future = (time.time() + 300,) * 2
    os.utime(me, future)
    os.utime(sibling, future)

    r = check(MAGIC, root, manifest_path=root.parent / "absent.json")
    assert r.state == RUNNING_UNKNOWN
    assert "ops/other.py" in r.touched, "control: a real touched file must be listed"
    assert "ops/process_freshness.py" not in r.touched
    assert "process_freshness.py" not in r.summary()


# ------------------------------------------------------------------ recording

def test_check_records_a_snapshot_when_asked(target, src_tree):
    root, _ = src_tree
    man = root.parent / "m.json"
    assert load_manifest(man) == (None, None)          # control: nothing there yet
    check(MAGIC, root, manifest_path=man, record=True)
    at, hashes = load_manifest(man)
    assert at is not None and "pkg/mod.py" in hashes


def test_check_does_not_record_by_default(target, src_tree):
    root, _ = src_tree
    man = root.parent / "m.json"
    check(MAGIC, root, manifest_path=man)
    assert load_manifest(man) == (None, None)


def test_cli_records_by_default(target, src_tree, capsys):
    """Without this the mechanism can never converge: no recorded snapshot ever
    predates a process start, so every answer is UNKNOWN forever."""
    from src.ops.process_freshness import main
    root, _ = src_tree
    man = root.parent / "m.json"
    main(["--pattern", MAGIC, "--source-root", str(root), "--manifest", str(man)])
    assert load_manifest(man)[1] is not None, "the CLI must record by default"


def test_cli_no_record_flag_suppresses_it(target, src_tree, capsys):
    from src.ops.process_freshness import main
    root, _ = src_tree
    man = root.parent / "m.json"
    main(["--pattern", MAGIC, "--source-root", str(root),
          "--manifest", str(man), "--no-record"])
    assert load_manifest(man) == (None, None)


def test_recorded_snapshot_makes_the_next_check_exact(target, src_tree):
    """End-to-end: record, restart-equivalent, edit, get a real verdict.

    Simulates convergence by recording a snapshot and backdating it to before
    the process start — the state a daily cron run reaches on its own after one
    restart cycle.
    """
    root, f = src_tree
    man = root.parent / "m.json"
    check(MAGIC, root, manifest_path=man, record=True)
    start = process_start_time(find_processes(MAGIC)[0])
    save_manifest(man, source_hashes(root), start - 60)

    assert check(MAGIC, root, manifest_path=man).state == RUNNING_FRESH
    f.write_text("x = 'changed'\n")
    os.utime(f, (time.time() + 300,) * 2)
    assert check(MAGIC, root, manifest_path=man).state == RUNNING_STALE


# ------------------------------------------------------------------- scope
#
# The check compares a process against the code on disk. Which code? It scanned
# every .py under src/. But a process only goes stale against modules it
# actually imported, and this repo has TWO consumers of src/ with opposite
# freshness properties:
#
#   * the scheduler — one long-running process (pid 298 at the time of
#     writing), started once, imports a module once. This is the one that can
#     go stale, and did, for 20 days.
#   * the nightly stock picker — `scripts/stockpicker-nightly.sh`, four
#     `python -m src.stockpicker.*` invocations under a 23:30 cron entry. A
#     fresh interpreter every run, so it re-imports everything and CANNOT be
#     stale.
#
# Scanning the whole tree charges the first consumer for edits to the second.
# Measured 2026-07-27 against the live tree: 85 .py under src/, 68 reachable
# from the console-script entry point, 17 not — 13 of them src/stockpicker/.
# Of the 7 files committed since pid 298 launched, only 2 are in reach, and
# the tool's live output named `stockpicker/accuracy.py` and
# `stockpicker/engine.py` as evidence the scheduler might be stale.
#
# That is the false-positive direction, which this module's own docstring
# calls the one that kills a guard — and it is loudest exactly where it means
# least, because stockpicker/ is the most actively edited part of the repo.

import ast
import textwrap

from src.ops.process_freshness import (
    DEFAULT_ENTRYPOINT,
    RUNNING_UNKNOWN,
    import_closure,
    source_hashes,
)

REPO_SRC = Path(__file__).resolve().parents[1] / "src"


def _all_py(root: Path) -> set[str]:
    return {
        p.relative_to(root).as_posix()
        for p in root.rglob("*.py")
        if "__pycache__" not in p.parts
    }


def test_closure_of_the_real_tree_is_a_proper_nonempty_subset():
    """Positive control, both totals asserted.

    A closure function that returned the empty set would silently scope the
    check to nothing — every process fresh, forever, which is the exact
    fail-open that cost 20 days. A closure that returned *everything* would
    be the bug this section exists to fix. Assert it is strictly between.
    """
    closure = import_closure(REPO_SRC, DEFAULT_ENTRYPOINT)
    total = _all_py(REPO_SRC)
    assert closure, "empty closure would make every process read FRESH"
    assert closure < total, "closure must exclude something, or it fixes nothing"
    assert len(closure) > len(total) // 2, "implausibly small closure"


def test_default_entrypoint_is_the_console_script_not_the_scheduler():
    """The entry point is `src.cli:cli`, NOT `src/scheduler.py`.

    Rooting the closure at scheduler.py looks obviously right — the process is
    `financial-bytes schedule` and the module is named scheduler — and it is
    wrong. `.venv/bin/financial-bytes` is generated from pyproject's
    [tool.poetry.scripts] and does `from src.cli import cli`. Measured: a
    scheduler.py-rooted closure is 52 modules and MISSES 16 the process really
    imports, among them every src/alerts/* module. Excluding a module the
    process does import makes it read FRESH while serving old code — the
    20-day failure, reintroduced by a guard.

    So this pins the default to the declared entry point. If someone renames
    it, this fails instead of the check silently narrowing.
    """
    pyproject = (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text()
    declared = [
        line.split("=", 1)[1].strip().strip('"').strip("'")
        for line in pyproject.splitlines()
        if line.strip().startswith("financial-bytes")
        and "=" in line
        and ":" in line.split("=", 1)[1]
    ]
    assert declared, "could not find the console-script declaration"
    module = declared[0].split(":", 1)[0]           # "src.cli"
    expected = module.split(".", 1)[1].replace(".", "/") + ".py"
    assert DEFAULT_ENTRYPOINT == expected


def test_real_closure_keeps_a_lazy_import_and_drops_the_other_consumer():
    """The live claim, both directions.

    `portfolio/earnings_calendar.py` is imported *inside* a function body
    (scheduler._run_premarket_earnings_check), days after launch — it must
    still count, because once that job runs the module is loaded and frozen
    like any other. `stockpicker/*` is reachable from no entry point the
    scheduler process has, and runs under its own cron entry in a fresh
    interpreter, so it must not.
    """
    closure = import_closure(REPO_SRC, DEFAULT_ENTRYPOINT)
    assert "portfolio/earnings_calendar.py" in closure
    stockpicker = {p for p in _all_py(REPO_SRC) if p.startswith("stockpicker/")}
    assert len(stockpicker) >= 10, "control: stockpicker/ must be non-trivial"
    assert not (stockpicker & closure)


@pytest.fixture
def closure_tree(tmp_path):
    """root, plus an entrypoint reaching one lazy and one transitive module."""
    root = tmp_path / "src"
    (root / "pkg").mkdir(parents=True)
    (root / "__init__.py").write_text("")
    (root / "pkg" / "__init__.py").write_text("")
    (root / "entry.py").write_text(
        textwrap.dedent(
            """
            from src.pkg.direct import thing

            def job():
                from src.pkg.lazy import other
                return other
            """
        )
    )
    (root / "pkg" / "direct.py").write_text("from src.pkg.deep import d\nthing = 1\n")
    (root / "pkg" / "deep.py").write_text("d = 1\n")
    (root / "pkg" / "lazy.py").write_text("other = 1\n")
    (root / "pkg" / "orphan.py").write_text("nobody_imports_me = 1\n")
    return root


def test_closure_follows_function_level_imports(closure_tree):
    """A module imported inside a function body still gets imported, once."""
    closure = import_closure(closure_tree, "entry.py")
    assert "pkg/lazy.py" in closure


def test_closure_is_transitive(closure_tree):
    closure = import_closure(closure_tree, "entry.py")
    assert "pkg/deep.py" in closure


def test_closure_excludes_an_unreachable_module(closure_tree):
    closure = import_closure(closure_tree, "entry.py")
    assert "pkg/orphan.py" not in closure
    assert "pkg/direct.py" in closure          # control: it does find things


def test_missing_entrypoint_falls_back_to_the_whole_tree(closure_tree):
    """Never an empty scope.

    A typo'd or renamed entry point must degrade to the old, noisy,
    whole-tree behaviour — over-reporting. Returning an empty closure would
    under-report, and under-reporting here is the 20-day blackout.
    """
    assert import_closure(closure_tree, "does_not_exist.py") is None


def test_unparseable_module_in_the_closure_raises(closure_tree):
    """A silently-shrunk closure is indistinguishable from a correct one.

    If a file in the closure fails to parse, everything downstream of it drops
    out of scope and the check quietly stops watching those modules. That must
    be loud.
    """
    (closure_tree / "pkg" / "direct.py").write_text("def (((\n")
    with pytest.raises(SyntaxError):
        import_closure(closure_tree, "entry.py")


def _scoped_baseline(root, manifest, entrypoint=None):
    """A snapshot recorded before the probe process launched, as check() needs."""
    start = process_start_time(find_processes(MAGIC)[0])
    save_manifest(manifest, source_hashes(root), start - 60)


def _touch_future(path):
    stamp = time.time() + 500
    os.utime(path, (stamp, stamp))


def _touch_past(path):
    stamp = time.time() - 5000
    os.utime(path, (stamp, stamp))


def test_out_of_scope_change_is_not_stale(target, closure_tree):
    """The fix, stated as behaviour: editing a module the process cannot
    import is not staleness."""
    manifest = closure_tree.parent / "m.json"
    _scoped_baseline(closure_tree, manifest, entrypoint="entry.py")
    orphan = closure_tree / "pkg" / "orphan.py"
    orphan.write_text("nobody_imports_me = 2\n")
    _touch_future(orphan)
    r = check(MAGIC, closure_tree, manifest, entrypoint="entry.py")
    assert r.state == RUNNING_FRESH, r.summary()


def test_in_scope_change_is_still_stale(target, closure_tree):
    """...and the fix does not blind the check to a real one.

    `pkg/lazy.py` is imported inside a function body, which is the case most
    at risk of being scoped away by accident.
    """
    manifest = closure_tree.parent / "m.json"
    _scoped_baseline(closure_tree, manifest, entrypoint="entry.py")
    lazy = closure_tree / "pkg" / "lazy.py"
    lazy.write_text("other = 2\n")
    _touch_future(lazy)
    r = check(MAGIC, closure_tree, manifest, entrypoint="entry.py")
    assert r.state == RUNNING_STALE, r.summary()
    assert "pkg/lazy.py" in r.changed


def test_a_legacy_manifest_with_out_of_scope_keys_is_not_stale(target, closure_tree):
    """Scope BOTH sides, exactly as SELF_RELPATH already has to.

    A manifest written before scoping existed carries keys the scoped scan no
    longer produces. `changed_files` walks the baseline and treats a key
    missing from `current` as a deletion — so filtering only the scan would
    turn every pre-existing manifest into a permanent STALE. A fix that makes
    the guard cry wolf on every legacy install is worse than the noise it set
    out to remove.
    """
    manifest = closure_tree.parent / "m.json"
    start = process_start_time(find_processes(MAGIC)[0])
    legacy = source_hashes(closure_tree)                    # unscoped, all files
    assert "pkg/orphan.py" in legacy, "control: legacy manifests do carry these"
    save_manifest(manifest, legacy, start - 60)
    orphan = closure_tree / "pkg" / "orphan.py"
    orphan.write_text("nobody_imports_me = 3\n")
    _touch_future(orphan)
    r = check(MAGIC, closure_tree, manifest, entrypoint="entry.py")
    assert r.state == RUNNING_FRESH, r.summary()


def test_summary_states_the_scope_it_measured(target, closure_tree):
    """'Cannot scope' must be a stated answer, not an invisible default."""
    manifest = closure_tree.parent / "m.json"
    scoped = check(MAGIC, closure_tree, manifest, entrypoint="entry.py")
    assert "entry.py" in scoped.summary()
    unscoped = check(MAGIC, closure_tree, manifest, entrypoint="nope.py")
    assert "whole tree" in unscoped.summary()


def test_only_out_of_scope_edits_need_no_baseline_at_all(target, closure_tree):
    """Scoping the mtime trigger is load-bearing, not tidiness.

    mtime is the gate: if nothing in reach was written since launch the check
    short-circuits to FRESH without needing a snapshot. Unscoped, a single
    stockpicker edit drags the scheduler into UNKNOWN — a state that reads as
    "we might have a problem" and, with no manifest, never resolves. This is
    the difference between an answer and a shrug.
    """
    manifest = closure_tree.parent / "absent.json"
    for name in ("entry.py", "pkg/lazy.py", "pkg/direct.py", "pkg/deep.py"):
        _touch_past(closure_tree / name)
    _touch_past(closure_tree / "__init__.py")
    _touch_past(closure_tree / "pkg" / "__init__.py")
    _touch_future(closure_tree / "pkg" / "orphan.py")

    r = check(MAGIC, closure_tree, manifest, entrypoint="entry.py")
    assert r.state == RUNNING_FRESH, r.summary()
    # Control: the same tree, unscoped, must NOT be able to reach that answer,
    # or this proves nothing about the scoping.
    assert check(MAGIC, closure_tree, manifest, entrypoint="nope.py").state == (
        RUNNING_UNKNOWN
    )


def test_touched_list_is_scoped_too(target, closure_tree):
    """UNKNOWN names the files it saw; it must not name unreachable ones."""
    manifest = closure_tree.parent / "none.json"
    time.sleep(0.02)
    (closure_tree / "pkg" / "orphan.py").write_text("nobody_imports_me = 4\n")
    (closure_tree / "pkg" / "lazy.py").write_text("other = 5\n")
    r = check(MAGIC, closure_tree, manifest, entrypoint="entry.py")
    assert r.state == RUNNING_UNKNOWN
    assert "pkg/lazy.py" in r.touched
    assert "pkg/orphan.py" not in r.touched
