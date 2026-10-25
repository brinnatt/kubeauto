#!/usr/bin/env bash
# LONGHORN-49: independent three-replica qualification before dependency lifecycle.
set -Eeuo pipefail
umask 077
BASE=/usr/local/kubeauto
cd "$BASE"
[[ "${LONGHORN_PARENT_LEASE_HELD:-no}" == yes ]]
acceptance_mode="${1:-}"
case "$acceptance_mode" in v2-linked-acceptance|v2-linked-failure-acceptance) ;; *) exit 2;; esac
terminal_marker="LONGHORN_${acceptance_mode^^}"; terminal_marker="${terminal_marker//-/_}"
work="$(mktemp -d /var/tmp/kubeauto-longhorn-linked-acceptance.XXXXXX)"
chmod 0700 "$work"
echo "LONGHORN_V2_LINKED_ACCEPTANCE_DURABLE_DIRECTORY path=$work"
"$BASE/.venv/bin/python" "$BASE/tests/helpers/longhorn-manager-adopt.py"
"$BASE/.venv/bin/python" "$BASE/tests/helpers/longhorn-v2-clone.py" snapshot --phase before
for mode in linked-clone linked-lifecycle incomplete-clone; do
  marker="LONGHORN_V2_${mode^^}_QUALIFICATION"; marker="${marker//-/_}"
  echo "LONGHORN_V2_LINKED_ACCEPTANCE_STAGE_START stage=$mode"
  bash "$BASE/tests/helpers/run-durable-gate.sh" "$work/$mode" "${marker}_EXIT" \
    bash "$BASE/tests/helpers/longhorn-live.sh" "v2-$mode-qualification" 2>&1 | tee "$work/$mode.log"
  "$BASE/.venv/bin/python" "$BASE/tests/helpers/longhorn-parallel.py" verify-branch --directory "$work" --branch "$mode"
  "$BASE/.venv/bin/python" "$BASE/tests/helpers/longhorn-v2-clone.py" stage-receipt --directory "$work" --stage "$mode"
  echo "LONGHORN_V2_LINKED_ACCEPTANCE_STAGE_PASS stage=$mode durable_rc=0 finalized=0"
done
"$BASE/.venv/bin/python" "$BASE/tests/helpers/longhorn-v2-clone.py" snapshot --phase after
if [[ "$acceptance_mode" == v2-linked-failure-acceptance ]]; then
  bash "$BASE/tests/helpers/run-durable-gate.sh" "$work/failure" LONGHORN_V2_FAILURE_EXIT \
    bash "$BASE/tests/helpers/longhorn-v2-failure.sh" v2-failure 2>&1 | tee "$work/failure.log"
  "$BASE/.venv/bin/python" "$BASE/tests/helpers/longhorn-parallel.py" verify-branch --directory "$work" --branch failure
fi
echo "${terminal_marker}_SCOPED_CLEAN_PASS"
echo "${terminal_marker}_PASS"
