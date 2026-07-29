#!/bin/bash
# Persistent 130 regression preflight: restore delivery components, then G7.
set -euo pipefail

STATE_PREFIX=/tmp/kubeauto-regression-jumper
export PYTHONPATH=/usr/local/kubeauto
export PATH="/usr/local/bin:/usr/bin:/bin:$PATH"

rm -f "${STATE_PREFIX}.exit"
printf '%s\n' "$$" > "${STATE_PREFIX}.pid"
finish() {
  local rc=$?
  trap - EXIT
  printf '%s\n' "$rc" > "${STATE_PREFIX}.exit"
  exit "$rc"
}
trap finish EXIT

# -a installs Ansible only; -D is the documented all-components delivery path
# and is required before the mirror/bootstrap stage needs a Docker daemon.
kubecli download -D </dev/null
BOOTSTRAP_MODE=defaults bash /usr/local/kubeauto/tests/helpers/bootstrap-brinnatt-mirrors.sh
kubecli download -X </dev/null
set +e
bash /tmp/regression-jumper.sh
rc=$?
set -e
echo "REGRESSION_JUMPER_EXIT rc=$rc"
exit "$rc"
