#!/usr/bin/env bash
# Nested durable retirement: publish host-release receipts only after rc fences.
set -Eeuo pipefail
umask 077
BASE=/usr/local/kubeauto
cd "$BASE"
PY="$BASE/.venv/bin/python"
HELPER="$BASE/tests/helpers/longhorn-v2-cleanup.py"
export PYTHONPATH="$BASE" PATH="$BASE/kube-bin:$BASE/extra-bin:$PATH"
[[ "${LONGHORN_PARENT_LEASE_HELD:-no}" == yes ]] || exit 1
mode="${1:?mode required}"
case "$mode" in
  v2-cleanup|v2-cleanup-recovery) scope=source ;;
  v2-cleanup-target|v2-cleanup-target-recovery) scope=target ;;
  _inner|_recover) scope="${2:?scope required}" ;;
  *) exit 2 ;;
esac
case "$scope" in source|target) ;; *) exit 2;; esac
if [[ "$mode" != _inner && "$mode" != _recover ]]; then
  if [[ "$mode" == *-recovery ]]; then
    journal="${LONGHORN_V2_RETIREMENT_JOURNAL:?explicit original journal required}"
    "$PY" "$HELPER" recovery-plan --scope "$scope" --journal "$journal" >/dev/null
    original="${journal%/retirement.json}"
    work="$(mktemp -d "$original/recovery.XXXXXX")"
    inner=_recover
  else
    work="$(mktemp -d /var/tmp/kubeauto-longhorn-v2-retirement.XXXXXX)"
    original="$work"
    inner=_inner
  fi
  chmod 0700 "$work"
  echo "LONGHORN_V2_RETIREMENT_ATTEMPT work=$work original=$original"
  set +e
  bash "$BASE/tests/helpers/run-durable-gate.sh" "$work/retire" LONGHORN_V2_RETIRE_EXIT \
    bash "$BASE/tests/helpers/longhorn-v2-cleanup.sh" "$inner" "$scope" "$work" "$original" 2>&1 | tee "$work/retire.log"
  rc=${PIPESTATUS[0]} tee_rc=${PIPESTATUS[1]}
  set -e
  [[ "$rc" == 0 && "$tee_rc" == 0 ]] || exit 1
  "$PY" "$HELPER" finalize --scope "$scope" --journal "$original/retirement.json" \
    --status-prefix "$work/retire" --log "$work/retire.log" --out "$work/release-proofs"
  if [[ "$scope" == source ]]; then echo LONGHORN_V2_CLEANUP_PASS; else echo LONGHORN_V2_TARGET_CLEANUP_PASS; fi
  exit 0
fi
work="${3:?attempt directory required}"
original="${4:?original directory required}"
[[ -d "$work" && ! -L "$work" ]] || exit 1
journal="$original/retirement.json"
resume_phase=
if [[ "$mode" == _recover ]]; then
  resume_phase="$("$PY" "$HELPER" recovery-plan --scope "$scope" --journal "$journal")"
fi
# This path uses an explicit KUBECONFIG. Native setup step 09 never checks
# out a context, and core wipe touches only leased remote nodes. Do not copy
# or remove the jumper's shared /root/.kube/config.
if [[ "$scope" == source ]]; then
  PROFILE="$BASE/tests/helpers/longhorn-v2-install.py"
  RELAY_PROFILE="$PROFILE"
  CORE_PROFILE="$BASE/tests/helpers/longhorn-v2-core.py"
  export KUBECONFIG="$BASE/clusters/longhorn-v2-gate/kubectl.kubeconfig"
else
  PROFILE="$BASE/tests/helpers/longhorn-v2-dr-target-install.py"
  RELAY_PROFILE="$BASE/tests/helpers/longhorn-v2-dr-target.py"
  CORE_PROFILE="$RELAY_PROFILE"
  export KUBECONFIG="$BASE/clusters/longhorn-v2-dr-target/kubectl.kubeconfig"
  source_before="$("$PY" "$RELAY_PROFILE" source-boundary)"
  if [[ "$mode" == _recover ]]; then
    [[ "$source_before" == "$(cat "$original/source-boundary")" ]] || exit 1
  else
    printf '%s\n' "$source_before" > "$original/source-boundary"
  fi
fi
relay_pid=
phase=preflight
retirement_exit() {
  local rc=$? cleanup_rc=0
  trap - EXIT INT TERM
  if [[ "$rc" != 0 ]]; then
    echo "LONGHORN_FIRST_FAILURE phase=$phase rc=$rc diagnostics=$work" >&2
    printf 'rc=%s phase=%s\n' "$rc" "$phase" > "$work/first-failure.status"
    kubectl --request-timeout=5s get nodes -o yaml > "$work/nodes.yaml" 2>&1 || true
    kubectl --request-timeout=5s get pods -A -o wide > "$work/pods.txt" 2>&1 || true
    kubectl --request-timeout=5s get events -A --sort-by=.lastTimestamp > "$work/events.txt" 2>&1 || true
  fi
  if [[ -n "$relay_pid" ]]; then
    kill -TERM "$relay_pid" 2>/dev/null || true
    wait "$relay_pid" || cleanup_rc=$?
    "$PY" "$CORE_PROFILE" relay-clean || cleanup_rc=$?
  fi
  if [[ "$cleanup_rc" != 0 ]]; then
    echo "LONGHORN_CLEANUP_FAIL phase=$phase rc=$cleanup_rc journal=retained" >&2
    [[ "$rc" != 0 ]] || rc=$cleanup_rc
  fi
  exit "$rc"
}
trap retirement_exit EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
if [[ "$mode" == _inner ]]; then
  "$PY" "$HELPER" prepare --scope "$scope" --journal "$journal"
fi
if [[ "$mode" == _inner || "$resume_phase" == prepared || "$resume_phase" == native-uninstall ]]; then
phase=transport
"$PY" "$RELAY_PROFILE" relay-ensure
"$PY" "$RELAY_PROFILE" relay-maintain &
relay_pid=$!
if [[ "$mode" == _inner && "$scope" == source ]]; then
  for fault in failure sigterm; do
    phase="checkpoint-$fault"
    set +e
    "$PY" "$HELPER" checkpoint --scope "$scope" --journal "$journal" --relay-pid "$relay_pid" --fault "$fault"
    observed=$?
    set -e
    expected=86
    [[ "$fault" != sigterm ]] || expected=143
    [[ "$observed" == "$expected" ]] || exit 1
    "$PY" "$HELPER" checkpoint-verify --scope "$scope" --journal "$journal" --fault "$fault"
  done
fi
phase=native-storage-retirement
storage_action=storage
[[ "$mode" != _recover ]] || storage_action=recover-storage
"$PY" "$HELPER" "$storage_action" --scope "$scope" --journal "$journal" --relay-pid "$relay_pid"
phase=relay-clean
kill -TERM "$relay_pid"
wait "$relay_pid"
relay_pid=
"$PY" "$CORE_PROFILE" relay-clean
elif [[ "$resume_phase" == storage-clean ]]; then
  "$PY" "$CORE_PROFILE" relay-clean
fi
phase=core-retirement
core_action=core
[[ "$mode" != _recover ]] || core_action=recover-core
"$PY" "$HELPER" "$core_action" --scope "$scope" --journal "$journal"
if [[ "$scope" == target ]]; then
  source_after="$("$PY" "$RELAY_PROFILE" source-boundary)"
  [[ "$source_before" == "$source_after" ]] || exit 1
  printf '%s\n' "$source_after"
  echo LONGHORN_V2_TARGET_SOURCE_PRESERVED_PASS
fi
