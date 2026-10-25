#!/usr/bin/env bash
# Absent-volume reconstruction only; no independent DR site or full module PASS.
set -Eeuo pipefail
BASE=/usr/local/kubeauto
cd "$BASE"
PY="$BASE/.venv/bin/python"
CORE="$BASE/tests/helpers/longhorn-v2-core.py"
INSTALL="$BASE/tests/helpers/longhorn-v2-install.py"
RESTORE="$BASE/tests/helpers/longhorn-v2-systemrestore.py"
export PYTHONPATH="$BASE" PATH="$BASE/kube-bin:$BASE/extra-bin:$PATH"
export KUBECONFIG="$BASE/clusters/longhorn-v2-gate/kubectl.kubeconfig"
[[ "${LONGHORN_PARENT_LEASE_HELD:-no}" == yes ]] || exit 1
mode="${1:-}"
case "$mode" in v2-systemrestore-qualification|v2-systemrestore-recovery) ;; *) exit 2;; esac
marker="LONGHORN_${mode^^}"
marker="${marker//-/_}"
"$PY" "$CORE" verify-transport
"$PY" "$BASE/tests/helpers/longhorn-v2-backups.py" qualification-current
if [[ "$mode" == v2-systemrestore-recovery ]]; then
  baseline="$("$PY" "$RESTORE" recovery-baseline)"
else
  baseline="$("$PY" "$RESTORE" baseline)"
fi
printf '%s\n' "$baseline"
work="$(mktemp -d /var/tmp/kubeauto-longhorn-v2-systemrestore.XXXXXX)"
chmod 0700 "$work"
phase=transport
relay_pid=
restore_exit() {
  local rc=$? cleanup_rc=0 current
  trap - EXIT INT TERM
  if [[ "$rc" != 0 ]]; then
    echo "LONGHORN_FIRST_FAILURE phase=$phase rc=$rc diagnostics=$work" >&2
    printf 'rc=%s phase=%s\n' "$rc" "$phase" > "$work/first-failure.status"
    kubectl --request-timeout=5s get nodes -o yaml > "$work/nodes.yaml" 2>&1 || true
    kubectl --request-timeout=5s get pods -A -o wide > "$work/pods.txt" 2>&1 || true
    kubectl --request-timeout=5s get events -A --sort-by=.lastTimestamp > "$work/events.txt" 2>&1 || true
    kubectl --request-timeout=5s -n longhorn-system get systemrestores.longhorn.io,volumes.longhorn.io -o yaml > "$work/restore-state.yaml" 2>&1 || true
  fi
  "$PY" "$RESTORE" cleanup --relay-pid "${relay_pid:-0}" || cleanup_rc=$?
  if [[ "$cleanup_rc" == 0 ]]; then
    current="$("$PY" "$RESTORE" baseline)" || cleanup_rc=$?
    printf '%s\n' "$current"
    if [[ "$cleanup_rc" == 0 ]]; then
      [[ "$(grep '^LONGHORN_V2_BACKUP_BASELINE_VERIFIED ' <<<"$baseline")" == \
         "$(grep '^LONGHORN_V2_BACKUP_BASELINE_VERIFIED ' <<<"$current")" ]] || cleanup_rc=1
    fi
  fi
  if [[ -n "$relay_pid" ]]; then
    if [[ "$rc" == 0 ]] && ! kill -0 "$relay_pid" 2>/dev/null; then rc=1; fi
    kill -TERM "$relay_pid" 2>/dev/null || true
    wait "$relay_pid" || { [[ "$rc" != 0 ]] || rc=1; }
  fi
  "$PY" "$CORE" relay-clean || cleanup_rc=$?
  "$PY" "$CORE" verify-core || cleanup_rc=$?
  if [[ "$cleanup_rc" != 0 ]]; then
    echo "LONGHORN_CLEANUP_FAIL phase=$phase rc=$cleanup_rc recovery_material=retained" >&2
    [[ "$rc" != 0 ]] || rc=$cleanup_rc
  elif [[ "$rc" == 0 ]]; then
    rm -rf -- "$work"
    echo "${marker}_SCOPED_CLEAN_PASS"
    echo "${marker}_PASS"
  fi
  exit "$rc"
}
trap restore_exit EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
"$PY" "$INSTALL" relay-ensure
"$PY" "$INSTALL" relay-maintain &
relay_pid=$!
phase=systemrestore
action=business
[[ "$mode" != v2-systemrestore-recovery ]] || action=recover
"$PY" "$RESTORE" "$action" --relay-pid "$relay_pid" | tee "$work/business.log"
"$PY" "$RESTORE" verify --log "$work/business.log"
