# CLM-Hosted: notes for coding agents

Metatheory's hosted CLM (Contrastive Language Model): the upstream CLM serving engine, a
public deployment at https://clm.metatheory.dev that our agents use as a Jev replacement, and
integrations that put CLM inside Claude Code and Codex. This repo is **public**.

## What is where

- `src/clm/`: the `clm` package. `engine.py` / `heads.py` (projection heads over a frozen
  Qwen3-8B encoder), `server.py` (System One API: `/v1/systemone`, `/v1/rank`, `/v1/verify`,
  `/v1/encoder`, `/v1/admin/heads`), `decisions.py` + `decisions_cli.py` (`clm-decisions`:
  the Router for shadow/active routing, `report`, `export`, `label`), `heads_cli.py` (`clm-heads`).
- `infra/`: the whole stack as Pulumi YAML (RunPod Pod running vLLM + the API behind a Cloudflare
  Tunnel; Cloudflare Worker gateway in `infra/gateway/worker.js` with per-agent keys, rate limits,
  the D1 decisions collector and the admin gate). See `infra/README.md`.
- `Dockerfile`, `deploy/entrypoint.sh`: the Pod image (built by GitHub Actions to GHCR on push to main).
- `integrations/claude_code/`: the `clm` Claude Code plugin (this repo is its marketplace via
  `.claude-plugin/marketplace.json`). `clm_hook.py` is one hook for all events: tool-call
  classifier, subagent model downgrades, behavior checks at Stop; `clm_behaviors.py` renders a
  turn as a trace (Claude Code transcripts and Codex rollouts); `behaviors.json`, `rubrics/`.
- `integrations/codex/install.py`: installs that same hook into Codex (`~/.codex/hooks.json`).
- `train/finetune.py`: head training (`--task choice` for typed decisions). `evaluation/`:
  benchmarks (`behavior_eval.py` = Respan behavior benchmark, `judge_eval.py`, `bon_eval.py`).
- `docs/ROUTING.md`: how to route decisions with CLM (shadow → measure → active).

## Build and test

```bash
pip install torch --index-url https://download.pytorch.org/whl/cpu && pip install -e ".[serve,test]"
pytest -q                               # CPU only; the encoder is faked
node infra/gateway/worker.test.js       # the Worker
claude plugin validate .                # the plugin and marketplace manifests
```

CI runs the same (`.github/workflows/tests.yml`); the image only builds when tests pass.
`tests/test_recipe.py` needs the Qwen tokenizer download (`transformers`).

## Rules

- **Never print, log or commit keys.** People get personal keys by signing in (`/clm:login`, device login
  at `/login` through Cloudflare Access); the gateway stores only their hashes. Static agent keys live in Pulumi config (`agentKeys`) and in
  `~/.config/clm/claude-code.json` (mode 600). Everyone uses the `default` agent key; the `admin`
  key is only for `/v1/admin/*` (`clm-heads upload`). Read keys into env vars, never echo them.
- **Don't run `pulumi up`.** The maintainer runs it; propose the change and say what it will do.
  Deploying a new image = CI pushes `ghcr.io/metatheoryinc/clm-hosted:<sha>`, then `imageTag` in
  `infra/Pulumi.yaml` is bumped and the maintainer runs `pulumi up`.
- **Commit straight to main** and push; no feature branches for this repo.
- **Trained heads depend on exact text.** Don't change `SUBAGENT_INSTRUCTIONS` / `SUBAGENT_OPTIONS`
  or `INSTRUCTIONS` / `OPTIONS` in `clm_hook.py`, `clm_behaviors.INSTRUCTIONS` / `OPTIONS` /
  `render()`, or the state formats, without retraining: the uploaded heads (`tool-risk-v1`,
  `subagent-tier-v2`, `behavior-v1`) were trained on them. `behavior_eval.py` imports the hook's
  renderer so training and serving stay identical.
- **The hook is standard-library only and must run on Python 3.9** (macOS system python3). It must
  never break a session: any error or timeout means "do nothing". CLM may only make things
  stricter or cheaper (deny/ask, downgrade), never approve.
- CLM's encoder window is 2048 tokens and it keeps the **start** of a state; states are built to
  fit (tool inputs clipped from the middle to 6000 chars, traces rendered to 5600 chars).
- Uploaded heads live on the Pod volume; copies are in the maintainer's `~/.config/clm/heads/`.
  If the Pod is replaced, re-upload them with `clm-heads upload --name <name> <file>` (admin key).
- Decision records go to the shared D1 collector (`/v1/decisions`), all under the `default` key:
  treat what's in it as the team's data. Model-made labels carry `source: "llm:<name>"`.
- Large downloads (models, datasets) go to a scratch or remote location, not the repo; the
  maintainer's disk is small.
