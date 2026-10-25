#!/usr/bin/env bash
# LONGHORN-26 native V1 extra filesystems. Preserve the original six-node core.
set -Eeuo pipefail
BASE=/usr/local/kubeauto
cd "$BASE"
PY="$BASE/.venv/bin/python"
LAB="$BASE/tests/helpers/longhorn-lab.py"
DRIVER="$BASE/tests/helpers/longhorn-disk-lifecycle.py"
JOURNAL="$BASE/clusters/longhorn-gate/longhorn-disk-lifecycle.json"
ALLOWLIST="$BASE/tests/fixtures/longhorn-v1-qualified-disks.txt"
export PYTHONPATH="$BASE" PATH="$BASE/kube-bin:$BASE/extra-bin:$PATH"
export KUBECONFIG="$BASE/clusters/longhorn-gate/kubectl.kubeconfig"
[[ "${LONGHORN_PARENT_LEASE_HELD:-no}" == yes ]] || exit 1
mode="${1:-}"
case "$mode" in disk-lifecycle|disk-lifecycle-cleanup) ;; *) exit 2;; esac
work="$(mktemp -d /var/tmp/kubeauto-longhorn-disk-lifecycle.XXXXXX)"
chmod 0700 "$work"
echo "LONGHORN_DISK_DURABLE_WORK path=$work"
phase=preflight
relay_pid=
attempted=no
finish() {
  local rc=$? cleanup_rc=0
  trap - EXIT INT TERM
  if [[ "$rc" != 0 ]]; then
    printf 'rc=%s phase=%s\n' "$rc" "$phase" > "$work/first-failure.status"
    echo "LONGHORN_FIRST_FAILURE phase=$phase rc=$rc diagnostics=$work" >&2
    kubectl --request-timeout=5s get pods -A -o wide > "$work/pods.txt" 2>&1 || true
    kubectl --request-timeout=5s get events -A --sort-by=.lastTimestamp > "$work/events.txt" 2>&1 || true
  fi
  # Recover native flags and owned mount fixtures while the Registry relay is
  # still supervised. Empty cleanup cannot manufacture scoped acceptance.
  if [[ "$attempted" == yes && -n "$relay_pid" ]]; then
    if [[ -e "$JOURNAL" || -L "$JOURNAL" ]]; then
      "$PY" "$DRIVER" clean --relay-pid "$relay_pid" | tee "$work/cleanup.log" || cleanup_rc=$?
      if [[ "$cleanup_rc" == 0 ]]; then
        grep -Fxq LONGHORN_DISK_LIFECYCLE_SCOPED_CLEAN_PASS "$work/cleanup.log" || cleanup_rc=1
      fi
    elif [[ "$rc" == 0 ]]; then
      grep -Fxq LONGHORN_DISK_LIFECYCLE_SCOPED_CLEAN_PASS "$work/business.log" || cleanup_rc=1
    fi
    if [[ "$cleanup_rc" == 0 ]]; then
      "$PY" "$LAB" health | tee "$work/health-after.log" || cleanup_rc=$?
    fi
  fi
  if [[ -n "$relay_pid" ]]; then
    kill -0 "$relay_pid" 2>/dev/null || { [[ "$rc" != 0 ]] || rc=1; }
    kill -TERM "$relay_pid" 2>/dev/null || true
    wait "$relay_pid" || { [[ "$rc" != 0 ]] || rc=1; }
    "$PY" "$LAB" relay-clean || cleanup_rc=$?
    "$PY" "$LAB" verify-core || cleanup_rc=$?
  fi
  if [[ "$cleanup_rc" != 0 ]]; then
    echo "LONGHORN_CLEANUP_FAIL phase=$phase rc=$cleanup_rc journal=retained diagnostics=$work" >&2
    [[ "$rc" != 0 ]] || rc=$cleanup_rc
  elif [[ "$rc" == 0 ]]; then
    if [[ "$mode" == disk-lifecycle-cleanup ]]; then
      echo LONGHORN_DISK_LIFECYCLE_CLEANUP_SCOPED_CLEAN_PASS
      echo LONGHORN_DISK_LIFECYCLE_CLEANUP_PASS
    else
      echo LONGHORN_DISK_LIFECYCLE_GATE_SCOPED_CLEAN_PASS
      echo LONGHORN_DISK_LIFECYCLE_GATE_PASS
    fi
  fi
  if [[ "$rc" != 0 && ! -f "$work/first-failure.status" ]]; then
    printf 'rc=%s phase=%s cleanup_rc=%s\n' "$rc" "$phase" "$cleanup_rc" > "$work/first-failure.status"
    echo "LONGHORN_FIRST_FAILURE phase=$phase rc=$rc diagnostics=$work" >&2
  fi
  printf 'rc=%s phase=%s cleanup_rc=%s\n' "$rc" "$phase" "$cleanup_rc" > "$work/status.tmp"
  mv "$work/status.tmp" "$work/status"
  exit "$rc"
}
trap finish EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
"$PY" "$LAB" verify-transport
"$PY" "$LAB" verify-core
if [[ "$mode" == disk-lifecycle-cleanup ]]; then
  [[ -f "$JOURNAL" && ! -L "$JOURNAL" ]] || { echo 'LONGHORN_DISK_RECOVERY_REQUIRES_OWNED_JOURNAL' >&2; exit 1; }
else
  [[ ! -e "$JOURNAL" && ! -L "$JOURNAL" ]]
  [[ -f "$ALLOWLIST" && ! -L "$ALLOWLIST" ]]
  # This is the accepted 18-disk qualification input, not a new admission.
  # The Python fixture rechecks actual hardware and blank state before writes.
  [[ "$(sha256sum "$ALLOWLIST" | cut -d ' ' -f 1)" == fd50cf8ccb3b1f1e0b1efa0a1b7fc576cf84f897af6d5f59460cb87f412ee7c9 ]]
  cp "$ALLOWLIST" "$work/disk-allowlist"
fi
if [[ "$mode" == disk-lifecycle ]]; then
  "$PY" "$LAB" health | tee "$work/health-before.log"
fi
"$PY" "$LAB" relay-ensure
"$PY" "$LAB" relay-maintain > "$work/relay.log" 2>&1 &
relay_pid=$!
attempted=yes
if [[ "$mode" == disk-lifecycle-cleanup ]]; then phase=owned-disk-recovery; exit 0; fi
phase=filesystem-add-duplicate-eviction-replacement
"$PY" "$DRIVER" business --relay-pid "$relay_pid" --allowlist "$work/disk-allowlist" | tee "$work/business.log"
grep -q '^LONGHORN_DISK_DUPLICATE_FILESYSTEM_REFUSAL ' "$work/business.log"
grep -q '^LONGHORN_DISK_NATIVE_EVACUATION_VERIFIED ' "$work/business.log"
grep -q '^LONGHORN_DISK_REPLACEMENT_VERIFIED ' "$work/business.log"
grep -Fxq LONGHORN_DISK_LIFECYCLE_SCOPED_CLEAN_PASS "$work/business.log"
grep -Fxq LONGHORN_DISK_LIFECYCLE_PASS "$work/business.log"
