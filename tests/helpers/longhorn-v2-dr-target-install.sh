#!/usr/bin/env bash
# Target-only installation prerequisite, never a DR business or site-HA PASS.
set -Eeuo pipefail
BASE=/usr/local/kubeauto
cd "$BASE"
PY="$BASE/.venv/bin/python"
TARGET="$BASE/tests/helpers/longhorn-v2-dr-target.py"
INSTALL="$BASE/tests/helpers/longhorn-v2-dr-target-install.py"
DIR="$BASE/clusters/longhorn-v2-dr-target"
export PYTHONPATH="$BASE" PATH="$BASE/kube-bin:$BASE/extra-bin:$PATH"
export KUBECONFIG="$DIR/kubectl.kubeconfig"
[[ "${LONGHORN_PARENT_LEASE_HELD:-no}" == yes ]] || exit 1
mode="${1:-}"
case "$mode" in v2-dr-target-install-qualification|v2-dr-target-install-recovery) ;; *) exit 2;; esac
marker="LONGHORN_${mode^^}"
marker="${marker//-/_}"
"$PY" "$INSTALL" core-current
source_before="$("$PY" "$TARGET" source-boundary)"
printf '%s\n' "$source_before"
work="$(mktemp -d /var/tmp/kubeauto-longhorn-v2-dr-install.XXXXXX)"
chmod 0700 "$work"
control_config=/root/.kube/config
[[ ! -L "$control_config" ]] || exit 1
[[ ! -f "$control_config" ]] || cp -a "$control_config" "$work/control-kubeconfig"
phase=preflight
relay_pid=
owned=no
install_exit() {
  local rc=$? cleanup_rc=0 source_after
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
    elif [[ "$rc" == 0 && "$cleanup_rc" == 0 ]]; then
      "$PY" "$INSTALL" health || cleanup_rc=$?
    fi
  fi
  if [[ -n "$relay_pid" ]]; then
    if [[ "$rc" == 0 ]] && ! kill -0 "$relay_pid" 2>/dev/null; then rc=1; fi
    kill -TERM "$relay_pid" 2>/dev/null || true
    wait "$relay_pid" || { [[ "$rc" != 0 ]] || rc=1; }
  fi
  "$PY" "$TARGET" relay-clean || cleanup_rc=$?
  "$PY" "$TARGET" core-identity || cleanup_rc=$?
  if [[ -f "$work/control-kubeconfig" ]]; then
    cp -a "$work/control-kubeconfig" "$control_config" || cleanup_rc=$?
  else
    rm -f -- "$control_config" || cleanup_rc=$?
  fi
  source_after="$("$PY" "$TARGET" source-boundary)" || cleanup_rc=$?
  printf '%s\n' "$source_after"
  if [[ "$(grep '^LONGHORN_V2_DR_SOURCE_BOUNDARY ' <<<"$source_before")" != \
        "$(grep '^LONGHORN_V2_DR_SOURCE_BOUNDARY ' <<<"$source_after")" ]]; then cleanup_rc=1; fi
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
if [[ "$mode" == v2-dr-target-install-qualification ]]; then
  "$PY" "$INSTALL" host-inputs
fi
phase=transport
"$PY" "$TARGET" qualification-current
"$PY" "$TARGET" relay-ensure
"$PY" "$TARGET" relay-maintain &
relay_pid=$!
if [[ "$mode" == v2-dr-target-install-recovery ]]; then
  phase=installation-recovery
  "$PY" "$INSTALL" recover --relay-pid "$relay_pid"
  exit 0
fi
phase=host-journal
"$PY" "$INSTALL" capture-hosts
owned=yes
phase=multipath
"$PY" "$INSTALL" quiesce-multipath
phase=resources
"$PY" "$INSTALL" prepare-resources
"$PY" "$INSTALL" verify-resources
phase=configure
"$PY" "$INSTALL" configure
phase=artifacts
"$PY" "$INSTALL" cache
"$PY" "$TARGET" supervise --relay-pid "$relay_pid" --step longhorn-download
phase=install
"$PY" "$TARGET" supervise --relay-pid "$relay_pid" --step longhorn-setup
"$PY" "$INSTALL" health
phase=business
"$PY" "$INSTALL" business --relay-pid "$relay_pid" | tee "$work/business.log"
grep -Eq '^LONGHORN_V2_DR_TARGET_FSYNC_REMOUNT_VERIFIED bytes=9437184 sha256=[a-f0-9]{64} replicas=1 cross_node=false$' "$work/business.log"
grep -Fxq LONGHORN_V2_DR_TARGET_INSTALL_BUSINESS_PASS "$work/business.log"
grep -Fxq LONGHORN_BUSINESS_FIXTURES_CLEAN_PASS "$work/business.log"
