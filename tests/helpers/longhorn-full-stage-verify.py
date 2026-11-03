#!/usr/bin/env python3
"""Read-only validation of one accepted full-regression stage."""
import importlib.util
from pathlib import Path
import sys

BASE = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('longhorn_full_evidence', BASE/'tests/helpers/longhorn-full-evidence.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

if len(sys.argv) != 2 or sys.argv[1] not in module.MODES:
    raise SystemExit('usage: longhorn-full-stage-verify.py STAGE')
try:
    module.accepted_stage(sys.argv[1])
except Exception as exc:
    print(f'LONGHORN_FULL_STAGE_VERIFY_FAIL stage={sys.argv[1]} error={exc}', file=sys.stderr)
    raise
print('LONGHORN_FULL_STAGE_VERIFIED stage='+sys.argv[1], flush=True)
