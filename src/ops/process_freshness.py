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

import os
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

# What the scheduler's command line looks like (see the @reboot crontab entry).
DEFAULT_PATTERN = "financial-bytes schedule"
DEFAULT_SOURCE_ROOT = Path(__file__).resolve().parents[2] / "src"

NOT_RUNNING = "not_running"
RUNNING_STALE = "running_stale"
RUNNING_FRESH = "running_fresh"

# Distinct so a cron caller can branch on them. 0 is the healthy case.
EXIT_CODES = {RUNNING_FRESH: 0, NOT_RUNNING: 1, RUNNING_STALE: 2}

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
    """Epoch seconds at which `pid` started, or None if it's gone.

    Derived as /proc/stat btime + the process's starttime ticks. btime is the
    kernel's (now - uptime) and drifts a second or two under NTP — successive
    calls for one process were seen 3s apart on this host. That is noise
    against the hours-to-days staleness this check reports; do not read this
    as second-accurate.
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


def newest_source_mtime(root: Path) -> tuple[float | None, Path | None]:
    """(mtime, path) of the most recently modified .py under `root`.

    Only .py counts: a .pyc is a *product* of an import, so including it would
    make the check compare the process against its own bytecode cache.
    """
    newest_m: float | None = None
    newest_p: Path | None = None
    for path in Path(root).rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        try:
            m = path.stat().st_mtime
        except OSError:
            continue
        if newest_m is None or m > newest_m:
            newest_m, newest_p = m, path
    return newest_m, newest_p


@dataclass
class FreshnessResult:
    state: str
    pids: list[int] = field(default_factory=list)
    started_at: float | None = None
    newest_source_mtime: float | None = None
    newest_source_path: Path | None = None

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
        started = datetime.fromtimestamp(self.started_at).isoformat(timespec="seconds")
        if self.state == RUNNING_FRESH:
            return f"OK — pid {self.pids[0]} started {started}, newer than all source."
        age_h = (self.stale_by_seconds or 0) / 3600
        return (
            f"STALE — pid {self.pids[0]} started {started}, but "
            f"{self.newest_source_path} changed {age_h:.1f}h later. "
            "The process is serving code older than the working tree; "
            "restart it or the fix is not live."
        )


def check(pattern: str = DEFAULT_PATTERN, source_root: Path | None = None) -> FreshnessResult:
    """Classify the scheduler as down, stale, or fresh."""
    root = Path(source_root) if source_root is not None else DEFAULT_SOURCE_ROOT
    pids = find_processes(pattern)
    if not pids:
        return FreshnessResult(NOT_RUNNING)

    started = min(
        (t for t in (process_start_time(p) for p in pids) if t is not None),
        default=None,
    )
    newest_m, newest_p = newest_source_mtime(root)

    # No source or no start time -> we cannot show it is stale, so don't claim it.
    if started is None or newest_m is None:
        state = RUNNING_FRESH
    else:
        state = RUNNING_STALE if newest_m > started else RUNNING_FRESH

    return FreshnessResult(state, pids, started, newest_m, newest_p)


def main(argv: list[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--pattern", default=DEFAULT_PATTERN)
    ap.add_argument("--source-root", type=Path, default=None)
    args, _unknown = ap.parse_known_args(argv)

    result = check(args.pattern, args.source_root)
    stamp = datetime.now().isoformat(timespec="seconds")
    print(f"[{stamp}] scheduler-freshness: {result.summary()}")
    return result.exit_code


if __name__ == "__main__":
    sys.exit(main())
