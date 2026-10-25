#!/usr/bin/env bash
# Independent target transport/core prerequisites; no source storage mutation.
set -Eeuo pipefail
BASE=/usr/local/kubeauto
cd "$BASE"
PY="$BASE/.venv/bin/python"
HELPER="$BASE/tests/helpers/longhorn-v2-dr-target.py"
DIR="$BASE/clusters/longhorn-v2-dr-target"
export PYTHONPATH="$BASE" PATH="$BASE/kube-bin:$BASE/extra-bin:$PATH"
export KUBECONFIG="$DIR/kubectl.kubeconfig"
[[ "${LONGHORN_PARENT_LEASE_HELD:-no}" == yes ]] || exit 1
mode="${1:?mode required}"
case "$mode" in v2-dr-target-registry-qualification|v2-dr-target-core) ;; *) exit 2 ;; esac
marker="LONGHORN_${mode^^}"
marker="${marker//-/_}"
source_before="$("$PY" "$HELPER" source-boundary)"
printf '%s\n' "$source_before"
"$PY" "$HELPER" host-inputs
[[ ! -e "$DIR" && ! -L "$DIR" ]] || {
  echo 'LONGHORN_PREFLIGHT_FAIL class=environment reason=dr-target-requires-clean-start' >&2; exit 1;
}
bash "$BASE/tests/helpers/lab-wipe-nodes.sh" --verify --rocky-only 192.168.47.149
if [[ "$mode" == v2-dr-target-core ]]; then
  "$PY" "$HELPER" qualification-current
fi
work="$(mktemp -d /var/tmp/kubeauto-longhorn-v2-dr-target.XXXXXX)"
chmod 0700 "$work"
control_config=/root/.kube/config
[[ ! -L "$control_config" ]] || exit 1
[[ ! -f "$control_config" ]] || cp -a "$control_config" "$work/control-kubeconfig"
phase=configure
owned=no
relay_pid=
target_exit() {
  local rc=$? cleanup_rc=0 source_after
  trap - EXIT INT TERM
  if [[ "$rc" != 0 ]]; then
    echo "LONGHORN_FIRST_FAILURE phase=$phase rc=$rc diagnostics=$work" >&2
    printf 'rc=%s phase=%s\n' "$rc" "$phase" > "$work/first-failure.status"
    if [[ -f "$KUBECONFIG" ]]; then
      kubectl --request-timeout=5s get nodes -o yaml > "$work/nodes.yaml" 2>&1 || true
      kubectl --request-timeout=5s get pods -A -o wide > "$work/pods.txt" 2>&1 || true
      kubectl --request-timeout=5s get events -A --sort-by=.lastTimestamp > "$work/events.txt" 2>&1 || true
    fi
  fi
  if [[ -n "$relay_pid" ]]; then
    if [[ "$rc" == 0 ]] && ! kill -0 "$relay_pid" 2>/dev/null; then rc=1; fi
    kill -TERM "$relay_pid" 2>/dev/null || true
    wait "$relay_pid" || { [[ "$rc" != 0 ]] || rc=1; }
  fi
  if [[ "$owned" == yes ]]; then
    "$PY" "$HELPER" relay-clean || cleanup_rc=$?
    if [[ "$cleanup_rc" == 0 ]]; then
      if [[ "$mode" == v2-dr-target-registry-qualification ]]; then
        "$PY" "$HELPER" qualification-clean || cleanup_rc=$?
      elif [[ "$rc" != 0 ]]; then
        "$PY" "$HELPER" core-cleanup || cleanup_rc=$?
      else
        "$PY" "$HELPER" core-identity || cleanup_rc=$?
      fi
    fi
  fi
  if [[ -f "$work/control-kubeconfig" ]]; then
    cp -a "$work/control-kubeconfig" "$control_config" || cleanup_rc=$?
  else
    rm -f -- "$control_config" || cleanup_rc=$?
  fi
  source_after="$("$PY" "$HELPER" source-boundary)" || cleanup_rc=$?
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
trap target_exit EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
"$PY" "$BASE/kubecli.py" new longhorn-v2-dr-target </dev/null
# A configure failure keeps its input directory for diagnosis; it cannot be
# adopted by a later clean start or silently removed without an owner receipt.
"$PY" "$HELPER" configure
owned=yes
phase=transport
if [[ "$mode" == v2-dr-target-registry-qualification ]]; then
  "$PY" "$HELPER" relay-qualification
fi
"$PY" "$HELPER" relay-ensure
"$PY" "$HELPER" relay-maintain &
relay_pid=$!
if [[ "$mode" == v2-dr-target-registry-qualification ]]; then
  phase=transport-lifetime
  started=$SECONDS
  while (( SECONDS - started < 120 )); do
    kill -0 "$relay_pid"
    if (( (SECONDS - started) % 30 == 0 )); then
      echo "LONGHORN_V2_DR_TARGET_TRANSPORT_HEARTBEAT elapsed=$((SECONDS-started))"
    fi
    sleep 2
  done
  echo LONGHORN_V2_DR_TARGET_TRANSPORT_LIFETIME_PASS seconds=120
  exit 0
fi
phase=core-artifacts
"$PY" "$HELPER" supervise --relay-pid "$relay_pid" --step download
phase=core-setup
"$PY" "$HELPER" supervise --relay-pid "$relay_pid" --step setup
kubectl wait --for=condition=Ready nodes --all --timeout=10m
"$PY" "$HELPER" core-identity
phase=production-smoke
"$PY" "$HELPER" smoke
