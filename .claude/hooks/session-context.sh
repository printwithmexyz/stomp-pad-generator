#!/usr/bin/env bash
# SessionStart — inject git branch and a reminder to check the roadmap.
# Keeps Claude oriented at the start of every session.

BRANCH=$(git branch --show-current 2>/dev/null || echo "unknown")
DIRTY=$(git status --porcelain 2>/dev/null | wc -l | tr -d ' ')

if [ "$DIRTY" -gt 0 ]; then
  STATUS_NOTE="$DIRTY uncommitted file(s) present."
else
  STATUS_NOTE="Working tree clean."
fi

cat <<EOF
{"additionalContext": "Session context: git branch '$BRANCH'. $STATUS_NOTE Check docs/roadmap.md for the current phase before starting any task. Architecture hard rules are in CLAUDE.md section 7."}
EOF
