"""Tests for `terum-capture update` (reinstall-from-git + hook refresh)."""
import sys
from unittest.mock import MagicMock, patch

import pytest

import terum_capture.updater as u


class TestInstallManagerDetection:
    def test_pipx_branch_when_prefix_is_a_pipx_venv(self):
        with patch.object(u.shutil, "which", return_value="/usr/bin/pipx"), \
             patch.object(u.sys, "prefix", "/home/x/.local/pipx/venvs/terum-capture"):
            assert u._pipx_manages_this() is True
            cmd = u._reinstall_cmd()
            assert cmd[0] == "pipx" and "--force" in cmd and cmd[-1] == u.REPO_URL

    def test_pip_branch_when_pipx_absent(self):
        with patch.object(u.shutil, "which", return_value=None):
            assert u._pipx_manages_this() is False
            cmd = u._reinstall_cmd()
            assert cmd[0] == sys.executable and "pip" in cmd and "--force-reinstall" in cmd

    def test_pip_branch_when_not_inside_a_pipx_venv(self):
        with patch.object(u.shutil, "which", return_value="/usr/bin/pipx"), \
             patch.object(u.sys, "prefix", "/home/x/project/.venv"):
            assert u._pipx_manages_this() is False

    def test_uv_branch_when_prefix_is_a_uv_tool_venv(self):
        which = lambda name: "/opt/homebrew/bin/uv" if name == "uv" else None
        with patch.object(u.shutil, "which", which), \
             patch.object(u.sys, "prefix", "/home/x/.local/share/uv/tools/terum-capture"):
            assert u._uv_manages_this() is True
            cmd = u._reinstall_cmd()
            assert cmd[:3] == ["uv", "tool", "install"]
            assert "--force" in cmd and cmd[-1] == u.REPO_URL

    def test_uv_branch_on_a_windows_roaming_prefix(self):
        with patch.object(u.shutil, "which", return_value="C:\\uv.exe"), \
             patch.object(u.sys, "prefix", "C:\\Users\\X\\AppData\\Roaming\\uv\\tools\\terum-capture"):
            assert u._uv_manages_this() is True

    def test_pip_branch_when_uv_launcher_is_absent(self):
        # Inside a uv tool venv but no `uv` on PATH -> we cannot shell out to it;
        # detection must fail closed to the pip branch (which will error loudly)
        # rather than crash on a missing launcher.
        with patch.object(u.shutil, "which", return_value=None), \
             patch.object(u.sys, "prefix", "/home/x/.local/share/uv/tools/terum-capture"):
            assert u._uv_manages_this() is False
            cmd = u._reinstall_cmd()
            assert "pip" in cmd

    def test_pipx_venv_never_matches_the_uv_detector(self):
        with patch.object(u.shutil, "which", return_value="/usr/bin/uv"), \
             patch.object(u.sys, "prefix", "/home/x/.local/pipx/venvs/terum-capture"):
            assert u._uv_manages_this() is False


class TestCmdUpdate:
    _CMD = ["pipx", "install", "--force", u.REPO_URL]

    def test_success_reinstalls_then_refreshes_hook(self, capsys):
        ok = MagicMock(returncode=0)
        with patch.object(u.subprocess, "run", return_value=ok) as run, \
             patch.object(u, "_reinstall_cmd", return_value=self._CMD):
            u.cmd_update()
        assert run.call_count == 2                      # reinstall, then setup-hook
        first = run.call_args_list[0][0][0]
        second = run.call_args_list[1][0][0]
        assert first == self._CMD
        assert second[1:] == ["-m", "terum_capture", "setup-hook"]
        assert "Update complete" in capsys.readouterr().out

    def test_exits_1_when_reinstall_returns_nonzero(self):
        fail = MagicMock(returncode=1)
        with patch.object(u.subprocess, "run", return_value=fail), \
             patch.object(u, "_reinstall_cmd", return_value=self._CMD):
            with pytest.raises(SystemExit) as exc:
                u.cmd_update()
        assert exc.value.code == 1

    def test_exits_1_when_manager_missing(self):
        with patch.object(u.subprocess, "run", side_effect=FileNotFoundError()), \
             patch.object(u, "_reinstall_cmd", return_value=self._CMD):
            with pytest.raises(SystemExit) as exc:
                u.cmd_update()
        assert exc.value.code == 1

    def test_hook_refresh_failure_is_nonfatal(self, capsys):
        # Reinstall succeeds; the follow-up setup-hook subprocess raises -> we warn, not crash.
        results = [MagicMock(returncode=0)]
        def side_effect(cmd, **kw):
            if cmd == self._CMD:
                return results[0]
            raise OSError("spawn failed")
        with patch.object(u.subprocess, "run", side_effect=side_effect), \
             patch.object(u, "_reinstall_cmd", return_value=self._CMD):
            u.cmd_update()  # must not raise
        out = capsys.readouterr().out
        assert "could not refresh the hook" in out
