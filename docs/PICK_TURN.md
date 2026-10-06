# Turn model selection contract

Status: approved v1, 2026-10-06. This document defines the agreed
mipmap auto-picker request and its shared CLM policy.
CLM owns both the capability decision and the budget policy; clients only
collect limits, supply available candidates, execute the result, and record it.

## Entry points and scope

- MCP tool `clm_pick_turn`, added alongside `clm_pick_model` in
  `integrations/codex/clm_mcp.py`; one stdio server per caller chat.
- Python function `pick_turn(cfg, request, *, now=None)` backed by a shared,
  standard-library-only policy module. Classifier, clock, and decision sink
  must be replaceable in tests. Support Python 3.9 for the installed MCP path.
- Proposed HTTP `POST /v1/pick-turn` in `src/clm/server.py`, using the same
  validator and policy with the local engine as classifier. This fits the
  existing authenticated API and gateway forwarding. It should not call its
  own `/v1/systemone` over HTTP or import the installed hook at server boot.
  Hosting this route requires a later image deployment by the maintainer.

No changes to `pick()`, `clm_pick_model`, trained instructions/options,
existing state renderers, or existing decision workflows. New MCP/library
code introduces no third-party dependencies; the existing HTTP host retains
its current dependencies. Transport adapters must produce identical policy
results for the same inputs, classification, configuration, and time.

## Request

Illustrative request; these budgets are examples, not live persisted state.

```json
{
  "task": "Review the migration and identify correctness risks.",
  "context": "Optional concise prior-turn context, not the whole memory view.",
  "candidates": [
    {
      "provider": "codex", "model": "gpt-5.6-terra",
      "effort": "medium", "tier": "sonnet",
      "budget": {
        "observed_at": 1791300000,
        "windows": [
          {"name": "primary", "used_percent": 81,
           "window_minutes": 300, "resets_at": 1791307200},
          {"name": "secondary", "used_percent": 43,
           "window_minutes": 10080, "resets_at": 1791566400}
        ]
      }
    },
    {
      "provider": "claude", "model": "sonnet",
      "effort": "medium", "tier": "sonnet",
      "budget": {
        "observed_at": 1791300000,
        "windows": [
          {"name": "five_hour", "used_percent": 12,
           "window_minutes": 300, "resets_at": 1791309000}
        ]
      }
    }
  ],
  "prefer": "codex",
  "caller": "mipmap"
}
```

`task` is a nonempty string. `context` is an optional string. `caller` is
a required nonempty application identifier, never an auth identity or key.
`candidates` is a nonempty, ordered array of executable configurations:
`provider` is `codex|claude`, `tier` is `haiku|sonnet|opus`, and `model` and
`effort` are nonempty strings validated against the caller's supported CLI.
CLM never invents a configuration. Duplicate provider/model/effort/tier
tuples are invalid. All candidates for the same subscription scope must
receive the same applicable windows; model-specific limits must be attached
only to models they constrain. V1 assumes one account per provider.

Mipmap's initial auto menu (caller configuration, not CLM-owned model IDs):

| Tier | Codex model / effort | Claude model / effort |
| --- | --- | --- |
| haiku | `gpt-5.6-luna` / `low` | `haiku` / `low` |
| sonnet | `gpt-5.6-terra` / `medium` | `sonnet` / `medium` |
| opus | `gpt-5.6-sol` / `high` | `opus` / `high` |

The caller may override this menu through environment configuration and
must validate it before making a request. These names come from the agreed
mapping; the picker does not assert that every account has access to them.

**The first candidate is the caller's effective default.** For auto mode,
proposed `MIPMAP_AUTO_DEFAULT` is a full JSON candidate tuple, including
`tier`; its default is Codex `gpt-5.6-terra` / `medium` / `sonnet`. It may
identify an existing menu entry or add an explicit new one. Do not infer
tier from an arbitrary model name. Existing fixed-provider configuration
(including Codex's current `gpt-6-astra` default) stays unchanged and does
not implicitly become the auto default.

Build the ordered menu with the configured auto default first, followed by
the remaining entries; initial remaining order is Claude sonnet, Codex
haiku, Claude haiku, Codex opus, Claude opus. Validate configurations and
remove candidates explicitly denied by backend permission, then apply
provider/manual-tier restrictions, preserving relative order throughout.
The first remaining candidate becomes the effective default. An empty
executable/filtered menu is a local config error. This gives fallback an
exact wire meaning without a second, potentially inconsistent default field.
`prefer`, when present, is `codex|claude` and only breaks policy ties; it
does not force a provider or override budget guards.

Budget forms:

- The requested single-window form remains valid:
  `{"used_percent": 81, "window_minutes": 300, "resets_at": 1791307200}`.
  It is shorthand for one window; absent `observed_at` means observed at
  request receipt. Persisted snapshots must supply `observed_at`.
- The multi-window form above carries every applicable primary/secondary,
  weekly, or model-specific window. Do not collapse it to the currently
  displayed window. `observed_at` is a UTC epoch timestamp in seconds.
- `used_percent` is finite, in `[0,100]`; `window_minutes` is a positive
  number or null; `resets_at` is UTC epoch seconds or null. Optional `name`
  is an audit label, not a policy switch. A missing/null percent, absent
  budget, or empty windows means unknown capacity, never zero usage.
  Out-of-range utilization makes that snapshot unknown rather than
  fabricating headroom; wrong field types are validation errors.
- Window duration is informational. A known percentage still applies when
  duration/reset is unknown, but the near-reset exception requires a known
  future reset. Future `observed_at`, expired resets, or snapshots older
  than the freshness threshold make the affected budget unknown. An
  expired observation cannot prove the new window's utilization is zero.

Additive field `requested_tier?: "haiku"|"sonnet"|"opus"` supports an
explicit `/model` setting. It bypasses learned tier classification, sets
`probabilities` absent, and is logged as a manual tier request. It does
not bypass budget guards: select within that tier, with no automatic tier
drop. The caller filters the menu to that tier and supplies `requested_tier`
to avoid the learned classifier; the validator rejects mismatched candidates.
Here `/model haiku|sonnet|opus` selects a capability class; concrete model
and effort come from the configured menu. `/provider` restrictions also
filter the candidate menu before sending it. Fallback must therefore respect
both explicit controls, even when it exceeds the utilization guard.
The response's `tier` always describes the configuration actually selected.

## Response

```json
{
  "provider": "claude",
  "model": "sonnet",
  "effort": "medium",
  "tier": "sonnet",
  "why": "CLM: sonnet (p=0.86); claude has more measured budget headroom",
  "probabilities": {"haiku": 0.08, "sonnet": 0.86, "opus": 0.06},
  "fallback": false
}
```

The selected tuple must match a supplied candidate exactly. `why` explains
the head result, threshold adjustment, budget exclusions, tier drop, tie,
manual setting, unknown capacity, or fallback as applicable. It contains
no raw exceptions, credentials, task text, or numeric budget observations.
Budget numbers belong in the shared audit and transient display, so the
same `why` can safely be retained in mipmap's usage records.
`probabilities`, when present, is the validated raw CLM distribution, not
the probability that the budget
choice is correct. A threshold-adjusted or budget-dropped tier can differ
from its argmax. Jev's separate distribution belongs in audit metadata.

`fallback=true` means the exact first/default candidate was returned because
classification/policy failed or no permitted capacity choice remained.
A normal one-tier budget drop has `fallback=false`. Invalid requests fail
validation (MCP `isError`, library `ValueError`, HTTP 422); auth errors stay
401/403. Mipmap treats a transport error, timeout, invalid response, or
noncandidate result as a local default fallback and records that reason.

## Layer 1: capability tier

Use the existing `subagent-tier-v2` head and its verbatim
`SUBAGENT_INSTRUCTIONS`, `SUBAGENT_OPTIONS`, and `subagent_state()` shape.
For this new entry point only, the prompt is `task` followed, if present,
by `"\n\nContext:\n" + context`; the description is empty and subagent type
is `general-purpose`. Apply the existing redaction and clipping. Keep task
first because the encoder retains the start of its 2048-token input.
The current renderer clips instructions to 1200 characters, so context
following a long task can be clipped away entirely. V1 preserves that
trained limit; callers should supply a concise task/context, not pad it.
Candidates, provider identities, budgets, defaults, and preferences never
enter either classifier's input. Do not change existing callers' states.

Preserve `pick()`'s behavior using independently configurable v1 keys:

1. Opus wins only at probability >= `turn_pick_top_threshold` (0.8);
   a weaker opus prediction selects sonnet.
2. Haiku below `turn_pick_threshold` (0.8) selects sonnet.
3. A task shorter than `turn_pick_min_task_chars` (1000) cannot select
   haiku automatically, even if context makes the combined prompt longer.
4. Otherwise use the predicted tier. A CLM error returns the caller default,
   rather than inheriting `pick()`'s fixed middle-tier fallback.

Optional Jev second opinion uses the same budget-free state and rubric.
When `turn_pick_jev_enabled=true` and server-side Jev credentials are
configured, ask both within the shared deadline. Apply the same tier
thresholds to each result, then choose the more capable resulting tier.
This conservative v1 combination avoids inventing a calibrated ensemble;
it may cost more and must be logged. Jev failure leaves a successful CLM
result usable; CLM failure still returns the caller default. No paid LLM
judge escalation. Existing `clm_pick_model` remains unchanged.

**Known caveat:** this head was trained on subagent task descriptions, not
chat messages. Short chat turns will mostly select sonnet due to the length
guard. Do not pad a chat message to force haiku or claim its probabilities
are calibrated for this new distribution. Manual tier selection is separate.

## Layer 2: deterministic budget policy

Evaluate every window separately at one injected `now` timestamp.

1. A known candidate has room only if every applicable window has
   `used_percent + turn_pick_budget_reserve_percent <
   turn_pick_budget_ceiling_percent` (90 by default), or that window
   resets within the next `turn_pick_reset_grace_minutes` (30). Any window
   at 100% is excluded even during the grace period; a future reset does not
   give current capacity. Unknown reset times cannot qualify for grace.
2. Rank eligible candidates of the required tier: fully known budgets
   below the guard first, then unknown/partially unknown budgets, then
   known candidates allowed only by reset grace. Unknown windows never
   erase a known blocking window. Among candidates in a group with known
   usage, maximize the minimum `100 - used_percent` across applicable
   known windows. A wholly unknown candidate has no numeric score.
3. Break equal scores by `prefer`, then original candidate order. If both
   budgets are wholly unknown, use the same tie rule and explain that
   capacity is unknown. Preference cannot beat measured greater headroom.
4. If the requested tier has no eligible candidates (including a missing
   tier), try exactly one lower tier: opus -> sonnet, sonnet -> haiku.
   Do not silently upgrade or traverse further tiers. Skip the tier drop
   entirely for an explicit `requested_tier`.
5. If no choice remains, return the caller default with `fallback=true` and
   a specific `no eligible capacity` reason. This is an availability
   escape hatch and may exceed the guard; it must be visible. Do not claim
   the guard is a hard spending cap. A hard stop/wait response is a later
   contract change requiring caller support.

Budgets are subscription utilization percentages, not dollars, token
prices, or interchangeable absolute capacities. This policy compares
relative headroom only. It cannot predict how much a turn will consume or
guarantee the final utilization stays below 90%. The optional reserve is
a static buffer, not a learned consumption forecast. Shared accounts can
change between observation and execution. Use the saved snapshot without
waiting for a quota read, and refresh Codex in the background after every
chat turn, including Claude turns. This deliberately accepts imperfect
quota freshness to avoid adding about a second before each turn.

Configuration (CLM-owned, not caller-provided policy knobs):

| Key | Default | Purpose |
| --- | --- | --- |
| `turn_pick_threshold` | 0.8 | Minimum haiku probability |
| `turn_pick_top_threshold` | 0.8 | Minimum opus probability |
| `turn_pick_min_task_chars` | 1000 | Automatic haiku length guard |
| `turn_pick_budget_ceiling_percent` | 90 | Utilization guard |
| `turn_pick_budget_reserve_percent` | 0 | Static next-turn buffer |
| `turn_pick_reset_grace_minutes` | 30 | Near-reset exception |
| `turn_pick_budget_max_age_seconds` | 600 | Snapshot freshness |
| `turn_pick_timeout_seconds` | 1.5 | Classification/policy deadline |
| `turn_pick_jev_enabled` | false | Optional conservative second opinion |

Validate configuration ranges and fail to the caller default on invalid
configuration. MCP loads policy config with the existing safe config loader;
HTTP needs equivalent service-owned settings and no per-user config file.

## Timing, persistence, and audit

Mipmap starts one CLM stdio child per chat, initializes MCP once, and gives
each `tools/call` roughly two seconds. Classification/policy use a shorter
deadline; logs must not consume the remaining synchronous deadline.
The existing `pick()` can spend five seconds uploading a decision; the new
tool must not reuse that synchronous logging behavior. Use a bounded
best-effort queue, short sink timeout, and bounded shutdown flush. A logging
failure must not alter or delay a returned pick. No logging thread may
prevent process exit. A timed-out RPC is abandoned; late replies must not
be mistaken for the next turn's response.

Log CLM decisions to workflow **`routing/turn-picks`**, using the existing
D1 record envelope (`id`, `created_at`, `mode`, `state`, `questions`,
`clm`, `acted`, `worker`, `meta`). `worker` is the selected tier; model/provider
selection details live in metadata rather than pretending the tier head
was trained on model IDs. Record:

- Full candidate configurations and normalized budget snapshots, candidate
  order/default, preference, caller, and manual tier request.
- Redacted/clipped classifier state, raw CLM result, optional Jev result,
  capability tier before budget policy, selected tuple and reason, fallback.
- Policy version (`turn-picks-v1`), effective thresholds, evaluation time,
  per-candidate exclusions/scores, and whether reset grace or unknown budget
  handling determined the result. This makes decisions replayable.

The installed MCP adapter uses its existing CLM collector configuration.
For the HTTP service, set both `CLM_TURN_PICK_AUDIT_URL` to the collector's
exact `/v1/decisions` URL and `CLM_TURN_PICK_AUDIT_KEY` through service secret
configuration. Uploads have a 0.25-second timeout and run through the bounded
daemon queue. Neither variable is read from user files; a partial pair fails
service startup. A library/service embedding can instead inject a sink.
Enable this collector configuration when deploying the HTTP route; absent
configuration leaves auditing disabled for that route.

No keys, tokens, raw provider payloads, or unfiltered exceptions enter
records. Learned-model labels, when added later, use `source: "llm:<name>"`.
Audit budgets are explicitly authorized for the shared collector; local
provider budget snapshots must stay out of mipmap's git history and memory
log. Persist them in a private ignored data-dir file (atomic write, mode
0600), including observation time and subscription/model scope. On restart,
use the saved snapshot immediately; an optional startup Codex refresh runs
in the background without delaying selection. After every completed turn,
including a Claude turn, refresh Codex limits asynchronously. If another
turn starts first, use the prior snapshot; selection never waits for the
meter. Bound each read separately from the CLM RPC, coalesce overlapping
refresh requests, and reject older responses that would overwrite newer
state. A failed read must not advance the last successful observation time.
Stale/expired budgets follow the unknown-budget policy. Claude updates its
snapshot from `rate_limit_event`; stale Claude state stays unknown until a
new event. Claude has no separately authorized budget-fetch spike in this
phase.

Mipmap displays one selection line per turn, writes the pick to its activity
pane, and appends provider/model/effort/tier/why/fallback to `usage.jsonl`.
For example, `· claude opus (CLM p=0.86; codex at 81%)` uses the selected
tier's probability and transient budget data from the request. If the
chosen tier differs from CLM's prediction, show the original prediction
and adjustment instead of presenting its probability as selected confidence.
Chat/activity display can show percentages; retained usage explanations
and committed memory must not contain budget snapshots or percentages.
The memory log remains user/talk/tool/echo. Usage summaries group by provider
and tier, retaining model and legacy-record support; unknown dollar cost is
not zero dollars. Claude's reported cost is notional subscription spend;
Codex token usage does not itself establish a dollar amount.

Keep existing provider prompt construction and caching: Codex prompt on
stdin; Claude stable view prefix in the appended system prompt and the
remaining view in the user message, with no new cache markers. Routing
metadata must not be inserted into those prompts. Switching provider also
switches harness log source and usage identity for that turn.
The current harness binds one agent and source at construction; auto mode
requires a per-turn agent factory/selection seam. Freeze the selected
provider/model/effort/source for the entire turn before writing its user
messages. Dedupe keys, usage callbacks, activity, and status must all use
that frozen identity. Never attribute a Claude-selected turn to the initial
Codex agent. Selection chat/activity output uses a display-only path, not
the agent's persisted talk/tool/result stream.

## Verification before implementation is complete

Network-free tests use fake classifiers, clocks, sinks, MCP processes and
provider subprocesses. Cover threshold boundaries, the short-task guard,
manual tier selection, all-window exclusions, grace and 100% boundaries,
stale/expired/null budgets, known versus unknown ranking, preference ties,
exact one-tier drop, default fallback, validation, Jev disagreement/failure,
bounded logging, late RPC replies, and unchanged `clm_pick_model` behavior.
Verify MCP/library/HTTP parity if HTTP is implemented.

Mipmap tests must cover sticky exact slash commands, provider/model changes
between turns, restart budget persistence excluded from git/log, selection
that never waits for an in-flight meter, refresh after either provider's
turn, failed-read observation times, refresh coalescing and response ordering,
legacy usage summaries, and prompt/cache preservation. Failure to choose an agent
may use the default before execution. Never automatically replay a turn
after the chosen agent has already emitted output or run tools: it could
repeat side effects; report that execution failure instead.

After review and implementation, the requested final real check uses a
throwaway mipmap home and two tiny auto turns with different picks. If live
budgets do not naturally produce two picks, explicitly disclose a controlled
candidate/preference change or simulated budget fixture; do not present
fabricated budget pressure as a real observation. Report actual usage and
any measured/notional costs. No push, merge, or infrastructure apply.

## Step 1 evidence

See [CODEX_RATE_LIMIT_SPIKE.md](CODEX_RATE_LIMIT_SPIKE.md) for observed wire
shapes, nullable fields, evidence limits, two-run usage totals, and timing.
Use a read-only app-server sidecar alongside ephemeral exec. Metadata reads
take roughly one second, so the agreed policy runs them in the background
after every turn and selects from saved state without a blocking read.
Their bounded timeout is separate from the CLM RPC. This account
exposes a shared `codex` weekly bucket, not separate model prices or quotas.
Normalize all applicable windows and preserve bucket identity. Backend
permission explicitly denying ordinary usage removes those candidates
during the validation/filtering sequence above; missing permission is
unknown rather than denial.
Missing budget metadata follows the unknown-budget policy, without bypassing
CLM. Reject an empty executable menu locally.

## Decisions requiring company agreement

The v1 proposals above make the underspecified cases explicit: all windows
must fit; unknown capacity ranks between measured room and near-reset
pressure; the auto default is an explicit tuple (initially Codex sonnet)
and the effective filtered default is the first candidate; exhaustion
returns a visible default fallback; only one automatic tier drop is permitted; explicit
model/provider settings constrain fallback and prevent manual tier drops;
optional Jev can only increase capability; snapshots expire after ten
minutes; background refresh after either provider's turn replaces a blocking
quota preflight; and the 90% guard is a routing heuristic with a
configurable reserve rather than a guaranteed consumption cap.

The shared module plus MCP and an HTTP adapter is recommended so company
clients share one policy. A rollout-file-only budget source or moving entire
turn execution to app-server would couple mipmap more tightly to Codex
internals. The spike verified a read-only sidecar while preserving ephemeral
execution, with startup isolation and account/model-specific scope mapping
remaining implementation checks.
