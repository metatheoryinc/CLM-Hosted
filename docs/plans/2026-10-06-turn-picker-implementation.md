# Turn Picker Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Implement the reviewed CLM turn-selection contract and mipmap auto routing, with background quota refresh and unpushed commits.

**Architecture:** A standard-library CLM policy module selects capability then provider/model from caller candidates, exposed through MCP and a local-engine HTTP adapter. Mipmap owns candidate configuration, ephemeral provider execution, private saved quota snapshots, and one persistent MCP client plus a read-only Codex meter. Selection never awaits quota refresh.

**Tech Stack:** Python 3.9-compatible installed MCP code, FastAPI host, pytest; TypeScript, Node, pnpm, Vitest.

The authority for behavior is `docs/PICK_TURN.md`, including the approved post-turn refresh change. Use `/Users/jt/projects/CLM-Hosted` on `pick-turn` and `/Users/jt/.config/superpowers/worktrees/mem-mipmap/picker` on `picker`. No new worktrees, pushes, merges, or `pulumi up`. Never open `.env`, auth files, keys, or tokens; never touch `~/.mipmap`. No real provider/API calls in tests. Installed programs may reuse authentication internally for the explicitly authorized final smoke check.

## Task 1: Shared CLM policy

Create `src/clm/turn_picker.py` and `tests/test_turn_picker.py`.

1. Add a failing test that sends Codex/Claude sonnet candidates with known budgets to an injected sonnet classifier and verifies greater headroom wins.
2. Run `python -m pytest tests/test_turn_picker.py -q`; verify the failure demonstrates missing behavior.
3. Implement request/config validation, the existing tier thresholds, budget normalization/ranking, one automatic tier drop, strict manual tier, exact default fallback, and an injected clock/classifier/sink. Keep all provider/budget data outside learned state.
4. Extend tests before implementation for all-window guards, 90/100/reset boundaries, missing/stale/expired windows, unknown ranking, ties, malformed inputs, head failures, manual controls and optional Jev disagreement/failure. Preserve the trained rubric and renderer through parity tests.
5. Run the focused suite and commit the policy with a co-author trailer.

## Task 2: Installed MCP adapter and bounded audit

Modify `integrations/codex/clm_mcp.py`, `integrations/codex/install.py`, and `tests/test_codex.py`; add focused adapter tests if helpful.

1. Add failing MCP tool discovery/call tests for `clm_pick_turn`, preserving all `clm_pick_model` results and errors.
2. Run the focused tests and verify missing-tool/behavior failures.
3. Adapt the shared policy to the existing CLM config and HTTP classifier; install a copy of the same policy beside the standard-library hook. Add a bounded daemon audit queue to `routing/turn-picks`; logging never delays a pick or process exit. Do not change existing `pick()` behavior.
4. Add subprocess tests with throwaway config/home for installed imports, JSON-RPC validation, bounded classification/logging, redaction, queue failures and shutdown.
5. Run `python -m pytest tests/test_turn_picker.py tests/test_codex.py -q` and commit.

## Task 3: HTTP/library exposure

Modify `src/clm/server.py`, optionally `src/clm/client.py` / `src/clm/__init__.py`; add `tests/test_pick_turn_http.py` using the existing fake engine/server patterns.

1. Add failing tests for authenticated `POST /v1/pick-turn`, malformed requests, and parity with library/MCP policy.
2. Verify the expected failures with the focused pytest suite.
3. Add an adapter that invokes the local engine directly, shares policy/rubric/config, and sends audit records best effort to the collector. No self-HTTP classification or installed-hook import at server boot. Service settings come from explicit config/environment, not user config files.
4. Run focused pytest tests, then the CPU/network-free suite (`-m 'not network'` with offline flags), Worker tests and plugin validation. Report any unavailable prerequisite; do not download large artifacts.
5. Review the complete CLM diff independently, rerun checks, commit, and only then start mipmap implementation.

## Task 4: Mipmap configuration, MCP client and budget meter

Create `packages/cli/src/turn-picker.ts`, `packages/cli/src/budgets.ts` and corresponding tests; modify `config.ts`, `agent.ts`, `agents/claude-events.ts`, `git.ts` as needed.

1. Add failing config/client tests for auto mode, full candidate/default tuples, provider-specific effort validation, one stdio initialization, request timeout, late replies, bad/noncandidate responses and local default fallback.
2. Run the focused Vitest tests and verify failures before code changes.
3. Implement the thin MCP client (start once per chat, configurable Python/script path, roughly two-second deadline, restart/recovery without session failures). Implement normalized private atomic budget persistence with observation time and provider/bucket scope.
4. Add failing fake-process tests for app-server initialize/read, nullable windows, permission denial, sparse notifications, timeouts, refresh coalescing/ordering and close. Implement metadata-only reads after every completed turn and optional nonblocking startup read. No turn waits for the meter.
5. Add failing tests for all Claude `unifiedWindows`, normalization from fractions to percentages, and raw payload exclusion. Implement event propagation/persistence and git ignore coverage. Tests never spawn real providers.
6. Run focused tests/typecheck and commit.

## Task 5: Per-turn execution, commands and reporting

Modify `harness.ts`, `main.ts`, `usage.ts`, `status.ts`, `status-view.ts`, `tmux.ts` and related tests; preserve existing provider construction and cache tests.

1. Add failing integration tests for two turns choosing different providers/models, selection before user log writes, frozen source/dedupe/usage identity, sticky exact `/model` and `/provider` commands, and interruption during selection.
2. Verify the expected failures, then add the per-turn agent selection seam. Preserve fixed provider modes, Codex stdin, Claude appended-system-prefix caching, cancellation and queued-message semantics.
3. Add failing display/usage tests, then emit one display-only pick line and activity entry; retain tier/why/fallback/provider/model in usage without budget percentages. Summarize spend by provider/tier with legacy compatibility and unknown-cost handling.
4. Verify no automatic replay after partial provider output/tool activity; selection/setup failures use the configured effective default safely. Refresh Codex after either provider turn without delaying queue progress.
5. Run `pnpm test` and `pnpm typecheck`, update user docs/configuration guidance, and commit small changes with co-author trailers.

## Task 6: Independent review and final real check

1. Independently review spec compliance then code quality across both diffs; reproduce any defects with failing tests, fix them, and rerun the appropriate suites.
2. Rerun final CLM offline checks, Worker/plugin checks and mipmap tests/typecheck. Confirm clean worktrees and expected branches; verify trained text and `clm_pick_model` behavior remain unchanged.
3. Run exactly two cheap real auto turns in a scratch workspace and throwaway mipmap home, with a test summarizer seam so no paid summarization or `.env` loading is needed. Use real CLM MCP classification and real provider execution, preserving authentication internally without printing credentials. Arrange different picks through an explicitly disclosed supported control/menu change if natural quotas do not produce them; do not fake live pressure.
4. Capture chat selection lines, activity output, private-budget ignore evidence and `usage.jsonl`; report token usage and measured/notional dollar costs. The quota spike already used two exec runs; these final two are the separately authorized end-to-end check.
5. Leave all commits unpushed. Report what is implemented, how to run it, checks, any deployment/install step needed, and material limitations. Do not mark the task complete until both repository changes and the real check are accounted for.
