---
name: on
description: Resume the CLM hook after /clm:off.
disable-model-invocation: true
---

Run this and tell the user the result in one line:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/clm_hook.py" --resume --data "${CLAUDE_PLUGIN_DATA}"
```
