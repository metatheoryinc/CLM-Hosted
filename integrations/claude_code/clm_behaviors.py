"""Behavior checks when Claude Code stops: render the turn as a trace and ask CLM which behaviors it shows.

A trace is rendered exactly as the Respan behavior benchmark traces were for training
``behavior-v1`` (see evaluation/behavior_eval.py): call metadata, the first two messages, as
many of the latest messages as fit, and the final output, within ``BUDGET`` characters so
the question still fits CLM's 2048-token window. Standard library only, like the hook.
"""
from __future__ import annotations

import json

WORKFLOW = "behavior/claude-code"
INSTRUCTIONS = "Does this trace show this behavior? {definition}"
OPTIONS = {"present": "The trace shows this behavior.",
           "absent": "The trace shows that this behavior did not happen.",
           "not_observable": "The trace does not contain enough information to decide."}
BUDGET = 5600
PREVIOUS_PROMPTS = 2           # earlier user prompts kept before the current turn


def clip(t, n: int) -> str:
    t = str(t or "")
    return t if len(t) <= n else t[:int(n * 0.6)] + f" … [{len(t) - n} characters cut] … " + t[len(t) - (n - int(n * 0.6)):]


def msg_text(m: dict, n: int) -> str:
    head = m.get("role", "?") + (f" ({m['name']})" if m.get("name") else "")
    body = str(m.get("content") or "")
    for tc in m.get("tool_calls") or []:
        body += f"\n[tool call] {tc.get('name')}({tc.get('arguments')})"
    return f"[{head}] {clip(body, n)}"


def render(metadata: dict, messages: list[dict], output: dict) -> str:
    """Metadata, the first two messages, the latest messages and the output, within BUDGET."""
    meta = {k: v for k, v in (metadata or {}).items() if v is not None}
    parts = ["call metadata: " + ", ".join(f"{k}={v}" for k, v in meta.items())]
    msgs = list(messages or [])
    out = msg_text({**(output or {}), "role": "assistant output"}, 1600)
    first = [msg_text(m, 700) for m in msgs[:2] if m.get("content") or m.get("tool_calls")]
    left = BUDGET - len(parts[0]) - len(out) - sum(map(len, first))
    tail = []
    for m in reversed(msgs[2:]):
        t = msg_text(m, 900)
        if left - len(t) < 0:
            tail.append(f"[… {len(msgs) - 2 - len(tail)} earlier messages cut …]")
            break
        tail.append(t)
        left -= len(t)
    return "\n".join(parts + first + list(reversed(tail)) + [out])


def question(definition: str) -> dict:
    return {"type": "choice", "instructions": INSTRUCTIONS.format(definition=definition), "criteria": OPTIONS}


# ── Claude Code transcripts ──────────────────────────────────────────────────

def _is_prompt(line: dict) -> bool:
    """A message the user typed (not a tool result, command wrapper or injected reminder)."""
    if line.get("type") != "user" or line.get("isMeta") or line.get("isCompactSummary"):
        return False
    c = (line.get("message") or {}).get("content")
    if isinstance(c, list):
        c = "".join(b.get("text", "") for b in c if isinstance(b, dict) and b.get("type") == "text")
        if not c:
            return False
    return bool(str(c).strip()) and not str(c).lstrip().startswith(("<command-", "<local-command", "<system-reminder>"))


def _messages(line: dict) -> list[dict]:
    """One transcript line -> OpenAI-style messages (thinking blocks are dropped)."""
    m = line.get("message") or {}
    c = m.get("content")
    if isinstance(c, str):
        return [{"role": m.get("role", line.get("type")), "content": c}]
    out, text, calls = [], [], []
    for b in c or []:
        if not isinstance(b, dict):
            continue
        if b.get("type") == "text":
            text.append(b.get("text", ""))
        elif b.get("type") == "tool_use":
            calls.append({"name": b.get("name"), "arguments": json.dumps(b.get("input"), ensure_ascii=False)})
        elif b.get("type") == "tool_result":
            r = b.get("content")
            if isinstance(r, list):
                r = "\n".join(x.get("text", "") for x in r if isinstance(x, dict))
            out.append({"role": "tool", "content": ("[error] " if b.get("is_error") else "") + str(r or "")})
    if text or calls:
        msg = {"role": m.get("role", line.get("type")), "content": "\n".join(text)}
        if calls:
            msg["tool_calls"] = calls
        out.insert(0, msg)
    return out


def trace_of(transcript_path: str, last_assistant_message: str | None = None) -> tuple[dict, list[dict], dict] | None:
    """-> (metadata, messages, output) for the turn that just ended, or None if there is no turn."""
    lines = []
    with open(transcript_path, encoding="utf-8", errors="replace") as f:
        for raw in f:
            try:
                e = json.loads(raw)
            except ValueError:
                continue
            if e.get("type") in ("user", "assistant") and isinstance(e.get("message"), dict):
                lines.append(e)
    starts = [i for i, e in enumerate(lines) if _is_prompt(e)]
    if not starts:
        return None
    turn = lines[starts[-1]:]
    earlier = [m for i in starts[-1 - PREVIOUS_PROMPTS:-1] for m in _messages(lines[i])]
    msgs = earlier + [m for e in turn for m in _messages(e)]
    last = next((e for e in reversed(turn) if e.get("type") == "assistant"), {})
    usage = (last.get("message") or {}).get("usage") or {}
    meta = {"model": (last.get("message") or {}).get("model"), "status": "success",
            "prompt_tokens": sum(int(usage.get(k) or 0) for k in
                                 ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")) or None,
            "completion_tokens": usage.get("output_tokens"),
            "tools_defined": None}
    # the final reply is the output; everything before it is the input
    if msgs and msgs[-1]["role"] == "assistant" and not msgs[-1].get("tool_calls"):
        output = msgs.pop()
    else:
        output = {"content": ""}
    if last_assistant_message:
        output = {"content": last_assistant_message}
    return meta, msgs, output
