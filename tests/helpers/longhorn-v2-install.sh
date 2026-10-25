#!/usr/bin/env bash
# Narrow AIO prerequisite; preserve the verified installation for owning modules.
set -Eeuo pipefail
BASE=/usr/local/kubeauto
cd "$BASE"
PY="$BASE/.venv/bin/python"
CORE="$BASE/tests/helpers/longhorn-v2-core.py"
RESOURCES="$BASE/tests/helpers/longhorn-v2-resources.py"
INSTALL="$BASE/tests/helpers/longhorn-v2-install.py"
export PYTHONPATH="$BASE" PATH="$BASE/kube-bin:$BASE/extra-bin:$PATH"
export KUBECONFIG="$BASE/clusters/longhorn-v2-gate/kubectl.kubeconfig"
[[ "${LONGHORN_PARENT_LEASE_HELD:-no}" == yes ]] || exit 1
MODE="${1:?mode required}"
case "$MODE" in v2-aio-install-qualification|v2-host-qualification|v2-host-recovery) ;; *) exit 2;; esac
marker="LONGHORN_${MODE^^}"
marker="${marker//-/_}"
"$PY" "$CORE" verify-transport
"$PY" "$CORE" verify-core
if [[ "$MODE" == v2-aio-install-qualification ]]; then
  "$PY" "$RESOURCES" qualification-current
fi
work="$(mktemp -d /var/tmp/kubeauto-longhorn-v2-install.XXXXXX)"
chmod 0700 "$work"
phase=transport
relay_pid=
owned=no
install_exit() {
  local rc=$? cleanup_rc=0
  trap - EXIT INT TERM
  if [[ "$rc" != 0 ]]; then
    echo "LONGHORN_FIRST_FAILURE phase=$phase rc=$rc diagnostics=$work" >&2
    printf 'rc=%s phase=%s\n' "$rc" "$phase" > "$work/first-failure.status"
    kubectl --request-timeout=5s get nodes -o yaml > "$work/nodes.yaml" 2>&1 || true
    kubectl --request-timeout=5s get pods -A -o wide > "$work/pods.txt" 2>&1 || true
    kubectl --request-timeout=5s get events -A --sort-by=.lastTimestamp > "$work/events.txt" 2>&1 || true
  fi
  if [[ "$owned" == yes ]]; then
    "$PY" "$INSTALL" business-clean --relay-pid "${relay_pid:-0}" || cleanup_rc=$?
    if [[ "$rc" != 0 && "$cleanup_rc" == 0 ]]; then
      "$PY" "$INSTALL" recover --relay-pid "${relay_pid:-0}" || cleanup_rc=$?
      if [[ "$cleanup_rc" == 0 ]]; then
        "$PY" "$RESOURCES" restore || cleanup_rc=$?
      fi
    elif [[ "$rc" == 0 && "$cleanup_rc" == 0 ]]; then
      "$PY" "$INSTALL" health || cleanup_rc=$?
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
trap install_exit EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
"$PY" "$INSTALL" relay-ensure
"$PY" "$INSTALL" relay-maintain &
relay_pid=$!
if [[ "$MODE" == v2-host-recovery ]]; then
  phase=host-recovery
  "$PY" "$INSTALL" recover-host-qualification
  phase=core-smoke
  "$PY" "$CORE" smoke
  exit 0
fi
phase=host-journal
"$PY" "$INSTALL" capture-hosts
owned=yes
phase=multipath-preparation
"$PY" "$INSTALL" quiesce-multipath
if [[ "$MODE" == v2-host-qualification ]]; then
  phase=host-restore
  "$PY" "$INSTALL" restore-hosts
  owned=no
  phase=core-smoke
  "$PY" "$CORE" smoke
  exit 0
fi
phase=hugepage-prepare
"$PY" "$RESOURCES" prepare
"$PY" "$RESOURCES" verify
phase=configure
"$PY" "$INSTALL" configure
phase=artifacts
"$PY" "$INSTALL" cache
"$PY" "$INSTALL" supervise --relay-pid "$relay_pid" --step longhorn-download
phase=install
"$PY" "$INSTALL" supervise --relay-pid "$relay_pid" --step longhorn-setup
"$PY" "$INSTALL" health
phase=business
"$PY" "$INSTALL" business --relay-pid "$relay_pid" | tee "$work/business.log"
grep -Eq '^LONGHORN_FSYNC_REMOUNT_VERIFIED bytes=9437184 sha256=[a-f0-9]{64} replica_nodes=' "$work/business.log"
grep -Fxq LONGHORN_INSTALL_BUSINESS_PASS "$work/business.log"
grep -Fxq LONGHORN_BUSINESS_FIXTURES_CLEAN_PASS "$work/business.log"
