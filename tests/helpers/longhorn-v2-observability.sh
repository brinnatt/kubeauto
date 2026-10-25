#!/usr/bin/env bash
# LONGHORN-53 owns one source pool; retained monitoring is an explicit focus mode.
set -Eeuo pipefail
BASE=/usr/local/kubeauto
cd "$BASE"
PY="$BASE/.venv/bin/python"
CORE="$BASE/tests/helpers/longhorn-v2-core.py"
INSTALL="$BASE/tests/helpers/longhorn-v2-install.py"
MONITOR="$BASE/tests/helpers/longhorn-v2-monitoring.py"
DRIVER="$BASE/tests/helpers/longhorn-v2-observability.py"
CPU="$BASE/tests/helpers/longhorn-v2-cpu-isolation.py"
export PYTHONPATH="$BASE" PATH="$BASE/kube-bin:$BASE/extra-bin:$PATH"
export KUBECONFIG="$BASE/clusters/longhorn-v2-gate/kubectl.kubeconfig"
[[ "${LONGHORN_PARENT_LEASE_HELD:-no}" == yes ]] || exit 1
mode="${1:-v2-observability}"
case "$mode" in v2-observability|v2-observability-monitoring|v2-observability-business|v2-observability-cleanup) ;; *) exit 2;; esac
reconcile_args=()
if [[ -n "${LONGHORN_MONITORING_CACHE_RECONCILE_PAYLOAD:-}" || -n "${LONGHORN_MONITORING_CACHE_RECONCILE_SHA256:-}" ]]; then
  [[ "$mode" == v2-observability-monitoring && -n "${LONGHORN_MONITORING_CACHE_RECONCILE_PAYLOAD:-}" &&
     "${LONGHORN_MONITORING_CACHE_RECONCILE_SHA256:-}" =~ ^[0-9a-f]{64}$ ]] || exit 2
  export LONGHORN_MONITORING_CACHE_RECONCILE_FOCUS="$mode"
  reconcile_args=(--reconcile-cache)
fi
"$PY" "$CORE" verify-transport
"$PY" "$CORE" verify-core
work="$(mktemp -d /var/tmp/kubeauto-longhorn-v2-observability.XXXXXX)"
chmod 0700 "$work"
echo "LONGHORN_V2_OBSERVABILITY_DURABLE_DIRECTORY path=$work"
relay_pid=
cpu_active=no
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
    if [[ "$cpu_active" == yes ]]; then
      "$PY" "$CPU" clean --relay-pid "$relay_pid" || cleanup_rc=$?
    fi
    if [[ "$cleanup_rc" == 0 && "$business_active" == yes ]]; then
      "$PY" "$DRIVER" cleanup --relay-pid "$relay_pid" || cleanup_rc=$?
    fi
    # A failed business must restore its network/scrape faults before deleting
    # the monitoring volumes. Refuse the dependent cleanup after a failure.
    if [[ "$cleanup_rc" == 0 && "$mode" != v2-observability-business && "$monitor_active" == yes ]]; then
      "$PY" "$MONITOR" cleanup --relay-pid "$relay_pid" || cleanup_rc=$?
      if [[ "$cleanup_rc" == 0 ]]; then "$PY" "$INSTALL" volume-baseline || cleanup_rc=$?; fi
    fi
    if [[ "$rc" == 0 ]] && ! kill -0 "$relay_pid" 2>/dev/null; then rc=1; fi
    kill -TERM "$relay_pid" 2>/dev/null || true
    wait "$relay_pid" || { [[ "$rc" != 0 ]] || rc=1; }
  fi
  "$PY" "$INSTALL" relay-clean || cleanup_rc=$?
  "$PY" "$CORE" verify-core || cleanup_rc=$?
  if [[ "$cleanup_rc" != 0 ]]; then
    echo "LONGHORN_CLEANUP_FAIL phase=$phase rc=$cleanup_rc journal=retained" >&2
    [[ "$rc" != 0 ]] || rc=$cleanup_rc
  fi
  printf '%s\n' "$rc" > "$work/exit"
  printf '%s\n' "$cleanup_rc" > "$work/finalized"
  if [[ "$rc" == 0 ]]; then
    if [[ "$mode" == v2-observability-business ]]; then
      echo LONGHORN_V2_OBSERVABILITY_BUSINESS_BASELINE_RETAINED
    else
      echo LONGHORN_V2_OBSERVABILITY_PERFORMANCE_SCOPED_CLEAN_PASS
      if [[ "$mode" == v2-observability ]]; then echo LONGHORN_V2_OBSERVABILITY_PERFORMANCE_PASS; fi
      if [[ "$mode" == v2-observability-monitoring ]]; then
        echo LONGHORN_V2_OBSERVABILITY_MONITORING_SCOPED_CLEAN_PASS
        echo LONGHORN_V2_OBSERVABILITY_MONITORING_PASS
      fi
    fi
  fi
  exit "$rc"
}
trap finish EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
"$PY" "$INSTALL" relay-ensure
"$PY" "$INSTALL" relay-maintain &
relay_pid=$!
stage() {
  local name="$1" marker="$2"
  shift 2
  phase="$name"
  bash "$BASE/tests/helpers/run-durable-gate.sh" "$work/$name" LONGHORN_V2_OBSERVABILITY_STAGE_EXIT \
    "$@" 2>&1 | tee "$work/$name.log"
  [[ -f "$work/$name.exit" && ! -L "$work/$name.exit" && "$(cat "$work/$name.exit")" == 0 ]]
  [[ -f "$work/$name.finalized" && ! -L "$work/$name.finalized" && "$(cat "$work/$name.finalized")" == 0 ]]
  [[ "$(grep -Fxc 'LONGHORN_V2_OBSERVABILITY_STAGE_EXIT rc=0' "$work/$name.log")" == 1 ]]
  [[ "$(grep -Fxc "$marker" "$work/$name.log")" == 1 ]]
  if grep -Eq '^(LONGHORN_[A-Z0-9_]*(FAIL|FIRST_FAILURE)( |$)|FAILED|ERROR[:!]|FAIL:)' "$work/$name.log"; then exit 1; fi
}
if [[ "$mode" == v2-observability-cleanup ]]; then
  business_active=yes
  monitor_active=yes
  phase=owned-observability-recovery
  exit 0
fi
if [[ "$mode" == v2-observability-monitoring ]]; then
  # Recover only the original monitoring owner before admitting a fresh setup.
  # A dangling symlink must reach the driver's receipt refusal, never be skipped.
  if [[ -e "$BASE/clusters/longhorn-v2-gate/longhorn-v2-monitoring.json" || -L "$BASE/clusters/longhorn-v2-gate/longhorn-v2-monitoring.json" ]]; then
    stage prior-monitoring-cleanup LONGHORN_V2_MONITORING_SCOPED_CLEAN_PASS "$PY" "$MONITOR" cleanup --relay-pid "$relay_pid"
  fi
  # The strict CPU verifier requires its exact original input and no monitoring
  # receipt; only the original owner's completed cleanup restores that boundary.
  stage cpu-evidence-reuse LONGHORN_V2_CPU_ISOLATION_REUSE_PASS "$PY" "$CPU" verify-completed --relay-pid "$relay_pid"
fi
if [[ "$mode" == v2-observability || "$mode" == v2-observability-monitoring ]]; then
  "$PY" "$INSTALL" volume-baseline
fi
if [[ "$mode" == v2-observability ]]; then
  cpu_active=yes
  stage cpu-isolation LONGHORN_V2_CPU_ISOLATION_PASS "$PY" "$CPU" business --relay-pid "$relay_pid"
  [[ "$(grep -Fxc 'LONGHORN_V2_CPU_ISOLATION_SCOPED_CLEAN_PASS' "$work/cpu-isolation.log")" == 1 ]]
  cpu_active=no
fi
if [[ "$mode" == v2-observability || "$mode" == v2-observability-monitoring ]]; then
  monitor_active=yes
  stage monitoring-product-setup LONGHORN_V2_MONITORING_SETUP_PASS "$PY" "$MONITOR" setup --relay-pid "$relay_pid" "${reconcile_args[@]}"
fi
phase=monitoring-business-health
"$PY" "$MONITOR" verify --relay-pid "$relay_pid"
business_active=yes
stage sustained-business LONGHORN_V2_OBSERVABILITY_BUSINESS_PASS "$PY" "$DRIVER" business --relay-pid "$relay_pid"
"$PY" "$DRIVER" verify --log "$work/sustained-business.log"
