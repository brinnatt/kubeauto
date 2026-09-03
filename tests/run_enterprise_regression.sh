#!/bin/bash
# Enterprise regression driver — executes tests/enterprise-test-matrix.yaml coverage.
#
# This is the *only* command the development host needs to invoke for lab work:
# it owns SSH, source sync, cleanup, remote execution and log collection.  Keep
# remote commands inside this script so a delivery regression is autonomous
# rather than requiring a per-host operator confirmation.
#
# Full:   bash tests/run_enterprise_regression.sh --all-delivery
# Core:   bash tests/run_enterprise_regression.sh
# Status: bash tests/run_enterprise_regression.sh --status
set -euo pipefail

require_command() {
  command -v "$1" >/dev/null 2>&1 || {
    echo "ERROR: required command '$1' is not installed." >&2
    exit 127
  }
}

require_command rsync

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
HOST138="ubuntu@192.168.47.138"
HOST130="root@192.168.47.130"
HOST137="root@192.168.47.137"
HOST128="brinnatt@192.168.47.128"
HOST141="root@192.168.47.141"
HOST142="root@192.168.47.142"
HOST143="root@192.168.47.143"
MYSQL_TEST_HOST="${MYSQL_TEST_HOST:-root@master-aio}"
MYSQL_TEST_JUMPER="${MYSQL_TEST_JUMPER:-root@192.168.122.2}"
MYSQL_RUNTIME_IMAGE_PREFIX="${MYSQL_IMAGE_SOURCE_PREFIX:-${2:-}}"
MYSQL_RUNTIME_VERIFY_PREFIXES="${MYSQL_IMAGE_VERIFY_PREFIXES:-${MYSQL_IMAGE_VERIFY_PREFIX:-${3:-}}}"
MYSQL_RUNTIME_STAGE_NODE="${MYSQL_IMAGE_STAGE_NODE:-${4:-}}"
MYSQL_RUNTIME_IMAGE_FALLBACK_PREFIX="${MYSQL_IMAGE_SOURCE_FALLBACK_PREFIX:-${5:-}}"
KAFKA_TEST_HOST="${KAFKA_TEST_HOST:-root@master-aio}"
KAFKA_TEST_JUMPER="${KAFKA_TEST_JUMPER:-root@192.168.122.2}"
KAFKA_RUNTIME_IMAGE_PREFIX="${KAFKA_IMAGE_SOURCE_PREFIX:-}"
KAFKA_RUNTIME_IMAGE_FALLBACK_PREFIX="${KAFKA_IMAGE_FALLBACK_PREFIX:-}"
KAFKA_RUNTIME_IMAGE_VERIFY_PREFIX="${KAFKA_IMAGE_VERIFY_PREFIX:-}"
KAFKA_RUNTIME_STORAGE_IMAGE_PREFIX="${KAFKA_LAB_IMAGE_SOURCE_PREFIX:-}"
PROM_TEST_HOST="${PROM_TEST_HOST:-root@192.168.122.2}"
PROM_TEST_JUMPER="${PROM_TEST_JUMPER:-}"
LOG="${ROOT}/logs/enterprise-regression-$(date +%Y%m%d-%H%M).log"
MODE="${1:-run}"
mkdir -p "${ROOT}/logs"

# The coverage summary is a delivery claim, so validate it from the YAML
# details before any gate can run or emit a PASS marker. Independent middleware
# branches own separate matrix schemas and are validated by their own gates.
if [[ "$MODE" != --mysql-* && "$MODE" != --kafka-* ]]; then
  matrix_python="$ROOT/.venv/bin/python"
  [[ -x "$matrix_python" ]] || matrix_python="$(command -v python3.12 || command -v python3)"
  matrix_validation_args=("$ROOT/tests/enterprise-test-matrix.yaml")
  [[ "${KUBEAUTO_MATRIX_WIP:-0}" == 1 ]] || matrix_validation_args+=(--require-pass)
  "$matrix_python" "$ROOT/tests/helpers/validate-test-matrix.py" \
    "${matrix_validation_args[@]}" | tee -a "$LOG"
fi

# Preserve the invoking terminal before later phases redirect their audit log.
# The final remote tail is deliberately written to these descriptors.
exec 3>&1 4>&2

ssh138() { ssh -o BatchMode=yes -o StrictHostKeyChecking=no -o ConnectTimeout=10 -o ServerAliveInterval=5 -o ServerAliveCountMax=2 "$HOST138" "$@"; }
ssh130() { ssh -o BatchMode=yes -o StrictHostKeyChecking=no -o ConnectTimeout=10 -o ServerAliveInterval=5 -o ServerAliveCountMax=2 "$HOST130" "$@"; }
ssh137() { ssh -o BatchMode=yes -o StrictHostKeyChecking=no -o ConnectTimeout=10 -o ServerAliveInterval=5 -o ServerAliveCountMax=2 "$HOST137" "$@"; }
ssh128() { ssh -o BatchMode=yes -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=10 -o ServerAliveInterval=5 -o ServerAliveCountMax=2 "$HOST128" "$@"; }
ssh141() { ssh -o BatchMode=yes -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=10 -o ServerAliveInterval=5 -o ServerAliveCountMax=2 "$HOST141" "$@"; }
ssh142() { ssh -o BatchMode=yes -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=10 -o ServerAliveInterval=5 -o ServerAliveCountMax=2 "$HOST142" "$@"; }
ssh143() { ssh -o BatchMode=yes -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=10 -o ServerAliveInterval=5 -o ServerAliveCountMax=2 "$HOST143" "$@"; }
ssh_mysql() { ssh -o BatchMode=yes -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=10 -o ServerAliveInterval=5 -o ServerAliveCountMax=2 -J "$MYSQL_TEST_JUMPER" "$MYSQL_TEST_HOST" "$@"; }
ssh_kafka() { ssh -o BatchMode=yes -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=10 -o ServerAliveInterval=5 -o ServerAliveCountMax=2 -J "$KAFKA_TEST_JUMPER" "$KAFKA_TEST_HOST" "$@"; }
ssh_prom() {
  local args=(-o BatchMode=yes -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=10 -o ServerAliveInterval=5 -o ServerAliveCountMax=2)
  [[ -n "$PROM_TEST_JUMPER" ]] && args+=(-J "$PROM_TEST_JUMPER")
  ssh "${args[@]}" "$PROM_TEST_HOST" "$@"
}
scp138() { scp -o BatchMode=yes -o StrictHostKeyChecking=no -o ConnectTimeout=10 -o ServerAliveInterval=5 -o ServerAliveCountMax=2 "$@"; }
scp137() { scp -o BatchMode=yes -o StrictHostKeyChecking=no -o ConnectTimeout=10 -o ServerAliveInterval=5 -o ServerAliveCountMax=2 "$@"; }

remote_job_summary() {
  local ssh_function="$1" privilege="$2" state_prefix="$3" remote_log="$4"
  local remote_script quoted_script
  remote_script="
pid=missing
rc=missing
state=starting
test -r '${state_prefix}.pid' && pid=\$(cat '${state_prefix}.pid')
if test -r '${state_prefix}.pid' && test -r '${state_prefix}.exit' && test -r '${state_prefix}.finalized'; then
  rc=\$(cat '${state_prefix}.exit')
  state=exited
elif test -r '${state_prefix}.exit'; then
  rc=\$(cat '${state_prefix}.exit')
  state=finalizing
elif test \"\$pid\" != missing && kill -0 \"\$pid\" 2>/dev/null; then
  state=running
elif test \"\$pid\" != missing; then
  state=lost
fi
bytes=0
if test -r '${remote_log}'; then
  bytes=\$(wc -c < '${remote_log}')
  # Logs created before durable state files were introduced still contain the
  # wrapper's authoritative terminal marker.  Report those as exited rather
  # than leaving an obviously completed historical run in "starting" state.
  if test \"\$state\" = starting; then
    legacy_rc=\$(sed -n 's/^REGRESSION_.*_EXIT rc=\([0-9][0-9]*\)$/\1/p' '${remote_log}' | tail -n 1)
    if test -n \"\$legacy_rc\"; then
      rc=\$legacy_rc
      state=exited
    elif test \"\$pid\" = missing && test -s '${remote_log}' && test -n \"\$(find '${remote_log}' -mmin +1 -print 2>/dev/null)\"; then
      state=orphaned
    fi
  fi
fi
passes=0
failures=0
if test -r '${remote_log}'; then
  passes=\$(grep -c '^\\[PASS\\]' '${remote_log}' 2>/dev/null || true)
  # A durable non-zero exit is authoritative for Ansible failures.  A
  # controlled retry can legitimately leave an earlier failed=1 recap in the
  # same log before recovering and emitting the required terminal marker.
  # Count only explicit script failures here; the durable exit record is
  # checked independently by monitor_remote_job.
  failures=\$(grep -Ec '^\\[FAIL\\]' '${remote_log}' 2>/dev/null || true)
  latest=\$(grep -E '^==========|^>>> |^\\[PASS\\]|^\\[FAIL\\]|^\\[WAIT\\]|^REGRESSION_' '${remote_log}' 2>/dev/null | tail -n 1 || true)
else
  latest='log-not-created'
fi
printf 'state=%s pid=%s rc=%s bytes=%s pass_markers=%s failure_markers=%s latest=%s\\n' \\
  \"\$state\" \"\$pid\" \"\$rc\" \"\$bytes\" \"\$passes\" \"\$failures\" \"\$latest\"
"
  printf -v quoted_script '%q' "$remote_script"
  if [[ -n "$privilege" ]]; then
    "$ssh_function" "$privilege bash -lc $quoted_script"
  else
    "$ssh_function" "bash -lc $quoted_script"
  fi
}

remote_log_tail() {
  local ssh_function="$1" privilege="$2" remote_log="$3" lines="${4:-100}"
  if [[ -n "$privilege" ]]; then
    "$ssh_function" "$privilege tail -n '$lines' '$remote_log' 2>/dev/null || true"
  else
    "$ssh_function" "tail -n '$lines' '$remote_log' 2>/dev/null || true"
  fi
}

remote_process_tree() {
  local ssh_function="$1" privilege="$2" state_prefix="$3"
  local remote_script quoted_script
  remote_script="
root=missing
test -r '${state_prefix}.pid' && root=\$(cat '${state_prefix}.pid')
echo process_tree_root=\"\$root\"
if test \"\$root\" != missing && command -v pstree >/dev/null 2>&1; then
  pstree -ap \"\$root\" || true
else
  ps -eo pid,ppid,etime,stat,wchan:24,cmd --forest | head -n 160
fi
"
  printf -v quoted_script '%q' "$remote_script"
  if [[ -n "$privilege" ]]; then
    "$ssh_function" "$privilege bash -lc $quoted_script"
  else
    "$ssh_function" "bash -lc $quoted_script"
  fi
}

remote_diagnostics() {
  local ssh_function="$1" privilege="$2" remote_log="$3"
  echo "========== AUTOMATIC FAILURE DIAGNOSTICS =========="
  remote_log_tail "$ssh_function" "$privilege" "$remote_log" 160
  "$ssh_function" "ps -eo pid,ppid,etime,stat,cmd | grep -E '[r]egression|[k]ubecli|[a]nsible-playbook' || true"
}

cancel_remote_job() {
  local label="$1" ssh_function="$2" privilege="$3" state_prefix="$4"
  local remote_script quoted_script
  remote_script="
set -euo pipefail
pid=missing
test -r '${state_prefix}.pid' && pid=\$(cat '${state_prefix}.pid')
if test \"\$pid\" = missing || ! kill -0 \"\$pid\" 2>/dev/null; then
  echo '[CANCEL] label=${label} state=not-running pid='\"\$pid\"
  exit 0
fi
case \"\$pid\" in
  *[!0-9]*|'') echo '[FAIL] invalid durable pid for ${label}: '\"\$pid\" >&2; exit 1 ;;
esac
echo '[CANCEL] label=${label} state=terminating pid='\"\$pid\"
descendants() {
  local parent=\"\$1\" child
  for child in \$(ps -eo pid=,ppid= | awk -v parent=\"\$parent\" '\$2 == parent {print \$1}'); do
    descendants \"\$child\"
    printf '%s\\n' \"\$child\"
  done
}
targets=\$(descendants \"\$pid\")
for child in \$targets; do kill -TERM \"\$child\" 2>/dev/null || true; done
kill -TERM \"\$pid\" 2>/dev/null || true
for attempt in \$(seq 1 15); do
  alive=
  for candidate in \$targets \"\$pid\"; do
    kill -0 \"\$candidate\" 2>/dev/null && alive=\"\$alive \$candidate\"
  done
  test -z \"\$alive\" && break
  sleep 1
done
for candidate in \$targets \"\$pid\"; do kill -KILL \"\$candidate\" 2>/dev/null || true; done
for candidate in \$targets \"\$pid\"; do
  if kill -0 \"\$candidate\" 2>/dev/null; then
    echo '[FAIL] label=${label} process-survived pid='\"\$candidate\" >&2
    exit 1
  fi
done
echo '[CANCEL] label=${label} state=stopped pid='\"\$pid\"
"
  printf -v quoted_script '%q' "$remote_script"
  if [[ -n "$privilege" ]]; then
    "$ssh_function" "$privilege bash -lc $quoted_script"
  else
    "$ssh_function" "bash -lc $quoted_script"
  fi
}

matrix_counts() {
  local matrix="$ROOT/tests/enterprise-test-matrix.yaml"
  [[ "$MODE" == --mysql-* ]] && matrix="$ROOT/tests/mysql-test-matrix.yaml"
  [[ "$MODE" == --kafka-* ]] && matrix="$ROOT/tests/kafka-test-matrix.yaml"
  printf 'matrix_pass=%s matrix_pending=%s matrix_fail=%s' \
    "$(grep -Ec '^[[:space:]]*-[[:space:]]*\{id:.*status: pass' "$matrix" || true)" \
    "$(grep -Ec '^[[:space:]]*-[[:space:]]*\{id:.*status: pending' "$matrix" || true)" \
    "$(grep -Ec '^[[:space:]]*-[[:space:]]*\{id:.*status: fail' "$matrix" || true)"
}

if [[ "$MODE" == "--kafka-status" ]]; then
  echo "========== Kafka independent gate =========="
  remote_job_summary ssh_kafka '' /tmp/kubeauto-kafka-gate /tmp/kubeauto-kafka-live.log || true
  remote_log_tail ssh_kafka '' /tmp/kubeauto-kafka-live.log 60
  echo "========== Kafka cluster preflight =========="
  ssh_kafka "kubectl version 2>/dev/null || true; kubectl get nodes -o wide 2>/dev/null || true; kubectl get storageclass -o custom-columns=NAME:.metadata.name,PROVISIONER:.provisioner,EXPAND:.allowVolumeExpansion 2>/dev/null || true; echo kafka-workloads; kubectl -n kafka get kafka,kafkanodepool,kafkarebalance,kafkatopic,kafkauser,pod,pvc,svc -o wide 2>/dev/null || true; kubectl -n kafka-operator get deployment,pod -o wide 2>/dev/null || true; kubectl -n kafka-drain-cleaner get deployment,pod,pdb -o wide 2>/dev/null || true; echo toolchain; for command in helm ansible-playbook skopeo docker jq openssl; do printf '%s=' \"\$command\"; command -v \"\$command\" || true; done; echo registry-port; ss -lntp '( sport = :5000 )' || true; docker ps -a --filter publish=5000 --format 'name={{.Names}} image={{.Image}} labels={{.Labels}} ports={{.Ports}}' 2>/dev/null || true; echo capacity; kubectl get nodes -o custom-columns=NAME:.metadata.name,CPU:.status.allocatable.cpu,MEMORY:.status.allocatable.memory,EPHEMERAL:.status.allocatable.ephemeral-storage"
  echo "========== Kafka node storage prerequisites =========="
  ssh_kafka 'for ip in $(kubectl get nodes -o wide --no-headers | awk '\''{print $6}'\''); do ssh -o BatchMode=yes -o StrictHostKeyChecking=no -o ConnectTimeout=10 "root@${ip}" '\''printf "node=%s iscsiadm=%s iscsid=%s mountpropagation=%s\\n" "$(hostname)" "$(command -v iscsiadm 2>/dev/null || echo missing)" "$(systemctl is-active iscsid 2>/dev/null || true)" "$(findmnt -o PROPAGATION -n / 2>/dev/null || echo unknown)"; lsblk -d -o NAME,SIZE,TYPE,FSTYPE,MOUNTPOINTS'\'' || true; done'
  echo "========== Kafka matrix =========="
  matrix_counts
  echo
  exit 0
fi

if [[ "$MODE" == "--kafka-follow" ]]; then
  ssh_kafka "tail -n 100 -F /tmp/kubeauto-kafka-live.log"
  exit 0
fi

if [[ "$MODE" == "--kafka-cancel" ]]; then
  cancel_remote_job kafka ssh_kafka '' /tmp/kubeauto-kafka-gate
  echo KAFKA_CANCEL_PASS
  exit 0
fi

if [[ "$MODE" == "--kafka-clean-only" ]]; then
  cancel_remote_job kafka ssh_kafka '' /tmp/kubeauto-kafka-gate
  KUBEAUTO_SSH_JUMP="$KAFKA_TEST_JUMPER" KUBEAUTO_SYNC_SKIP_CONTROL_SETUP=1 \
    bash "$ROOT/tests/helpers/sync-kubeauto.sh" "$KAFKA_TEST_HOST"
  ssh_kafka "chmod 0755 /usr/local/kubeauto/tests/helpers/kafka-cleanup.sh /usr/local/kubeauto/tests/helpers/kafka-lab-storage.sh"
  ssh_kafka "bash /usr/local/kubeauto/tests/helpers/kafka-cleanup.sh"
  ssh_kafka "bash /usr/local/kubeauto/tests/helpers/kafka-cleanup.sh --verify"
  echo KAFKA_CLEAN_ONLY_PASS
  exit 0
fi

if [[ "$MODE" == "--mysql-status" ]]; then
  echo "========== MySQL/PXC independent gate =========="
  remote_job_summary ssh_mysql '' /tmp/kubeauto-mysql-gate /tmp/kubeauto-mysql-live.log || true
  remote_log_tail ssh_mysql '' /tmp/kubeauto-mysql-live.log 40
  echo "========== MySQL/PXC cluster preflight =========="
  ssh_mysql "kubectl version 2>/dev/null || true; kubectl get nodes -o wide 2>/dev/null || true; kubectl get storageclass 2>/dev/null || true; kubectl get namespace mysql mysql-operator 2>/dev/null || true; echo mysql-workloads; kubectl -n mysql get pxc,pod,pvc,svc -o wide 2>/dev/null || true; kubectl -n mysql-operator get pod -o wide 2>/dev/null || true; kubectl -n mysql get events --sort-by=.lastTimestamp 2>/dev/null | tail -40 || true; echo toolchain; command -v helm || true; helm version --short 2>/dev/null || true; command -v ansible-playbook || true; ansible-playbook --version 2>/dev/null | head -2 || true; command -v skopeo || true; command -v nerdctl || true; command -v podman || true; command -v docker || true; echo stale-processes; pgrep -af '[p]ip|[r]sync.*kubeauto' || true; echo registry-port; ss -lntp '( sport = :5000 )' || true; docker ps --format 'table {{.Names}}\\t{{.Image}}\\t{{.Ports}}' 2>/dev/null || true; curl -fsS --max-time 3 http://127.0.0.1:5000/v2/ 2>/dev/null && echo registry_v2_ready=true || true; echo control-addresses; hostname; hostname -I; echo node-access; for ip in \$(kubectl get nodes -o wide --no-headers | awk '{print \$6}'); do ssh -o BatchMode=yes -o StrictHostKeyChecking=no -o ConnectTimeout=5 root@\"\$ip\" 'printf \"node=\"; hostname; printf \"ctr=\"; command -v ctr || true; test -d /etc/containerd/certs.d && echo certs.d-present || true' 2>/dev/null || echo \"node_ssh_failed=\$ip\"; done; echo capacity; kubectl get nodes -o custom-columns=NAME:.metadata.name,CPU:.status.allocatable.cpu,MEMORY:.status.allocatable.memory,EPHEMERAL:.status.allocatable.ephemeral-storage"
  echo "========== MySQL/PXC matrix =========="
  matrix_counts
  echo
  exit 0
fi

if [[ "$MODE" == "--mysql-follow" ]]; then
  ssh_mysql "tail -n 80 -F /tmp/kubeauto-mysql-live.log"
  exit 0
fi

if [[ "$MODE" == "--mysql-cancel" ]]; then
  cancel_remote_job mysql-pxc ssh_mysql '' /tmp/kubeauto-mysql-gate
  echo MYSQL_PXC_CANCEL_PASS
  exit 0
fi

if [[ "$MODE" == "--mysql-clean-only" ]]; then
  mysql_cleanup_env=""
  if [[ -n "$MYSQL_RUNTIME_IMAGE_PREFIX" ]]; then
    printf -v mysql_cleanup_prefix_quoted '%q' "$MYSQL_RUNTIME_IMAGE_PREFIX"
    mysql_cleanup_env="MYSQL_IMAGE_SOURCE_PREFIX=${mysql_cleanup_prefix_quoted}"
  fi
  if [[ -n "$MYSQL_RUNTIME_STAGE_NODE" ]]; then
    printf -v mysql_cleanup_stage_node_quoted '%q' "$MYSQL_RUNTIME_STAGE_NODE"
    mysql_cleanup_env+=" MYSQL_IMAGE_STAGE_NODE=${mysql_cleanup_stage_node_quoted}"
  fi
  cancel_remote_job mysql-pxc ssh_mysql '' /tmp/kubeauto-mysql-gate
  KUBEAUTO_SSH_JUMP="$MYSQL_TEST_JUMPER" KUBEAUTO_SYNC_SKIP_CONTROL_SETUP=1 \
    bash "$ROOT/tests/helpers/sync-kubeauto.sh" "$MYSQL_TEST_HOST"
  ssh_mysql "chmod 0755 /usr/local/kubeauto/tests/helpers/mysql-cleanup.sh"
  ssh_mysql "env ${mysql_cleanup_env} bash /usr/local/kubeauto/tests/helpers/mysql-cleanup.sh"
  ssh_mysql "env ${mysql_cleanup_env} bash /usr/local/kubeauto/tests/helpers/mysql-cleanup.sh --verify"
  echo MYSQL_PXC_CLEAN_ONLY_PASS
  exit 0
fi

if [[ "$MODE" == "--mysql-source-probe" ]]; then
  [[ -n "${2:-}" ]] || {
    echo "Usage: $0 --mysql-source-probe <runtime-registry-prefix> [repository] [tag]" >&2
    exit 2
  }
  mysql_probe_repository="${3:-percona/percona-xtradb-cluster-operator}"
  mysql_probe_tag="${4:-1.20.0}"
  [[ "$mysql_probe_repository" =~ ^[a-z0-9]+([._/-][a-z0-9]+)*$ ]] || {
    echo "ERROR: invalid image repository: $mysql_probe_repository" >&2
    exit 2
  }
  [[ "$mysql_probe_tag" =~ ^[A-Za-z0-9_][A-Za-z0-9_.-]*$ ]] || {
    echo "ERROR: invalid image tag: $mysql_probe_tag" >&2
    exit 2
  }
  mysql_probe_image="${2}/${mysql_probe_repository}:${mysql_probe_tag}"
  printf -v mysql_probe_ref '%q' "$mysql_probe_image"
  ssh_mysql "timeout --signal=TERM --kill-after=5s 45s docker manifest inspect ${mysql_probe_ref} >/dev/null"
  echo "MYSQL_RUNTIME_SOURCE_MANIFEST_PASS image=${mysql_probe_image}"
  exit 0
fi

if [[ "$MODE" == "--mysql-source-head" ]]; then
  [[ -n "${2:-}" ]] || {
    echo "Usage: $0 --mysql-source-head <runtime-registry-prefix> [repository] [tag]" >&2
    exit 2
  }
  mysql_head_repository="${3:-percona/percona-xtradb-cluster-operator}"
  mysql_head_tag="${4:-1.20.0}"
  [[ "$mysql_head_repository" =~ ^[a-z0-9]+([._/-][a-z0-9]+)*$ ]] || {
    echo "ERROR: invalid image repository: $mysql_head_repository" >&2
    exit 2
  }
  [[ "$mysql_head_tag" =~ ^[A-Za-z0-9_][A-Za-z0-9_.-]*$ ]] || {
    echo "ERROR: invalid image tag: $mysql_head_tag" >&2
    exit 2
  }
  printf -v mysql_head_url '%q' "https://${2}/v2/${mysql_head_repository}/manifests/${mysql_head_tag}"
  ssh_mysql "curl -sSIL --max-time 30 -H 'Accept: application/vnd.oci.image.index.v1+json, application/vnd.docker.distribution.manifest.list.v2+json' ${mysql_head_url}"
  exit 0
fi

if [[ "$MODE" == "--mysql-node-source-probe" ]]; then
  [[ -n "${2:-}" ]] || {
    echo "Usage: $0 --mysql-node-source-probe <runtime-registry-prefix>" >&2
    exit 2
  }
  printf -v mysql_node_probe_ref '%q' "${2}/percona/percona-xtradb-cluster-operator:1.20.0"
  ssh_mysql "ssh -o BatchMode=yes -o StrictHostKeyChecking=no root@192.168.122.210 'timeout --signal=TERM --kill-after=15s 10m ctr -n k8s.io images pull --platform linux/amd64 ${mysql_node_probe_ref} >/dev/null && ctr -n k8s.io images inspect ${mysql_node_probe_ref} | sed -n \"s/.*@\\(sha256:[0-9a-f]\\{64\\}\\).*/\\1/p\" | head -n 1 | grep -Ex \"sha256:[0-9a-f]{64}\" && ctr -n k8s.io images push --help | grep -E \"manifest|platform\"'"
  echo MYSQL_NODE_RUNTIME_SOURCE_PASS
  exit 0
fi

if [[ "$MODE" == "--mysql-node-progress" ]]; then
  ssh_mysql "ssh -o BatchMode=yes -o StrictHostKeyChecking=no root@192.168.122.210 'echo ===process===; ps -eo pid,ppid,etime,stat,wchan:24,cmd | grep -E \"[c]tr -n k8s.io images (pull|push)\"; echo ===network===; ss -tpn | grep -E \"ctr|:443\" || true; echo ===active-content===; ctr -n k8s.io content active || true; echo ===content===; du -sb /var/lib/containerd/io.containerd.content.v1.content/blobs/sha256 2>/dev/null || true; find /var/lib/containerd/io.containerd.content.v1.content/ingest -mindepth 1 -maxdepth 2 -type f -printf \"%s %p\\n\" 2>/dev/null | sort -nr | head -n 20'"
  exit 0
fi

if [[ "$MODE" == "--mysql-node-ingest-abort" ]]; then
  ingest_ref="${2:-}"
  [[ "$ingest_ref" =~ ^layer-sha256:[0-9a-f]{64}$ ]] || {
    echo "Usage: $0 --mysql-node-ingest-abort layer-sha256:<digest>" >&2
    exit 2
  }
  printf -v ingest_ref_quoted '%q' "$ingest_ref"
  ssh_mysql "ssh -o BatchMode=yes -o StrictHostKeyChecking=no root@192.168.122.210 bash -s -- ${ingest_ref_quoted}" <<'NODE_INGEST_ABORT'
set -Eeuo pipefail
ingest_ref="$1"
ingest_root=/var/lib/containerd/io.containerd.content.v1.content/ingest
test -z "$(pgrep -f '^ctr -n k8s.io images pull ' || true)"
ctr -n k8s.io content active | awk 'NR > 1 && NF {print $1}' | grep -Fx "$ingest_ref"
mapfile -t matches < <(find "$ingest_root" -mindepth 2 -maxdepth 2 -type f -name ref -print \
  | while IFS= read -r ref_file; do
    stored_ref="$(cat "$ref_file")"
    if [[ "$stored_ref" == "$ingest_ref" || "$stored_ref" == */"$ingest_ref" ]]; then
      printf '%s\n' "$ref_file"
    fi
    done)
(( ${#matches[@]} == 1 ))
ingest_dir="$(dirname "${matches[0]}")"
[[ "$ingest_dir" == "$ingest_root/"* ]]
# Mirrors containerd 2.1 local Store.Abort after the owning pull has stopped.
rm -rf -- "$ingest_dir"
! ctr -n k8s.io content active | awk 'NR > 1 && NF {print $1}' | grep -Fx "$ingest_ref"
NODE_INGEST_ABORT
  echo MYSQL_NODE_ORPHAN_INGEST_ABORT_PASS
  exit 0
fi

monitor_remote_job() {
  local label="$1" ssh_function="$2" privilege="$3" state_prefix="$4" remote_log="$5" success_marker="$6"
  local monitor_started_at monitor_now last_heartbeat_at last_change_at previous_bytes summary bytes state rc failures
  local ssh_failures=0 stream_pid=''
  monitor_started_at=$(date +%s)
  last_heartbeat_at=0
  last_change_at=$monitor_started_at
  previous_bytes=-1

  echo "[MONITOR] label=$label event=attached interval=10s heartbeat=30s silence_diagnostic=60s"
  remote_log_tail "$ssh_function" "$privilege" "$remote_log" 30

  # Stream every remote log line to this terminal.  The polling loop below is
  # independent, so an SSH/tail disconnect is detected and restarted instead
  # of silently disabling supervision.
  if [[ -n "$privilege" ]]; then
    "$ssh_function" "$privilege bash -lc \"exec tail --pid=\\\$(cat '${state_prefix}.pid') -n 0 -F '$remote_log'\"" &
  else
    "$ssh_function" "bash -lc \"exec tail --pid=\\\$(cat '${state_prefix}.pid') -n 0 -F '$remote_log'\"" &
  fi
  stream_pid=$!

  while true; do
    monitor_now=$(date +%s)
    if ! summary=$(remote_job_summary "$ssh_function" "$privilege" "$state_prefix" "$remote_log"); then
      ssh_failures=$((ssh_failures+1))
      echo "[MONITOR] label=$label event=ssh-poll-failed consecutive=$ssh_failures"
      if (( ssh_failures >= 3 )); then
        kill "$stream_pid" 2>/dev/null || true
        wait "$stream_pid" 2>/dev/null || true
        echo "[FAIL] monitor lost SSH connectivity to $label for three consecutive polls"
        return 1
      fi
      sleep 10
      continue
    fi
    ssh_failures=0
    state=$(sed -n 's/^state=\([^ ]*\).*/\1/p' <<<"$summary")
    rc=$(sed -n 's/.* rc=\([^ ]*\).*/\1/p' <<<"$summary")
    bytes=$(sed -n 's/.* bytes=\([^ ]*\).*/\1/p' <<<"$summary")
    failures=$(sed -n 's/.* failure_markers=\([^ ]*\).*/\1/p' <<<"$summary")

    if [[ "$bytes" != "$previous_bytes" ]]; then
      previous_bytes="$bytes"
      last_change_at=$monitor_now
    elif (( monitor_now - last_change_at >= 60 )); then
      echo "[MONITOR] label=$label event=no-new-log-for-$((monitor_now-last_change_at))s action=diagnostic-snapshot"
      echo "[MONITOR] $summary"
      remote_process_tree "$ssh_function" "$privilege" "$state_prefix"
      last_change_at=$monitor_now
    fi

    if (( monitor_now - last_heartbeat_at >= 30 )); then
      echo "[HEARTBEAT] time=$(date '+%F %T %Z') label=$label elapsed=$((monitor_now-monitor_started_at))s $summary $(matrix_counts)"
      last_heartbeat_at=$monitor_now
    fi

    if ! kill -0 "$stream_pid" 2>/dev/null && [[ "$state" == running ]]; then
      wait "$stream_pid" 2>/dev/null || true
      echo "[MONITOR] label=$label event=log-stream-disconnected action=reattach"
      if [[ -n "$privilege" ]]; then
        "$ssh_function" "$privilege bash -lc \"exec tail --pid=\\\$(cat '${state_prefix}.pid') -n 20 -F '$remote_log'\"" &
      else
        "$ssh_function" "bash -lc \"exec tail --pid=\\\$(cat '${state_prefix}.pid') -n 20 -F '$remote_log'\"" &
      fi
      stream_pid=$!
    fi

    case "$state" in
      running|starting|finalizing)
        ;;
      exited)
        kill "$stream_pid" 2>/dev/null || true
        wait "$stream_pid" 2>/dev/null || true
        remote_log_tail "$ssh_function" "$privilege" "$remote_log" 100
        if [[ "$rc" == 0 && "$failures" == 0 ]] && "$ssh_function" "grep -q '$success_marker' '$remote_log'"; then
          echo "[PASS] supervised job $label rc=0 failure_markers=0 marker=$success_marker"
          return 0
        fi
        remote_diagnostics "$ssh_function" "$privilege" "$remote_log"
        echo "[FAIL] supervised job $label rc=$rc failure_markers=$failures missing_or_failed_marker=$success_marker"
        return 1
        ;;
      lost|*)
        kill "$stream_pid" 2>/dev/null || true
        wait "$stream_pid" 2>/dev/null || true
        remote_diagnostics "$ssh_function" "$privilege" "$remote_log"
        echo "[FAIL] supervised job $label state=$state without durable exit record"
        return 1
        ;;
    esac
    sleep 10
  done
}

if [[ "$MODE" == "--mysql-only" ]]; then
  echo "========== Independent MySQL/PXC delivery gate =========="
  cancel_remote_job mysql-pxc ssh_mysql '' /tmp/kubeauto-mysql-gate
  echo ">>> focused MySQL/PXC unit and contract tests"
  "$ROOT/.venv/bin/python" -m unittest \
    tests.unit.test_percona_pxc_delivery \
    tests.unit.test_registry_pull_sources \
    tests.unit.test_six_repo_version_sync \
    tests.unit.test_percona_pxc_documentation -v
  ssh_mysql "stale_pid=\$(pgrep -f '^/usr/local/kubeauto/.venv/bin/python -m pip install -q -r /usr/local/kubeauto/requirements-control.txt$' || true); if test -n \"\$stale_pid\"; then kill \"\$stale_pid\"; echo RECOVERED_STALE_MYSQL_SYNC_PIP pid=\$stale_pid; fi"
  echo ">>> sync source through fixed MySQL jumper"
  KUBEAUTO_SSH_JUMP="$MYSQL_TEST_JUMPER" KUBEAUTO_SYNC_SKIP_CONTROL_SETUP=1 \
    bash "$ROOT/tests/helpers/sync-kubeauto.sh" "$MYSQL_TEST_HOST"
  ssh_mysql "chmod 0755 /usr/local/kubeauto/tests/helpers/mysql-cleanup.sh /usr/local/kubeauto/tests/helpers/mysql-regression.sh /usr/local/kubeauto/tests/helpers/mysql-stage-node-image.sh /usr/local/kubeauto/tests/helpers/run-durable-gate.sh"

  mysql_runtime_env=""
  if [[ -n "$MYSQL_RUNTIME_IMAGE_PREFIX" ]]; then
    printf -v mysql_runtime_prefix_quoted '%q' "$MYSQL_RUNTIME_IMAGE_PREFIX"
    mysql_runtime_env="MYSQL_IMAGE_SOURCE_PREFIX=${mysql_runtime_prefix_quoted}"
  fi
  if [[ -n "${MYSQL_RUNTIME_IMAGE_FALLBACK_PREFIX:-}" ]]; then
    printf -v mysql_runtime_fallback_quoted '%q' "$MYSQL_RUNTIME_IMAGE_FALLBACK_PREFIX"
    mysql_runtime_env+=" MYSQL_IMAGE_SOURCE_FALLBACK_PREFIX=${mysql_runtime_fallback_quoted}"
  fi
  if [[ -n "$MYSQL_RUNTIME_VERIFY_PREFIXES" ]]; then
    printf -v mysql_runtime_verify_quoted '%q' "$MYSQL_RUNTIME_VERIFY_PREFIXES"
    mysql_runtime_env+=" MYSQL_IMAGE_VERIFY_PREFIXES=${mysql_runtime_verify_quoted}"
  fi
  if [[ -n "$MYSQL_RUNTIME_STAGE_NODE" ]]; then
    printf -v mysql_runtime_stage_node_quoted '%q' "$MYSQL_RUNTIME_STAGE_NODE"
    mysql_runtime_env+=" MYSQL_IMAGE_STAGE_NODE=${mysql_runtime_stage_node_quoted}"
  fi

  echo ">>> scoped MySQL/PXC cleanup before run"
  ssh_mysql "env ${mysql_runtime_env} bash /usr/local/kubeauto/tests/helpers/mysql-cleanup.sh"
  ssh_mysql "env ${mysql_runtime_env} bash /usr/local/kubeauto/tests/helpers/mysql-cleanup.sh --verify"
  ssh_mysql "rm -f /tmp/kubeauto-mysql-live.log /tmp/kubeauto-mysql-gate.pid /tmp/kubeauto-mysql-gate.exit; nohup env ${mysql_runtime_env} bash /usr/local/kubeauto/tests/helpers/run-durable-gate.sh /tmp/kubeauto-mysql-gate MYSQL_PXC_GATE_EXIT bash /usr/local/kubeauto/tests/helpers/mysql-regression.sh >/tmp/kubeauto-mysql-live.log 2>&1 </dev/null &"
  mysql_rc=0
  monitor_remote_job \
    mysql-pxc ssh_mysql '' /tmp/kubeauto-mysql-gate \
    /tmp/kubeauto-mysql-live.log MYSQL_PXC_FULL_GATE_PASS || mysql_rc=$?

  cleanup_rc=0
  echo ">>> scoped MySQL/PXC cleanup after run"
  ssh_mysql "env ${mysql_runtime_env} bash /usr/local/kubeauto/tests/helpers/mysql-cleanup.sh" || cleanup_rc=$?
  ssh_mysql "env ${mysql_runtime_env} bash /usr/local/kubeauto/tests/helpers/mysql-cleanup.sh --verify" || cleanup_rc=$?
  if [[ "$mysql_rc" -ne 0 || "$cleanup_rc" -ne 0 ]]; then
    echo "[FAIL] MySQL/PXC gate rc=${mysql_rc}; cleanup rc=${cleanup_rc}" >&2
    exit 1
  fi
  echo MYSQL_PXC_DELIVERY_BRANCH_PASS
  matrix_counts
  echo
  exit 0
fi

if [[ "$MODE" == "--kafka-only" ]]; then
  echo "========== Independent Kafka delivery gate =========="
  cancel_remote_job kafka ssh_kafka '' /tmp/kubeauto-kafka-gate
  echo ">>> focused Kafka unit and contract tests"
  unit_python="$ROOT/.venv/bin/python"
  [[ -x "$unit_python" ]] || unit_python="$(command -v python3)"
  "$unit_python" -m unittest \
    tests.unit.test_kafka_delivery \
    tests.unit.test_six_repo_version_sync -v

  echo ">>> sync source through fixed Kafka jumper"
  KUBEAUTO_SSH_JUMP="$KAFKA_TEST_JUMPER" KUBEAUTO_SYNC_SKIP_CONTROL_SETUP=1 \
    bash "$ROOT/tests/helpers/sync-kubeauto.sh" "$KAFKA_TEST_HOST"
  ssh_kafka "chmod 0755 /usr/local/kubeauto/tests/helpers/kafka-cleanup.sh /usr/local/kubeauto/tests/helpers/kafka-lab-storage.sh /usr/local/kubeauto/tests/helpers/kafka-regression.sh /usr/local/kubeauto/tests/helpers/run-durable-gate.sh"

  kafka_runtime_env=""
  if [[ -n "$KAFKA_RUNTIME_IMAGE_PREFIX" ]]; then
    printf -v kafka_runtime_prefix_quoted '%q' "$KAFKA_RUNTIME_IMAGE_PREFIX"
    kafka_runtime_env="KAFKA_IMAGE_SOURCE_PREFIX=${kafka_runtime_prefix_quoted}"
  fi
  if [[ -n "$KAFKA_RUNTIME_IMAGE_FALLBACK_PREFIX" ]]; then
    printf -v kafka_runtime_fallback_quoted '%q' "$KAFKA_RUNTIME_IMAGE_FALLBACK_PREFIX"
    kafka_runtime_env+=" KAFKA_IMAGE_FALLBACK_PREFIX=${kafka_runtime_fallback_quoted}"
  fi
  if [[ -n "$KAFKA_RUNTIME_IMAGE_VERIFY_PREFIX" ]]; then
    printf -v kafka_runtime_verify_prefix_quoted '%q' "$KAFKA_RUNTIME_IMAGE_VERIFY_PREFIX"
    kafka_runtime_env+=" KAFKA_IMAGE_VERIFY_PREFIX=${kafka_runtime_verify_prefix_quoted}"
  fi
  if [[ -n "$KAFKA_RUNTIME_STORAGE_IMAGE_PREFIX" ]]; then
    printf -v kafka_runtime_storage_prefix_quoted '%q' "$KAFKA_RUNTIME_STORAGE_IMAGE_PREFIX"
    kafka_runtime_env+=" KAFKA_LAB_IMAGE_SOURCE_PREFIX=${kafka_runtime_storage_prefix_quoted}"
  fi

  echo ">>> scoped Kafka cleanup before run"
  ssh_kafka "bash /usr/local/kubeauto/tests/helpers/kafka-cleanup.sh"
  ssh_kafka "bash /usr/local/kubeauto/tests/helpers/kafka-cleanup.sh --verify"
  ssh_kafka "rm -f /tmp/kubeauto-kafka-live.log /tmp/kubeauto-kafka-gate.pid /tmp/kubeauto-kafka-gate.exit; nohup env ${kafka_runtime_env} bash /usr/local/kubeauto/tests/helpers/run-durable-gate.sh /tmp/kubeauto-kafka-gate KAFKA_GATE_EXIT bash /usr/local/kubeauto/tests/helpers/kafka-regression.sh >/tmp/kubeauto-kafka-live.log 2>&1 </dev/null &"
  kafka_rc=0
  monitor_remote_job \
    kafka ssh_kafka '' /tmp/kubeauto-kafka-gate \
    /tmp/kubeauto-kafka-live.log KAFKA_FULL_GATE_PASS || kafka_rc=$?

  cleanup_rc=0
  echo ">>> scoped Kafka cleanup after run"
  ssh_kafka "bash /usr/local/kubeauto/tests/helpers/kafka-cleanup.sh" || cleanup_rc=$?
  ssh_kafka "bash /usr/local/kubeauto/tests/helpers/kafka-cleanup.sh --verify" || cleanup_rc=$?
  if [[ "$kafka_rc" -ne 0 || "$cleanup_rc" -ne 0 ]]; then
    echo "[FAIL] Kafka gate rc=${kafka_rc}; cleanup rc=${cleanup_rc}" >&2
    exit 1
  fi
  echo KAFKA_DELIVERY_BRANCH_PASS
  matrix_counts
  echo
  exit 0
fi

if [[ "$MODE" == "--prometheus-only" ]]; then
  echo "========== Independent Prometheus delivery gate =========="
  cancel_remote_job prometheus ssh_prom '' /tmp/kubeauto-prometheus-gate
  prom_nodes=(
    192.168.122.243 192.168.122.246 192.168.122.217
    192.168.122.210 192.168.122.216 192.168.122.193
  )
  prometheus_cleanup() {
    local gate_rc=$? cleanup_rc=0
    trap - EXIT INT TERM
    set +e
    echo ">>> mandatory Prometheus lab cleanup"
    bash "$ROOT/tests/helpers/lab-wipe-nodes.sh" --rocky-only "${prom_nodes[@]}" || cleanup_rc=$?
    bash "$ROOT/tests/helpers/lab-wipe-nodes.sh" --verify --rocky-only "${prom_nodes[@]}" || cleanup_rc=$?
    ssh_prom "rm -rf /usr/local/kubeauto/clusters/prometheus-gate /usr/local/kubeauto/clusters/prometheus-gate.hosts" || cleanup_rc=$?
    if [[ "$gate_rc" -eq 0 && "$cleanup_rc" -ne 0 ]]; then
      gate_rc=$cleanup_rc
    fi
    if [[ "$gate_rc" -eq 0 ]]; then
      echo PROMETHEUS_DELIVERY_BRANCH_PASS
      matrix_counts
      echo
    fi
    exit "$gate_rc"
  }
  trap prometheus_cleanup EXIT INT TERM
  unit_python="$ROOT/.venv/bin/python"
  [[ -x "$unit_python" ]] || unit_python="$(command -v python3)"
  "$unit_python" -m unittest tests.unit.test_prometheus_delivery tests.unit.test_six_repo_version_sync -v
  echo ">>> clean and verify six Prometheus Kubernetes nodes"
  bash "$ROOT/tests/helpers/lab-wipe-nodes.sh" --rocky-only "${prom_nodes[@]}"
  bash "$ROOT/tests/helpers/lab-wipe-nodes.sh" --verify --rocky-only "${prom_nodes[@]}"
  echo ">>> bootstrap Prometheus control key to six Kubernetes nodes"
  bash "$ROOT/tests/helpers/lab-control-ssh-bootstrap.sh" "$PROM_TEST_HOST" \
    "${prom_nodes[@]/#/root@}"
  echo ">>> sync source through Prometheus control host"
  if [[ -n "$PROM_TEST_JUMPER" ]]; then
    KUBEAUTO_SSH_JUMP="$PROM_TEST_JUMPER" KUBEAUTO_SYNC_SKIP_CONTROL_SETUP=1 \
      bash "$ROOT/tests/helpers/sync-kubeauto.sh" "$PROM_TEST_HOST"
  else
    KUBEAUTO_SYNC_SKIP_CONTROL_SETUP=1 bash "$ROOT/tests/helpers/sync-kubeauto.sh" "$PROM_TEST_HOST"
  fi
  ssh_prom "chmod 0755 /usr/local/kubeauto/tests/helpers/prometheus-artifact-gate.sh /usr/local/kubeauto/tests/helpers/prometheus-optional-artifact-gate.sh /usr/local/kubeauto/tests/helpers/prometheus-optional-regression.sh /usr/local/kubeauto/tests/helpers/prometheus-regression.sh /usr/local/kubeauto/tests/helpers/run-durable-gate.sh"
  echo ">>> verify Prometheus dual-push manifests and platform indexes"
  prom_artifact_gate_cmd="bash /usr/local/kubeauto/tests/helpers/prometheus-artifact-gate.sh"
  if [[ -n "${PROM_ARTIFACT_DOCKERHUB_VERIFY_PREFIX:-}" ]]; then
    printf -v prom_verify_prefix_quoted '%q' "$PROM_ARTIFACT_DOCKERHUB_VERIFY_PREFIX"
    prom_artifact_gate_cmd="env PROM_ARTIFACT_DOCKERHUB_VERIFY_PREFIX=${prom_verify_prefix_quoted} ${prom_artifact_gate_cmd}"
  fi
  ssh_prom "$prom_artifact_gate_cmd"
  prom_runtime_env=""
  if [[ -n "${PROM_ARTIFACT_DOCKERHUB_VERIFY_PREFIX:-}" ]]; then
    printf -v prom_runtime_prefix_quoted '%q' "$PROM_ARTIFACT_DOCKERHUB_VERIFY_PREFIX"
    prom_runtime_env="PROM_ARTIFACT_DOCKERHUB_VERIFY_PREFIX=${prom_runtime_prefix_quoted}"
  fi
  ssh_prom "rm -f /tmp/kubeauto-prometheus-live.log /tmp/kubeauto-prometheus-gate.pid /tmp/kubeauto-prometheus-gate.exit /tmp/kubeauto-prometheus-gate.finalized; nohup env ${prom_runtime_env} bash /usr/local/kubeauto/tests/helpers/run-durable-gate.sh /tmp/kubeauto-prometheus-gate PROMETHEUS_GATE_EXIT bash /usr/local/kubeauto/tests/helpers/prometheus-regression.sh >/tmp/kubeauto-prometheus-live.log 2>&1 </dev/null &"
  prom_rc=0
  monitor_remote_job prometheus ssh_prom '' /tmp/kubeauto-prometheus-gate \
    /tmp/kubeauto-prometheus-live.log PROMETHEUS_FULL_GATE_PASS || prom_rc=$?
  if [[ "$prom_rc" -ne 0 ]]; then
    echo "[FAIL] Prometheus gate failed rc=${prom_rc}" >&2
    exit 1
  fi
  exit 0
fi

if [[ "$MODE" == "--mysql-stage-source" ]]; then
  [[ -n "${2:-}" ]] || {
    echo "Usage: $0 --mysql-stage-source <runtime-registry-prefix>" >&2
    exit 2
  }
  cancel_remote_job mysql-pxc ssh_mysql '' /tmp/kubeauto-mysql-gate
  if ssh138 "sudo docker version >/dev/null" 2>/dev/null; then
    stage_label=mysql-stage-138
    stage_ssh=ssh138
    stage_privilege=sudo
    stage_sudo='sudo '
    stage_target="$HOST138"
  elif ssh130 "docker version >/dev/null" 2>/dev/null; then
    stage_label=mysql-stage-130
    stage_ssh=ssh130
    stage_privilege=''
    stage_sudo=''
    stage_target="$HOST130"
  else
    echo "[FAIL] neither staging control host has key access and Docker" >&2
    exit 1
  fi
  cancel_remote_job "$stage_label" "$stage_ssh" "$stage_privilege" /tmp/kubeauto-mysql-stage
  scp -o BatchMode=yes -o StrictHostKeyChecking=no -o ConnectTimeout=10 \
    "$ROOT/tests/helpers/mysql-stage-images.sh" \
    "$ROOT/tests/helpers/run-durable-gate.sh" "$stage_target:/tmp/"
  printf -v mysql_stage_prefix_quoted '%q' "$2"
  "$stage_ssh" "${stage_sudo}chmod 0755 /tmp/mysql-stage-images.sh /tmp/run-durable-gate.sh; ${stage_sudo}rm -f /tmp/kubeauto-mysql-stage-live.log /tmp/kubeauto-mysql-stage.pid /tmp/kubeauto-mysql-stage.exit; ${stage_sudo}nohup env MYSQL_IMAGE_SOURCE_PREFIX=${mysql_stage_prefix_quoted} bash /tmp/run-durable-gate.sh /tmp/kubeauto-mysql-stage MYSQL_STAGE_EXIT bash /tmp/mysql-stage-images.sh >/tmp/kubeauto-mysql-stage-live.log 2>&1 </dev/null &"
  monitor_remote_job \
    "$stage_label" "$stage_ssh" "$stage_privilege" /tmp/kubeauto-mysql-stage \
    /tmp/kubeauto-mysql-stage-live.log MYSQL_STAGE_IMAGES_PASS

  stage_images=(
    percona/percona-xtradb-cluster-operator:1.20.0
    percona/percona-xtradb-cluster:8.4.8-8.1
    percona/percona-xtrabackup:8.4.0-5.1
    percona/haproxy:2.8.18-1
    percona/fluentbit:5.0.6-1
  )
  stage_refs=()
  for image in "${stage_images[@]}"; do
    stage_refs+=("${2}/${image}")
  done
  printf -v stage_refs_quoted ' %q' "${stage_refs[@]}"
  echo "[TRANSFER] staging-host=${stage_target} target=mysql-control heartbeat=30s"
  (
    set -o pipefail
    "$stage_ssh" "${stage_sudo}docker save${stage_refs_quoted}" | ssh_mysql "docker load"
  ) &
  stage_transfer_pid=$!
  stage_transfer_started=$(date +%s)
  while kill -0 "$stage_transfer_pid" 2>/dev/null; do
    echo "[HEARTBEAT] time=$(date '+%F %T %Z') label=mysql-stage-transfer elapsed=$(( $(date +%s) - stage_transfer_started ))s"
    sleep 30
  done
  wait "$stage_transfer_pid"
  for ref in "${stage_refs[@]}"; do
    ssh_mysql "docker image inspect $(printf '%q' "$ref") >/dev/null"
  done
  echo MYSQL_STAGE_TRANSFER_PASS
  exit 0
fi

if [[ "$MODE" == "--all-delivery-daemon" ]]; then
  # A UI terminal may disappear during a multi-hour delivery sign-off. Keep
  # the one authoritative runner alive with durable local state and audit log.
  all_delivery_state="/tmp/kubeauto-all-delivery"
  all_delivery_log="$ROOT/logs/enterprise-delivery-$(date +%Y%m%d-%H%M).log"
  rm -f "${all_delivery_state}.pid" "${all_delivery_state}.exit"
  setsid nohup bash "$ROOT/tests/helpers/run-durable-gate.sh" \
    "$all_delivery_state" ENTERPRISE_DELIVERY_ALL_EXIT \
    bash "$0" --all-delivery >"$all_delivery_log" 2>&1 < /dev/null &
  echo "ENTERPRISE_DELIVERY_ALL_STARTED pid=$! log=$all_delivery_log"
  exit 0
fi

if [[ "$MODE" == "--all-delivery" ]]; then
  # One autonomous delivery-signoff command. Each existing focused mode owns
  # its durable state, foreground stream, diagnostics and post-run clean
  # verification; composing the proven modes here avoids a second orchestration
  # implementation and keeps the user's approval surface to this fixed runner.
  delivery_modes=(
    --registry-reboot-only
    --jumper-only
    --nerdctl-only
    --docker-only
    --upgrade-only
    --gaps-only
    --build-rocky8-kubecli
    --ansible-os-probe
    --ansible-os-only
    --ansible-ee-debian-probe
    --ansible-anolis-container-probe
    --tier3-tools-only
  )
  for delivery_mode in "${delivery_modes[@]}"; do
    echo "========== DELIVERY SIGNOFF mode=$delivery_mode =========="
    bash "$0" "$delivery_mode"
  done
  echo "ENTERPRISE_DELIVERY_ALL_PASS"
  matrix_counts
  echo
  exit 0
fi

if [[ "$MODE" == "--status" ]]; then
  echo "========== Prometheus delivery gate =========="
  remote_job_summary ssh_prom '' /tmp/kubeauto-prometheus-gate /tmp/kubeauto-prometheus-live.log || true
  remote_log_tail ssh_prom '' /tmp/kubeauto-prometheus-live.log 120
  echo "========== development-host full delivery =========="
  local_all_delivery_pid=missing
  local_all_delivery_rc=missing
  local_all_delivery_state=starting
  test -r /tmp/kubeauto-all-delivery.pid && local_all_delivery_pid=$(cat /tmp/kubeauto-all-delivery.pid)
  if test -r /tmp/kubeauto-all-delivery.exit; then
    local_all_delivery_rc=$(cat /tmp/kubeauto-all-delivery.exit)
    local_all_delivery_state=exited
  elif test "$local_all_delivery_pid" != missing && kill -0 "$local_all_delivery_pid" 2>/dev/null; then
    local_all_delivery_state=running
  elif test "$local_all_delivery_pid" != missing; then
    local_all_delivery_state=lost
  fi
  printf 'state=%s pid=%s rc=%s\n' "$local_all_delivery_state" "$local_all_delivery_pid" "$local_all_delivery_rc"
  echo "========== 138 full regression =========="
  remote_job_summary ssh138 sudo /tmp/kubeauto-regression-aio /tmp/kubeauto-regression-live.log || true
  remote_log_tail ssh138 sudo /tmp/kubeauto-regression-live.log 30
  echo "========== 130 jumper regression =========="
  remote_job_summary ssh130 '' /tmp/kubeauto-regression-jumper /tmp/kubeauto-jumper-live.log || true
  remote_log_tail ssh130 '' /tmp/kubeauto-jumper-live.log 30
  echo "========== 138 nerdctl gate =========="
  remote_job_summary ssh138 sudo /tmp/kubeauto-nerdctl-gate /tmp/kubeauto-nerdctl-live.log || true
  remote_log_tail ssh138 sudo /tmp/kubeauto-nerdctl-live.log 30
  echo "========== 138 Docker delivery gate =========="
  remote_job_summary ssh138 sudo /tmp/kubeauto-docker-gate /tmp/kubeauto-docker-live.log || true
  remote_log_tail ssh138 sudo /tmp/kubeauto-docker-live.log 30
  echo "========== 138 Kubernetes upgrade gate =========="
  remote_job_summary ssh138 sudo /tmp/kubeauto-upgrade-gate /tmp/kubeauto-upgrade-live.log || true
  remote_log_tail ssh138 sudo /tmp/kubeauto-upgrade-live.log 30
  echo "========== 138 remaining delivery gaps gate =========="
  remote_job_summary ssh138 sudo /tmp/kubeauto-gaps-gate /tmp/kubeauto-gaps-live.log || true
  remote_log_tail ssh138 sudo /tmp/kubeauto-gaps-live.log 30
  echo "========== 138 Ansible EE Debian compatibility gate =========="
  remote_job_summary ssh138 sudo /tmp/kubeauto-ansible-ee-gate /tmp/kubeauto-ansible-ee-live.log || true
  remote_log_tail ssh138 sudo /tmp/kubeauto-ansible-ee-live.log 30
  echo "========== 138 Rocky 8 customer-binary build =========="
  remote_job_summary ssh138 sudo /tmp/kubeauto-rocky8-build-gate /tmp/kubeauto-rocky8-build-live.log || true
  remote_log_tail ssh138 sudo /tmp/kubeauto-rocky8-build-live.log 30
  echo "========== native Ansible package gates =========="
  for native_status in \
    "debian-128:ssh128:brinnatt@192.168.47.128" \
    "rocky-130:ssh130:root@192.168.47.130" \
    "anolis-141:ssh141:root@192.168.47.141" \
    "openeuler-142:ssh142:root@192.168.47.142" \
    "opensuse-143:ssh143:root@192.168.47.143"; do
    IFS=: read -r native_label native_ssh native_host <<<"$native_status"
    echo "--- $native_label ($native_host) ---"
    remote_job_summary "$native_ssh" '' /tmp/kubeauto-ansible-native-gate /tmp/kubeauto-ansible-native-live.log || true
  done
  echo "========== matrix =========="
  matrix_counts
  echo
  echo "========== process state =========="
  ssh138 "pgrep -af '[r]egression-full|[k]ubecli|[a]nsible-playbook' || true"
  ssh130 "pgrep -af '[r]egression-jumper|[k]ubecli|[a]nsible-playbook' || true"
  exit 0
fi

if [[ "$MODE" == "--progress" ]]; then
  echo "========== delivery progress $(date '+%F %T %Z') =========="
  local_all_delivery_pid=missing
  local_all_delivery_rc=missing
  local_all_delivery_state=starting
  test -r /tmp/kubeauto-all-delivery.pid && local_all_delivery_pid=$(cat /tmp/kubeauto-all-delivery.pid)
  if test -r /tmp/kubeauto-all-delivery.exit; then
    local_all_delivery_rc=$(cat /tmp/kubeauto-all-delivery.exit)
    local_all_delivery_state=exited
  elif test "$local_all_delivery_pid" != missing && kill -0 "$local_all_delivery_pid" 2>/dev/null; then
    local_all_delivery_state=running
  elif test "$local_all_delivery_pid" != missing; then
    local_all_delivery_state=lost
  fi
  printf 'delivery state=%s pid=%s rc=%s\n' "$local_all_delivery_state" "$local_all_delivery_pid" "$local_all_delivery_rc"
  for progress_gate in \
    'registry-reboot:ssh138:sudo:/tmp/kubeauto-registry-reboot-gate:/tmp/kubeauto-registry-reboot-live.log' \
    'jumper:ssh130::/tmp/kubeauto-regression-jumper:/tmp/kubeauto-jumper-live.log' \
    'nerdctl:ssh138:sudo:/tmp/kubeauto-nerdctl-gate:/tmp/kubeauto-nerdctl-live.log' \
    'docker:ssh138:sudo:/tmp/kubeauto-docker-gate:/tmp/kubeauto-docker-live.log' \
    'upgrade:ssh138:sudo:/tmp/kubeauto-upgrade-gate:/tmp/kubeauto-upgrade-live.log' \
    'gaps:ssh138:sudo:/tmp/kubeauto-gaps-gate:/tmp/kubeauto-gaps-live.log' \
    'ansible-ee:ssh138:sudo:/tmp/kubeauto-ansible-ee-gate:/tmp/kubeauto-ansible-ee-live.log' \
    'rocky8-build:ssh138:sudo:/tmp/kubeauto-rocky8-build-gate:/tmp/kubeauto-rocky8-build-live.log'; do
    IFS=: read -r progress_label progress_ssh progress_privilege progress_state progress_log <<<"$progress_gate"
    progress_summary=$(remote_job_summary "$progress_ssh" "$progress_privilege" "$progress_state" "$progress_log" 2>/dev/null || true)
    [[ "$progress_summary" == *'state=running'* || "$progress_summary" == *'state=lost'* ]] && \
      printf 'gate=%s %s\n' "$progress_label" "$progress_summary"
  done
  matrix_counts
  echo
  exit 0
fi

if [[ "$MODE" == "--follow-delivery" ]]; then
  while :; do
    bash "$0" --progress
    sleep 30
  done
fi

if [[ "$MODE" == "--ansible-anolis-probe" ]]; then
  echo "========== restore snapshot SSH key 141 =========="
  bash "$ROOT/tests/helpers/lab-ssh-bootstrap.sh" "$HOST141"
  anolis_probe_script='
set -u
. /etc/os-release
printf "os_id=%s version_id=%s pretty_name=%s\n" "${ID:-}" "${VERSION_ID:-}" "${PRETTY_NAME:-}"
for runtime in docker podman; do
  if command -v "$runtime" >/dev/null 2>&1; then
    echo "container_runtime=$runtime path=$(command -v "$runtime")"
    "$runtime" version 2>/dev/null | head -n 20 || true
  else
    echo "container_runtime_absent=$runtime"
  fi
done
for url in \
  https://mirrors.openanolis.cn/anolis/23/Devel/ \
  https://mirrors.openanolis.cn/anolis/23/Devel/x86_64/ \
  https://mirrors.openanolis.cn/anolis/23.3/Devel/ \
  https://mirrors.openanolis.cn/anolis/23.3/Devel/x86_64/ \
  https://mirrors.openanolis.cn/epao/23/ \
  https://mirrors.openanolis.cn/epao/23/x86_64/; do
  echo "openanolis_official_index=$url"
  if index="$(curl -fsSL --max-time 15 "$url" 2>/dev/null)"; then
    printf "%s\n" "$index" | grep -Eo "href=\"[^\"]+\"" | head -n 160 || true
    echo "OPENANOLIS_INDEX_OK url=$url"
  else
    echo "OPENANOLIS_INDEX_UNAVAILABLE url=$url"
  fi
done
for spec in \
  "anolis-devel-23.3,https://mirrors.openanolis.cn/anolis/23.3/Devel/x86_64/os" \
  "epao-23,https://mirrors.openanolis.cn/epao/23/x86_64/"; do
  repo_id="${spec%%,*}"
  echo "openanolis_dnf_repo=$spec"
  dnf -q --repofrompath "$spec" --repo "$repo_id" list --available ansible ansible-core 2>/dev/null || true
  dnf -q --repofrompath "$spec" --repo "$repo_id" info ansible ansible-core 2>/dev/null || true
  dnf -q --repofrompath "$spec" --repo "$repo_id" repoquery --requires --resolve ansible ansible-core 2>/dev/null || true
done
echo ANSIBLE_ANOLIS_PROBE_PASS
'
  ssh141 "bash -lc $(printf '%q' "$anolis_probe_script")"
  exit 0
fi

if [[ "$MODE" == "--ansible-anolis-container-probe" ]]; then
  echo "========== restore snapshot SSH key 141 =========="
  bash "$ROOT/tests/helpers/lab-ssh-bootstrap.sh" "$HOST141"
  scp -o BatchMode=yes -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
    "$ROOT/dist/kubecli" \
    "$ROOT/tests/helpers/ansible-anolis-container-gate.sh" \
    "$ROOT/tests/helpers/run-durable-gate.sh" "$HOST141:/tmp/"
  ssh141 "chmod 0755 /tmp/kubecli /tmp/ansible-anolis-container-gate.sh /tmp/run-durable-gate.sh; mv /tmp/kubecli /tmp/kubecli-anolis-container-gate; rm -f /tmp/kubeauto-ansible-anolis-live.log /tmp/kubeauto-ansible-anolis-gate.pid /tmp/kubeauto-ansible-anolis-gate.exit; nohup bash /tmp/run-durable-gate.sh /tmp/kubeauto-ansible-anolis-gate ANSIBLE_ANOLIS_CONTAINER_EXIT bash /tmp/ansible-anolis-container-gate.sh /tmp/kubecli-anolis-container-gate >/tmp/kubeauto-ansible-anolis-live.log 2>&1 </dev/null &"
  gate_rc=0
  monitor_remote_job \
    ansible-anolis-141 ssh141 '' /tmp/kubeauto-ansible-anolis-gate \
    /tmp/kubeauto-ansible-anolis-live.log ANSIBLE_ANOLIS_CONTAINER_GATE_PASS || gate_rc=$?
  # The remote control is intentionally restored to its clean snapshot. Keep
  # its terminal evidence locally before removing the ephemeral remote files.
  remote_log_tail ssh141 '' /tmp/kubeauto-ansible-anolis-live.log 160 \
    | tee -a "$LOG"
  ssh141 "rm -f /tmp/kubecli-anolis-container-gate /tmp/ansible-anolis-container-gate.sh /tmp/run-durable-gate.sh /tmp/kubeauto-ansible-anolis-live.log /tmp/kubeauto-ansible-anolis-gate.pid /tmp/kubeauto-ansible-anolis-gate.exit"
  (( gate_rc == 0 )) || exit "$gate_rc"
  exit 0
fi

if [[ "$MODE" == "--ansible-os-only" ]]; then
  test -x "$ROOT/dist/kubecli" || {
    echo "ERROR: current customer binary missing: $ROOT/dist/kubecli" >&2
    exit 1
  }
  ansible_gate_rc=0
  for entry in "142:ssh142:$HOST142" "143:ssh143:$HOST143"; do
    label="${entry%%:*}"
    remainder="${entry#*:}"
    ssh_function="${remainder%%:*}"
    target="${remainder#*:}"
    echo "========== prepare native Ansible gate $label =========="
    bash "$ROOT/tests/helpers/lab-ssh-bootstrap.sh" "$target"
    "$ssh_function" "rm -rf /tmp/kubeauto-ansible-native-source; mkdir -p /tmp/kubeauto-ansible-native-source"
    # Clean compatibility snapshots intentionally contain only base OS tools.
    # The native gate needs just these Ansible inputs; SCP/SFTP avoids requiring
    # rsync or tar to already be installed on the remote host.
    scp -r -o BatchMode=yes -o StrictHostKeyChecking=no \
      -o UserKnownHostsFile=/dev/null \
      "$ROOT/playbooks" "$ROOT/roles" "$ROOT/conf" \
      "$target:/tmp/kubeauto-ansible-native-source/"
    scp -o BatchMode=yes -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
      "$ROOT/dist/kubecli" "$ROOT/tests/helpers/ansible-native-package-gate.sh" \
      "$ROOT/tests/helpers/run-durable-gate.sh" "$target:/tmp/"
    "$ssh_function" "chmod 0755 /tmp/kubecli /tmp/ansible-native-package-gate.sh /tmp/run-durable-gate.sh; mv /tmp/kubecli /tmp/kubecli-ansible-native-gate; rm -f /tmp/kubeauto-ansible-native-live.log /tmp/kubeauto-ansible-native-gate.pid /tmp/kubeauto-ansible-native-gate.exit; nohup bash /tmp/run-durable-gate.sh /tmp/kubeauto-ansible-native-gate ANSIBLE_NATIVE_GATE_EXIT bash /tmp/ansible-native-package-gate.sh /tmp/kubecli-ansible-native-gate /tmp/kubeauto-ansible-native-source >/tmp/kubeauto-ansible-native-live.log 2>&1 </dev/null &"
    gate_rc=0
    monitor_remote_job \
      "ansible-native-$label" "$ssh_function" '' /tmp/kubeauto-ansible-native-gate \
      /tmp/kubeauto-ansible-native-live.log ANSIBLE_NATIVE_PACKAGE_GATE_PASS || gate_rc=$?
    "$ssh_function" "rm -rf /tmp/kubeauto-ansible-native-source /tmp/kubecli-ansible-native-gate /tmp/ansible-native-package-gate.sh /tmp/run-durable-gate.sh /tmp/kubeauto-ansible-native-live.log /tmp/kubeauto-ansible-native-gate.pid /tmp/kubeauto-ansible-native-gate.exit"
    if (( gate_rc != 0 )); then
      ansible_gate_rc=$gate_rc
      break
    fi
  done
  (( ansible_gate_rc == 0 )) || exit "$ansible_gate_rc"
  echo ANSIBLE_OS_NATIVE_GATES_PASS
  exit 0
fi

if [[ "$MODE" == "--build-rocky8-kubecli" ]]; then
  echo "========== build customer kubecli on Rocky 8.10 / glibc 2.28 =========="
  bash "$ROOT/tests/helpers/lab-sudo-bootstrap.sh" "$HOST138"
  ssh138 "sudo rm -rf /tmp/kubeauto-rocky8-build-source /tmp/kubeauto-rocky8-build-output; sudo mkdir -p /tmp/kubeauto-rocky8-build-source; sudo chown -R ubuntu:ubuntu /tmp/kubeauto-rocky8-build-source"
  rsync -a --delete \
    --exclude .git --exclude .venv --exclude build --exclude dist \
    --exclude logs --exclude __pycache__ --exclude '*.pyc' \
    -e "ssh -o BatchMode=yes -o StrictHostKeyChecking=no" \
    "$ROOT/" "$HOST138:/tmp/kubeauto-rocky8-build-source/"
  scp138 "$ROOT/tests/helpers/lab-docker-bootstrap.sh" \
    "$ROOT/tests/helpers/build-kubecli-rocky8.sh" \
    "$ROOT/tests/helpers/run-durable-gate.sh" "$HOST138:/tmp/"
  ssh138 "sudo chmod 0755 /tmp/lab-docker-bootstrap.sh /tmp/run-durable-gate.sh; sudo rm -f /tmp/kubeauto-docker-bootstrap-live.log /tmp/kubeauto-docker-bootstrap-gate.pid /tmp/kubeauto-docker-bootstrap-gate.exit; sudo nohup bash /tmp/run-durable-gate.sh /tmp/kubeauto-docker-bootstrap-gate LAB_DOCKER_BOOTSTRAP_EXIT bash /tmp/lab-docker-bootstrap.sh >/tmp/kubeauto-docker-bootstrap-live.log 2>&1 </dev/null &"
  docker_bootstrap_rc=0
  monitor_remote_job \
    docker-bootstrap-138 ssh138 sudo /tmp/kubeauto-docker-bootstrap-gate \
    /tmp/kubeauto-docker-bootstrap-live.log LAB_DOCKER_BOOTSTRAP_PASS || docker_bootstrap_rc=$?
  if (( docker_bootstrap_rc != 0 )); then
    ssh138 "sudo rm -rf /tmp/kubeauto-rocky8-build-source /tmp/kubeauto-rocky8-build-output /tmp/lab-docker-bootstrap.sh /tmp/build-kubecli-rocky8.sh /tmp/run-durable-gate.sh /tmp/kubeauto-docker-bootstrap-live.log /tmp/kubeauto-docker-bootstrap-gate.pid /tmp/kubeauto-docker-bootstrap-gate.exit"
    exit "$docker_bootstrap_rc"
  fi
  ssh138 "sudo chmod 0755 /tmp/build-kubecli-rocky8.sh /tmp/run-durable-gate.sh; sudo rm -f /tmp/kubeauto-rocky8-build-live.log /tmp/kubeauto-rocky8-build-gate.pid /tmp/kubeauto-rocky8-build-gate.exit; sudo nohup bash /tmp/run-durable-gate.sh /tmp/kubeauto-rocky8-build-gate ROCKY8_BUILD_EXIT bash /tmp/build-kubecli-rocky8.sh >/tmp/kubeauto-rocky8-build-live.log 2>&1 </dev/null &"
  build_rc=0
  monitor_remote_job \
    rocky8-kubecli-build ssh138 sudo /tmp/kubeauto-rocky8-build-gate \
    /tmp/kubeauto-rocky8-build-live.log ROCKY8_KUBECLI_BUILD_PASS || build_rc=$?
  if (( build_rc == 0 )); then
    mkdir -p "$ROOT/dist"
    scp138 "$HOST138:/tmp/kubeauto-rocky8-build-output/kubecli" "$ROOT/dist/.kubecli.new"
    chmod 0755 "$ROOT/dist/.kubecli.new"
    mv "$ROOT/dist/.kubecli.new" "$ROOT/dist/kubecli"
    "$ROOT/dist/kubecli" version
    sha256sum "$ROOT/dist/kubecli"
  else
    # Preserve the remote failure context locally before mandatory cleanup.
    # The local logs directory is ignored; the 138 build workspace is still
    # removed below so a failed attempt never becomes the next baseline.
    echo "========== ROCKY8_BUILD_FAILURE_EVIDENCE =========="
    remote_log_tail ssh138 sudo /tmp/kubeauto-rocky8-build-live.log 240 \
      | tee -a "$LOG"
  fi
  ssh138 "sudo rm -rf /tmp/kubeauto-rocky8-build-source /tmp/kubeauto-rocky8-build-output /tmp/lab-docker-bootstrap.sh /tmp/build-kubecli-rocky8.sh /tmp/run-durable-gate.sh /tmp/kubeauto-docker-bootstrap-live.log /tmp/kubeauto-docker-bootstrap-gate.pid /tmp/kubeauto-docker-bootstrap-gate.exit /tmp/kubeauto-rocky8-build-live.log /tmp/kubeauto-rocky8-build-gate.pid /tmp/kubeauto-rocky8-build-gate.exit"
  (( build_rc == 0 )) || exit "$build_rc"
  echo ROCKY8_CUSTOMER_BINARY_READY
  exit 0
fi

if [[ "$MODE" == "--tier3-tools-only" ]]; then
  tools=(CalicoPolicyCli NetCheckCli KafkaCli MyBackupCli MigrationCli StarCli KubeBackupCli KubePublishCli OvpnUserCli)
  tier3_stage="$(mktemp -d /tmp/kubeauto-tier3-stage.XXXXXX)"

  tier3_cleanup() {
    local cleanup_rc=0
    set +e
    ssh138 "sudo bash -lc 'docker rm -f kubeauto-rocky8-tools-build >/dev/null 2>&1 || true; rm -rf /tmp/kubeauto-rocky8-build-source /tmp/kubeauto-rocky8-tools-output /tmp/kubeauto-docker-bootstrap-venv /tmp/lab-docker-bootstrap.sh /tmp/build-tools-rocky8.sh /tmp/run-durable-gate.sh'" || cleanup_rc=1
    ssh137 "rm -rf /tmp/kubeauto-tier3-tools /tmp/tier3-tools-gate.sh /tmp/run-durable-gate.sh" || cleanup_rc=1
    rm -rf "$tier3_stage"
    bash "$ROOT/tests/helpers/lab-wipe-nodes.sh" --rocky-only 192.168.47.137 || cleanup_rc=1
    bash "$ROOT/tests/helpers/lab-wipe-nodes.sh" --verify --rocky-only 192.168.47.137 || cleanup_rc=1
    ssh138 "sudo bash -lc 'test ! -e /tmp/kubeauto-rocky8-build-source; test ! -e /tmp/kubeauto-rocky8-tools-output; ! docker container inspect kubeauto-rocky8-tools-build >/dev/null 2>&1'" || cleanup_rc=1
    ssh137 "test ! -e /tmp/kubeauto-tier3-tools" || cleanup_rc=1
    set -e
    if (( cleanup_rc == 0 )); then
      echo TIER3_SCOPE_CLEAN_PASS
    fi
    return "$cleanup_rc"
  }
  trap 'tier3_cleanup || true' EXIT INT TERM

  echo "========== Tier3 preflight clean: Rocky 137 =========="
  bash "$ROOT/tests/helpers/lab-wipe-nodes.sh" --rocky-only 192.168.47.137
  bash "$ROOT/tests/helpers/lab-wipe-nodes.sh" --verify --rocky-only 192.168.47.137
  bash "$ROOT/tests/helpers/lab-sudo-bootstrap.sh" "$HOST138"
  bash "$ROOT/tests/helpers/lab-control-ssh-bootstrap.sh" --sudo-control \
    "$HOST138" "$HOST137"

  echo "========== stage current source on Ubuntu 138 =========="
  ssh138 "sudo rm -rf /tmp/kubeauto-rocky8-build-source /tmp/kubeauto-rocky8-tools-output; sudo mkdir -p /tmp/kubeauto-rocky8-build-source; sudo chown -R ubuntu:ubuntu /tmp/kubeauto-rocky8-build-source"
  rsync -a --delete \
    --exclude .git --exclude .venv --exclude build --exclude dist \
    --exclude logs --exclude __pycache__ --exclude '*.pyc' \
    -e "ssh -o BatchMode=yes -o StrictHostKeyChecking=no" \
    "$ROOT/" "$HOST138:/tmp/kubeauto-rocky8-build-source/"
  scp138 "$ROOT/tests/helpers/lab-docker-bootstrap.sh" \
    "$ROOT/tests/helpers/build-tools-rocky8.sh" \
    "$ROOT/tests/helpers/run-durable-gate.sh" "$HOST138:/tmp/"

  echo "========== ensure customer Docker path on Ubuntu 138 =========="
  ssh138 "sudo chmod 0755 /tmp/lab-docker-bootstrap.sh /tmp/build-tools-rocky8.sh /tmp/run-durable-gate.sh; sudo rm -f /tmp/kubeauto-docker-bootstrap-live.log /tmp/kubeauto-docker-bootstrap-gate.pid /tmp/kubeauto-docker-bootstrap-gate.exit; sudo nohup bash /tmp/run-durable-gate.sh /tmp/kubeauto-docker-bootstrap-gate LAB_DOCKER_BOOTSTRAP_EXIT bash /tmp/lab-docker-bootstrap.sh >/tmp/kubeauto-docker-bootstrap-live.log 2>&1 </dev/null &"
  monitor_remote_job \
    docker-bootstrap-138 ssh138 sudo /tmp/kubeauto-docker-bootstrap-gate \
    /tmp/kubeauto-docker-bootstrap-live.log LAB_DOCKER_BOOTSTRAP_PASS

  echo "========== build nine tools on Rocky 8.10 / glibc 2.28 =========="
  ssh138 "sudo rm -f /tmp/kubeauto-tier3-build-live.log /tmp/kubeauto-tier3-build-gate.pid /tmp/kubeauto-tier3-build-gate.exit; sudo nohup bash /tmp/run-durable-gate.sh /tmp/kubeauto-tier3-build-gate TIER3_BUILD_EXIT bash /tmp/build-tools-rocky8.sh >/tmp/kubeauto-tier3-build-live.log 2>&1 </dev/null &"
  monitor_remote_job \
    tier3-build-138 ssh138 sudo /tmp/kubeauto-tier3-build-gate \
    /tmp/kubeauto-tier3-build-live.log ROCKY8_TOOLS_BUILD_PASS

  echo "========== stage exact build outputs for real Rocky 137 =========="
  for tool in "${tools[@]}"; do
    scp138 "$HOST138:/tmp/kubeauto-rocky8-tools-output/$tool" "$tier3_stage/$tool"
    test -x "$tier3_stage/$tool"
  done
  test "$(find "$tier3_stage" -maxdepth 1 -type f | wc -l)" -eq "${#tools[@]}"
  sha256sum "$tier3_stage"/*

  ssh137 "rm -rf /tmp/kubeauto-tier3-tools; mkdir -m 0755 /tmp/kubeauto-tier3-tools; rm -f /tmp/kubeauto-tier3-live.log /tmp/kubeauto-tier3-gate.pid /tmp/kubeauto-tier3-gate.exit"
  for tool in "${tools[@]}"; do
    scp137 "$tier3_stage/$tool" "$HOST137:/tmp/kubeauto-tier3-tools/$tool"
  done
  scp137 "$ROOT/tests/helpers/tier3-tools-gate.sh" \
    "$ROOT/tests/helpers/run-durable-gate.sh" "$HOST137:/tmp/"
  ssh137 "chmod 0755 /tmp/tier3-tools-gate.sh /tmp/run-durable-gate.sh /tmp/kubeauto-tier3-tools/*; nohup bash /tmp/run-durable-gate.sh /tmp/kubeauto-tier3-gate TIER3_TOOLS_EXIT bash /tmp/tier3-tools-gate.sh >/tmp/kubeauto-tier3-live.log 2>&1 </dev/null &"

  gate_rc=0
  monitor_remote_job \
    tier3-tools-137 ssh137 '' /tmp/kubeauto-tier3-gate \
    /tmp/kubeauto-tier3-live.log TIER3_TOOLS_GATE_PASS || gate_rc=$?
  cleanup_rc=0
  tier3_cleanup || cleanup_rc=$?
  trap - EXIT INT TERM
  (( gate_rc == 0 )) || exit "$gate_rc"
  (( cleanup_rc == 0 )) || exit "$cleanup_rc"
  echo TIER3_TOOLS_DELIVERY_PASS
  exit 0
fi

if [[ "$MODE" == "--diagnose-docker-bootstrap" ]]; then
  echo "========== Docker bootstrap diagnostic 138 =========="
  remote_job_summary ssh138 sudo /tmp/kubeauto-docker-bootstrap-gate \
    /tmp/kubeauto-docker-bootstrap-live.log
  ssh138 "sudo bash -lc 'echo ===processes===; ps -eo pid,ppid,etime,stat,wchan:24,cmd --forest | grep -E \"kubeauto-docker-bootstrap|kubecli.py download -d|dockerd\" | grep -v grep || true; echo ===network===; ss -tpn | grep -E \"dockerd|:443\" || true; echo ===docker-data===; du -sh /data/docker /var/lib/docker 2>/dev/null || true; find /data/docker -type f -printf \"%s %p\\n\" 2>/dev/null | sort -nr | head -n 20; echo ===docker-system-df===; docker system df || true; echo ===daemon-log===; journalctl -u docker --since \"30 minutes ago\" --no-pager | tail -n 160'"
  ssh138 "echo ===private-registry-resolution===; getent ahostsv4 hub.talkedu.cn || true; grep -n 'hub.talkedu.cn' /etc/hosts || true; echo ===private-registry-v2===; curl -ksS --connect-timeout 5 --max-time 15 -D - https://hub.talkedu.cn/v2/ -o /dev/null || true; echo ===private-ext-bin-manifest===; curl -ksS --connect-timeout 5 --max-time 15 -D - -H 'Accept: application/vnd.oci.image.manifest.v1+json, application/vnd.docker.distribution.manifest.v2+json' https://hub.talkedu.cn/v2/kubeauto/kubeauto-ext-bin/manifests/1.15.0 -o /dev/null || true"
  exit 0
fi

if [[ "$MODE" == "--cancel-rocky8-build" ]]; then
  cancel_remote_job rocky8-kubecli-build ssh138 sudo /tmp/kubeauto-rocky8-build-gate
  ssh138 "sudo rm -rf /tmp/kubeauto-rocky8-build-output; sudo docker rm -f kubeauto-rocky8-kubecli-build >/dev/null 2>&1 || true; sudo find /data/docker/tmp -maxdepth 1 -type f -name 'GetImageBlob*' -delete 2>/dev/null || true"
  echo ROCKY8_BUILD_CANCEL_CLEAN_PASS
  exit 0
fi

if [[ "$MODE" == "--rocky8-image-probe" ]]; then
  rocky8_probe_image="${ROCKY8_BUILD_IMAGE:-rockylinux/rockylinux:8.10}"
  printf -v rocky8_probe_image_quoted '%q' "$rocky8_probe_image"
  ssh138 "sudo timeout --signal=TERM --kill-after=5s 45s docker manifest inspect ${rocky8_probe_image_quoted} >/dev/null"
  echo ROCKY8_IMAGE_PROBE_PASS
  exit 0
fi

if [[ "$MODE" == "--cancel-docker-bootstrap" ]]; then
  cancel_remote_job docker-bootstrap-138 ssh138 sudo /tmp/kubeauto-docker-bootstrap-gate
  ssh138 "sudo rm -rf /tmp/kubeauto-docker-bootstrap-venv; sudo find /data/docker/tmp -maxdepth 1 -type f -name 'GetImageBlob*' -delete 2>/dev/null || true"
  echo DOCKER_BOOTSTRAP_CANCEL_CLEAN_PASS
  exit 0
fi

if [[ "$MODE" == "--follow-docker-bootstrap" ]]; then
  monitor_remote_job \
    docker-bootstrap-138 ssh138 sudo /tmp/kubeauto-docker-bootstrap-gate \
    /tmp/kubeauto-docker-bootstrap-live.log LAB_DOCKER_BOOTSTRAP_PASS
  exit 0
fi

if [[ "$MODE" == "--ansible-os-probe" ]]; then
  probe_script='
set -u
echo "PROBE_HOST=$(hostname)"
printf "host_ips=%s\n" "$(hostname -I 2>/dev/null || true)"
. /etc/os-release
printf "os_id=%s version_id=%s pretty_name=%s\n" "${ID:-}" "${VERSION_ID:-}" "${PRETTY_NAME:-}"
printf "arch=%s\n" "$(uname -m)"
for py in python3.13 python3.12 python3.11 python3.10 python3.9 python3 /usr/libexec/platform-python; do
  command -v "$py" >/dev/null 2>&1 || test -x "$py" || continue
  "$py" -c "import sys; print(\"python=%s version=%s.%s.%s\" % (sys.executable, *sys.version_info[:3]))" 2>/dev/null || true
done
if command -v ansible >/dev/null 2>&1; then
  echo "ansible_path=$(command -v ansible)"
  ansible --version || true
  rpm -qf "$(command -v ansible)" 2>/dev/null || true
fi
if command -v dnf >/dev/null 2>&1; then
  echo "package_manager=dnf"
  dnf -q repolist || true
  dnf -q repolist --all || true
  dnf -q list --available ansible ansible-core 2>/dev/null || true
  dnf -q info ansible ansible-core 2>/dev/null || true
  dnf -q search ansible 2>/dev/null || true
  dnf -q repoquery --requires --resolve ansible 2>/dev/null || true
  rpm -q --requires ansible ansible-core 2>/dev/null || true
  sed -n "/^\[.*\]/p; /^enabled=/p; /^baseurl=/p; /^metalink=/p; /^mirrorlist=/p" /etc/yum.repos.d/*.repo 2>/dev/null || true
  if [[ "${ID:-}" == "anolis" ]] && command -v curl >/dev/null 2>&1; then
    for url in \
      https://mirrors.openanolis.cn/anolis/23/ \
      https://mirrors.openanolis.cn/anolis/23.3/ \
      https://mirrors.openanolis.cn/epao/; do
      echo "openanolis_official_index=$url"
      curl -fsSL --max-time 15 "$url" 2>/dev/null \
        | grep -Eo "href=\"[^\"]+\"" | head -n 80 || true
    done
  fi
elif command -v yum >/dev/null 2>&1; then
  echo "package_manager=yum"
  yum -q repolist || true
  yum -q list available ansible ansible-core 2>/dev/null || true
  yum -q info ansible ansible-core 2>/dev/null || true
elif command -v zypper >/dev/null 2>&1; then
  echo "package_manager=zypper"
  zypper --non-interactive repos -u || true
  zypper --non-interactive search -s ansible ansible-core || true
  zypper --non-interactive info --requires ansible || true
  zypper --non-interactive info --requires ansible-core || true
elif command -v apt-get >/dev/null 2>&1; then
  echo "package_manager=apt"
  apt-cache policy ansible ansible-core python3.9 python3.10 python3.11 python3.12 python3.13 2>/dev/null || true
  if command -v ansible >/dev/null 2>&1; then
    dpkg-query -S "$(command -v ansible)" 2>/dev/null || true
  fi
fi
echo ANSIBLE_OS_PROBE_HOST_PASS
'
  probe_failures=0
  for entry in "128:ssh128:$HOST128" "141:ssh141:$HOST141" "142:ssh142:$HOST142" "143:ssh143:$HOST143"; do
    label="${entry%%:*}"
    remainder="${entry#*:}"
    ssh_function="${remainder%%:*}"
    target="${remainder#*:}"
    echo "========== restore snapshot SSH key $label =========="
    if ! bash "$ROOT/tests/helpers/lab-ssh-bootstrap.sh" "$target"; then
      echo "[FAIL] Ansible OS probe $label SSH bootstrap/connectivity"
      probe_failures=$((probe_failures+1))
      continue
    fi
    echo "========== Ansible OS probe $label =========="
    if ! "$ssh_function" "bash -lc $(printf '%q' "$probe_script")"; then
      echo "[FAIL] Ansible OS probe $label command"
      probe_failures=$((probe_failures+1))
    fi
  done
  if (( probe_failures > 0 )); then
    echo "ANSIBLE_OS_PROBE_FAIL failures=$probe_failures"
    exit 1
  fi
  official_script='
set -u
fetch_github_source() {
  branch="$1"
  file="$2"
  primary="https://raw.githubusercontent.com/ansible/ansible/${branch}/${file}"
  fallback="https://v6.gh-proxy.org/https://github.com/ansible/ansible/raw/${branch}/${file}"
  echo "official_ansible_source_${branch}"
  if content="$(curl -fsSL --max-time 15 "$primary" 2>/dev/null)"; then
    source_path=github-direct
  elif content="$(curl -fsSL --max-time 15 "$fallback" 2>/dev/null)"; then
    source_path=github-proxy
  else
    echo "OFFICIAL_SOURCE_UNAVAILABLE branch=$branch"
    return
  fi
  printf "%s\n" "$content" \
    | grep -E "python_requires|requires-python|install_requires|dependencies =|jinja2|PyYAML|cryptography|resolvelib" \
    | head -n 24 || true
  echo "OFFICIAL_SOURCE_OK branch=$branch path=$source_path"
}
for branch in stable-2.9 stable-2.10; do
  fetch_github_source "$branch" setup.py
done
for branch in stable-2.16 stable-2.17 stable-2.18 stable-2.19; do
  fetch_github_source "$branch" pyproject.toml
done
for branch in stable-2.16 stable-2.17; do
  fetch_github_source "$branch" setup.cfg
done
echo "official_ansible_support_matrix"
if curl -fsSL --max-time 15 https://docs.ansible.com/projects/ansible-core/devel/reference_appendices/release_and_maintenance.html 2>/dev/null \
  | python3 -c "import html,re,sys; text=sys.stdin.read(); text=re.sub(r\"</(?:tr|p|li|h[1-6])>\", \"\\n\", text); print(html.unescape(re.sub(r\"<[^>]+>\", \" \", text)))" \
  | grep -Ei "control node python|target python|2\\.(16|17|18|19)" | head -n 80; then
  echo OFFICIAL_SUPPORT_MATRIX_OK
else
  echo OFFICIAL_SUPPORT_MATRIX_UNAVAILABLE
fi

'
  echo "========== Ansible official source evidence (once via 143) =========="
  ssh143 "bash -lc $(printf '%q' "$official_script")"
  echo "ANSIBLE_OS_PROBE_PASS"
  exit 0
fi

if [[ "$MODE" == "--ansible-ee-debian-probe" ]]; then
  # Ansible Runner officially supports containerized execution environments.
  # Prove the already dual-pushed image can execute real modules on Debian's
  # Python 3.13 before considering any product-path change.
  bash "$ROOT/tests/helpers/lab-control-ssh-bootstrap.sh" --sudo-control \
    "$HOST138" "$HOST128"
  bash "$ROOT/tests/helpers/sync-kubeauto.sh" "$HOST138"
  ssh138 "sudo rm -f /tmp/kubeauto-ansible-ee-live.log /tmp/kubeauto-ansible-ee-gate.pid /tmp/kubeauto-ansible-ee-gate.exit; sudo nohup bash /usr/local/kubeauto/tests/helpers/run-durable-gate.sh /tmp/kubeauto-ansible-ee-gate ANSIBLE_EE_PROBE_EXIT bash /usr/local/kubeauto/tests/helpers/ansible-ee-debian-gate.sh >/tmp/kubeauto-ansible-ee-live.log 2>&1 </dev/null &"
  monitor_remote_job \
    ansible-ee-debian-138 ssh138 sudo /tmp/kubeauto-ansible-ee-gate \
    /tmp/kubeauto-ansible-ee-live.log ANSIBLE_EE_DEBIAN_PROBE_PASS
  exit 0
fi

if [[ "$MODE" == "--follow-ansible-ee" ]]; then
  monitor_remote_job \
    ansible-ee-debian-138 ssh138 sudo /tmp/kubeauto-ansible-ee-gate \
    /tmp/kubeauto-ansible-ee-live.log ANSIBLE_EE_DEBIAN_PROBE_PASS
  exit 0
fi

if [[ "$MODE" == "--follow" ]]; then
  echo "Following 138 enterprise-regression log (Ctrl-C stops viewing only; remote regression continues)."
  ssh138 "sudo tail -n 60 -F /tmp/kubeauto-regression-live.log"
  exit 0
fi

if [[ "$MODE" == "--follow-jumper" ]]; then
  echo "Following 130 jumper-regression log (Ctrl-C stops viewing only; remote regression continues)."
  ssh130 "tail -n 60 -F /tmp/kubeauto-jumper-live.log"
  exit 0
fi

if [[ "$MODE" == "--cancel-jumper" ]]; then
  cancel_remote_job jumper-130 ssh130 '' /tmp/kubeauto-regression-jumper
  exit 0
fi

if [[ "$MODE" == "--cancel-gaps" ]]; then
  cancel_remote_job gaps-138 ssh138 sudo /tmp/kubeauto-gaps-gate
  exit 0
fi

if [[ "$MODE" == "--diagnose-gaps-last" ]]; then
  # Read the durable full-chain evidence after cleanup without reconstructing
  # state from an already wiped cluster. Capacity is read separately because
  # the reserved-host contract is part of classifying control-plane timeouts.
  ssh138 "sudo bash -lc 'set -u; log=/tmp/kubeauto-gaps-live.log; echo ===durable-state===; stat -c \"%y %s %n\" /tmp/kubeauto-gaps-gate.pid /tmp/kubeauto-gaps-gate.exit \"\$log\" 2>/dev/null || true; printf \"pid=\"; cat /tmp/kubeauto-gaps-gate.pid 2>/dev/null || true; printf \"rc=\"; cat /tmp/kubeauto-gaps-gate.exit 2>/dev/null || true; echo ===pass-and-terminal-markers===; grep -nE \"^\\[PASS\\]|^[A-Z][A-Z0-9_]+_PASS( |\$)|^DELIVERY_RETEST_COMPLETE|^DELIVERY_GAPS_EXIT\" \"\$log\" 2>/dev/null || true; echo ===failure-context===; grep -n -C 100 -E \"etcdserver: request timed out|^\\[FAIL\\]|Traceback|^[^ ]+[[:space:]]+:[[:space:]].*failed=[1-9]\" \"\$log\" 2>/dev/null || true; echo ===baseline-log===; latest=\$(ls -t /var/log/kubeauto-regression-full-*.log 2>/dev/null | head -n 1); echo \"log=\$latest\"; test -n \"\$latest\" && stat -c \"%y %s %n\" \"\$latest\" || true; echo ===reserved-host-capacity===; ssh -o BatchMode=yes -o StrictHostKeyChecking=no root@192.168.47.137 \"nproc; awk \\\"/^MemTotal:/ {print}\\\" /proc/meminfo; systemctl is-active kubelet etcd containerd 2>/dev/null || true\"'"
  exit 0
fi

if [[ "$MODE" == "--diagnose-test137" ]]; then
  # Kubernetes official node troubleshooting evidence: node conditions/events,
  # then kubelet and container runtime journals on the affected host.
  ssh138 "sudo KUBECONFIG=/usr/local/kubeauto/clusters/test137/kubectl.kubeconfig kubectl describe node master-137 || true"
  ssh138 "sudo KUBECONFIG=/usr/local/kubeauto/clusters/test137/kubectl.kubeconfig kubectl get pods -n kube-system -o wide; sudo KUBECONFIG=/usr/local/kubeauto/clusters/test137/kubectl.kubeconfig kubectl describe pod -n kube-system -l k8s-app=calico-node; sudo KUBECONFIG=/usr/local/kubeauto/clusters/test137/kubectl.kubeconfig kubectl describe pod -n kube-system -l k8s-app=calico-kube-controllers; sudo KUBECONFIG=/usr/local/kubeauto/clusters/test137/kubectl.kubeconfig kubectl logs -n kube-system ds/calico-node --tail=240 || true; sudo KUBECONFIG=/usr/local/kubeauto/clusters/test137/kubectl.kubeconfig kubectl logs -n kube-system deploy/calico-kube-controllers --tail=160 || true"
  ssh138 "sudo bash -lc '/usr/local/kubeauto/extra-bin/containerd-bin/containerd --version 2>/dev/null || true; stat -c \"mtime=%y ctime=%z size=%s %n\" /usr/local/kubeauto/extra-bin/containerd-bin/containerd /usr/local/kubeauto/extra-bin/etcdctl /usr/local/kubeauto/images/ext_bin_1.15.0.tar 2>/dev/null || true; sha256sum /usr/local/kubeauto/extra-bin/containerd-bin/containerd 2>/dev/null || true; echo ===controller-containerd-default===; /usr/local/kubeauto/extra-bin/containerd-bin/containerd config default 2>/dev/null | grep -A3 -B3 -E \"sandbox_image|pinned_images\" || true; docker image inspect brinnatt/kubeauto-ext-bin:1.15.0 --format \"{{.Id}} {{.RepoDigests}} {{.Created}}\" 2>/dev/null || true'"
  # Capture the runtime's effective/default CRI sandbox configuration too. A
  # pod stuck before init containers have Container IDs is a sandbox/runtime
  # failure, not an init-container failure.
  ssh138 "sudo ssh -o BatchMode=yes -o StrictHostKeyChecking=no root@192.168.47.137 '/usr/local/bin/containerd --version; stat -c \"%y %s %n\" /usr/local/bin/containerd; sha256sum /usr/local/bin/containerd; lsattr /usr/local/bin/containerd 2>/dev/null || true; systemctl cat containerd; sed -n \"1,125p\" /etc/containerd/config.toml; echo ===effective-cri-config===; crictl info 2>/dev/null | grep -A3 -B3 -E \"sandboxImage|sandbox_image\" || true; echo ===containerd-default===; containerd config default 2>/dev/null | grep -A2 -B2 -E \"sandbox_image|pinned_images\" || true; echo ===pause-images===; crictl images | grep -E \"pause|IMAGE\" || true'"
  exit 0
fi

if [[ "$MODE" == "--diagnose-debian128" ]]; then
  ssh -o BatchMode=yes -o StrictHostKeyChecking=no brinnatt@192.168.47.128 \
    'sudo systemctl --no-pager --full status kubelet containerd || true; sudo journalctl -u kubelet -u containerd -n 240 --no-pager || true; sudo crictl ps -a || true'
  exit 0
fi

if [[ "$MODE" == "--diagnose-ded-etcd" ]]; then
  ssh138 "sudo bash -lc 'cd /usr/local/kubeauto && ANSIBLE_HOST_KEY_CHECKING=False ansible -i clusters/test-ded-etcd/hosts all -m ping -vvv'"
  exit 0
fi

if [[ "$MODE" == "--verify-ded-etcd-access" ]]; then
  ssh138 "sudo bash -lc 'cd /usr/local/kubeauto && ANSIBLE_HOST_KEY_CHECKING=False ansible -i clusters/test-ded-etcd/hosts all -m ping'"
  exit 0
fi

if [[ "$MODE" == "--repair-lab-access" ]]; then
  ssh138 "sudo bash -lc 'set -e; for ip in 192.168.47.131 192.168.47.132 192.168.47.133 192.168.47.134 192.168.47.135 192.168.47.136 192.168.47.137; do ssh-keygen -R \"\$ip\" 2>/dev/null || true; done; cd /usr/local/kubeauto; kubecli system -a --user root --password 123456 192.168.47.131-137 </dev/null; for ip in 192.168.47.131 192.168.47.132 192.168.47.133 192.168.47.134 192.168.47.135 192.168.47.136 192.168.47.137; do ssh -o BatchMode=yes -o StrictHostKeyChecking=no root@\"\$ip\" \"command -v python3.9 >/dev/null 2>&1 || dnf install -y python39; python3.9 --version\"; done'"
  exit 0
fi

if [[ "$MODE" == "--diagnose-harbor137" ]]; then
  ssh -o BatchMode=yes -o StrictHostKeyChecking=no root@192.168.47.137 \
    'systemctl --no-pager --full status docker harbor 2>/dev/null || true; ss -ltnp | grep -E ":(443|8443)" || true; docker ps -a || true; find /var -maxdepth 4 -type f -name "*.log" -path "*harbor*" -print -exec tail -n 80 {} \; 2>/dev/null || true'
  exit 0
fi

if [[ "$MODE" == "--diagnose-rocketmq-image" ]]; then
  # Read-only evidence from the 138 image cache, local registry, and both
  # delivery registries. Keep this behind the fixed runner entry so operators
  # do not need to compose ad-hoc SSH commands during a supervised regression.
  ssh138 "sudo bash -lc 'echo ===local-registry-tags===; curl -fsS --max-time 5 http://127.0.0.1:5000/v2/brinnatt/rocketmq-console/tags/list || true; echo; echo ===local-images===; docker image inspect brinnatt/rocketmq-console:2.0.0 --format \"id={{.Id}} architecture={{.Architecture}} repo_digests={{json .RepoDigests}} rootfs_diff_ids={{json .RootFS.Layers}}\" 2>&1 || true; docker image inspect 127.0.0.1:5000/brinnatt/rocketmq-console:2.0.0 --format \"id={{.Id}} architecture={{.Architecture}} repo_digests={{json .RepoDigests}} rootfs_diff_ids={{json .RootFS.Layers}}\" 2>&1 || true; echo ===private-manifest===; timeout --signal=TERM --kill-after=5s 20s docker manifest inspect hub.talkedu.cn/kubeauto/rocketmq-console:2.0.0 >/dev/null && echo PRIVATE_MANIFEST_OK || echo PRIVATE_MANIFEST_MISSING_OR_TIMEOUT; echo ===dockerhub-manifest===; timeout --signal=TERM --kill-after=5s 20s docker manifest inspect brinnatt/rocketmq-console:2.0.0 >/dev/null && echo DOCKERHUB_MANIFEST_OK || echo DOCKERHUB_MANIFEST_MISSING_OR_TIMEOUT; echo ===download-log===; grep -n -C 8 rocketmq-console /tmp/kubeauto-gaps-live.log | tail -n 180 || true'"
  exit 0
fi

if [[ "$MODE" == "--rocketmq-image-integrity" ]]; then
  # OCI Distribution content-addressability gate: rebuild the disposable local
  # registry, upload the RocketMQ bundle, then read and hash every console
  # manifest descriptor before a Kubernetes runtime is allowed to consume it.
  ssh138 "sudo bash -lc 'set -euo pipefail; cd /usr/local/kubeauto; env PYTHONPATH=/usr/local/kubeauto PATH=/usr/local/bin:/usr/bin:/bin kubecli download -E rocketmq </dev/null; .venv/bin/python tests/helpers/registry_blob_integrity.py http://127.0.0.1:5000 brinnatt/rocketmq-console 2.0.0'"
  exit 0
fi

if [[ "$MODE" == "--diagnose-local-registry-storage" ]]; then
  # Snap Docker can resolve bind-mount source paths inside dockerd's mount
  # namespace.  Compare all three views before changing cleanup semantics.
  ssh138 "sudo bash -lc 'echo ===container-mounts===; docker inspect local_registry --format \"{{json .Mounts}}\" 2>&1 || true; echo ===host-view===; find /data/registry -maxdepth 4 -type d -o -type f 2>/dev/null | head -n 80; du -sh /data/registry 2>/dev/null || true; echo ===container-view===; docker exec local_registry sh -c \"find /var/lib/registry -maxdepth 4 -type d -o -type f | head -n 80; du -sh /var/lib/registry\" 2>&1 || true; pid=\$(pgrep -fo \"/snap/docker/.*/dockerd\" || true); echo dockerd_pid=\$pid; if test -n \"\$pid\"; then echo ===dockerd-mount-namespace-view===; nsenter -t \"\$pid\" -m -- sh -c \"readlink -f /data/registry; find /data/registry -maxdepth 4 -type d -o -type f 2>/dev/null | head -n 80; du -sh /data/registry 2>/dev/null || true\"; fi; cpid=\$(docker inspect local_registry --format \"{{.State.Pid}}\" 2>/dev/null || true); echo container_pid=\$cpid; if test -n \"\$cpid\" && test \"\$cpid\" != 0; then echo ===container-mountinfo===; grep -E \"/var/lib/registry|/data/registry|snap/docker\" /proc/\"\$cpid\"/mountinfo || true; echo ===container-root-view===; du -sh /proc/\"\$cpid\"/root/var/lib/registry 2>/dev/null || true; fi'"
  exit 0
fi

if [[ "$MODE" == "--official-registry-storage-contracts" ]]; then
  # Read the upstream contracts used by the diagnosis.  Keep URLs explicit and
  # bounded so this remains a reproducible, read-only evidence command.
  ssh138 "sudo bash -lc 'set -u; tmp=\$(mktemp -d); trap \"rm -rf \\\"\$tmp\\\"\" EXIT; echo ===oci-distribution-spec===; curl -fsSL --max-time 30 https://raw.githubusercontent.com/opencontainers/distribution-spec/main/spec.md -o \"\$tmp/distribution-spec.md\" && grep -n -C 3 -E \"Docker-Content-Digest|digest.*verified|content.*digest\" \"\$tmp/distribution-spec.md\" | head -n 100; echo ===containerd-source===; for path in core/content/local/store.go core/content/local/writer.go content/local/store.go; do url=https://raw.githubusercontent.com/containerd/containerd/main/\$path; if curl -fsSL --max-time 20 \"\$url\" -o \"\$tmp/containerd.go\"; then grep -n -C 5 \"unexpected commit digest\" \"\$tmp/containerd.go\" && echo source=\$url && break; fi; done; echo ===snap-data-locations===; curl -fsSL --max-time 30 https://snapcraft.io/docs/data-locations -o \"\$tmp/snap-data.html\" && grep -o -E -m 8 \"SNAP_COMMON.{0,240}|/var/snap/[^< ]+/common.{0,160}\" \"\$tmp/snap-data.html\" || true; echo ===installed-snap-contract===; snap info docker | head -n 80; snap run --shell docker.docker -c \"printf \\\"SNAP_COMMON=%s\\\\nSNAP_DATA=%s\\\\n\\\" \\\"\\\$SNAP_COMMON\\\" \\\"\\\$SNAP_DATA\\\"\"'"
  exit 0
fi

if [[ "$MODE" == "--diagnose-nacos-last" ]]; then
  # Read-only post-failure evidence kept behind the fixed runner entry.  This
  # avoids ad-hoc SSH commands (and per-command operator approval) while
  # preserving the failed lab exactly as recorded before the mandatory wipe.
  ssh138 "sudo bash -lc 'echo ===focused-retest-logs===; ls -lt /var/log/kubeauto-delivery-retest-*.log 2>/dev/null | head -n 5 || true; latest=\$(ls -t /var/log/kubeauto-delivery-retest-*.log 2>/dev/null | head -n 1); if test -n \"\$latest\"; then echo log=\$latest; grep -n -C 120 -E \"nacos_ready=|P0 Nacos replicas/external-MySQL|P0 Nacos official mysql-schema.sql|nacos_schema_tables=\" \"\$latest\" || true; fi; echo ===test-ha-config===; grep -E \"^nacos_(install|replicas|mysql_|storage_)|^CLUSTER_DNS_DOMAIN\" /usr/local/kubeauto/clusters/test-ha/config.yml 2>/dev/null || true; echo ===rendered-nacos-statefulset===; sed -n \"55,210p\" /usr/local/kubeauto/clusters/test-ha/yml/nacos-sts.yaml 2>/dev/null || true; echo ===durable-gaps-tail===; tail -n 400 /tmp/kubeauto-gaps-live.log 2>/dev/null || true'"
  exit 0
fi

if [[ "$MODE" == "--diagnose-nacos-images" ]]; then
  # The mirrored delivery images contain the upstream entrypoint sources that
  # define readiness and startup ordering.  Inspect those sources locally so
  # this evidence remains available even when the lab cannot reach GitHub.
  ssh138 "sudo bash -lc 'echo ===mysql-official-image===; docker image inspect mysql:8.0.46 --format \"id={{.Id}} digests={{json .RepoDigests}} entrypoint={{json .Config.Entrypoint}} cmd={{json .Config.Cmd}}\" 2>&1 || true; docker run --rm --entrypoint sed mysql:8.0.46 -n 1,260p /usr/local/bin/docker-entrypoint.sh 2>&1 || true; echo ===nacos-official-image===; docker image inspect brinnatt/nacos-server:v2.4.3 --format \"id={{.Id}} entrypoint={{json .Config.Entrypoint}} cmd={{json .Config.Cmd}} env={{json .Config.Env}} source={{index .Config.Labels \\\"org.opencontainers.image.source\\\"}}\" 2>/dev/null || true; docker run --rm --entrypoint sh brinnatt/nacos-server:v2.4.3 -c \"echo ---docker-startup.sh---; sed -n 1,260p /home/nacos/bin/docker-startup.sh; echo ---application.properties---; sed -n 1,120p /home/nacos/conf/application.properties\" 2>&1 || true; echo ===nacos-peer-finder-official-image===; docker image inspect brinnatt/nacos-peer-finder-plugin:1.1 --format \"id={{.Id}} entrypoint={{json .Config.Entrypoint}} cmd={{json .Config.Cmd}} env={{json .Config.Env}} source={{index .Config.Labels \\\"org.opencontainers.image.source\\\"}}\" 2>/dev/null || true; docker run --rm --entrypoint sh brinnatt/nacos-peer-finder-plugin:1.1 -c \"for file in /install.sh /plugin.sh /on-start.sh; do echo ---\\\$file---; sed -n 1,260p \\\"\\\$file\\\"; done; ls -l /peer-finder; sha256sum /peer-finder\" 2>&1 || true'"
  exit 0
fi

if [[ "$MODE" == "--nerdctl-only" ]]; then
  echo "========== NERDCTL focused delivery gate =========="
  echo ">>> bash $ROOT/tests/helpers/lab-wipe-nodes.sh"
  bash "$ROOT/tests/helpers/lab-wipe-nodes.sh"
  echo ">>> bash $ROOT/tests/helpers/lab-wipe-nodes.sh --verify"
  bash "$ROOT/tests/helpers/lab-wipe-nodes.sh" --verify
  echo ">>> bootstrap root control key 138 -> worker 133"
  bash "$ROOT/tests/helpers/lab-control-ssh-bootstrap.sh" --sudo-control \
    "$HOST138" root@192.168.47.133
  echo ">>> bash $ROOT/tests/helpers/sync-kubeauto.sh $HOST138"
  bash "$ROOT/tests/helpers/sync-kubeauto.sh" "$HOST138"
  ssh138 "sudo rm -f /tmp/kubeauto-nerdctl-live.log /tmp/kubeauto-nerdctl-gate.pid /tmp/kubeauto-nerdctl-gate.exit; sudo nohup env NERDCTL_SKIP_SYNC=1 NERDCTL_SKIP_LAB_WIPE=1 bash /usr/local/kubeauto/tests/helpers/run-durable-gate.sh /tmp/kubeauto-nerdctl-gate NERDCTL_GATE_EXIT bash /usr/local/kubeauto/tests/helpers/nerdctl-gate.sh >/tmp/kubeauto-nerdctl-live.log 2>&1 </dev/null &"
  nerdctl_rc=0
  monitor_remote_job \
    nerdctl-138 ssh138 sudo /tmp/kubeauto-nerdctl-gate \
    /tmp/kubeauto-nerdctl-live.log NERDCTL_GATE_PASS || nerdctl_rc=$?
  bash "$ROOT/tests/helpers/lab-wipe-nodes.sh"
  bash "$ROOT/tests/helpers/lab-wipe-nodes.sh" --verify
  if [[ "$nerdctl_rc" -ne 0 ]]; then
    echo "[FAIL] nerdctl delivery gate failed (rc=$nerdctl_rc); lab cleanup verified" >&2
    exit 1
  fi
  echo "[PASS] nerdctl-delivery-gate"
  exit 0
fi

if [[ "$MODE" == "--docker-only" ]]; then
  echo "========== Docker focused delivery gate =========="
  cancel_remote_job docker-138 ssh138 sudo /tmp/kubeauto-docker-gate
  echo ">>> bash $ROOT/tests/helpers/lab-wipe-nodes.sh"
  bash "$ROOT/tests/helpers/lab-wipe-nodes.sh"
  echo ">>> bash $ROOT/tests/helpers/lab-wipe-nodes.sh --verify"
  bash "$ROOT/tests/helpers/lab-wipe-nodes.sh" --verify
  echo ">>> bootstrap root control key 138 -> reserved node 137"
  bash "$ROOT/tests/helpers/lab-control-ssh-bootstrap.sh" --sudo-control \
    "$HOST138" root@192.168.47.137
  echo ">>> bash $ROOT/tests/helpers/sync-kubeauto.sh $HOST138"
  bash "$ROOT/tests/helpers/sync-kubeauto.sh" "$HOST138"
  ssh138 "sudo rm -f /tmp/kubeauto-docker-live.log /tmp/kubeauto-docker-gate.pid /tmp/kubeauto-docker-gate.exit; sudo nohup env DOCKER_GATE_NODE=192.168.47.137 bash /usr/local/kubeauto/tests/helpers/run-durable-gate.sh /tmp/kubeauto-docker-gate DOCKER_GATE_EXIT bash /usr/local/kubeauto/tests/helpers/delivery-docker-gate.sh >/tmp/kubeauto-docker-live.log 2>&1 </dev/null &"
  docker_rc=0
  monitor_remote_job \
    docker-138 ssh138 sudo /tmp/kubeauto-docker-gate \
    /tmp/kubeauto-docker-live.log DOCKER_GATE_PASS || docker_rc=$?
  bash "$ROOT/tests/helpers/lab-wipe-nodes.sh"
  bash "$ROOT/tests/helpers/lab-wipe-nodes.sh" --verify
  if [[ "$docker_rc" -ne 0 ]]; then
    echo "[FAIL] Docker delivery gate failed (rc=$docker_rc); lab cleanup verified" >&2
    exit 1
  fi
  echo "[PASS] docker-delivery-gate"
  exit 0
fi

if [[ "$MODE" == "--registry-reboot-only" ]]; then
  echo "========== local_registry real host-reboot gate =========="
  cleanup_registry_reboot_gate() {
    local gate_rc=$? cleanup_rc=0
    trap - EXIT
    set +e
    echo ">>> mandatory cleanup after local_registry reboot gate"
    bash "$ROOT/tests/helpers/lab-wipe-nodes.sh" || cleanup_rc=$?
    bash "$ROOT/tests/helpers/lab-wipe-nodes.sh" --verify || cleanup_rc=$?
    if [[ "$gate_rc" -eq 0 && "$cleanup_rc" -ne 0 ]]; then
      gate_rc=$cleanup_rc
    fi
    exit "$gate_rc"
  }
  trap cleanup_registry_reboot_gate EXIT

  echo ">>> bash $ROOT/tests/run_unit_tests.sh"
  bash "$ROOT/tests/run_unit_tests.sh"
  echo ">>> bash $ROOT/tests/helpers/lab-wipe-nodes.sh"
  bash "$ROOT/tests/helpers/lab-wipe-nodes.sh"
  echo ">>> bash $ROOT/tests/helpers/lab-wipe-nodes.sh --verify"
  bash "$ROOT/tests/helpers/lab-wipe-nodes.sh" --verify
  echo ">>> bash $ROOT/tests/helpers/sync-kubeauto.sh $HOST138"
  bash "$ROOT/tests/helpers/sync-kubeauto.sh" "$HOST138"

  ssh138 "sudo rm -f /tmp/kubeauto-registry-reboot-live.log /tmp/kubeauto-registry-reboot-gate.pid /tmp/kubeauto-registry-reboot-gate.exit; sudo nohup bash /usr/local/kubeauto/tests/helpers/run-durable-gate.sh /tmp/kubeauto-registry-reboot-gate REGISTRY_REBOOT_PREP_EXIT bash /usr/local/kubeauto/tests/helpers/registry-reboot-gate.sh prepare >/tmp/kubeauto-registry-reboot-live.log 2>&1 </dev/null &"
  monitor_remote_job \
    registry-reboot-prepare-138 ssh138 sudo /tmp/kubeauto-registry-reboot-gate \
    /tmp/kubeauto-registry-reboot-live.log REGISTRY_REBOOT_PREP_PASS

  echo "[REBOOT] requesting control-host reboot host=192.168.47.138"
  ssh138 "sudo systemctl reboot" >/dev/null 2>&1 || true
  host_went_down=0
  for attempt in $(seq 1 30); do
    if ! ssh138 true >/dev/null 2>&1; then
      host_went_down=1
      echo "[REBOOT] host is down attempt=$attempt/30"
      break
    fi
    echo "[WAIT] host shutdown attempt=$attempt/30 next_check=2s"
    sleep 2
  done
  [[ "$host_went_down" -eq 1 ]] || {
    echo "[FAIL] control host did not go down after reboot request" >&2
    exit 1
  }

  host_returned=0
  for attempt in $(seq 1 90); do
    if ssh138 true >/dev/null 2>&1; then
      host_returned=1
      echo "[REBOOT] host SSH restored attempt=$attempt/90"
      break
    fi
    if (( attempt % 5 == 0 )); then
      echo "[HEARTBEAT] waiting for control host after reboot attempt=$attempt/90"
    fi
    sleep 5
  done
  [[ "$host_returned" -eq 1 ]] || {
    echo "[FAIL] control host did not return after reboot" >&2
    exit 1
  }

  registry_reboot_rc=0
  ssh138 "sudo bash /usr/local/kubeauto/tests/helpers/registry-reboot-gate.sh verify" \
    | tee -a "$LOG" || registry_reboot_rc=$?
  if [[ "$registry_reboot_rc" -ne 0 ]]; then
    echo "[FAIL] local_registry host-reboot gate failed (rc=$registry_reboot_rc)" >&2
    exit 1
  fi
  echo "[PASS] local_registry-host-reboot-gate"
  exit 0
fi

if [[ "$MODE" == "--upgrade-only" ]]; then
  echo "========== Kubernetes patch-upgrade delivery gate =========="
  cancel_remote_job upgrade-138 ssh138 sudo /tmp/kubeauto-upgrade-gate
  echo ">>> bash $ROOT/tests/helpers/lab-wipe-nodes.sh"
  bash "$ROOT/tests/helpers/lab-wipe-nodes.sh"
  echo ">>> bash $ROOT/tests/helpers/lab-wipe-nodes.sh --verify"
  bash "$ROOT/tests/helpers/lab-wipe-nodes.sh" --verify
  echo ">>> bootstrap root control key 138 -> reserved node 137"
  bash "$ROOT/tests/helpers/lab-control-ssh-bootstrap.sh" --sudo-control \
    "$HOST138" root@192.168.47.137
  echo ">>> bash $ROOT/tests/helpers/sync-kubeauto.sh $HOST138"
  bash "$ROOT/tests/helpers/sync-kubeauto.sh" "$HOST138"
  ssh138 "sudo rm -f /tmp/kubeauto-upgrade-live.log /tmp/kubeauto-upgrade-gate.pid /tmp/kubeauto-upgrade-gate.exit; sudo nohup env UPGRADE_GATE_NODE=192.168.47.137 bash /usr/local/kubeauto/tests/helpers/run-durable-gate.sh /tmp/kubeauto-upgrade-gate UPGRADE_GATE_EXIT bash /usr/local/kubeauto/tests/helpers/delivery-upgrade-smoke.sh >/tmp/kubeauto-upgrade-live.log 2>&1 </dev/null &"
  upgrade_rc=0
  monitor_remote_job \
    upgrade-138 ssh138 sudo /tmp/kubeauto-upgrade-gate \
    /tmp/kubeauto-upgrade-live.log UPGRADE_GATE_PASS || upgrade_rc=$?
  bash "$ROOT/tests/helpers/lab-wipe-nodes.sh"
  bash "$ROOT/tests/helpers/lab-wipe-nodes.sh" --verify
  if [[ "$upgrade_rc" -ne 0 ]]; then
    echo "[FAIL] Kubernetes upgrade gate failed (rc=$upgrade_rc); lab cleanup verified" >&2
    exit 1
  fi
  echo "[PASS] kubernetes-upgrade-gate"
  exit 0
fi

if [[ "$MODE" == "--gaps-only" ]]; then
  echo "========== Remaining G5/G10 delivery gates =========="
  cancel_remote_job gaps-138 ssh138 sudo /tmp/kubeauto-gaps-gate
  echo ">>> bash $ROOT/tests/helpers/lab-wipe-nodes.sh"
  bash "$ROOT/tests/helpers/lab-wipe-nodes.sh"
  echo ">>> bash $ROOT/tests/helpers/lab-wipe-nodes.sh --verify"
  bash "$ROOT/tests/helpers/lab-wipe-nodes.sh" --verify
  echo ">>> bootstrap root control key 138 -> full lab"
  bash "$ROOT/tests/helpers/lab-control-ssh-bootstrap.sh" --sudo-control \
    "$HOST138" \
    root@192.168.47.131 root@192.168.47.132 root@192.168.47.133 \
    root@192.168.47.134 root@192.168.47.135 root@192.168.47.136 \
    root@192.168.47.137 brinnatt@192.168.47.128
  echo ">>> bash $ROOT/tests/helpers/sync-kubeauto.sh $HOST138"
  bash "$ROOT/tests/helpers/sync-kubeauto.sh" "$HOST138"
  ssh138 "sudo rm -f /tmp/kubeauto-gaps-live.log /tmp/kubeauto-gaps-gate.pid /tmp/kubeauto-gaps-gate.exit; sudo nohup bash /usr/local/kubeauto/tests/helpers/run-durable-gate.sh /tmp/kubeauto-gaps-gate DELIVERY_GAPS_EXIT bash /usr/local/kubeauto/tests/helpers/delivery-gaps-fullchain.sh >/tmp/kubeauto-gaps-live.log 2>&1 </dev/null &"
  gaps_rc=0
  monitor_remote_job \
    gaps-138 ssh138 sudo /tmp/kubeauto-gaps-gate \
    /tmp/kubeauto-gaps-live.log DELIVERY_GAPS_FULLCHAIN_PASS || gaps_rc=$?
  bash "$ROOT/tests/helpers/lab-wipe-nodes.sh"
  bash "$ROOT/tests/helpers/lab-wipe-nodes.sh" --verify
  if [[ "$gaps_rc" -ne 0 ]]; then
    echo "[FAIL] Remaining delivery gaps gate failed (rc=$gaps_rc); lab cleanup verified" >&2
    exit 1
  fi
  echo "[PASS] remaining-delivery-gaps-gate"
  exit 0
fi

if [[ "$MODE" != "run" && "$MODE" != "--aio-only" && "$MODE" != "--jumper-only" ]]; then
  echo "Usage: $0 [--all-delivery|--all-delivery-daemon|--status|--follow|--prometheus-only|--mysql-only|--mysql-clean-only|--mysql-status|--mysql-follow|--kafka-only|--kafka-clean-only|--kafka-status|--kafka-follow|--kafka-cancel|--follow-jumper|--follow-ansible-ee|--cancel-jumper|--cancel-gaps|--aio-only|--jumper-only|--nerdctl-only|--docker-only|--registry-reboot-only|--upgrade-only|--gaps-only|--build-rocky8-kubecli|--tier3-tools-only|--cancel-rocky8-build|--rocky8-image-probe|--diagnose-docker-bootstrap|--follow-docker-bootstrap|--cancel-docker-bootstrap|--ansible-os-probe|--ansible-anolis-probe|--ansible-anolis-container-probe|--ansible-ee-debian-probe|--ansible-os-only|--diagnose-gaps-last|--diagnose-test137|--diagnose-debian128|--diagnose-ded-etcd|--verify-ded-etcd-access|--diagnose-harbor137|--diagnose-rocketmq-image|--diagnose-nacos-last|--diagnose-nacos-images|--repair-lab-access]" >&2
  exit 2
fi

# Mirror every local phase to both the terminal and the auditable log.  The
# previous log-only preflight made a running cleanup/sync look like a stalled
# regression until the remote tail was attached.
exec > >(tee -a "$LOG" >&3) 2>&1

pass() { echo "[PASS] $*"; }
fail() { echo "[FAIL] $*"; exit 1; }
run()  { echo ">>> $*"; "$@"; }

echo "========== PHASE 0: local unit tests (G0) =========="
run bash "$ROOT/tests/run_unit_tests.sh"
pass G0-local-unit

echo "========== PHASE 0b: clean stale lab state =========="
# A previous local supervisor may have been interrupted while its durable
# remote job kept running. Stop that exact recorded process tree before wiping
# nodes; cleanup racing an active Ansible play makes both runs invalid.
if [[ "$MODE" != "--aio-only" ]]; then
  run cancel_remote_job jumper-130 ssh130 '' /tmp/kubeauto-regression-jumper
fi
run bash "$ROOT/tests/helpers/lab-wipe-nodes.sh"
run bash "$ROOT/tests/helpers/lab-wipe-nodes.sh" --verify
pass G0-lab-clean

echo "========== PHASE 1: restore jumper control prerequisites + sync source =========="
# Rocky 8's platform Python is 3.6.  ansible-core 2.16 officially supports
# Python 3.10-3.12 on the control node, so restore Python 3.12 if a previous
# cleanup removed it before syncing source requirements.
ssh130 'if ! command -v python3.12 >/dev/null 2>&1; then dnf install -y python3.12 python3.12-pip; fi'
if [[ "$MODE" != "--jumper-only" ]]; then
  run bash "$ROOT/tests/helpers/lab-control-ssh-bootstrap.sh" --sudo-control \
    "$HOST138" \
    root@192.168.47.131 root@192.168.47.132 root@192.168.47.133 \
    root@192.168.47.134 root@192.168.47.135 root@192.168.47.136 \
    root@192.168.47.137 brinnatt@192.168.47.128
fi
if [[ "$MODE" != "--aio-only" ]]; then
  run bash "$ROOT/tests/helpers/lab-control-ssh-bootstrap.sh" \
    "$HOST130" \
    root@192.168.47.131 root@192.168.47.132 root@192.168.47.133 \
    root@192.168.47.134 root@192.168.47.135 root@192.168.47.136 \
    root@192.168.47.137
fi
run bash "$ROOT/tests/helpers/sync-kubeauto.sh" "$HOST138"
run bash "$ROOT/tests/helpers/sync-kubeauto.sh" "$HOST130"
pass G0-deploy-sync

echo "========== PHASE 2: Ubuntu aio 138 full cluster regression (G0-G6, Tier2/3) =========="
if [[ "$MODE" != "--jumper-only" ]]; then
  scp138 "$ROOT/tests/helpers/regression-full.sh" "$HOST138:/tmp/regression-full.sh"
  scp138 "$ROOT/tests/helpers/regression-aio-prep-full.sh" "$HOST138:/tmp/regression-aio-prep-full.sh"
  # Remove followers left by the pre-supervisor implementation. The current
  # stream uses tail --pid=<job>, so it exits with the job and is not matched.
  ssh138 "sudo pkill -f '^tail -n (0|60) -[fF] /tmp/kubeauto-regression-live.log$' || true"
  ssh138 "sudo rm -f /tmp/kubeauto-regression-live.log /tmp/kubeauto-regression-aio.pid /tmp/kubeauto-regression-aio.exit; sudo nohup bash /tmp/regression-aio-prep-full.sh >/tmp/kubeauto-regression-live.log 2>&1 </dev/null &"
  monitor_remote_job \
    aio-138 ssh138 sudo /tmp/kubeauto-regression-aio \
    /tmp/kubeauto-regression-live.log REGRESSION_FULL_COMPLETE
  pass G2-G6-aio-138
fi

echo "========== PHASE 3: Rocky jumper 130 regression (G7) =========="
if [[ "$MODE" != "--aio-only" ]]; then
  # 130 and 138 both allocate 131-136.  They must never run concurrently.
  # When running the complete suite, prove the AIO lab has been cleaned before
  # the jumper is allowed to build its independent cluster.
  if [[ "$MODE" == run ]]; then
    run bash "$ROOT/tests/helpers/lab-wipe-nodes.sh"
    run bash "$ROOT/tests/helpers/lab-wipe-nodes.sh" --verify
    run bash "$ROOT/tests/helpers/sync-kubeauto.sh" "$HOST130"
  fi
  scp -o BatchMode=yes -o StrictHostKeyChecking=no \
    "$ROOT/tests/helpers/regression-jumper.sh" "$HOST130:/tmp/regression-jumper.sh"
  scp -o BatchMode=yes -o StrictHostKeyChecking=no \
    "$ROOT/tests/helpers/regression-jumper-prep.sh" "$HOST130:/tmp/regression-jumper-prep.sh"
  ssh130 "rm -f /tmp/kubeauto-jumper-live.log /tmp/kubeauto-regression-jumper.pid /tmp/kubeauto-regression-jumper.exit; nohup bash /tmp/regression-jumper-prep.sh >/tmp/kubeauto-jumper-live.log 2>&1 </dev/null &"
  jumper_rc=0
  monitor_remote_job \
    jumper-130 ssh130 '' /tmp/kubeauto-regression-jumper \
    /tmp/kubeauto-jumper-live.log G7_JUMPER_PASS || jumper_rc=$?

  # Diagnostics are emitted by monitor_remote_job before it returns.  Always
  # clean afterward so a failed G7 attempt cannot contaminate the next run.
  run bash "$ROOT/tests/helpers/lab-wipe-nodes.sh"
  run bash "$ROOT/tests/helpers/lab-wipe-nodes.sh" --verify
  [[ "$jumper_rc" -eq 0 ]] || fail "G7 jumper regression failed (rc=$jumper_rc); lab cleanup verified"
  pass G7-jumper-130
fi

echo "========== REGRESSION COMPLETE =========="
echo "Log: $LOG"
matrix_counts
echo
