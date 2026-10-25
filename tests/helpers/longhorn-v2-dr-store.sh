#!/usr/bin/env bash
# Shared-store prerequisite only; no standby, activation or DR business PASS.
set -Eeuo pipefail
BASE=/usr/local/kubeauto
cd "$BASE"
PY="$BASE/.venv/bin/python"
SOURCE="$BASE/tests/helpers/longhorn-v2-install.py"
TARGET="$BASE/tests/helpers/longhorn-v2-dr-target.py"
STORE="$BASE/tests/helpers/longhorn-v2-dr-store.py"
export PYTHONPATH="$BASE" PATH="$BASE/kube-bin:$BASE/extra-bin:$PATH"
export KUBECONFIG="$BASE/clusters/longhorn-v2-dr-target/kubectl.kubeconfig"
[[ "${LONGHORN_PARENT_LEASE_HELD:-no}" == yes ]] || exit 1
[[ "${1:-}" == v2-dr-store-qualification ]] || exit 2
source_before="$("$PY" "$TARGET" source-boundary)"
target_before="$("$PY" "$STORE" target-baseline)"
printf '%s\n%s\n' "$source_before" "$target_before"
work="$(mktemp -d /var/tmp/kubeauto-longhorn-v2-dr-store.XXXXXX)"
chmod 0700 "$work"
phase=preflight
source_pid=
target_pid=
store_exit() {
  local rc=$? cleanup_rc=0 source_after target_after cluster pid
  trap - EXIT INT TERM
  if [[ "$rc" != 0 ]]; then
    echo "LONGHORN_FIRST_FAILURE phase=$phase rc=$rc diagnostics=$work" >&2
    printf 'rc=%s phase=%s\n' "$rc" "$phase" > "$work/first-failure.status"
    for cluster in longhorn-v2-gate longhorn-v2-dr-target; do
      kubectl --kubeconfig="$BASE/clusters/$cluster/kubectl.kubeconfig" --request-timeout=5s get pods -A -o wide > "$work/$cluster-pods.txt" 2>&1 || true
      kubectl --kubeconfig="$BASE/clusters/$cluster/kubectl.kubeconfig" --request-timeout=5s get events -A --sort-by=.lastTimestamp > "$work/$cluster-events.txt" 2>&1 || true
    done
  fi
  # Consumer recovery is first. Its failure retains the shared backend.
  "$PY" "$STORE" cleanup || cleanup_rc=$?
  for pid in "$source_pid" "$target_pid"; do
    [[ -n "$pid" ]] || continue
    if [[ "$rc" == 0 ]] && ! kill -0 "$pid" 2>/dev/null; then rc=1; fi
    kill -TERM "$pid" 2>/dev/null || true
    wait "$pid" || { [[ "$rc" != 0 ]] || rc=1; }
  done
  "$PY" "$SOURCE" relay-clean || cleanup_rc=$?
  "$PY" "$TARGET" relay-clean || cleanup_rc=$?
  if [[ "$cleanup_rc" == 0 ]]; then
    source_after="$("$PY" "$TARGET" source-boundary)" || cleanup_rc=$?
    target_after="$("$PY" "$STORE" target-baseline)" || cleanup_rc=$?
    printf '%s\n%s\n' "$source_after" "$target_after"
    if [[ "$(grep '^LONGHORN_V2_DR_SOURCE_BOUNDARY ' <<<"$source_before")" != \
          "$(grep '^LONGHORN_V2_DR_SOURCE_BOUNDARY ' <<<"$source_after")" || \
          "$(grep '^LONGHORN_V2_DR_TARGET_STORE_BASELINE ' <<<"$target_before")" != \
          "$(grep '^LONGHORN_V2_DR_TARGET_STORE_BASELINE ' <<<"$target_after")" ]]; then cleanup_rc=1; fi
  fi
  if [[ "$cleanup_rc" != 0 ]]; then
    echo "LONGHORN_CLEANUP_FAIL phase=$phase rc=$cleanup_rc journal=retained" >&2
    [[ "$rc" != 0 ]] || rc=$cleanup_rc
  elif [[ "$rc" == 0 ]]; then
    rm -rf -- "$work"
    echo LONGHORN_V2_DR_STORE_QUALIFICATION_SCOPED_CLEAN_PASS
    echo LONGHORN_V2_DR_STORE_QUALIFICATION_PASS
  fi
  exit "$rc"
}
trap store_exit EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
phase=transport
"$PY" "$BASE/tests/helpers/longhorn-v2-core.py" verify-transport
"$PY" "$TARGET" qualification-current
"$PY" "$SOURCE" relay-ensure
"$PY" "$SOURCE" relay-maintain &
source_pid=$!
"$PY" "$TARGET" relay-ensure
"$PY" "$TARGET" relay-maintain &
target_pid=$!
phase=shared-store
"$PY" "$STORE" qualify --target-relay-pid "$target_pid" | tee "$work/store.log"
for marker in \
  'LONGHORN_BACKUP_STORE_QUALIFIED clients=3 tls=verified object_readback=exact' \
  'LONGHORN_V2_DR_SOURCE_BACKUP_TRANSPORT_ABSENT shared_server=retained' \
  'LONGHORN_V2_DR_TARGET_DIRECT_TLS_PASS valid_ca=true wrong_ca_refused=true source_transport=false' \
  'LONGHORN_V2_DR_TARGET_SHARED_STORE_VERIFIED target_available=true independent_endpoint=true' \
  'LONGHORN_V2_DR_TARGET_STORE_CONSUMER_CLEAN_PASS shared_server=retained' \
  LONGHORN_BACKUP_STORE_CLEAN_PASS; do
  grep -Fxq "$marker" "$work/store.log"
done
