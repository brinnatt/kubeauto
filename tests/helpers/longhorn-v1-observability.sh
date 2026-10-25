#!/usr/bin/env bash
# LONGHORN-34 reuses product monitoring and short bounded V1 business actions.
set -Eeuo pipefail
BASE=/usr/local/kubeauto
cd "$BASE"
PY="$BASE/.venv/bin/python"
LAB="$BASE/tests/helpers/longhorn-lab.py"
DRIVER="$BASE/tests/helpers/longhorn-v1-observability.py"
export PYTHONPATH="$BASE" PATH="$BASE/kube-bin:$BASE/extra-bin:$PATH"
export KUBECONFIG="$BASE/clusters/longhorn-gate/kubectl.kubeconfig"
[[ "${LONGHORN_PARENT_LEASE_HELD:-no}" == yes ]] || exit 1
mode="${1:-observability}"
case "$mode" in observability|observability-business|observability-support|observability-image-cache|observability-cleanup) ;; *) exit 2;; esac
"$PY" "$LAB" verify-transport
"$PY" "$LAB" verify-core
work="$(mktemp -d /var/tmp/kubeauto-longhorn-v1-observability.XXXXXX)"
chmod 0700 "$work"
echo "LONGHORN_V1_OBSERVABILITY_DURABLE_DIRECTORY path=$work"
relay_pid=
business_active=no
monitor_active=no
phase=relay
finish() {
  local rc=$? cleanup_rc=0
  trap - EXIT INT TERM
  if [[ "$rc" != 0 ]]; then
    printf 'rc=%s phase=%s\n' "$rc" "$phase" > "$work/first-failure.status"
    echo "LONGHORN_FIRST_FAILURE phase=$phase rc=$rc diagnostics=$work" >&2
    kubectl --request-timeout=5s get pods -A -o wide > "$work/pods.txt" 2>&1 || true
    kubectl --request-timeout=5s get events -A --sort-by=.lastTimestamp > "$work/events.txt" 2>&1 || true
  fi
  if [[ -n "$relay_pid" ]]; then
    if [[ "$business_active" == yes ]]; then "$PY" "$DRIVER" cleanup --relay-pid "$relay_pid" || cleanup_rc=$?; fi
    # Fault and business cleanup is a prerequisite for shared monitoring PVCs.
    if [[ "$cleanup_rc" == 0 && "$monitor_active" == yes && "$mode" != observability-business ]]; then
      "$PY" "$DRIVER" monitor-cleanup --relay-pid "$relay_pid" || cleanup_rc=$?
      if [[ "$cleanup_rc" == 0 ]]; then "$PY" "$DRIVER" baseline --relay-pid "$relay_pid" || cleanup_rc=$?; fi
    fi
    if [[ "$cleanup_rc" == 0 && "$mode" == observability-support ]]; then "$PY" "$DRIVER" baseline --relay-pid "$relay_pid" || cleanup_rc=$?; fi
    if [[ "$rc" == 0 ]] && ! kill -0 "$relay_pid" 2>/dev/null; then rc=1; fi
    kill -TERM "$relay_pid" 2>/dev/null || true
    wait "$relay_pid" || { [[ "$rc" != 0 ]] || rc=1; }
  fi
  if [[ "$mode" != observability-image-cache ]]; then "$PY" "$LAB" relay-clean || cleanup_rc=$?; fi
  "$PY" "$LAB" verify-core || cleanup_rc=$?
  if [[ "$cleanup_rc" != 0 ]]; then
    echo "LONGHORN_CLEANUP_FAIL phase=$phase rc=$cleanup_rc journal=retained" >&2
    [[ "$rc" != 0 ]] || rc=$cleanup_rc
  fi
  printf '%s\n' "$rc" > "$work/exit"
  printf '%s\n' "$cleanup_rc" > "$work/finalized"
  if [[ "$rc" == 0 ]]; then
    if [[ "$mode" == observability-image-cache ]]; then
      echo LONGHORN_OBSERVABILITY_IMAGE_CACHE_SCOPED_CLEAN_PASS
      echo LONGHORN_OBSERVABILITY_IMAGE_CACHE_PASS
    elif [[ "$mode" == observability-support ]]; then
      echo LONGHORN_OBSERVABILITY_SUPPORT_SCOPED_CLEAN_PASS
      echo LONGHORN_OBSERVABILITY_SUPPORT_PASS
    elif [[ "$mode" == observability-business ]]; then
      echo LONGHORN_OBSERVABILITY_BUSINESS_BASELINE_RETAINED
    else
      echo LONGHORN_OBSERVABILITY_SCOPED_CLEAN_PASS
      if [[ "$mode" == observability ]]; then echo LONGHORN_OBSERVABILITY_PASS; fi
      if [[ "$mode" == observability-cleanup ]]; then echo LONGHORN_OBSERVABILITY_CLEANUP_PASS; fi
    fi
  fi
  exit "$rc"
}
trap finish EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
if [[ "$mode" == observability-image-cache ]]; then
  phase=verified-artifact-node-cache
  "$PY" "$DRIVER" image-cache | tee "$work/image-cache.log"
  grep -q '^LONGHORN_V1_MONITORING_IMAGE_CACHE_PASS nodes=3' "$work/image-cache.log"
  exit 0
fi
"$PY" "$LAB" relay-ensure
"$PY" "$LAB" relay-maintain &
relay_pid=$!
stage() {
  local name="$1" marker="$2"
  shift 2
  phase="$name"
  bash "$BASE/tests/helpers/run-durable-gate.sh" "$work/$name" LONGHORN_V1_OBSERVABILITY_STAGE_EXIT \
    "$@" 2>&1 | tee "$work/$name.log"
  [[ -f "$work/$name.exit" && ! -L "$work/$name.exit" && "$(cat "$work/$name.exit")" == 0 ]]
  [[ -f "$work/$name.finalized" && ! -L "$work/$name.finalized" && "$(cat "$work/$name.finalized")" == 0 ]]
  [[ "$(grep -Fxc 'LONGHORN_V1_OBSERVABILITY_STAGE_EXIT rc=0' "$work/$name.log")" == 1 ]]
  [[ "$(grep -Fxc "$marker" "$work/$name.log")" == 1 ]]
  if grep -Eq '^(LONGHORN_[A-Z0-9_]*(FAIL|FIRST_FAILURE)( |$)|FAILED|ERROR[:!]|FAIL:)' "$work/$name.log"; then exit 1; fi
}
if [[ "$mode" == observability-cleanup ]]; then
  business_active=yes
  monitor_active=yes
  phase=owned-observability-recovery
  exit 0
fi
if [[ "$mode" == observability ]]; then
  "$PY" "$DRIVER" baseline --relay-pid "$relay_pid"
  monitor_active=yes
  stage monitoring-product-setup LONGHORN_V1_MONITORING_SETUP_PASS "$PY" "$DRIVER" setup --relay-pid "$relay_pid"
fi
if [[ "$mode" == observability-support ]]; then
  "$PY" "$DRIVER" baseline --relay-pid "$relay_pid"
  business_active=yes
  stage support-business LONGHORN_V1_SUPPORT_BUSINESS_PASS "$PY" "$DRIVER" support-business --relay-pid "$relay_pid"
  "$PY" "$DRIVER" verify-support --log "$work/support-business.log"
  exit 0
fi
phase=monitoring-health
"$PY" "$DRIVER" monitor-verify --relay-pid "$relay_pid"
business_active=yes
stage bounded-business LONGHORN_V1_OBSERVABILITY_BUSINESS_PASS "$PY" "$DRIVER" business --relay-pid "$relay_pid"
"$PY" "$DRIVER" verify --log "$work/bounded-business.log"
