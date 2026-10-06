#!/usr/bin/env python3
"""MCP server (stdio) with one tool for Codex: clm_pick_model, the model a subagent task needs.

Codex encrypts the task it hands a spawned agent, so a hook cannot read it; the main agent
describes the task to this tool instead (AGENTS.md tells it to, before `spawn_agent`). CLM's
subagent-tier head answers haiku / sonnet / opus; each tier maps to a Codex model and reasoning
effort (``codex.pick_models`` in ~/.config/clm/claude-code.json). The main chat runs on a small
model and hands work up: an unsure lookup goes to the middle tier, while the top tier needs CLM to be
sure (``pick_top_threshold``, 0.8: Fable's labels of 118 picks put it there), since it costs the most. Every pick is logged as
``routing/codex-model-picks``. Standard library only; installed by integrations/codex/install.py.
"""
from __future__ import annotations

import json
import os
import sys
import uuid
import atexit
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)                                            # installed: next to clm_hook.py
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "claude_code"))   # in the repo
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(HERE)), "src", "clm"))
os.environ.setdefault("CLM_HOOK_RUNTIME", "codex")
import clm_hook as H  # noqa: E402
import turn_picker as TP  # noqa: E402

WORKFLOW = "routing/codex-model-picks"
TIERS = ("haiku", "sonnet", "opus")
DEFAULT_MODELS = {"haiku": {"model": "gpt-5.6-luna", "reasoning_effort": "low"},
                  "sonnet": {"model": "gpt-5.6-terra", "reasoning_effort": "medium"},
                  "opus": {"model": "gpt-5.6-sol", "reasoning_effort": "high"}}
TOOL = {
    "name": "clm_pick_model",
    "description": ("Pick the model and reasoning effort for a subagent before you call spawn_agent. Pass the task "
                    "exactly as you will give it to the subagent. Returns {model, reasoning_effort, tier, why}; "
                    "pass model and reasoning_effort to spawn_agent."),
    "inputSchema": {"type": "object", "additionalProperties": False, "required": ["task"],
                    "properties": {"task": {"type": "string", "description": "the full task message for the subagent"},
                                   "task_name": {"type": "string", "description": "the short name you will give spawn_agent"}}},
}
TURN_TOOL = {
    "name": "clm_pick_turn",
    "description": ("Pick one supplied provider/model/effort configuration for a chat turn using the trained "
                    "capability tier and current budget snapshots. The selected tuple always comes from candidates."),
    "inputSchema": {
        "type": "object", "additionalProperties": False,
        "required": ["task", "caller", "candidates"],
        "properties": {
            "task": {"type": "string"},
            "context": {"type": "string"},
            "caller": {"type": "string"},
            "prefer": {"enum": ["codex", "claude"]},
            "requested_tier": {"enum": list(TP.TIERS)},
            "candidates": {
                "type": "array", "minItems": 1,
                "items": {
                    "type": "object", "additionalProperties": False,
                    "required": ["provider", "model", "effort", "tier"],
                    "properties": {
                        "provider": {"enum": ["codex", "claude"]},
                        "model": {"type": "string"}, "effort": {"type": "string"},
                        "tier": {"enum": list(TP.TIERS)}, "budget": {"type": ["object", "null"]},
                    },
                },
            },
        },
    },
}

_AUDIT = None
_AUDIT_LOCK = threading.Lock()


def _audit_sink(cfg: dict):
    global _AUDIT
    with _AUDIT_LOCK:
        if _AUDIT is None:
            upload_cfg = dict(cfg)
            _AUDIT = TP.AuditQueue(lambda record: H.post(upload_cfg, "/v1/decisions", record, 0.25), maxsize=64)
    return _AUDIT.submit


def _close_audit():
    if _AUDIT is not None:
        _AUDIT.close(timeout=0.25)


atexit.register(_close_audit)


def pick(cfg: dict, task: str, task_name: str = "") -> dict:
    models = {**DEFAULT_MODELS, **(cfg.get("pick_models") or {})}
    threshold = float(cfg.get("pick_threshold", 0.8))
    top = float(cfg.get("pick_top_threshold", 0.8))
    state = H.subagent_state({"tool_input": {"description": task_name, "prompt": task, "subagent_type": "general-purpose"}})
    clm = H.classify_subagent(cfg, state, float(cfg.get("subagent_timeout", 1.5)) * 2)
    if "error" in clm:
        tier, why = "sonnet", f"CLM unavailable ({clm['error'][:80]}); using the middle tier"
    elif clm["choice"] == "opus":               # the top tier costs most: only when CLM is sure
        tier = "opus" if clm["probability"] >= top else "sonnet"
        why = (f"CLM: opus (p={clm['probability']:.2f})" if tier == "opus" else
               f"CLM leaned opus (p={clm['probability']:.2f}), below the {top:.2f} bar for the top tier, so sonnet")
    elif clm["choice"] == "haiku" and clm["probability"] < threshold:   # an unsure lookup: err toward quality
        tier, why = "sonnet", f"CLM leaned haiku (p={clm['probability']:.2f}) but was unsure, so sonnet"
    else:
        tier, why = clm["choice"], f"CLM: {clm['choice']} (p={clm['probability']:.2f})"
    if len(task) < int(cfg.get("subagent_min_prompt_chars", 1000)) and tier == "haiku":
        tier, why = "sonnet", why + "; the task is too short for CLM to judge a lookup, so sonnet"
    out = {"tier": tier, **models[tier], "why": why}
    try:
        H.post(cfg, "/v1/decisions", {
            "id": uuid.uuid4().hex, "workflow": WORKFLOW, "created_at": H.now(), "mode": "active", "threshold": top if tier == "opus" else threshold,
            "state": state, "questions": {H.QID: {"type": "choice", "instructions": H.SUBAGENT_INSTRUCTIONS,
                                                   "criteria": H.SUBAGENT_OPTIONS}},
            "clm": clm, "acted": "clm" if "error" not in clm else "baseline", "worker": tier,
            "meta": {"picked": out, "task_name": task_name}}, 5)
    except Exception:  # noqa: BLE001  (logging never blocks the pick)
        pass
    return out


def pick_turn(cfg: dict, request: dict, *, now=None) -> dict:
    """MCP/library adapter for the shared turn policy."""
    task, context = request.get("task"), request.get("context", "")
    prompt = str(task) + (("\n\nContext:\n" + str(context)) if context else "")
    state = H.subagent_state({"tool_input": {"description": "", "prompt": prompt,
                                             "subagent_type": "general-purpose"}})

    def classify(_state, timeout):
        return H.classify_subagent(cfg, state, timeout)

    jev_classifier = None
    if cfg.get("turn_pick_jev_enabled") and cfg.get("jev_api_key"):
        def classify_jev(_state, timeout):
            raw = H.jev(cfg, state, {H.QID: {"type": "choice", "instructions": H.SUBAGENT_INSTRUCTIONS,
                                             "criteria": H.SUBAGENT_OPTIONS}}, timeout)
            return H.jev_pick(raw) or {"error": "classification failed"}
        jev_classifier = classify_jev
    return TP.pick_turn(request, classify, config=cfg, now=now, jev_classifier=jev_classifier,
                        sink=_audit_sink(cfg))


def handle(msg: dict, cfg: dict) -> dict | None:
    method, mid = msg.get("method"), msg.get("id")
    if mid is None:                                          # notifications need no reply
        return None
    if method == "initialize":
        result = {"protocolVersion": (msg.get("params") or {}).get("protocolVersion", "2025-06-18"),
                  "capabilities": {"tools": {}}, "serverInfo": {"name": "clm", "version": "0.1.0"}}
    elif method == "tools/list":
        result = {"tools": [TOOL, TURN_TOOL]}
    elif method == "tools/call":
        p = msg.get("params") or {}
        args = p.get("arguments") or {}
        if p.get("name") == TURN_TOOL["name"]:
            try:
                selected = pick_turn(cfg, args)
            except ValueError as e:
                result = {"content": [{"type": "text", "text": "clm_pick_turn invalid request: " + str(e)}],
                          "isError": True}
            else:
                result = {"content": [{"type": "text", "text": json.dumps(selected)}]}
        elif p.get("name") != TOOL["name"] or not str(args.get("task", "")).strip():
            result = {"content": [{"type": "text", "text": "clm_pick_model needs a non-empty task"}], "isError": True}
        else:
            result = {"content": [{"type": "text", "text": json.dumps(pick(cfg, str(args["task"]), str(args.get("task_name", ""))))}]}
    elif method == "ping":
        result = {}
    else:
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"unknown method {method}"}}
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def main() -> int:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        cfg = H.load_config()                               # per call, so config edits apply at once
        reply = handle(msg, cfg)
        if reply is not None:
            sys.stdout.write(json.dumps(reply) + "\n")
            sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
