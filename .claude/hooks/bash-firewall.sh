#!/usr/bin/env bash
# PreToolUse — block destructive Bash commands not already covered by deny list
# Provides explicit error messages back to Claude rather than silent rejection.

INPUT=$(cat)
CMD=$(echo "$INPUT" | python3 -c "
import sys, json
try:
    print(json.load(sys.stdin).get('command', ''))
except Exception:
    print('')
" 2>/dev/null)

if echo "$CMD" | grep -qE '(^|[;&|])\s*(rm\s+-rf|git\s+push\s+(-f\b|--force)|git\s+reset\s+--hard)'; then
  echo "Blocked: destructive command detected. All changes go through feature branches and PRs. Use 'git stash' to preserve work." >&2
  exit 2
fi

exit 0
