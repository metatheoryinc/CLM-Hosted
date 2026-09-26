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


def _read_jsonl(path: str) -> list[dict]:
    out = []
    with open(path, encoding="utf-8", errors="replace") as f:
        for raw in f:
            try:
                e = json.loads(raw)
            except ValueError:
                continue
            if isinstance(e, dict):
                out.append(e)
    return out


def trace_of(transcript_path: str, last_assistant_message: str | None = None,
             turn_id: str | None = None) -> tuple[dict, list[dict], dict] | None:
    """-> (metadata, messages, output) for the turn that just ended, or None if there is no turn.

    Reads Claude Code transcripts and Codex rollouts (``~/.codex/sessions/…/rollout-*.jsonl``).
    """
    events = _read_jsonl(transcript_path)
    if events and events[0].get("type") == "session_meta":
        return _codex_trace(events, last_assistant_message, turn_id)
    lines = [e for e in events if e.get("type") in ("user", "assistant") and isinstance(e.get("message"), dict)]
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


# ── Codex rollouts ───────────────────────────────────────────────────────────

def _text(content) -> str:
    if isinstance(content, str):
        return content
    return "\n".join(str(c.get("text", "")) for c in content or [] if isinstance(c, dict) and c.get("text"))


def _codex_trace(events: list[dict], last_assistant_message: str | None, turn_id: str | None):
    starts = [i for i, e in enumerate(events) if e.get("type") == "event_msg"
              and (e.get("payload") or {}).get("type") == "task_started"]
    if not starts:
        return None
    start = next((i for i in starts if events[i]["payload"].get("turn_id") == turn_id), starts[-1])
    # what the user typed (injected context arrives as user messages too; UserMessage items are the prompts)
    prompts = [(i, _text((e["payload"].get("item") or {}).get("content"))) for i, e in enumerate(events)
               if e.get("type") == "event_msg" and e["payload"].get("type") == "item_completed"
               and (e["payload"].get("item") or {}).get("type") == "UserMessage"]
    this = [t for i, t in prompts if i > start][:1]
    earlier = [t for i, t in prompts if i < start][-PREVIOUS_PROMPTS:]
    msgs = [{"role": "user", "content": t} for t in earlier + this if t]
    meta, model = {}, None
    for e in events[start:]:
        p = e.get("payload") or {}
        if e.get("type") == "turn_context":
            model = p.get("model") or model
        if e.get("type") == "event_msg" and p.get("type") == "token_count":
            meta = (p.get("info") or {}).get("last_token_usage") or meta
        if e.get("type") != "response_item":
            continue
        t = p.get("type")
        if t == "message" and p.get("role") == "assistant":
            msgs.append({"role": "assistant", "content": _text(p.get("content"))})
        elif t in ("function_call", "custom_tool_call"):
            args = p.get("arguments") if t == "function_call" else p.get("input")
            msgs.append({"role": "assistant", "content": "", "tool_calls": [{"name": p.get("name"), "arguments": args}]})
        elif t in ("function_call_output", "custom_tool_call_output"):
            msgs.append({"role": "tool", "content": _text(p.get("output"))})
    if model is None:
        model = next(((e.get("payload") or {}).get("model") for e in reversed(events[:start])
                      if e.get("type") == "turn_context"), None)
    metadata = {"model": model, "status": "success",
                "prompt_tokens": meta.get("input_tokens"), "completion_tokens": meta.get("output_tokens"),
                "tools_defined": None}
    if msgs and msgs[-1]["role"] == "assistant" and not msgs[-1].get("tool_calls"):
        output = msgs.pop()
    else:
        output = {"content": ""}
    if last_assistant_message:
        output = {"content": last_assistant_message}
    return metadata, msgs, output
