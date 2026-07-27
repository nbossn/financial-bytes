"""Is the scheduler running, and is it running the code that's on disk?

Motivated by a measured 20-day outage. From 2026-06-25 to 2026-07-14 the daily
newsletter was generated every morning and delivered zero times: the group-send
crashed on `re.error: bad escape \\u`. The fix was committed 2026-06-25
(92c0e6b) — the same day the blackout started — and made no difference, because
Python imports a module once. The scheduler process had started 2026-06-23 and
ran unrestarted until 07-14, serving 20-day-old code the entire time.

Two things follow, and this module exists for both:

**Liveness is not freshness.** The process was alive on all 20 days. `pgrep`
would have reported success every time. The question worth asking is not "is it
up?" but "is what's up older than the code it imported?"

**A guard that cannot fail is not a guard.** The 5:50 AM crontab watchdog is
`pgrep -f 'financial-bytes schedule' || (restart)`. cron runs that under
`sh -c`, so the shell's own argv contains the pattern and `pgrep -f` matches
*itself* — verified with a pattern matching no real process, which still exits
0. It has never been able to report the scheduler down. So discovery here reads
/proc directly and excludes this process and its ancestors.

CLI:
    python -m src.ops.process_freshness            # exit 0 fresh / 1 down / 2 stale
"""
from __future__ import annotations

import ast
import hashlib
import json
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

# What the scheduler's command line looks like (see the @reboot crontab entry).
DEFAULT_PATTERN = "financial-bytes schedule"
DEFAULT_SOURCE_ROOT = Path(__file__).resolve().parents[2] / "src"
DEFAULT_MANIFEST = Path(__file__).resolve().parents[2] / "var" / "freshness-manifest.json"

# This module's own path relative to the source root. A guard that reports
# itself as the fault is shape-matching, not detection — see the 2026-07-26
# false alarm in the module docstring.
SELF_RELPATH = "ops/process_freshness.py"

# Where the scheduler process's import graph starts. This is `src/cli.py`, NOT
# `src/scheduler.py`, and the difference is the whole reason this constant is
# spelled out rather than inferred: the process argv reads
# `.../bin/financial-bytes schedule`, but that console script is generated from
# pyproject's [tool.poetry.scripts] and its body is `from src.cli import cli`.
#
# Measured 2026-07-27: a closure rooted at scheduler.py is 52 modules and omits
# 16 the process genuinely imports, including every src/alerts/* module.
# Omitting an imported module makes it read FRESH while the process serves old
# code — precisely the 20-day blackout above, reintroduced by the guard meant
# to catch it. A test pins this to the declaration in pyproject.toml so a
# rename fails loudly instead of quietly narrowing the scope.
DEFAULT_ENTRYPOINT = "cli.py"

# How many snapshots to retain. Enough to survive a long-lived process; small
# enough that the file stays human-readable.
MANIFEST_HISTORY = 20

NOT_RUNNING = "not_running"
RUNNING_STALE = "running_stale"
RUNNING_FRESH = "running_fresh"
RUNNING_UNKNOWN = "running_unknown"

# Distinct so a cron caller can branch on them. 0 is the healthy case.
# 3 is deliberately NOT folded into 0 or 2: "cannot tell" is its own answer.
EXIT_CODES = {RUNNING_FRESH: 0, NOT_RUNNING: 1, RUNNING_STALE: 2, RUNNING_UNKNOWN: 3}

_PROC = Path("/proc")


def _cmdline(pid: int) -> str | None:
    """Full argv of `pid`, NUL-separated on disk, joined with spaces."""
    try:
        raw = (_PROC / str(pid) / "cmdline").read_bytes()
    except (OSError, ValueError):
        return None
    if not raw:
        return None                      # kernel thread
    return raw.replace(b"\x00", b" ").decode("utf-8", "replace").strip()


def _ppid(pid: int) -> int | None:
    try:
        stat = (_PROC / str(pid) / "stat").read_text()
    except OSError:
        return None
    # comm (field 2) may contain spaces and parens, so split after the LAST ')'.
    try:
        fields = stat[stat.rindex(")") + 1:].split()
        return int(fields[1])            # field 4 = ppid, index 1 after the ')'
    except (ValueError, IndexError):
        return None


def _ancestors(pid: int) -> set[int]:
    """`pid` and every process above it, so a wrapper shell can't self-match."""
    chain, seen = {pid}, pid
    while True:
        parent = _ppid(seen)
        if parent is None or parent in chain or parent <= 0:
            break
        chain.add(parent)
        seen = parent
    return chain


def find_processes(pattern: str, exclude_pids: set[int] | None = None) -> list[int]:
    """PIDs whose argv contains `pattern`, excluding this process and its ancestors.

    The exclusion is the whole point: `pgrep -f` does not do it, which is why
    the crontab watchdog has never fired.
    """
    excluded = set(exclude_pids or ()) | _ancestors(os.getpid())
    found = []
    for entry in _PROC.iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        if pid in excluded:
            continue
        cmd = _cmdline(pid)
        if cmd and pattern in cmd:
            found.append(pid)
    return sorted(found)


def _boot_time() -> float | None:
    try:
        for line in (_PROC / "stat").read_text().splitlines():
            if line.startswith("btime "):
                return float(line.split()[1])
    except OSError:
        pass
    return None


def process_start_time(pid: int) -> float | None:
    """A LOWER BOUND on the epoch seconds at which `pid` started — never later
    than the truth, and on this host substantially earlier. None if it's gone.

    Derived as /proc/stat btime + the process's starttime ticks. Both terms
    come from the monotonic clock, which on this host runs ~5-6% fast against
    a realtime clock that WSL keeps resynced to the Windows host. So btime
    (= realtime now - uptime) slides steadily *earlier*, and with it every
    start time derived from it. The error is proportional to the process's age
    and unbounded: at 20 days — precisely the blackout this module was built
    for — it is over half a day.

    Measured 2026-07-26, not inferred:

      * three unrelated pids' reported start times each moved -19.00s over
        341s of wall clock, linearly and identically (-5.57%);
      * the overnight daemon, whose own `date`-stamped log records its launch
        at 23:00:01 and which cron fires on the minute, reported 22:58:19 —
        102s early at 30 minutes of age;
      * over a 90s sleep, CLOCK_MONOTONIC advanced 90.00s while CLOCK_REALTIME
        advanced 84.80s, and btime moved -5.00s.

    An earlier version of this docstring called that "a second or two under
    NTP ... noise against the hours-to-days staleness this check reports". It
    is not noise; it grows without bound. It was spotted then (successive
    calls 3s apart) and mis-attributed.

    Not corrected, because it cannot be: the rate varies with where the resync
    steps land (4.4%-6.1% across measured windows), so no fixed factor recovers
    the true instant, and /proc exposes no realtime start. `/proc/<pid>`'s
    inode mtime is NOT an alternative — it is stamped at first lookup, not at
    creation (verified: stat a process 6s after spawning it and the inode reads
    exactly 6s late).

    What survives is the direction, which is the half the verdict rests on. An
    under-estimate can only make `newest_source_mtime <= started` fail, sending
    the check on to the content comparison; it can never short-circuit to
    FRESH. Over-estimating would reintroduce the silent failure that cost 20
    days. Callers may rely on the bound; they must not quote the instant.
    """
    try:
        stat = (_PROC / str(pid) / "stat").read_text()
    except OSError:
        return None
    boot = _boot_time()
    if boot is None:
        return None
    try:
        fields = stat[stat.rindex(")") + 1:].split()
        starttime_ticks = int(fields[19])    # field 22 = starttime
    except (ValueError, IndexError):
        return None
    return boot + starttime_ticks / os.sysconf("SC_CLK_TCK")


def import_closure(root: Path, entrypoint: str | None = None) -> set[str] | None:
    """Every module under `root` reachable from `entrypoint`, or None.

    Returns relative posix paths. None means the entry point does not exist —
    the caller must then fall back to scanning the whole tree. It must NOT
    fall back to an empty scope: an empty scope reports every process fresh
    forever, which is the fail-open this module was written to end.

    Why a static AST walk and not `sys.modules`: the thing being judged is a
    *different* process, and it imports almost nothing at launch. The
    scheduler's launch-time closure inside this repo is two files; every job
    module is imported inside a function body at first call, hours or days
    later. So "what has it imported" is unanswerable from outside, while "what
    could it ever import" is exact and errs toward including too much — the
    conservative direction, because an over-broad scope can only over-report.

    Imports at any nesting depth count. A module imported inside a function is
    still imported once and cached for the life of the process, so it goes
    stale exactly like a top-level one.
    """
    root = Path(root)
    entry = root / (entrypoint or DEFAULT_ENTRYPOINT)
    if not entry.is_file():
        return None
    package = root.name

    def resolve(dotted: str) -> Path | None:
        parts = dotted.split(".")
        if not parts or parts[0] != package:
            return None                  # third-party or stdlib; not our code
        rest = parts[1:]
        if rest:
            module = root.joinpath(*rest).with_suffix(".py")
            if module.is_file():
                return module
        init = root.joinpath(*rest, "__init__.py")
        return init if init.is_file() else None

    def dotted_names(node: ast.AST, path: Path) -> list[str]:
        if isinstance(node, ast.Import):
            return [alias.name for alias in node.names]
        if not isinstance(node, ast.ImportFrom):
            return []
        base = [package, *Path(path).relative_to(root).parts[:-1]]
        if node.level:                   # `from . import x` / `from ..pkg import y`
            keep = len(base) - (node.level - 1)
            base = base[:keep] if keep > 0 else base[:1]
        elif node.module:
            base = []
        prefix = ".".join([*base, node.module] if node.module else base)
        if not prefix:
            return []
        # Both forms: `from a.b import c` may name a module OR an attribute.
        return [prefix, *(f"{prefix}.{alias.name}" for alias in node.names)]

    seen: set[Path] = set()
    queue: list[Path] = [entry]
    package_init = root / "__init__.py"
    if package_init.is_file():
        queue.append(package_init)       # importing src.anything runs src/__init__

    while queue:
        path = queue.pop()
        if path in seen:
            continue
        seen.add(path)
        # Deliberately NOT caught. A file that fails to parse would drop itself
        # and everything downstream of it out of scope, and a silently smaller
        # closure is indistinguishable from a correct one — it just stops
        # watching. Loud beats narrow.
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            for dotted in dotted_names(node, path):
                parts = dotted.split(".")
                # Importing a.b.c also imports a and a.b.
                for depth in range(1, len(parts) + 1):
                    found = resolve(".".join(parts[:depth]))
                    if found is not None and found not in seen:
                        queue.append(found)

    return {p.relative_to(root).as_posix() for p in seen}


def newest_source_mtime(
    root: Path, include: set[str] | None = None
) -> tuple[float | None, Path | None]:
    """(mtime, path) of the most recently modified .py under `root`.

    Only .py counts: a .pyc is a *product* of an import, so including it would
    make the check compare the process against its own bytecode cache.
    """
    newest_m: float | None = None
    newest_p: Path | None = None
    for path in Path(root).rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        if include is not None and path.relative_to(root).as_posix() not in include:
            continue
        try:
            m = path.stat().st_mtime
        except OSError:
            continue
        if newest_m is None or m > newest_m:
            newest_m, newest_p = m, path
    return newest_m, newest_p


def source_hashes(root: Path, exclude: set[str] | None = None) -> dict[str, str]:
    """{relative posix path: sha256} for every .py under `root`.

    Content, not mtime. The two diverge constantly in practice: a mutation-test
    run, a `git checkout`, or a formatter rewriting a file to identical bytes
    all move mtime while leaving the code the process imported unchanged.
    """
    skip = set(exclude or ())
    out: dict[str, str] = {}
    for path in sorted(Path(root).rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        rel = path.relative_to(root).as_posix()
        if rel in skip:
            continue
        try:
            out[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            continue
    return out


def changed_files(baseline: dict[str, str], current: dict[str, str]) -> list[str]:
    """Files whose content diverged from `baseline`. Additions are NOT changes.

    A module that did not exist when the process launched cannot be code the
    process imported and then went stale against — at worst a deferred import
    picks up the *new* file, which is fresh by definition. Deletions do count:
    the process may still hold a module that is no longer on disk.
    """
    return sorted(
        rel for rel, digest in baseline.items() if current.get(rel) != digest
    )


def save_manifest(path: Path, hashes: dict[str, str], recorded_at: float) -> None:
    """Append a snapshot, keeping the most recent MANIFEST_HISTORY entries."""
    path = Path(path)
    try:
        existing = json.loads(path.read_text()).get("snapshots", [])
    except (OSError, ValueError, AttributeError):
        existing = []
    existing.append({"recorded_at": recorded_at, "hashes": hashes})
    existing.sort(key=lambda s: s.get("recorded_at", 0.0))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"snapshots": existing[-MANIFEST_HISTORY:]}, indent=2, sort_keys=True)
    )


def load_manifest(
    path: Path, before: float | None = None
) -> tuple[float | None, dict[str, str] | None]:
    """Newest snapshot recorded at-or-before `before`, else (None, None).

    Returns None rather than {} when there is nothing usable. An empty dict
    would compare equal to everything and read as "nothing changed" — a
    fail-open in a module that exists because a fail-open cost 20 days.
    """
    try:
        snapshots = json.loads(Path(path).read_text())["snapshots"]
    except (OSError, ValueError, KeyError, TypeError):
        return None, None
    usable = [
        s for s in snapshots
        if before is None or s.get("recorded_at", float("inf")) <= before
    ]
    if not usable:
        return None, None
    newest = max(usable, key=lambda s: s.get("recorded_at", 0.0))
    return newest.get("recorded_at"), newest.get("hashes")


@dataclass
class FreshnessResult:
    state: str
    pids: list[int] = field(default_factory=list)
    started_at: float | None = None
    newest_source_mtime: float | None = None
    newest_source_path: Path | None = None
    changed: list[str] = field(default_factory=list)
    touched: list[str] = field(default_factory=list)
    scope: str = ""

    @property
    def exit_code(self) -> int:
        return EXIT_CODES[self.state]

    @property
    def stale_by_seconds(self) -> float | None:
        if self.started_at is None or self.newest_source_mtime is None:
            return None
        return self.newest_source_mtime - self.started_at

    def summary(self) -> str:
        if self.state == NOT_RUNNING:
            return "DOWN — no scheduler process found. Restart it."
        # "no later than", not a bare instant: this string is what gets quoted.
        # Block 6 copied "pid 298 started 07-24 16:06" out of here into BACKLOG;
        # the same never-restarted process read 15:05:43 two days later and
        # reads earlier every minute. See process_start_time — the value is a
        # lower bound, so the wording has to say so or it decays into a wrong
        # fact in a document someone acts on.
        started = "no later than " + datetime.fromtimestamp(
            self.started_at
        ).isoformat(timespec="seconds")
        scope = f" [{self.scope}]" if self.scope else ""
        if self.state == RUNNING_FRESH:
            return (
                f"OK — pid {self.pids[0]} started {started}; no imported source "
                f"has changed content since.{scope}"
            )
        age_h = (self.stale_by_seconds or 0) / 3600
        if self.state == RUNNING_UNKNOWN:
            return (
                f"UNKNOWN — pid {self.pids[0]} started {started}, and "
                f"{len(self.touched)} file(s) were written up to {age_h:.1f}h "
                f"later ({', '.join(self.touched[:5])}). No content snapshot from "
                "before that launch exists, so whether the code actually changed "
                "cannot be established. Re-run after the next restart for an "
                f"exact answer.{scope}"
            )
        return (
            f"STALE — pid {self.pids[0]} started {started}, but "
            f"{len(self.changed)} file(s) changed content since "
            f"({', '.join(self.changed[:5])}), up to {age_h:.1f}h later. "
            "The process is serving code older than the working tree; "
            f"restart it or the fix is not live.{scope}"
        )


def check(
    pattern: str = DEFAULT_PATTERN,
    source_root: Path | None = None,
    manifest_path: Path | None = None,
    record: bool = False,
    entrypoint: str | None = None,
) -> FreshnessResult:
    """Classify the scheduler as down, fresh, stale, or unknown.

    Two-stage on purpose. mtime is the *trigger*: it is the only signal that
    survives a `git checkout` (which stamps old content with a new mtime), so
    dropping it would trade today's false positive for a false negative — and
    the false negative is the failure that cost 20 days. Content is the
    *verdict*: mtime moving is necessary but not sufficient for staleness.
    """
    root = Path(source_root) if source_root is not None else DEFAULT_SOURCE_ROOT
    manifest = Path(manifest_path) if manifest_path is not None else DEFAULT_MANIFEST
    pids = find_processes(pattern)
    if not pids:
        return FreshnessResult(NOT_RUNNING)

    # Scope the comparison to code this process can actually import. Without
    # it the check charges the scheduler for edits to src/stockpicker/, which
    # belongs to a different consumer entirely — `scripts/stockpicker-nightly.sh`
    # runs `python -m src.stockpicker.*` under its own 23:30 cron entry, a
    # fresh interpreter each time, so it re-imports everything and can never be
    # stale. Measured 2026-07-27: 85 .py under src/, 68 reachable, 17 not, 13 of
    # them stockpicker/ — and stockpicker/ is the most actively edited part of
    # the repo, so the noise was loudest where it meant least.
    scope_set = import_closure(root, entrypoint)
    if scope_set is None:
        # Entry point missing or renamed: degrade to the old whole-tree scan.
        # Over-reporting is survivable; scoping to nothing is the fail-open.
        total = len(source_hashes(root))
        scope = f"scope: whole tree, {total} modules — no entry point to narrow it"
    else:
        total = sum(
            1 for p in Path(root).rglob("*.py") if "__pycache__" not in p.parts
        )
        scope = (
            f"scope: {len(scope_set)} of {total} modules reachable from "
            f"{entrypoint or DEFAULT_ENTRYPOINT}"
        )

    started = min(
        (t for t in (process_start_time(p) for p in pids) if t is not None),
        default=None,
    )
    newest_m, newest_p = newest_source_mtime(root, include=scope_set)
    # Not excluded here: filtering the baseline below is what actually decides
    # the verdict, and a second exclusion on this side is invisible to every
    # test (mutation M4b survived a full suite). One tested mechanism beats two
    # where only one can fail.
    # Deliberately UNSCOPED, and the scope filter lives on the baseline side
    # only. `changed_files` walks the baseline, so filtering `current` cannot
    # change any verdict — verified by mutation: removing it left all 58 tests
    # green, which is the M4b shape this module already carries a comment
    # about. Recording the whole tree is also the more robust choice: the
    # reachable set changes whenever someone adds an import, and a manifest
    # that captured everything stays usable across that change.
    current = source_hashes(root)
    if record and started is not None:
        save_manifest(manifest, current, datetime.now().timestamp())

    # No source or no start time -> we cannot show it is stale, so don't claim it.
    if started is None or newest_m is None:
        return FreshnessResult(
            RUNNING_FRESH, pids, started, newest_m, newest_p, scope=scope
        )

    # Nothing was written since launch. No baseline needed for that answer, so
    # the healthy case never degrades to UNKNOWN.
    if newest_m <= started:
        return FreshnessResult(
            RUNNING_FRESH, pids, started, newest_m, newest_p, scope=scope
        )

    touched = sorted(
        rel
        for path in Path(root).rglob("*.py")
        if "__pycache__" not in path.parts
        and (rel := path.relative_to(root).as_posix()) != SELF_RELPATH
        and (scope_set is None or rel in scope_set)
        and path.stat().st_mtime > started
    )
    _at, baseline = load_manifest(manifest, before=started)
    if baseline is None:
        state, changed = RUNNING_UNKNOWN, []
    else:
        # Drop this module from BOTH sides, not just the scan. Excluding it only
        # from `current` is unreachable: check() writes the manifest already
        # excluded, so the key is absent from the baseline and the
        # additions-are-not-changes rule hides it anyway — a mutation removing
        # that exclusion left all 35 tests green. A manifest written by an
        # older build (or by hand) *can* carry the key, and then an edit to the
        # guard would report the scheduler stale. Filtering the baseline is what
        # actually closes that.
        #
        # The scope filter has to be applied on this side for the same reason.
        # A manifest written before scoping existed carries keys the scoped
        # scan no longer produces, and `changed_files` walks the BASELINE,
        # counting a key absent from `current` as a deletion. Filtering only
        # the scan would therefore turn every pre-existing manifest into a
        # permanent STALE — a "fix" that makes the guard cry wolf on every
        # install that already had one.
        baseline = {
            k: v
            for k, v in baseline.items()
            if k != SELF_RELPATH and (scope_set is None or k in scope_set)
        }
        changed = changed_files(baseline, current)
        state = RUNNING_STALE if changed else RUNNING_FRESH

    return FreshnessResult(
        state,
        pids,
        started,
        newest_m,
        newest_p,
        changed=changed,
        touched=touched,
        scope=scope,
    )


def main(argv: list[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--pattern", default=DEFAULT_PATTERN)
    ap.add_argument("--source-root", type=Path, default=None)
    ap.add_argument("--manifest", type=Path, default=None)
    ap.add_argument(
        "--entrypoint",
        default=None,
        help=f"module the process's import graph starts at (default {DEFAULT_ENTRYPOINT})",
    )
    # Recording is ON by default and that is the whole design. The content
    # baseline can only come from a snapshot taken before the process started,
    # so a check that never records can only ever answer UNKNOWN — a converging
    # mechanism that never converges. Each run pays for the next one.
    ap.add_argument("--no-record", dest="record", action="store_false", default=True)
    args, _unknown = ap.parse_known_args(argv)

    result = check(
        args.pattern,
        args.source_root,
        args.manifest,
        record=args.record,
        entrypoint=args.entrypoint,
    )
    stamp = datetime.now().isoformat(timespec="seconds")
    print(f"[{stamp}] scheduler-freshness: {result.summary()}")
    return result.exit_code


if __name__ == "__main__":
    sys.exit(main())
