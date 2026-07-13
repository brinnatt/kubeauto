#!/bin/bash
# Run kubeauto unit tests (no cluster required).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export PYTHONPATH="$ROOT"
python3 -m unittest discover -s "$ROOT/tests/unit" -p 'test_*.py' -v
