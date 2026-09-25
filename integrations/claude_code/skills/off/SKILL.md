---
name: off
description: Pause the CLM hook in every Claude Code session on this machine until /clm:on. Use when the user wants CLM to stop changing subagent models or checking turns.
disable-model-invocation: true
---

Run this and tell the user the result in one line:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/clm_hook.py" --pause --data "${CLAUDE_PLUGIN_DATA}"
```

To remove the plugin entirely: `/plugin` → uninstall `clm`.
