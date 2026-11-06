#!/usr/bin/env bash
# One supervised, leased module. A healthy source baseline can survive focused
# gates; complete delivery must run the final source/DR cleanup module.
set -Eeuo pipefail
BASE=/usr/local/kubeauto
PY="$BASE/.venv/bin/python"
LAB="$BASE/tests/helpers/elasticsearch-lab.py"
MODULE="${1:?module required}"
[[ "${ELASTICSEARCH_PARENT_LEASE_HELD:-}" == yes ]] || exit 1
case "$MODULE" in only|admission|registry-recover|bootstrap|bootstrap-resume|storage|csi-probe|standard|compact|business|lifecycle|security|security-identity|security-certificates|security-network|security-s3|security-certificate-restore|faults|restore-faults|maintenance|backup|backup-source|backup-refusal|backup-restore|backup-features|dr-bootstrap|dr|upgrade|upgrade-platform|coexistence|coexistence-isolation|coexistence-restores|logging|logging-cleanup|cleanup-probes|observability|soak|rally|performance|cleanup) ;; *) exit 2;; esac
export PYTHONPATH="$BASE" PYTHONUNBUFFERED=1
export PATH="$BASE/kube-bin:$BASE/extra-bin:$PATH"
work=$(mktemp -d /var/tmp/kubeauto-elasticsearch-module.XXXXXX)
chmod 0700 "$work"
context_existed=no
if [[ -e /root/.kube/config ]]; then
  [[ -f /root/.kube/config && ! -L /root/.kube/config ]] || exit 1
  cp -p /root/.kube/config "$work/control-context"
  context_existed=yes
fi
finish() {
  local rc=$? restore_rc=0
  trap - EXIT INT TERM
  if (( rc != 0 )); then
    printf 'ELASTICSEARCH_FIRST_FAILURE module=%s rc=%s diagnostics=%s\n' "$MODULE" "$rc" "$work" >&2
    printf '%s\n' "$rc" > "$work/first-failure.rc"
    "$PY" "$LAB" diagnostics > "$work/diagnostics.log" 2>&1 || true
    if compgen -G '/var/tmp/kubeauto-elasticsearch/fault-*.json' >/dev/null; then
      "$PY" "$LAB" restore-faults > "$work/fault-recovery.log" 2>&1 || \
        echo ELASTICSEARCH_FAULT_RECOVERY_FAIL >&2
    fi
  fi
  if [[ "$context_existed" == yes ]]; then
    install -d -m 0700 /root/.kube
    cp -p "$work/control-context" /root/.kube/config || restore_rc=$?
    cmp -s "$work/control-context" /root/.kube/config || restore_rc=1
    rm -f "$work/control-context"
  else
    rm -f /root/.kube/config || restore_rc=$?
  fi
  if (( restore_rc != 0 )); then
    echo ELASTICSEARCH_CONTROL_CONTEXT_RESTORE_FAIL >&2
    (( rc != 0 )) || rc=$restore_rc
  elif (( rc == 0 )); then
    echo "ELASTICSEARCH_CONTROL_CONTEXT_RESTORED module=$MODULE"
    echo "ELASTICSEARCH_MODULE_PASS module=$MODULE"
    rmdir "$work"
  fi
  exit "$rc"
}
trap finish EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
printf 'ELASTICSEARCH_MODULE_BEGIN module=%s run_id=%s\n' "$MODULE" "${ELASTICSEARCH_RUN_ID:-none}"
if [[ "$MODULE" == only ]]; then
  "$PY" "$BASE/tests/helpers/elasticsearch-full.py" </dev/null
else
  "$PY" "$LAB" "$MODULE" </dev/null
fi
