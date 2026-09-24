#!/usr/bin/env bash
# Ordered complete tools regression. Each live stage retains its own scoped runner and cleanup.
set -Eeuo pipefail

ROOT="${TOOLS_ROOT:?TOOLS_ROOT is required}"
RUNNER="$ROOT/tests/run_tools_regression.sh"
PY="${PYTHON:?PYTHON is required}"

run_stage() {
  local option="$1"
  echo "TOOLS_FULL_STAGE_START option=$option"
  bash "$RUNNER" "$option"
  echo "TOOLS_FULL_STAGE_PASS option=$option"
}

run_stage --preflight
"$PY" -m unittest discover -s "$ROOT/tests/unit" -p 'test_tools_*.py'
echo "TOOLS_FULL_STAGE_PASS option=tools-unit"
run_stage --star-artifact-prepare
run_stage --build-only
run_stage --cross-live
run_stage --calico-live
run_stage --kube-backup-live
run_stage --kafka-live
run_stage --kube-publish-live
run_stage --migration-live
run_stage --mybackup-live
run_stage --mylogi-backup-live
run_stage --star-live
echo "TOOLS_FULL_INNER_PASS"
