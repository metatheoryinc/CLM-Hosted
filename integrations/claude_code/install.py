#!/usr/bin/env python3
"""Install, update or remove the CLM hook for Claude Code on this machine (standard library only).

    python3 integrations/claude_code/install.py              install or update (asks for the agent key)
    python3 integrations/claude_code/install.py status       what is installed
    python3 integrations/claude_code/install.py uninstall    remove the hooks (--purge also deletes the config)

Install registers the hook in ~/.claude/settings.json for every event it handles, pointing at
this checkout (a backup of the old file is kept next to it), and writes
~/.config/clm/claude-code.json (mode 600) with the recommended settings: subagent model
downgrades and behavior checks active, tool calls in shadow. Settings already in the config
file are kept, so re-running it only updates the hooks and fills in new defaults.
New Claude Code sessions pick it up; `CLM_HOOK_MODE=off claude` turns it off for one session.
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import shutil
import sys
import time

HOOK = os.path.join(os.path.dirname(os.path.abspath(__file__)), "clm_hook.py")
MARK = "clm_hook.py"                # how an installed hook entry is recognised
BLOCKING = ("PreToolUse", "Stop", "SubagentStop")        # may change what Claude Code does: waited for
LOGGING = ("PermissionRequest", "PermissionDenied", "PostToolUse", "PostToolUseFailure", "SubagentStart")
RECOMMENDED = {
    "base_url": "https://clm.metatheory.dev",
    "mode": "shadow", "model": "tool-risk-v1", "calibrate": "none", "threshold": 0.7,
    "subagent_mode": "active", "subagent_model": "subagent-tier-v2", "subagent_calibrate": "none",
    "subagent_threshold": 0.9, "subagent_thresholds": {"sonnet": 0.95, "haiku": 0.95},
    "behavior_mode": "active", "behavior_model": "behavior-v1",
}


def home(*p: str) -> str:
    return os.path.join(os.path.expanduser("~"), *p)


def settings_path() -> str:
    return home(".claude", "settings.json")


def config_path() -> str:
    return os.environ.get("CLM_HOOK_CONFIG") or home(".config", "clm", "claude-code.json")


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
    if private:
        os.chmod(path, 0o600)


def command() -> str:
    # if the checkout moves or is deleted the hook does nothing, rather than exiting 2 ("block")
    return f'f="{HOOK}"; [ -f "$f" ] && python3 "$f"; exit 0'


def strip_hooks(settings: dict) -> int:
    """Remove every hook entry that runs clm_hook.py. -> how many were removed."""
    n = 0
    hooks = settings.get("hooks") or {}
    for event in list(hooks):
        kept = []
        for group in hooks[event]:
            mine = [h for h in group.get("hooks", []) if MARK in str(h.get("command", ""))]
            n += len(mine)
            rest = [h for h in group.get("hooks", []) if h not in mine]
            if rest:
                kept.append({**group, "hooks": rest})
        if kept:
            hooks[event] = kept
        else:
            del hooks[event]
    if not hooks:
        settings.pop("hooks", None)
    return n


def backup(path: str) -> None:
    if os.path.exists(path):
        shutil.copy2(path, f"{path}.bak-{time.strftime('%Y%m%d-%H%M%S')}")


def install(args) -> None:
    cfg_file = config_path()
    cfg = read_json(cfg_file)
    key = args.key or os.environ.get("CLM_API_KEY") or cfg.get("api_key")
    if not key:
        if not sys.stdin.isatty():
            raise SystemExit("no agent key: pass --key, set CLM_API_KEY, or run this in a terminal")
        key = getpass.getpass("CLM agent key (ask whoever runs the CLM stack): ").strip()
    if not key:
        raise SystemExit("no agent key given")
    new = {**RECOMMENDED, **cfg, "api_key": key}
    if args.shadow:
        new.update(subagent_mode="shadow", behavior_mode="shadow")
    if new.get("mode") == "off":                 # re-installing after "uninstall" without --purge
        new["mode"] = "shadow"
    write_json(cfg_file, new, private=True)

    path = settings_path()
    settings = read_json(path)
    backup(path)
    strip_hooks(settings)
    hooks = settings.setdefault("hooks", {})
    for event in BLOCKING + LOGGING:
        h = {"type": "command", "command": command(), "timeout": 40 if event in BLOCKING else 5}
        if event in LOGGING:
            h["async"] = True
        hooks.setdefault(event, []).append({"matcher": "", "hooks": [h]})
    write_json(path, settings)
    print(f"installed: {len(BLOCKING + LOGGING)} hooks in {path} -> {HOOK}")
    print(f"config: {cfg_file} (tool calls {new['mode']}, subagent models {new['subagent_mode']}, "
          f"behavior checks {new['behavior_mode']})")
    print("new Claude Code sessions use it; `CLM_HOOK_MODE=off claude` skips it for one session")


def uninstall(args) -> None:
    path = settings_path()
    settings = read_json(path)
    backup(path)
    n = strip_hooks(settings)
    write_json(path, settings)
    cfg_file = config_path()
    if args.purge:
        if os.path.exists(cfg_file):
            os.remove(cfg_file)
        print(f"removed {n} hooks from {path} and deleted {cfg_file}")
    else:
        cfg = read_json(cfg_file)
        if cfg:                                   # also stops the repo's own .claude/settings.json copy
            write_json(cfg_file, {**cfg, "mode": "off"}, private=True)
        print(f"removed {n} hooks from {path}; {cfg_file} kept with mode off (--purge deletes it)")
    print("running sessions keep their hooks until restarted")


def status(_args) -> None:
    settings = read_json(settings_path())
    events = sorted(e for e, groups in (settings.get("hooks") or {}).items()
                    for g in groups for h in g.get("hooks", []) if MARK in str(h.get("command", "")))
    cfg = read_json(config_path())
    print(f"hooks in {settings_path()}: {', '.join(events) or 'none'}")
    if cfg:
        shown = {k: v for k, v in cfg.items() if k != "api_key"}
        print(f"config {config_path()}: key {'set' if cfg.get('api_key') else 'MISSING'}, {json.dumps(shown)}")
    else:
        print(f"config {config_path()}: none")


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd")
    i = sub.add_parser("install", help="install or update (the default)")
    for p in (ap, i):
        p.add_argument("--key", help="agent key (default: $CLM_API_KEY, the existing config, or a prompt)")
        p.add_argument("--shadow", action="store_true", help="log only: CLM never changes what Claude Code does")
    u = sub.add_parser("uninstall", help="remove the hooks")
    u.add_argument("--purge", action="store_true", help="also delete the config file (and the key in it)")
    sub.add_parser("status", help="show what is installed")
    args = ap.parse_args(argv)
    {"uninstall": uninstall, "status": status}.get(args.cmd, install)(args)


if __name__ == "__main__":
    main()
