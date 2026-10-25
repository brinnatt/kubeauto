#!/usr/bin/env bash
# Scoped V2 recurring qualification; preserve the owned healthy installation.
set -Eeuo pipefail
BASE=/usr/local/kubeauto
cd "$BASE"
PY="$BASE/.venv/bin/python"
CORE="$BASE/tests/helpers/longhorn-v2-core.py"
INSTALL="$BASE/tests/helpers/longhorn-v2-install.py"
BACKUPS="$BASE/tests/helpers/longhorn-v2-backups.py"
RECURRING="$BASE/tests/helpers/longhorn-v2-recurring.py"
export PYTHONPATH="$BASE" PATH="$BASE/kube-bin:$BASE/extra-bin:$PATH"
export KUBECONFIG="$BASE/clusters/longhorn-v2-gate/kubectl.kubeconfig"
[[ "${LONGHORN_PARENT_LEASE_HELD:-no}" == yes ]] || exit 1
mode="${1:-}"
case "$mode" in
  v2-recurring-selector-qualification|v2-recurring-snapshot-qualification|v2-recurring-backup-qualification|v2-recurring-rotation-qualification) ;;
  *) exit 2;;
esac
stage="${mode#v2-recurring-}"
stage="${stage%-qualification}"
marker="LONGHORN_${mode^^}"
marker="${marker//-/_}"
"$PY" "$CORE" verify-transport
"$PY" "$BACKUPS" qualification-current
check_baseline() {
  [[ "$(grep -Ec '^LONGHORN_V2_BACKUP_BASELINE_VERIFIED target_sha256=[a-f0-9]{64} backup_objects=0 fixture_objects=0$' <<<"$1")" == 1 ]] &&
  [[ "$(grep -Ec '^LONGHORN_V2_RECURRING_BASELINE_VERIFIED sha256=[a-f0-9]{64}$' <<<"$1")" == 1 ]]
}
baseline="$("$PY" "$RECURRING" baseline)"
printf '%s\n' "$baseline"
check_baseline "$baseline"
work="$(mktemp -d /var/tmp/kubeauto-longhorn-v2-recurring.XXXXXX)"
chmod 0700 "$work"
phase=transport
relay_pid=
recurring_exit() {
  local rc=$? cleanup_rc=0 current
  trap - EXIT INT TERM
  if [[ "$rc" != 0 ]]; then
    echo "LONGHORN_FIRST_FAILURE phase=$phase rc=$rc diagnostics=$work" >&2
    printf 'rc=%s phase=%s\n' "$rc" "$phase" > "$work/first-failure.status"
    kubectl --request-timeout=5s get nodes -o yaml > "$work/nodes.yaml" 2>&1 || true
    kubectl --request-timeout=5s get pods -A -o wide > "$work/pods.txt" 2>&1 || true
    kubectl --request-timeout=5s get events -A --sort-by=.lastTimestamp > "$work/events.txt" 2>&1 || true
  fi
  "$PY" "$RECURRING" cleanup --relay-pid "${relay_pid:-0}" || cleanup_rc=$?
  if [[ "$cleanup_rc" == 0 ]]; then
    current="$("$PY" "$RECURRING" baseline)" || cleanup_rc=$?
    printf '%s\n' "$current"
    check_baseline "$current" || cleanup_rc=$?
    if [[ "$cleanup_rc" == 0 ]]; then
      [[ "$(grep '^LONGHORN_V2_\(BACKUP\|RECURRING\)_BASELINE_VERIFIED ' <<<"$baseline")" == \
         "$(grep '^LONGHORN_V2_\(BACKUP\|RECURRING\)_BASELINE_VERIFIED ' <<<"$current")" ]] || cleanup_rc=1
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
    echo "LONGHORN_CLEANUP_FAIL phase=$phase rc=$cleanup_rc journal=retained" >&2
    [[ "$rc" != 0 ]] || rc=$cleanup_rc
  elif [[ "$rc" == 0 ]]; then
    rm -rf -- "$work"
    echo "${marker}_SCOPED_CLEAN_PASS"
    echo "${marker}_PASS"
  fi
  exit "$rc"
}
trap recurring_exit EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
"$PY" "$INSTALL" relay-ensure
"$PY" "$INSTALL" relay-maintain &
relay_pid=$!
phase="$stage"
"$PY" "$RECURRING" "$stage" --relay-pid "$relay_pid" | tee "$work/business.log"
"$PY" "$RECURRING" verify --stage "$stage" --log "$work/business.log"
