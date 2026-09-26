"""integrations/claude_code/clm_hook.py, run as Claude Code runs it, against a fake CLM."""
import http.server
import importlib.util
import json
import re
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
                    answers = {}
                    for qid, q in body["questions"].items():
                        keys = list(q["criteria"])
                        pr = outer.probs.get(qid) if isinstance((outer.probs or {}).get(qid), dict) else outer.probs
                        p = pr if pr and set(pr) == set(keys) else \
                            {k: (0.9 if i == 0 else 0.1 / (len(keys) - 1)) for i, k in enumerate(keys)}
                        answers[qid] = {"type": "choice", "choice": max(p, key=p.get), "confidence": 0.5,
                                        "probabilities": p}
                    out = {"model": "clm-latest", "answers": answers}
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

    def _run(event, mode="shadow", probs=None, delay=0.0, threshold=0.9, config=True, extra=None, env_extra=None):
        clm = FakeCLM(probs, delay)
        servers.append(clm)
        cfg = tmp_path / "cfg.json"
        cfg.write_text(json.dumps({"mode": mode, "base_url": clm.url, "api_key": "agent-key",
                                   "threshold": threshold, "timeout": 1.0, **(extra or {})}) if config else "{}")
        env = {k: v for k, v in os.environ.items() if not k.startswith("CLM_")}
        env["CLM_HOOK_CONFIG"] = str(cfg)
        env["CLM_HOOK_STATE_DIR"] = str(tmp_path / "claims")
        env.pop("CLAUDE_CODE_SUBAGENT_MODEL", None)
        env["HOME"] = str(tmp_path / "home")                 # no real ~/.claude/agents
        env.update(env_extra or {})
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
    s = hook.describe_input({"content": "x" * 20000, "file_path": "/repo/a.py"})
    assert len(s) < hook.MAX_INPUT + 100 and "more characters" in s and "file_path: /repo/a.py" in s


def test_a_second_registration_of_the_hook_steps_aside(run, tmp_path):
    e = pre(tid="toolu_dup")
    _, _, clm1 = run(e)
    assert len(clm1.wait(1)) == 1                     # (before the next run rewrites the config)
    _, _, clm2 = run(e)                               # same event again: the other registration
    time.sleep(0.5)
    assert clm2.systemone == [] and clm2.decisions == []


# ── subagents ────────────────────────────────────────────────────────────────

LONG = "Search the repo for where settings are parsed. " + "Report file and line. " * 60


def agent_call(model=None, tid="toolu_a", prompt=None):
    ti = {"description": "Find the config loader", "prompt": prompt or "Search the repo for where settings are parsed.",
          "subagent_type": "Explore"}
    if model:
        ti["model"] = model
    return pre(tool="Agent", tool_input=ti, tid=tid)


def test_agent_calls_also_get_a_model_tier_decision(run):
    p, took, clm = run(agent_call(model="haiku"), probs={"haiku": 0.7, "sonnet": 0.2, "opus": 0.1})
    assert p.stdout == "" and took < 1.0
    recs = {r["workflow"]: r for r in clm.wait(2)}
    sub = recs["routing/claude-code-subagents"]
    assert sub["id"] == "toolu_a:model" and recs["routing/claude-code-tools"]["id"] == "toolu_a"
    assert sub["state"] == {"task": "Find the config loader",
                            "instructions": "Search the repo for where settings are parsed.",
                            "subagent type": "Explore"}             # the requested model is not a feature
    assert sub["baseline"] == {"route": {"label": "haiku"}} and sub["meta"]["requested_model"] == "haiku"
    assert set(sub["questions"]["route"]["criteria"]) == {"haiku", "sonnet", "opus"} and sub["acted"] == "baseline"
    questions = {b["questions"]["route"]["instructions"] for _, b in clm.systemone}
    assert len(questions) == 2                                      # the risk and the tier question


def test_unset_or_unknown_models_have_no_baseline(run):
    for model, tid in ((None, "toolu_b"), ("fable", "toolu_c")):
        _, _, clm = run(agent_call(model=model, tid=tid))
        sub = next(r for r in clm.wait(2) if r["workflow"] == "routing/claude-code-subagents")
        assert "baseline" not in sub and sub["meta"]["requested_model"] == (model or "not set")


def test_other_tools_get_no_tier_decision(run):
    _, _, clm = run(pre())
    time.sleep(0.5)
    assert [r["workflow"] for r in clm.wait(1)] == ["routing/claude-code-tools"]


def test_raw_subagent_payloads_are_logged_locally_when_asked(run, tmp_path):
    log = tmp_path / "events.jsonl"
    extra = {"raw_log": True, "raw_log_path": str(log)}
    _, _, first = run(agent_call(tid="toolu_r"), extra=extra)
    first.wait(2)                                    # (before the next run rewrites the config)
    stop = {"hook_event_name": "SubagentStop", "session_id": "s1", "agent_id": "ag1", "agent_type": "Explore",
            "last_assistant_message": "done"}
    _, _, clm = run(stop, extra=extra)
    run(stop, extra=extra)                                           # the second registration
    time.sleep(0.5)
    lines = [json.loads(line) for line in log.read_text().splitlines()]
    assert [e["hook_event_name"] for e in lines] == ["PreToolUse", "SubagentStop"]
    assert lines[1]["agent_id"] == "ag1" and "logged_at" in lines[1] and oct(log.stat().st_mode)[-3:] == "600"
    assert clm.systemone == [] and clm.decisions == []               # raw events never leave the machine


def test_no_raw_log_unless_asked(run, tmp_path):
    run({"hook_event_name": "SubagentStop", "agent_id": "ag2"}, extra={"raw_log_path": str(tmp_path / "x.jsonl")})
    time.sleep(0.3)
    assert not (tmp_path / "x.jsonl").exists()


def test_agent_results_report_the_tier_that_ran_and_the_cost(run):
    e = dict(agent_call(tid="toolu_done"), hook_event_name="PostToolUse", tool_response={
        "status": "completed", "resolvedModel": "claude-haiku-4-5-20251001", "totalTokens": 37739,
        "totalDurationMs": 6817, "totalToolUseCount": 1, "usage": {"output_tokens": 384},
        "toolStats": {"readCount": 1, "editFileCount": 0}, "content": [{"type": "text", "text": "secret answer"}]})
    _, _, clm = run(e)
    got = clm.wait(3)
    by = {(x.get("event"), x["id"]): x for x in got}
    assert by[("baseline", "toolu_done")]["label"] == "allow"                  # the tool-risk baseline
    assert by[("baseline", "toolu_done:model")]["label"] == "haiku"            # the tier that ran
    run_ = by[("outcome", "toolu_done:model")]["run"]
    assert run_ == {"status": "completed", "resolved_model": "claude-haiku-4-5-20251001", "total_tokens": 37739,
                    "output_tokens": 384, "duration_ms": 6817, "tool_uses": 1,
                    "tool_stats": {"readCount": 1, "editFileCount": 0}}
    assert "secret answer" not in json.dumps(got)                              # the subagent's output stays local


@pytest.mark.parametrize("model, tier", [("claude-opus-5-5", "opus"), ("claude-fable-5-1", "opus"),
                                         ("claude-sonnet-5", "sonnet"), ("some-other-model", None), (None, None)])
def test_tier_of(model, tier):
    assert hook.tier_of(model) == tier


# ── subagents, active ────────────────────────────────────────────────────────

ACTIVE = {"subagent_mode": "active", "subagent_min_prompt_chars": 0}
SONNET = {"haiku": 0.02, "sonnet": 0.95, "opus": 0.03}


def test_active_downgrades_an_inherited_model(run):
    p, _, clm = run(agent_call(tid="toolu_d1"), probs=SONNET, extra=ACTIVE)
    out = json.loads(p.stdout)["hookSpecificOutput"]
    assert out["updatedInput"] == {**agent_call()["tool_input"], "model": "sonnet"}   # full input, model set
    assert "permissionDecision" not in out                                          # permissions untouched
    sub = next(r for r in clm.wait(2) if r["workflow"] == "routing/claude-code-subagents")
    assert (sub["acted"], sub["mode"], sub["meta"]["applied_model"], sub["meta"]["why"]) == \
        ("clm", "active", "sonnet", "opus -> sonnet")


@pytest.mark.parametrize("event, probs, env, why", [
    (agent_call(model="opus", tid="toolu_d2"), SONNET, {}, "Claude set the model"),
    (dict(agent_call(tid="toolu_d3"), tool_input={**agent_call()["tool_input"], "subagent_type": "superpowers:code-reviewer"}),
     SONNET, {}, "plugin agent"),
    (agent_call(tid="toolu_d4"), {"haiku": 0.1, "sonnet": 0.8, "opus": 0.1}, {}, "below the threshold"),
    (agent_call(tid="toolu_d5"), {"haiku": 0.02, "sonnet": 0.03, "opus": 0.95}, {}, "not cheaper"),     # never upgrades
    (agent_call(tid="toolu_d6"), SONNET, {"CLAUDE_CODE_SUBAGENT_MODEL": "sonnet"}, "not cheaper"),
])
def test_active_leaves_the_call_alone(run, event, probs, env, why):
    p, _, clm = run(event, probs=probs, extra=ACTIVE, env_extra=env)
    assert p.stdout == ""
    sub = next(r for r in clm.wait(2) if r["workflow"] == "routing/claude-code-subagents")
    assert sub["meta"]["why"] == why and sub["meta"]["applied_model"] is None and sub["acted"] == "baseline"


def test_active_respects_the_env_default_tier(run):
    p, _, _ = run(agent_call(tid="toolu_d7"), probs={"haiku": 0.96, "sonnet": 0.02, "opus": 0.02}, extra=ACTIVE,
                  env_extra={"CLAUDE_CODE_SUBAGENT_MODEL": "sonnet"})
    assert json.loads(p.stdout)["hookSpecificOutput"]["updatedInput"]["model"] == "haiku"


@pytest.mark.parametrize("front, rewritten", [("model: opus\n", False), ("", True)])
def test_active_respects_agent_definitions(run, tmp_path, front, rewritten):
    d = tmp_path / "proj" / ".claude" / "agents"
    d.mkdir(parents=True)
    (d / "researcher.md").write_text(f"---\nname: researcher\ndescription: Researches things\n{front}---\nBody\n")
    e = agent_call(tid=f"toolu_def{int(rewritten)}")
    e = dict(e, cwd=str(tmp_path / "proj"), tool_input={**e["tool_input"], "subagent_type": "researcher"})
    p, _, clm = run(e, probs=SONNET, extra=ACTIVE)
    assert (p.stdout != "") == rewritten
    if not rewritten:
        sub = next(r for r in clm.wait(2) if r["workflow"] == "routing/claude-code-subagents")
        assert sub["meta"]["why"] == "the agent definition sets the model"


def test_active_times_out_to_the_original_model(run):
    p, took, clm = run(agent_call(tid="toolu_d8"), probs=SONNET, delay=3,
                       extra={**ACTIVE, "subagent_timeout": 0.5})
    assert p.stdout == "" and took < 2.5
    sub = next(r for r in clm.wait(2, timeout=15) if r["workflow"] == "routing/claude-code-subagents")
    assert sub["meta"]["why"] == "CLM error"


def test_shadow_subagent_mode_never_rewrites(run):
    p, _, _ = run(agent_call(tid="toolu_d9"), probs=SONNET)
    assert p.stdout == ""


@pytest.mark.parametrize("extra, sent", [({}, "content-free"), ({"calibrate": "none"}, None)])
def test_calibration_is_requested_by_default(run, extra, sent):
    _, _, clm = run(pre(tid=f"toolu_cal_{sent}"), extra=extra)
    clm.wait(1)
    assert clm.systemone[0][1].get("calibrate") == sent


def test_the_subagent_question_uses_its_own_head_calibration_and_thresholds(run):
    extra = {**ACTIVE, "subagent_model": "subagent-tier", "subagent_calibrate": "none",
             "subagent_thresholds": {"sonnet": 0.8, "haiku": 0.95}}
    p, _, clm = run(agent_call(tid="toolu_h1"), probs={"haiku": 0.05, "sonnet": 0.85, "opus": 0.10}, extra=extra)
    assert json.loads(p.stdout)["hookSpecificOutput"]["updatedInput"]["model"] == "sonnet"   # 0.85 >= 0.8
    clm.wait(2)
    bodies = {b["questions"]["route"]["instructions"]: b for _, b in clm.systemone}
    sub = bodies[hook.SUBAGENT_INSTRUCTIONS]
    assert sub["model"] == "subagent-tier" and "calibrate" not in sub
    tool = bodies[hook.INSTRUCTIONS]
    assert tool["model"] == "clm-latest" and tool["calibrate"] == "content-free"         # the tool gate unchanged
    p, _, _ = run(agent_call(tid="toolu_h2"), probs={"haiku": 0.90, "sonnet": 0.05, "opus": 0.05}, extra=extra)
    assert p.stdout == ""                                                                  # 0.90 < haiku's 0.95



def test_short_prompts_are_never_downgraded(run):
    extra = {"subagent_mode": "active"}                              # the default minimum (1000 chars)
    p, _, clm = run(agent_call(tid="toolu_s1"), probs=SONNET, extra=extra)
    assert p.stdout == ""
    sub = next(r for r in clm.wait(2) if r["workflow"] == "routing/claude-code-subagents")
    assert sub["meta"]["why"] == "prompt shorter than the trained range"
    p, _, _ = run(agent_call(tid="toolu_s2", prompt=LONG), probs=SONNET, extra=extra)
    assert json.loads(p.stdout)["hookSpecificOutput"]["updatedInput"]["model"] == "sonnet"


def test_subagent_state_hides_the_prompt_length():
    a = hook.subagent_state({"tool_input": {"prompt": "x" * 1500}})["instructions"]
    b = hook.subagent_state({"tool_input": {"prompt": "x" * 9000}})["instructions"]
    assert a == b == "x" * hook.SUBAGENT_MAX_INSTRUCTIONS + " …"


def test_clipping_keeps_the_start_and_the_end():
    cmd = "cd repo && " + "echo filler; " * 1000 + "git push origin main"
    out = hook.clip(cmd, hook.MAX_INPUT)
    assert out.startswith("cd repo && ") and out.endswith("git push origin main")
    assert re.search(r"… \[\d+ more characters\] …", out) and len(out) < hook.MAX_INPUT + 50


def test_a_clipped_state_still_matches_the_labelers_skip_pattern():
    from clm.decisions_cli import CLIPPED
    s = hook.describe_input({"command": "x" * 20000})
    assert re.search(CLIPPED, s)


# ── accept when confident, escalate when unsure ──────────────────────────────

FAKE_JUDGE = r'''
import json, os, sys, time
prompt = sys.stdin.read()
with open(os.environ["FAKE_JUDGE_LOG"], "a") as f:
    f.write(json.dumps({"prompt": prompt[:200]}) + "\n")
time.sleep(float(os.environ.get("FAKE_JUDGE_SLEEP", "0")))
ans = {"label": os.environ["FAKE_JUDGE_LABEL"], "confidence": "high", "reason": "fake"}
print(json.dumps({"type": "result", "result": "```json\n" + json.dumps(ans) + "\n```"}))
'''


@pytest.fixture
def fake_judge(tmp_path):
    f = tmp_path / "judge.py"
    f.write_text(FAKE_JUDGE)
    log = tmp_path / "judge.log"

    def calls():
        return len(log.read_text().splitlines()) if log.exists() else 0
    return {"cmd": f"{sys.executable} {f}", "log": str(log), "calls": calls}


UNSURE = {"haiku": 0.40, "sonnet": 0.35, "opus": 0.25}


def esc_run(run, fake_judge, event, probs, label, extra=None, sleep="0"):
    cfg = {**ACTIVE, "judge_cmd": fake_judge["cmd"], "judge_name": "fake", **(extra or {})}
    return run(event, probs=probs, extra=cfg,
               env_extra={"FAKE_JUDGE_LABEL": label, "FAKE_JUDGE_LOG": fake_judge["log"], "FAKE_JUDGE_SLEEP": sleep})


def test_confident_subagent_picks_never_reach_the_judge(run, fake_judge):
    p, _, _ = esc_run(run, fake_judge, agent_call(tid="toolu_e1"), SONNET, "haiku")
    assert json.loads(p.stdout)["hookSpecificOutput"]["updatedInput"]["model"] == "sonnet"
    assert fake_judge["calls"]() == 0


def test_uncertain_subagent_picks_are_escalated(run, fake_judge):
    p, _, clm = esc_run(run, fake_judge, agent_call(tid="toolu_e2"), UNSURE, "haiku")
    assert json.loads(p.stdout)["hookSpecificOutput"]["updatedInput"]["model"] == "haiku"
    assert fake_judge["calls"]() == 1
    got = clm.wait(3)
    sub = next(r for r in got if r.get("workflow") == "routing/claude-code-subagents")
    assert sub["acted"] == "judge" and sub["meta"]["why"] == "judge (fake): opus -> haiku"
    assert sub["escalation"]["label"] == "haiku" and sub["escalation"]["latency_ms"] >= 0
    out = next(e for e in got if e.get("event") == "outcome" and e["id"] == "toolu_e2:model")
    assert (out["label"], out["source"]) == ("haiku", "llm:fake-escalation")


def test_a_judge_that_says_opus_changes_nothing(run, fake_judge):
    p, _, _ = esc_run(run, fake_judge, agent_call(tid="toolu_e3"), UNSURE, "opus")
    assert p.stdout == "" and fake_judge["calls"]() == 1


@pytest.mark.parametrize("event", [agent_call(model="opus", tid="toolu_e4"),
                                   agent_call(tid="toolu_e5", prompt="short")])
def test_untouchable_calls_never_reach_the_judge(run, fake_judge, event):
    p, _, _ = esc_run(run, fake_judge, event, UNSURE, "haiku", extra={"subagent_min_prompt_chars": 1000})
    assert p.stdout == "" and fake_judge["calls"]() == 0


def test_a_slow_judge_leaves_the_call_alone(run, fake_judge):
    p, took, clm = esc_run(run, fake_judge, agent_call(tid="toolu_e6"), UNSURE, "haiku",
                           extra={"judge_timeout": 0.5}, sleep="3")
    assert p.stdout == "" and took < 2.5
    sub = next(r for r in clm.wait(2) if r.get("workflow") == "routing/claude-code-subagents")
    assert "TimeoutExpired" in sub["escalation"]["error"] and sub["acted"] == "baseline"


def test_a_judge_that_cannot_tell_leaves_the_call_alone(run, fake_judge):
    p, _, clm = esc_run(run, fake_judge, agent_call(tid="toolu_e7"), UNSURE, "not_observable")
    assert p.stdout == "" and fake_judge["calls"]() == 1
    sub = next(r for r in clm.wait(2) if r.get("workflow") == "routing/claude-code-subagents")
    assert sub["escalation"]["abstained"] is True and "error" not in sub["escalation"]
    assert sub["acted"] == "baseline"


LEANS_REVIEW = {"allow": 0.30, "review": 0.60, "block": 0.10}


def test_tool_escalation_is_off_by_default(run, fake_judge):
    p, _, _ = run(pre(tid="toolu_t1"), mode="active", probs=LEANS_REVIEW, extra={"judge_cmd": fake_judge["cmd"]},
                  env_extra={"FAKE_JUDGE_LABEL": "review", "FAKE_JUDGE_LOG": fake_judge["log"]})
    assert p.stdout == "" and fake_judge["calls"]() == 0


@pytest.mark.parametrize("label, decision", [("review", "ask"), ("block", "deny"), ("allow", None)])
def test_uncertain_tool_calls_leaning_risky_are_escalated_when_enabled(run, fake_judge, label, decision):
    p, _, clm = run(pre(tid=f"toolu_t2{label}"), mode="active", probs=LEANS_REVIEW,
                    extra={"judge_cmd": fake_judge["cmd"], "judge_name": "fake", "tool_escalate": True},
                    env_extra={"FAKE_JUDGE_LABEL": label, "FAKE_JUDGE_LOG": fake_judge["log"]})
    assert fake_judge["calls"]() == 1
    if decision:
        out = json.loads(p.stdout)["hookSpecificOutput"]
        assert out["permissionDecision"] == decision and "escalation judge (fake)" in out["permissionDecisionReason"]
    else:
        assert p.stdout == ""
    rec = next(r for r in clm.wait(2) if r.get("workflow") == "routing/claude-code-tools")
    assert rec["escalation"]["label"] == label and rec["acted"] == ("judge" if decision else "baseline")


def test_uncertain_allows_are_not_escalated(run, fake_judge):
    run(pre(tid="toolu_t3"), mode="active", probs={"allow": 0.5, "review": 0.3, "block": 0.2},
        extra={"judge_cmd": fake_judge["cmd"], "tool_escalate": True},
        env_extra={"FAKE_JUDGE_LABEL": "review", "FAKE_JUDGE_LOG": fake_judge["log"]})
    assert fake_judge["calls"]() == 0


# ── behavior checks at Stop ──────────────────────────────────────────────────

CB = hook.CB
BEHAVIORS = list(json.load(open(os.path.join(os.path.dirname(HOOK), "behaviors.json"))))
ABSENT = {"present": 0.05, "absent": 0.9, "not_observable": 0.05}


def transcript(tmp_path, final="Done: the tests pass now."):
    lines = [
        {"type": "user", "message": {"role": "user", "content": "Rename the config loader."}},
        {"type": "assistant", "message": {"role": "assistant", "model": "claude-x", "content": [{"type": "text", "text": "Renamed."}]}},
        {"type": "user", "isMeta": True, "message": {"role": "user", "content": "<system-reminder>ignore</system-reminder>"}},
        {"type": "user", "message": {"role": "user", "content": "<command-name>/clear</command-name>"}},
        {"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": "Now fix the failing test."}]}},
        {"type": "assistant", "message": {"role": "assistant", "model": "claude-x", "content": [
            {"type": "thinking", "thinking": "hmm"},
            {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "export API_KEY=sk-ant-abcdefghijklmnopqrstuvwxyz0123"}}]}},
        {"type": "user", "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": [{"type": "text", "text": "ok"}]}]}},
        {"type": "assistant", "message": {"role": "assistant", "model": "claude-x", "content": [{"type": "text", "text": final}],
                                          "usage": {"input_tokens": 10, "cache_read_input_tokens": 90, "output_tokens": 7}}},
    ]
    f = tmp_path / "t.jsonl"
    f.write_text("\n".join(json.dumps(x) for x in lines) + "\nnot json\n")
    return str(f)


def stop(tmp_path, **kw):
    return {"hook_event_name": "Stop", "session_id": "s1", "transcript_path": transcript(tmp_path),
            "stop_hook_active": False, "last_assistant_message": "Done: the tests pass now.", **kw}


def test_trace_of_keeps_the_turn_and_the_prompts_before_it(tmp_path):
    meta, msgs, out = CB.trace_of(transcript(tmp_path))
    assert meta == {"model": "claude-x", "status": "success", "prompt_tokens": 100, "completion_tokens": 7,
                    "tools_defined": None}
    assert [m["role"] for m in msgs] == ["user", "user", "assistant", "tool"]
    assert msgs[0]["content"] == "Rename the config loader." and msgs[1]["content"] == "Now fix the failing test."
    assert msgs[2]["tool_calls"][0]["name"] == "Bash" and "thinking" not in json.dumps(msgs)
    assert out["content"] == "Done: the tests pass now."
    text = CB.render(meta, msgs, out)
    assert text.startswith("call metadata: model=claude-x, status=success, prompt_tokens=100, completion_tokens=7")
    assert text.endswith("[assistant output] Done: the tests pass now.") and len(text) <= CB.BUDGET + 200


def test_behavior_checks_are_off_by_default(run, tmp_path):
    p, _, clm = run(stop(tmp_path))
    assert p.stdout == "" and clm.systemone == []


def test_shadow_logs_every_behavior_and_never_blocks(run, tmp_path):
    probs = {k: ABSENT for k in BEHAVIORS}
    probs["unverified_success"] = {"present": 0.97, "absent": 0.02, "not_observable": 0.01}
    p, _, clm = run(stop(tmp_path), extra={"behavior_mode": "shadow"}, probs=probs)
    assert p.stdout == ""
    [(_, body)] = clm.systemone
    assert set(body["questions"]) == set(BEHAVIORS) and body["model"] == "behavior-v1"
    assert "sk-ant-abcdef" not in body["state"] and "Now fix the failing test." in body["state"]
    recs = clm.wait(len(BEHAVIORS))
    assert {r["meta"]["behavior"] for r in recs} == set(BEHAVIORS)
    assert all(r["workflow"] == "behavior/claude-code" and r["acted"] == "baseline" for r in recs)
    assert all(set(r["questions"]) == {"route"} for r in recs)       # the labeler and report read "route"


def test_active_blocks_the_stop_on_a_confident_flag(run, tmp_path):
    probs = {k: ABSENT for k in BEHAVIORS}
    probs["unverified_success"] = {"present": 0.95, "absent": 0.04, "not_observable": 0.01}
    p, _, clm = run(stop(tmp_path), extra={"behavior_mode": "active"}, probs=probs)
    out = json.loads(p.stdout)
    assert out["decision"] == "block" and "unverified_success (p=0.95)" in out["reason"]
    assert "Run the relevant test" in out["reason"] and "say so in one sentence and stop" in out["reason"]
    rec = next(r for r in clm.wait(len(BEHAVIORS)) if r["meta"]["behavior"] == "unverified_success")
    assert rec["acted"] == "clm"


def test_never_blocks_twice_in_a_row(run, tmp_path):
    probs = {k: {"present": 0.99, "absent": 0.005, "not_observable": 0.005} for k in BEHAVIORS}
    p, _, clm = run(stop(tmp_path, stop_hook_active=True), extra={"behavior_mode": "active"}, probs=probs)
    assert p.stdout == ""
    assert all(r["acted"] == "baseline" and r["meta"]["stop_hook_active"] for r in clm.wait(len(BEHAVIORS)))


@pytest.mark.parametrize("label, blocks", [("present", True), ("absent", False), ("not_observable", False)])
def test_unsure_flags_go_to_the_judge(run, tmp_path, fake_judge, label, blocks):
    probs = {k: ABSENT for k in BEHAVIORS}
    probs["stale_task"] = {"present": 0.7, "absent": 0.25, "not_observable": 0.05}
    p, _, clm = run(stop(tmp_path), probs=probs,
                    extra={"behavior_mode": "active", "judge_cmd": fake_judge["cmd"], "judge_name": "fake"},
                    env_extra={"FAKE_JUDGE_LABEL": label, "FAKE_JUDGE_LOG": fake_judge["log"]})
    assert fake_judge["calls"]() == 1                       # only the unsure behavior is escalated
    if blocks:
        assert "stale_task (judge fake)" in json.loads(p.stdout)["reason"]
    else:
        assert p.stdout == ""
    rec = next(r for r in clm.wait(len(BEHAVIORS)) if r.get("meta", {}).get("behavior") == "stale_task")
    assert rec["acted"] == ("judge" if blocks else "baseline")


def test_a_clm_error_never_blocks(run, tmp_path):
    p, _, clm = run(stop(tmp_path), extra={"behavior_mode": "active", "base_url": "http://127.0.0.1:9"})
    assert p.returncode == 0 and p.stdout == ""


def test_the_same_stop_is_checked_once(run, tmp_path):
    ev = stop(tmp_path)
    run(ev, extra={"behavior_mode": "shadow"})
    p, _, clm = run(ev, extra={"behavior_mode": "shadow"})
    assert clm.systemone == []


def test_a_behavior_can_set_its_own_thresholds(run, tmp_path, fake_judge):
    beh = json.load(open(os.path.join(os.path.dirname(HOOK), "behaviors.json")))
    beh = {"stale_task": {**beh["stale_task"], "escalate_from": 0.3, "threshold": 0.99}}
    f = tmp_path / "beh.json"
    f.write_text(json.dumps(beh))
    probs = {"stale_task": {"present": 0.95, "absent": 0.04, "not_observable": 0.01}}
    p, _, _ = run(stop(tmp_path), probs=probs,
                  extra={"behavior_mode": "active", "behaviors_file": str(f), "judge_cmd": fake_judge["cmd"]},
                  env_extra={"FAKE_JUDGE_LABEL": "absent", "FAKE_JUDGE_LOG": fake_judge["log"]})
    assert p.stdout == "" and fake_judge["calls"]() == 1       # 0.95 is below its own 0.99: the judge said no


# ── as a Claude Code plugin ──────────────────────────────────────────────────

def plugin_env(tmp_path, **options):
    env = {"CLAUDE_PLUGIN_ROOT": os.path.dirname(HOOK), "CLAUDE_PLUGIN_DATA": str(tmp_path / "pdata")}
    env.update({f"CLAUDE_PLUGIN_OPTION_{k.upper()}": v for k, v in options.items()})
    return env


def test_plugin_settings_and_switches(tmp_path, monkeypatch):
    monkeypatch.setenv("CLM_HOOK_CONFIG", str(tmp_path / "none.json"))
    for k, v in plugin_env(tmp_path, api_key="k-plugin", subagent_downgrades="false", behavior_checks="true",
                           tool_gate="true").items():
        monkeypatch.setenv(k, v)
    cfg = hook.load_config()
    assert (cfg["api_key"], cfg["base_url"]) == ("k-plugin", "https://clm.metatheory.dev")
    assert (cfg["subagent_mode"], cfg["behavior_mode"], cfg["mode"]) == ("shadow", "active", "active")
    assert cfg["subagent_model"] == "subagent-tier-v2" and cfg["behavior_model"] == "behavior-v1"
    # the config file tunes; the plugin's own settings still win for the key and the switches
    (tmp_path / "c.json").write_text(json.dumps({"api_key": "k-file", "threshold": 0.8, "subagent_mode": "active"}))
    monkeypatch.setenv("CLM_HOOK_CONFIG", str(tmp_path / "c.json"))
    cfg = hook.load_config()
    assert (cfg["api_key"], cfg["threshold"], cfg["subagent_mode"]) == ("k-plugin", 0.8, "shadow")


def test_without_the_plugin_nothing_changes(tmp_path, monkeypatch):
    monkeypatch.setenv("CLM_HOOK_CONFIG", str(tmp_path / "none.json"))
    for k in [k for k in os.environ if k.startswith("CLAUDE_PLUGIN_")]:
        monkeypatch.delenv(k)
    monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_API_KEY", "ignored")
    cfg = hook.load_config()
    assert cfg["mode"] == "off" and cfg["api_key"] is None


def test_a_downgrade_tells_the_user(run, tmp_path):
    p, _, _ = run(agent_call(tid="toolu_n1"), probs=SONNET, extra=ACTIVE, env_extra=plugin_env(tmp_path))
    out = json.loads(p.stdout)
    assert out["hookSpecificOutput"]["updatedInput"]["model"] == "sonnet"
    assert out["systemMessage"].startswith("CLM: this subagent runs on sonnet")
    [a] = [json.loads(x) for x in (tmp_path / "pdata" / "actions.jsonl").read_text().splitlines()]
    assert a["kind"] == "subagent" and a["session_id"] == "s1"


def test_a_behavior_block_tells_the_user(run, tmp_path):
    probs = {k: ABSENT for k in BEHAVIORS}
    probs["stale_task"] = {"present": 0.97, "absent": 0.02, "not_observable": 0.01}
    p, _, _ = run(stop(tmp_path), extra={"behavior_mode": "active"}, probs=probs)
    out = json.loads(p.stdout)
    assert out["decision"] == "block" and out["systemMessage"].startswith("CLM: flagged stale_task; the agent was asked")


def test_pause_resume_and_status(run, tmp_path):
    env = {**{k: v for k, v in os.environ.items() if not k.startswith(("CLM_", "CLAUDE_PLUGIN"))},
           "CLM_HOOK_CONFIG": str(tmp_path / "none.json"), "HOME": str(tmp_path / "home")}
    cli = lambda *a: subprocess.run([sys.executable, HOOK, *a, "--data", str(tmp_path / "pdata")],  # noqa: E731
                                    capture_output=True, text=True, env=env, timeout=30).stdout
    out = cli("--pause")
    assert "CLM paused" in out and "PAUSED" in out and (tmp_path / "pdata" / "paused").exists()
    # paused: the hook does nothing at all
    p, _, clm = run(agent_call(tid="toolu_n2"), probs=SONNET, extra=ACTIVE, env_extra=plugin_env(tmp_path))
    assert p.stdout == "" and clm.systemone == []
    assert "CLM resumed" in cli("--resume") and not (tmp_path / "pdata" / "paused").exists()
    run(agent_call(tid="toolu_n3"), probs=SONNET, extra=ACTIVE, env_extra=plugin_env(tmp_path))
    out = cli("--status")
    assert "1 subagent downgrades" in out and "this subagent runs on sonnet" in out and "agent-key" not in out


def test_the_plugin_files_agree_with_the_hook():
    root = os.path.dirname(HOOK)
    manifest = json.load(open(os.path.join(root, ".claude-plugin", "plugin.json")))
    assert set(manifest["userConfig"]) == set(hook.PLUGIN_OPTIONS) | set(hook.PLUGIN_SWITCHES)
    assert manifest["userConfig"]["api_key"]["sensitive"] is True
    hooks = json.load(open(os.path.join(root, "hooks", "hooks.json")))["hooks"]
    for event, groups in hooks.items():
        [h] = groups[0]["hooks"]
        assert "${CLAUDE_PLUGIN_ROOT}/clm_hook.py" in h["command"]
        assert bool(h.get("async")) == (event not in ("PreToolUse", "Stop", "SubagentStop")), event
    assert set(hooks) == {"PreToolUse", "Stop", "SubagentStop", "PermissionRequest", "PermissionDenied",
                          "PostToolUse", "PostToolUseFailure", "SubagentStart"}
    market = json.load(open(os.path.join(os.path.dirname(os.path.dirname(root)), ".claude-plugin", "marketplace.json")))
    assert market["plugins"][0]["source"] == "./integrations/claude_code"
