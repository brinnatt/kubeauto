#!/bin/bash
# Enterprise regression driver — executes tests/enterprise-test-matrix.yaml coverage.
# Run from dev host with network to lab: bash tests/run_enterprise_regression.sh
set -euo pipefail

require_command() {
  command -v "$1" >/dev/null 2>&1 || {
    echo "ERROR: required command '$1' is not installed." >&2
    exit 127
  }
}

require_command sshpass
require_command rsync

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PASS="${KUBEAUTO_SSH_PASS:-123456}"
HOST138="ubuntu@192.168.47.138"
HOST130="root@192.168.47.130"
LOG="${ROOT}/logs/enterprise-regression-$(date +%Y%m%d-%H%M).log"
mkdir -p "${ROOT}/logs"
exec > >(tee -a "$LOG") 2>&1

pass() { echo "[PASS] $*"; }
fail() { echo "[FAIL] $*"; exit 1; }
run()  { echo ">>> $*"; "$@"; }

ssh138() { sshpass -p "$PASS" ssh -o StrictHostKeyChecking=no "$HOST138" "$@"; }
ssh130() { sshpass -p "$PASS" ssh -o StrictHostKeyChecking=no "$HOST130" "$@"; }
scp138() { sshpass -p "$PASS" scp -o StrictHostKeyChecking=no "$@"; }

echo "========== PHASE 0: local unit tests (G0) =========="
run bash "$ROOT/tests/run_unit_tests.sh"
pass G0-local-unit

echo "========== PHASE 1: sync source to Ubuntu aio 138 + jumper 130 =========="
run bash "$ROOT/tests/helpers/sync-kubeauto.sh" "$HOST138" "$PASS"
run bash "$ROOT/tests/helpers/sync-kubeauto.sh" "$HOST130" "$PASS"
pass G0-deploy-sync

echo "========== PHASE 1b: bootstrap brinnatt mirrors + upload defaults =========="
# Until CI dual-pushes new images, retag upstream → brinnatt/* then upload to local registry.
ssh138 "sudo bash /usr/local/kubeauto/tests/helpers/bootstrap-brinnatt-mirrors.sh"
ssh138 "sudo bash -s" <<'REMOTE138_IMG'
set -euo pipefail
export PYTHONPATH=/usr/local/kubeauto
export PATH=/usr/local/bin:/usr/bin:$PATH
kubecli download -X </dev/null
# CNI / addon images used by Tier2 + setup 07
for c in flannel cilium kube-router kube-ovn prometheus ingress-nginx network-check local-path-provisioner; do
  kubecli download -E "$c" </dev/null || true
done
echo IMG_PREP_138_OK
REMOTE138_IMG
ssh130 "BOOTSTRAP_MODE=defaults bash /usr/local/kubeauto/tests/helpers/bootstrap-brinnatt-mirrors.sh"
ssh130 "bash -s" <<'REMOTE130_IMG'
set -euo pipefail
export PYTHONPATH=/usr/local/kubeauto
export PATH=/usr/local/bin:/usr/bin:$PATH
kubecli download -X </dev/null
echo IMG_PREP_130_OK
REMOTE130_IMG
pass G0-brinnatt-bootstrap

echo "========== PHASE 2: Ubuntu aio 138 preflight (G0/G1) =========="
ssh138 "sudo bash -s" <<'REMOTE138_PREFLIGHT'
set -euo pipefail
BASE=/usr/local/kubeauto
export PYTHONPATH="$BASE"
K=kubecli

python3 -c "
from common.utils import run_command
from common.ansible_python import ansible_python_policy, format_policy_summary
r = run_command(['ansible', '--version'], capture=True, check=False)
assert r.returncode == 0, r.stderr or r.stdout
p = ansible_python_policy()
assert p.core_version[0] >= 2, p.core_version
print(format_policy_summary(p))
"
bash "$BASE/tests/run_unit_tests.sh"
$K version
$K list
$K completion bash >/dev/null
$K completion zsh >/dev/null
echo PREFLIGHT_OK
REMOTE138_PREFLIGHT
pass G0-138-preflight

echo "========== PHASE 3: jumper 130 preflight + k8s-dev (G7) =========="
ssh130 "bash -s" <<'REMOTE130'
set -euo pipefail
BASE=/usr/local/kubeauto
export PYTHONPATH="$BASE"
export PATH="/usr/local/bin:/usr/bin:$PATH"
# Rocky jumper: prefer python3.12 (system python3 may still be 3.6)
PY="$(command -v python3.12 || command -v python3)"

# Deploy source wrapper if frozen binary only
if ! "$PY" -c "import common.ansible_python" 2>/dev/null; then
  echo "common module missing on 130 (py=$PY)"
  exit 1
fi

"$PY" -c "
from common.utils import run_command
from common.ansible_python import ansible_python_policy, format_policy_summary
r = run_command(['ansible', '--version'], capture=True, check=False)
assert r.returncode == 0
p = ansible_python_policy()
assert p.core_version == (2, 16), p.core_version
print(format_policy_summary(p))
"
bash "$BASE/tests/run_unit_tests.sh"

kubecli system -a --user root --password 123456 192.168.47.131-137 </dev/null

# k8s-dev: policy preflight + setup (non-interactive)
cd "$BASE"
if [ -d clusters/k8s-dev ]; then
  kubecli setup k8s-dev 90 </dev/null
  echo K8S_DEV_SETUP_OK
else
  echo "k8s-dev cluster dir missing — skip setup"
fi
REMOTE130
pass G7-jumper-130

echo "========== PHASE 4: Ubuntu aio 138 full cluster regression (G2-G6, Tier2/3) =========="
scp138 "$ROOT/tests/helpers/regression-full.sh" "$HOST138:/tmp/regression-full.sh"
ssh138 "sudo bash /tmp/regression-full.sh"
pass G2-G6-138

echo "========== REGRESSION COMPLETE =========="
echo "Log: $LOG"
