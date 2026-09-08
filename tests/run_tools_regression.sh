#!/usr/bin/env bash
# 独立 tools 测试分路。评审阶段只允许静态 preflight；live 入口须在矩阵批准后解锁。
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
MATRIX="$ROOT/tests/tools-test-matrix.yaml"
BUILD_HOST="${TOOLS_BUILD_HOST:-root@192.168.122.2}"
BUILD_SOURCE="/tmp/kubeauto-rocky8-build-source"
BUILD_OUTPUT="/tmp/kubeauto-rocky8-tools-output"
BUILD_GATE="/tmp/kubeauto-tools-build-gate"
BUILD_LOG="/tmp/kubeauto-tools-build-live.log"
TOOLS=(CalicoPolicyCli NetCheckCli KafkaCli MyBackupCli MigrationCli StarCli KubeBackupCli KubePublishCli OvpnUserCli)
BUILD_SUDO="sudo"
[[ "$BUILD_HOST" == root@* ]] && BUILD_SUDO=""
BUILD_USER="${BUILD_HOST%%@*}"
if [[ -n "${PYTHON:-}" ]]; then
  PY="$PYTHON"
elif [[ -x "$ROOT/.venv/bin/python" ]]; then
  PY="$ROOT/.venv/bin/python"
else
  PY="$(command -v python3.12 || command -v python3)"
fi

usage() {
  cat <<'EOF'
用法: tests/run_tools_regression.sh [--preflight|--status|--build-only|--calico-live|--full]

  --preflight  校验 tools 矩阵、脚本语法和独立导入边界（无远程变更）
  --status     输出当前 tools 矩阵状态
  --build-only 在固定 Rocky 8.10/glibc 2.28 环境构建并校验九个冻结工具
  --calico-live 在授权 Calico 集群运行 CalicoPolicyCli 完整 host/pod/both 回归
  --full       仅在矩阵全部 pass 且显式批准后进入 live（评审阶段拒绝）
EOF
}

case "${1:---preflight}" in
  --preflight)
    "$PY" "$ROOT/tests/helpers/validate_tools_test_matrix.py" "$MATRIX"
    "$PY" -m compileall -q "$ROOT/tools"
    echo TOOLS_PREFLIGHT_PASS
    ;;
  --status)
    "$PY" - <<'PY' "$MATRIX"
import sys, yaml
data = yaml.safe_load(open(sys.argv[1], encoding="utf-8"))
print("TOOLS_MATRIX_STATUS", data["meta"]["status"], data["coverage_summary"]["overall_assessment"])
PY
    ;;
  --build-only)
    command -v rsync >/dev/null 2>&1 || { echo "TOOLS_BUILD_PREFLIGHT_FAIL: rsync missing" >&2; exit 127; }
    LOCK="${TMPDIR:-/tmp}/kubeauto-tools-run.lock"
    exec 9>"$LOCK"
    flock -n 9 || { echo "TOOLS_BUILD_BLOCKED: another tools run owns $LOCK" >&2; exit 2; }
    stage="$(mktemp -d /tmp/kubeauto-tools-artifacts.XXXXXX)"
    evidence_log="$ROOT/logs/tools-build-failure-$(date +%Y%m%d-%H%M%S).log"
    cleanup() {
      set +e
      ssh -o BatchMode=yes -o StrictHostKeyChecking=no -o ConnectTimeout=8 "$BUILD_HOST" \
        "$BUILD_SUDO rm -rf '$BUILD_SOURCE' '$BUILD_OUTPUT' '$BUILD_LOG' '${BUILD_GATE}.pid' '${BUILD_GATE}.exit' '${BUILD_GATE}.finalized' /tmp/build-tools-rocky8.sh /tmp/run-durable-gate.sh" >/dev/null 2>&1
      rm -rf "$stage"
    }
    trap cleanup EXIT INT TERM
    ssh -o BatchMode=yes -o StrictHostKeyChecking=no -o ConnectTimeout=8 "$BUILD_HOST" \
      "$BUILD_SUDO rm -rf '$BUILD_SOURCE' '$BUILD_OUTPUT'; $BUILD_SUDO mkdir -p '$BUILD_SOURCE'; $BUILD_SUDO chown -R '$BUILD_USER':'$BUILD_USER' '$BUILD_SOURCE'"
    rsync -a --delete --exclude .git --exclude .venv --exclude build --exclude dist \
      --exclude logs --exclude __pycache__ --exclude '*.pyc' -e "ssh -o BatchMode=yes -o StrictHostKeyChecking=no" \
      "$ROOT/" "$BUILD_HOST:$BUILD_SOURCE/"
    scp -o BatchMode=yes -o StrictHostKeyChecking=no "$ROOT/tests/helpers/build-tools-rocky8.sh" \
      "$ROOT/tests/helpers/run-durable-gate.sh" "$BUILD_HOST:/tmp/"
    ssh -o BatchMode=yes -o StrictHostKeyChecking=no -o ConnectTimeout=8 "$BUILD_HOST" \
      "$BUILD_SUDO chmod 0755 /tmp/build-tools-rocky8.sh /tmp/run-durable-gate.sh; $BUILD_SUDO rm -f '$BUILD_LOG' '${BUILD_GATE}.pid' '${BUILD_GATE}.exit' '${BUILD_GATE}.finalized'; $BUILD_SUDO nohup bash /tmp/run-durable-gate.sh '$BUILD_GATE' TOOLS_BUILD_EXIT bash /tmp/build-tools-rocky8.sh '$BUILD_SOURCE' '$BUILD_OUTPUT' >'$BUILD_LOG' 2>&1 </dev/null &"
    while ! ssh -o BatchMode=yes -o StrictHostKeyChecking=no -o ConnectTimeout=8 "$BUILD_HOST" "$BUILD_SUDO test -s '${BUILD_GATE}.exit'" >/dev/null 2>&1; do
      ssh -o BatchMode=yes -o StrictHostKeyChecking=no -o ConnectTimeout=8 "$BUILD_HOST" "$BUILD_SUDO tail -n 20 '$BUILD_LOG'" || true
      sleep 10
    done
    build_rc="$(ssh -o BatchMode=yes -o StrictHostKeyChecking=no "$BUILD_HOST" "$BUILD_SUDO cat '${BUILD_GATE}.exit'")"
    if [[ "$build_rc" != 0 ]]; then
      {
        echo "TOOLS_BUILD_FAILURE_EVIDENCE rc=$build_rc host=$BUILD_HOST"
        ssh -o BatchMode=yes -o StrictHostKeyChecking=no "$BUILD_HOST" "$BUILD_SUDO tail -n 240 '$BUILD_LOG'" || true
      } | tee "$evidence_log"
      echo "TOOLS_BUILD_FAILURE_LOG=$evidence_log" >&2
      exit "$build_rc"
    fi
    for tool in "${TOOLS[@]}"; do
      scp -q -o BatchMode=yes -o StrictHostKeyChecking=no "$BUILD_HOST:$BUILD_OUTPUT/$tool" "$stage/$tool"
      test -x "$stage/$tool"
    done
    test "$(find "$stage" -maxdepth 1 -type f | wc -l)" -eq "${#TOOLS[@]}"
    sha256sum "$stage"/*
    echo "TOOLS_BUILD_PASS count=${#TOOLS[@]} glibc=2.28"
    ;;
  --calico-live)
    CALICO_HOST="${CALICO_TEST_HOST:-root@192.168.122.243}"
    CALICO_CONTEXT="${CALICO_TEST_CONTEXT:-context-cluster1}"
    CALICO_DENY_HOST="${CALICO_TEST_DENY_HOST:-192.168.122.193}"
    [[ "$CALICO_HOST" == root@192.168.122.243 ]] || { echo "CALICO_LIVE_BLOCKED_UNAUTHORIZED_HOST" >&2; exit 2; }
    [[ "$CALICO_DENY_HOST" == 192.168.122.193 ]] || { echo "CALICO_LIVE_BLOCKED_UNAUTHORIZED_DENY_SOURCE" >&2; exit 2; }
    "$PY" "$ROOT/tests/helpers/validate_tools_test_matrix.py" "$MATRIX"
    lock="${TMPDIR:-/tmp}/kubeauto-tools-calico-run.lock"
    exec 9>"$lock"
    flock -n 9 || { echo "CALICO_LIVE_BLOCKED: another tools run owns $lock" >&2; exit 2; }
    state="${TMPDIR:-/tmp}/kubeauto-tools-calico-live"
    gate_log="$ROOT/logs/tools-calico-live-$(date +%Y%m%d-%H%M%S).log"
    stage="$(mktemp -d /tmp/kubeauto-calico-live.XXXXXX)"
    cleanup_live() {
      set +e
      rm -rf "$stage"
      ssh -o BatchMode=yes -o StrictHostKeyChecking=no -o ConnectTimeout=8 "$CALICO_HOST" \
        "rm -f /tmp/CalicoPolicyCli.py /tmp/calico-live-regression.sh" >/dev/null 2>&1
    }
    trap cleanup_live EXIT INT TERM
    scp -q -o BatchMode=yes -o StrictHostKeyChecking=no "$ROOT/tools/k8stools/CalicoPolicyCli.py" \
      "$CALICO_HOST:/tmp/CalicoPolicyCli.py"
    scp -q -o BatchMode=yes -o StrictHostKeyChecking=no "$ROOT/tests/helpers/calico-live-regression.sh" \
      "$CALICO_HOST:/tmp/calico-live-regression.sh"
    rm -f "${state}.pid" "${state}.exit" "${state}.finalized"
    set +e
    bash "$ROOT/tests/helpers/run-durable-gate.sh" "$state" TOOLS_CALICO_EXIT \
      ssh -o BatchMode=yes -o StrictHostKeyChecking=no -o ConnectTimeout=8 "$CALICO_HOST" \
      "chmod 0755 /tmp/calico-live-regression.sh; CALICO_TOOL=/tmp/CalicoPolicyCli.py CALICO_CONTEXT='$CALICO_CONTEXT' CALICO_DENY_HOST='$CALICO_DENY_HOST' bash /tmp/calico-live-regression.sh" \
      2>&1 | tee "$gate_log"
    gate_rc=${PIPESTATUS[0]}
    set -e
    test "$gate_rc" -eq 0
    test "$(cat "${state}.exit")" = 0
    test "$(cat "${state}.finalized")" = 0
    grep -q '^CALICO_LIVE_REGRESSION_PASS ' "$gate_log"
    ssh -o BatchMode=yes -o StrictHostKeyChecking=no -o ConnectTimeout=8 "$CALICO_HOST" \
      "! calicoctl get globalnetworkpolicy kubeauto-delivery-cal-host-39091 >/dev/null 2>&1; ! calicoctl get globalnetworkpolicy kubeauto-delivery-cal-both-39093 >/dev/null 2>&1; ! kubectl get networkpolicy -n monitor kubeauto-delivery-cal-pod-39092 kubeauto-delivery-cal-pod-39093 >/dev/null 2>&1; for p in 39091 39092 39093; do ! ss -ltn 'sport = :'\$p | tail -n +2 | grep -q .; done"
    echo "TOOLS_CLEAN_VERIFY_PASS scope=calico host=$CALICO_HOST"
    ;;
  --full)
    if [[ "${TOOLS_MATRIX_APPROVED:-no}" != yes ]]; then
      echo "TOOLS_LIVE_BLOCKED_REVIEW: set TOOLS_MATRIX_APPROVED=yes only after review approval" >&2
      exit 2
    fi
    "$PY" "$ROOT/tests/helpers/validate_tools_test_matrix.py" "$MATRIX" --require-pass
    echo "TOOLS_LIVE_NOT_IMPLEMENTED: complete the approved runner stages before live execution" >&2
    exit 2
    ;;
  -h|--help)
    usage
    ;;
  *)
    usage >&2
    exit 2
    ;;
esac
