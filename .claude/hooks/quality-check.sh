#!/usr/bin/env bash
# PostToolUse — run TypeScript and ESLint after every file edit.
# Implements iamfakeguru Forced Verification rule deterministically.
# Non-blocking (exit 0): Claude sees errors and must fix them before claiming done.

INPUT=$(cat)
FILE=$(echo "$INPUT" | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
    inp = d.get('tool_input', {})
    print(inp.get('file_path') or inp.get('path') or '')
except Exception:
    print('')
" 2>/dev/null)

# Only run on JS/TS source files
if ! echo "$FILE" | grep -qE '\.(ts|tsx|js|jsx)$'; then
  exit 0
fi

# Skip test utility files that intentionally have loose types
if echo "$FILE" | grep -qE '(\.test\.|\.spec\.|__tests__)'; then
  TSC_ARGS="--noEmit --pretty false"
else
  TSC_ARGS="--noEmit --pretty false"
fi

echo "=== TypeScript check ==="
TSC_OUTPUT=$(npx tsc $TSC_ARGS 2>&1 | head -30)
if [ -n "$TSC_OUTPUT" ]; then
  echo "$TSC_OUTPUT"
  echo "TypeScript errors detected. Fix all errors before marking this task complete."
else
  echo "No TypeScript errors."
fi

echo "=== ESLint check ==="
LINT_OUTPUT=$(npm run lint --silent 2>&1 | head -20)
if [ -n "$LINT_OUTPUT" ]; then
  echo "$LINT_OUTPUT"
  echo "ESLint errors detected. Fix all errors before marking this task complete."
else
  echo "No ESLint errors."
fi

exit 0
