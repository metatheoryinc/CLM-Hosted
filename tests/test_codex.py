"""The CLM hook under OpenAI Codex: rollout transcripts, Codex defaults, and integrations/codex/install.py."""
import importlib.util
import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "integrations", "claude_code"))
import clm_behaviors as CB  # noqa: E402

spec = importlib.util.spec_from_file_location("clm_hook_codex", os.path.join(ROOT, "integrations", "claude_code", "clm_hook.py"))
hook = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hook)
INSTALL = os.path.join(ROOT, "integrations", "codex", "install.py")


def ev(kind, payload):
    return {"timestamp": "2026-09-26T00:00:00Z", "type": kind, "payload": payload}


def item(kind, **kw):
    return ev("event_msg", {"type": "item_completed", "item": {"type": kind, **kw}})


def rollout(tmp_path):
    lines = [
        ev("session_meta", {"session_id": "s", "cwd": "/repo"}),
        ev("event_msg", {"type": "task_started", "turn_id": "t1"}),
        ev("response_item", {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "<recommended_plugins>x"}]}),
        item("UserMessage", content=[{"type": "text", "text": "Rename the loader."}]),
        ev("response_item", {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "Renamed."}]}),
        ev("event_msg", {"type": "task_started", "turn_id": "t2"}),
        ev("turn_context", {"turn_id": "t2", "model": "gpt-5.6-terra"}),
        item("UserMessage", content=[{"type": "text", "text": "Fix add()."}]),
        ev("response_item", {"type": "reasoning", "encrypted_content": "gAAAA"}),
        ev("response_item", {"type": "custom_tool_call", "name": "exec", "input": "apply_patch(...)", "call_id": "c1"}),
        ev("response_item", {"type": "custom_tool_call_output", "call_id": "c1", "output": [{"type": "input_text", "text": "ok"}]}),
        ev("response_item", {"type": "function_call", "name": "spawn_agent", "arguments": "{\"task_name\": \"x\"}", "call_id": "c2"}),
        ev("response_item", {"type": "function_call_output", "call_id": "c2", "output": "spawned"}),
        ev("event_msg", {"type": "token_count", "info": {"last_token_usage": {"input_tokens": 120, "output_tokens": 9}}}),
        ev("response_item", {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "Fixed, tests pass."}]}),
    ]
    f = tmp_path / "rollout-x.jsonl"
    f.write_text("\n".join(json.dumps(x) for x in lines) + "\n")
    return str(f)


def test_a_codex_rollout_becomes_the_same_kind_of_trace(tmp_path):
    meta, msgs, out = CB.trace_of(rollout(tmp_path), None, "t2")
    assert meta["model"] == "gpt-5.6-terra" and (meta["prompt_tokens"], meta["completion_tokens"]) == (120, 9)
    assert [m["role"] for m in msgs] == ["user", "user", "assistant", "tool", "assistant", "tool"]
    assert msgs[0]["content"] == "Rename the loader." and msgs[1]["content"] == "Fix add()."
    assert msgs[2]["tool_calls"][0]["name"] == "exec" and msgs[3]["content"] == "ok"
    assert out["content"] == "Fixed, tests pass."
    assert "recommended_plugins" not in json.dumps(msgs) and "gAAAA" not in json.dumps(msgs)
    # the hook's last_assistant_message wins; an unknown turn falls back to the latest one
    assert CB.trace_of(rollout(tmp_path), "Done.", "nope")[2]["content"] == "Done."


def test_codex_runtime_defaults_and_overrides(tmp_path, monkeypatch):
    for k in [k for k in os.environ if k.startswith(("CLAUDE_PLUGIN_", "CLM_"))]:
        monkeypatch.delenv(k)
    cfg_file = tmp_path / "c.json"
    cfg_file.write_text(json.dumps({"api_key": "k", "behavior_threshold": 0.8, "codex": {"behavior_threshold": 0.95}}))
    monkeypatch.setenv("CLM_HOOK_CONFIG", str(cfg_file))
    cfg = hook.load_config()                                      # Claude Code: "codex" overrides ignored
    assert cfg["behavior_threshold"] == 0.8 and "codex" not in cfg and cfg["judge_cmd"].startswith("claude")
    monkeypatch.setenv("CLM_HOOK_RUNTIME", "codex")
    cfg = hook.load_config()
    assert (cfg["mode"], cfg["behavior_mode"], cfg["behavior_threshold"]) == ("shadow", "active", 0.95)
    assert cfg["judge_cmd"].startswith("codex exec") and "features.hooks=false" in cfg["judge_cmd"]
    assert hook.data_dir() == os.path.expanduser("~/.codex/clm")


def run_install(tmp_path, *args, **env):
    e = {k: v for k, v in os.environ.items() if not k.startswith("CLM_")}
    e.update(CODEX_HOME=str(tmp_path / "codex"), CLM_HOOK_CONFIG=str(tmp_path / "clm.json"), **env)
    return subprocess.run([sys.executable, INSTALL, *args], capture_output=True, text=True, env=e,
                          stdin=subprocess.DEVNULL, timeout=30)


def test_install_copies_the_hook_and_keeps_other_hooks(tmp_path):
    (tmp_path / "codex").mkdir()
    other = {"matcher": "", "hooks": [{"type": "command", "command": "say hi"}]}
    (tmp_path / "codex" / "hooks.json").write_text(json.dumps({"description": "mine", "hooks": {"Stop": [other]}}))
    p = run_install(tmp_path, "--key", "k-1")
    assert p.returncode == 0, p.stderr
    doc = json.loads((tmp_path / "codex" / "hooks.json").read_text())
    assert doc["description"] == "mine" and other in doc["hooks"]["Stop"]
    ours = {e for e, gs in doc["hooks"].items() for g in gs for h in g["hooks"] if "clm_hook.py" in h["command"]}
    assert ours == {"PreToolUse", "Stop", "SubagentStop", "PermissionRequest", "PostToolUse", "SubagentStart"}
    h = next(h for g in doc["hooks"]["PreToolUse"] for h in g["hooks"])
    assert set(h) == {"type", "command"} and "CLM_HOOK_RUNTIME=codex" in h["command"]   # Codex rejects unknown fields
    for f in ("clm_hook.py", "clm_behaviors.py", "behaviors.json", "rubrics/behaviors.md"):
        assert (tmp_path / "codex" / "clm" / f).exists(), f
    assert json.loads((tmp_path / "clm.json").read_text())["api_key"] == "k-1"
    assert oct(os.stat(tmp_path / "clm.json").st_mode & 0o777) == "0o600"
    run_install(tmp_path, "--key", "k-1")                              # idempotent
    doc = json.loads((tmp_path / "codex" / "hooks.json").read_text())
    assert sum("clm_hook.py" in h["command"] for g in doc["hooks"]["PreToolUse"] for h in g["hooks"]) == 1


def test_uninstall_leaves_other_hooks(tmp_path):
    (tmp_path / "codex").mkdir()
    other = {"matcher": "", "hooks": [{"type": "command", "command": "say hi"}]}
    (tmp_path / "codex" / "hooks.json").write_text(json.dumps({"hooks": {"Stop": [other]}}))
    run_install(tmp_path, "--key", "k")
    p = run_install(tmp_path, "uninstall")
    assert "removed 6 hooks" in p.stdout
    assert json.loads((tmp_path / "codex" / "hooks.json").read_text()) == {"hooks": {"Stop": [other]}}
    assert not (tmp_path / "codex" / "clm").exists() and (tmp_path / "clm.json").exists()
    run_install(tmp_path, "uninstall", "--purge")
    assert not (tmp_path / "clm.json").exists()


def test_no_key_fails_cleanly(tmp_path):
    p = run_install(tmp_path)
    assert p.returncode != 0 and "no agent key" in p.stderr
