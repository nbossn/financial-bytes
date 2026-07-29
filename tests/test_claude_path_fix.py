"""Tests for the claude-path fix in director_agent and analyst_agent.

Verifies:
  1. director_agent uses shutil.which() to resolve claude, not bare 'claude'
  2. Falls back to known install path when which() returns None
  3. analyst_agent uses same resolution pattern
  4. PATH-less environments (like daemon) still find the binary
"""
from __future__ import annotations

import os
import subprocess
from unittest.mock import patch, MagicMock

import pytest


class TestDirectorAgentClaudePath:
    """director_agent._call_claude must not use bare 'claude' string."""

    def test_claude_bin_resolved_via_shutil_which(self):
        """director_agent._call_claude uses claude_bin = shutil.which() at call time."""
        import src.agents.director_agent as da
        import inspect
        source = inspect.getsource(da._call_claude)
        # The fix: shutil.which is called at call-time, not module init
        assert "shutil.which" in source, (
            "director_agent._call_claude must call shutil.which('claude') — fix not applied"
        )
        assert 'cmd = [claude_bin' in source or 'cmd = [claude_bin,' in source, (
            "director_agent must use resolved claude_bin in cmd list, not bare 'claude'"
        )

    def test_claude_bin_fallback_when_which_returns_none(self):
        """When shutil.which returns None the resolved binary must still be an
        absolute path.

        Previously this asserted `da is not None` — a tautology that passes
        whether or not a fallback exists. It now executes the call with
        which() forced to None and reads the argv actually built.
        """
        import src.agents.director_agent as da
        with patch("shutil.which", return_value=None), \
             patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="{}", stderr="")
            try:
                da._call_claude("test prompt")
            except Exception:
                pass
            assert mock_run.called, "the claude call never executed — test is vacuous"
            cmd = mock_run.call_args[0][0]
            assert cmd[0].startswith("/"), f"no absolute fallback: {cmd[0]!r}"

    def test_director_call_uses_list_not_bare_string(self):
        """subprocess.run must receive a list — bare string allows shell injection."""
        import src.agents.director_agent as da
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout='{"market_theme": "test", "five_min_summary": "x", '
                       '"portfolio_summary": "x", "action_items": [], '
                       '"top_opportunities": [], "top_risks": [], '
                       '"overall_sentiment": 0.0}',
                stderr="",
            )
            # Build a minimal prompt and call
            try:
                da._call_claude("test prompt")
            except Exception:
                pass
            assert mock_run.called, "the claude call never executed — test is vacuous"
            if True:
                call_args = mock_run.call_args
                cmd = call_args[0][0] if call_args[0] else call_args[1].get("args", [])
                assert isinstance(cmd, list), "cmd must be a list, not a string (shell injection risk)"
                assert cmd[0] != "claude", f"cmd[0] must be a full path, not bare 'claude': {cmd[0]}"

    def test_no_shell_true_in_subprocess_call(self):
        """subprocess.run must NOT use shell=True (command injection risk)."""
        import src.agents.director_agent as da
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="{}", stderr="")
            try:
                da._call_claude("test prompt")
            except Exception:
                pass
            assert mock_run.called, "the claude call never executed — test is vacuous"
            if True:
                kwargs = mock_run.call_args[1]
                assert not kwargs.get("shell", False), "shell=True is a security risk"


class TestAnalystAgentClaudePath:
    """analyst_agent path resolution.

    HISTORY: these two tests were written while analyst_agent still used bare
    'claude', and they called pytest.xfail INSIDE `if uses_bare_claude:`. Once
    the code was fixed the condition went false and they passed having asserted
    nothing — and they would have stayed silent if it regressed, because a
    regression makes them xfail, not fail. They now assert the fix directly.

    The broader sweep (every call site, discovered rather than listed) lives in
    test_claude_binary_sweep.py — written after this file's two-site list was
    found to have missed quant_agent and managing_director_agent entirely.
    """

    def test_analyst_sync_call_claude_resolves_a_path(self):
        import src.agents.analyst_agent as aa
        import inspect
        source = inspect.getsource(aa._call_claude)
        assert 'cmd = ["claude"' not in source and "cmd = ['claude'" not in source, (
            "analyst_agent._call_claude invokes claude by bare name — fails under "
            "cron/daemon PATH")
        assert "_CLAUDE_BIN" in source or "shutil.which" in source

    def test_analyst_async_call_claude_resolves_a_path(self):
        import src.agents.analyst_agent as aa
        import inspect
        source = inspect.getsource(aa._call_claude_async)
        assert "_CLAUDE_BIN" in source or "shutil.which" in source, (
            "analyst_agent._call_claude_async invokes claude by bare name")

    def test_analyst_claude_bin_module_attribute(self):
        """_CLAUDE_BIN must exist at module level AND be an absolute path.

        Previously both assertions sat under `if claude_bin:`, so deleting the
        attribute outright — the exact regression that reintroduces the bug —
        made this test pass.
        """
        import src.agents.analyst_agent as aa
        claude_bin = getattr(aa, "_CLAUDE_BIN", None)
        assert claude_bin, "analyst_agent._CLAUDE_BIN is missing entirely"
        assert claude_bin != "claude", (
            f"_CLAUDE_BIN must be a full path, got: {claude_bin!r}")
        assert os.path.isabs(claude_bin), (
            f"_CLAUDE_BIN must be absolute, got: {claude_bin!r}")


class TestDaemonPathResolution:
    """Simulate a daemon environment without ~/.local/bin in PATH."""

    def test_director_works_with_restricted_path(self):
        """Even with PATH=/usr/bin:/bin, director should find or fallback to claude."""
        restricted_env = {**os.environ, "PATH": "/usr/bin:/bin"}
        import shutil
        found = shutil.which("claude", path=restricted_env["PATH"])
        # Either it's found in a standard path, or the fallback hardcoded path exists
        fallback = "/home/nboss/.local/bin/claude"
        assert found or os.path.exists(fallback), (
            f"claude not in restricted PATH and fallback {fallback} missing — "
            "daemon will fail. Check the fix."
        )
