#!/bin/bash
# Enterprise regression driver — executes tests/enterprise-test-matrix.yaml coverage.
# Run from dev host with network to lab: bash tests/run_enterprise_regression.sh
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PASS="${KUBEAUTO_SSH_PASS:-123456}"
HOST147="ubuntu@192.168.47.147"
HOST136="root@192.168.47.136"
LOG="${ROOT}/logs/enterprise-regression-$(date +%Y%m%d-%H%M).log"
mkdir -p "${ROOT}/logs"
exec > >(tee -a "$LOG") 2>&1

pass() { echo "[PASS] $*"; }
fail() { echo "[FAIL] $*"; exit 1; }
run()  { echo ">>> $*"; "$@"; }

ssh147() { sshpass -p "$PASS" ssh -o StrictHostKeyChecking=no "$HOST147" "$@"; }
ssh136() { sshpass -p "$PASS" ssh -o StrictHostKeyChecking=no "$HOST136" "$@"; }
scp147() { sshpass -p "$PASS" scp -o StrictHostKeyChecking=no "$@"; }

echo "========== PHASE 0: local unit tests (G0) =========="
run bash "$ROOT/tests/run_unit_tests.sh"
pass G0-local-unit

echo "========== PHASE 1: sync source to 147 + 136 =========="
run bash "$ROOT/tests/helpers/sync-kubeauto.sh" "$HOST147" "$PASS"
run bash "$ROOT/tests/helpers/sync-kubeauto.sh" "$HOST136" "$PASS"
pass G0-deploy-sync

echo "========== PHASE 1b: bootstrap brinnatt mirrors + upload defaults =========="
# Until CI dual-pushes new images, retag upstream → brinnatt/* then upload to local registry.
ssh147 "sudo bash /usr/local/kubeauto/tests/helpers/bootstrap-brinnatt-mirrors.sh"
ssh147 "sudo bash -s" <<'REMOTE147_IMG'
set -euo pipefail
export PYTHONPATH=/usr/local/kubeauto
export PATH=/usr/local/bin:/usr/bin:$PATH
kubecli download -X </dev/null
# CNI / addon images used by Tier2 + setup 07
for c in flannel cilium kube-router kube-ovn prometheus ingress-nginx network-check local-path-provisioner; do
  kubecli download -E "$c" </dev/null || true
done
echo IMG_PREP_147_OK
REMOTE147_IMG
ssh136 "BOOTSTRAP_MODE=defaults bash /usr/local/kubeauto/tests/helpers/bootstrap-brinnatt-mirrors.sh"
ssh136 "bash -s" <<'REMOTE136_IMG'
set -euo pipefail
export PYTHONPATH=/usr/local/kubeauto
export PATH=/usr/local/bin:/usr/bin:$PATH
kubecli download -X </dev/null
echo IMG_PREP_136_OK
REMOTE136_IMG
pass G0-brinnatt-bootstrap

echo "========== PHASE 2: 147 preflight (G0/G1) =========="
ssh147 "sudo bash -s" <<'REMOTE147_PREFLIGHT'
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
REMOTE147_PREFLIGHT
pass G0-147-preflight

echo "========== PHASE 3: 136 jumper preflight + k8s-dev (G7) =========="
ssh136 "bash -s" <<'REMOTE136'
set -euo pipefail
BASE=/usr/local/kubeauto
export PYTHONPATH="$BASE"
export PATH="/usr/local/bin:/usr/bin:$PATH"
# Rocky jumper: prefer python3.12 (system python3 may still be 3.6)
PY="$(command -v python3.12 || command -v python3)"

# Deploy source wrapper if frozen binary only
if ! "$PY" -c "import common.ansible_python" 2>/dev/null; then
  echo "common module missing on 136 (py=$PY)"
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

kubecli system -a --user root --password 123456 192.168.47.130-131 192.168.47.140-141 </dev/null

# k8s-dev: policy preflight + setup (non-interactive)
cd "$BASE"
if [ -d clusters/k8s-dev ]; then
  kubecli setup k8s-dev 90 </dev/null
  echo K8S_DEV_SETUP_OK
else
  echo "k8s-dev cluster dir missing — skip setup"
fi
REMOTE136
pass G7-jumper

echo "========== PHASE 4: 147 full cluster regression (G2-G6, Tier2/3) =========="
scp147 "$ROOT/tests/helpers/regression-147-full.sh" "$HOST147:/tmp/regression-147-full.sh"
ssh147 "sudo bash /tmp/regression-147-full.sh"
pass G2-G6-147

echo "========== REGRESSION COMPLETE =========="
echo "Log: $LOG"
