#!/usr/bin/env bash
# LONGHORN-49 supplement: run after the independent three-replica qualifier.
set -Eeuo pipefail
BASE=/usr/local/kubeauto
cd "$BASE"
PY="$BASE/.venv/bin/python"
CORE="$BASE/tests/helpers/longhorn-v2-core.py"
INSTALL="$BASE/tests/helpers/longhorn-v2-install.py"
BACKUPS="$BASE/tests/helpers/longhorn-v2-backups.py"
DRIVER="$BASE/tests/helpers/longhorn-v2-linked-lifecycle.py"
JOURNAL="$BASE/clusters/longhorn-v2-gate/longhorn-v2-linked-lifecycle.json"
export PYTHONPATH="$BASE" PATH="$BASE/kube-bin:$BASE/extra-bin:$PATH"
export KUBECONFIG="$BASE/clusters/longhorn-v2-gate/kubectl.kubeconfig"
[[ "${LONGHORN_PARENT_LEASE_HELD:-no}" == yes ]] || exit 1
mode="${1:-}"
case "$mode" in v2-linked-lifecycle-qualification|v2-linked-lifecycle-cleanup) ;; *) exit 2;; esac
marker="LONGHORN_${mode^^}"; marker="${marker//-/_}"
work="$(mktemp -d /var/tmp/kubeauto-longhorn-v2-linked-lifecycle.XXXXXX)"
chmod 0700 "$work"
printf 'LONGHORN_DURABLE_WORK path=%s\n' "$work"
phase=preflight
relay_pid=
baseline=
attempted=no
finish() {
  local rc=$? cleanup_rc=0 current=
  trap - EXIT INT TERM
  if [[ "$rc" != 0 ]]; then
    printf 'rc=%s phase=%s\n' "$rc" "$phase" > "$work/first-failure.status"
    echo "LONGHORN_FIRST_FAILURE phase=$phase rc=$rc diagnostics=$work" >&2
    kubectl --request-timeout=5s get pods -A -o wide > "$work/pods.txt" 2>&1 || true
    kubectl --request-timeout=5s get events -A --sort-by=.lastTimestamp > "$work/events.txt" 2>&1 || true
  fi
  if [[ "$attempted" == yes && -n "$relay_pid" ]]; then
    if [[ -e "$JOURNAL" || -L "$JOURNAL" ]]; then
      "$PY" "$DRIVER" clean --relay-pid "$relay_pid" | tee "$work/cleanup.log" || cleanup_rc=$?
      if [[ "$cleanup_rc" == 0 ]]; then
        grep -Fxq LONGHORN_V2_LINKED_LIFECYCLE_SCOPED_CLEAN_PASS "$work/cleanup.log" || cleanup_rc=1
      fi
    elif [[ "$rc" == 0 ]]; then
      # Business owns its successful cleanup. Absence alone proves nothing.
      grep -Fxq LONGHORN_V2_LINKED_LIFECYCLE_SCOPED_CLEAN_PASS "$work/business.log" || cleanup_rc=1
    fi
    if [[ "$cleanup_rc" == 0 ]]; then
      current="$("$PY" "$BACKUPS" baseline)" || cleanup_rc=$?
      printf '%s\n' "$current" | tee "$work/baseline-after.log"
      if [[ "$cleanup_rc" == 0 && -n "$baseline" ]]; then
        [[ "$(grep '^LONGHORN_V2_BACKUP_BASELINE_VERIFIED ' <<<"$baseline")" == \
           "$(grep '^LONGHORN_V2_BACKUP_BASELINE_VERIFIED ' <<<"$current")" ]] || cleanup_rc=1
      fi
      "$PY" "$INSTALL" health | tee "$work/health-after.log" || cleanup_rc=$?
    fi
  fi
  if [[ -n "$relay_pid" ]]; then
    kill -0 "$relay_pid" 2>/dev/null || { [[ "$rc" != 0 ]] || rc=1; }
    kill -TERM "$relay_pid" 2>/dev/null || true
    wait "$relay_pid" || { [[ "$rc" != 0 ]] || rc=1; }
    "$PY" "$CORE" relay-clean || cleanup_rc=$?
    "$PY" "$CORE" verify-core || cleanup_rc=$?
  fi
  if [[ "$cleanup_rc" != 0 ]]; then
    echo "LONGHORN_CLEANUP_FAIL phase=$phase rc=$cleanup_rc journal=retained diagnostics=$work" >&2
    [[ "$rc" != 0 ]] || rc=$cleanup_rc
  elif [[ "$rc" == 0 ]]; then
    echo "${marker}_SCOPED_CLEAN_PASS"
    echo "${marker}_PASS"
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
"$PY" "$CORE" verify-transport
if [[ "$mode" == v2-linked-lifecycle-cleanup ]]; then
  # An empty cleanup invocation must never manufacture a scoped PASS.
  [[ -f "$JOURNAL" && ! -L "$JOURNAL" ]] || { echo 'LONGHORN_LINKED_RECOVERY_REQUIRES_OWNED_JOURNAL' >&2; exit 1; }
else
  [[ ! -e "$JOURNAL" && ! -L "$JOURNAL" ]]
  baseline="$("$PY" "$BACKUPS" baseline)"
  printf '%s\n' "$baseline" | tee "$work/baseline-before.log"
  grep -q '^LONGHORN_V2_BACKUP_BASELINE_VERIFIED ' <<<"$baseline"
fi
"$PY" "$INSTALL" health | tee "$work/health-before.log"
"$PY" "$INSTALL" relay-ensure
"$PY" "$INSTALL" relay-maintain > "$work/relay.log" 2>&1 &
relay_pid=$!
attempted=yes
if [[ "$mode" == v2-linked-lifecycle-cleanup ]]; then phase=owned-fixture-cleanup; exit 0; fi
phase=linked-dependency-rebuild-and-independent-restore
"$PY" "$DRIVER" business --relay-pid "$relay_pid" | tee "$work/business.log"
grep -q '^LONGHORN_V2_LINKED_DEPENDENCY_REBUILD_VERIFIED ' "$work/business.log"
grep -q '^LONGHORN_V2_LINKED_INDEPENDENT_RESTORE_VERIFIED ' "$work/business.log"
grep -Fxq LONGHORN_V2_LINKED_LIFECYCLE_SCOPED_CLEAN_PASS "$work/business.log"
grep -Fxq LONGHORN_V2_LINKED_LIFECYCLE_PASS "$work/business.log"
