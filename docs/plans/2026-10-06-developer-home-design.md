# CLM and MipMap developer home

Approved by the user on October 6, 2026. The home page must explain what these tools are and why developers should use them before showing installation commands.

## Content and visual design

Lead with “Keep the context. Match the model to the work.” Explain MipMap as an ongoing local conversation that keeps recent context detailed and lets the agent recover older source messages. Explain CLM as a hosted decision service that routes suitable work between available models and checks for common agent behavior mistakes. Avoid guaranteed savings, correctness claims, fabricated benchmarks, and implying that model selection is perfect.

Use concrete scenarios: returning to a project after a week; choosing a model for a quick lookup versus difficult debugging; flagging a claim of testing without supporting evidence. Show a four-step combined workflow: continue the conversation, ask for a change, choose from available models, retrieve relevant history and work in the repo. Clarify that CLM can be installed in existing agents independently of MipMap.

Use a restrained, polished responsive visual style with the existing CLM red mark, readable typography, generous spacing, and a simple memory-resolution illustration made with HTML/CSS. Navigation includes CLM, MipMap, guides, playground, and a prominent Sign in link to /login. Installation commands live in three practical guides: Claude Code, Codex, and MipMap. Each includes prerequisites, login, verification, update/help links, billing, and relevant privacy boundaries.

## Serving and compatibility

Reuse the FastAPI static support; add no frontend build dependency or external runtime assets. Serve the new home at / and /index.html. Preserve the existing playground at /playground, with absolute asset URLs and legacy #r= shared-link forwarding. Keep local playground functionality working. Serve guides with stable links under /guides/.

Allow only exact public GET/HEAD page and asset routes through the gateway before API-key authentication. Forward public requests without credentials, cookies, or agent identity; never attach the upstream API key. All API and admin auth, rate limits, login/device routes, and sanitized health behavior remain intact. No broad prefix bypasses.

No changes to trained hooks, model policy, private config, or MipMap data. No paid model turns or real login/key generation during verification. No deployment or remote push without the user's authorization.

## Validation

Use fake-engine HTTP tests for home, guide routes/assets, playground compatibility, and ui=False. Add gateway tests for exact anonymous GET/HEAD access, nonpublic paths and methods staying protected, and stripped credentials. Run the existing offline Python suite and Worker suite. Inspect desktop/mobile layouts and exercise navigation/copy links in a local browser. Review copy against current source/README, including MipMap's hosted /login and separate paid Anthropic summarization.
