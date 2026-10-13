#!/usr/bin/env bash
# One clean Redis regression; every stage keeps durable, launch-bound evidence.
set -Eeuo pipefail
BASE="${KUBEAUTO_BASE:-/usr/local/kubeauto}"
PY="${REDIS_PYTHON:-$BASE/.venv/bin/python}"
MODE="${1:---full}"
[[ "$MODE" == --preflight || "$MODE" == --full || "$MODE" == --clean-verify ]] || exit 2
"$PY" - "$BASE" <<'PY'
from pathlib import Path
import importlib.util,json,sys,yaml
base=Path(sys.argv[1])
matrix=yaml.safe_load((base/'tests/redis-test-matrix.yaml').read_text())
cases={c['id']:c for c in matrix['cases']}
expected={f'REDIS-{i:02d}' for i in range(1,25)}
assert set(cases)==expected
pending=[key for key in sorted(expected-{'REDIS-24'}) if cases[key]['status']!='pass']
if pending:
 raise SystemExit('REDIS_PREFLIGHT_FAIL class=test-gate reason=focused-qualification-pending cases='+','.join(pending))
spec=importlib.util.spec_from_file_location('redis_full_artifacts',base/'tests/helpers/redis-artifacts.py')
artifacts=importlib.util.module_from_spec(spec);spec.loader.exec_module(artifacts)
for name,pins in [('redis-images-20261012.json',artifacts.IMAGES),
                  ('redis-topology-operator-images-20261013.json',artifacts.PROTECTED_IMAGES),
                  ('redis-state-refresh-images-20261013.json',artifacts.CLUSTER_IMAGES)]:
 artifacts.publication_records(json.loads((base/'tests/evidence'/name).read_text()),pins)
for name in ('redis-images-20261012.json','redis-topology-operator-images-20261013.json',
             'redis-state-refresh-images-20261013.json','redis-storage-images-20261012.json',
             'redis-observability-images-20261013.json'):
 doc=json.loads((base/'tests/evidence'/name).read_text())
 assert doc['workflow_conclusion']=='success' and doc['durable_rc']==0 and doc['failure_markers']==0
print('REDIS_FULL_PREFLIGHT_PASS focused-cases=23 artifacts=qualified')
PY
[[ "$MODE" != --preflight ]] || exit 0
lease="${REDIS_LAB_LEASE_FILE:-/var/lock/kubeauto-redis-core-pool.lock}"
exec 9>"$lease"
flock -n 9 || { echo 'REDIS_PREFLIGHT_FAIL class=environment reason=core-pool-leased' >&2; exit 1; }
export REDIS_PARENT_LEASE_HELD=yes KUBEAUTO_BASE="$BASE"
export PATH="$BASE/kube-bin:$BASE/extra-bin:$PATH" PYTHONPATH="$BASE"
run="$(mktemp -d "${REDIS_FULL_EVIDENCE_ROOT:-/var/tmp}/kubeauto-redis-full.XXXXXX")"
chmod 0700 "$run"
echo "REDIS_FULL_EVIDENCE_DIRECTORY path=$run"
on_exit() {
  local rc=$? cleanup_rc=0
  trap - EXIT
  if [[ "$rc" -ne 0 ]]; then
    echo "REDIS_FULL_FIRST_FAILURE rc=$rc evidence=$run" >&2
    bash "$BASE/tests/helpers/redis-lab-clean-gate.sh" --clean || cleanup_rc=$?
    echo "REDIS_FULL_FAILURE_CLEANUP rc=$cleanup_rc evidence=$run" >&2
  fi
  exit "$rc"
}
trap on_exit EXIT
stage() {
  local module="$1" marker="$2"
  shift 2
  local directory="$run/$module"
  mkdir "$directory"
  local source_module="$module"
  [[ "$module" != final-idempotence ]] || source_module=idempotence
  "$PY" "$BASE/tests/helpers/redis-evidence.py" --capture "$source_module" --root "$BASE" > "$directory/launch-source.json"
  echo "REDIS_FULL_STAGE_BEGIN module=$module"
  bash "$BASE/tests/helpers/run-durable-gate.sh" "$directory/durable" REDIS_GATE_EXIT "$@" 2>&1 | tee "$directory/first-run.log"
  [[ "$(cat "$directory/durable.exit")" == 0 && "$(cat "$directory/durable.finalized")" == 0 ]]
  grep -Eq "^${marker}( |$)" "$directory/first-run.log"
  if [[ "$marker" == 'REDIS_FOCUSED_MODULE_PASS module='* ]]; then
    grep -Eq "^REDIS_FOCUSED_SCOPED_CLEAN_PASS module=${marker#*module=}( |$)" "$directory/first-run.log"
  fi
  if grep -Eq '\[FAIL\]|REDIS_[A-Z_]*(FAIL|FIRST_FAILURE)( |$)' "$directory/first-run.log"; then
    return 1
  fi
  echo "REDIS_FULL_STAGE_PASS module=$module durable-rc=0 failure-markers=0"
}
focused() {
  stage "$1" "REDIS_FOCUSED_MODULE_PASS module=$1" \
    bash "$BASE/tests/helpers/redis-focused-gate.sh" "$1"
}
if [[ "$MODE" == --clean-verify ]]; then
  stage clean-before-verify REDIS_LAB_CLEAN_VERIFY_PASS bash "$BASE/tests/helpers/redis-lab-clean-gate.sh" --verify
  echo REDIS_FULL_LEASE_VERIFY_PASS
  exit
fi
stage clean-before REDIS_LAB_CLEANUP_PASS bash "$BASE/tests/helpers/redis-lab-clean-gate.sh" --clean
stage clean-before-verify REDIS_LAB_CLEAN_VERIFY_PASS bash "$BASE/tests/helpers/redis-lab-clean-gate.sh" --verify
stage install REDIS_INSTALL_MODULE_PASS bash "$BASE/tests/helpers/redis-install-gate.sh" --fresh
for module in platform sentinel-data sentinel-security sentinel-failure sentinel-quorum \
              persistence backup capacity observability performance cleanup-qualification \
              idempotence password-rotation tls-rotation upgrade-prepare upgrade-sentinel; do
  focused "$module"
done
for module in cluster-install cluster-data cluster-snapshot cluster-failure cluster-rebalance \
              maintenance-rolling maintenance-drain crd-upgrade upgrade-cluster idempotence stability; do
  # The final idempotence is a separate record from the pre-maintenance one.
  if [[ "$module" == idempotence ]]; then
    stage final-idempotence 'REDIS_FOCUSED_MODULE_PASS module=idempotence' \
      bash "$BASE/tests/helpers/redis-focused-gate.sh" idempotence
  else
    focused "$module"
  fi
done
stage clean-after REDIS_LAB_CLEANUP_PASS bash "$BASE/tests/helpers/redis-lab-clean-gate.sh" --clean
stage clean-after-verify REDIS_LAB_CLEAN_VERIFY_PASS bash "$BASE/tests/helpers/redis-lab-clean-gate.sh" --verify
echo REDIS_FULL_GATE_PASS
echo REDIS_DELIVERY_BRANCH_PASS
