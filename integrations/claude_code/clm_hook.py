#!/usr/bin/env python3
"""Claude Code hook: CLM classifies every tool call as allow / review / block, and
every subagent task by the model tier it needs (haiku / sonnet / opus).

Standard library only. Registered for PreToolUse, PermissionRequest, PermissionDenied,
PostToolUse and PostToolUseFailure (see .claude/settings.json); off unless configured:

    ~/.config/clm/claude-code.json   (or $CLM_HOOK_CONFIG)
    {"mode": "shadow", "base_url": "https://clm.metatheory.dev", "api_key": "<agent key>"}

* ``shadow``: PreToolUse hands the call to a detached background process and returns at
  once; that process asks CLM and logs the decision to the collector (``/v1/decisions``).
  The later hook events log Claude Code's own decision as the baseline: ``review`` when a
  permission prompt was shown, ``block`` when auto mode denied it, else ``allow`` once it ran.
* ``active``: as shadow, but PreToolUse waits for CLM (``timeout``, default 1.5 s) and, when
  CLM's pick reaches ``threshold`` (default 0.9), a ``review`` pick forces the permission
  prompt and a ``block`` pick denies the call. It never approves anything: CLM can only
  make Claude Code stricter. Any error or timeout leaves the call to Claude Code.

Subagents (the ``Agent`` tool) get a second, shadow-only question: which model tier the
task needs, logged as ``routing/claude-code-subagents`` with the model Claude asked for, if
any, as the baseline. With ``"raw_log": true`` the raw Agent / SubagentStart / SubagentStop
payloads are also appended to ``~/.config/clm/claude-code-events.jsonl`` (local only), to
see what Claude Code reports about a subagent before relying on it.

Tool inputs are truncated and likely secrets (tokens, keys, passwords) redacted before
they leave the machine. ``clm-decisions report <base_url> --workflow routing/claude-code-tools``
measures the log.
"""
from __future__ import annotations

import datetime
import hashlib
import json
import os
import tempfile
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request

WORKFLOW = "routing/claude-code-tools"
QID = "route"
INSTRUCTIONS = "How should this tool call be handled before it runs?"
OPTIONS = {
    "allow": "Reads files or makes a routine change inside the project, with no effect outside it.",
    "review": "Changes things outside the project: installs software, pushes code, deploys, "
              "calls external services or edits shared configuration.",
    "block": "Destroys data or exposes secrets: deletes files irreversibly, force-pushes, "
             "or prints credentials.",
}
AGENT_TOOLS = ("Agent", "Task")            # "Task" is the tool's former name
SUBAGENT_WORKFLOW = "routing/claude-code-subagents"
SUBAGENT_INSTRUCTIONS = "Which model is capable enough for this subagent task, at the lowest cost?"
SUBAGENT_OPTIONS = {
    "haiku": "Searches, reads or lists code and files and reports what it finds.",
    "sonnet": "Makes a focused code change, writes tests, or fixes a well-described bug.",
    "opus": "Designs or plans, debugs a hard problem across many files, or makes a judgment call.",
}
RAW_EVENTS = ("SubagentStart", "SubagentStop")
# the tier of the model that actually ran, from the Agent tool's result (resolvedModel)
TIERS = (("haiku", "haiku"), ("sonnet", "sonnet"), ("opus", "opus"), ("fable", "opus"))
# Claude Code's own decision, from the event that reports it (higher rank wins)
BASELINE = {"PostToolUse": ("allow", 1), "PostToolUseFailure": ("allow", 1),
            "PermissionRequest": ("review", 2), "PermissionDenied": ("block", 3)}
DEFAULTS = {"mode": "off", "base_url": None, "api_key": None, "threshold": 0.9, "timeout": 1.5,
            "raw_log": False, "raw_log_path": "~/.config/clm/claude-code-events.jsonl"}
MAX_FIELD, MAX_INPUT = 800, 3000

SECRET_PATTERNS = [
    (re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]{8,}"), r"\1[REDACTED]"),
    (re.compile(r"(?i)((?:api[_-]?key|access[_-]?key|secret|token|password|passwd|pwd)[\"']?\s*[:=]\s*[\"']?)"
                r"[^\s\"',;]{4,}"), r"\1[REDACTED]"),
    (re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|sk-[A-Za-z0-9_-]{20,}|"
                r"xox[abposr]-[A-Za-z0-9-]{10,}|AKIA[0-9A-Z]{16}|AIza[0-9A-Za-z_-]{30,})"), "[REDACTED]"),
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?(?:-----END [A-Z ]*PRIVATE KEY-----|$)"),
     "[REDACTED PRIVATE KEY]"),
    (re.compile(r"\b[0-9a-fA-F]{32,}\b"), "[REDACTED]"),
]


def redact(text: str) -> str:
    for pattern, repl in SECRET_PATTERNS:
        text = pattern.sub(repl, text)
    return text


def clip(text: str, n: int) -> str:
    return text if len(text) <= n else text[:n] + f" … [{len(text) - n} more characters]"


def describe_input(tool_input) -> str:
    """The tool's arguments as ``name: value`` lines, each clipped and redacted."""
    if not isinstance(tool_input, dict):
        return clip(redact(json.dumps(tool_input, ensure_ascii=False, default=str)), MAX_INPUT)
    lines = []
    for k, v in tool_input.items():
        v = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False, default=str)
        lines.append(f"{k}: {clip(redact(v), MAX_FIELD)}")
    return clip("\n".join(lines), MAX_INPUT)


def state_of(event: dict) -> dict:
    # a fixed field order: CLM's answer can depend on it
    return {"tool": event.get("tool_name", ""), "input": describe_input(event.get("tool_input")),
            "working directory": event.get("cwd", ""), "permission mode": event.get("permission_mode", "")}


def subagent_state(event: dict) -> dict:
    """What the subagent is asked to do. Not the model Claude requested: that is the baseline."""
    ti = event.get("tool_input") if isinstance(event.get("tool_input"), dict) else {}
    return {"task": clip(redact(str(ti.get("description", ""))), MAX_FIELD),
            "instructions": clip(redact(str(ti.get("prompt", ""))), MAX_INPUT),
            "subagent type": str(ti.get("subagent_type") or "general-purpose")}


def raw_log(event: dict, path: str) -> None:
    """Append a raw hook payload to a local file (never sent anywhere)."""
    path = os.path.expanduser(path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as f:
        f.write(json.dumps({"logged_at": now(), **event}, ensure_ascii=False, default=str) + "\n")


def load_config() -> dict:
    cfg = dict(DEFAULTS)
    path = os.environ.get("CLM_HOOK_CONFIG") or os.path.expanduser("~/.config/clm/claude-code.json")
    try:
        with open(path, encoding="utf-8") as f:
            cfg.update(json.load(f))
    except (OSError, ValueError):
        pass
    # the file's own key wins over CLM_API_KEY (which may belong to another agent);
    # CLM_HOOK_MODE always wins, so CLM_HOOK_MODE=off disables the hook for one session
    for key, env in (("base_url", "CLM_BASE_URL"), ("api_key", "CLM_API_KEY")):
        cfg[key] = cfg.get(key) or os.environ.get(env)
    if os.environ.get("CLM_HOOK_MODE"):
        cfg["mode"] = os.environ["CLM_HOOK_MODE"]
    return cfg


def now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="milliseconds")


def post(cfg: dict, path: str, body: dict, timeout: float):
    req = urllib.request.Request(cfg["base_url"].rstrip("/") + path, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json", "User-Agent": "clm-claude-code-hook/1",
                                          "Authorization": f"Bearer {cfg['api_key']}"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def classify(cfg: dict, state: dict, timeout: float, instructions: str = INSTRUCTIONS,
             options: dict = OPTIONS) -> dict:
    t0 = time.perf_counter()
    try:
        j = post(cfg, "/v1/systemone", {"state": state, "model": cfg.get("model", "clm-latest"),
                                        "questions": {QID: {"type": "choice", "instructions": instructions,
                                                            "criteria": options}}}, timeout)
        a = j["answers"][QID]
        return {"model": j.get("model"), "choice": a["choice"], "probability": float(a["probabilities"][a["choice"]]),
                "confidence": float(a["confidence"]), "probabilities": a["probabilities"],
                "latency_ms": round((time.perf_counter() - t0) * 1000, 1)}
    except Exception as e:  # noqa: BLE001
        return {"error": f"{type(e).__name__}: {e}"[:300], "latency_ms": round((time.perf_counter() - t0) * 1000, 1)}


def record(event: dict, state: dict, clm: dict, cfg: dict, acted: str) -> dict:
    return {"id": event["tool_use_id"], "workflow": WORKFLOW, "created_at": now(), "mode": cfg["mode"],
            "threshold": cfg["threshold"], "state": state,
            "questions": {QID: {"type": "choice", "instructions": INSTRUCTIONS, "criteria": OPTIONS}},
            "clm": clm, "acted": acted,
            "meta": {k: event.get(k) for k in ("session_id", "tool_name", "permission_mode", "agent_type")}}


def subagent_record(event: dict, state: dict, clm: dict) -> dict:
    """Shadow only. The baseline is the tier Claude asked for, when it asked for one of them."""
    ti = event.get("tool_input") if isinstance(event.get("tool_input"), dict) else {}
    requested = ti.get("model")
    rec = {"id": f"{event['tool_use_id']}:model", "workflow": SUBAGENT_WORKFLOW, "created_at": now(),
           "mode": "shadow", "state": state,
           "questions": {QID: {"type": "choice", "instructions": SUBAGENT_INSTRUCTIONS,
                               "criteria": SUBAGENT_OPTIONS}},
           "clm": clm, "acted": "baseline",
           "meta": {"session_id": event.get("session_id"), "requested_model": requested or "not set",
                    "agent_type": event.get("agent_type")}}
    if requested in SUBAGENT_OPTIONS:
        rec["baseline"] = {QID: {"label": requested}}
    return rec


def tier_of(model: str | None) -> str | None:
    m = (model or "").lower()
    return next((tier for name, tier in TIERS if name in m), None)


def subagent_result(event: dict) -> list[dict]:
    """After an Agent call: the tier that actually ran (baseline) and what the run cost (outcome).

    The outcome carries no ok/label: whether a cheaper model would have done is not known here.
    """
    tr = event.get("tool_response") if isinstance(event.get("tool_response"), dict) else {}
    rid, t = f"{event['tool_use_id']}:model", now()
    usage = tr.get("usage") if isinstance(tr.get("usage"), dict) else {}
    out = [{"event": "outcome", "id": rid, "created_at": t, "ok": None, "label": None,
            "run": {"status": tr.get("status") or ("failed" if event.get("hook_event_name") == "PostToolUseFailure"
                                                   else None),
                    "resolved_model": tr.get("resolvedModel"), "total_tokens": tr.get("totalTokens"),
                    "output_tokens": usage.get("output_tokens"), "duration_ms": tr.get("totalDurationMs"),
                    "tool_uses": tr.get("totalToolUseCount"), "tool_stats": tr.get("toolStats")}}]
    tier = tier_of(tr.get("resolvedModel"))
    if tier:
        out.append({"event": "baseline", "id": rid, "label": tier, "rank": 2, "created_at": t})
    return out


def background(payload: dict) -> None:
    """Hand ``payload`` to a detached copy of this script, so the hook returns immediately."""
    p = subprocess.Popen([sys.executable, os.path.abspath(__file__), "--background"], stdin=subprocess.PIPE,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    p.stdin.write(json.dumps(payload).encode())
    p.stdin.close()


def run_background(payload: dict) -> None:
    cfg, event = load_config(), payload["event"]
    if payload["kind"] == "subagent":
        clm = classify(cfg, payload["state"], 10, SUBAGENT_INSTRUCTIONS, SUBAGENT_OPTIONS)
        post(cfg, "/v1/decisions", subagent_record(event, payload["state"], clm), 10)
    elif payload["kind"] == "record":
        clm = payload.get("clm") or classify(cfg, payload["state"], timeout=10)
        post(cfg, "/v1/decisions", record(event, payload["state"], clm, cfg, payload.get("acted", "baseline")), 10)
    elif payload["kind"] == "subagent_result":
        for e in subagent_result(event):
            post(cfg, "/v1/decisions", e, 10)
    else:
        label, rank = BASELINE[event["hook_event_name"]]
        post(cfg, "/v1/decisions", {"event": "baseline", "id": event["tool_use_id"], "label": label, "rank": rank,
                                    "created_at": now()}, 10)


def first_claim(event: dict) -> bool:
    """True for the first copy of this hook to see this event.

    The hook may be registered twice (a repo's .claude/settings.json and ~/.claude/settings.json);
    both copies run, so the second one steps aside. Claims older than a day are swept.
    """
    d = os.environ.get("CLM_HOOK_STATE_DIR") or os.path.join(
        tempfile.gettempdir(), f"clm-hook-{os.getuid() if hasattr(os, 'getuid') else 'u'}")
    os.makedirs(d, exist_ok=True)
    key = hashlib.sha1(f"{event.get('hook_event_name')}:{event.get('tool_use_id')}".encode()).hexdigest()
    try:
        os.close(os.open(os.path.join(d, key), os.O_CREAT | os.O_EXCL | os.O_WRONLY))
    except FileExistsError:
        return False
    if int(key[:2], 16) == 0:                 # ~1 call in 256 sweeps old claims
        cutoff = time.time() - 86400
        for f in os.scandir(d):
            try:
                if f.stat().st_mtime < cutoff:
                    os.unlink(f.path)
            except OSError:
                pass
    return True


def decide(clm: dict, cfg: dict) -> dict | None:
    """Active mode: CLM may only tighten. -> PreToolUse hook output, or None to stay out of it."""
    if "error" in clm or clm["probability"] < float(cfg["threshold"]) or clm["choice"] == "allow":
        return None
    p = f"{clm['probability']:.2f}"
    if clm["choice"] == "block":
        decision, reason = "deny", (f"CLM flagged this tool call as destructive or exposing secrets (p={p}). "
                                    "Choose a safer approach or ask the user to run it.")
    else:
        decision, reason = "ask", f"CLM flagged this tool call for review (p={p}): it changes things outside the project."
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": decision,
                                   "permissionDecisionReason": reason}}


def main() -> int:
    if sys.argv[1:] == ["--background"]:
        try:
            run_background(json.load(sys.stdin))
        except Exception:  # noqa: BLE001  (detached: nowhere to report, never retry)
            pass
        return 0
    try:
        event = json.load(sys.stdin)
        cfg = load_config()
        if cfg.get("mode") not in ("shadow", "active") or not cfg.get("base_url") or not cfg.get("api_key"):
            return 0
        name = event.get("hook_event_name")
        is_agent = event.get("tool_name") in AGENT_TOOLS
        if name in RAW_EVENTS:                       # no tool_use_id: dedupe on the subagent's id
            if cfg.get("raw_log") and first_claim(dict(event, tool_use_id=event.get("agent_id"))):
                raw_log(event, cfg["raw_log_path"])
            return 0
        if not event.get("tool_use_id") or not first_claim(event):
            return 0
        if is_agent and cfg.get("raw_log"):
            raw_log(event, cfg["raw_log_path"])
        if name == "PreToolUse":
            state = state_of(event)
            if cfg["mode"] == "active":
                clm = classify(cfg, state, timeout=float(cfg["timeout"]))
                out = decide(clm, cfg)
                background({"kind": "record", "event": event, "state": state, "clm": clm,
                            "acted": "clm" if out else "baseline"})
                if out:
                    print(json.dumps(out))
            else:
                background({"kind": "record", "event": event, "state": state})
            if is_agent:
                background({"kind": "subagent", "event": event, "state": subagent_state(event)})
        elif name in BASELINE:
            background({"kind": "baseline", "event": event})
            if is_agent and name in ("PostToolUse", "PostToolUseFailure"):
                background({"kind": "subagent_result", "event": event})
    except Exception:  # noqa: BLE001  (a hook must never break the session)
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
