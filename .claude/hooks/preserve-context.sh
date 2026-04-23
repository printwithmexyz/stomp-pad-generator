#!/usr/bin/env bash
# PreCompact — re-inject the 5 architecture hard rules before context compaction.
# Ensures these non-negotiables survive the compaction event.

cat <<'EOF'
{"additionalContext": "ARCHITECTURE RULES — re-injected before compaction: (1) No fire-and-forget async: all side effects use processing_jobs state machine (pending→processing→complete/failed). (2) Subscription state via Stripe webhook handler only — never from client or non-webhook server code. (3) No magic numbers: all constants in src/lib/constants.js. (4) App Router only — no Pages Router patterns. (5) RLS on every Supabase table — never rely on application-level checks alone. See CLAUDE.md section 7 for full detail."}
EOF
