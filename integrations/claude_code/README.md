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
and does nothing until you install it (below).

## Install

You need a clone of this repo and the agent key (ask whoever runs the
[infra](../../infra/README.md) stack). Then, from the clone:

```bash
python3 integrations/claude_code/install.py
```

It asks for the key (or reads `--key` / `CLM_API_KEY`), registers the hook in
`~/.claude/settings.json` for every event it handles, pointing at this clone, and writes
`~/.config/clm/claude-code.json` (mode 600) with the defaults: **subagent model downgrades
and behavior checks active, tool calls in shadow**. New sessions use it. Re-run it after a
`git pull` to update the hooks; settings you changed in the config file are kept.

```bash
python3 integrations/claude_code/install.py status
```

```bash
python3 integrations/claude_code/install.py uninstall
```

`uninstall` removes only this hook's entries (a timestamped backup of `settings.json` is
kept) and sets the config to `"mode": "off"`, which also silences the copy registered in
this repo's own [.claude/settings.json](../../.claude/settings.json); `--purge` deletes the
config and the key in it. For one session only: `CLM_HOOK_MODE=off claude`. `--shadow` at
install logs everything and changes nothing.

What active means for you: a subagent Claude would run on Opus may run on Sonnet or Haiku
when CLM is confident the task does not need it; at the end of a turn Claude may be told to
verify or fix something (see [Behavior checks](#behavior-checks-when-a-turn-ends)); unsure
cases ask Opus (`claude -p`, on your own account, a few seconds, only then). Any CLM error
or timeout leaves Claude Code as it was.

To change the settings by hand, edit the config file; the keys are in `DEFAULTS` at the top
of [clm_hook.py](clm_hook.py). If the clone moves or is deleted the hook does nothing (the
registered command checks the file exists first), rather than blocking every tool call.

## Subagent model tiers

Every subagent launch (the `Agent` tool) also gets a shadow-only second question: which
tier, `haiku`, `sonnet` or `opus`, is enough for the task. It is logged as
`routing/claude-code-subagents` with what CLM would pick and, when the subagent finishes,
the tier that actually ran (from the tool result's `resolvedModel`) and what the run cost
(status, tokens, duration, tool counts). The subagent's output itself is not sent. CLM
only sees the task description, prompt and subagent type, not the model Claude asked for.

Subagents without a `model` inherit the main conversation's model, so a search task can
run on the most expensive tier; the report shows how often CLM would have picked a cheaper
one.

### Active: downgrade only

With `"subagent_mode": "active"` in the config (independent of `"mode"`, which governs the
tool-call gate), a subagent launch waits for CLM (`subagent_timeout`, default 1.5 s) and,
when CLM's pick reaches `subagent_threshold` (default 0.9) and is cheaper than what would
otherwise run, rewrites the call's `model`. What would otherwise run is taken to be the
top tier (subagents inherit the main model), or `CLAUDE_CODE_SUBAGENT_MODEL`'s tier when
that is set. It never:

* upgrades, so a pick of `opus` changes nothing;
* overrides a model Claude chose for the call;
* overrides a custom agent whose definition (`.claude/agents/**/*.md`, in the project or
  `~/.claude`) sets `model:`;
* touches plugin agents (`plugin:agent`), whose definitions it cannot see;
* acts on an error or timeout.

### A trained head for this question

Zero-shot `clm-latest` is at chance on real subagent prompts, calibrated or not. A head
fine-tuned on labelled subagent calls (rubric labels from a strong model, trained with
`train/finetune.py --task choice --balance --select-metric balanced_acc`) is not: on
held-out projects it never downgraded a task the labels said needed `opus` at a
threshold of 0.6 or more. Serve it (`clm-heads upload … --name subagent-tier`) and point
the subagent question at it, uncalibrated (it was trained without calibration), with a
stricter bar for `haiku` than for `sonnet`:

```json
"subagent_model": "subagent-tier-v2", "subagent_calibrate": "none",
"subagent_thresholds": {"sonnet": 0.95, "haiku": 0.95}
```

Two things the first trained head got wrong, and what guards against them now:

* **Prompt length leaked the tier.** In real subagent calls, lookups are shorter and
  high-stakes work longer, and the clip marker ("… [N more characters]") spelled the
  length out; the head learned "short means haiku" and sent one-line design tasks to
  haiku with 0.9+ confidence. The subagent state is now clipped to a fixed 1200
  characters with a plain `…`, and the head is trained with length-balanced synthetic
  tasks (Fable-written, blind-relabelled) alongside the real ones.
* **Out-of-range prompts.** `subagent_min_prompt_chars` (default 1000) keeps the hook
  from downgrading prompts shorter than the trained range, whatever the head says.

Measured on held-out projects (real prompts, rubric labels): at 0.95 about a fifth of
subagents are downgraded and none the labels say needed `opus`; 15/15 on hand-written
short tasks it never saw. Re-check the threshold table after retraining.

The head is trained on the exact `SUBAGENT_INSTRUCTIONS` / `SUBAGENT_OPTIONS` text in the
hook; editing either means retraining. The tool-call question keeps `model` and
`calibrate`.

Each record says what it did and why (`meta.applied_model`, `meta.why`), and the tier
that actually ran is logged when the subagent finishes. A wrong downgrade costs quality,
not safety: watch for subagents that fail or come back thin, and raise the threshold or
set `"subagent_mode": "shadow"` if they do. CLM's confidence here is not yet trustworthy
(it has picked `sonnet` at 0.99 for tasks the `haiku` option describes).

`"raw_log": true` in the config also appends the raw Agent, SubagentStart and
SubagentStop payloads to `~/.config/clm/claude-code-events.jsonl` (local only, mode 600),
for checking what Claude Code reports. SubagentStop carries no model or cost; the
Agent tool's `PostToolUse` result does.

## Accept when confident, escalate when unsure

The flow of *JEV-as-a-Judge* (Li et al., 2026): CLM's verdict is used when it is confident,
and an LLM judge decides the rest.

* **Subagents** (`"subagent_escalate": true`, the default, in active subagent mode): when
  CLM's tier pick is below its threshold, or CLM fails, the judge picks the tier with the
  subagents rubric. Its pick goes through the same rules (downgrade only; never for a
  model Claude set, an agent definition's model, a plugin agent or a short prompt, which
  are checked before the judge is asked).
* **Tool calls** (`"tool_escalate": true`, off by default, in active mode): only when CLM
  leans `review` or `block` but is below the threshold; uncertain `allow`s go straight
  through, so most commands never wait. A judge `review` forces the permission prompt, a
  `block` denies, an `allow` leaves the call to Claude Code.

The judge is headless Claude Code with Opus and no tools, MCP servers or settings
(`judge_cmd`, `judge_name`), given the rubric in [rubrics/](rubrics/) and told the item is
data. It takes about 3–6 s; after `judge_timeout` (25 s) the call is left alone. The hook's
`PreToolUse` timeout is 40 s so Claude Code does not cut an escalation short. The judge may also
answer `not_observable` (the call does not show enough to decide); that is recorded as an
abstention and the call is left to Claude Code, like a timeout. Each record
keeps the judge's answer and latency, and the answer is also written as a labelled outcome
(`source: "llm:opus-escalation"`): the cases CLM found hard become training data for the
next head. `clm-decisions report` summarizes the escalations. Use a different model as the
judge than as the labeller, or the cascade is scored against the judge's own opinions.

## Behavior checks when a turn ends

With `"behavior_mode": "shadow"` or `"active"` (off by default), each time Claude Code
finishes a turn (`Stop`) or a subagent finishes (`SubagentStop`), the hook renders the turn
as a trace and asks CLM (`behavior_model`, default `behavior-v1`) about every behavior in
[behaviors.json](behaviors.json), in one request (about 0.5 s):

| behavior | flags a turn where |
|---|---|
| `unverified_success` | the reply says it works or tests pass, but nothing checked it after the last change |
| `stale_task` | the user redirected the task and the work continued on the old one |
| `truncation_ignored` | a truncated or capped tool output was relied on as if complete |
| `repeat_ask` | the reply asks for something the user already gave |

In **active** mode a behavior with p(present) >= `behavior_threshold` (0.9) blocks the stop:
Claude gets that behavior's `fix` as its next instruction, plus "if a flag is wrong, say so
in one sentence and stop". Between `behavior_escalate_from` (0.6) and the threshold the
judge decides, as for subagents (about 5 s, only then). A behavior may set its own
`threshold` / `escalate_from` in the file; `unverified_success` escalates from 0.45 because
it is our own definition and scores lower than the benchmark's. It never blocks twice in a
row (`stop_hook_active`), and any error or timeout (`behavior_timeout`, 3 s) lets the stop
through. Edit the file (or point `behaviors_file` at your own) to add or drop behaviors: a
definition in the benchmark's "Include: … / Exclude: …" style reads best.

The trace is what [evaluation/behavior_eval.py](../../evaluation/behavior_eval.py) trained
`behavior-v1` on: model and token counts, the last two user prompts before this turn, the
turn's messages and tool calls (as many of the latest as fit in 5600 characters) and the final
reply, secrets redacted. On the Respan behavior benchmark's held-out traces, `behavior-v1` has
F1 0.66 on `present` (Jev 0.715, Span-01 0.843) and 86% precision at p >= 0.9; behaviors that
depend on the middle of a long turn are its weak spot. Every answer is logged as
`behavior/claude-code` (and the judge's answers as labels), so
`clm-decisions report … --workflow behavior/claude-code` and `clm-decisions label` work on it.

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

For each tool call: the tool name, its arguments (up to 6000 characters, about 1700 tokens
of code, so the whole state and question fit the 2048 tokens CLM embeds; longer inputs keep
their start and end, with the middle marked as cut), the working directory and the
permission mode. Before sending, the
hook redacts likely secrets: bearer tokens, `key=` / `token=` / `password=` values,
GitHub, OpenAI, Slack, AWS and Google keys, private key blocks and long hex strings.
Redaction is best effort: don't opt in on a machine where tool calls carry secrets
these patterns would miss. Records are stored in the stack's D1 database under your
agent key; the transcript path and file contents beyond the clipped arguments are
never sent. With behavior checks on, each turn's rendered trace (up to 5600 characters of
prompts, messages, tool calls and results, redacted the same way) is also sent and stored.
