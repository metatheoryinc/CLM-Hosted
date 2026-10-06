#!/usr/bin/env bash
# Register every secret value of the stack with GitHub's log masking (the repo is public, so its
# logs are too). JSON secrets are masked whole and leaf by leaf, so a provider error that prints a
# decoded value still shows ***. Prints nothing itself; workflow commands are not echoed.
set -euo pipefail
pulumi config --stack "$STACK" --show-secrets --json |
  jq -r 'to_entries[] | select(.value.secret == true) | .value.value // empty' |
  while IFS= read -r v; do
    [ -n "$v" ] || continue
    echo "::add-mask::$v"
    # leaves of a JSON object or array (e.g. agentKeys), each masked on its own
    if parsed=$(printf '%s' "$v" | jq -r '.. | scalars | tostring' 2>/dev/null); then
      while IFS= read -r leaf; do
        [ "${#leaf}" -ge 8 ] && echo "::add-mask::$leaf"
      done <<< "$parsed"
    fi
  done
