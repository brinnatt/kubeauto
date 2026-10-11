#!/bin/bash
# Sourced by the fixed supervisor; archives completed evidence before publishing.

ceph_evidence_begin() {
  CEPH_EVIDENCE_CASES=("$@")
  "$middleware_doc_python" "$ROOT/tests/helpers/ceph_case_evidence.py" audit --root "$ROOT"
  CEPH_EVIDENCE_RUN_ID="ceph-module-$(date -u +%Y%m%dT%H%M%SZ)-${BASHPID}"
  mkdir -p "$ROOT/logs/ceph-cases"
  CEPH_EVIDENCE_STAGE="$(mktemp -d "$ROOT/logs/ceph-cases/.stage-XXXXXX")"
  "$middleware_doc_python" "$ROOT/tests/helpers/ceph_case_evidence.py" snapshot \
    --root "$ROOT" --snapshot "$CEPH_EVIDENCE_STAGE/before.json" --cases "${CEPH_EVIDENCE_CASES[@]}"
}

ceph_evidence_publish() {
  "$middleware_doc_python" "$ROOT/tests/helpers/ceph_case_evidence.py" publish \
    --root "$ROOT" --snapshot "$CEPH_EVIDENCE_STAGE/before.json" \
    --log "$CEPH_EVIDENCE_STAGE/live.log" --state "$CEPH_EVIDENCE_STAGE/durable.exit" \
    --run-id "$CEPH_EVIDENCE_RUN_ID" --cases "${CEPH_EVIDENCE_CASES[@]}" "$@"
  matrix_counts ceph
  echo
}

ceph_evidence_collect_remote() {
  ssh_ceph 'cat /tmp/kubeauto-ceph-live.log' >"$CEPH_EVIDENCE_STAGE/live.log"
  ssh_ceph 'cat /tmp/kubeauto-ceph-gate.exit /tmp/kubeauto-ceph-gate.finalized' \
    >"$CEPH_EVIDENCE_STAGE/durable.exit"
}

ceph_verify_or_cleanup_focused() {
  # The remote gate owns fault recovery and retained-baseline verification.
  # A failed module never becomes PASS, even if its healthy baseline is retained.
  if ssh_ceph 'test -s /var/lib/kubeauto-ceph-test/focused-baseline.json'; then
    ssh_ceph 'python3 /usr/local/kubeauto/tests/helpers/ceph_baseline.py verify \
      --root /usr/local/kubeauto --state /var/lib/kubeauto-ceph-test'
  else
    ssh_ceph 'bash /usr/local/kubeauto/tests/helpers/ceph-cleanup.sh'
    ssh_ceph 'bash /usr/local/kubeauto/tests/helpers/ceph-cleanup.sh --verify'
  fi
}
