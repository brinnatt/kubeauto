#!/bin/bash
# Focused retest after OpenEBS ns-race / hubble timeout / prom timeout fixes.
set -uo pipefail
LOG=/var/log/kubeauto-delivery-retest-$(date +%Y%m%d-%H%M).log
exec > >(tee -a "$LOG") 2>&1
BASE=/usr/local/kubeauto
export PYTHONPATH="$BASE" PATH=/usr/local/bin:/usr/bin:$PATH
K=kubecli
PASS_N=0; FAIL_N=0; SKIP_N=0
pass(){ echo "[PASS] $*"; PASS_N=$((PASS_N+1)); }
fail(){ echo "[FAIL] $*"; FAIL_N=$((FAIL_N+1)); }
skip(){ echo "[SKIP] $*"; SKIP_N=$((SKIP_N+1)); }
section(){ echo; echo "========== $* =========="; }
abort(){
  fail "$*"
  echo "DELIVERY_RETEST_COMPLETE PASS=$PASS_N FAIL=$FAIL_N SKIP=$SKIP_N LOG=$LOG"
  echo "DELIVERY_RETEST_FAILED"
  exit 1
}
abort_after_namespace_cleanup(){
  local message="$1" namespace="$2"
  wait_namespace_deleted "$namespace" 120 || true
  abort "$message"
}
capture_etcd_timeout_diagnostics(){
  local cluster="$1" cluster_dir="$BASE/clusters/$1" etcd_ip metrics
  echo "========== transient etcd diagnostics cluster=$cluster =========="
  kubectl --kubeconfig="$cluster_dir/kubectl.kubeconfig" get --raw='/readyz?verbose' 2>&1 || true
  kubectl --kubeconfig="$cluster_dir/kubectl.kubeconfig" get nodes -o wide 2>&1 || true
  metrics='etcd_server_has_leader|etcd_server_leader_changes_seen_total|etcd_server_proposals_pending|etcd_server_proposals_failed_total|etcd_disk_wal_fsync_duration_seconds|etcd_disk_backend_commit_duration_seconds'
  while read -r etcd_ip; do
    [ -n "$etcd_ip" ] || continue
    echo "--- etcd endpoint $etcd_ip ---"
    "$BASE/extra-bin/etcdctl" \
      --endpoints="https://$etcd_ip:2379" \
      --cacert="$cluster_dir/ssl/ca.pem" \
      --cert="$cluster_dir/ssl/etcd.pem" \
      --key="$cluster_dir/ssl/etcd-key.pem" \
      endpoint status --write-out=table 2>&1 || true
    curl -fsS --max-time 10 \
      --cacert "$cluster_dir/ssl/ca.pem" \
      --cert "$cluster_dir/ssl/etcd.pem" \
      --key "$cluster_dir/ssl/etcd-key.pem" \
      "https://$etcd_ip:2379/metrics" 2>/dev/null \
      | grep -E "^($metrics)" || true
    if [[ "$etcd_ip" == "127.0.0.1" || "$etcd_ip" == "192.168.47.138" ]]; then
      bash -lc 'uptime; free -m; df -h /var/lib/etcd; vmstat 1 3; journalctl -u etcd --since "-3 minutes" --no-pager | tail -n 160' || true
    else
      ssh -o BatchMode=yes -o StrictHostKeyChecking=no -o ConnectTimeout=8 \
        "root@$etcd_ip" \
        'uptime; free -m; df -h /var/lib/etcd; vmstat 1 3; journalctl -u etcd --since "-3 minutes" --no-pager | tail -n 160' \
        || true
    fi
  done < <(awk '
    /^\[etcd\]$/ { in_etcd=1; next }
    /^\[/ { if (in_etcd) exit; next }
    in_etcd && $1 !~ /^#/ && NF { print $1 }
  ' "$cluster_dir/hosts")
  echo "========== transient etcd diagnostics complete =========="
}
setup_with_transient_retry(){
  local cluster="$1" step="$2" attempts="${3:-3}" attempt log
  log=$(mktemp "/tmp/kubeauto-${cluster}-${step}.XXXXXX.log")
  for attempt in $(seq 1 "$attempts"); do
    : > "$log"
    echo "[WAIT] kubecli setup cluster=$cluster step=$step attempt=$attempt/$attempts"
    if "$K" setup "$cluster" "$step" </dev/null 2>&1 | tee "$log"; then
      rm -f "$log"
      return 0
    fi
    if [ "$attempt" -ge "$attempts" ] \
      || ! grep -Eq 'etcdserver: request timed out|TLS handshake timeout|connection reset by peer|context deadline exceeded' "$log"; then
      echo "[FAIL] kubecli setup cluster=$cluster step=$step non-transient failure"
      rm -f "$log"
      return 1
    fi
    echo "[WAIT] kubecli setup transient API/etcd failure; control-plane recovery before retry"
    capture_etcd_timeout_diagnostics "$cluster"
    for recovery in $(seq 1 30); do
      if [ "$(kubectl --kubeconfig="$BASE/clusters/$cluster/kubectl.kubeconfig" get --raw=/readyz 2>/dev/null || true)" = ok ]; then
        break
      fi
      [ "$recovery" -lt 30 ] && sleep 10
    done
  done
  rm -f "$log"
  return 1
}
fix_registry_hosts(){
  local ip
  for ip in 131 132 133 134 135 136 137; do
    ssh -o BatchMode=yes -o StrictHostKeyChecking=no -o ConnectTimeout=8 root@192.168.47.$ip \
      "sed -i '/registry.talkschool.cn/d' /etc/hosts; echo '192.168.47.138    registry.talkschool.cn' >> /etc/hosts" \
      2>/dev/null || echo "[WARN] hosts 47.$ip"
  done
}
nodes_ready(){
  local c="$1" tries="${2:-48}"; export KUBECONFIG="$BASE/clusters/$c/kubectl.kubeconfig"
  local attempt ready total
  for attempt in $(seq 1 "$tries"); do
    total=$(kubectl get nodes --no-headers 2>/dev/null | wc -l)
    ready=$(kubectl get nodes --no-headers 2>/dev/null | awk '$2 ~ /^Ready/ {count++} END {print count+0}')
    if [ "$total" -gt 0 ] && [ "$ready" -eq "$total" ]; then
      echo "[WAIT] cluster=$c nodes status=Ready ready=$ready/$total attempt=$attempt/$tries"
      return 0
    fi
    echo "[WAIT] cluster=$c nodes status=not-ready ready=$ready/$total attempt=$attempt/$tries next_check=10s"
    [ "$attempt" -lt "$tries" ] && sleep 10
  done
  kubectl get nodes -o wide || true
  return 1
}
wait_pods_ns(){
  local ns="$1" tries="${2:-48}" attempt bad total pod_counts
  for attempt in $(seq 1 "$tries"); do
    pod_counts=$(kubectl get pods -n "$ns" --no-headers 2>/dev/null | awk '
      $3 != "Completed" && $3 != "Succeeded" {
        total++
        split($2, ready, "/")
        if ($3 != "Running" || ready[1] != ready[2]) bad++
      }
      END {print total+0, bad+0}
    ')
    read -r total bad <<< "${pod_counts:-0 1}"
    if [ "$total" -gt 0 ] && [ "$bad" -eq 0 ]; then
      echo "[WAIT] namespace=$ns pods status=Ready total=$total attempt=$attempt/$tries"
      return 0
    fi
    echo "[WAIT] namespace=$ns pods status=not-ready total=$total bad=$bad attempt=$attempt/$tries next_check=10s"
    [ "$attempt" -lt "$tries" ] && sleep 10
  done
  kubectl get pods -n "$ns" -o wide || true
  kubectl get events -n "$ns" --sort-by=.lastTimestamp 2>/dev/null | tail -n 80 || true
  while read -r pod; do
    [ -n "$pod" ] && kubectl describe pod -n "$ns" "$pod" || true
  done < <(kubectl get pods -n "$ns" --no-headers 2>/dev/null | awk '
    $3 != "Completed" && $3 != "Succeeded" {
      split($2, ready, "/")
      if ($3 != "Running" || ready[1] != ready[2]) print $1
    }
  ')
  return 1
}
wait_rocketmq_ha(){
  local tries="${1:-90}" attempt pod_counts nameservice_ready broker_ready not_ready
  for attempt in $(seq 1 "$tries"); do
    pod_counts=$(kubectl get pods -n rocketmq --no-headers 2>/dev/null | awk '
      $1 ~ /^name-service-/ || $1 ~ /^broker-/ {
        split($2, ready, "/")
        is_ready = ($3 == "Running" && ready[1] == ready[2])
        if ($1 ~ /^name-service-/ && is_ready) nameservice_ready++
        if ($1 ~ /^broker-/ && is_ready) broker_ready++
        if (!is_ready) not_ready++
      }
      END {print nameservice_ready+0, broker_ready+0, not_ready+0}
    ')
    read -r nameservice_ready broker_ready not_ready <<< "${pod_counts:-0 0 1}"
    echo "[WAIT] RocketMQ HA nameservice_ready=$nameservice_ready/2 broker_ready=$broker_ready/1 not_ready=$not_ready attempt=$attempt/$tries"
    if [ "$nameservice_ready" -ge 2 ] && [ "$broker_ready" -ge 1 ] \
      && [ "$not_ready" -eq 0 ]; then
      return 0
    fi
    [ "$attempt" -lt "$tries" ] && sleep 10
  done
  kubectl -n rocketmq get nameservice,broker -o wide || true
  kubectl -n rocketmq get pods -o wide || true
  kubectl -n rocketmq get events --sort-by=.lastTimestamp 2>/dev/null | tail -n 80 || true
  return 1
}
assert_no_imagepull(){
  local scope=("-A")
  if [ -n "${1:-}" ]; then
    scope=("-n" "$1")
  fi
  if kubectl get pods "${scope[@]}" --no-headers 2>/dev/null | grep -E 'ImagePullBackOff|ErrImagePull'; then
    kubectl get pods "${scope[@]}" | grep -E 'ImagePullBackOff|ErrImagePull' || true
    return 1
  fi; return 0
}
require_registry_tag(){
  local repository="$1" tag="$2"
  curl -fsS "http://127.0.0.1:5000/v2/${repository}/tags/list" 2>/dev/null \
    | grep -Fq "\"${tag}\""
}
materialize_registry_image(){
  local repository="$1" tag="$2" timeout_seconds="$3" source
  local local_ref="127.0.0.1:5000/${repository}:${tag}"
  shift 3

  if require_registry_tag "$repository" "$tag"; then
    echo "[PASS] registry fixture already present image=$local_ref"
    return 0
  fi

  for source in "$@"; do
    echo "[WAIT] registry fixture pull source=$source target=$local_ref"
    if timeout "$timeout_seconds" docker pull "$source"; then
      if docker image inspect "$source" >/dev/null 2>&1 \
        && docker tag "$source" "$local_ref" \
        && docker push "$local_ref" \
        && require_registry_tag "$repository" "$tag"; then
        echo "[PASS] registry fixture materialized source=$source target=$local_ref"
        return 0
      fi
      echo "[WARN] registry fixture publish/verify failed source=$source"
      docker image rm "$local_ref" >/dev/null 2>&1 || true
    else
      pull_rc=$?
      echo "[WARN] registry fixture pull failed source=$source rc=$pull_rc timeout_seconds=$timeout_seconds"
    fi
  done
  return 1
}
prep_lvm_remote(){
  local ip="$1"
  scp -o BatchMode=yes -o StrictHostKeyChecking=no "$BASE/tests/helpers/prep-node-lvm-loop.sh" root@192.168.47.$ip:/tmp/prep-lvm.sh
  ssh -o BatchMode=yes -o StrictHostKeyChecking=no root@192.168.47.$ip "bash /tmp/prep-lvm.sh vg_k8s ${2:-20}"
}
set_cfg(){
  local f="$1" k="$2" v="$3" replacement
  replacement=$(printf '%s' "$v" | sed 's/[\\&|]/\\&/g')
  if grep -q "^${k}:" "$f"; then
    sed -i "s|^${k}:.*|${k}: ${replacement}|" "$f"
  else
    printf '%s: %s\n' "$k" "$v" >> "$f"
  fi
}
hard_reset_node(){
  local ip="$1"
  ssh -o BatchMode=yes -o StrictHostKeyChecking=no "root@192.168.47.${ip}" 'bash -s' <<'RST'
systemctl stop kubelet 2>/dev/null || true
pkill -9 kubelet 2>/dev/null || true
mount | awk '/kubelet|kubernetes/ {print $3}' | sort -r | while read m; do umount -l "$m" 2>/dev/null || true; done
rm -rf /etc/kubernetes /var/lib/kubelet /var/lib/etcd /etc/cni /opt/cni/bin /var/lib/cni /run/flannel
systemctl reset-failed kubelet 2>/dev/null || true
systemctl disable kubelet 2>/dev/null || true
sed -i '/registry.talkschool.cn/d' /etc/hosts
echo '192.168.47.138    registry.talkschool.cn' >> /etc/hosts
RST
}
hard_reset_133(){ hard_reset_node 133; }
schedulable_ready_count(){
  kubectl get nodes --no-headers 2>/dev/null | awk '$2 == "Ready" {count++} END {print count+0}'
}

wait_nacos_external_mysql(){
  local tries="${1:-60}" attempt nacos_ready nacos_platform mysql_pod mysql_connections
  for attempt in $(seq 1 "$tries"); do
    nacos_ready=$(kubectl -n nacos get pods -l app=nacos --no-headers 2>/dev/null \
      | awk '$2=="1/1" && $3=="Running" {count++} END {print count+0}')
    nacos_platform=$(kubectl -n nacos get statefulset nacos \
      -o jsonpath='{.spec.template.spec.containers[0].env[?(@.name=="SPRING_DATASOURCE_PLATFORM")].value}' \
      2>/dev/null || true)
    mysql_pod=$(kubectl -n nacos get pod -l app=nacos-mysql \
      -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)
    mysql_connections=0
    if [ -n "$mysql_pod" ]; then
      mysql_connections=$(kubectl -n nacos exec "$mysql_pod" -- \
        mysql -h127.0.0.1 -uroot -p'NacosRoot!23' -Nse \
        "SELECT COUNT(*) FROM information_schema.processlist WHERE USER='nacos';" \
        2>/dev/null || echo 0)
    fi
    echo "[WAIT] nacos_ready=$nacos_ready/3 datasource_platform=${nacos_platform:-missing} mysql_connections=${mysql_connections:-0} attempt=$attempt/$tries next_check=10s"
    if [ "$nacos_ready" -ge 3 ] && [ "$nacos_platform" = mysql ] \
      && [ "${mysql_connections:-0}" -ge 1 ]; then
      return 0
    fi
    [ "$attempt" -lt "$tries" ] && sleep 10
  done
  kubectl -n nacos get pods -o wide || true
  kubectl -n nacos get pods -l app=nacos \
    -o custom-columns='NAME:.metadata.name,NODE:.spec.nodeName,RESTARTS:.status.containerStatuses[0].restartCount,LAST_REASON:.status.containerStatuses[0].lastState.terminated.reason,LAST_EXIT:.status.containerStatuses[0].lastState.terminated.exitCode,LAST_FINISHED:.status.containerStatuses[0].lastState.terminated.finishedAt' \
    || true
  kubectl -n nacos get events --sort-by=.lastTimestamp 2>/dev/null | tail -n 80 || true
  while read -r pod; do
    [ -n "$pod" ] || continue
    echo "[DIAG] pod=$pod previous container stdout"
    kubectl -n nacos logs "$pod" -c nacos --previous --tail=300 || true
    echo "[DIAG] pod=$pod persistent Nacos startup logs"
    for diagnostic_attempt in $(seq 1 6); do
      if kubectl -n nacos exec "$pod" -c nacos -- sh -c '
        for file in /home/nacos/logs/start.out /home/nacos/logs/nacos.log; do
          if [ -s "$file" ]; then
            echo "---$file---"
            tail -n 300 "$file"
          fi
        done
      '; then
        break
      fi
      [ "$diagnostic_attempt" -lt 6 ] && sleep 5
    done
  done < <(kubectl -n nacos get pods -l app=nacos \
    -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}' 2>/dev/null)
  return 1
}
ensure_test_ha_worker(){
  local ip="$1" name="$2" attempt ready unschedulable
  if ! kubectl get node "$name" >/dev/null 2>&1; then
    echo "[PREP] adding test-ha worker name=$name ip=192.168.47.$ip"
    hard_reset_node "$ip"
    $K add-node test-ha "192.168.47.$ip" "$name" </dev/null || return 1
  fi
  for attempt in $(seq 1 30); do
    ready=$(kubectl get node "$name" -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}' 2>/dev/null || true)
    unschedulable=$(kubectl get node "$name" -o jsonpath='{.spec.unschedulable}' 2>/dev/null || true)
    if [ "$ready" = "True" ] && [ "$unschedulable" != "true" ]; then
      echo "[PREP] test-ha worker=$name status=Ready,Schedulable attempt=$attempt/30"
      return 0
    fi
    echo "[PREP] test-ha worker=$name ready=${ready:-missing} unschedulable=${unschedulable:-false} attempt=$attempt/30 next_check=10s"
    [ "$attempt" -lt 30 ] && sleep 10
  done
  return 1
}

release_ha_addon_capacity(){
  local ns attempt remaining
  echo "[CLEAN] releasing verified HA addon workloads before network-check"
  for ns in nacos minio minio-operator rocketmq monitor openebs ingress-nginx; do
    kubectl delete namespace "$ns" --ignore-not-found --wait=false >/dev/null || return 1
  done
  for attempt in $(seq 1 60); do
    remaining=0
    for ns in nacos minio minio-operator rocketmq monitor openebs ingress-nginx; do
      kubectl get namespace "$ns" >/dev/null 2>&1 && remaining=$((remaining+1))
    done
    echo "[WAIT] HA addon namespace cleanup remaining=$remaining attempt=$attempt/60"
    [ "$remaining" -eq 0 ] && break
    [ "$attempt" -lt 60 ] && sleep 5
  done
  [ "$remaining" -eq 0 ] || return 1
  for attempt in $(seq 1 30); do
    if [ "$(kubectl get --raw=/readyz 2>/dev/null || true)" = "ok" ] \
      && nodes_ready test-ha 1; then
      echo "[WAIT] test-ha control plane recovered attempt=$attempt/30"
      return 0
    fi
    echo "[WAIT] test-ha control plane recovering attempt=$attempt/30 next_check=10s"
    [ "$attempt" -lt 30 ] && sleep 10
  done
  return 1
}

wait_namespace_deleted(){
  local ns="$1" tries="${2:-120}" attempt
  kubectl delete namespace "$ns" --ignore-not-found --wait=false >/dev/null || return 1
  for attempt in $(seq 1 "$tries"); do
    if ! kubectl get namespace "$ns" >/dev/null 2>&1; then
      echo "[WAIT] namespace=$ns cleanup status=Deleted attempt=$attempt/$tries"
      return 0
    fi
    echo "[WAIT] namespace=$ns cleanup status=Terminating attempt=$attempt/$tries next_check=5s"
    [ "$attempt" -lt "$tries" ] && sleep 5
  done
  echo "[DIAG] namespace=$ns deletion did not complete"
  kubectl get namespace "$ns" -o yaml || true
  kubectl get all,pvc -n "$ns" -o wide || true
  kubectl get events -n "$ns" --sort-by=.lastTimestamp | tail -n 80 || true
  return 1
}

recover_ha_control_plane(){
  local attempt
  for attempt in $(seq 1 30); do
    if [ "$(kubectl get --raw=/readyz 2>/dev/null || true)" = ok ] \
      && nodes_ready test-ha 1; then
      echo "[WAIT] test-ha control plane status=Ready attempt=$attempt/30"
      return 0
    fi
    echo "[WAIT] test-ha control plane status=recovering attempt=$attempt/30 next_check=10s"
    [ "$attempt" -lt 30 ] && sleep 10
  done
  return 1
}

set_ha_addons_disabled(){
  local cfg="$1"
  set_cfg "$cfg" nacos_install '"no"'
  set_cfg "$cfg" minio_install '"no"'
  set_cfg "$cfg" rocketmq_install '"no"'
  set_cfg "$cfg" prom_install '"no"'
  set_cfg "$cfg" ingress_nginx_install '"no"'
  set_cfg "$cfg" dashboard_install '"no"'
  set_cfg "$cfg" network_check_enabled 'false'
}

echo "LOG=$LOG"
section "0 prep"
if $K docker -e </dev/null; then pass "G5 docker -e"; else abort "G5 docker -e"; fi
fix_registry_hosts
hard_reset_133
prep_lvm_remote 133 20 || true
scp -o BatchMode=yes -o StrictHostKeyChecking=no "$BASE/tests/helpers/prep-nfs-server.sh" root@192.168.47.133:/tmp/prep-nfs.sh
ssh -o BatchMode=yes -o StrictHostKeyChecking=no root@192.168.47.133 'bash /tmp/prep-nfs.sh /data/nfs'
$K download -E openebs </dev/null || abort "download openebs images"
$K download -E nfs-provisioner </dev/null || abort "download nfs-provisioner images"
$K download -E rocketmq </dev/null || abort "download rocketmq images"
$K download -E cilium </dev/null || abort "download cilium images"
$K download -E cilium-hubble </dev/null || abort "download cilium-hubble images"
$K download -E prometheus-dingtalk </dev/null || abort "download prometheus-dingtalk image"
$K download -E network-check </dev/null || abort "download network-check images"
require_registry_tag brinnatt/rocketmq-console 2.0.0 \
  || abort "local registry missing brinnatt/rocketmq-console:2.0.0"

###############################################################################
section "P1 Rocky G8b on large-memory test137"
export KUBECONFIG="$BASE/clusters/test137/kubectl.kubeconfig"
$K checkout test137 </dev/null || true
if nodes_ready test137 18; then
  CFG="$BASE/clusters/test137/config.yml"
  set_cfg "$CFG" dashboard_install '"yes"'; set_cfg "$CFG" prom_install '"yes"'
  set_cfg "$CFG" ingress_nginx_install '"yes"'; set_cfg "$CFG" local_path_provisioner_install '"yes"'
  set_cfg "$CFG" openebs_install '"yes"'; set_cfg "$CFG" openebs_lvm_enabled '"no"'
  set_cfg "$CFG" minio_install '"no"'; set_cfg "$CFG" nacos_install '"no"'; set_cfg "$CFG" rocketmq_install '"no"'
  if setup_with_transient_retry test137 07 3; then
    export KUBECONFIG="$BASE/clusters/test137/kubectl.kubeconfig"
    wait_pods_ns monitor 90 || abort "P1 Rocky monitor pods"
    wait_pods_ns openebs 48 || abort "P1 Rocky openebs pods"
    kubectl -n ingress-nginx rollout status deploy/ingress-nginx-controller --timeout=600s || abort "P1 Rocky ingress rollout"
    kubectl -n kube-system rollout status deploy/kubernetes-dashboard-api --timeout=600s || abort "P1 Rocky dashboard rollout"
    assert_no_imagepull || abort "P1 Rocky ImagePull"
    prom=$(kubectl -n monitor get deploy prometheus-kube-prometheus-operator -o jsonpath='{.spec.template.spec.containers[0].image}' 2>/dev/null || true)
    dash=$(kubectl -n kube-system get deploy kubernetes-dashboard-api -o jsonpath='{.spec.template.spec.containers[0].image}' 2>/dev/null || true)
    ingress=$(kubectl -n ingress-nginx get deploy ingress-nginx-controller -o jsonpath='{.spec.template.spec.containers[0].image}' 2>/dev/null || true)
    echo "prom=$prom dash=$dash ingress=$ingress"
    if echo "$prom" | grep -q brinnatt/prometheus-operator \
      && echo "$dash" | grep -q brinnatt/dashboard-api \
      && echo "$ingress" | grep -q brinnatt/ingress-nginx-controller; then
      pass "P1 Rocky G8b addons"
    else
      abort "P1 Rocky G8b images"
    fi
  else
    abort "P1 Rocky test137 setup 07"
  fi
else
  abort "P1 Rocky test137 not Ready"
fi

###############################################################################
section "P0 RocketMQ single-node on large-memory aio (138)"
export KUBECONFIG="$BASE/clusters/aio/kubectl.kubeconfig"
$K checkout aio </dev/null || abort "checkout aio for RocketMQ"
nodes_ready aio 18 || abort "aio not Ready for RocketMQ"
CFG="$BASE/clusters/aio/config.yml"
set_cfg "$CFG" rocketmq_install '"yes"'
set_cfg "$CFG" rocketmq_storage_class '"local-path"'
set_cfg "$CFG" rocketmq_nameservice_size '1'
setup_with_transient_retry aio 07 3 || abort "P0 RocketMQ aio setup"
export KUBECONFIG="$BASE/clusters/aio/kubectl.kubeconfig"
if wait_pods_ns rocketmq 72 && assert_no_imagepull rocketmq; then
  pass "P0 RocketMQ single-node Ready"
else
  abort "P0 RocketMQ single-node pods"
fi
/usr/local/kubeauto/extra-bin/helm uninstall rocketmq-operator -n rocketmq --wait --timeout 10m 2>/dev/null || true
set_cfg "$CFG" rocketmq_install '"no"'
wait_namespace_deleted rocketmq 120 || abort "P0 RocketMQ aio cleanup"

###############################################################################
section "P0 LVM+NFS on deliver-lvm (133)"
C_LVM=deliver-lvm
$K destroy "$C_LVM" </dev/null 2>/dev/null || rm -rf "$BASE/clusters/$C_LVM"
$K new "$C_LVM" 2>/dev/null || true
bash "$BASE/tests/helpers/mk-cluster-133.sh" "$C_LVM" calico ipvs
sed -i 's/__k8s_ver__/1.33.6/g' "$BASE/clusters/$C_LVM/config.yml"
CFG="$BASE/clusters/$C_LVM/config.yml"
set_cfg "$CFG" openebs_install '"yes"'; set_cfg "$CFG" openebs_lvm_enabled '"yes"'; set_cfg "$CFG" openebs_lvm_vg '"vg_k8s"'
set_cfg "$CFG" local_path_provisioner_install '"yes"'
set_cfg "$CFG" nfs_provisioner_install '"yes"'; set_cfg "$CFG" nfs_server '"192.168.47.133"'; set_cfg "$CFG" nfs_path '"/data/nfs"'
set_cfg "$CFG" rocketmq_install '"no"'
set_cfg "$CFG" dashboard_install '"no"'; set_cfg "$CFG" prom_install '"no"'; set_cfg "$CFG" minio_install '"no"'; set_cfg "$CFG" nacos_install '"no"'

if $K setup "$C_LVM" 90 </dev/null && $K setup "$C_LVM" 07 </dev/null; then
  export KUBECONFIG="$BASE/clusters/$C_LVM/kubectl.kubeconfig"
  nodes_ready "$C_LVM" || abort "deliver-lvm nodes"
  sleep 20
  kubectl -n openebs get pods -o wide || true
  if wait_pods_ns openebs 48 && assert_no_imagepull openebs; then
    cat <<'EOF' | kubectl apply -f -
apiVersion: v1
kind: PersistentVolumeClaim
metadata: { name: lvm-smoke-pvc }
spec: { accessModes: ["ReadWriteOnce"], storageClassName: openebs-lvmpv, resources: { requests: { storage: 1Gi } } }
---
apiVersion: v1
kind: Pod
metadata: { name: lvm-smoke-pod }
spec:
  containers:
  - name: busy
    image: registry.talkschool.cn:5000/brinnatt/linux-utils:4.2.0
    command: ["sh","-c","echo lvm-ok > /data/out.txt && cat /data/out.txt && sleep 3600"]
    volumeMounts: [{ name: d, mountPath: /data }]
  volumes: [{ name: d, persistentVolumeClaim: { claimName: lvm-smoke-pvc } }]
  restartPolicy: Never
EOF
    ok=0
    for i in $(seq 1 48); do
      ph=$(kubectl get pod lvm-smoke-pod -o jsonpath='{.status.phase}' 2>/dev/null || true)
      if [ "$ph" = "Running" ] || [ "$ph" = "Succeeded" ]; then
        out=$(kubectl exec lvm-smoke-pod -- cat /data/out.txt 2>/dev/null || true)
        echo "$out" | grep -q lvm-ok && ok=1 && break
      fi; sleep 10
    done
    kubectl get pvc lvm-smoke-pvc -o wide || true
    kubectl get pod lvm-smoke-pod -o wide || true
    [ "$ok" = 1 ] && pass "P0 OpenEBS LVM R/W" || abort "P0 OpenEBS LVM R/W"
  else abort "P0 OpenEBS pods"; fi

  cat <<'EOF' | kubectl apply -f -
apiVersion: v1
kind: PersistentVolumeClaim
metadata: { name: nfs-smoke-pvc }
spec: { accessModes: ["ReadWriteMany"], storageClassName: managed-nfs-storage, resources: { requests: { storage: 1Gi } } }
---
apiVersion: v1
kind: Pod
metadata: { name: nfs-smoke-pod }
spec:
  containers:
  - name: busy
    image: registry.talkschool.cn:5000/brinnatt/linux-utils:4.2.0
    command: ["sh","-c","echo nfs-ok > /data/out.txt && cat /data/out.txt && sleep 3600"]
    volumeMounts: [{ name: d, mountPath: /data }]
  volumes: [{ name: d, persistentVolumeClaim: { claimName: nfs-smoke-pvc } }]
EOF
  ok=0
  for i in $(seq 1 48); do
    ph=$(kubectl get pod nfs-smoke-pod -o jsonpath='{.status.phase}' 2>/dev/null || true)
    if [ "$ph" = "Running" ]; then
      out=$(kubectl exec nfs-smoke-pod -- cat /data/out.txt 2>/dev/null || true)
      echo "$out" | grep -q nfs-ok && ok=1 && break
    fi; sleep 10
  done
  kubectl get pvc nfs-smoke-pvc -o wide || true
  kubectl get pod nfs-smoke-pod -o wide || true
  [ "$ok" = 1 ] && pass "P0 NFS R/W" || abort "P0 NFS R/W"
else
  abort "P0 deliver-lvm setup"
fi

###############################################################################
section "P1 Hubble on deliver-hubble (133)"
$K destroy "$C_LVM" </dev/null 2>/dev/null || rm -rf "$BASE/clusters/$C_LVM"
hard_reset_133
C_HUB=deliver-hubble
$K destroy "$C_HUB" </dev/null 2>/dev/null || rm -rf "$BASE/clusters/$C_HUB"
$K new "$C_HUB" 2>/dev/null || true
bash "$BASE/tests/helpers/mk-cluster-133.sh" "$C_HUB" cilium ipvs
sed -i 's/__k8s_ver__/1.33.6/g' "$BASE/clusters/$C_HUB/config.yml"
CFG="$BASE/clusters/$C_HUB/config.yml"
set_cfg "$CFG" cilium_hubble_enabled 'true'
set_cfg "$CFG" cilium_hubble_ui_enabled 'true'
if $K setup "$C_HUB" 90 </dev/null; then
  export KUBECONFIG="$BASE/clusters/$C_HUB/kubectl.kubeconfig"
  nodes_ready "$C_HUB" || abort "hubble nodes"
  sleep 40
  kubectl -n kube-system get pods | grep -iE 'cilium|hubble' || true
  rel=$(kubectl -n kube-system get deploy hubble-relay -o jsonpath='{.spec.template.spec.containers[0].image}' 2>/dev/null || true)
  ui=$(kubectl -n kube-system get deploy hubble-ui -o jsonpath='{.spec.template.spec.containers[*].image}' 2>/dev/null || true)
  echo "relay=$rel ui=$ui"
  if echo "$rel" | grep -q brinnatt/hubble-relay && echo "$ui" | grep -q brinnatt/hubble-ui; then
    ok=1
    for d in hubble-relay hubble-ui; do
      kubectl -n kube-system rollout status deploy/$d --timeout=600s || ok=0
    done
    [ "$ok" = 1 ] && pass "P1 Cilium Hubble Running" || abort "P1 Hubble rollout"
  else abort "P1 Hubble deploy/images missing"; fi
  assert_no_imagepull || abort "P1 Hubble ImagePull"
else abort "P1 Hubble setup"; fi

###############################################################################
section "P0 Nacos + P1 MinIO=4 + HA addons on test-ha"
$K destroy "$C_HUB" </dev/null 2>/dev/null || rm -rf "$BASE/clusters/$C_HUB"
hard_reset_133
export KUBECONFIG="$BASE/clusters/test-ha/kubectl.kubeconfig"
$K checkout test-ha </dev/null || true
if ! nodes_ready test-ha 12; then abort "test-ha down"; fi
fix_registry_hosts
topology_ok=1
ensure_test_ha_worker 132 worker-132 || topology_ok=0
ensure_test_ha_worker 133 worker-133 || topology_ok=0
fix_registry_hosts
schedulable_n=$(schedulable_ready_count)
kubectl get nodes -o wide
echo "Schedulable Ready nodes=$schedulable_n required=3"
if [ "$schedulable_n" -lt 3 ]; then
  abort "P0 test-ha schedulable worker topology ready=$schedulable_n required=3"
fi

# A resumed focused gate must not inherit workloads from an interrupted run.
# Remove the owning Helm releases first, then require namespaces to disappear;
# a terminating namespace is a failed precondition, not something to ignore.
/usr/local/kubeauto/extra-bin/helm uninstall prometheus -n monitor --wait --timeout 10m 2>/dev/null || true
/usr/local/kubeauto/extra-bin/helm uninstall minio -n minio --wait --timeout 10m 2>/dev/null || true
/usr/local/kubeauto/extra-bin/helm uninstall minio-operator -n minio-operator --wait --timeout 10m 2>/dev/null || true
/usr/local/kubeauto/extra-bin/helm uninstall rocketmq-operator -n rocketmq --wait --timeout 10m 2>/dev/null || true
for ns in nacos minio minio-operator rocketmq monitor openebs ingress-nginx network-test; do
  wait_namespace_deleted "$ns" 120 || abort "preflight namespace cleanup $ns"
done
recover_ha_control_plane || abort "test-ha recovery after preflight cleanup"

kubectl create ns nacos 2>/dev/null || true
materialize_registry_image brinnatt/mysql 8.0.46 900 \
  docker.sparkcr.cn/mysql:8.0.46 \
  hub.talkedu.cn/kubeauto/mysql:8.0.46 \
  swr.cn-north-4.myhuaweicloud.com/ddn-k8s/docker.io/library/mysql:8.0.46 \
  mysql:8.0.46 \
  || abort_after_namespace_cleanup "P0 Nacos MySQL registry seed" nacos
cat <<'EOF' | kubectl apply -f -
apiVersion: v1
kind: Secret
metadata: { name: nacos-mysql, namespace: nacos }
type: Opaque
stringData: { MYSQL_ROOT_PASSWORD: "NacosRoot!23", MYSQL_DATABASE: "nacos", MYSQL_USER: "nacos", MYSQL_PASSWORD: "NacosUser!23" }
---
apiVersion: apps/v1
kind: Deployment
metadata: { name: nacos-mysql, namespace: nacos }
spec:
  replicas: 1
  selector: { matchLabels: { app: nacos-mysql } }
  template:
    metadata: { labels: { app: nacos-mysql } }
    spec:
      containers:
      - name: mysql
        image: registry.talkschool.cn:5000/brinnatt/mysql:8.0.46
        env:
        - { name: MYSQL_ROOT_PASSWORD, value: "NacosRoot!23" }
        - { name: MYSQL_DATABASE, value: nacos }
        - { name: MYSQL_USER, value: nacos }
        - { name: MYSQL_PASSWORD, value: "NacosUser!23" }
        ports: [{ containerPort: 3306 }]
        args: ["--character-set-server=utf8mb4","--collation-server=utf8mb4_unicode_ci","--explicit_defaults_for_timestamp=true"]
        readinessProbe:
          exec:
            command:
            - sh
            - -ec
            - mysqladmin ping -h 127.0.0.1 -uroot -p"$MYSQL_ROOT_PASSWORD" --silent
          initialDelaySeconds: 5
          periodSeconds: 5
          timeoutSeconds: 3
          failureThreshold: 60
---
apiVersion: v1
kind: Service
metadata: { name: nacos-mysql, namespace: nacos }
spec: { selector: { app: nacos-mysql }, ports: [{ port: 3306, targetPort: 3306 }] }
EOF
if wait_pods_ns nacos 48; then
  MYSQL_POD=$(kubectl -n nacos get pod -l app=nacos-mysql -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)
  kubectl -n nacos delete pod nacos-schema-source --ignore-not-found >/dev/null 2>&1 || true
  kubectl -n nacos run nacos-schema-source \
    --image=registry.talkschool.cn:5000/brinnatt/nacos-server:v2.4.3 \
    --restart=Never --command -- sh -c 'sleep 600'
  if [ -n "$MYSQL_POD" ] \
    && kubectl -n nacos wait --for=condition=Ready pod/nacos-schema-source --timeout=300s \
    && kubectl -n nacos exec nacos-schema-source -- test -s /home/nacos/conf/mysql-schema.sql \
    && kubectl -n nacos exec nacos-schema-source -- cat /home/nacos/conf/mysql-schema.sql \
      | kubectl -n nacos exec -i "$MYSQL_POD" -- mysql -h127.0.0.1 -uroot -p'NacosRoot!23' nacos; then
    nacos_tables=$(kubectl -n nacos exec "$MYSQL_POD" -- mysql -h127.0.0.1 -uroot -p'NacosRoot!23' -Nse \
      "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='nacos';" 2>/dev/null || echo 0)
    echo "nacos_schema_tables=$nacos_tables source=/home/nacos/conf/mysql-schema.sql"
    if [ "${nacos_tables:-0}" -ge 10 ]; then
      pass "P0 Nacos official mysql-schema.sql"
    else
      abort_after_namespace_cleanup "P0 Nacos schema table count=$nacos_tables" nacos
    fi
  else
    abort_after_namespace_cleanup "P0 Nacos official mysql-schema.sql import" nacos
  fi
  kubectl -n nacos delete pod nacos-schema-source --wait=false >/dev/null 2>&1 || true
else
  abort_after_namespace_cleanup "P0 Nacos MySQL not Ready" nacos
fi

CFG="$BASE/clusters/test-ha/config.yml"
set_cfg "$CFG" local_path_provisioner_install '"yes"'
set_cfg "$CFG" openebs_install '"yes"'; set_cfg "$CFG" openebs_lvm_enabled '"no"'
set_cfg "$CFG" nacos_mysql_host '"nacos-mysql.nacos.svc.cluster.local"'
set_cfg "$CFG" nacos_mysql_db '"nacos"'; set_cfg "$CFG" nacos_mysql_port '"3306"'
set_cfg "$CFG" nacos_mysql_user '"nacos"'; set_cfg "$CFG" nacos_mysql_password '"NacosUser!23"'
set_cfg "$CFG" nacos_mysql_db_param '"characterEncoding=utf8&connectTimeout=1000&socketTimeout=3000&autoReconnect=true&useUnicode=true&useSSL=false&serverTimezone=UTC&allowPublicKeyRetrieval=true"'
set_cfg "$CFG" nacos_storage_class '"local-path"'
# The lab workers have 3.5Gi each.  Nacos' official container defaults to a
# 1Gi heap plus a 512Mi young generation; size only this delivery topology down
# through the upstream-supported JVM variables, without changing product defaults.
set_cfg "$CFG" nacos_jvm_xms '"512m"'; set_cfg "$CFG" nacos_jvm_xmx '"512m"'
set_cfg "$CFG" nacos_jvm_xmn '"256m"'
set_cfg "$CFG" minio_pool_servers '4'
set_cfg "$CFG" minio_storage_class '"openebs-hostpath"'
set_cfg "$CFG" rocketmq_storage_class '"openebs-hostpath"'
set_cfg "$CFG" rocketmq_nameservice_size '2'; set_cfg "$CFG" rocketmq_replica_per_group '0'
set_ha_addons_disabled "$CFG"

$K download -E nacos </dev/null || abort "download nacos images"
$K download -E minio </dev/null || abort "download minio images"
$K download -E prometheus </dev/null || abort "download prometheus images"
$K download -E dashboard </dev/null || abort "download dashboard images"
$K download -E ingress-nginx </dev/null || abort "download ingress-nginx images"
$K download -E rocketmq </dev/null || abort "download rocketmq images"

[ "$topology_ok" -eq 1 ] || abort "test-ha topology precondition"

# These official charts together request more memory than the delivery lab's
# three small workers can sustain.  Validate one heavy addon at a time, remove
# it completely, and require API/node recovery before starting the next gate.
setup_with_transient_retry test-ha 07 3 || abort "test-ha base addon setup"

section "P0 Nacos HA with external MySQL"
set_cfg "$CFG" nacos_install '"yes"'
setup_with_transient_retry test-ha 07 3 || abort "P0 Nacos setup"
if wait_pods_ns nacos 72 && wait_nacos_external_mysql 60; then
  pass "P0 Nacos Ready replicas=3 external-MySQL"
else
  abort_after_namespace_cleanup "P0 Nacos replicas/external-MySQL" nacos
fi
set_cfg "$CFG" nacos_install '"no"'
wait_namespace_deleted nacos 120 || abort "P0 Nacos cleanup"
recover_ha_control_plane || abort "test-ha recovery after Nacos"

section "P1 MinIO official four-server Tenant"
set_cfg "$CFG" minio_install '"yes"'
setup_with_transient_retry test-ha 07 3 || abort "P1 MinIO setup"
if wait_pods_ns minio 72; then
  minio_health=""; pool_n=0
  for attempt in $(seq 1 60); do
    pool_n=$(kubectl -n minio get pods --no-headers 2>/dev/null | awk '$1 ~ /^myminio-pool-/ && $2=="2/2" && $3=="Running" {count++} END {print count+0}')
    minio_health=$(kubectl -n minio get tenant myminio -o jsonpath='{.status.healthStatus}' 2>/dev/null || true)
    echo "[WAIT] minio tenant=myminio health=${minio_health:-unknown} ready_pool_pods=$pool_n/4 attempt=$attempt/60 next_check=10s"
    [ "$pool_n" -ge 4 ] && [ "$minio_health" = green ] && break
    [ "$attempt" -lt 60 ] && sleep 10
  done
  if [ "$pool_n" -ge 4 ] && [ "$minio_health" = green ]; then
    pass "P1 MinIO operator+tenant pool=4 health=green"
  else
    kubectl -n minio get tenant,pods -o wide || true
    abort_after_namespace_cleanup "P1 MinIO pool=$pool_n health=${minio_health:-unknown}" minio
  fi
else
  kubectl -n minio get tenant,pods -o wide || true
  abort_after_namespace_cleanup "P1 MinIO" minio
fi
/usr/local/kubeauto/extra-bin/helm uninstall minio -n minio --wait --timeout 10m 2>/dev/null || true
/usr/local/kubeauto/extra-bin/helm uninstall minio-operator -n minio-operator --wait --timeout 10m 2>/dev/null || true
set_cfg "$CFG" minio_install '"no"'
wait_namespace_deleted minio 120 || abort "P1 MinIO tenant cleanup"
wait_namespace_deleted minio-operator 120 || abort "P1 MinIO operator cleanup"
recover_ha_control_plane || abort "test-ha recovery after MinIO"

section "P0 RocketMQ HA"
set_cfg "$CFG" rocketmq_install '"yes"'
setup_with_transient_retry test-ha 07 3 || abort "P0 RocketMQ setup"
if wait_rocketmq_ha 90 && assert_no_imagepull rocketmq; then
  nameservice_n=$(kubectl -n rocketmq get pods --no-headers 2>/dev/null | awk '$1 ~ /name-service/ && $3=="Running" {count++} END {print count+0}')
  broker_n=$(kubectl -n rocketmq get pods --no-headers 2>/dev/null | awk '$1 ~ /broker/ && $3=="Running" {count++} END {print count+0}')
  echo "rocketmq_nameservice_running=$nameservice_n broker_running=$broker_n"
  if [ "$nameservice_n" -ge 2 ] && [ "$broker_n" -ge 1 ]; then
    pass "P0 RocketMQ HA nameservice=2"
  else
    kubectl -n rocketmq get pods -o wide || true
    abort_after_namespace_cleanup "P0 RocketMQ HA topology" rocketmq
  fi
else
  kubectl -n rocketmq get pods -o wide || true
  abort_after_namespace_cleanup "P0 RocketMQ HA pods" rocketmq
fi
/usr/local/kubeauto/extra-bin/helm uninstall rocketmq-operator -n rocketmq --wait --timeout 10m 2>/dev/null || true
set_cfg "$CFG" rocketmq_install '"no"'
wait_namespace_deleted rocketmq 120 || abort "P0 RocketMQ cleanup"
recover_ha_control_plane || abort "test-ha recovery after RocketMQ"

section "P1 Prometheus, Ingress, Dashboard and DingTalk"
set_cfg "$CFG" dashboard_install '"yes"'
set_cfg "$CFG" prom_install '"yes"'
set_cfg "$CFG" ingress_nginx_install '"yes"'
setup_with_transient_retry test-ha 07 3 || abort "P1 monitoring/ingress/dashboard setup"
if wait_pods_ns monitor 90; then pass "P1 Prometheus HA"; else abort "P1 Prometheus HA"; fi
if wait_pods_ns ingress-nginx 60; then pass "P1 Ingress HA"; else abort "P1 Ingress HA"; fi
if kubectl -n kube-system rollout status deploy/kubernetes-dashboard-api --timeout=600s; then
  pass "P1 Dashboard HA"
else
  abort "P1 Dashboard HA"
fi
if kubectl apply -f "$BASE/roles/cluster-addon/templates/prometheus/dingtalk-webhook.yaml" \
  && kubectl -n monitor rollout status deploy/webhook-dingtalk --timeout=600s; then
  dingtalk_image=$(kubectl -n monitor get deploy webhook-dingtalk -o jsonpath='{.spec.template.spec.containers[0].image}' 2>/dev/null || true)
  if echo "$dingtalk_image" | grep -q 'brinnatt/prometheus-webhook-dingtalk:v2.1.0'; then
    pass "P1 prometheus-webhook-dingtalk Running"
  else
    abort "P1 dingtalk image=$dingtalk_image"
  fi
else
  abort "P1 prometheus-webhook-dingtalk rollout"
fi
/usr/local/kubeauto/extra-bin/helm uninstall prometheus -n monitor --wait --timeout 10m 2>/dev/null || true
set_cfg "$CFG" prom_install '"no"'
set_cfg "$CFG" ingress_nginx_install '"no"'
set_cfg "$CFG" dashboard_install '"no"'
wait_namespace_deleted monitor 120 || abort "P1 Prometheus cleanup"
wait_namespace_deleted ingress-nginx 120 || abort "P1 Ingress cleanup"
wait_namespace_deleted openebs 120 || abort "P1 OpenEBS cleanup before network-check"
set_cfg "$CFG" openebs_install '"no"'
recover_ha_control_plane || abort "test-ha recovery before network-check"

section "P1 network-check workloads"
set_cfg "$CFG" network_check_enabled 'true'
setup_with_transient_retry test-ha 07 3 || abort "P1 network-check setup"
assert_no_imagepull || abort "HA ImagePull"
network_ok=1
for deploy in echo-server echo-server-host; do
  kubectl -n network-test rollout status deploy/$deploy --timeout=600s || network_ok=0
done
json_mock_image=$(kubectl -n network-test get deploy echo-server -o jsonpath='{.spec.template.spec.containers[0].image}' 2>/dev/null || true)
echo "network_check_echo_image=$json_mock_image"
echo "$json_mock_image" | grep -q 'brinnatt/json-mock:v1.3.1' || network_ok=0
mapfile -t network_cronjobs < <(kubectl -n network-test get cronjob -l job=network-check -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}' 2>/dev/null)
if [ "${#network_cronjobs[@]}" -ne 9 ]; then
  echo "network_check_cronjob_count=${#network_cronjobs[@]}"
  network_ok=0
fi
gate_suffix=$(date +%H%M%S)
for cronjob in "${network_cronjobs[@]}"; do
  job="${cronjob}-gate-${gate_suffix}"
  echo "[WAIT] network-check cronjob=$cronjob job=$job status=creating"
  if kubectl -n network-test create job --from=cronjob/$cronjob "$job" \
    && kubectl -n network-test wait --for=condition=complete job/$job --timeout=300s; then
    echo "[WAIT] network-check cronjob=$cronjob job=$job status=Complete"
  else
    kubectl -n network-test get job,pod -l job-name="$job" -o wide || true
    kubectl -n network-test describe job "$job" || true
    kubectl -n network-test describe pod -l job-name="$job" || true
    kubectl -n network-test logs job/$job --all-containers --tail=100 || true
    network_ok=0
  fi
done
if [ "$network_ok" -eq 1 ]; then
  pass "P1 network-check 9/9 jobs Complete json-mock:v1.3.1"
else
  abort "P1 network-check execution"
fi

echo
echo "DELIVERY_RETEST_COMPLETE PASS=$PASS_N FAIL=$FAIL_N SKIP=$SKIP_N LOG=$LOG"
if [ "$FAIL_N" -ne 0 ] || [ "$SKIP_N" -ne 0 ]; then
  echo "DELIVERY_RETEST_FAILED"
  exit 1
fi
echo DELIVERY_RETEST_PASS
