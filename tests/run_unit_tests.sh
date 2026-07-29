#!/bin/bash
# Run kubeauto unit tests (no cluster required).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export PYTHONPATH="$ROOT"
UNIT_LOG_DIR="$(mktemp -d /tmp/kubeauto-unit-logs.XXXXXX)"
trap 'rm -rf "$UNIT_LOG_DIR"' EXIT
export KUBEAUTO_LOG_DIR="$UNIT_LOG_DIR"
VENV_PY="$ROOT/.venv/bin/python"
if [[ -x "$VENV_PY" ]]; then
  PY="$VENV_PY"
else
  PY="$(command -v python3.12 || command -v python3)"
fi
"$PY" -m unittest discover -s "$ROOT/tests/unit" -p 'test_*.py' -v
