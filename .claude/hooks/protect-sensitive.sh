#!/usr/bin/env bash
# PreToolUse — block writes to .env files and the sealed base migration.
# Belt-and-suspenders on top of settings.json deny list.

INPUT=$(cat)
FILE=$(echo "$INPUT" | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
    print(d.get('file_path') or d.get('path') or '')
except Exception:
    print('')
" 2>/dev/null)

if echo "$FILE" | grep -qE '(^|/)(\.env|\.env\.[a-z]+|\.env\.local|\.env\.production[^/]*)$'; then
  echo "Blocked: .env files must be edited manually outside Claude Code. Never write secrets through an agent." >&2
  exit 2
fi

if echo "$FILE" | grep -q '00000000000000_base_schema.sql'; then
  echo "Blocked: the base migration is sealed. Create a new timestamped migration file instead (e.g. supabase/migrations/YYYYMMDDHHMMSS_description.sql)." >&2
  exit 2
fi

exit 0
