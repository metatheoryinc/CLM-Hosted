"""integrations/claude_code/clm_hook.py, run as Claude Code runs it, against a fake CLM."""
import http.server
import importlib.util
import json
import os
import subprocess
import sys
import threading
import time

import pytest

HOOK = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "integrations", "claude_code", "clm_hook.py")
spec = importlib.util.spec_from_file_location("clm_hook", HOOK)
hook = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hook)


class FakeCLM:
    """/v1/systemone answers with ``probs`` after ``delay`` s; /v1/decisions records events."""

    def __init__(self, probs=None, delay=0.0):
        self.probs, self.delay, self.systemone, self.decisions = probs, delay, [], []
        outer = self

        class H(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                if self.path == "/v1/systemone":
                    outer.systemone.append((self.headers["Authorization"], body))
                    time.sleep(outer.delay)
                    p = outer.probs or {"allow": 0.9, "review": 0.07, "block": 0.03}
                    out = {"model": "clm-latest", "answers": {"route": {
                        "type": "choice", "choice": max(p, key=p.get), "confidence": 0.5, "probabilities": p}}}
                else:
                    outer.decisions.append(body)
                    out = {"stored": 1}
                data = json.dumps(out).encode()
                self.send_response(200); self.send_header("Content-Length", str(len(data))); self.end_headers()
                self.wfile.write(data)

            def log_message(self, *a):
                pass

        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.srv.server_port}"

    def wait(self, n, timeout=10):
        deadline = time.time() + timeout
        while len(self.decisions) < n and time.time() < deadline:
            time.sleep(0.05)
        return self.decisions


@pytest.fixture
def run(tmp_path):
    servers = []

    def _run(event, mode="shadow", probs=None, delay=0.0, threshold=0.9, config=True):
        clm = FakeCLM(probs, delay)
        servers.append(clm)
        cfg = tmp_path / "cfg.json"
        cfg.write_text(json.dumps({"mode": mode, "base_url": clm.url, "api_key": "agent-key",
                                   "threshold": threshold, "timeout": 1.0}) if config else "{}")
        env = {k: v for k, v in os.environ.items() if not k.startswith("CLM_")}
        env["CLM_HOOK_CONFIG"] = str(cfg)
        t0 = time.perf_counter()
        p = subprocess.run([sys.executable, HOOK], input=json.dumps(event), capture_output=True, text=True, env=env,
                           timeout=30)
        return p, time.perf_counter() - t0, clm

    yield _run
    for s in servers:
        s.srv.shutdown()


def pre(tool="Bash", tool_input=None, tid="toolu_1"):
    return {"hook_event_name": "PreToolUse", "session_id": "s1", "tool_use_id": tid, "tool_name": tool,
            "tool_input": tool_input or {"command": "ls -la", "description": "List files"},
            "cwd": "/repo", "permission_mode": "default", "transcript_path": "/secret/path.jsonl"}


def test_shadow_returns_before_clm_answers_and_logs_in_the_background(run):
    p, took, clm = run(pre(), delay=1.5)
    assert p.returncode == 0 and p.stdout == "" and took < 1.0
    [rec] = clm.wait(1)
    assert rec["id"] == "toolu_1" and rec["workflow"] == "routing/claude-code-tools"
    assert rec["clm"]["choice"] == "allow" and rec["acted"] == "baseline"
    assert rec["state"] == {"tool": "Bash", "input": "command: ls -la\ndescription: List files",
                            "working directory": "/repo", "permission mode": "default"}
    assert "transcript_path" not in json.dumps(rec)
    assert clm.systemone[0][0] == "Bearer agent-key"


@pytest.mark.parametrize("event, label, rank", [("PostToolUse", "allow", 1), ("PostToolUseFailure", "allow", 1),
                                                ("PermissionRequest", "review", 2), ("PermissionDenied", "block", 3)])
def test_later_events_log_claude_codes_own_decision(run, event, label, rank):
    p, _, clm = run(dict(pre(), hook_event_name=event))
    assert p.returncode == 0 and p.stdout == ""
    [e] = clm.wait(1)
    assert (e["event"], e["id"], e["label"], e["rank"]) == ("baseline", "toolu_1", label, rank)
    assert clm.systemone == []


def test_active_denies_confident_blocks(run):
    p, _, clm = run(pre(tool_input={"command": "rm -rf ~"}), mode="active",
                    probs={"allow": 0.02, "review": 0.03, "block": 0.95})
    out = json.loads(p.stdout)["hookSpecificOutput"]
    assert out["permissionDecision"] == "deny" and "p=0.95" in out["permissionDecisionReason"]
    [rec] = clm.wait(1)
    assert rec["acted"] == "clm" and len(clm.systemone) == 1      # the answer is reused, not re-asked


def test_active_forces_review(run):
    p, _, _ = run(pre(), mode="active", probs={"allow": 0.04, "review": 0.93, "block": 0.03})
    assert json.loads(p.stdout)["hookSpecificOutput"]["permissionDecision"] == "ask"


@pytest.mark.parametrize("probs", [{"allow": 0.97, "review": 0.02, "block": 0.01},       # never approves
                                   {"allow": 0.1, "review": 0.2, "block": 0.7}])          # below the threshold
def test_active_never_loosens_or_acts_unsure(run, probs):
    p, _, clm = run(pre(), mode="active", probs=probs)
    assert p.stdout == "" and clm.wait(1)[0]["acted"] == "baseline"


def test_active_times_out_to_claude_code(run):
    p, took, clm = run(pre(), mode="active", probs={"allow": 0.0, "review": 0.0, "block": 1.0}, delay=3)
    assert p.stdout == "" and took < 2.5
    assert "error" in clm.wait(1)[0]["clm"]


@pytest.mark.parametrize("mode, config", [("off", True), ("shadow", False)])
def test_does_nothing_unless_configured(run, mode, config):
    p, _, clm = run(pre(), mode=mode, config=config)
    time.sleep(0.5)
    assert p.returncode == 0 and p.stdout == "" and clm.systemone == [] and clm.decisions == []


def test_garbage_input_never_breaks_the_session():
    p = subprocess.run([sys.executable, HOOK], input="not json", capture_output=True, text=True, timeout=10)
    assert p.returncode == 0 and p.stdout == ""


@pytest.mark.parametrize("text", [
    "curl -H 'Authorization: Bearer abcdef0123456789xyz' https://x",
    "export API_KEY=hunter2hunter2", "password: s3cretpass", "git push https://ghp_abcdefghijklmnopqrstuvwx@github.com",
    "aws configure set aws_access_key_id AKIAABCDEFGHIJKLMNOP", "key 0123456789abcdef0123456789abcdef01",
    "-----BEGIN RSA PRIVATE KEY-----\nMIIEow\n-----END RSA PRIVATE KEY-----", "OPENAI sk-proj-abcdefghijklmnopqrstuv",
])
def test_secrets_are_redacted(text):
    out = hook.redact(text)
    assert "[REDACTED" in out
    for secret in ("abcdef0123456789xyz", "hunter2hunter2", "s3cretpass", "ghp_abcdefghijklmnopqrstuvwx",
                   "AKIAABCDEFGHIJKLMNOP", "0123456789abcdef0123456789abcdef01", "MIIEow", "sk-proj-abcdefghijklmnopqrstuv"):
        assert secret not in out


def test_long_inputs_are_clipped():
    s = hook.describe_input({"content": "x" * 5000, "file_path": "/repo/a.py"})
    assert len(s) < hook.MAX_INPUT + 100 and "more characters" in s and "file_path: /repo/a.py" in s
