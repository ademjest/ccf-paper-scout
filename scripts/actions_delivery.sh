#!/usr/bin/env bash
# Shared Daily/Manual gate: no SMTP until pending evidence is remote.
set -euo pipefail
scout_pid=''
cleanup() {
  if [ -n "$scout_pid" ] && kill -0 "$scout_pid" 2>/dev/null; then
    kill "$scout_pid" 2>/dev/null || true
    wait "$scout_pid" 2>/dev/null || true
  fi
  rm -f .runtime-state/state/.delivery-approved
}
trap cleanup EXIT INT TERM
ccf-paper-scout run --config config.action.json --output recommendations.md & scout_pid=$!
while [ ! -f .runtime-state/state/.delivery-pending ] && kill -0 "$scout_pid" 2>/dev/null; do sleep 1; done
if [ -f .runtime-state/state/.delivery-pending ]; then
  python scripts/actions_state.py pack --dir .runtime-state/state --check-only
  python scripts/actions_state.py persist-pending --dir .runtime-state/state
  touch .runtime-state/state/.delivery-approved
fi
wait "$scout_pid"
scout_pid=''
