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
用法: tests/run_tools_regression.sh [--preflight|--status|--build-only|--calico-live|--kafka-live|--kube-backup-live|--kube-publish-live|--migration-live|--mybackup-live|--star-live|--full]

  --preflight  校验 tools 矩阵、脚本语法和独立导入边界（无远程变更）
  --status     输出当前 tools 矩阵状态
  --build-only 在固定 Rocky 8.10/glibc 2.28 环境构建并校验九个冻结工具
  --calico-live 在授权 Calico 集群运行 CalicoPolicyCli 完整 host/pod/both 回归
  --kafka-live 在授权 122.2 控制机运行 KafkaCli 独立 Kafka 发行版全功能回归
  --kube-backup-live 在授权 Kubernetes 集群运行 KubeBackupCli 完整备份/恢复回归
  --kube-publish-live 在授权 Docker/nerdctl 主机运行 KubePublishCli 完整镜像回归
  --migration-live 在授权 122.2 控制机运行 MigrationCli MySQL 逻辑迁移回归
  --mybackup-live 在授权 122.2 控制机运行 MyBackupCli XtraBackup 全功能回归
  --star-live    在授权 122.2 控制机使用已登记 StarRocks 固定归档运行 StarCli 全功能回归
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
    # Keep the target-built binary as the only live input; a developer-host
    # dist/ binary can require a newer glibc than the Rocky 8.10 contract.
    install -m 0755 "$stage/StarCli" "$ROOT/dist/StarCli-rocky8"
    sha256sum "$stage"/*
    echo "TOOLS_BUILD_PASS count=${#TOOLS[@]} glibc=2.28 starcli=$ROOT/dist/StarCli-rocky8"
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
  --kube-backup-live)
    KUBE_BACKUP_HOST="${KUBE_BACKUP_TEST_HOST:-root@192.168.122.243}"
    KUBE_BACKUP_CONTEXT="${KUBE_BACKUP_TEST_CONTEXT:-context-cluster1}"
    [[ "$KUBE_BACKUP_HOST" == root@192.168.122.243 ]] || { echo "KUBE_BACKUP_LIVE_BLOCKED_UNAUTHORIZED_HOST" >&2; exit 2; }
    "$PY" "$ROOT/tests/helpers/validate_tools_test_matrix.py" "$MATRIX"
    lock="${TMPDIR:-/tmp}/kubeauto-tools-kube-backup-run.lock"
    exec 9>"$lock"
    flock -n 9 || { echo "KUBE_BACKUP_LIVE_BLOCKED: another KubeBackup run owns $lock" >&2; exit 2; }
    state="${TMPDIR:-/tmp}/kubeauto-tools-kube-backup-live"
    gate_log="$ROOT/logs/tools-kube-backup-live-$(date +%Y%m%d-%H%M%S).log"
    set +e
    bash "$ROOT/tests/helpers/run-durable-gate.sh" "$state" TOOLS_KUBE_BACKUP_EXIT \
      env PYTHON="$PY" bash "$ROOT/tests/helpers/kube-backup-live-regression.sh" \
      "$ROOT/tools/k8stools/KubeBackupCli.py" "$KUBE_BACKUP_HOST" "$KUBE_BACKUP_CONTEXT" \
      2>&1 | tee "$gate_log"
    gate_rc=${PIPESTATUS[0]}
    set -e
    test "$gate_rc" -eq 0
    test "$(cat "${state}.exit")" = 0
    test "$(cat "${state}.finalized")" = 0
    grep -q '^KUBE_BACKUP_LIVE_REGRESSION_PASS ' "$gate_log"
    grep -q '^KUBE_BACKUP_CLEAN_VERIFY_PASS ' "$gate_log"
    echo "TOOLS_CLEAN_VERIFY_PASS scope=kube-backup host=$KUBE_BACKUP_HOST"
    ;;
  --kafka-live)
    KAFKA_HOST="${KAFKA_TEST_HOST:-root@192.168.122.2}"
    KAFKA_MULTI_NODES=(192.168.122.217 192.168.122.246 192.168.122.193 192.168.122.210 192.168.122.216)
    KAFKA_MULTI_ROOT=/tmp/kafka-cli-multi
    KAFKA_FIXTURE_REPOSITORY=quay.io/strimzi/kafka
    KAFKA_FIXTURE_IMAGE=quay.io/strimzi/kafka:1.2.0-kafka-4.3.1
    KAFKA_FIXTURE_DIGEST=sha256:fef34b5438e8556cc08c01f3e254e47346f061b53a4e38d4289853777e0ea7f1
    [[ "$KAFKA_HOST" == root@192.168.122.2 ]] || { echo "KAFKA_LIVE_BLOCKED_UNAUTHORIZED_HOST" >&2; exit 2; }
    "$PY" "$ROOT/tests/helpers/validate_tools_test_matrix.py" "$MATRIX"
    lock="${TMPDIR:-/tmp}/kubeauto-tools-kafka-run.lock"; exec 9>"$lock"
    flock -n 9 || { echo "KAFKA_LIVE_BLOCKED: another Kafka run owns $lock" >&2; exit 2; }
    state="${TMPDIR:-/tmp}/kubeauto-tools-kafka-live"; remote_log=/tmp/kafka-cli-live.log
    gate_log="$ROOT/logs/tools-kafka-live-$(date +%Y%m%d-%H%M%S).log"
    kafka_lease_token="kafka-tools-$$-$(date +%s)"
    kafka_leases_ready=no
    cleanup_kafka_live() (
      set +e
      ssh -o BatchMode=yes -o StrictHostKeyChecking=no -o ConnectTimeout=8 "$KAFKA_HOST" \
        "pkill -f '[k]afka.Kafka.*kafka-cli-' || true; rm -f /tmp/KafkaCli.py /tmp/kafka-cli-live-regression.sh /tmp/kafka-cli-multinode-regression.sh /tmp/run-durable-gate.sh '$remote_log' '${state}.pid' '${state}.exit' '${state}.finalized' /tmp/kafka-cli-multi-broker-down.out; rm -rf /tmp/kafka-cli-live '$KAFKA_MULTI_ROOT'"
      if [[ "$kafka_leases_ready" == yes ]]; then
        for node in "${KAFKA_MULTI_NODES[@]}"; do
          ssh -o BatchMode=yes -o StrictHostKeyChecking=yes -o ConnectTimeout=8 "root@$node" \
            "if test -f '$KAFKA_MULTI_ROOT/.lease' && test \"\$(cat '$KAFKA_MULTI_ROOT/.lease')\" = '$kafka_lease_token'; then pkill -f '[k]afka.Kafka.*kafka-cli-multi' || true; rm -rf '$KAFKA_MULTI_ROOT'; fi" || true
        done
      fi
    )
    trap cleanup_kafka_live EXIT INT TERM
    echo "KAFKA_LIVE_STAGE pre-clean"
    cleanup_kafka_live
    ssh -o BatchMode=yes -o StrictHostKeyChecking=no -o ConnectTimeout=8 "$KAFKA_HOST" \
      "! pgrep -af '[k]afka.Kafka.*kafka-cli-'; ! test -e /tmp/KafkaCli.py; ! test -e /tmp/kafka-cli-live-regression.sh; ! test -e /tmp/kafka-cli-multinode-regression.sh; ! test -e /tmp/run-durable-gate.sh; ! test -e '$remote_log'; ! test -e '${state}.pid'; ! test -e '${state}.exit'; ! test -e '${state}.finalized'; ! test -e /tmp/kafka-cli-multi-broker-down.out; ! test -d /tmp/kafka-cli-live; ! test -d '$KAFKA_MULTI_ROOT'; ! find /tmp -maxdepth 1 -type f -name 'kafkacli-*.client.properties' -print -quit | grep -q ."
    for node in "${KAFKA_MULTI_NODES[@]}"; do
      ssh -o BatchMode=yes -o StrictHostKeyChecking=yes -o ConnectTimeout=8 "root@$node" \
        "! pgrep -af '[k]afka.Kafka.*kafka-cli-multi'; ! test -d '$KAFKA_MULTI_ROOT'; ! find /tmp -maxdepth 1 -type f -name 'kafkacli-*.client.properties' -print -quit | grep -q ."
    done
    echo "KAFKA_LIVE_STAGE pre-clean-verified"
    echo "KAFKA_LIVE_STAGE lease-acquire nodes=${#KAFKA_MULTI_NODES[@]}"
    for node in "${KAFKA_MULTI_NODES[@]}"; do
      ssh -o BatchMode=yes -o StrictHostKeyChecking=yes -o ConnectTimeout=8 "root@$node" \
        "mkdir '$KAFKA_MULTI_ROOT' && printf '%s' '$kafka_lease_token' >'$KAFKA_MULTI_ROOT/.lease'" \
        || { echo "KAFKA_LIVE_BLOCKED_LEASE host=$node" >&2; exit 2; }
    done
    kafka_leases_ready=yes
    echo "KAFKA_LIVE_STAGE control-fixture-extract"
    scp -q -o BatchMode=yes -o StrictHostKeyChecking=no "$ROOT/tools/kafka/KafkaCli.py" "$ROOT/tests/helpers/kafka-cli-live-regression.sh" "$ROOT/tests/helpers/kafka-cli-multinode-regression.sh" "$ROOT/tests/helpers/run-durable-gate.sh" "$KAFKA_HOST:/tmp/"
    ssh -o BatchMode=yes -o StrictHostKeyChecking=no "$KAFKA_HOST" "rm -rf /tmp/kafka-cli-live '$KAFKA_MULTI_ROOT'; mkdir -p '$KAFKA_MULTI_ROOT'; cid=\$(docker create '$KAFKA_FIXTURE_IMAGE'); docker cp \"\$cid:/opt/kafka\" '$KAFKA_MULTI_ROOT/kafka'; docker rm \"\$cid\" >/dev/null; tar -C '$KAFKA_MULTI_ROOT' -czf '$KAFKA_MULTI_ROOT/kafka.tgz' kafka; test -x '$KAFKA_MULTI_ROOT/kafka/bin/kafka-storage.sh'"
    fixture_repo_digests="$(ssh -o BatchMode=yes -o StrictHostKeyChecking=no "$KAFKA_HOST" "docker image inspect '$KAFKA_FIXTURE_IMAGE' --format '{{json .RepoDigests}}'")"
    [[ "$fixture_repo_digests" == *"$KAFKA_FIXTURE_REPOSITORY@$KAFKA_FIXTURE_DIGEST"* ]] || { echo "KAFKA_FIXTURE_DIGEST_MISMATCH" >&2; exit 3; }
    echo "KAFKA_LIVE_STAGE node-fixture-distribute"
    for node in "${KAFKA_MULTI_NODES[@]}"; do
      echo "KAFKA_LIVE_STAGE node-fixture-copy host=$node"
      ssh -o BatchMode=yes -o StrictHostKeyChecking=no "$KAFKA_HOST" \
        "scp -o BatchMode=yes -o StrictHostKeyChecking=yes '$KAFKA_MULTI_ROOT/kafka.tgz' 'root@$node:$KAFKA_MULTI_ROOT/kafka.tgz'"
      ssh -o BatchMode=yes -o StrictHostKeyChecking=yes -o ConnectTimeout=8 "root@$node" \
        "if ! command -v tar >/dev/null 2>&1; then dnf install -y tar || exit 40; fi; if ! command -v java >/dev/null 2>&1; then dnf install -y java-17-openjdk-headless || exit 41; fi; tar -C '$KAFKA_MULTI_ROOT' -xzf '$KAFKA_MULTI_ROOT/kafka.tgz'; rm -f '$KAFKA_MULTI_ROOT/kafka.tgz'; test -x '$KAFKA_MULTI_ROOT/kafka/bin/kafka-storage.sh'"
    done
    echo "KAFKA_LIVE_STAGE durable-launch"
    ssh -o BatchMode=yes -o StrictHostKeyChecking=no "$KAFKA_HOST" "chmod 0755 /tmp/kafka-cli-live-regression.sh /tmp/kafka-cli-multinode-regression.sh /tmp/run-durable-gate.sh; rm -f '${state}.pid' '${state}.exit' '${state}.finalized'; nohup env KAFKA_TOOL=/tmp/KafkaCli.py KAFKA_HOME='$KAFKA_MULTI_ROOT/kafka' KAFKA_MULTI_ROOT='$KAFKA_MULTI_ROOT' KAFKA_SSH_KEY=/root/.ssh/id_ed25519 bash /tmp/run-durable-gate.sh '$state' TOOLS_KAFKA_EXIT bash -c 'bash /tmp/kafka-cli-live-regression.sh && bash /tmp/kafka-cli-multinode-regression.sh' >'$remote_log' 2>&1 </dev/null &"
    echo "KAFKA_LIVE_STAGE durable-follow"
    while ! ssh -o BatchMode=yes -o StrictHostKeyChecking=no "$KAFKA_HOST" "test -s '${state}.exit'" >/dev/null 2>&1; do ssh -o BatchMode=yes -o StrictHostKeyChecking=no "$KAFKA_HOST" "tail -n 20 '$remote_log'" || true; sleep 10; done
    {
      echo "KAFKA_FIXTURE_IMAGE image=$KAFKA_FIXTURE_IMAGE digest=$KAFKA_FIXTURE_DIGEST"
      ssh -o BatchMode=yes -o StrictHostKeyChecking=no "$KAFKA_HOST" "cat '$remote_log'"
    } | tee "$gate_log"
    gate_rc="$(ssh -o BatchMode=yes -o StrictHostKeyChecking=no "$KAFKA_HOST" "cat '${state}.exit'")"; test "$gate_rc" -eq 0
    finalized_rc="$(ssh -o BatchMode=yes -o StrictHostKeyChecking=no "$KAFKA_HOST" "cat '${state}.finalized'")"; test "$finalized_rc" -eq 0
    echo "KAFKA_DURABLE_STATUS rc=$gate_rc finalized=$finalized_rc" | tee -a "$gate_log"
    grep -q '^KAFKA_CLI_LIVE_REGRESSION_PASS ' "$gate_log"
    grep -q '^KAFKA_CLI_MULTINODE_REGRESSION_PASS ' "$gate_log"
    cleanup_kafka_live
    ssh -o BatchMode=yes -o StrictHostKeyChecking=no "$KAFKA_HOST" "! pgrep -af '[k]afka.Kafka.*kafka-cli-'; ! test -e /tmp/KafkaCli.py; ! test -e /tmp/kafka-cli-live-regression.sh; ! test -e /tmp/kafka-cli-multinode-regression.sh; ! test -e /tmp/run-durable-gate.sh; ! test -e '$remote_log'; ! test -e '${state}.pid'; ! test -e '${state}.exit'; ! test -e '${state}.finalized'; ! test -e /tmp/kafka-cli-multi-broker-down.out; ! test -d /tmp/kafka-cli-live; ! test -d '$KAFKA_MULTI_ROOT'; ! find /tmp -maxdepth 1 -type f -name 'kafkacli-*.client.properties' -print -quit | grep -q ."
    for node in "${KAFKA_MULTI_NODES[@]}"; do
      ssh -o BatchMode=yes -o StrictHostKeyChecking=yes -o ConnectTimeout=8 "root@$node" \
        "! pgrep -af '[k]afka.Kafka.*kafka-cli-multi'; ! test -d '$KAFKA_MULTI_ROOT'; ! find /tmp -maxdepth 1 -type f -name 'kafkacli-*.client.properties' -print -quit | grep -q ."
    done
    echo "TOOLS_CLEAN_VERIFY_PASS scope=kafka host=$KAFKA_HOST nodes=${#KAFKA_MULTI_NODES[@]}" | tee -a "$gate_log"
    ;;
  --kube-publish-live)
    KUBE_PUBLISH_HOST="${KUBE_PUBLISH_TEST_HOST:-root@192.168.122.2}"
    KUBE_PUBLISH_TARGETS="${KUBE_PUBLISH_TEST_TARGETS:-192.168.122.217:22 192.168.122.210-210:22 192.168.122.216:22}"
    [[ "$KUBE_PUBLISH_HOST" == root@192.168.122.2 ]] || { echo "KUBE_PUBLISH_LIVE_BLOCKED_UNAUTHORIZED_HOST" >&2; exit 2; }
    [[ "$KUBE_PUBLISH_TARGETS" == "192.168.122.217:22 192.168.122.210-210:22 192.168.122.216:22" ]] || { echo "KUBE_PUBLISH_LIVE_BLOCKED_UNAUTHORIZED_TARGETS" >&2; exit 2; }
    "$PY" "$ROOT/tests/helpers/validate_tools_test_matrix.py" "$MATRIX"
    lock="${TMPDIR:-/tmp}/kubeauto-tools-kube-publish-run.lock"
    exec 9>"$lock"
    flock -n 9 || { echo "KUBE_PUBLISH_LIVE_BLOCKED: another KubePublish run owns $lock" >&2; exit 2; }
    state="/tmp/kubeauto-tools-kube-publish-live"
    remote_log="/tmp/kubeauto-tools-kube-publish-live.log"
    gate_log="$ROOT/logs/tools-kube-publish-live-$(date +%Y%m%d-%H%M%S).log"
    cleanup_publish_live() {
      set +e
      ssh -o BatchMode=yes -o StrictHostKeyChecking=no -o ConnectTimeout=8 "$KUBE_PUBLISH_HOST" \
        "rm -f /tmp/KubePublishCli.py /tmp/kube-publish-live-regression.sh /tmp/run-durable-gate.sh '$remote_log' '${state}.pid' '${state}.exit' '${state}.finalized'" >/dev/null 2>&1
    }
    trap cleanup_publish_live EXIT INT TERM
    scp -q -o BatchMode=yes -o StrictHostKeyChecking=no "$ROOT/tools/k8stools/KubePublishCli.py" \
      "$ROOT/tests/helpers/kube-publish-live-regression.sh" "$ROOT/tests/helpers/run-durable-gate.sh" "$KUBE_PUBLISH_HOST:/tmp/"
    ssh -o BatchMode=yes -o StrictHostKeyChecking=no -o ConnectTimeout=8 "$KUBE_PUBLISH_HOST" \
      "chmod 0755 /tmp/kube-publish-live-regression.sh /tmp/run-durable-gate.sh; rm -f '$remote_log' '${state}.pid' '${state}.exit' '${state}.finalized'; nohup env KUBE_PUBLISH_TOOL=/tmp/KubePublishCli.py KUBE_PUBLISH_TARGETS='$KUBE_PUBLISH_TARGETS' bash /tmp/run-durable-gate.sh '$state' TOOLS_KUBE_PUBLISH_EXIT bash /tmp/kube-publish-live-regression.sh >'$remote_log' 2>&1 </dev/null &"
    while ! ssh -o BatchMode=yes -o StrictHostKeyChecking=no -o ConnectTimeout=8 "$KUBE_PUBLISH_HOST" "test -s '${state}.exit'" >/dev/null 2>&1; do
      ssh -o BatchMode=yes -o StrictHostKeyChecking=no -o ConnectTimeout=8 "$KUBE_PUBLISH_HOST" "tail -n 30 '$remote_log'" || true
      sleep 10
    done
    ssh -o BatchMode=yes -o StrictHostKeyChecking=no "$KUBE_PUBLISH_HOST" "cat '$remote_log'" | tee "$gate_log"
    gate_rc="$(ssh -o BatchMode=yes -o StrictHostKeyChecking=no "$KUBE_PUBLISH_HOST" "cat '${state}.exit'")"
    test "$gate_rc" -eq 0
    ssh -o BatchMode=yes -o StrictHostKeyChecking=no "$KUBE_PUBLISH_HOST" "test \"$(cat '${state}.finalized')\" = 0"
    grep -q '^KUBE_PUBLISH_LIVE_REGRESSION_PASS ' "$gate_log"
    ssh -o BatchMode=yes -o StrictHostKeyChecking=no -o ConnectTimeout=8 "$KUBE_PUBLISH_HOST" \
      "! test -e /tmp/kubeauto-kp-live; ! docker image inspect 127.0.0.1:5000/kubeauto-kp-live:v1 >/dev/null 2>&1"
    for target in 192.168.122.217 192.168.122.210 192.168.122.216; do
      ssh -o BatchMode=yes -o StrictHostKeyChecking=no -o ConnectTimeout=8 "root@$target" \
        "! nerdctl -n kubeauto-kp-live image inspect 127.0.0.1:5000/kubeauto-kp-live:v1 >/dev/null 2>&1"
    done
    echo "TOOLS_CLEAN_VERIFY_PASS scope=kube-publish host=$KUBE_PUBLISH_HOST targets=3"
    ;;
  --migration-live)
    MIGRATION_HOST="${MIGRATION_TEST_HOST:-root@192.168.122.2}"
    [[ "$MIGRATION_HOST" == root@192.168.122.2 ]] || { echo "MIGRATION_LIVE_BLOCKED_UNAUTHORIZED_HOST" >&2; exit 2; }
    "$PY" "$ROOT/tests/helpers/validate_tools_test_matrix.py" "$MATRIX"
    lock="${TMPDIR:-/tmp}/kubeauto-tools-migration-run.lock"; exec 9>"$lock"
    flock -n 9 || { echo "MIGRATION_LIVE_BLOCKED: another Migration run owns $lock" >&2; exit 2; }
    state="${TMPDIR:-/tmp}/kubeauto-tools-migration-live"
    gate_log="$ROOT/logs/tools-migration-live-$(date +%Y%m%d-%H%M%S).log"
    scp -q -o BatchMode=yes -o StrictHostKeyChecking=no "$ROOT/tools/mysqltools/MigrationCli.py" "$MIGRATION_HOST:/tmp/MigrationCli-tools-live.py"
    scp -q -o BatchMode=yes -o StrictHostKeyChecking=no "$ROOT/tests/helpers/migration-live-regression.sh" "$MIGRATION_HOST:/tmp/migration-live-regression.sh"
    ssh -o BatchMode=yes -o StrictHostKeyChecking=no "$MIGRATION_HOST" \
      "rm -rf /tmp/tools-mig-venv; python3 -m venv /tmp/tools-mig-venv; /tmp/tools-mig-venv/bin/pip install --no-input --disable-pip-version-check PyMySQL==1.1.2 cryptography==44.0.2 >/dev/null"
    rm -f "${state}.pid" "${state}.exit" "${state}.finalized"
    set +e
    bash "$ROOT/tests/helpers/run-durable-gate.sh" "$state" TOOLS_MIGRATION_EXIT \
      ssh -o BatchMode=yes -o StrictHostKeyChecking=no -o ConnectTimeout=8 "$MIGRATION_HOST" \
      "chmod 0755 /tmp/migration-live-regression.sh; MIGRATION_TOOL=/tmp/MigrationCli-tools-live.py MIGRATION_PYTHON=/tmp/tools-mig-venv/bin/python bash /tmp/migration-live-regression.sh" \
      2>&1 | tee "$gate_log"
    gate_rc=${PIPESTATUS[0]}
    set -e
    test "$gate_rc" -eq 0
    test "$(cat "${state}.exit")" = 0
    test "$(cat "${state}.finalized")" = 0
    grep -q '^MIGRATION_CLI_LIVE_REGRESSION_PASS ' "$gate_log"
    ssh -o BatchMode=yes -o StrictHostKeyChecking=no "$MIGRATION_HOST" \
      "! docker ps -a --format '{{.Names}}' | grep -Eq '^(tools-mig-src|tools-mig-tgt|tools-mig-tgt9)$'; ! test -e /tmp/MigrationCli-tools-live.py; ! test -e /tmp/migration-live-regression.sh; ! test -e /tmp/migration-live.json; ! test -e /tmp/migration-dry-run.json; ! test -e /tmp/migration-dry.log; ! test -e /tmp/migration-structure-only.json; ! test -e /tmp/migration-definer.json; ! test -e /tmp/migration-definer.log; ! test -e /tmp/migration-invalid.json; ! test -e /tmp/migration-invalid.log; ! test -e /tmp/migration-unreachable.json; ! test -e /tmp/migration-unreachable.log; ! test -d /tmp/migration-live-report; ! test -d /tmp/migration-dry-report; ! test -d /tmp/migration-structure-report; ! test -d /tmp/migration-definer-report; ! test -d /tmp/migration-invalid-report; ! test -d /tmp/migration-unreachable-report; ! test -d /tmp/tools-mig-venv"
    echo "TOOLS_CLEAN_VERIFY_PASS scope=migration host=$MIGRATION_HOST" | tee -a "$gate_log"
    ;;
  --mybackup-live)
    MYBACKUP_HOST="${MYBACKUP_TEST_HOST:-root@192.168.122.2}"
    [[ "$MYBACKUP_HOST" == root@192.168.122.2 ]] || { echo "MYBACKUP_LIVE_BLOCKED_UNAUTHORIZED_HOST" >&2; exit 2; }
    "$PY" "$ROOT/tests/helpers/validate_tools_test_matrix.py" "$MATRIX"
    bash -n "$ROOT/tests/helpers/mybackup-cli-live-regression.sh"
    lock="${TMPDIR:-/tmp}/kubeauto-tools-mybackup-run.lock"; exec 9>"$lock"
    flock -n 9 || { echo "MYBACKUP_LIVE_BLOCKED: another MyBackup run owns $lock" >&2; exit 2; }
    state="${TMPDIR:-/tmp}/kubeauto-tools-mybackup-live"
    gate_log="$ROOT/logs/tools-mybackup-live-$(date +%Y%m%d-%H%M%S).log"
    remote_log=/tmp/tools-mybackup-live.log
    cleanup_mybackup_live() {
      set +e
      ssh -o BatchMode=yes -o StrictHostKeyChecking=no -o ConnectTimeout=8 "$MYBACKUP_HOST" \
        "docker rm -f tools-mbk-mysql >/dev/null 2>&1 || true; rm -rf /tmp/tools-mbk-live /tmp/MyBackupCli-tools-live.py /tmp/mybackup-cli-live-regression.sh /tmp/run-durable-gate.sh '$remote_log' /tmp/mybackup-9x.out /tmp/tools-mybackup-file.log" >/dev/null 2>&1 || true
    }
    trap cleanup_mybackup_live EXIT INT TERM
    scp -q -o BatchMode=yes -o StrictHostKeyChecking=no "$ROOT/tools/mysqltools/MyBackupCli.py" "$ROOT/tests/helpers/mybackup-cli-live-regression.sh" "$ROOT/tests/helpers/run-durable-gate.sh" "$MYBACKUP_HOST:/tmp/"
    ssh -o BatchMode=yes -o StrictHostKeyChecking=no "$MYBACKUP_HOST" "mv /tmp/MyBackupCli.py /tmp/MyBackupCli-tools-live.py"
    ssh -o BatchMode=yes -o StrictHostKeyChecking=no "$MYBACKUP_HOST" "chmod 0755 /tmp/mybackup-cli-live-regression.sh /tmp/run-durable-gate.sh; rm -f '$remote_log' '${state}.pid' '${state}.exit' '${state}.finalized'; nohup env MYBACKUP_TOOL=/tmp/MyBackupCli-tools-live.py MYBACKUP_PYTHON=python3 bash /tmp/run-durable-gate.sh '$state' TOOLS_MYBACKUP_EXIT bash /tmp/mybackup-cli-live-regression.sh >'$remote_log' 2>&1 </dev/null &"
    while ! ssh -o BatchMode=yes -o StrictHostKeyChecking=no "$MYBACKUP_HOST" "test -s '${state}.exit'" >/dev/null 2>&1; do
      ssh -o BatchMode=yes -o StrictHostKeyChecking=no "$MYBACKUP_HOST" "tail -n 30 '$remote_log'" || true
      sleep 10
    done
    ssh -o BatchMode=yes -o StrictHostKeyChecking=no "$MYBACKUP_HOST" "cat '$remote_log' 2>/dev/null; cat /tmp/tools-mybackup-file.log 2>/dev/null" | tee "$gate_log"
    gate_rc="$(ssh -o BatchMode=yes -o StrictHostKeyChecking=no "$MYBACKUP_HOST" "cat '${state}.exit'")"; test "$gate_rc" -eq 0
    finalized_rc="$(ssh -o BatchMode=yes -o StrictHostKeyChecking=no "$MYBACKUP_HOST" "cat '${state}.finalized'")"; test "$finalized_rc" -eq 0
    grep -q '^MYBACKUP_CLI_LIVE_REGRESSION_PASS ' "$gate_log"
    cleanup_mybackup_live
    ssh -o BatchMode=yes -o StrictHostKeyChecking=no "$MYBACKUP_HOST" "rm -f '${state}.pid' '${state}.exit' '${state}.finalized'"
    ssh -o BatchMode=yes -o StrictHostKeyChecking=no "$MYBACKUP_HOST" "! docker ps -a --format '{{.Names}}' | grep -qx tools-mbk-mysql; ! test -e /tmp/MyBackupCli-tools-live.py; ! test -e /tmp/mybackup-cli-live-regression.sh; ! test -e /tmp/tools-mbk-live; ! test -e '$remote_log'; ! test -e '${state}.pid'; ! test -e '${state}.exit'; ! test -e '${state}.finalized'"
    echo "TOOLS_CLEAN_VERIFY_PASS scope=mybackup host=$MYBACKUP_HOST" | tee -a "$gate_log"
    ;;
  --star-live)
    STAR_HOST="${STARCLI_TEST_HOST:-root@192.168.122.2}"
    STAR_ARCHIVE="${STARCLI_ARCHIVE:-}"
    STAR_ARCHIVE_REMOTE="${STARCLI_ARCHIVE_REMOTE:-}"
    STAR_ARCHIVE_SHA256="${STARCLI_ARCHIVE_SHA256:-}"
    STARCLI_BINARY="${STARCLI_BINARY:-$ROOT/dist/StarCli-rocky8}"
    [[ "$STAR_HOST" == root@192.168.122.2 ]] || { echo "STARCLI_LIVE_BLOCKED_UNAUTHORIZED_HOST" >&2; exit 2; }
    "$PY" "$ROOT/tests/helpers/validate_tools_test_matrix.py" "$MATRIX"
    bash -n "$ROOT/tests/helpers/starcli-live-regression.sh"
    "$PY" "$ROOT/tests/helpers/starcli-contract-test.py"
    [[ -n "$STAR_ARCHIVE_SHA256" ]] || {
      echo "STARCLI_LIVE_BLOCKED_ARTIFACT: provide the fixed, dual-pushed StarRocks archive SHA256" >&2
      exit 2
    }
    forbidden_prefix="192.168.122."
    forbidden_host="${forbidden_prefix}1"
    [[ "$STAR_ARCHIVE" != *"$forbidden_host"* ]] || { echo "STARCLI_LIVE_BLOCKED_FORBIDDEN_ADDRESS" >&2; exit 2; }
    if [[ -n "$STAR_ARCHIVE_REMOTE" ]]; then
      [[ "$STAR_ARCHIVE_REMOTE" == /root/StarRocks-3.5.12-centos-amd64.tar.gz ]] || { echo "STARCLI_LIVE_BLOCKED_ARTIFACT_PATH" >&2; exit 2; }
      remote_archive="$STAR_ARCHIVE_REMOTE"
    else
      [[ -n "$STAR_ARCHIVE" && -f "$STAR_ARCHIVE" ]] || { echo "STARCLI_LIVE_BLOCKED_ARTIFACT_MISSING: $STAR_ARCHIVE" >&2; exit 2; }
      remote_archive=/tmp/starrocks-fixed.tar.gz
    fi
    [[ -x "$STARCLI_BINARY" ]] || { echo "STARCLI_LIVE_BLOCKED_BINARY: build dist/StarCli first" >&2; exit 2; }
    lock="${TMPDIR:-/tmp}/kubeauto-tools-starcli-run.lock"; exec 9>"$lock"
    flock -n 9 || { echo "STARCLI_LIVE_BLOCKED: another StarCli run owns $lock" >&2; exit 2; }
    state="${TMPDIR:-/tmp}/kubeauto-tools-starcli-live"
    gate_log="$ROOT/logs/tools-starcli-live-$(date +%Y%m%d-%H%M%S).log"
    remote_log=/tmp/starcli-live.log
    verify_star_live_clean() {
      ssh -o BatchMode=yes -o StrictHostKeyChecking=no -o ConnectTimeout=8 "$STAR_HOST" \
        "for unit in starrocks-cn starrocks-be starrocks-fe; do test ! -e /etc/systemd/system/\$unit.service || exit 1; ! systemctl is-active --quiet \$unit || exit 1; done; test ! -e /tmp/starcli-live; ps -eo comm=,args= | awk '\$1 == \"java\" || \$1 == \"starrocks_be\" { if (index(\$0, \"/tmp/starcli-live/\")) found=1 } END { exit found }'"
    }
    cleanup_star_live() {
      set +e
      ssh -o BatchMode=yes -o StrictHostKeyChecking=no -o ConnectTimeout=8 "$STAR_HOST" \
        "for unit in starrocks-cn starrocks-be starrocks-fe; do systemctl disable --now \$unit >/dev/null 2>&1 || true; systemctl kill --kill-who=all \$unit >/dev/null 2>&1 || true; rm -f /etc/systemd/system/\$unit.service; done; systemctl daemon-reload >/dev/null 2>&1 || true; rm -rf /tmp/starcli-live /tmp/StarCli.py /tmp/StarCli-tools-live.py /tmp/starcli-live-regression.sh /tmp/run-durable-gate.sh '$remote_log' '${state}.pid' '${state}.exit' '${state}.finalized'" >/dev/null 2>&1 || true
    }
    trap cleanup_star_live EXIT INT TERM
    echo "STARCLI_LIVE_STAGE pre-clean"
    verify_star_live_clean
    echo "STARCLI_LIVE_STAGE pre-clean-verified"
    scp -q -o BatchMode=yes -o StrictHostKeyChecking=no "$STARCLI_BINARY" "$STAR_HOST:/tmp/StarCli"
    scp -q -o BatchMode=yes -o StrictHostKeyChecking=no "$ROOT/tests/helpers/starcli-live-regression.sh" "$ROOT/tests/helpers/run-durable-gate.sh" "$STAR_HOST:/tmp/"
    ssh -o BatchMode=yes -o StrictHostKeyChecking=no "$STAR_HOST" "mv /tmp/StarCli /tmp/StarCli.py; rm -f '$remote_log' '${state}.pid' '${state}.exit' '${state}.finalized'; chmod 0755 /tmp/StarCli.py /tmp/starcli-live-regression.sh /tmp/run-durable-gate.sh"
    if [[ -z "$STAR_ARCHIVE_REMOTE" ]]; then
      scp -q -o BatchMode=yes -o StrictHostKeyChecking=no "$STAR_ARCHIVE" "$STAR_HOST:/tmp/starrocks-fixed.tar.gz"
    fi
    set +e
    bash "$ROOT/tests/helpers/run-durable-gate.sh" "$state" TOOLS_STARCLI_EXIT \
      ssh -o BatchMode=yes -o StrictHostKeyChecking=no -o ConnectTimeout=8 "$STAR_HOST" \
      "STARCLI_TOOL=/tmp/StarCli.py STARCLI_ARCHIVE='$remote_archive' STARCLI_ARCHIVE_SHA256='$STAR_ARCHIVE_SHA256' bash /tmp/starcli-live-regression.sh" \
      2>&1 | tee "$gate_log"
    gate_rc=${PIPESTATUS[0]}
    set -e
    test "$gate_rc" -eq 0
    test "$(cat "${state}.exit")" = 0
    test "$(cat "${state}.finalized")" = 0
    grep -q '^STARCLI_LIVE_REGRESSION_PASS ' "$gate_log"
    cleanup_star_live
    for _ in $(seq 1 30); do
      verify_star_live_clean && break
      sleep 1
    done
    verify_star_live_clean
    rm -f "${state}.pid" "${state}.exit" "${state}.finalized"
    echo "TOOLS_CLEAN_VERIFY_PASS scope=starcli host=$STAR_HOST" | tee -a "$gate_log"
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
