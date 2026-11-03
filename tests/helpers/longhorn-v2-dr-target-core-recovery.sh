#!/usr/bin/env bash
# Retire a target Kubernetes core after native Longhorn storage was already
# recovered. Installation recovery preserves the target core for DR checks;
# this path consumes only an owner state proving native storage is gone.
set -Eeuo pipefail
BASE=/usr/local/kubeauto
cd "$BASE"
PY="$BASE/.venv/bin/python"
TARGET="$BASE/tests/helpers/longhorn-v2-dr-target.py"
export PYTHONPATH="$BASE" PATH="$BASE/kube-bin:$BASE/extra-bin:$PATH"
export KUBECONFIG="$BASE/clusters/longhorn-v2-dr-target/kubectl.kubeconfig"
[[ "${LONGHORN_PARENT_LEASE_HELD:-no}" == yes ]] || exit 1

source_before="$($PY "$TARGET" source-boundary)"
$PY "$TARGET" core-identity

# Installation recovery owns the native-uninstall evidence. Refuse any
# retained namespace/CRD or active storage receipt instead of guessing.
$PY - <<'PY'
import importlib.util
from pathlib import Path
base=Path('/usr/local/kubeauto')
spec=importlib.util.spec_from_file_location('target', base/'tests/helpers/longhorn-v2-dr-target.py')
target=importlib.util.module_from_spec(spec); spec.loader.exec_module(target)
lab=target.fixture(); state=lab.owner()
if state.get('namespace_uid') or state.get('disks') or state.get('relays') or not state.get('native_uninstalled'):
    raise SystemExit('target core recovery requires completed native uninstall and no active owner fixtures')
if lab.get('namespace','longhorn-system') or any(
    item.get('metadata',{}).get('name','').endswith('.longhorn.io')
    for item in lab.get('crd').get('items', [])):
    raise SystemExit('target core recovery refuses retained Longhorn resources')
receipt=lab.DIRECTORY/'yml/longhorn/installation.json'
native=lab.DIRECTORY/'yml/longhorn/block-disks.json'
if receipt.exists() or native.exists() or receipt.is_symlink() or native.is_symlink():
    raise SystemExit('target core recovery refuses active native receipts')
PY

$PY "$TARGET" core-cleanup
bash "$BASE/tests/helpers/lab-wipe-nodes.sh" --verify --rocky-only 192.168.47.149

source_after="$($PY "$TARGET" source-boundary)"
before_boundary="$(grep '^LONGHORN_V2_DR_SOURCE_BOUNDARY ' <<<"$source_before")"
after_boundary="$(grep '^LONGHORN_V2_DR_SOURCE_BOUNDARY ' <<<"$source_after")"
[[ "$before_boundary" == "$after_boundary" ]]
echo LONGHORN_V2_DR_TARGET_CORE_RECOVERY_SCOPED_CLEAN_PASS
echo LONGHORN_V2_DR_TARGET_CORE_RECOVERY_PASS
