---
name: status
description: Show what the CLM hook is doing - its modes, whether it is paused, and the subagent downgrades, behavior flags and blocked tool calls it made recently. Use when the user asks what CLM did or whether CLM is on.
---

Run this and show the user its output as is (it never prints the agent key):

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/clm_hook.py" --status --data "${CLAUDE_PLUGIN_DATA}"
```

If they want the team-wide numbers, `clm-decisions report https://clm.metatheory.dev --workflow <workflow>`
(from the CLM-Hosted repo's `clm` package) measures each workflow: `routing/claude-code-subagents`,
`behavior/claude-code`, `routing/claude-code-tools`.
