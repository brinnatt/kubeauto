#!/usr/bin/env bash
# Execute one regression gate with durable PID/exit state for the supervisor.
# Usage: run-durable-gate.sh <state-prefix> <exit-label> <command> [args...]
set -uo pipefail

STATE_PREFIX="${1:?state prefix required}"
EXIT_LABEL="${2:?exit label required}"
shift 2
[[ "$#" -gt 0 ]] || {
  echo "ERROR: gate command required" >&2
  exit 2
}

printf '%s\n' "$$" > "${STATE_PREFIX}.pid"
rm -f "${STATE_PREFIX}.exit" "${STATE_PREFIX}.finalized"
set +e
"$@"
rc=$?
set -e
printf '%s\n' "$rc" > "${STATE_PREFIX}.exit"
# This fence is written only after the child and its EXIT diagnostics return.
printf '%s\n' "$rc" > "${STATE_PREFIX}.finalized"
printf '%s rc=%s\n' "$EXIT_LABEL" "$rc"
exit "$rc"
