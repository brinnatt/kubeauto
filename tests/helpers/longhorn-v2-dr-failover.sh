#!/usr/bin/env bash
# Planned handoff; source proof before activation and reverse proof before return writes.
set -Eeuo pipefail
BASE=/usr/local/kubeauto
cd "$BASE"
PY="$BASE/.venv/bin/python"
SOURCE="$BASE/tests/helpers/longhorn-v2-install.py"
TARGET="$BASE/tests/helpers/longhorn-v2-dr-target.py"
DR="$BASE/tests/helpers/longhorn-v2-dr-failover.py"
export PYTHONPATH="$BASE" PATH="$BASE/kube-bin:$BASE/extra-bin:$PATH"
export KUBECONFIG="$BASE/clusters/longhorn-v2-dr-target/kubectl.kubeconfig"
[[ "${LONGHORN_PARENT_LEASE_HELD:-no}" == yes ]] || exit 1
mode="${1:-}"
case "$mode" in
  v2-dr-activation-qualification) stage=activation ;;
  v2-dr-failback-qualification) stage=failback ;;
  *) exit 2 ;;
esac
marker="LONGHORN_${mode^^}"
marker="${marker//-/_}"
# Failback is a later ladder level. Validate its unchanged activation behavior
# before transport setup or fixture cleanup; this shell adds only this guard.
if [[ "$stage" == failback ]]; then
  "$PY" - "$BASE" <<'PY_ACTIVATION'
import hashlib,json,sys
from pathlib import Path
root=Path(sys.argv[1])
import importlib.util
spec=importlib.util.spec_from_file_location('longhorn_full_material',root/'tests/helpers/longhorn-full-material.py')
material=importlib.util.module_from_spec(spec);spec.loader.exec_module(material)
e=json.loads((material.path(root,'tests/evidence/longhorn-v2-dr-activation-qualification-20261021.json')).read_text())
standby=json.loads((material.path(root,'tests/evidence/longhorn-v2-dr-standby-qualification-20261021.json')).read_text())
if (e['status']!='pass' or e['rc']!=0 or e['failure_markers']!=0 or
        e['baseline_identity']!=standby['baseline_identity'] or
        e['activated_business'].get('source_fenced') is not True or
        e['remote_input_verification'].get('mismatches')!=0 or
        e['remote_input_verification'].get('matching_files')!=len(e['frozen_inputs'])):
    raise ValueError('DR failback requires current completed activation qualification')
for name in ('common/constants.py','controller/cluster/cli.py','service/cluster/manager.py',
             'roles/longhorn/files/longhorn.py','roles/longhorn/files/block-check.py',
             'tests/helpers/longhorn-lab.py','tests/helpers/longhorn-v2-lab.py',
             'tests/helpers/longhorn-backups.py','tests/helpers/longhorn-v2-backups.py',
             'tests/helpers/longhorn-v2-install.py','tests/helpers/longhorn-v2-dr-target.py',
             'tests/helpers/longhorn-v2-dr-target-install.py','tests/helpers/longhorn-v2-dr-store.py',
             'tests/helpers/longhorn-v2-disaster.py','tests/helpers/longhorn-v2-disaster.sh',
             'tests/helpers/longhorn-v2-dr-failover.py'):
    if e['frozen_inputs'].get(name)!=hashlib.sha256((root/name).read_bytes()).hexdigest():
        raise ValueError('DR activation dependency changed: '+name)
if not all(e['independent_readback'].get(key) is True for key in
        ('backend_container_removed','server_directory_removed','api_console_tunnel_not_listening','target_consumer_removed')):
    raise ValueError('DR activation cleanup was not independently verified')
if set(e['independent_readback']['clusters'])!={'source','target'}:
    raise ValueError('DR activation lacks independent readback for both clusters')
if any(c.get(key)!=0 for c in e['independent_readback']['clusters'].values()
       for key in ('volumes','backup_resources','backup_fixture_credentials','relays')):
    raise ValueError('DR activation left consumers or transport residue')
print('LONGHORN_V2_DR_ACTIVATION_QUALIFICATION_CURRENT',flush=True)
PY_ACTIVATION
fi
source_before="$("$PY" "$TARGET" source-boundary)"
target_before="$("$PY" "$BASE/tests/helpers/longhorn-v2-dr-store.py" target-baseline)"
printf '%s\n%s\n' "$source_before" "$target_before"
work="$(mktemp -d /var/tmp/kubeauto-longhorn-v2-dr-handoff.XXXXXX)"
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
      kubectl --kubeconfig="$BASE/clusters/$cluster/kubectl.kubeconfig" --request-timeout=5s describe pods -A > "$work/$cluster-pod-descriptions.txt" 2>&1 || true
      kubectl --kubeconfig="$BASE/clusters/$cluster/kubectl.kubeconfig" --request-timeout=5s -n longhorn-system get volumes.longhorn.io,engines.longhorn.io,enginefrontends.longhorn.io,replicas.longhorn.io,volumeattachments.longhorn.io,backups.longhorn.io,backupvolumes.longhorn.io,backuptargets.longhorn.io -o json > "$work/$cluster-native-state.json" 2>&1 || true
      kubectl --kubeconfig="$BASE/clusters/$cluster/kubectl.kubeconfig" --request-timeout=5s -n longhorn-system logs -l app=longhorn-manager --all-containers --prefix --tail=100 > "$work/$cluster-manager.log" 2>&1 || true
      for input in config.yml yml/longhorn/input.json longhorn-v2-dr.json longhorn-install-business.json longhorn-dr-failback-business.json; do
        if [[ -f "$BASE/clusters/$cluster/$input" && ! -L "$BASE/clusters/$cluster/$input" ]]; then
          cp -- "$BASE/clusters/$cluster/$input" "$work/$cluster-${input//\//_}" || true
        fi
      done
    done
  fi
  # Consumer recovery is first. Its failure retains the shared backend.
  "$PY" "$DR" cleanup || cleanup_rc=$?
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
    target_after="$("$PY" "$BASE/tests/helpers/longhorn-v2-dr-store.py" target-baseline)" || cleanup_rc=$?
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
    echo "${marker}_SCOPED_CLEAN_PASS"
    echo "${marker}_PASS"
  fi
  exit "$rc"
}
trap store_exit EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
phase=transport
"$PY" "$DR" qualification-current
"$PY" "$BASE/tests/helpers/longhorn-v2-core.py" verify-transport
"$PY" "$TARGET" qualification-current
"$PY" "$SOURCE" relay-ensure
"$PY" "$SOURCE" relay-maintain &
source_pid=$!
"$PY" "$TARGET" relay-ensure
"$PY" "$TARGET" relay-maintain &
target_pid=$!
phase="$stage"
"$PY" "$DR" qualify --stage "$stage" --source-relay-pid "$source_pid" --target-relay-pid "$target_pid" | tee "$work/handoff.log"
for expected in \
  'LONGHORN_V2_DR_WORKFLOW_UNFENCED_ACTIVATION_REFUSED native_api_not_called=true' \
  'LONGHORN_V2_DR_CACHE_RETIRED side=target external_backups=preserved' \
  'LONGHORN_V2_DR_CACHE_RETIRED side=source external_backups=preserved' \
  LONGHORN_V2_DR_FAILOVER_FIXTURES_CLEAN_PASS \
  "LONGHORN_V2_DR_${stage^^}_BUSINESS_PASS"; do
  grep -Fxq "$expected" "$work/handoff.log"
done
grep -q '^LONGHORN_V2_DR_SOURCE_FENCED ' "$work/handoff.log"
grep -q '^LONGHORN_V2_DR_ACTIVATED_BUSINESS_VERIFIED ' "$work/handoff.log"
if [[ "$stage" == failback ]]; then
  grep -q '^LONGHORN_V2_DR_RETURN_BACKUP ' "$work/handoff.log"
  grep -q '^LONGHORN_V2_DR_RECOVERY_FENCED ' "$work/handoff.log"
  grep -q '^LONGHORN_V2_DR_FAILBACK_BUSINESS_VERIFIED ' "$work/handoff.log"
fi
