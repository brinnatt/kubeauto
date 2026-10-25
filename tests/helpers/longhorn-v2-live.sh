#!/usr/bin/env bash
# Invoked by longhorn-live.sh after its inherited lease checks.
set -Eeuo pipefail
BASE=/usr/local/kubeauto
cd "$BASE"
PY="$BASE/.venv/bin/python"
DIR="$BASE/clusters/longhorn-v2-gate"
HELPER="$BASE/tests/helpers/longhorn-v2-lab.py"
[[ "${LONGHORN_PARENT_LEASE_HELD:-no}" == yes ]] || exit 1
[[ "${1:?mode required}" == v2-registry-qualification ]] || exit 2
# host-inputs validates inherited file descriptors again, before any mutation.
"$PY" "$HELPER" host-inputs
[[ ! -e "$DIR" && ! -L "$DIR" ]] || {
  echo 'LONGHORN_PREFLIGHT_FAIL class=environment reason=existing-v2-lab-directory' >&2; exit 1;
}
curl --fail --silent --show-error --noproxy '*' --max-time 5 http://127.0.0.1:5000/v2/ >/dev/null
phase=configure
owned=no
v2_registry_exit() {
  local rc=$? cleanup_rc=0
  trap - EXIT INT TERM
  if [[ "$rc" != 0 ]]; then echo "LONGHORN_FIRST_FAILURE phase=$phase rc=$rc" >&2; fi
  if [[ "$owned" == yes ]]; then
    "$PY" "$HELPER" relay-clean || cleanup_rc=$?
    if [[ "$cleanup_rc" == 0 ]]; then
      "$PY" "$HELPER" qualification-clean || cleanup_rc=$?
    fi
  fi
  "$PY" "$HELPER" host-inputs || cleanup_rc=$?
  if [[ "$cleanup_rc" != 0 ]]; then
    echo "LONGHORN_CLEANUP_FAIL phase=$phase rc=$cleanup_rc" >&2
    [[ "$rc" != 0 ]] || rc=$cleanup_rc
  elif [[ "$rc" == 0 ]]; then
    echo LONGHORN_V2_REGISTRY_QUALIFICATION_SCOPED_CLEAN_PASS
    echo LONGHORN_V2_REGISTRY_QUALIFICATION_PASS
  fi
  exit "$rc"
}
trap v2_registry_exit EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
"$PY" "$BASE/kubecli.py" new longhorn-v2-gate </dev/null
owned=yes
"$PY" "$HELPER" configure
phase=registry-qualification
"$PY" "$HELPER" relay-qualification
phase=registry-lifetime
"$PY" "$HELPER" relay-ensure
for sample in {1..8}; do
  sleep 15
  "$PY" "$HELPER" relay-ensure
  echo "LONGHORN_V2_REGISTRY_LIFETIME_SAMPLE sample=$sample/8"
done
echo LONGHORN_V2_REGISTRY_LIFETIME_PASS
