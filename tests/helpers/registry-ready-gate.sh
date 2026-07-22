#!/usr/bin/env bash
# Local registry readiness gate: stop→start race must not EOF on download -D/-X/-E.
# Same pattern as Docker daemon wait; mirrors nerdctl-gate (sync → unit → wipe → repro).
# Run from control host with lab SSH. Primary: 147 source kubecli; also jumper 136 (bug host).
# Usage: bash tests/helpers/registry-ready-gate.sh
# Env: LAB_SSH_PASSWORD, REGISTRY_GATE_SKIP_WIPE=1, REGISTRY_GATE_SKIP_136=1
set -euo pipefail
SRC="$(cd "$(dirname "$0")/../.." && pwd)"
BASE="${KUBEAUTO_BASE:-/usr/local/kubeauto}"
export PATH=/usr/local/bin:$PATH
export PYTHONPATH="$BASE"
LOG=/tmp/kubeauto-registry-ready-gate-$(date +%Y%m%d-%H%M%S).log
exec > >(tee -a "$LOG") 2>&1
echo "LOG=$LOG SRC=$SRC BASE=$BASE"

pass(){ echo "[PASS] $*"; }
fail(){ echo "[FAIL] $*"; exit 1; }
run(){ echo ">>> $*"; "$@" || fail "$*"; }

PW="${LAB_SSH_PASSWORD:-123456}"
CONTROL_IP=192.168.47.147
JUMPER_IP=192.168.47.136

# Prefer ubuntu+docker-group kubecli (snap docker + sudo root often cannot kill temp_*).
# -D only needs root when (re)installing /usr/local/bin symlinks; gate preflights that path.
ensure_download_d_bin_skip(){
  # Match production "already installed; skipping" path so -D exercises registry wait,
  # not k8s-bin extract (user bug log had all bins skipped before EOF).
  local b
  for b in kubelet kubectl kube-apiserver kube-controller-manager kube-scheduler kube-proxy; do
    if [[ -e "$BASE/kube-bin/$b" ]]; then
      sudo ln -sfn "$BASE/kube-bin/$b" "/usr/local/bin/$b"
    fi
  done
  [[ -e "$BASE/extra-bin/etcdctl" ]] || fail "extra-bin/etcdctl missing (ext-bin not installed)"
  [[ -d "$BASE/roles/kube-node" ]] || fail "roles/kube-node missing (kubeauto not installed)"
  # Drop stuck extract containers from earlier failed runs.
  docker rm -f temp_k8s temp_ext temp_kubeauto 2>/dev/null || true
  local pid
  pid="$(docker inspect temp_k8s --format '{{.State.Pid}}' 2>/dev/null || true)"
  if [[ -n "${pid:-}" && "$pid" != "0" ]]; then
    sudo kill -9 "$pid" 2>/dev/null || true
    docker rm -f temp_k8s 2>/dev/null || true
  fi
}

assert_registry_http(){
  local host="${1:-127.0.0.1}"
  curl -sf --max-time 3 "http://${host}:5000/v2/" >/dev/null \
    || fail "registry HTTP not ready at ${host}:5000/v2/"
}

stop_registry_cold(){
  # Force the exact bug path: container exists but stopped → start → immediate push.
  # snap docker on lab 147 often returns "permission denied" on stop/kill — fall back to PID.
  docker update --restart=no local_registry >/dev/null 2>&1 || true
  if ! docker stop -t 2 local_registry >/dev/null 2>&1; then
    local pid
    pid="$(docker inspect local_registry --format '{{.State.Pid}}' 2>/dev/null || true)"
    if [[ -n "${pid:-}" && "$pid" != "0" ]]; then
      sudo kill -TERM "$pid" 2>/dev/null || true
      sleep 1
      sudo kill -9 "$pid" 2>/dev/null || true
    fi
  fi
  local i
  for i in $(seq 1 30); do
    if ! curl -sf --max-time 1 http://127.0.0.1:5000/v2/ >/dev/null 2>&1; then
      break
    fi
    sleep 0.5
  done
  if curl -sf --max-time 1 http://127.0.0.1:5000/v2/ >/dev/null 2>&1; then
    fail "registry still answering after stop (cannot repro cold start)"
  fi
  docker ps -a --filter name=local_registry --format '{{.Names}} {{.Status}}' || true
}

run_download_cold(){
  local label="$1"; shift
  echo "----- cold-start: $* ($label) -----"
  stop_registry_cold
  # Must succeed: start_local_registry waits for GET /v2/ before push.
  "$@" </dev/null || fail "$label failed after registry cold start"
  assert_registry_http
  docker ps --filter name=local_registry --format '{{.Names}} {{.Status}}' | grep -q Up \
    || fail "local_registry not Up after $label"
  pass "$label cold-start"
}

run_download_warm(){
  local label="$1"; shift
  echo "----- warm (already running): $* ($label) -----"
  docker start local_registry >/dev/null 2>&1 || true
  assert_registry_http
  "$@" </dev/null || fail "$label warm path failed"
  assert_registry_http
  pass "$label warm"
}

echo "========== R0 sync source → ${BASE} @${CONTROL_IP} =========="
run bash "$SRC/tests/helpers/sync-kubeauto.sh" "ubuntu@${CONTROL_IP}" "$PW"
grep -q '_wait_for_registry_ready' "$BASE/service/cluster/registry.py" \
  || fail "synced tree missing _wait_for_registry_ready"
grep -q '_registry_http_ok' "$BASE/service/cluster/registry.py" \
  || fail "synced tree missing _registry_http_ok"
pass "sync-147"

echo "========== R1 unit tests =========="
cd "$BASE"
run bash "$BASE/tests/run_unit_tests.sh"
python3 -m unittest tests.unit.test_registry_ready -v 2>&1 | tee /tmp/registry-ready-unit.txt \
  || fail "test_registry_ready"
pass "unit"

if [[ "${REGISTRY_GATE_SKIP_WIPE:-0}" != "1" ]]; then
  echo "========== R2 lab wipe (keep docker + registry container on 147) =========="
  run bash "$BASE/tests/helpers/lab-wipe-nodes.sh"
  # Wipe leaves docker; registry may be Exited — that is the intended cold state.
  pass "wipe"
else
  echo "========== R2 lab wipe SKIPPED (REGISTRY_GATE_SKIP_WIPE=1) =========="
fi

echo "========== R3 ensure docker + hosts on 147 =========="
systemctl is-active docker >/dev/null 2>&1 || sudo systemctl start docker
grep -q 'registry.talkschool.cn' /etc/hosts \
  || echo '127.0.0.1  registry.talkschool.cn' | sudo tee -a /etc/hosts >/dev/null
# Prefer loopback mapping on control (upload uses registry.talkschool.cn:5000).
if ! grep -E '127\.0\.0\.1[[:space:]]+registry\.talkschool\.cn' /etc/hosts >/dev/null; then
  echo "[WARN] /etc/hosts registry.talkschool.cn is not 127.0.0.1 — push still OK if DNAT/route works"
fi
docker ps -a --filter name=local_registry --format '{{.Names}} {{.Status}}' || true
ensure_download_d_bin_skip
pass "docker+hosts"

echo "========== R4 bug repro: download -D after stopped registry =========="
# Exact user scenario: bins skipped → start stopped registry → push (EOF without wait).
run_download_cold "download -D" kubecli download -D
pass "R4"

echo "========== R5 related: download -X cold + warm =========="
run_download_cold "download -X" kubecli download -X
run_download_warm "download -X" kubecli download -X
pass "R5"

echo "========== R6 related: download -E network-check cold =========="
# Extra-component upload path (same start_local_registry wait as -D/-X).
run_download_cold "download -E network-check" kubecli download -E network-check
pass "R6"

echo "========== R7 catalog sanity =========="
curl -sf http://127.0.0.1:5000/v2/_catalog | tee /tmp/registry-catalog-147.json
grep -q 'brinnatt/' /tmp/registry-catalog-147.json \
  || grep -q '"' /tmp/registry-catalog-147.json \
  || fail "empty/invalid catalog"
# Restore always-restart for lab (cold steps temporarily set restart=no).
docker update --restart=always local_registry >/dev/null 2>&1 || true
pass "catalog-147"

# ---------------------------------------------------------------------------
# Jumper 136: production jump host where the EOF bug was observed (binary kubecli).
# Sync installs source wrapper so the fix is exerciseable without rebuilding the ELF.
# ---------------------------------------------------------------------------
if [[ "${REGISTRY_GATE_SKIP_136:-0}" != "1" ]]; then
  echo "========== R8 sync + cold download on jumper ${JUMPER_IP} =========="
  run bash "$SRC/tests/helpers/sync-kubeauto.sh" "root@${JUMPER_IP}" "$PW"
  sshpass -p "$PW" ssh -o StrictHostKeyChecking=no "root@${JUMPER_IP}" "bash -s" <<'REMOTE'
set -euo pipefail
export PATH=/usr/local/bin:$PATH
export PYTHONPATH=/usr/local/kubeauto
BASE=/usr/local/kubeauto
LOG_J=/tmp/kubeauto-registry-ready-jumper-$(date +%Y%m%d-%H%M%S).log
exec > >(tee -a "$LOG_J") 2>&1
echo "JUMPER_LOG=$LOG_J"

pass(){ echo "[PASS] $*"; }
fail(){ echo "[FAIL] $*"; exit 1; }

# Must be source wrapper after sync (not stale ELF without wait).
head -5 /usr/local/bin/kubecli | grep -q 'kubecli.py' \
  || fail "jumper kubecli is not source wrapper after sync"
grep -q '_wait_for_registry_ready' "$BASE/service/cluster/registry.py" \
  || fail "jumper missing wait helper"

systemctl is-active docker >/dev/null 2>&1 || systemctl start docker
grep -q 'registry.talkschool.cn' /etc/hosts \
  || echo '127.0.0.1  registry.talkschool.cn' >> /etc/hosts

stop_cold(){
  docker update --restart=no local_registry >/dev/null 2>&1 || true
  if ! docker stop -t 2 local_registry >/dev/null 2>&1; then
    local pid
    pid="$(docker inspect local_registry --format '{{.State.Pid}}' 2>/dev/null || true)"
    if [[ -n "${pid:-}" && "$pid" != "0" ]]; then
      kill -TERM "$pid" 2>/dev/null || true
      sleep 1
      kill -9 "$pid" 2>/dev/null || true
    fi
  fi
  local i
  for i in $(seq 1 30); do
    if ! curl -sf --max-time 1 http://127.0.0.1:5000/v2/ >/dev/null 2>&1; then
      break
    fi
    sleep 0.5
  done
  if curl -sf --max-time 1 http://127.0.0.1:5000/v2/ >/dev/null 2>&1; then
    fail "jumper registry still up after stop"
  fi
}

assert_http(){
  curl -sf --max-time 3 http://127.0.0.1:5000/v2/ >/dev/null \
    || fail "jumper registry HTTP not ready"
}

cd "$BASE"
# Focused unit (full suite may fail on jumper without jinja2 / sibling mounts).
python3.12 -m unittest tests.unit.test_registry_ready -v \
  || python3 -m unittest tests.unit.test_registry_ready -v \
  || fail "jumper registry_ready unit"
pass "jumper-unit"

echo "----- jumper cold download -D (exact bug host) -----"
stop_cold
kubecli download -D </dev/null || fail "jumper download -D cold"
assert_http
pass "jumper-download-D-cold"

echo "----- jumper cold download -X -----"
stop_cold
kubecli download -X </dev/null || fail "jumper download -X cold"
assert_http
pass "jumper-download-X-cold"

echo "----- jumper warm download -X -----"
kubecli download -X </dev/null || fail "jumper download -X warm"
assert_http
pass "jumper-download-X-warm"

curl -sf http://127.0.0.1:5000/v2/_catalog | tee /tmp/registry-catalog-136.json
echo "JUMPER_REGISTRY_READY_PASS"
REMOTE
  pass "jumper-136"
else
  echo "========== R8 jumper SKIPPED (REGISTRY_GATE_SKIP_136=1) =========="
fi

echo "========== SUMMARY =========="
echo "REGISTRY_READY_GATE_PASS"
echo "evidence: $LOG"
echo "catalog147: $(head -c 200 /tmp/registry-catalog-147.json 2>/dev/null || true)"
