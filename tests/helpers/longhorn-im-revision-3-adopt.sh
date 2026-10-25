#!/usr/bin/env bash
# Exact .2 -> .3 IM adoption; preserve cluster, image and journal on failure.
set -Eeuo pipefail
BASE=/usr/local/kubeauto
cd "$BASE"
PY="$BASE/.venv/bin/python"
CORE="$BASE/tests/helpers/longhorn-v2-core.py"
INSTALL="$BASE/tests/helpers/longhorn-v2-install.py"
DRIVER="$BASE/tests/helpers/longhorn-v2-im-adopt.py"
export PYTHONPATH="$BASE" PATH="$BASE/kube-bin:$BASE/extra-bin:$PATH"
[[ "${LONGHORN_PARENT_LEASE_HELD:-no}" == yes ]] || exit 1
mode="${1:-}"
revision=no
case "$mode" in
  v1-im-revision-3-adopt-qualification|v2-im-revision-3-adopt-qualification|v2-dr-target-im-revision-3-adopt-qualification)
    revision=yes
    DRIVER="$BASE/tests/helpers/longhorn-im-revision-3-adopt.py"
    mode="${mode/-im-revision-3-adopt-qualification/-im-adopt-qualification}"
    ;;
  *) exit 2 ;;
esac
transport_step=verify-transport
core_step=verify-core
case "$mode" in
  v1-im-adopt-qualification)
    engine=v1
    CORE="$BASE/tests/helpers/longhorn-lab.py"
    INSTALL="$CORE"
    export KUBECONFIG="$BASE/clusters/longhorn-gate/kubectl.kubeconfig"
    ;;
  v2-im-adopt-qualification)
    engine=v2
    export KUBECONFIG="$BASE/clusters/longhorn-v2-gate/kubectl.kubeconfig"
    ;;
  v2-dr-target-im-adopt-qualification)
    engine=v2-dr-target
    CORE="$BASE/tests/helpers/longhorn-v2-dr-target.py"
    INSTALL="$CORE"
    transport_step=qualification-current
    core_step=core-identity
    export KUBECONFIG="$BASE/clusters/longhorn-v2-dr-target/kubectl.kubeconfig"
    ;;
  *) exit 2 ;;
esac
marker="LONGHORN_${engine^^}_IM_ADOPT_QUALIFICATION"
if [[ "$revision" == yes ]]; then marker="LONGHORN_${engine^^}_IM_REVISION_3_ADOPT_QUALIFICATION"; fi
marker="${marker//-/_}"
"$PY" "$DRIVER" preflight --engine "$engine"
"$PY" "$CORE" "$transport_step"
"$PY" "$CORE" "$core_step"
work="$(mktemp -d /var/tmp/kubeauto-longhorn-v2-im-adopt.XXXXXX)"
chmod 0700 "$work"
echo "${marker}_DURABLE_DIRECTORY path=$work"
phase=relay
relay_pid=
adopt_exit() {
  local rc=$? cleanup_rc=0
  trap - EXIT INT TERM
  if [[ "$rc" != 0 ]]; then
    printf 'rc=%s phase=%s\n' "$rc" "$phase" > "$work/first-failure.status"
    echo "LONGHORN_FIRST_FAILURE phase=$phase rc=$rc diagnostics=$work image=retained journal=retained" >&2
    kubectl --request-timeout=5s get pods -A -o wide > "$work/pods.txt" 2>&1 || true
    kubectl --request-timeout=5s get instancemanagers.longhorn.io -n longhorn-system -o yaml > "$work/managers.yaml" 2>&1 || true
    kubectl --request-timeout=5s get events -A --sort-by=.lastTimestamp > "$work/events.txt" 2>&1 || true
    "$PY" "$DRIVER" clean --engine "$engine" --relay-pid "${relay_pid:-0}" || cleanup_rc=$?
  fi
  if [[ -n "$relay_pid" ]]; then
    if [[ "$rc" == 0 ]] && ! kill -0 "$relay_pid" 2>/dev/null; then rc=1; fi
    kill -TERM "$relay_pid" 2>/dev/null || true
    wait "$relay_pid" || { [[ "$rc" != 0 ]] || rc=1; }
  fi
  "$PY" "$CORE" relay-clean || cleanup_rc=$?
  "$PY" "$CORE" "$core_step" || cleanup_rc=$?
  if [[ "$revision" == yes && "$rc" == 0 && "$cleanup_rc" == 0 ]]; then
    phase=independent-readback
    "$PY" "$DRIVER" readback --engine "$engine" --log "$work/business.log" --output "$work/independent-readback.json" \
      > "$work/independent-readback.log" 2>&1 || rc=$?
    if [[ "$rc" == 0 && "$(grep -c "^${marker%_QUALIFICATION}_INDEPENDENT_READBACK " "$work/independent-readback.log")" != 1 ]]; then
      rc=1
    fi
    if [[ "$rc" == 0 && ( ! -s "$work/independent-readback.json" || -L "$work/independent-readback.json" ) ]]; then rc=1; fi
    if [[ "$rc" != 0 ]]; then
      printf 'rc=%s phase=%s\n' "$rc" "$phase" > "$work/first-failure.status"
      echo "LONGHORN_FIRST_FAILURE phase=$phase rc=$rc diagnostics=$work/independent-readback.log" >&2
    fi
  fi
  if [[ "$cleanup_rc" != 0 ]]; then
    echo "LONGHORN_CLEANUP_FAIL phase=$phase rc=$cleanup_rc journal=retained" >&2
    [[ "$rc" != 0 ]] || rc=$cleanup_rc
  fi
  printf '%s\n' "$rc" > "$work/exit"
  if [[ "$rc" == 0 ]]; then
    echo "${marker}_SCOPED_CLEAN_PASS"
    echo "${marker}_PASS"
  fi
  exit "$rc"
}
trap adopt_exit EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
"$PY" "$INSTALL" relay-ensure
"$PY" "$INSTALL" relay-maintain &
relay_pid=$!
phase=published-image-cache
# publication_records must already include the verified dual-registry new pin.
"$PY" "$DRIVER" cache --engine "$engine"
phase=product-adoption-and-business
"$PY" "$DRIVER" business --engine "$engine" --relay-pid "$relay_pid" | tee "$work/business.log"
phase=finalize
"$PY" "$DRIVER" finish --engine "$engine" | tee -a "$work/business.log"
"$PY" "$DRIVER" verify --engine "$engine" --log "$work/business.log"
