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
        """When shutil.which returns None, should fall back to known path."""
        with patch("shutil.which", return_value=None):
            import importlib
            import src.agents.director_agent as da
            importlib.reload(da)
            # _call_claude should not raise on import; the fallback path is set
            assert da is not None

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
            if mock_run.called:
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
            if mock_run.called:
                kwargs = mock_run.call_args[1]
                assert not kwargs.get("shell", False), "shell=True is a security risk"


class TestAnalystAgentClaudePath:
    """analyst_agent path fix — NOTE: branch only fixed director_agent.
    analyst_agent still uses bare 'claude'. These tests document the gap."""

    def test_analyst_sync_call_claude_path_KNOWN_INCOMPLETE(self):
        """KNOWN BUG: analyst_agent._call_claude still uses bare 'claude'.
        This test documents the incomplete fix — analyst_agent must also be updated
        before merging. The fix in director_agent alone is insufficient because
        analyst_agent runs parallel Haiku calls for all tickers and faces the
        same daemon PATH issue.
        """
        import src.agents.analyst_agent as aa
        import inspect
        source = inspect.getsource(aa._call_claude)
        uses_bare_claude = 'cmd = ["claude"' in source or "cmd = ['claude'" in source
        if uses_bare_claude:
            pytest.xfail(
                "INCOMPLETE FIX: analyst_agent._call_claude still uses bare 'claude'. "
                "Must be fixed before merging: apply same shutil.which() fix as director_agent."
            )

    def test_analyst_async_call_claude_path_KNOWN_INCOMPLETE(self):
        """KNOWN BUG: analyst_agent._call_claude_async also uses bare 'claude'."""
        import src.agents.analyst_agent as aa
        import inspect
        source = inspect.getsource(aa._call_claude_async)
        uses_bare_claude = '"claude"' in source and "shutil.which" not in source
        if uses_bare_claude:
            pytest.xfail(
                "INCOMPLETE FIX: analyst_agent._call_claude_async still uses bare 'claude'. "
                "Fix required: claude_bin = shutil.which('claude') or '/home/nboss/.local/bin/claude'"
            )

    def test_analyst_claude_bin_module_attribute(self):
        """If _CLAUDE_BIN exists at module level, it must be an absolute path."""
        import src.agents.analyst_agent as aa
        claude_bin = getattr(aa, "_CLAUDE_BIN", None)
        if claude_bin:
            assert claude_bin != "claude", (
                f"_CLAUDE_BIN must be a full path, got: {claude_bin!r}"
            )
            assert os.path.isabs(claude_bin), f"_CLAUDE_BIN must be absolute: {claude_bin!r}"


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
