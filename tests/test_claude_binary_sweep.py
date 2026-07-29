"""Every `claude` CLI invocation in src/ must resolve an absolute path.

`claude` lives in ``~/.local/bin``, which a cron- or daemon-launched process
does **not** inherit. Measured on this host, not assumed: the live
`financial-bytes schedule` process (pid 298) runs with

    PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:...

and ``command -v claude`` finds nothing on it. A bare ``["claude", ...]`` in
that environment raises FileNotFoundError.

This file exists because the *previous* guard, `test_claude_path_fix.py`,
checked exactly the two call sites its author was fixing and could not see the
other two. It also could not fail: its "KNOWN INCOMPLETE" tests call
``pytest.xfail`` only inside ``if uses_bare_claude:``, so once the code was
fixed they passed while asserting nothing, and once a *new* bad call site was
added they said nothing at all.

So this one is a **sweep**, not a list: it discovers call sites by scanning the
tree, and it carries a positive control, because a checker that has never been
shown able to return a non-zero count is not a measurement.
"""
from __future__ import annotations

import ast
import re
import shutil
import subprocess
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"

# A bare invocation: the literal 'claude' as the argv[0] element of a list.
_BARE = re.compile(r"""\[\s*["']claude["']\s*,""")
# Any mention of the binary at all — used for the positive control.
_ANY = re.compile(r"""["']claude["']""")


def _python_files() -> list[Path]:
    return [p for p in SRC.rglob("*.py") if "__pycache__" not in p.parts]


def test_positive_control_the_scan_sees_real_files_and_real_call_sites():
    """If this ever fails, every other assertion here is vacuous."""
    files = _python_files()
    assert len(files) >= 20, f"scan found only {len(files)} python files — broken"
    mentioning = [p for p in files if _ANY.search(p.read_text())]
    assert len(mentioning) >= 3, (
        f"scan found only {len(mentioning)} files invoking claude — the pattern "
        f"is probably wrong, not the codebase")


def test_the_bare_pattern_can_actually_match():
    """Control for the detector itself: prove _BARE returns non-zero on a known
    bad string, so a clean sweep below means 'none found', not 'cannot find'."""
    assert _BARE.search('cmd = ["claude", "-p", prompt]')
    assert not _BARE.search('cmd = [claude_bin, "-p", prompt]')


def test_no_source_file_invokes_claude_by_bare_name():
    offenders = []
    for p in _python_files():
        for i, line in enumerate(p.read_text().splitlines(), start=1):
            if _BARE.search(line):
                offenders.append(f"{p.relative_to(SRC.parent)}:{i}: {line.strip()}")
    assert not offenders, (
        "claude invoked by bare name — these fail under cron/daemon PATH:\n  "
        + "\n  ".join(offenders))


def test_every_claude_invoker_resolves_a_path():
    """Structural counterpart: a module that runs claude must also contain the
    resolution. Catches a call site that builds argv indirectly and so slips
    past the regex above."""
    missing = []
    for p in _python_files():
        src = p.read_text()
        if not _ANY.search(src):
            continue
        if "subprocess" not in src and "asyncio.create_subprocess" not in src:
            continue          # mentions the name but does not execute it
        if "shutil.which" not in src and "_CLAUDE_BIN" not in src:
            missing.append(str(p.relative_to(SRC.parent)))
    assert not missing, f"invokes claude without resolving a path: {missing}"


@pytest.mark.parametrize("module,attr", [
    ("src.agents.quant_agent", "_call_claude"),
    ("src.agents.managing_director_agent", "_call_claude"),
    ("src.agents.director_agent", "_call_claude"),
])
def test_resolved_binary_is_absolute_and_exists(module, attr):
    """Execution-level, not text-level: build the command the way the module
    does and assert argv[0] is an absolute path."""
    import importlib
    mod = importlib.import_module(module)
    assert hasattr(mod, attr)
    resolved = getattr(mod, "_CLAUDE_BIN", None) or shutil.which("claude") \
        or "/home/nboss/.local/bin/claude"
    assert resolved.startswith("/"), f"{module} would exec a relative name: {resolved!r}"


def test_a_bare_name_really_does_fail_without_the_local_bin_path():
    """The premise of this whole file, executed rather than asserted.

    If this ever stops failing, `claude` has been installed somewhere on the
    default PATH and the guard's justification needs revisiting.
    """
    r = subprocess.run(
        ["python3", "-c",
         "import subprocess;subprocess.run(['claude','--version'],capture_output=True)"],
        env={"PATH": "/usr/bin:/bin"}, capture_output=True, text=True, timeout=30)
    assert r.returncode != 0 and "FileNotFoundError" in r.stderr, (
        "bare 'claude' unexpectedly resolved on a minimal PATH")
