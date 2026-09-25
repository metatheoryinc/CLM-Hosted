"""integrations/claude_code/install.py against a throwaway HOME."""
import json
import os
import subprocess
import sys

import pytest

INSTALL = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "integrations", "claude_code", "install.py")


@pytest.fixture
def sh(tmp_path):
    env = {k: v for k, v in os.environ.items() if not k.startswith("CLM_")}
    env["HOME"] = str(tmp_path)

    def run(*args, **extra):
        return subprocess.run([sys.executable, INSTALL, *args], capture_output=True, text=True,
                              env={**env, **extra}, stdin=subprocess.DEVNULL, timeout=30)
    return run


def settings(tmp_path):
    return json.loads((tmp_path / ".claude" / "settings.json").read_text())


def config(tmp_path):
    return json.loads((tmp_path / ".config" / "clm" / "claude-code.json").read_text())


def test_install_registers_every_event_and_writes_an_active_private_config(sh, tmp_path):
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "settings.json").write_text(json.dumps(
        {"model": "opus", "hooks": {"Stop": [{"matcher": "", "hooks": [{"type": "command", "command": "say done"}]}]}}))
    p = sh("--key", "k-123")
    assert p.returncode == 0, p.stderr
    s = settings(tmp_path)
    assert s["model"] == "opus"
    mine = {e: h for e, gs in s["hooks"].items() for g in gs for h in g["hooks"] if "clm_hook.py" in h["command"]}
    assert set(mine) == {"PreToolUse", "Stop", "SubagentStop", "PermissionRequest", "PermissionDenied",
                         "PostToolUse", "PostToolUseFailure", "SubagentStart"}
    assert all(not h.get("async") and h["timeout"] == 40 for e, h in mine.items() if e in ("PreToolUse", "Stop", "SubagentStop"))
    assert mine["PostToolUse"]["async"] is True
    assert any(h["command"] == "say done" for g in s["hooks"]["Stop"] for h in g["hooks"])    # theirs kept
    c = config(tmp_path)
    assert (c["api_key"], c["subagent_mode"], c["behavior_mode"], c["mode"]) == ("k-123", "active", "active", "shadow")
    assert oct(os.stat(tmp_path / ".config" / "clm" / "claude-code.json").st_mode & 0o777) == "0o600"
    assert "k-123" not in p.stdout


def test_reinstall_is_idempotent_and_keeps_the_users_settings(sh, tmp_path):
    sh("--key", "k1")
    cfg = config(tmp_path)
    cfg["behavior_mode"] = "off"
    (tmp_path / ".config" / "clm" / "claude-code.json").write_text(json.dumps(cfg))
    sh(CLM_API_KEY="")                                  # the key comes from the existing config
    s = settings(tmp_path)
    assert all(len(gs) == 1 for gs in s["hooks"].values())
    assert config(tmp_path)["behavior_mode"] == "off" and config(tmp_path)["api_key"] == "k1"


def test_shadow_flag_and_env_key(sh, tmp_path):
    sh("--shadow", CLM_API_KEY="k-env")
    c = config(tmp_path)
    assert (c["api_key"], c["subagent_mode"], c["behavior_mode"]) == ("k-env", "shadow", "shadow")


def test_no_key_without_a_terminal_fails_cleanly(sh, tmp_path):
    p = sh()
    assert p.returncode != 0 and "no agent key" in p.stderr
    assert not (tmp_path / ".claude" / "settings.json").exists()


def test_uninstall_removes_only_our_hooks_and_turns_the_config_off(sh, tmp_path):
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "settings.json").write_text(json.dumps(
        {"hooks": {"Stop": [{"matcher": "", "hooks": [{"type": "command", "command": "say done"}]}]}}))
    sh("--key", "k1")
    p = sh("uninstall")
    assert p.returncode == 0 and "removed 8 hooks" in p.stdout
    assert settings(tmp_path)["hooks"] == {"Stop": [{"matcher": "", "hooks": [{"type": "command", "command": "say done"}]}]}
    assert config(tmp_path)["mode"] == "off"
    assert list((tmp_path / ".claude").glob("settings.json.bak-*"))
    sh("uninstall", "--purge")
    assert not (tmp_path / ".config" / "clm" / "claude-code.json").exists()


def test_status_never_prints_the_key(sh, tmp_path):
    sh("--key", "secret-key-xyz")
    p = sh("status")
    assert "PreToolUse" in p.stdout and "key set" in p.stdout and "secret-key-xyz" not in p.stdout
