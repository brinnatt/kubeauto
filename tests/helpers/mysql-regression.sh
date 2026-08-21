#!/usr/bin/env bash
# Independent Percona PXC live gate. It does not execute the enterprise matrix.
set -Eeuo pipefail

BASE="${KUBEAUTO_BASE:-/usr/local/kubeauto}"
MYSQL_NAMESPACE="${MYSQL_NAMESPACE:-mysql}"
MYSQL_OPERATOR_NAMESPACE="${MYSQL_OPERATOR_NAMESPACE:-mysql-operator}"
MYSQL_CLUSTER="${MYSQL_CLUSTER:-cluster1}"
MYSQL_STORAGE_CLASS="${MYSQL_STORAGE_CLASS:-local-path}"
REGISTRY_HOST="registry.talkschool.cn:5000"
REGISTRY_IP="${MYSQL_REGISTRY_IP:-$(hostname -I | awk '{print $1}')}"
REGISTRY_NAME="kubeauto-mysql-registry"
REGISTRY_DATA="/var/lib/kubeauto-mysql-registry"
SOURCE_PREFIX="${MYSQL_IMAGE_SOURCE_PREFIX:-docker.io}"
SOURCE_FALLBACK_PREFIX="${MYSQL_IMAGE_SOURCE_FALLBACK_PREFIX:-}"
VERIFY_PREFIXES="${MYSQL_IMAGE_VERIFY_PREFIXES:-${MYSQL_IMAGE_VERIFY_PREFIX:-}}"
STAGE_NODE="${MYSQL_IMAGE_STAGE_NODE:-}"
MARKER="kubeauto-mysql-gate"
MYSQL_EXEC_TIMEOUT="${MYSQL_EXEC_TIMEOUT:-30}"
MINIO_IMAGE="minio/minio:RELEASE.2025-04-08T15-41-24Z"
MINIO_MC_IMAGE="minio/mc:RELEASE.2025-04-08T15-39-49Z"
SYSBENCH_IMAGE="perconalab/sysbench:1.1"
MYSQL_ISOLATION_NODE=""
MYSQL_ISOLATION_POD_IP=""
MYSQL_ISOLATION_PEER_IPS=()
MYSQL_ISOLATION_TABLES=()

pass() { echo "[PASS] $*"; }
fail() { echo "[FAIL] $*" >&2; exit 1; }

cleanup_mysql_isolation() {
  local index peer table priority
  [[ -n "$MYSQL_ISOLATION_NODE" && -n "$MYSQL_ISOLATION_POD_IP" ]] || return 0
  for index in "${!MYSQL_ISOLATION_PEER_IPS[@]}"; do
    peer="${MYSQL_ISOLATION_PEER_IPS[$index]}"
    table="${MYSQL_ISOLATION_TABLES[$index]:-}"
    priority="$((31990 + index))"
    [[ -n "$peer" ]] || continue
    ssh -o BatchMode=yes -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
      -o ConnectTimeout=10 "root@${MYSQL_ISOLATION_NODE}" \
      "iptables -D FORWARD -s ${MYSQL_ISOLATION_POD_IP} -d ${peer} -j REJECT 2>/dev/null || true; iptables -D FORWARD -s ${peer} -d ${MYSQL_ISOLATION_POD_IP} -j REJECT 2>/dev/null || true; ip rule del priority ${priority} to ${peer}/32 lookup ${table} 2>/dev/null || true; ip route del blackhole ${peer}/32 table ${table} 2>/dev/null || true" \
      >/dev/null 2>&1 || true
  done
  MYSQL_ISOLATION_NODE=""
  MYSQL_ISOLATION_POD_IP=""
  MYSQL_ISOLATION_PEER_IPS=()
  MYSQL_ISOLATION_TABLES=()
}

diagnostics() {
  rc=$?
  cleanup_mysql_isolation
  if [[ "$rc" -ne 0 ]]; then
    echo "========== MYSQL/PXC FAILURE DIAGNOSTICS =========="
    kubectl get nodes -o wide || true
    kubectl -n "$MYSQL_OPERATOR_NAMESPACE" get all -o wide || true
    kubectl -n "$MYSQL_NAMESPACE" get pxc,pod,pvc,svc,endpointslice -o wide || true
    kubectl -n "$MYSQL_NAMESPACE" get events --sort-by=.lastTimestamp | tail -100 || true
    kubectl -n "$MYSQL_OPERATOR_NAMESPACE" logs deployment/pxc-operator --tail=200 || true
    for pod in $(kubectl -n "$MYSQL_NAMESPACE" get pod -l app.kubernetes.io/component=pxc -o name 2>/dev/null); do
      kubectl -n "$MYSQL_NAMESPACE" logs "$pod" -c pxc --tail=120 || true
      kubectl -n "$MYSQL_NAMESPACE" logs "$pod" -c pxc --previous --tail=120 || true
    done
  fi
  unset ROOT_PASSWORD APP_PASSWORD OLD_ROOT_PASSWORD NEW_ROOT_PASSWORD \
    MINIO_ROOT_USER MINIO_ROOT_PASSWORD || true
}
trap diagnostics EXIT

if [[ -n "$STAGE_NODE" && ! "$STAGE_NODE" =~ ^[a-zA-Z0-9.-]+$ ]]; then
  fail "invalid MYSQL_IMAGE_STAGE_NODE"
fi
if [[ ! "$MYSQL_EXEC_TIMEOUT" =~ ^[0-9]+$ ]] \
  || (( MYSQL_EXEC_TIMEOUT < 5 || MYSQL_EXEC_TIMEOUT > 120 )); then
  fail "invalid MYSQL_EXEC_TIMEOUT"
fi

node_ips() {
  kubectl get nodes -o wide --no-headers | awk '{print $6}'
}

official_manifest_digest() {
  local repository="$1" tag="$2" token headers
  token="$(curl -fsSL --max-time 30 \
    "https://auth.docker.io/token?service=registry.docker.io&scope=repository:${repository}:pull" | jq -r '.token')"
  [[ -n "$token" && "$token" != null ]] || return 1
  headers="$(curl -fsSI --max-time 30 \
    -H "Authorization: Bearer ${token}" \
    -H 'Accept: application/vnd.oci.image.index.v1+json, application/vnd.docker.distribution.manifest.list.v2+json, application/vnd.oci.image.manifest.v1+json, application/vnd.docker.distribution.manifest.v2+json' \
    "https://registry-1.docker.io/v2/${repository}/manifests/${tag}")"
  tr -d '\r' <<<"$headers" | awk -F': ' 'tolower($1) == "docker-content-digest" {print $2; exit}'
}

registry_manifest_digest() {
  local prefix="$1" repository="$2" tag="$3" url headers challenge realm service scope token digest
  url="https://${prefix}/v2/${repository}/manifests/${tag}"
  headers="$(curl -sSI --max-time 30 \
    -H 'Accept: application/vnd.oci.image.index.v1+json, application/vnd.docker.distribution.manifest.list.v2+json, application/vnd.oci.image.manifest.v1+json, application/vnd.docker.distribution.manifest.v2+json' \
    "$url")"
  digest="$(tr -d '\r' <<<"$headers" | awk -F': ' 'tolower($1) == "docker-content-digest" {print $2; exit}')"
  if [[ "$digest" == sha256:* ]]; then
    printf '%s\n' "$digest"
    return 0
  fi
  challenge="$(tr -d '\r' <<<"$headers" | awk 'tolower($1) == "www-authenticate:" {$1=""; sub(/^ /, ""); print; exit}')"
  realm="$(sed -n 's/.*realm="\([^"]*\)".*/\1/p' <<<"$challenge")"
  service="$(sed -n 's/.*service="\([^"]*\)".*/\1/p' <<<"$challenge")"
  scope="$(sed -n 's/.*scope="\([^"]*\)".*/\1/p' <<<"$challenge")"
  [[ -n "$realm" && -n "$service" && -n "$scope" ]] || return 1
  token="$(curl -fsSL --max-time 30 --get \
    --data-urlencode "service=${service}" --data-urlencode "scope=${scope}" "$realm" \
    | jq -r '.token // .access_token // empty')"
  [[ -n "$token" ]] || return 1
  headers="$(curl -fsSI --max-time 30 \
    -H "Authorization: Bearer ${token}" \
    -H 'Accept: application/vnd.oci.image.index.v1+json, application/vnd.docker.distribution.manifest.list.v2+json, application/vnd.oci.image.manifest.v1+json, application/vnd.docker.distribution.manifest.v2+json' \
    "$url")"
  tr -d '\r' <<<"$headers" | awk -F': ' 'tolower($1) == "docker-content-digest" {print $2; exit}'
}

matching_verifier_digest() {
  local source_digest="$1" repository="$2" tag="$3" prefix digest
  local -a prefixes
  IFS=',' read -r -a prefixes <<<"$VERIFY_PREFIXES"
  for prefix in "${prefixes[@]}"; do
    [[ "$prefix" =~ ^[A-Za-z0-9.-]+(:[0-9]+)?$ ]] || fail "invalid runtime verifier prefix"
    digest="$(registry_manifest_digest "$prefix" "$repository" "$tag" 2>/dev/null || true)"
    if [[ "$digest" == "$source_digest" ]]; then
      printf '%s|%s\n' "$digest" "$prefix"
      return 0
    fi
    if [[ "$digest" == sha256:* ]]; then
      echo "[WARN] runtime verifier digest mismatch repository=${repository} tag=${tag} verifier=${prefix}" >&2
    else
      echo "[WARN] runtime verifier manifest unavailable repository=${repository} tag=${tag} verifier=${prefix}" >&2
    fi
  done
  return 1
}

source_repository() {
  local repository="$1"
  if [[ "$SOURCE_PREFIX" == "hub.talkedu.cn" ]]; then
    case "$repository" in
      percona/percona-xtradb-cluster-operator) printf 'kubeauto/percona-xtradb-cluster-operator\n' ;;
      percona/percona-xtradb-cluster) printf 'kubeauto/percona-xtradb-cluster\n' ;;
      percona/percona-xtrabackup) printf 'kubeauto/percona-xtrabackup\n' ;;
      percona/haproxy) printf 'kubeauto/percona-haproxy\n' ;;
      percona/fluentbit) printf 'kubeauto/percona-fluentbit\n' ;;
      minio/minio) printf 'kubeauto/minio\n' ;;
      *) printf '%s\n' "$repository" ;;
    esac
    return
  fi
  printf '%s\n' "$repository"
}

wait_pxc_ready() {
  local attempt state pxc_ready haproxy_ready pvc_bound pxc_ready_pods haproxy_ready_pods
  for attempt in $(seq 1 270); do
    state="$(kubectl -n "$MYSQL_NAMESPACE" get pxc "$MYSQL_CLUSTER" -o jsonpath='{.status.state}' 2>/dev/null || true)"
    pxc_ready="$(kubectl -n "$MYSQL_NAMESPACE" get pxc "$MYSQL_CLUSTER" -o jsonpath='{.status.pxc.ready}' 2>/dev/null || true)"
    haproxy_ready="$(kubectl -n "$MYSQL_NAMESPACE" get pxc "$MYSQL_CLUSTER" -o jsonpath='{.status.haproxy.ready}' 2>/dev/null || true)"
    pvc_bound="$(kubectl -n "$MYSQL_NAMESPACE" get pvc -o jsonpath='{range .items[*]}{.status.phase}{"\n"}{end}' 2>/dev/null | grep -cx Bound || true)"
    pxc_ready_pods="$(kubectl -n "$MYSQL_NAMESPACE" get pod \
      -l app.kubernetes.io/component=pxc -o json 2>/dev/null \
      | jq '[.items[] | select(.metadata.deletionTimestamp == null) | select(.status.phase == "Running") | select(any(.status.conditions[]?; .type == "Ready" and .status == "True"))] | length' \
      || true)"
    haproxy_ready_pods="$(kubectl -n "$MYSQL_NAMESPACE" get pod \
      -l app.kubernetes.io/component=haproxy -o json 2>/dev/null \
      | jq '[.items[] | select(.metadata.deletionTimestamp == null) | select(.status.phase == "Running") | select(any(.status.conditions[]?; .type == "Ready" and .status == "True"))] | length' \
      || true)"
    if [[ "$state" == ready && "$pxc_ready" == 3 && "$haproxy_ready" == 3 \
      && "$pxc_ready_pods" == 3 && "$haproxy_ready_pods" == 3 && "$pvc_bound" -ge 3 ]]; then
      echo "PXC_READY state=${state} pxc=${pxc_ready} haproxy=${haproxy_ready} pxc_ready_pods=${pxc_ready_pods} haproxy_ready_pods=${haproxy_ready_pods} pvc=${pvc_bound}"
      return 0
    fi
    if (( attempt % 12 == 0 )); then
      echo "[WAIT] PXC attempt=${attempt}/270 state=${state:-missing} pxc=${pxc_ready:-0} haproxy=${haproxy_ready:-0} pxc_ready_pods=${pxc_ready_pods:-0} haproxy_ready_pods=${haproxy_ready_pods:-0} pvc=${pvc_bound}"
      kubectl -n "$MYSQL_NAMESPACE" get pod,pvc -o wide || true
    fi
    sleep 10
  done
  return 1
}

wait_recreated_pod_ready() {
  local pod="$1" old_uid="$2" timeout_seconds="${3:-1800}"
  local attempt=0 current_uid="" phase="" deletion_timestamp="" ready=""
  local deadline=$((SECONDS + timeout_seconds)) old_uid_gone=false state

  RECREATED_POD_UID=""
  while (( SECONDS < deadline )); do
    attempt=$((attempt + 1))
    state="$(kubectl -n "$MYSQL_NAMESPACE" get pod "$pod" -o json 2>/dev/null \
      | jq -r '[.metadata.uid, (.status.phase // "missing"), (.metadata.deletionTimestamp // "none"), (([.status.conditions[]? | select(.type == "Ready") | .status][0]) // "False")] | @tsv' \
      || true)"
    if [[ -z "$state" ]]; then
      current_uid=""
      phase="missing"
      deletion_timestamp="none"
      ready="False"
      old_uid_gone=true
    else
      IFS=$'\t' read -r current_uid phase deletion_timestamp ready <<<"$state"
      if [[ "$current_uid" != "$old_uid" ]]; then
        old_uid_gone=true
      fi
    fi

    if [[ "$old_uid_gone" == true && -n "$current_uid" && "$current_uid" != "$old_uid" \
      && "$deletion_timestamp" == none && "$phase" == Running && "$ready" == True ]]; then
      RECREATED_POD_UID="$current_uid"
      echo "PXC_POD_RECREATED pod=${pod} old_uid=${old_uid} new_uid=${current_uid} phase=${phase} ready=${ready}"
      return 0
    fi
    if (( attempt == 1 || attempt % 12 == 0 )); then
      echo "[WAIT] PXC pod reconstruction pod=${pod} old_uid_gone=${old_uid_gone} current_uid=${current_uid:-missing} phase=${phase:-missing} deleting=${deletion_timestamp:-none} ready=${ready:-False} elapsed=$((timeout_seconds - (deadline - SECONDS)))s timeout=${timeout_seconds}s"
    fi
    sleep 5
  done
  return 1
}

wait_backup_state() {
  local backup="$1" expected="$2" timeout_seconds="${3:-3600}"
  local state="" error="" deadline=$((SECONDS + timeout_seconds))
  while (( SECONDS < deadline )); do
    state="$(kubectl -n "$MYSQL_NAMESPACE" get pxc-backup "$backup" \
      -o jsonpath='{.status.state}' 2>/dev/null || true)"
    if [[ "$state" == "$expected" ]]; then
      echo "PXC_BACKUP_STATE name=${backup} state=${state}"
      return 0
    fi
    if [[ "$state" == Failed && "$expected" != Failed ]]; then
      error="$(kubectl -n "$MYSQL_NAMESPACE" get pxc-backup "$backup" \
        -o jsonpath='{.status.error}' 2>/dev/null || true)"
      echo "PXC_BACKUP_FAILED name=${backup} error=${error}" >&2
      return 1
    fi
    sleep 10
  done
  kubectl -n "$MYSQL_NAMESPACE" get pxc-backup "$backup" -o yaml || true
  return 1
}

wait_restore_state() {
  local restore="$1" expected="$2" timeout_seconds="${3:-3600}"
  local state="" comments="" deadline=$((SECONDS + timeout_seconds))
  while (( SECONDS < deadline )); do
    state="$(kubectl -n "$MYSQL_NAMESPACE" get pxc-restore "$restore" \
      -o jsonpath='{.status.state}' 2>/dev/null || true)"
    if [[ "$state" == "$expected" ]]; then
      echo "PXC_RESTORE_STATE name=${restore} state=${state}"
      return 0
    fi
    if [[ "$state" == Failed && "$expected" != Failed ]]; then
      comments="$(kubectl -n "$MYSQL_NAMESPACE" get pxc-restore "$restore" \
        -o jsonpath='{.status.comments}' 2>/dev/null || true)"
      echo "PXC_RESTORE_FAILED name=${restore} comments=${comments}" >&2
      return 1
    fi
    sleep 10
  done
  kubectl -n "$MYSQL_NAMESPACE" get pxc-restore "$restore" -o yaml || true
  return 1
}

wait_pitr_deployment_ready() {
  local deployment="${MYSQL_CLUSTER}-pitr"
  local attempt
  # Re-enabling PITR is asynchronous: the Operator may remove the old
  # Deployment before creating the new one. Treat that bounded gap as a
  # convergence window, not as a failed rollout.
  for attempt in $(seq 1 90); do
    if kubectl -n "$MYSQL_NAMESPACE" get deployment "$deployment" >/dev/null 2>&1; then
      kubectl -n "$MYSQL_NAMESPACE" rollout status \
        deployment/"$deployment" --timeout=15m
      return 0
    fi
    sleep 2
  done
  kubectl -n "$MYSQL_NAMESPACE" get pxc "$MYSQL_CLUSTER" -o yaml || true
  return 1
}

create_backup() {
  local backup="$1"
  cat <<EOF | kubectl apply -f - >/dev/null
apiVersion: pxc.percona.com/v1
kind: PerconaXtraDBClusterBackup
metadata:
  name: ${backup}
  namespace: ${MYSQL_NAMESPACE}
  labels:
    kubeauto.io/component: percona-pxc-test
spec:
  pxcCluster: ${MYSQL_CLUSTER}
  storageName: s3-full
EOF
  wait_backup_state "$backup" Succeeded 3600
}

minio_object_evidence() {
  local prefix="$1"
  kubectl -n "$MYSQL_NAMESPACE" exec mysql-gate-mc -- sh -eu -c '
    mc alias set gate https://minio-service.mysql.svc:9000 \
      "$MINIO_ROOT_USER" "$MINIO_ROOT_PASSWORD" --api S3v4 --path on >/dev/null
    listing="$(mc find "gate/$1" --print "{}")"
    count=0
    bytes=0
    sample=""
    while IFS= read -r object; do
      [ -n "$object" ] || continue
      size="$(mc cat "$object" | wc -c)"
      case "$size" in
        ""|*[!0-9]*) exit 1 ;;
      esac
      count=$((count + 1))
      bytes=$((bytes + size))
      if [ -z "$sample" ] && [ "$size" -gt 0 ]; then
        sample="$object"
      fi
    done <<EOF
$listing
EOF
    test "$count" -gt 0 && test "$bytes" -gt 0 && test -n "$sample"
    set -- $(mc cat "$sample" | sha256sum)
    digest="${1:-}"
    case "$digest" in
      [0-9a-f][0-9a-f][0-9a-f][0-9a-f]*) ;;
      *) exit 1 ;;
    esac
    printf "%s|%s|%s\n" "$count" "$bytes" "$digest"
  ' sh "$prefix"
}

sysbench_command() {
  local operation="$1" threads="$2" duration="$3"
  kubectl -n "$MYSQL_NAMESPACE" exec mysql-gate-sysbench -- sh -eu -c '
    exec sysbench /usr/share/sysbench/oltp_read_write.lua \
      --db-driver=mysql \
      --mysql-host="cluster1-haproxy.mysql.svc" \
      --mysql-port=3306 \
      --mysql-user=root \
      --mysql-password="$MYSQL_PASSWORD" \
      --mysql-db=pxc_benchmark \
      --mysql-ssl=VERIFY_CA \
      --mysql-ssl-ca=/etc/mysql/ssl-internal/ca.crt \
      --mysql_storage_engine=innodb \
      --tables=4 \
      --table-size=10000 \
      --threads="$1" \
      --time="$2" \
      --events=0 \
      --report-interval=5 \
      --rand-type=pareto \
      --mysql-ignore-errors=all \
      "$3"
  ' sh "$threads" "$duration" "$operation"
}

run_sysbench_measurement() {
  local label="$1" threads="$2" duration="$3" output tps p95 errors rc=0
  set +e
  output="$(sysbench_command run "$threads" "$duration" 2>&1)"
  rc=$?
  set -e
  echo "$output"
  if [[ "$rc" -ne 0 ]]; then
    echo "SYSBENCH_COMMAND_FAILED label=${label} threads=${threads} seconds=${duration} rc=${rc}" >&2
    return "$rc"
  fi
  tps="$(awk '/transactions:/ {gsub(/[()]/, ""); print $(NF-2)}' <<<"$output" | tail -n 1)"
  p95="$(awk '/95th percentile:/ {print $NF}' <<<"$output" | tail -n 1)"
  errors="$(awk '/ignored errors:/ {print $3}' <<<"$output" | tail -n 1)"
  [[ "$tps" =~ ^[0-9]+([.][0-9]+)?$ && "$p95" =~ ^[0-9]+([.][0-9]+)?$ ]] || return 1
  echo "SYSBENCH_RESULT label=${label} threads=${threads} seconds=${duration} tps=${tps} p95_ms=${p95} ignored_errors=${errors:-0}"
}

mysql_exec() {
  local host="$1" sql="$2" pod="${3:-${MYSQL_CLUSTER}-pxc-0}"
  mysql_exec_as root "$ROOT_PASSWORD" "$host" "$sql" "$pod"
}

mysql_exec_as() {
  local user="$1" password="$2" host="$3" sql="$4" pod="${5:-${MYSQL_CLUSTER}-pxc-0}"
  printf '%s\n' "$password" | timeout --signal=TERM --kill-after=5s \
    "${MYSQL_EXEC_TIMEOUT}s" kubectl -n "$MYSQL_NAMESPACE" exec -i "$pod" -c pxc -- sh -eu -c '
    IFS= read -r MYSQL_PWD
    export MYSQL_PWD
    exec mysql --user="$1" --protocol=TCP -h "$2" --batch --skip-column-names -e "$3"
  ' sh "$user" "$host" "$sql"
}

mysql_exec_tls_as() {
  local user="$1" password="$2" host="$3" sql="$4" pod="${5:-${MYSQL_CLUSTER}-pxc-0}"
  printf '%s\n' "$password" | timeout --signal=TERM --kill-after=5s \
    "${MYSQL_EXEC_TIMEOUT}s" kubectl -n "$MYSQL_NAMESPACE" exec -i "$pod" -c pxc -- sh -eu -c '
    IFS= read -r MYSQL_PWD
    export MYSQL_PWD
    exec mysql --user="$1" --protocol=TCP -h "$2" \
      --ssl-mode=VERIFY_CA --ssl-ca=/etc/mysql/ssl-internal/ca.crt \
      --batch --skip-column-names -e "$3"
  ' sh "$user" "$host" "$sql"
}

echo "========== MYSQL-01 static and version preflight =========="
test "$(kubectl version -o json | jq -r '.serverVersion.gitVersion')" = v1.33.6 || fail "Kubernetes version mismatch"
test "$(sha256sum "$BASE/roles/cluster-addon/files/pxc-operator-1.20.0.tgz" | awk '{print $1}')" = \
  b8bff81d0f9691b1e495f958fba1bb268e48838cf548adf6ae6570ea0e0059cf || fail "Operator chart checksum mismatch"
tar -xOf "$BASE/roles/cluster-addon/files/pxc-operator-1.20.0.tgz" pxc-operator/Chart.yaml | grep -qx 'appVersion: 1.20.0' || fail "Operator chart appVersion mismatch"
pass "MYSQL-01 pinned GA versions and chart SHA256"

echo "========== MYSQL-02 clean topology and storage preflight =========="
for namespace in "$MYSQL_NAMESPACE" "$MYSQL_OPERATOR_NAMESPACE"; do
  ! kubectl get namespace "$namespace" >/dev/null 2>&1 || fail "namespace is not clean: ${namespace}"
done
test "$(kubectl get nodes --no-headers | awk '$2 == "Ready" {count++} END {print count+0}')" -ge 3 || fail "fewer than three Ready nodes"
kubectl get storageclass "$MYSQL_STORAGE_CLASS" >/dev/null || fail "StorageClass missing: ${MYSQL_STORAGE_CLASS}"
for ip in $(node_ips); do
  ssh -o BatchMode=yes -o StrictHostKeyChecking=no -o ConnectTimeout=10 "root@${ip}" true || fail "node SSH unavailable: ${ip}"
done
pass "MYSQL-02 clean namespace, six Ready nodes and StorageClass"

echo "========== MYSQL-03 official images to scoped local registry =========="
if ss -lnt | awk '{print $4}' | grep -Eq '(^|:)5000$'; then
  curl -fsS --max-time 3 http://127.0.0.1:5000/v2/ >/dev/null || fail "occupied TCP port 5000 is not a Registry v2 endpoint"
  echo "REGISTRY_REUSED endpoint=127.0.0.1:5000"
else
  install -d -m 0700 "$REGISTRY_DATA"
  docker pull docker.io/library/registry:2.8.3 >/dev/null
  docker run -d --restart=no --name "$REGISTRY_NAME" --label kubeauto.mysql-gate=true \
    -e REGISTRY_STORAGE_DELETE_ENABLED=true \
    -p 5000:5000 -v "$REGISTRY_DATA:/var/lib/registry" docker.io/library/registry:2.8.3 >/dev/null
fi
for attempt in $(seq 1 30); do
  curl -fsS --max-time 3 http://127.0.0.1:5000/v2/ >/dev/null && break
  sleep 2
done
curl -fsS --max-time 3 http://127.0.0.1:5000/v2/ >/dev/null || fail "scoped registry did not become ready"

for ip in $(node_ips); do
  ssh -o BatchMode=yes -o StrictHostKeyChecking=no "root@${ip}" bash -s -- "$REGISTRY_IP" "$REGISTRY_HOST" "$MARKER" <<'NODE_REGISTRY'
set -Eeuo pipefail
registry_ip="$1"
registry_host="$2"
marker="$3"
host_name="${registry_host%:*}"
existing_ip="$(awk -v host="$host_name" '
  $1 !~ /^#/ {
    for (field = 2; field <= NF; field++) {
      if ($field == host) print $1
    }
  }
' /etc/hosts | head -n 1)"
if [[ -n "$existing_ip" && "$existing_ip" != "$registry_ip" ]]; then
  echo "Conflicting existing registry mapping for ${host_name}: ${existing_ip}" >&2
  exit 1
fi
if [[ -z "$existing_ip" ]]; then
  sed -i "/[[:space:]]# ${marker}$/d" /etc/hosts
  printf '%s %s # %s\n' "$registry_ip" "$host_name" "$marker" >> /etc/hosts
fi
config_dir="/etc/containerd/certs.d/${registry_host}"
if [[ -e "$config_dir" && ! -f "$config_dir/.kubeauto-mysql-gate" ]]; then
  if [[ ! -f "$config_dir/hosts.toml" ]] || ! grep -Fq "http://${registry_host}" "$config_dir/hosts.toml"; then
    echo "Refusing to replace incompatible existing ${config_dir}" >&2
    exit 1
  fi
  exit 0
fi
install -d -m 0755 "$config_dir"
touch "$config_dir/.kubeauto-mysql-gate"
cat >"$config_dir/hosts.toml" <<EOF
server = "http://${registry_host}"
[host."http://${registry_host}"]
  capabilities = ["pull", "resolve"]
EOF
NODE_REGISTRY
done

images=(
  'percona/percona-xtradb-cluster-operator:1.20.0|percona-xtradb-cluster-operator:1.20.0'
  'percona/percona-xtradb-cluster:8.4.8-8.1|percona-xtradb-cluster:8.4.8-8.1'
  'percona/percona-xtrabackup:8.4.0-5.1|percona-xtrabackup:8.4.0-5.1'
  'percona/haproxy:2.8.18-1|percona-haproxy:2.8.18-1'
  'percona/fluentbit:5.0.6-1|percona-fluentbit:5.0.6-1'
  "${MINIO_IMAGE}|mysql-gate-minio:${MINIO_IMAGE##*:}"
  "${MINIO_MC_IMAGE}|mysql-gate-mc:${MINIO_MC_IMAGE##*:}"
  "${SYSBENCH_IMAGE}|mysql-gate-sysbench:${SYSBENCH_IMAGE##*:}"
)
for mapping in "${images[@]}"; do
  target="${mapping##*|}"
  if curl -fsS --max-time 5 "http://127.0.0.1:5000/v2/brinnatt/${target%:*}/tags/list" 2>/dev/null | grep -Fq "\"${target##*:}\""; then
    fail "pre-existing PXC registry tag would be overwritten: ${target}"
  fi
done
touch /var/tmp/kubeauto-mysql-registry-tags-owned

for mapping in "${images[@]}"; do
  upstream="${mapping%%|*}"
  target="${mapping##*|}"
  upstream_repository="${upstream%:*}"
  upstream_tag="${upstream##*:}"
  source_repository_path="$(source_repository "$upstream_repository")"
  source_prefix="$SOURCE_PREFIX"
  source_ref="${source_prefix}/${source_repository_path}:${upstream_tag}"
  if [[ -n "$SOURCE_FALLBACK_PREFIX" ]]; then
    source_probe_digest="$(registry_manifest_digest \
      "$source_prefix" "$source_repository_path" "$upstream_tag" 2>/dev/null || true)"
    if [[ "$source_probe_digest" != sha256:* ]]; then
      source_prefix="$SOURCE_FALLBACK_PREFIX"
      source_repository_path="$upstream_repository"
      source_ref="${source_prefix}/${source_repository_path}:${upstream_tag}"
      echo "[WARN] private runtime image unavailable; using non-persisted fallback target=${target} source=${source_ref}"
    fi
  fi
  echo "[WAIT] materializing official PXC image target=${target}"
  if [[ "$source_prefix" == hub.talkedu.cn && "$source_repository_path" == kubeauto/* ]]; then
    source_manifest_digest="$(registry_manifest_digest "$source_prefix" "$source_repository_path" "$upstream_tag")" || fail "runtime source manifest unavailable: ${target}"
    [[ "$source_manifest_digest" == sha256:* ]] || fail "runtime source manifest digest unavailable: ${target}"
    verify_result="$(matching_verifier_digest "$source_manifest_digest" "brinnatt/${source_repository_path#kubeauto/}" "$upstream_tag")" \
      || fail "TalkEdu and Docker Hub dual-push manifest digests differ: ${target}"
    expected_digest="${verify_result%%|*}"
    verifier_prefix="${verify_result##*|}"
    digest_evidence="talkedu-dockerhub-dual-push:${verifier_prefix}"
  else
    official_digest="$(official_manifest_digest "$upstream_repository" "$upstream_tag" 2>/dev/null || true)"
    if [[ "$official_digest" == sha256:* ]]; then
      expected_digest="$official_digest"
      digest_evidence=official-docker-hub-api
    elif [[ -n "$VERIFY_PREFIXES" ]]; then
      source_manifest_digest="$(registry_manifest_digest "$source_prefix" "$source_repository_path" "$upstream_tag")" || fail "runtime source manifest unavailable: ${target}"
      [[ "$source_manifest_digest" == sha256:* ]] || fail "runtime source manifest digest unavailable: ${target}"
      verify_result="$(matching_verifier_digest "$source_manifest_digest" "$upstream_repository" "$upstream_tag")" \
        || fail "no independent runtime verifier matched source manifest digest: ${target}"
      expected_digest="${verify_result%%|*}"
      verifier_prefix="${verify_result##*|}"
      digest_evidence="two-independent-runtime-manifest-digests:${verifier_prefix}"
    else
      fail "official Docker Hub manifest unavailable and no independent runtime verifier was provided: ${target}"
    fi
  fi
  if [[ -n "$STAGE_NODE" ]]; then
    printf -v source_ref_quoted '%q' "$source_ref"
    printf -v target_ref_quoted '%q' "${REGISTRY_HOST}/brinnatt/${target}"
    printf -v expected_digest_quoted '%q' "$expected_digest"
    ssh -o BatchMode=yes -o StrictHostKeyChecking=no -o ConnectTimeout=10 "root@${STAGE_NODE}" \
      "env MYSQL_STAGE_SOURCE_REF=${source_ref_quoted} MYSQL_STAGE_TARGET_REF=${target_ref_quoted} MYSQL_STAGE_EXPECTED_DIGEST=${expected_digest_quoted} bash -s" \
      <"$BASE/tests/helpers/mysql-stage-node-image.sh" || fail "node image staging failed: ${target}"
  else
    timeout --signal=TERM --kill-after=15s 20m docker pull "$source_ref" || fail "runtime image pull failed or timed out: ${target}"
    pulled_digest="$(docker image inspect "$source_ref" --format '{{join .RepoDigests " "}}' | tr ' ' '\n' | sed -n 's/.*@//p' | head -n 1)"
    [[ "$pulled_digest" == sha256:* ]] || fail "pulled image repository digest missing: ${target}"
    [[ "$pulled_digest" == "$expected_digest" ]] || fail "pulled manifest digest does not match the verified digest: ${target}"
    docker tag "$source_ref" "127.0.0.1:5000/brinnatt/${target}"
    docker push "127.0.0.1:5000/brinnatt/${target}" >/dev/null
  fi
  curl -fsS --max-time 10 \
    -H 'Accept: application/vnd.oci.image.manifest.v1+json, application/vnd.docker.distribution.manifest.v2+json' \
    "http://127.0.0.1:5000/v2/brinnatt/${target%:*}/manifests/${target##*:}" >/dev/null || fail "registry manifest missing: ${target}"
  echo "IMAGE_MATERIALIZED target=${target} digest_present=true evidence=${digest_evidence} transport=${STAGE_NODE:+node-containerd}"
done
pass "MYSQL-03 five Percona and three test-support images verified and materialized"

echo "========== MYSQL-09/11 TLS S3 test dependency =========="
kubectl create namespace "$MYSQL_NAMESPACE" >/dev/null
MINIO_ROOT_USER="mysqlgate$(openssl rand -hex 6)"
MINIO_ROOT_PASSWORD="$(openssl rand -base64 36 | tr -d '\n')"
install -d -m 0700 /tmp/kubeauto-mysql-minio-tls
openssl req -x509 -newkey rsa:2048 -nodes -days 2 -sha256 \
  -subj '/CN=kubeauto-mysql-gate-ca' \
  -keyout /tmp/kubeauto-mysql-minio-tls/ca.key \
  -out /tmp/kubeauto-mysql-minio-tls/ca.crt >/dev/null 2>&1
openssl req -newkey rsa:2048 -nodes -sha256 \
  -subj '/CN=minio-service.mysql.svc' \
  -keyout /tmp/kubeauto-mysql-minio-tls/private.key \
  -out /tmp/kubeauto-mysql-minio-tls/server.csr >/dev/null 2>&1
cat >/tmp/kubeauto-mysql-minio-tls/server.ext <<'EOF'
subjectAltName=DNS:minio-service,DNS:minio-service.mysql,DNS:minio-service.mysql.svc,DNS:minio-service.mysql.svc.cluster.local
extendedKeyUsage=serverAuth
keyUsage=digitalSignature,keyEncipherment
EOF
openssl x509 -req -days 2 -sha256 \
  -in /tmp/kubeauto-mysql-minio-tls/server.csr \
  -CA /tmp/kubeauto-mysql-minio-tls/ca.crt \
  -CAkey /tmp/kubeauto-mysql-minio-tls/ca.key \
  -CAcreateserial \
  -extfile /tmp/kubeauto-mysql-minio-tls/server.ext \
  -out /tmp/kubeauto-mysql-minio-tls/public.crt >/dev/null 2>&1
kubectl -n "$MYSQL_NAMESPACE" create secret generic mysql-gate-minio-tls \
  --from-file=public.crt=/tmp/kubeauto-mysql-minio-tls/public.crt \
  --from-file=private.key=/tmp/kubeauto-mysql-minio-tls/private.key \
  --from-file=ca.crt=/tmp/kubeauto-mysql-minio-tls/ca.crt \
  --dry-run=client -o yaml | kubectl apply -f - >/dev/null
kubectl -n "$MYSQL_NAMESPACE" create secret generic mysql-gate-minio-root \
  --from-literal=MINIO_ROOT_USER="$MINIO_ROOT_USER" \
  --from-literal=MINIO_ROOT_PASSWORD="$MINIO_ROOT_PASSWORD" \
  --dry-run=client -o yaml | kubectl apply -f - >/dev/null
kubectl -n "$MYSQL_NAMESPACE" create secret generic cluster1-backup-s3 \
  --from-literal=AWS_ACCESS_KEY_ID="$MINIO_ROOT_USER" \
  --from-literal=AWS_SECRET_ACCESS_KEY="$MINIO_ROOT_PASSWORD" \
  --dry-run=client -o yaml | kubectl apply -f - >/dev/null
cat <<EOF | kubectl apply -f - >/dev/null
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: mysql-gate-minio-data
  namespace: ${MYSQL_NAMESPACE}
  labels:
    kubeauto.io/component: percona-pxc-test
spec:
  accessModes: [ReadWriteOnce]
  storageClassName: ${MYSQL_STORAGE_CLASS}
  resources:
    requests:
      storage: 20Gi
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: mysql-gate-minio
  namespace: ${MYSQL_NAMESPACE}
  labels:
    kubeauto.io/component: percona-pxc-test
spec:
  replicas: 1
  strategy:
    type: Recreate
  selector:
    matchLabels:
      app: mysql-gate-minio
  template:
    metadata:
      labels:
        app: mysql-gate-minio
        kubeauto.io/component: percona-pxc-test
    spec:
      containers:
        - name: minio
          image: ${REGISTRY_HOST}/brinnatt/mysql-gate-minio:${MINIO_IMAGE##*:}
          imagePullPolicy: IfNotPresent
          args: [server, /data, --certs-dir, /certs, --console-address, ":9001"]
          envFrom:
            - secretRef:
                name: mysql-gate-minio-root
          ports:
            - {name: s3, containerPort: 9000}
          readinessProbe:
            httpGet: {scheme: HTTPS, path: /minio/health/ready, port: s3}
            periodSeconds: 5
          livenessProbe:
            httpGet: {scheme: HTTPS, path: /minio/health/live, port: s3}
            periodSeconds: 10
          resources:
            requests: {cpu: 250m, memory: 512Mi}
            limits: {cpu: "2", memory: 2Gi}
          volumeMounts:
            - {name: data, mountPath: /data}
            - {name: tls, mountPath: /certs, readOnly: true}
      volumes:
        - name: data
          persistentVolumeClaim: {claimName: mysql-gate-minio-data}
        - name: tls
          secret:
            secretName: mysql-gate-minio-tls
---
apiVersion: v1
kind: Service
metadata:
  name: minio-service
  namespace: ${MYSQL_NAMESPACE}
  labels:
    kubeauto.io/component: percona-pxc-test
spec:
  selector: {app: mysql-gate-minio}
  ports:
    - {name: s3, port: 9000, targetPort: s3}
EOF
kubectl -n "$MYSQL_NAMESPACE" rollout status deployment/mysql-gate-minio --timeout=10m
cat <<EOF | kubectl apply -f - >/dev/null
apiVersion: v1
kind: Pod
metadata:
  name: mysql-gate-mc
  namespace: ${MYSQL_NAMESPACE}
  labels:
    kubeauto.io/component: percona-pxc-test
spec:
  restartPolicy: Never
  containers:
    - name: mc
      image: ${REGISTRY_HOST}/brinnatt/mysql-gate-mc:${MINIO_MC_IMAGE##*:}
      imagePullPolicy: IfNotPresent
      command: [/bin/sh, -c, 'exec sleep 86400']
      envFrom:
        - secretRef:
            name: mysql-gate-minio-root
      volumeMounts:
        - name: minio-ca
          mountPath: /root/.mc/certs/CAs/ca.crt
          subPath: ca.crt
          readOnly: true
  volumes:
    - name: minio-ca
      secret:
        secretName: mysql-gate-minio-tls
EOF
kubectl -n "$MYSQL_NAMESPACE" wait --for=condition=Ready pod/mysql-gate-mc --timeout=5m
kubectl -n "$MYSQL_NAMESPACE" exec mysql-gate-mc -- sh -eu -c '
  mc alias set gate https://minio-service.mysql.svc:9000 \
    "$MINIO_ROOT_USER" "$MINIO_ROOT_PASSWORD" --api S3v4 --path on >/dev/null
  mc mb --ignore-existing gate/operator-testing >/dev/null
'
unset MINIO_ROOT_USER MINIO_ROOT_PASSWORD
pass "MYSQL-09 TLS MinIO endpoint and bucket ready"

echo "========== MYSQL-04 exact Ansible role install =========="
install -d -m 0755 "$BASE/extra-bin"
if [[ ! -x "$BASE/extra-bin/helm" ]]; then
  install -m 0755 "$(command -v helm)" "$BASE/extra-bin/helm"
fi
install -d -m 0700 "$BASE/clusters/mysql-gate"
install -m 0600 "${KUBECONFIG:-/root/.kube/config}" "$BASE/clusters/mysql-gate/kubectl.kubeconfig"
cat >/tmp/kubeauto-mysql-playbook.yml <<EOF
- name: Percona PXC live delivery gate
  hosts: localhost
  connection: local
  gather_facts: false
  vars:
    base_dir: ${BASE}
    cluster_dir: ${BASE}/clusters/mysql-gate
    mysql_namespace: ${MYSQL_NAMESPACE}
    mysql_operator_namespace: ${MYSQL_OPERATOR_NAMESPACE}
    mysql_cluster_name: ${MYSQL_CLUSTER}
    mysql_storage_class: ${MYSQL_STORAGE_CLASS}
    mysql_pvc_size: 20Gi
    mysql_pxc_size: 3
    mysql_haproxy_size: 3
    mysql_pxc_cpu_request: "1"
    mysql_pxc_memory_request: 2Gi
    mysql_pxc_cpu_limit: "3"
    mysql_pxc_memory_limit: 6Gi
    mysql_haproxy_cpu_request: 250m
    mysql_haproxy_memory_request: 256Mi
    mysql_haproxy_cpu_limit: "1"
    mysql_haproxy_memory_limit: 1Gi
    mysql_gcache_size: 1G
    mysql_secrets_name: ""
    mysql_tls_enabled: true
    mysql_logcollector_enabled: false
    mysql_backup_enabled: true
    mysql_backup_storage_name: s3-full
    mysql_backup_credentials_secret: cluster1-backup-s3
    mysql_backup_bucket: operator-testing/full
    mysql_backup_region: us-east-1
    mysql_backup_endpoint: https://minio-service.${MYSQL_NAMESPACE}.svc:9000
    mysql_backup_verify_tls: true
    mysql_backup_ca_bundle_secret: mysql-gate-minio-tls
    mysql_backup_ca_bundle_key: ca.crt
    mysql_backup_force_path_style: true
    mysql_backup_schedule: "0 1 * * *"
    mysql_backup_retention_count: 7
    mysql_pitr_enabled: true
    mysql_pitr_storage_name: s3-binlogs
    mysql_pitr_bucket: operator-testing/binlogs
    mysql_pitr_upload_interval: 60
    pxc_operator_ver: 1.20.0
    pxc_ver: 8.4.8-8.1
    pxc_xtrabackup_ver: 8.4.0-5.1
    pxc_haproxy_ver: 2.8.18-1
    pxc_fluentbit_ver: 5.0.6-1
    mysql_ready_retries: 270
    mysql_ready_delay: 10
  tasks:
    - name: Execute the production Percona PXC task file
      ansible.builtin.include_role:
        name: cluster-addon
        tasks_from: percona-pxc
EOF
ANSIBLE_PLAYBOOK="$(command -v ansible-playbook || true)"
if [[ -z "$ANSIBLE_PLAYBOOK" && -x "$BASE/.venv/bin/ansible-playbook" ]]; then
  ANSIBLE_PLAYBOOK="$BASE/.venv/bin/ansible-playbook"
fi
[[ -n "$ANSIBLE_PLAYBOOK" ]] || fail "ansible-playbook is unavailable"
ANSIBLE_ROLES_PATH="$BASE/roles" "$ANSIBLE_PLAYBOOK" -i localhost, /tmp/kubeauto-mysql-playbook.yml
wait_pxc_ready || fail "PXC did not reach ready state"

pxc_nodes="$(kubectl -n "$MYSQL_NAMESPACE" get pod -l app.kubernetes.io/component=pxc -o jsonpath='{range .items[*]}{.spec.nodeName}{"\n"}{end}' | sort -u | wc -l)"
[[ "$pxc_nodes" -eq 3 ]] || fail "PXC pods are not spread across three nodes"
kubectl -n "$MYSQL_NAMESPACE" get pxc,pod,pvc,svc -o wide
pass "MYSQL-04 Operator, CRD, PXC 3/3, HAProxy 3/3 and PVC readiness"

echo "========== MYSQL-02 capacity, failure domains and storage R/W =========="
for pod in ${MYSQL_CLUSTER}-pxc-0 ${MYSQL_CLUSTER}-pxc-1 ${MYSQL_CLUSTER}-pxc-2; do
  node="$(kubectl -n "$MYSQL_NAMESPACE" get pod "$pod" -o jsonpath='{.spec.nodeName}')"
  [[ -n "$node" ]] || fail "PXC pod has no assigned failure domain: $pod"
  [[ "$(kubectl get node "$node" -o jsonpath='{.metadata.labels.kubernetes\.io/hostname}')" == "$node" ]] \
    || fail "node hostname failure-domain label mismatch: $node"
  resources="$(kubectl -n "$MYSQL_NAMESPACE" get pod "$pod" -o json | jq -r '
    .spec.containers[] | select(.name == "pxc")
    | [.resources.requests.cpu, .resources.requests.memory, .resources.limits.cpu, .resources.limits.memory]
    | @tsv
  ')"
  [[ "$resources" == $'1\t2Gi\t3\t6Gi' ]] || fail "PXC resource contract mismatch: $pod"
  kubectl -n "$MYSQL_NAMESPACE" exec "$pod" -c pxc -- sh -eu -c '
    probe=/var/lib/mysql/.kubeauto-pvc-rw
    printf "%s\n" "$1" >"$probe"
    test "$(cat "$probe")" = "$1"
    rm -f "$probe"
  ' sh "$pod-rw" || fail "PVC read/write probe failed: $pod"
done
[[ "$(kubectl -n "$MYSQL_NAMESPACE" get pvc -l app.kubernetes.io/instance="$MYSQL_CLUSTER" -o json \
  | jq '[.items[] | select(.status.phase == "Bound" and .spec.resources.requests.storage == "20Gi")] | length')" -eq 3 ]] \
  || fail "three 20Gi Bound PXC PVCs were not found"
pass "MYSQL-02 capacity, three failure domains and PVC read/write"

echo "========== MYSQL-05 TLS, SQL and least privilege =========="
ROOT_PASSWORD="$(kubectl -n "$MYSQL_NAMESPACE" get secret "${MYSQL_CLUSTER}-secrets" -o jsonpath='{.data.root}' | base64 -d)"
[[ -n "$ROOT_PASSWORD" ]] || fail "root password is empty"
for pod in ${MYSQL_CLUSTER}-pxc-0 ${MYSQL_CLUSTER}-pxc-1 ${MYSQL_CLUSTER}-pxc-2; do
  wsrep="$(mysql_exec 127.0.0.1 "SHOW GLOBAL STATUS WHERE Variable_name IN ('wsrep_cluster_size','wsrep_cluster_status','wsrep_local_state_comment','wsrep_ready','wsrep_connected');" "$pod")"
  echo "WSREP_STATUS pod=${pod} $(tr '\n' ' ' <<<"$wsrep")"
  grep -q $'wsrep_cluster_size\t3' <<<"$wsrep" || fail "wsrep cluster size mismatch on ${pod}"
  grep -q $'wsrep_cluster_status\tPrimary' <<<"$wsrep" || fail "wsrep non-Primary on ${pod}"
  grep -q $'wsrep_local_state_comment\tSynced' <<<"$wsrep" || fail "wsrep non-Synced on ${pod}"
  grep -q $'wsrep_ready\tON' <<<"$wsrep" || fail "wsrep not ready on ${pod}"
done
mysql_exec "${MYSQL_CLUSTER}-haproxy.${MYSQL_NAMESPACE}.svc" \
  "CREATE DATABASE IF NOT EXISTS kubeauto_gate; CREATE DATABASE IF NOT EXISTS kubeauto_app; CREATE TABLE IF NOT EXISTS kubeauto_gate.marker (id BIGINT PRIMARY KEY, value VARCHAR(64) NOT NULL); CREATE TABLE IF NOT EXISTS kubeauto_app.marker (id BIGINT PRIMARY KEY, value VARCHAR(64)); REPLACE INTO kubeauto_gate.marker VALUES (1, 'pxc-primary-write');"
replica_value="$(mysql_exec "${MYSQL_CLUSTER}-haproxy-replicas.${MYSQL_NAMESPACE}.svc" "SELECT value FROM kubeauto_gate.marker WHERE id=1;")"
[[ "$replica_value" == pxc-primary-write ]] || fail "replicas Service did not return committed marker"
tls_cipher="$(mysql_exec_tls_as root "$ROOT_PASSWORD" "${MYSQL_CLUSTER}-haproxy.${MYSQL_NAMESPACE}.svc" \
  "SHOW SESSION STATUS LIKE 'Ssl_cipher';")"
[[ -n "$(awk -F $'\t' '$1 == "Ssl_cipher" {print $2}' <<<"$tls_cipher")" ]] || fail "VERIFY_CA session did not negotiate TLS"
if printf '%s\n' "$ROOT_PASSWORD" | kubectl -n "$MYSQL_NAMESPACE" exec -i "${MYSQL_CLUSTER}-pxc-0" -c pxc -- sh -eu -c '
  IFS= read -r MYSQL_PWD
  export MYSQL_PWD
  mysql -uroot --protocol=TCP -h "$1" --ssl-mode=VERIFY_CA \
    --ssl-ca=/etc/mysql/ssl-internal/tls.crt -e "SELECT 1"
' sh "${MYSQL_CLUSTER}-haproxy.${MYSQL_NAMESPACE}.svc" >/dev/null 2>&1; then
  fail "TLS connection unexpectedly trusted the wrong CA file"
fi

APP_PASSWORD="$(openssl rand -base64 32 | tr -d '\n')"
APP_PASSWORD_B64="$(printf '%s' "$APP_PASSWORD" | base64 --wrap=0)"
jq -n --arg namespace "$MYSQL_NAMESPACE" --arg password "$APP_PASSWORD_B64" '{
  apiVersion: "v1",
  kind: "Secret",
  metadata: {
    name: "cluster1-app-user",
    namespace: $namespace,
    labels: {"kubeauto.io/component": "percona-pxc-test"}
  },
  type: "Opaque",
  data: {password: $password}
}' | kubectl apply -f - >/dev/null
unset APP_PASSWORD_B64
kubectl -n "$MYSQL_NAMESPACE" patch pxc "$MYSQL_CLUSTER" --type=merge -p '{
  "spec": {
    "users": [{
      "name": "pxc_app",
      "dbs": ["kubeauto_app"],
      "hosts": ["%"],
      "grants": ["SELECT", "INSERT", "UPDATE", "DELETE"],
      "withGrantOption": false,
      "passwordSecretRef": {"name": "cluster1-app-user", "key": "password"}
    }]
  }
}' >/dev/null
for attempt in $(seq 1 60); do
  mysql_exec_tls_as pxc_app "$APP_PASSWORD" "${MYSQL_CLUSTER}-haproxy.${MYSQL_NAMESPACE}.svc" \
    "SELECT 1;" >/dev/null 2>&1 && break
  (( attempt < 60 )) && sleep 5
done
mysql_exec_tls_as pxc_app "$APP_PASSWORD" "${MYSQL_CLUSTER}-haproxy.${MYSQL_NAMESPACE}.svc" \
  "REPLACE INTO kubeauto_app.marker VALUES (1, 'least-privilege'); SELECT value FROM kubeauto_app.marker WHERE id=1;" \
  | grep -qx least-privilege || fail "least-privilege application CRUD failed"
if mysql_exec_as pxc_app "$APP_PASSWORD" "${MYSQL_CLUSTER}-haproxy.${MYSQL_NAMESPACE}.svc" \
  "CREATE DATABASE kubeauto_forbidden;" >/dev/null 2>&1; then
  fail "least-privilege user unexpectedly created a database"
fi
if mysql_exec_as pxc_app "$APP_PASSWORD" "${MYSQL_CLUSTER}-haproxy.${MYSQL_NAMESPACE}.svc" \
  "GRANT SELECT ON *.* TO 'pxc_app'@'%';" >/dev/null 2>&1; then
  fail "least-privilege user unexpectedly granted global privileges"
fi
unset APP_PASSWORD
pass "MYSQL-05 VERIFY_CA TLS, Primary write, replicas read and least-privilege negatives"

echo "========== MYSQL-06/07 single-node quorum and transfer =========="
old_uid="$(kubectl -n "$MYSQL_NAMESPACE" get pod "${MYSQL_CLUSTER}-pxc-1" -o jsonpath='{.metadata.uid}')"
kubectl -n "$MYSQL_NAMESPACE" delete pod "${MYSQL_CLUSTER}-pxc-1" --wait=false
for attempt in $(seq 1 30); do
  mysql_exec "${MYSQL_CLUSTER}-haproxy.${MYSQL_NAMESPACE}.svc" "SELECT value FROM kubeauto_gate.marker WHERE id=1;" >/dev/null 2>&1 && break
  sleep 2
done
mysql_exec "${MYSQL_CLUSTER}-haproxy.${MYSQL_NAMESPACE}.svc" \
  "INSERT INTO kubeauto_gate.marker VALUES (2, 'single-node-write') ON DUPLICATE KEY UPDATE value=VALUES(value);" || fail "single-node loss interrupted writes"
wait_recreated_pod_ready "${MYSQL_CLUSTER}-pxc-1" "$old_uid" 1800 || fail "PXC pod was not reconstructed and Ready before timeout"
new_uid="$RECREATED_POD_UID"
wait_pxc_ready || fail "PXC did not recover after single-node loss"
transfer_log="$(kubectl -n "$MYSQL_NAMESPACE" logs "${MYSQL_CLUSTER}-pxc-1" -c pxc --since=35m 2>&1 || true)"
grep -Eiq 'IST|SST|state transfer|incremental state transfer' <<<"$transfer_log" || fail "no IST/SST evidence in reconstructed node log"
pass "MYSQL-07 reconstructed node completed state transfer"

echo "========== MYSQL-06 two-node peer loss refuses unsafe writes =========="
# Suspend Operator reconciliation and set the surviving test member's
# Galera quorum weight to zero.  This is a runtime-only fault injection:
# after the two peers are terminated, the one-member component must be
# Non-Primary and reject writes.  The production CR and templates are not
# modified.
kubectl -n "$MYSQL_OPERATOR_NAMESPACE" scale deployment pxc-operator --replicas=0 >/dev/null
kubectl -n "$MYSQL_OPERATOR_NAMESPACE" rollout status deployment/pxc-operator --timeout=5m >/dev/null
mysql_exec 127.0.0.1 "SET GLOBAL wsrep_provider_options='pc.weight=0';" \
  "${MYSQL_CLUSTER}-pxc-0" || fail "could not set runtime Galera quorum weight"
set +e
kubectl -n "$MYSQL_NAMESPACE" exec "${MYSQL_CLUSTER}-pxc-1" -c pxc -- sh -c 'kill -KILL 1' >/dev/null 2>&1 & kill_pid_1=$!
kubectl -n "$MYSQL_NAMESPACE" exec "${MYSQL_CLUSTER}-pxc-2" -c pxc -- sh -c 'kill -KILL 1' >/dev/null 2>&1 & kill_pid_2=$!
wait "$kill_pid_1"; kill_rc_1=$?
wait "$kill_pid_2"; kill_rc_2=$?
set -e
kubectl -n "$MYSQL_NAMESPACE" patch statefulset "${MYSQL_CLUSTER}-pxc" \
  --type=merge -p '{"spec":{"replicas":1}}' >/dev/null
kubectl -n "$MYSQL_NAMESPACE" delete pod "${MYSQL_CLUSTER}-pxc-1" "${MYSQL_CLUSTER}-pxc-2" \
  --grace-period=30 --wait=true >/dev/null 2>&1 || true
echo "PXC_TWO_MEMBER_CRASH injected=true quorum_weight=0 exec_rc=${kill_rc_1},${kill_rc_2}"
minority_state_ready=false
minority_wsrep=""
minority_log=""
for attempt in $(seq 1 90); do
  minority_wsrep="$(MYSQL_EXEC_TIMEOUT=5 mysql_exec 127.0.0.1 \
    "SHOW GLOBAL STATUS WHERE Variable_name IN ('wsrep_cluster_size','wsrep_cluster_status','wsrep_local_state_comment','wsrep_ready');" \
    "${MYSQL_CLUSTER}-pxc-0" 2>/dev/null || true)"
  minority_log="$(kubectl -n "$MYSQL_NAMESPACE" logs "${MYSQL_CLUSTER}-pxc-0" -c pxc --since=5m 2>/dev/null || true)"
  if grep -Eiq 'Received NON-PRIMARY|status: non-primary' <<<"$minority_log" \
    && grep -Eiq 'members\(1\):' <<<"$minority_log"; then
    minority_state_ready=true
    break
  fi
  (( attempt < 90 )) && sleep 2
done
[[ "$minority_state_ready" == true ]] || fail "remaining PXC member did not log Non-Primary size=1"
echo "PXC_TWO_MEMBER_LOSS isolated_peer=${MYSQL_CLUSTER}-pxc-0 evidence=non-primary-members-1"
if MYSQL_EXEC_TIMEOUT=10 mysql_exec 127.0.0.1 \
  "INSERT INTO kubeauto_gate.marker VALUES (3, 'unsafe-minority-write');" \
  "${MYSQL_CLUSTER}-pxc-0" >/dev/null 2>&1; then
  fail "minority component unexpectedly accepted a write"
fi
# Resume reconciliation.  The unchanged production CR restores the
# original three-member topology and discards the runtime-only weight.
kubectl -n "$MYSQL_OPERATOR_NAMESPACE" scale deployment pxc-operator --replicas=1 >/dev/null
kubectl -n "$MYSQL_OPERATOR_NAMESPACE" rollout status deployment/pxc-operator --timeout=5m >/dev/null
kubectl -n "$MYSQL_NAMESPACE" patch statefulset "${MYSQL_CLUSTER}-pxc" \
  --type=merge -p '{"spec":{"replicas":3}}' >/dev/null
kubectl -n "$MYSQL_NAMESPACE" annotate pxc "$MYSQL_CLUSTER" \
  "kubeauto.io/mysql-gate-reconcile=$(date +%s)" --overwrite >/dev/null
wait_pxc_ready || fail "PXC did not recover after two-node peer loss"
mysql_exec "${MYSQL_CLUSTER}-haproxy.${MYSQL_NAMESPACE}.svc" \
  "INSERT INTO kubeauto_gate.marker VALUES (3, 'post-quorum-recovery') ON DUPLICATE KEY UPDATE value=VALUES(value);"
pass "MYSQL-06 one-node loss retained quorum and two-node peer loss refused writes"

echo "========== MYSQL-08 system password rotation =========="
OLD_ROOT_PASSWORD="$ROOT_PASSWORD"
NEW_ROOT_PASSWORD="$(openssl rand -base64 36 | tr -d '\n')"
NEW_ROOT_PASSWORD_B64="$(printf '%s' "$NEW_ROOT_PASSWORD" | base64 --wrap=0)"
jq -n --arg password "$NEW_ROOT_PASSWORD_B64" '{data: {root: $password}}' \
  | kubectl -n "$MYSQL_NAMESPACE" patch secret "${MYSQL_CLUSTER}-secrets" \
      --type=merge --patch-file=/dev/stdin >/dev/null
unset NEW_ROOT_PASSWORD_B64
new_password_ready=false
for attempt in $(seq 1 60); do
  if mysql_exec_as root "$NEW_ROOT_PASSWORD" "${MYSQL_CLUSTER}-haproxy.${MYSQL_NAMESPACE}.svc" \
    "SELECT 1;" >/dev/null 2>&1; then
    new_password_ready=true
    break
  fi
  (( attempt < 60 )) && sleep 5
done
[[ "$new_password_ready" == true ]] || fail "new root credential did not converge"
if mysql_exec_as root "$OLD_ROOT_PASSWORD" "${MYSQL_CLUSTER}-haproxy.${MYSQL_NAMESPACE}.svc" \
  "SELECT 1;" >/dev/null 2>&1; then
  fail "old root credential remained valid after rotation"
fi
ROOT_PASSWORD="$NEW_ROOT_PASSWORD"
unset OLD_ROOT_PASSWORD NEW_ROOT_PASSWORD
pass "MYSQL-08 new system credential succeeded and old credential failed"

echo "========== MYSQL-13 idempotence =========="
before_pods="$(kubectl -n "$MYSQL_NAMESPACE" get pod -l app.kubernetes.io/component=pxc -o jsonpath='{range .items[*]}{.metadata.name}={.metadata.uid}{"\n"}{end}' | sort)"
before_pvcs="$(kubectl -n "$MYSQL_NAMESPACE" get pvc -o jsonpath='{range .items[*]}{.metadata.name}={.metadata.uid}{"\n"}{end}' | sort)"
before_secret="$(kubectl -n "$MYSQL_NAMESPACE" get secret "${MYSQL_CLUSTER}-secrets" -o jsonpath='{.metadata.uid}')"
ANSIBLE_ROLES_PATH="$BASE/roles" "$ANSIBLE_PLAYBOOK" -i localhost, /tmp/kubeauto-mysql-playbook.yml
after_pods="$(kubectl -n "$MYSQL_NAMESPACE" get pod -l app.kubernetes.io/component=pxc -o jsonpath='{range .items[*]}{.metadata.name}={.metadata.uid}{"\n"}{end}' | sort)"
after_pvcs="$(kubectl -n "$MYSQL_NAMESPACE" get pvc -o jsonpath='{range .items[*]}{.metadata.name}={.metadata.uid}{"\n"}{end}' | sort)"
after_secret="$(kubectl -n "$MYSQL_NAMESPACE" get secret "${MYSQL_CLUSTER}-secrets" -o jsonpath='{.metadata.uid}')"
[[ "$before_pods" == "$after_pods" ]] || fail "idempotent apply rolled PXC pods"
[[ "$before_pvcs" == "$after_pvcs" ]] || fail "idempotent apply replaced PVCs"
[[ "$before_secret" == "$after_secret" ]] || fail "idempotent apply replaced system Secret"
pass "MYSQL-13 second production-role run preserved Pod, PVC and Secret identities"

echo MYSQL_PXC_CORE_GATE_PASS

echo "========== MYSQL-12 fixed sysbench baseline and node-loss load =========="
mysql_exec "${MYSQL_CLUSTER}-haproxy.${MYSQL_NAMESPACE}.svc" \
  "DROP DATABASE IF EXISTS pxc_benchmark; CREATE DATABASE pxc_benchmark;"
cat <<EOF | kubectl apply -f - >/dev/null
apiVersion: v1
kind: Pod
metadata:
  name: mysql-gate-sysbench
  namespace: ${MYSQL_NAMESPACE}
  labels:
    kubeauto.io/component: percona-pxc-test
spec:
  restartPolicy: Never
  containers:
    - name: sysbench
      image: ${REGISTRY_HOST}/brinnatt/mysql-gate-sysbench:${SYSBENCH_IMAGE##*:}
      imagePullPolicy: IfNotPresent
      command: [/bin/sh, -c, 'exec sleep 86400']
      env:
        - name: MYSQL_PASSWORD
          valueFrom:
            secretKeyRef:
              name: ${MYSQL_CLUSTER}-secrets
              key: root
      volumeMounts:
        - name: pxc-internal-ca
          mountPath: /etc/mysql/ssl-internal
          readOnly: true
  volumes:
    - name: pxc-internal-ca
      secret:
        secretName: ${MYSQL_CLUSTER}-ssl-internal
EOF
kubectl -n "$MYSQL_NAMESPACE" wait --for=condition=Ready pod/mysql-gate-sysbench --timeout=5m
sysbench_version="$(kubectl -n "$MYSQL_NAMESPACE" exec mysql-gate-sysbench -- sysbench --version)"
[[ "$sysbench_version" =~ ^sysbench[[:space:]]+1\. ]] || fail "unexpected sysbench version: ${sysbench_version}"
sysbench_command prepare 4 0 >/dev/null || fail "sysbench dataset preparation failed"
run_sysbench_measurement concurrency-1 1 30 || fail "sysbench single-thread baseline failed"
run_sysbench_measurement concurrency-4 4 30 || fail "sysbench four-thread baseline failed"
run_sysbench_measurement concurrency-16 16 30 || fail "sysbench sixteen-thread baseline failed"
failure_old_uid="$(kubectl -n "$MYSQL_NAMESPACE" get pod "${MYSQL_CLUSTER}-pxc-2" -o jsonpath='{.metadata.uid}')"
sysbench_command run 8 60 >/tmp/kubeauto-sysbench-node-loss.log 2>&1 &
sysbench_pid=$!
sleep 15
kubectl -n "$MYSQL_NAMESPACE" delete pod "${MYSQL_CLUSTER}-pxc-2" --wait=false >/dev/null
sysbench_rc=0
wait "$sysbench_pid" || sysbench_rc=$?
cat /tmp/kubeauto-sysbench-node-loss.log
[[ "$sysbench_rc" -eq 0 ]] || fail "sysbench node-loss workload failed rc=${sysbench_rc}"
failure_tps="$(awk '/transactions:/ {gsub(/[()]/, ""); print $(NF-2)}' /tmp/kubeauto-sysbench-node-loss.log | tail -n 1)"
failure_p95="$(awk '/95th percentile:/ {print $NF}' /tmp/kubeauto-sysbench-node-loss.log | tail -n 1)"
[[ "$failure_tps" =~ ^[0-9]+([.][0-9]+)?$ && "$failure_p95" =~ ^[0-9]+([.][0-9]+)?$ ]] \
  || fail "sysbench node-loss metrics missing"
echo "SYSBENCH_RESULT label=single-node-loss threads=8 seconds=60 tps=${failure_tps} p95_ms=${failure_p95}"
wait_recreated_pod_ready "${MYSQL_CLUSTER}-pxc-2" "$failure_old_uid" 1800 \
  || fail "PXC pod did not recover after sysbench node-loss run"
wait_pxc_ready || fail "PXC did not recover after sysbench node-loss run"
sysbench_command cleanup 4 0 >/dev/null || fail "sysbench cleanup failed"
pass "MYSQL-12 sysbench concurrency ladder and single-node-loss measurements"

echo "========== MYSQL-09 full S3 backup and object readability =========="
wait_pitr_deployment_ready || fail "PITR collector did not become ready"
mysql_exec "${MYSQL_CLUSTER}-haproxy.${MYSQL_NAMESPACE}.svc" \
  "CREATE DATABASE IF NOT EXISTS kubeauto_restore; CREATE TABLE IF NOT EXISTS kubeauto_restore.marker (id BIGINT PRIMARY KEY, value VARCHAR(64)); REPLACE INTO kubeauto_restore.marker VALUES (90, 'before-full-backup');"
create_backup full-backup || fail "full S3 backup failed"
full_destination="$(kubectl -n "$MYSQL_NAMESPACE" get pxc-backup full-backup -o jsonpath='{.status.destination}')"
[[ "$full_destination" == s3://operator-testing/full/* ]] || fail "unexpected full-backup destination: ${full_destination}"
full_object_path="${full_destination#s3://}"
[[ "$full_object_path" =~ ^[A-Za-z0-9._:/+-]+$ ]] || fail "invalid full-backup object path"
full_object_evidence="$(minio_object_evidence "$full_object_path")" \
  || fail "full-backup object is unreadable"
IFS='|' read -r full_object_count full_object_bytes full_sample_sha256 <<<"$full_object_evidence"
[[ "$full_object_count" =~ ^[0-9]+$ && "$full_object_count" -gt 0 \
  && "$full_object_bytes" =~ ^[0-9]+$ && "$full_object_bytes" -gt 0 \
  && "$full_sample_sha256" =~ ^[0-9a-f]{64}$ ]] || fail "full-backup object evidence is invalid"
echo "PXC_BACKUP_OBJECTS name=full-backup destination=${full_destination} files=${full_object_count} bytes=${full_object_bytes} sample_sha256=${full_sample_sha256}"
pass "MYSQL-09 full backup Succeeded and S3 objects are readable over verified TLS"

echo "========== MYSQL-10 full restore and new backup baseline =========="
mysql_exec "${MYSQL_CLUSTER}-haproxy.${MYSQL_NAMESPACE}.svc" \
  "REPLACE INTO kubeauto_restore.marker VALUES (91, 'after-full-backup');"
cat <<EOF | kubectl apply -f - >/dev/null
apiVersion: pxc.percona.com/v1
kind: PerconaXtraDBClusterRestore
metadata:
  name: full-restore
  namespace: ${MYSQL_NAMESPACE}
  labels:
    kubeauto.io/component: percona-pxc-test
spec:
  pxcCluster: ${MYSQL_CLUSTER}
  backupName: full-backup
EOF
wait_restore_state full-restore Succeeded 3600 || fail "full restore failed"
wait_pxc_ready || fail "PXC did not return after full restore"
restored_value="$(mysql_exec "${MYSQL_CLUSTER}-haproxy.${MYSQL_NAMESPACE}.svc" \
  "SELECT value FROM kubeauto_restore.marker WHERE id=90;")"
[[ "$restored_value" == before-full-backup ]] || fail "pre-backup marker missing after restore"
post_backup_count="$(mysql_exec "${MYSQL_CLUSTER}-haproxy.${MYSQL_NAMESPACE}.svc" \
  "SELECT COUNT(*) FROM kubeauto_restore.marker WHERE id=91;")"
[[ "$post_backup_count" == 0 ]] || fail "post-backup marker survived full restore"
create_backup post-restore-baseline || fail "post-restore backup baseline failed"
baseline_destination="$(kubectl -n "$MYSQL_NAMESPACE" get pxc-backup post-restore-baseline -o jsonpath='{.status.destination}')"
[[ "$baseline_destination" == s3://operator-testing/full/* ]] \
  || fail "unexpected post-restore baseline destination: ${baseline_destination}"
baseline_object_path="${baseline_destination#s3://}"
[[ "$baseline_object_path" =~ ^[A-Za-z0-9._:/+-]+$ ]] || fail "invalid post-restore baseline object path"
baseline_evidence="$(minio_object_evidence "$baseline_object_path")" \
  || fail "post-restore baseline object is unreadable"
IFS='|' read -r baseline_objects baseline_bytes baseline_sample_sha256 <<<"$baseline_evidence"
[[ "$baseline_objects" =~ ^[0-9]+$ && "$baseline_objects" -gt 0 \
  && "$baseline_bytes" =~ ^[0-9]+$ && "$baseline_bytes" -gt 0 \
  && "$baseline_sample_sha256" =~ ^[0-9a-f]{64}$ ]] || fail "post-restore baseline object evidence is invalid"
echo "PXC_BACKUP_OBJECTS name=post-restore-baseline files=${baseline_objects} bytes=${baseline_bytes} sample_sha256=${baseline_sample_sha256}"
pass "MYSQL-10 full restore preserved the expected point and created a new readable baseline"

echo "========== MYSQL-11 PITR transaction target =========="
wait_pitr_deployment_ready || fail "PITR collector was not ready after full restore"
mysql_exec "${MYSQL_CLUSTER}-haproxy.${MYSQL_NAMESPACE}.svc" \
  "CREATE TABLE IF NOT EXISTS kubeauto_restore.pitr_marker (id BIGINT PRIMARY KEY, value VARCHAR(64));"
create_backup pitr-base || fail "PITR base backup failed"
mysql_exec "${MYSQL_CLUSTER}-haproxy.${MYSQL_NAMESPACE}.svc" \
  "REPLACE INTO kubeauto_restore.pitr_marker VALUES (100, 'pitr-target');"
for pod in ${MYSQL_CLUSTER}-pxc-0 ${MYSQL_CLUSTER}-pxc-1 ${MYSQL_CLUSTER}-pxc-2; do
  mysql_exec 127.0.0.1 "FLUSH BINARY LOGS;" "$pod"
done
sleep 75
# Percona transaction recovery excludes the target GTID and everything after
# it. Capture the first transaction after the desired recovery boundary in the
# same MySQL session so the test does not guess from a multi-UUID GTID set.
pitr_gtid="$(mysql_exec "${MYSQL_CLUSTER}-haproxy.${MYSQL_NAMESPACE}.svc" \
  "SET @kubeauto_gtid_before=@@GLOBAL.gtid_executed; REPLACE INTO kubeauto_restore.pitr_marker VALUES (101, 'after-pitr-target'); SELECT GTID_SUBTRACT(@@GLOBAL.gtid_executed, @kubeauto_gtid_before);" \
  | tr -d '\r\n[:space:]' | sed 's/\\n//g')"
[[ "$pitr_gtid" =~ ^[0-9A-Fa-f-]{36}:[1-9][0-9]*$ ]] || fail "invalid or ambiguous PITR boundary GTID: ${pitr_gtid}"
echo "PXC_PITR_BOUNDARY next_transaction_gtid=${pitr_gtid}"
for pod in ${MYSQL_CLUSTER}-pxc-0 ${MYSQL_CLUSTER}-pxc-1 ${MYSQL_CLUSTER}-pxc-2; do
  mysql_exec 127.0.0.1 "FLUSH BINARY LOGS;" "$pod"
done
sleep 75
pitr_ready=false
for attempt in $(seq 1 30); do
  latest_restorable_time="$(kubectl -n "$MYSQL_NAMESPACE" get pxc-backup pitr-base \
    -o jsonpath='{.status.latestRestorableTime}' 2>/dev/null || true)"
  if [[ "$latest_restorable_time" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}T ]]; then
    pitr_ready=true
    break
  fi
  sleep 10
done
[[ "$pitr_ready" == true ]] || fail "PITR latestRestorableTime was not published"
cat <<EOF | kubectl apply -f - >/dev/null
apiVersion: pxc.percona.com/v1
kind: PerconaXtraDBClusterRestore
metadata:
  name: pitr-restore
  namespace: ${MYSQL_NAMESPACE}
  labels:
    kubeauto.io/component: percona-pxc-test
spec:
  pxcCluster: ${MYSQL_CLUSTER}
  backupName: pitr-base
  pitr:
    type: transaction
    gtid: ${pitr_gtid}
    backupSource:
      storageName: s3-binlogs
EOF
wait_restore_state pitr-restore Succeeded 3600 || fail "PITR transaction restore failed"
wait_pxc_ready || fail "PXC did not return after PITR"
target_count="$(mysql_exec "${MYSQL_CLUSTER}-haproxy.${MYSQL_NAMESPACE}.svc" \
  "SELECT COUNT(*) FROM kubeauto_restore.pitr_marker WHERE id=100;")"
after_target_count="$(mysql_exec "${MYSQL_CLUSTER}-haproxy.${MYSQL_NAMESPACE}.svc" \
  "SELECT COUNT(*) FROM kubeauto_restore.pitr_marker WHERE id=101;")"
[[ "$target_count" == 1 && "$after_target_count" == 0 ]] || fail "PITR transaction boundary is incorrect"
echo "PXC_PITR_RESTORED gtid=${pitr_gtid} latest_restorable_time=${latest_restorable_time}"

echo "========== MYSQL-11 binlog-gap refusal =========="
wait_pitr_deployment_ready || fail "PITR collector was not ready before gap baseline"
# PITR restore starts a new Galera timeline. Give the collector one complete
# upload cycle on that timeline before stopping it; otherwise the old timeline
# cache is intentionally treated as a new cluster rather than a binlog gap.
mysql_exec "${MYSQL_CLUSTER}-haproxy.${MYSQL_NAMESPACE}.svc" \
  "REPLACE INTO kubeauto_restore.pitr_marker VALUES (102, 'gap-baseline');"
for pod in ${MYSQL_CLUSTER}-pxc-0 ${MYSQL_CLUSTER}-pxc-1 ${MYSQL_CLUSTER}-pxc-2; do
  mysql_exec 127.0.0.1 "FLUSH BINARY LOGS;" "$pod"
done
sleep 75
create_backup pitr-gap-base || fail "PITR gap base backup failed"
kubectl -n "$MYSQL_NAMESPACE" patch pxc "$MYSQL_CLUSTER" --type=merge \
  -p '{"spec":{"backup":{"pitr":{"enabled":false}}}}' >/dev/null
for attempt in $(seq 1 60); do
  ! kubectl -n "$MYSQL_NAMESPACE" get deployment "${MYSQL_CLUSTER}-pitr" >/dev/null 2>&1 && break
  sleep 2
done
! kubectl -n "$MYSQL_NAMESPACE" get deployment "${MYSQL_CLUSTER}-pitr" >/dev/null 2>&1 \
  || fail "PITR collector did not stop for gap injection"
mysql_exec "${MYSQL_CLUSTER}-haproxy.${MYSQL_NAMESPACE}.svc" \
  "REPLACE INTO kubeauto_restore.pitr_marker VALUES (103, 'intentional-binlog-gap');"
for pod in ${MYSQL_CLUSTER}-pxc-0 ${MYSQL_CLUSTER}-pxc-1 ${MYSQL_CLUSTER}-pxc-2; do
  mysql_exec 127.0.0.1 "FLUSH BINARY LOGS; PURGE BINARY LOGS BEFORE NOW();" "$pod"
done
kubectl -n "$MYSQL_NAMESPACE" patch pxc "$MYSQL_CLUSTER" --type=merge \
  -p '{"spec":{"backup":{"pitr":{"enabled":true}}}}' >/dev/null
wait_pitr_deployment_ready || fail "PITR collector did not restart after gap injection"
mysql_exec "${MYSQL_CLUSTER}-haproxy.${MYSQL_NAMESPACE}.svc" \
  "REPLACE INTO kubeauto_restore.pitr_marker VALUES (104, 'after-binlog-gap');"
for pod in ${MYSQL_CLUSTER}-pxc-0 ${MYSQL_CLUSTER}-pxc-1 ${MYSQL_CLUSTER}-pxc-2; do
  mysql_exec 127.0.0.1 "FLUSH BINARY LOGS;" "$pod"
done
gap_detected=false
for attempt in $(seq 1 30); do
  gap_condition="$(kubectl -n "$MYSQL_NAMESPACE" get pxc-backup pitr-gap-base -o json \
    | jq -r '.status.conditions[]? | select(.type == "PITRReady" and .status == "False" and .reason == "BinlogGapDetected") | .reason')"
  if [[ "$gap_condition" == BinlogGapDetected ]]; then
    gap_detected=true
    break
  fi
  sleep 10
done
[[ "$gap_detected" == true ]] || fail "Operator did not mark the backup PITR-unready after a binlog gap"
cat <<EOF | kubectl apply -f - >/dev/null
apiVersion: pxc.percona.com/v1
kind: PerconaXtraDBClusterRestore
metadata:
  name: pitr-gap-refusal
  namespace: ${MYSQL_NAMESPACE}
  labels:
    kubeauto.io/component: percona-pxc-test
spec:
  pxcCluster: ${MYSQL_CLUSTER}
  backupName: pitr-gap-base
  pitr:
    type: latest
    backupSource:
      storageName: s3-binlogs
EOF
wait_restore_state pitr-gap-refusal Failed 300 || fail "unsafe PITR was not refused"
gap_comments="$(kubectl -n "$MYSQL_NAMESPACE" get pxc-restore pitr-gap-refusal -o jsonpath='{.status.comments}')"
grep -Fq "Backup doesn't guarantee consistent recovery with PITR" <<<"$gap_comments" \
  || fail "PITR gap refusal did not expose the official safety reason"
[[ -z "$(kubectl -n "$MYSQL_NAMESPACE" get pxc-restore pitr-gap-refusal \
  -o jsonpath='{.metadata.annotations.percona\.com/unsafe-pitr}' 2>/dev/null || true)" ]] \
  || fail "unsafe-pitr annotation must not be used by the delivery gate"
pass "MYSQL-11 PITR restored an exact GTID and refused a binlog-gap chain"

echo MYSQL_PXC_FULL_GATE_PASS
