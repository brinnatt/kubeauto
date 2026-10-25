#!/usr/bin/env bash
# Independently qualified external store before V2 full/incremental recovery.
set -Eeuo pipefail
BASE=/usr/local/kubeauto
cd "$BASE"
PY="$BASE/.venv/bin/python"
CORE="$BASE/tests/helpers/longhorn-v2-core.py"
INSTALL="$BASE/tests/helpers/longhorn-v2-install.py"
BACKUPS="$BASE/tests/helpers/longhorn-v2-backups.py"
export PYTHONPATH="$BASE" PATH="$BASE/kube-bin:$BASE/extra-bin:$PATH"
export KUBECONFIG="$BASE/clusters/longhorn-v2-gate/kubectl.kubeconfig"
[[ "${LONGHORN_PARENT_LEASE_HELD:-no}" == yes ]] || exit 1
mode="${1:-}"
case "$mode" in v2-backup-store-qualification|v2-backup-qualification|v2-backup-refusal-qualification) ;; *) exit 2;; esac
marker="LONGHORN_${mode^^}"
marker="${marker//-/_}"
"$PY" "$CORE" verify-transport
if [[ "$mode" != v2-backup-store-qualification ]]; then "$PY" "$BACKUPS" qualification-current; fi
baseline="$("$PY" "$BACKUPS" baseline)"
printf '%s\n' "$baseline"
work="$(mktemp -d /var/tmp/kubeauto-longhorn-v2-backups.XXXXXX)"
chmod 0700 "$work"
phase=transport
relay_pid=
backups_exit() {
  local rc=$? cleanup_rc=0 current
  trap - EXIT INT TERM
  if [[ "$rc" != 0 ]]; then
    echo "LONGHORN_FIRST_FAILURE phase=$phase rc=$rc diagnostics=$work" >&2
    printf 'rc=%s phase=%s\n' "$rc" "$phase" > "$work/first-failure.status"
    kubectl --request-timeout=5s get nodes -o yaml > "$work/nodes.yaml" 2>&1 || true
    kubectl --request-timeout=5s get pods -A -o wide > "$work/pods.txt" 2>&1 || true
    kubectl --request-timeout=5s get events -A --sort-by=.lastTimestamp > "$work/events.txt" 2>&1 || true
  fi
  "$PY" "$BACKUPS" cleanup --relay-pid "${relay_pid:-0}" || cleanup_rc=$?
  if [[ "$cleanup_rc" == 0 ]]; then
    current="$("$PY" "$BACKUPS" baseline)" || cleanup_rc=$?
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
    echo "LONGHORN_CLEANUP_FAIL phase=$phase rc=$cleanup_rc journal=retained" >&2
    [[ "$rc" != 0 ]] || rc=$cleanup_rc
  elif [[ "$rc" == 0 ]]; then
    rm -rf -- "$work"
    echo "${marker}_SCOPED_CLEAN_PASS"
    echo "${marker}_PASS"
  fi
  exit "$rc"
}
trap backups_exit EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
"$PY" "$INSTALL" relay-ensure
"$PY" "$INSTALL" relay-maintain &
relay_pid=$!
phase=backup-store
action=qualify
if [[ "$mode" == v2-backup-qualification ]]; then action=business; phase=backup; fi
if [[ "$mode" == v2-backup-refusal-qualification ]]; then action=refusal; phase=backup-refusal; fi
"$PY" "$BACKUPS" "$action" --relay-pid "$relay_pid" | tee "$work/business.log"
grep -Fxq 'LONGHORN_BACKUP_STORE_QUALIFIED clients=3 tls=verified object_readback=exact' "$work/business.log"
grep -Fxq LONGHORN_BACKUP_TRANSPORT_CLEAN_PASS "$work/business.log"
grep -Fxq LONGHORN_BACKUP_STORE_CLEAN_PASS "$work/business.log"
if [[ "$mode" == v2-backup-refusal-qualification ]]; then
  "$PY" - "$work/business.log" <<'PY'
import json, re, sys
from pathlib import Path
log = Path(sys.argv[1]).read_text()
refusals = re.findall(r'^LONGHORN_BACKUP_REFUSAL_VERIFIED kind=(\S+) available=false request_created=false backup_objects=0$', log, re.M)
restored = re.findall(r'^LONGHORN_BACKUP_REFUSAL_CREDENTIAL_RESTORED kind=(\S+)$', log, re.M)
assert refusals == restored == ['credential', 'ca', 'endpoint']
rows = re.findall(r'^LONGHORN_BACKUP_OBJECT_RECEIPT mode=(\S+) (.+)$', log, re.M)
assert len(rows) == 1 and rows[0][0] == 'full'
r = json.loads(rows[0][1])
assert r['incremental'] is False and r['size'] == 2*1024**3 and r['objects']
assert all(o['size'] > 0 and re.fullmatch('[a-f0-9]{64}', o['sha256']) for o in r['objects'].values())
boundaries = re.findall(r'^LONGHORN_BACKUP_RESTORE_BOUNDARY_VERIFIED mode=full backup_sha256=([a-f0-9]{64}) source_sha256=([a-f0-9]{64}) independent_volume=(\S+)$', log, re.M)
assert len(boundaries) == 1 and boundaries[0][0] != boundaries[0][1] and boundaries[0][2] != r['volume']
remounts = re.findall(r'^LONGHORN_FSYNC_REMOUNT_VERIFIED bytes=(\d+) sha256=([a-f0-9]{64}) replica_nodes=(\S+)$', log, re.M)
assert len(remounts) == 1 and int(remounts[0][0]) > 0 and remounts[0][1] == boundaries[0][1]
assert len(set(remounts[0][2].split(','))) == 3
PY
  grep -Fxq LONGHORN_BACKUP_PRODUCT_FIXTURE_RESTORED "$work/business.log"
  grep -Fxq LONGHORN_BUSINESS_FIXTURES_CLEAN_PASS "$work/business.log"
  grep -Fxq LONGHORN_BACKUP_REFUSAL_BUSINESS_PASS "$work/business.log"
fi
if [[ "$mode" == v2-backup-qualification ]]; then
  "$PY" - "$work/business.log" <<'PY'
import json, re, sys
from pathlib import Path
log = Path(sys.argv[1]).read_text()
rows = re.findall(r'^LONGHORN_BACKUP_OBJECT_RECEIPT mode=(full|incremental) (.+)$', log, re.M)
assert [mode for mode, _ in rows] == ['full', 'incremental']
receipts = {mode: json.loads(raw) for mode, raw in rows}
assert len({r['volume'] for r in receipts.values()}) == 1
assert len({r['backup'] for r in receipts.values()}) == 2
for mode, r in receipts.items():
    assert r['incremental'] is (mode == 'incremental') and r['size'] == 2*1024**3
    assert r['newly_uploaded_bytes'] > 0 and r['objects']
    assert all(o['size'] > 0 and re.fullmatch('[a-f0-9]{64}', o['sha256']) for o in r['objects'].values())
boundaries = re.findall(r'^LONGHORN_BACKUP_RESTORE_BOUNDARY_VERIFIED mode=(full|incremental) backup_sha256=([a-f0-9]{64}) source_sha256=([a-f0-9]{64}) independent_volume=(\S+)$', log, re.M)
assert [r[0] for r in boundaries] == ['full', 'incremental']
assert len({r[1] for r in boundaries}) == len({r[3] for r in boundaries}) == 2
assert len({r[2] for r in boundaries}) == 1 and all(r[1] != r[2] for r in boundaries)
assert all(r[3] != receipts[r[0]]['volume'] for r in boundaries)
remounts = re.findall(r'^LONGHORN_FSYNC_REMOUNT_VERIFIED bytes=(\d+) sha256=([a-f0-9]{64}) replica_nodes=(\S+)$', log, re.M)
assert len(remounts) == 1 and int(remounts[0][0]) > 0 and remounts[0][1] == boundaries[0][2]
assert len(set(remounts[0][2].split(','))) == 3
PY
  grep -Fxq LONGHORN_BACKUP_PRODUCT_FIXTURE_RESTORED "$work/business.log"
  grep -Fxq LONGHORN_BUSINESS_FIXTURES_CLEAN_PASS "$work/business.log"
  grep -Fxq LONGHORN_BACKUP_BUSINESS_PASS "$work/business.log"
fi
