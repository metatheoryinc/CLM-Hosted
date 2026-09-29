# CLM in OpenAI Codex

The same hook as the [Claude Code plugin](../claude_code/README.md), run by Codex's lifecycle
hooks (Codex 0.150+, `hooks` feature on by default). Codex sends the same event payloads as
Claude Code (`Bash` tool calls, `Stop` with `last_assistant_message` and `stop_hook_active`),
so most of it carries over unchanged:

| | Codex |
|---|---|
| Behavior checks when a turn ends | **active**: a confident flag blocks the stop and Codex continues with the fix (unsure ones go to the judge) |
| Tool calls | shadow: classified and logged |
| Subagent models | logged only: Codex encrypts a spawned agent's task (`spawn_agent`'s `message`), so CLM cannot judge which model it needs |
| Judge for unsure cases | `codex exec` with `gpt-5.6-terra`, hooks and MCP servers off (about 6 s, only then) |

## Install

Codex plugins cannot carry hooks, so a script installs it. From a clone of this repo:

```bash
python3 integrations/codex/install.py
```

It asks for the agent key (or takes `--key` / `CLM_API_KEY`), copies the hook to
`~/.codex/clm/`, registers it in `~/.codex/hooks.json` (keeping your other hooks; a backup is
kept) and stores the key in `~/.config/clm/claude-code.json` (mode 600), the same file the
Claude Code plugin falls back to. **The next time Codex starts it asks you to review and trust
the new hooks**; they do not run until you do. Re-run the script after a `git pull` to update.

```bash
python3 integrations/codex/install.py status
```

```bash
python3 integrations/codex/install.py uninstall
```

`--jev-key` (or `TYPESAFE_API_KEY`) adds Jev, as in the Claude Code plugin's
[With a Jev key](../claude_code/README.md#with-a-jev-key).

`uninstall` removes only these hooks and `~/.codex/clm/`; `--purge` also deletes the shared config
(and the key). `--shadow` at install keeps behavior checks log-only.

## Picking subagent models: clm_pick_model

Codex encrypts the task it hands a spawned agent, so the hook can't choose its model. Instead the
installer registers an MCP server, `clm`, with one tool the main agent calls itself:
`clm_pick_model(task)`. CLM's trained subagent-tier head reads the task and returns a Codex model and
reasoning effort; when CLM is unsure it picks one tier *up*, so a cheap main chat hands hard work to a
stronger model rather than a weaker one. Add this to your `AGENTS.md`:

```markdown
Before you call spawn_agent, call the clm_pick_model tool with the task exactly as you will give it
to the subagent, and pass the model and reasoning_effort it returns to spawn_agent.
```

| CLM tier | Codex model | reasoning effort |
|---|---|---|
| haiku (look something up) | gpt-5.6-luna | low |
| sonnet (a well-defined change) | gpt-5.6-terra | medium |
| opus (design, root causes, review) | gpt-6-astra | high |

Change the mapping with `{"codex": {"pick_models": {"opus": {"model": "gpt-5.6-sol", "reasoning_effort": "high"}}}}`
in the config file, and the confidence bar with `"pick_threshold"` (0.8). A task shorter than 1000
characters never goes to the lookup tier (the head wasn't trained on short prompts). If CLM is
unreachable the tool answers the middle tier. The installer marks the tool as not needing approval
(it only reads the task and returns a model name), which `codex exec` requires. Every pick is logged as
`routing/codex-model-picks`. The main model can still forget to call the tool; the log shows how often
it does.

## Tuning

Settings for Codex alone go under `"codex"` in the config file, e.g.
`{"codex": {"behavior_threshold": 0.95, "judge_cmd": "…"}}`; everything else in the file applies to
both. Behaviors are in `~/.codex/clm/behaviors.json` (copied from
[../claude_code/behaviors.json](../claude_code/behaviors.json)). What CLM changed is logged in
`~/.codex/clm/actions.jsonl`; `CLM_HOOK_RUNTIME=codex python3 ~/.codex/clm/clm_hook.py --status`
shows it. Records are logged as `behavior/claude-code` and `routing/claude-code-tools` with the
Codex model name, so `clm-decisions report … --model <name>` separates them.

Codex transcripts (`~/.codex/sessions/…/rollout-*.jsonl`) are read for the current `turn_id`:
the prompts you typed, the assistant's messages, tool calls and outputs, and the final reply;
encrypted reasoning and injected context are skipped. What leaves your machine is the same as
for Claude Code (see [What leaves your machine](../claude_code/README.md#what-leaves-your-machine)).
