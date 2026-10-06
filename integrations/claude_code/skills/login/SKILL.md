---
name: login
description: Get a personal CLM key by signing in with a Metatheory Google account. Use when the user wants to set up CLM, connect CLM, log in to CLM, or replace the shared key with their own.
disable-model-invocation: true
---

1. Run this and show the user the link and code it prints, exactly as printed:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/clm_hook.py" --login start --data "${CLAUDE_PLUGIN_DATA}"
```

2. In the same reply, tell them to open the link, sign in with their Metatheory Google account and approve
the code, then run this with a 10-minute timeout; it waits for the approval and saves the key:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/clm_hook.py" --login finish --data "${CLAUDE_PLUGIN_DATA}"
```

3. Tell them the result in one line. New Claude Code sessions use the key. Never print or repeat the key.
