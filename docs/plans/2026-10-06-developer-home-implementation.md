# Developer Home Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Build an explanation-first public CLM/MipMap home with practical setup guides and existing team login.

**Architecture:** Keep the current static HTML/CSS/JS and FastAPI serving model. Move the playground to a stable route, add home/guides, and allow only exact public GET/HEAD paths at the gateway without upstream credentials. Existing API and login authentication remain unchanged.

**Tech Stack:** Static HTML/CSS/JavaScript, FastAPI, Cloudflare Worker JavaScript, pytest and Node tests.

---

### Task 1: Add page routing and preserve the playground

**Files:** Modify `src/clm/server.py`, preserve `src/clm/static/index.html` as `src/clm/static/playground.html`; create `tests/test_public_ui.py`.

1. Add meaningful fake-engine HTTP tests: root is the developer home; every guide responds; playground retains its interactive markup/assets; trailing slash variants work or canonically redirect; ui=False omits static pages; assets use cache revalidation.
2. Run the focused tests and confirm the new route assertions fail before implementation.
3. Add explicit page routes and cache-busted absolute asset URLs without changing existing API routes or requiring encoder downloads.
4. Run `HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 /tmp/clm-turn-tests-20261006/bin/python -m pytest -q tests/test_public_ui.py` and confirm all pass.

### Task 2: Build the home and three guides

**Files:** Create home/guide HTML, shared home CSS and minimal copy/legacy-link JS under `src/clm/static/`. Update README links only where needed.

1. Implement the approved content/visual design in `2026-10-06-developer-home-design.md`, with real examples and no unsupported promises.
2. Verify all commands against current integrations and `/Users/jt/projects/mem-mipmap/Readme.MD` plus `packages/cli/src/clm-login.ts`. Never read private config or memory.
3. Make keyboard navigation, focus states, narrow layouts, copy feedback, and no-JS reading work. Preserve legacy `#r=` links by forwarding them to the playground with query/hash intact.
4. Inspect all pages in a browser at desktop and mobile widths; exercise navigation, anchors, copy buttons, and legacy links.

### Task 3: Expose only public UI paths at the gateway

**Files:** Modify `infra/gateway/worker.js` and `infra/gateway/worker.test.js`.

1. Add tests for anonymous exact GET/HEAD paths, refusal of nonpublic/mutation requests, and no Authorization/Cookie/CF Access/agent headers forwarded on public paths. Run `node infra/gateway/worker.test.js` and observe intended failures.
2. Add an explicit path allowlist and forward public requests using a small safe header allowlist. Do not inject upstream API credentials or broadly open static path prefixes.
3. Run the Worker suite and confirm all existing API/login/device tests and new public-route tests pass.

### Task 4: Review and verify

1. Review the diff against the approved design, inspect public copy and installed command paths, and independently review auth boundaries.
2. Run the complete offline Python suite and Worker suite. Run `git diff --check` and static link/asset checks.
3. Fix any review defects, repeating relevant checks only when changed.
4. Save the verified work locally and show the home to the user. Do not run Pulumi, push, or claim that production changed.
