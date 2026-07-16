#!/bin/bash
# Run kubeauto unit tests (no cluster required).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export PYTHONPATH="$ROOT"
PY="$(command -v python3.12 || command -v python3)"
"$PY" -m unittest discover -s "$ROOT/tests/unit" -p 'test_*.py' -v
