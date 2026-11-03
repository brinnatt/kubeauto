#!/usr/bin/env bash
# LONGHORN-45. Source and target leases are both held by the parent runner.
set -Eeuo pipefail
BASE=/usr/local/kubeauto
cd "$BASE"
PY="$BASE/.venv/bin/python"
CORE="$BASE/tests/helpers/longhorn-v2-core.py"
DRIVER="$BASE/tests/helpers/longhorn-v2-disk-lifecycle.py"
JOURNAL="$BASE/clusters/longhorn-v2-gate/longhorn-v2-disk-lifecycle.json"
export PYTHONPATH="$BASE" PATH="$BASE/kube-bin:$BASE/extra-bin:$PATH"
export KUBECONFIG="$BASE/clusters/longhorn-v2-gate/kubectl.kubeconfig"
[[ "${LONGHORN_PARENT_LEASE_HELD:-no}" == yes ]] || exit 1
mode="${1:-v2-disk-lifecycle}"
case "$mode" in v2-disk-lifecycle|v2-disk-lifecycle-cleanup) ;; *) exit 2;; esac
work="$(mktemp -d /var/tmp/kubeauto-longhorn-v2-disk-lifecycle.XXXXXX)"
chmod 0700 "$work"
echo "LONGHORN_V2_DISK_DURABLE_DIRECTORY path=$work"
control_config=/root/.kube/config
[[ ! -L "$control_config" ]] || exit 1
[[ ! -f "$control_config" ]] || cp -a "$control_config" "$work/control-kubeconfig"
phase=preflight relay_pid= admitted=no transport=no resume_business=
finish() {
  local rc=$? cleanup_rc=0
  trap - EXIT INT TERM
  if [[ "$rc" != 0 ]]; then
    printf 'rc=%s phase=%s\n' "$rc" "$phase" > "$work/first-failure.status"
    echo "LONGHORN_FIRST_FAILURE phase=$phase rc=$rc diagnostics=$work" >&2
    kubectl --request-timeout=5s get pods -A -o wide > "$work/pods.txt" 2>&1 || true
    kubectl --request-timeout=5s get events -A --sort-by=.lastTimestamp > "$work/events.txt" 2>&1 || true
  fi
  if [[ "$admitted" == yes && -n "$relay_pid" ]]; then
    "$PY" "$DRIVER" clean --relay-pid "$relay_pid" 2>&1 | tee "$work/cleanup.log" || cleanup_rc=$?
    if [[ "$cleanup_rc" == 0 ]]; then
      [[ "$(grep -Fxc LONGHORN_V2_DISK_LIFECYCLE_SCOPED_CLEAN_PASS "$work/cleanup.log")" == 1 ]] || cleanup_rc=1
    fi
  fi
  if [[ -n "$relay_pid" ]]; then
    if [[ "$rc" == 0 ]] && ! kill -0 "$relay_pid" 2>/dev/null; then rc=1; fi
    kill -TERM "$relay_pid" 2>/dev/null || true
    wait "$relay_pid" || { [[ "$rc" != 0 ]] || rc=1; }
  fi
  if [[ "$transport" == yes ]]; then "$PY" "$DRIVER" relay-clean || cleanup_rc=$?; fi
  if [[ -f "$work/control-kubeconfig" ]]; then
    cp -a "$work/control-kubeconfig" "$control_config" || cleanup_rc=$?
  else
    rm -f -- "$control_config" || cleanup_rc=$?
  fi
  if [[ "$cleanup_rc" == 0 && "$admitted" == yes ]]; then "$PY" "$DRIVER" finalize || cleanup_rc=$?; fi
  "$PY" "$CORE" verify-core || cleanup_rc=$?
  if [[ "$cleanup_rc" != 0 ]]; then
    echo "LONGHORN_CLEANUP_FAIL phase=$phase rc=$cleanup_rc journal=retained diagnostics=$work" >&2
    [[ "$rc" != 0 ]] || rc=$cleanup_rc
  fi
  if [[ "$rc" != 0 && ! -f "$work/first-failure.status" ]]; then
    printf 'rc=%s phase=%s cleanup_rc=%s\n' "$rc" "$phase" "$cleanup_rc" > "$work/first-failure.status"
    echo "LONGHORN_FIRST_FAILURE phase=$phase rc=$rc diagnostics=$work" >&2
  fi
  printf '%s\n' "$rc" > "$work/exit"
  printf '%s\n' "$cleanup_rc" > "$work/finalized"
  if [[ "$rc" == 0 ]]; then
    if [[ "$mode" == v2-disk-lifecycle-cleanup ]]; then
      echo LONGHORN_V2_DISK_LIFECYCLE_CLEANUP_PASS
    else
      echo LONGHORN_V2_DISK_LIFECYCLE_PASS
    fi
  fi
  exit "$rc"
}
trap finish EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
"$PY" "$CORE" verify-transport
if [[ "$mode" == v2-disk-lifecycle-cleanup ]]; then
  [[ -f "$JOURNAL" && ! -L "$JOURNAL" ]] || exit 1
  admitted=yes
else
  if [[ -f "$JOURNAL" && ! -L "$JOURNAL" && "${LONGHORN_FULL_RESUME:-no}" == yes ]]; then
    resume_business="$("$PY" "$BASE/tests/helpers/longhorn-full-evidence.py" disk-resume)"
    [[ -n "$resume_business" && -f "$resume_business" && ! -L "$resume_business" ]] || exit 1
    admitted=yes
  else
  [[ ! -e "$JOURNAL" && ! -L "$JOURNAL" ]] || exit 1
  proof="${LONGHORN_V2_TARGET_RELEASE_PROOF:-}"
  [[ "$proof" == /* && -f "$proof" && ! -L "$proof" ]] || { echo LONGHORN_V2_DISK_REQUIRES_TARGET_RELEASE >&2; exit 1; }
  phase=target-release-admission
  "$PY" "$DRIVER" admit --released-target-proof "$proof" | tee "$work/admission.log"
  admitted=yes
  fi
fi
transport=yes
"$PY" "$DRIVER" relay-ensure
"$PY" "$DRIVER" relay-maintain > "$work/relay.log" 2>&1 &
relay_pid=$!
if [[ "$mode" == v2-disk-lifecycle-cleanup ]]; then phase=owned-disk-recovery; exit 0; fi
if [[ -n "$resume_business" ]]; then
  phase=owned-disk-recovery
  cat -- "$resume_business" | tee "$work/business.log"
  exit 0
fi
phase=product-expansion-evacuation-retirement
"$PY" "$DRIVER" business --relay-pid "$relay_pid" | tee "$work/business.log"
grep -q '^LONGHORN_V2_DISK_CLIENT_EXPANSION_PASS host=192.168.47.149 ' "$work/business.log"
grep -q '^LONGHORN_V2_DISK_NATIVE_RETIREMENT_PASS host=192.168.47.148 ' "$work/business.log"
grep -q '^LONGHORN_V2_DISK_NATIVE_RETIREMENT_PASS host=192.168.47.149 ' "$work/business.log"
grep -q '^LONGHORN_V2_DISK_REPLACEMENT_REIMPORT_PASS source=192.168.47.148 ' "$work/business.log"
grep -Fxq LONGHORN_V2_DISK_LIFECYCLE_BUSINESS_PASS "$work/business.log"
