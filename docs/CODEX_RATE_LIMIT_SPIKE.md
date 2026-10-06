# Codex subscription budget spike

2026-10-06, local Codex CLI 0.154.0. Step 1 of the mipmap auto-picker
request. Two tiny real inference runs; no application implementation.

## Recommended collection path

Keep existing `codex exec --json --ephemeral --ignore-user-config` turns.
Use a separate long-lived `codex app-server --stdio` process as a read-only
subscription meter. Initialize it once, send `initialized`, and call:

```json
{
  "id": 2,
  "method": "account/rateLimits/read",
  "params": {"excludeResetCreditDetails": true}
}
```

This performs an authenticated quota metadata read, without a model turn.
The local schema says `excludeResetCreditDetails` skips a separate reset
credit detail lookup. Do not enable `supportsLunaReserve`: it can record
experiment exposure and is unrelated to this ordinary budget meter.

Agreed collection policy after review: select immediately from the latest
persisted snapshot, without a quota read before the turn. Refresh Codex
limits in the background after every completed chat turn, including Claude
turns, carrying the receipt time as `observed_at`. Reading after Claude
turns prevents an old Codex snapshot from persisting merely because routing
switched providers. Each metadata read has its own bounded timeout
(proposed two seconds), separate from the roughly two-second CLM RPC timeout.

If the next turn starts while a refresh is running, use the prior snapshot
without waiting. Failed reads retain the last successful snapshot and its
original observation time; expired/stale snapshots become unknown. Reads
do not select a model locally or bypass CLM. Coalesce refresh requests while
a read is in flight and prevent an older response from replacing a newer
snapshot. On restart, load the saved state immediately; a startup refresh
may run in the background but must not delay the first turn. Accept that
other apps can consume quota between observations: this is a routing
heuristic, not a capacity reservation.

The [official app-server documentation](https://learn.chatgpt.com/docs/app-server#6-rate-limits-chatgpt)
describes this read, its multi-bucket view, window durations, and reset
timestamps. The installed CLI's generated schema is authoritative for
nullable fields on this machine; regenerate it when upgrading the CLI.

## Results and exact field mapping

| Method | Observation | Suitability |
| --- | --- | --- |
| Ephemeral `exec --json` | Final token usage; no rate-limit event | Keep for execution, cannot meter quotas |
| Persistent `exec --json` | Same usage stream; rollout adds `event_msg` / `token_count` / `rate_limits` | Useful verification, would require storing transcripts |
| App-server `account/rateLimits/read` | Direct snapshot, all returned buckets, no inference | Recommended meter |

Rollout shape below uses **illustrative utilization/reset values**, not a
persisted live account snapshot. The observed primary duration was 10080
minutes (weekly) and secondary was null; primary must not be assumed to
mean a five-hour window.

```json
{
  "type": "event_msg",
  "payload": {
    "type": "token_count",
    "info": {"last_token_usage": {"input_tokens": 14306, "output_tokens": 5}},
    "rate_limits": {
      "limit_id": "codex",
      "plan_type": "pro",
      "primary": {
        "used_percent": 25,
        "window_minutes": 10080,
        "resets_at": 1791760000
      },
      "secondary": null
    }
  }
}
```

Equivalent app-server response example (values are also illustrative):

```json
{
  "id": 2,
  "result": {
    "ordinaryUsageAllowed": true,
    "rateLimits": {
      "limitId": "codex", "planType": "pro",
      "primary": {"usedPercent": 25, "windowDurationMins": 10080, "resetsAt": 1791760000},
      "secondary": null
    },
    "rateLimitsByLimitId": {
      "codex": {
        "limitId": "codex", "planType": "pro",
        "primary": {"usedPercent": 25, "windowDurationMins": 10080, "resetsAt": 1791760000},
        "secondary": null
      }
    }
  }
}
```

| Rollout | App-server | CLM candidate budget |
| --- | --- | --- |
| `used_percent` | `usedPercent` | `used_percent` |
| `window_minutes` | `windowDurationMins` | `window_minutes` |
| `resets_at` | `resetsAt` | `resets_at` |
| `limit_id` | `limitId` / bucket map key | Persisted source/scope metadata |
| `primary`, `secondary` | `primary`, `secondary` | Named applicable windows |

The local v2 schema requires response `rateLimits`; optional
`rateLimitsByLimitId` is an object or null. A snapshot's `primary`,
`secondary`, `limitId`, `planType`, and `normalModelSlug` can be absent or
null. Each window requires integer `usedPercent`; `windowDurationMins`
and Unix-second `resetsAt` can be absent or null. Preserve this distinction;
null secondary is not a zero-used second window.

Prefer the multi-bucket map when present, and use the legacy snapshot only
when the map is unavailable. Retain bucket identity rather than flattening
unrelated buckets onto every model. This account returned only the `codex`
bucket, which applies as shared quota to its ordinary Codex candidates.
There is no experimentally verified mapping for additional bucket IDs.
Unknown buckets must not be guessed from their names; a future integration
needs an explicit mapping before using them for candidate eligibility.
The map is not a price list: it cannot establish per-model dollar cost or
consumption weights. CLM still chooses tier and policy within shared quota.

`ordinaryUsageAllowed` is optional/nullable backend permission for ordinary
usage. The observed read returned true. The schema explicitly warns that
null is unavailable and that percentages/reset times cannot establish
recovery. A future caller must omit candidates explicitly denied by the
backend, rather than infer access from headroom. Null is unknown permission,
not proof of denial. Quota percentages alone do not establish model access.
No credit purchase, reset consumption, account mutation, or special fallback
entitlement is part of this spike.

`account/rateLimits/updated` is a sparse rolling notification. Its schema
requires `rateLimits` but warns that nullable metadata must not clear a
prior observation. Merge defined updates into the prior matching bucket
or, preferably for v1, refetch a complete snapshot. Notifications in a
sidecar are not proven to stream updates caused by a separate `exec` process;
the explicit read remains necessary.

## Measured overhead and inference usage

The investigation observed a cold start/read at 940 ms. In a persistent
sidecar, initialization took 202 ms; reads then took about 999 ms and 949 ms.
A later cold read took 1110 ms. The parent independently repeated the
read-only probe: 1335 ms and the same primary/secondary snapshot. Retaining
the process avoids repeated startup, but the network read remains about a
second. These are samples, not a latency guarantee.

The read after the separate persistent exec agreed with that run's rollout.
Utilization is integral, and neither tiny run changed the displayed
percentage, so this demonstrates visibility of the account snapshot,
not sensitivity to a one-turn increment or absence of backend lag.

Both inference runs used `gpt-5.6-luna`, low effort, a read-only sandbox,
a scratch working directory, and a one-word reply without tools:

| Run | Input | Cached input (included in input) | Output | Reasoning |
| --- | ---: | ---: | ---: | ---: |
| Ephemeral | 15880 | 8960 | 5 | 0 |
| Persistent | 14306 | 9984 | 5 | 0 |
| Total | 30186 | 18944 | 10 | 0 |

Neither run reported a dollar cost. They used ChatGPT subscription quota;
token usage cannot be converted to an actual paid API charge from this
evidence. No third inference run was needed. Read-only metadata probes
started no model turns.

## Reproduction and evidence limits

Generate schemas without inference or reading credential files:

```sh
codex --version
codex app-server generate-json-schema --experimental --out <scratch-schema-dir>
```

Inspect `GetAccountRateLimitsParams`, `GetAccountRateLimitsResponse`,
`RateLimitSnapshot`, `RateLimitWindow`, and
`AccountRateLimitsUpdatedNotification` under `definitions` in the generated
combined v2 schema. Start `codex app-server --stdio`, send `initialize`
with client name/version, send `initialized`, then the read shown above.
Filter output to window/scope/permission fields; never print account details
or raw RPC envelopes. Reuse the existing login without opening auth files.

The safe scratch artifacts from this investigation are under
`/tmp/mipmap-codex-schema.iTeaAI/`: generated schemas, `read-rate-limits.js`,
`exec-run-evidence.safe.json`, and `extract-persistent-exec-evidence.js`.
They are temporary, not a runtime dependency or committed live budget store.
The parent verified the schema and live read, and independently extracted
the persistent run's one `token_count` event and usage totals. The extractor
locates only the known spike session by filename and emits no transcript
text. The ephemeral run intentionally saved no session file; its filtered
stdout was captured during the experiment but cannot be reconstructed
from session storage. Its usage figures remain the investigator's capture.

`app-server --help` does not expose exec's `--ignore-user-config`; sidecar
startup can load user configuration. This read-only probe created no thread
and executed no tool. Production integration still needs to verify isolated
startup, bounded shutdown, stderr redaction, and that the meter creates no
agent turns. Keep exec's existing isolation flags and prompt construction.

Neither repository's implementation was changed in the spike. Investigators
did not open `.env` or credential files or print keys/tokens; Codex reused
its existing login internally. `~/.mipmap` was untouched.

The corresponding proposed company contract is [PICK_TURN.md](PICK_TURN.md).
