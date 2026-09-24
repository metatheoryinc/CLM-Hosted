# CLM in Claude Code

A Claude Code hook that has CLM classify every tool call as **allow**, **review** or
**block**, alongside Claude Code's own permission system. It is the tool-call version
of [docs/ROUTING.md](../../docs/ROUTING.md): shadow first, measure, then let CLM act.

* **shadow**: nothing about Claude Code changes. Each tool call is classified in a
  detached background process and logged to the collector, with Claude Code's own
  decision as the baseline: `review` when it showed a permission prompt, `block` when
  auto mode denied the call, `allow` when it just ran.
* **active**: when CLM is confident (`threshold`, default 0.9), a `review` pick forces
  the permission prompt and a `block` pick denies the call, with the reason shown to
  Claude. CLM never approves anything, so it can only make Claude Code stricter; on
  any error or after `timeout` (1.5 s) the call goes to Claude Code as usual.

The hook is registered in this repo's [.claude/settings.json](../../.claude/settings.json)
and does nothing until you opt in.

## Opt in

Ask for your own agent key (whoever runs the [infra](../../infra/README.md) stack adds it
to `agentKeys`), then:

```bash
mkdir -p ~/.config/clm
```

```bash
cat > ~/.config/clm/claude-code.json <<'EOF'
{"mode": "shadow", "base_url": "https://clm.metatheory.dev", "api_key": "<your agent key>"}
EOF
```

```bash
chmod 600 ~/.config/clm/claude-code.json
```

It takes effect on the next tool call. `CLM_HOOK_MODE=off claude` disables it for one
session; `"mode": "off"` disables it everywhere.

### In every repo

The config file is per machine; to classify tool calls in every project, add the hooks
to `~/.claude/settings.json` too, pointing at your checkout of this repo. Use this
command (not a bare `python3 <path>`): if the file is ever missing, `python3` exits
with code 2, which Claude Code treats as "block the tool call", in every session.

```json
"command": "f=\"/path/to/CLM-Hosted/integrations/claude_code/clm_hook.py\"; [ -f \"$f\" ] && python3 \"$f\"; exit 0"
```

Register it for the same seven events as this repo's
[.claude/settings.json](../../.claude/settings.json) (`matcher: ""`, `timeout: 5`,
`async: true` on all but `PreToolUse`: PermissionRequest, PermissionDenied,
PostToolUse, PostToolUseFailure, SubagentStart, SubagentStop). In this repo both registrations fire; the
hook handles each event once and the second copy exits.

## Subagent model tiers

Every subagent launch (the `Agent` tool) also gets a shadow-only second question: which
tier, `haiku`, `sonnet` or `opus`, is enough for the task. It is logged as
`routing/claude-code-subagents` with what CLM would pick and, when the subagent finishes,
the tier that actually ran (from the tool result's `resolvedModel`) and what the run cost
(status, tokens, duration, tool counts). The subagent's output itself is not sent. CLM
only sees the task description, prompt and subagent type, not the model Claude asked for.

Subagents without a `model` inherit the main conversation's model, so a search task can
run on the most expensive tier; the report shows how often CLM would have picked a cheaper
one. There is no active mode for this yet: a wrong downgrade costs quality, so it needs
labels (was the cheaper tier good enough?) before it changes anything.

`"raw_log": true` in the config also appends the raw Agent, SubagentStart and
SubagentStop payloads to `~/.config/clm/claude-code-events.jsonl` (local only, mode 600),
for checking what Claude Code reports. SubagentStop carries no model or cost; the
Agent tool's `PostToolUse` result does.

## Measure

```bash
clm-decisions report https://clm.metatheory.dev --workflow routing/claude-code-tools
```

```bash
clm-decisions report https://clm.metatheory.dev --workflow routing/claude-code-subagents
```

The baseline is Claude Code's permission system, so "agreement" is how often CLM agrees
with it; the disagreements (CLM says `review` for a call that ran without a prompt, or
`allow` for one that was prompted) are the calls worth looking at. The baseline only
means something in the `default`, `acceptEdits` and `auto` permission modes; in
`bypassPermissions` every call runs. Each record keeps the permission mode.

To go active, set `"mode": "active"` (and a `"threshold"`) once the report shows CLM's
confident `review` / `block` picks are ones you would want enforced.

## What leaves your machine

For each tool call: the tool name, its arguments (each field clipped to 800 characters,
3000 in total), the working directory and the permission mode. Before sending, the
hook redacts likely secrets: bearer tokens, `key=` / `token=` / `password=` values,
GitHub, OpenAI, Slack, AWS and Google keys, private key blocks and long hex strings.
Redaction is best effort: don't opt in on a machine where tool calls carry secrets
these patterns would miss. Records are stored in the stack's D1 database under your
agent key; the transcript path and file contents beyond the clipped arguments are
never sent.
