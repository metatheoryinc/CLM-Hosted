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

### In your other repos

The config file is per machine, so to classify tool calls in every project, add the same
hooks to `~/.claude/settings.json` with an absolute path to `clm_hook.py` (copy the
`hooks` block from this repo's `.claude/settings.json` and replace
`"$CLAUDE_PROJECT_DIR"/integrations/claude_code/clm_hook.py` with the full path). The
script needs only `python3`.

## Measure

```bash
clm-decisions report https://clm.metatheory.dev --workflow routing/claude-code-tools
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
