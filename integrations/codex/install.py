#!/usr/bin/env python3
"""Install, update or remove the CLM hook for OpenAI Codex (standard library only).

    python3 integrations/codex/install.py              install or update (asks for the agent key)
    python3 integrations/codex/install.py status       what is installed
    python3 integrations/codex/install.py uninstall    remove it (--purge also deletes the shared config)

Codex plugins cannot carry hooks, so this copies the hook (the same one the Claude Code plugin
runs) to ~/.codex/clm/ and registers it in ~/.codex/hooks.json, keeping any other hooks there.
Codex asks you to review and trust new hooks the next time it starts; until you do, they do not
run. The agent key goes in ~/.config/clm/claude-code.json (mode 600), shared with Claude Code.
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import shutil
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SRC = os.path.join(REPO, "integrations", "claude_code")
FILES = ("clm_hook.py", "clm_behaviors.py", "behaviors.json")
DIRS = ("rubrics",)
MARK = "clm_hook.py"
BLOCKING = ("PreToolUse", "Stop", "SubagentStop")
LOGGING = ("PermissionRequest", "PostToolUse", "SubagentStart")


def codex_home() -> str:
    return os.environ.get("CODEX_HOME") or os.path.expanduser("~/.codex")


def dest() -> str:
    return os.path.join(codex_home(), "clm")


def hooks_path() -> str:
    return os.path.join(codex_home(), "hooks.json")


def config_path() -> str:
    return os.environ.get("CLM_HOOK_CONFIG") or os.path.expanduser("~/.config/clm/claude-code.json")


def read_json(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def write_json(path: str, data: dict, private: bool = False) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600 if private else 0o644)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(json.dumps(data, indent=2) + "\n")
    os.replace(tmp, path)


def command() -> str:
    # a missing hook file does nothing rather than failing every tool call
    f = os.path.join(dest(), "clm_hook.py")
    return f'f="{f}"; [ -f "$f" ] && CLM_HOOK_RUNTIME=codex python3 "$f"; exit 0'


def strip(doc: dict) -> int:
    n, hooks = 0, doc.get("hooks") or {}
    for event in list(hooks):
        kept = []
        for group in hooks[event]:
            rest = [h for h in group.get("hooks", []) if MARK not in str(h.get("command", ""))]
            n += len(group.get("hooks", [])) - len(rest)
            if rest:
                kept.append({**group, "hooks": rest})
        if kept:
            hooks[event] = kept
        else:
            del hooks[event]
    return n


def backup(path: str) -> None:
    if os.path.exists(path):
        shutil.copy2(path, f"{path}.bak-{time.strftime('%Y%m%d-%H%M%S')}")


def install(args) -> None:
    cfg = read_json(config_path())
    key = args.key or os.environ.get("CLM_API_KEY") or cfg.get("api_key")
    if not key:
        if not sys.stdin.isatty():
            raise SystemExit("no agent key: pass --key, set CLM_API_KEY, or run this in a terminal")
        key = getpass.getpass("CLM agent key (ask whoever runs the CLM stack): ").strip()
    if not key:
        raise SystemExit("no agent key given")
    new = {"base_url": "https://clm.metatheory.dev", **cfg, "api_key": key}
    if new.get("mode") == "off":                 # re-installing after uninstall
        new.pop("mode")
    if args.shadow:
        new.setdefault("codex", {})["behavior_mode"] = "shadow"
    write_json(config_path(), new, private=True)
    os.chmod(config_path(), 0o600)

    d = dest()
    os.makedirs(d, exist_ok=True)
    for f in FILES:
        shutil.copy2(os.path.join(SRC, f), os.path.join(d, f))
    for sub in DIRS:
        shutil.copytree(os.path.join(SRC, sub), os.path.join(d, sub), dirs_exist_ok=True)

    path = hooks_path()
    doc = read_json(path)
    backup(path)
    strip(doc)
    hooks = doc.setdefault("hooks", {})
    for event in BLOCKING + LOGGING:
        hooks.setdefault(event, []).append({"matcher": "", "hooks": [{"type": "command", "command": command()}]})
    write_json(path, doc)
    print(f"installed: hook copied to {d}, {len(BLOCKING + LOGGING)} events registered in {path}")
    print(f"config: {config_path()} (behavior checks "
          f"{(new.get('codex') or {}).get('behavior_mode', new.get('behavior_mode', 'active'))}, tool calls shadow)")
    print("next time Codex starts it asks you to review and trust the new hooks; they run once you do")


def uninstall(args) -> None:
    path = hooks_path()
    doc = read_json(path)
    backup(path)
    n = strip(doc)
    if doc.get("hooks") or set(doc) - {"hooks"}:
        write_json(path, doc)
    elif os.path.exists(path):
        os.remove(path)
    shutil.rmtree(dest(), ignore_errors=True)
    if args.purge and os.path.exists(config_path()):
        os.remove(config_path())
    print(f"removed {n} hooks from {path} and {dest()}" + (f"; deleted {config_path()}" if args.purge else ""))


def status(_args) -> None:
    doc = read_json(hooks_path())
    events = sorted(e for e, gs in (doc.get("hooks") or {}).items()
                    for g in gs for h in g.get("hooks", []) if MARK in str(h.get("command", "")))
    print(f"hooks in {hooks_path()}: {', '.join(events) or 'none'}")
    print(f"hook files in {dest()}: {'present' if os.path.exists(os.path.join(dest(), 'clm_hook.py')) else 'missing'}")
    cfg = read_json(config_path())
    print(f"config {config_path()}: key {'set' if cfg.get('api_key') else 'MISSING'}")
    if os.path.exists(os.path.join(dest(), "clm_hook.py")):
        os.environ["CLM_HOOK_RUNTIME"] = "codex"
        os.execv(sys.executable, [sys.executable, os.path.join(dest(), "clm_hook.py"), "--status"])


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd")
    i = sub.add_parser("install", help="install or update (the default)")
    for p in (ap, i):
        p.add_argument("--key", help="agent key (default: $CLM_API_KEY, the existing config, or a prompt)")
        p.add_argument("--shadow", action="store_true", help="behavior checks log only in Codex")
    u = sub.add_parser("uninstall", help="remove the hooks and the copied hook files")
    u.add_argument("--purge", action="store_true", help="also delete the config file shared with Claude Code")
    sub.add_parser("status", help="show what is installed")
    args = ap.parse_args(argv)
    {"uninstall": uninstall, "status": status}.get(args.cmd, install)(args)


if __name__ == "__main__":
    main()
