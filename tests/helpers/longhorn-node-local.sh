#!/usr/bin/env bash
# LONGHORN-25: isolated same-subnet V1 setup, expansion and complete retirement.
set -Eeuo pipefail
BASE=/usr/local/kubeauto
cd "$BASE"
PY="$BASE/.venv/bin/python"
DRIVER="$BASE/tests/helpers/longhorn-node-local.py"
DIR="$BASE/clusters/longhorn-node-gate"
JOURNAL="$DIR/longhorn-node-lifecycle.json"
NODES=(192.168.47.146 192.168.47.147 192.168.47.148 192.168.47.149)
export PYTHONPATH="$BASE" PATH="$BASE/kube-bin:$BASE/extra-bin:$PATH"
export KUBECONFIG="$DIR/kubectl.kubeconfig"
[[ "${LONGHORN_PARENT_LEASE_HELD:-no}" == yes ]] || exit 2
mode="${1:-}"
case "$mode" in node-lifecycle|node-lifecycle-cleanup) ;; *) exit 2;; esac
resume=no
if [[ "$mode" == node-lifecycle ]]; then
  source_proof="${LONGHORN_V2_RELEASE_PROOF:-}"
  target_proof="${LONGHORN_V2_TARGET_RELEASE_PROOF:-}"
  for proof in "$source_proof" "$target_proof"; do
    [[ "$proof" == /* && -f "$proof" && ! -L "$proof" ]] || exit 2
  done
  if [[ -e "$DIR" ]]; then
    [[ ! -L "$DIR" && -f "$DIR/longhorn-lab-owner.json" && ! -L "$DIR/longhorn-lab-owner.json" &&
       -f "$JOURNAL" && ! -L "$JOURNAL" ]] || exit 1
    resume=yes
  else
    [[ ! -L "$DIR" ]] || exit 1
    for cluster in longhorn-v2-gate longhorn-v2-dr-target; do
      [[ ! -e "$BASE/clusters/$cluster" && ! -L "$BASE/clusters/$cluster" ]] || exit 1
    done
    bash tests/helpers/lab-wipe-nodes.sh --verify --rocky-only "${NODES[@]}"
  fi
else
  [[ -f "$DIR/longhorn-lab-owner.json" && ! -L "$DIR" && ! -L "$DIR/longhorn-lab-owner.json" ]] || exit 1
fi
work="$(mktemp -d /var/tmp/kubeauto-longhorn-node-local.XXXXXX)"
chmod 0700 "$work"
echo "LONGHORN_NODE_LOCAL_DURABLE_WORK path=$work"
control_config=/root/.kube/config
[[ ! -L "$control_config" ]] || exit 1
[[ ! -f "$control_config" ]] || cp -a "$control_config" "$work/control-kubeconfig"
phase=configure
owned=no
relay_pid=
start_relay() {
  "$PY" "$DRIVER" relay-ensure
  "$PY" "$DRIVER" relay-maintain >> "$work/relay.log" 2>&1 &
  relay_pid=$!
}
stop_relay() {
  local rc=0
  if [[ -n "$relay_pid" ]]; then
    kill -0 "$relay_pid" 2>/dev/null || rc=1
    kill -TERM "$relay_pid" 2>/dev/null || true
    wait "$relay_pid" || rc=$?
    relay_pid=
  fi
  "$PY" "$DRIVER" relay-clean || rc=$?
  return "$rc"
}
finish() {
  local rc=$? cleanup_rc=0
  trap - EXIT INT TERM
  if [[ "$rc" != 0 ]]; then
    printf 'rc=%s phase=%s\n' "$rc" "$phase" > "$work/first-failure.status"
    echo "LONGHORN_FIRST_FAILURE phase=$phase rc=$rc diagnostics=$work" >&2
    if [[ -f "$KUBECONFIG" ]]; then
      kubectl --request-timeout=5s get pods -A -o wide > "$work/pods.txt" 2>&1 || true
      kubectl --request-timeout=5s get events -A --sort-by=.lastTimestamp > "$work/events.txt" 2>&1 || true
      kubectl --request-timeout=5s -n longhorn-system describe pods > "$work/descriptions.txt" 2>&1 || true
    fi
  fi
  if [[ "$owned" == yes ]]; then
    if [[ -f "$JOURNAL" ]]; then
      [[ -n "$relay_pid" ]] || start_relay || cleanup_rc=$?
      if [[ "$cleanup_rc" == 0 ]]; then
        "$PY" "$DRIVER" recover-partial-join || cleanup_rc=$?
        if [[ "$cleanup_rc" == 0 ]]; then
          "$PY" "$DRIVER" clean --relay-pid "$relay_pid" | tee "$work/receiving-cleanup.log" || cleanup_rc=$?
          if [[ "$cleanup_rc" == 0 ]]; then
            grep -Fxq LONGHORN_NODE_LIFECYCLE_SCOPED_CLEAN_PASS "$work/receiving-cleanup.log" || cleanup_rc=1
          fi
        fi
      fi
      stop_relay || cleanup_rc=$?
      if [[ "$cleanup_rc" == 0 ]]; then "$PY" "$DRIVER" finalize || cleanup_rc=$?; fi
    fi
    if [[ "$cleanup_rc" == 0 ]]; then
      [[ -n "$relay_pid" ]] || start_relay || cleanup_rc=$?
      if [[ "$cleanup_rc" == 0 ]]; then
        "$PY" "$DRIVER" retire-storage --relay-pid "$relay_pid" | tee "$work/storage-retirement.log" || cleanup_rc=$?
      fi
    fi
    stop_relay || cleanup_rc=$?
    if [[ "$cleanup_rc" == 0 ]]; then
      "$PY" "$DRIVER" retire-core | tee "$work/core-retirement.log" || cleanup_rc=$?
    fi
    if [[ "$cleanup_rc" == 0 ]]; then
      bash tests/helpers/lab-wipe-nodes.sh --verify --rocky-only "${NODES[@]}" | tee "$work/clean-verify.log" || cleanup_rc=$?
    fi
  fi
  if [[ -f "$work/control-kubeconfig" ]]; then
    cp -a "$work/control-kubeconfig" "$control_config" || cleanup_rc=$?
  else
    rm -f -- "$control_config" || cleanup_rc=$?
  fi
  if [[ "$cleanup_rc" != 0 ]]; then
    echo "LONGHORN_CLEANUP_FAIL phase=$phase rc=$cleanup_rc journal=retained diagnostics=$work" >&2
    [[ "$rc" != 0 ]] || rc=$cleanup_rc
  fi
  if [[ "$rc" == 0 ]]; then
    if [[ "$mode" == node-lifecycle-cleanup ]]; then
      echo LONGHORN_NODE_LIFECYCLE_CLEANUP_SCOPED_CLEAN_PASS
      echo LONGHORN_NODE_LIFECYCLE_CLEANUP_PASS
    else
      echo LONGHORN_NODE_LIFECYCLE_GATE_SCOPED_CLEAN_PASS
      echo LONGHORN_NODE_LIFECYCLE_GATE_PASS
    fi
  fi
  printf 'rc=%s phase=%s cleanup_rc=%s\n' "$rc" "$phase" "$cleanup_rc" > "$work/status.tmp"
  mv "$work/status.tmp" "$work/status"
  exit "$rc"
}
trap finish EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
if [[ "$mode" == node-lifecycle-cleanup ]]; then owned=yes; phase=owned-same-subnet-recovery; exit 0; fi
if [[ "$resume" == yes ]]; then
  owned=yes
  phase=verified-partial-add-resume
  start_relay
  "$PY" "$DRIVER" resume-business --relay-pid "$relay_pid" --released-v2-proof "$target_proof" | tee "$work/business.log"
else
"$PY" "$BASE/kubecli.py" new longhorn-node-gate </dev/null
"$PY" "$DRIVER" configure --source-directory "$(dirname "$source_proof")" --target-proof "$target_proof"
owned=yes
start_relay
phase=product-core-setup
"$PY" "$DRIVER" core-setup --relay-pid "$relay_pid" | tee "$work/core-setup.log"
phase=storage-preparation
"$PY" "$DRIVER" prepare-storage | tee "$work/storage-preparation.log"
phase=product-storage-setup
"$PY" "$DRIVER" storage-setup --relay-pid "$relay_pid" | tee "$work/storage-setup.log"
phase=receiving-host-admission
"$PY" "$DRIVER" admit --released-v2-proof "$target_proof" | tee "$work/admission.log"
phase=add-node-expansion-evacuation-retirement
"$PY" "$DRIVER" business --relay-pid "$relay_pid" | tee "$work/business.log"
fi
if ! grep -q '^LONGHORN_NODE_COMPLETED_ADD_PREFIX_VERIFIED log_sha256=' "$work/business.log"; then
  grep -q '^LONGHORN_NODE_DATA_EXPANSION_VERIFIED replicas=4 ' "$work/business.log"
  grep -Fxq 'LONGHORN_NODE_PRODUCT_REMOVAL_REFUSAL step=del-node' "$work/business.log"
  grep -Fxq 'LONGHORN_NODE_PRODUCT_REMOVAL_REFUSAL step=del-master' "$work/business.log"
fi
grep -q '^LONGHORN_NODE_NATIVE_EVACUATION_RETIRED ' "$work/business.log"
grep -Fxq LONGHORN_NODE_LIFECYCLE_SCOPED_CLEAN_PASS "$work/business.log"
grep -Fxq LONGHORN_NODE_LIFECYCLE_PASS "$work/business.log"
