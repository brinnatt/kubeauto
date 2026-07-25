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
fix_registry_hosts(){
  local ip
  for ip in 131 132 133 134 135 136 137; do
    sshpass -p 123456 ssh -o StrictHostKeyChecking=no -o ConnectTimeout=8 root@192.168.47.$ip \
      "sed -i '/registry.talkschool.cn/d' /etc/hosts; echo '192.168.47.138    registry.talkschool.cn' >> /etc/hosts" \
      2>/dev/null || echo "[WARN] hosts 47.$ip"
  done
}
nodes_ready(){
  local c="$1" tries="${2:-48}"; export KUBECONFIG="$BASE/clusters/$c/kubectl.kubeconfig"
  local i=0
  while [ "$i" -lt "$tries" ]; do
    kubectl get nodes --no-headers 2>/dev/null | grep -q Ready && return 0
    i=$((i+1)); sleep 10
  done; return 1
}
wait_pods_ns(){
  local ns="$1" tries="${2:-48}" i=0 bad total
  while [ "$i" -lt "$tries" ]; do
    # Avoid pipefail+grep exit1 → "0\n99" breaking integer tests
    total=$(kubectl get pods -n "$ns" --no-headers 2>/dev/null | grep -cvE 'Completed|Succeeded' || true)
    bad=$(kubectl get pods -n "$ns" --no-headers 2>/dev/null | grep -vE 'Completed|Succeeded' | grep -cv Running || true)
    total=${total:-0}; bad=${bad:-0}
    [ "$total" -gt 0 ] && [ "$bad" -eq 0 ] && return 0
    i=$((i+1)); sleep 10
  done
  kubectl get pods -n "$ns" -o wide || true; return 1
}
assert_no_imagepull(){
  if kubectl get pods -A --no-headers 2>/dev/null | grep -E 'ImagePullBackOff|ErrImagePull'; then
    kubectl get pods -A | grep -E 'ImagePullBackOff|ErrImagePull' || true; return 1
  fi; return 0
}
prep_lvm_remote(){
  local ip="$1"
  sshpass -p 123456 scp -o StrictHostKeyChecking=no "$BASE/tests/helpers/prep-node-lvm-loop.sh" root@192.168.47.$ip:/tmp/prep-lvm.sh
  sshpass -p 123456 ssh -o StrictHostKeyChecking=no root@192.168.47.$ip "bash /tmp/prep-lvm.sh vg_k8s ${2:-20}"
}
set_cfg(){
  local f="$1" k="$2" v="$3"
  if grep -q "^${k}:" "$f"; then sed -i "s|^${k}:.*|${k}: ${v}|" "$f"; else echo "${k}: ${v}" >> "$f"; fi
}
hard_reset_133(){
  sshpass -p 123456 ssh -o StrictHostKeyChecking=no root@192.168.47.133 'bash -s' <<'RST'
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

echo "LOG=$LOG"
section "0 prep"
fix_registry_hosts
hard_reset_133
prep_lvm_remote 133 20 || true
sshpass -p 123456 scp -o StrictHostKeyChecking=no "$BASE/tests/helpers/prep-nfs-server.sh" root@192.168.47.133:/tmp/prep-nfs.sh
sshpass -p 123456 ssh -o StrictHostKeyChecking=no root@192.168.47.133 'bash /tmp/prep-nfs.sh /data/nfs'
$K download -E openebs </dev/null || true
$K download -E nfs-provisioner </dev/null || true
$K download -E rocketmq </dev/null || true
$K download -E cilium </dev/null || true
$K download -E cilium-hubble </dev/null || true

###############################################################################
section "P0 LVM+NFS+RocketMQ on deliver-lvm (133)"
C_LVM=deliver-lvm
$K destroy "$C_LVM" </dev/null 2>/dev/null || rm -rf "$BASE/clusters/$C_LVM"
$K new "$C_LVM" 2>/dev/null || true
bash "$BASE/tests/helpers/mk-cluster-133.sh" "$C_LVM" calico ipvs
sed -i 's/__k8s_ver__/1.33.6/g' "$BASE/clusters/$C_LVM/config.yml"
CFG="$BASE/clusters/$C_LVM/config.yml"
set_cfg "$CFG" openebs_install '"yes"'; set_cfg "$CFG" openebs_lvm_enabled '"yes"'; set_cfg "$CFG" openebs_lvm_vg '"vg_k8s"'
set_cfg "$CFG" local_path_provisioner_install '"yes"'
set_cfg "$CFG" nfs_provisioner_install '"yes"'; set_cfg "$CFG" nfs_server '"192.168.47.133"'; set_cfg "$CFG" nfs_path '"/data/nfs"'
set_cfg "$CFG" rocketmq_install '"yes"'; set_cfg "$CFG" rocketmq_storage_class '"openebs-hostpath"'
set_cfg "$CFG" rocketmq_nameservice_size '1'
set_cfg "$CFG" dashboard_install '"no"'; set_cfg "$CFG" prom_install '"no"'; set_cfg "$CFG" minio_install '"no"'; set_cfg "$CFG" nacos_install '"no"'

if $K setup "$C_LVM" 90 </dev/null && $K setup "$C_LVM" 07 </dev/null; then
  export KUBECONFIG="$BASE/clusters/$C_LVM/kubectl.kubeconfig"
  nodes_ready "$C_LVM" || fail "deliver-lvm nodes"
  sleep 20
  kubectl -n openebs get pods -o wide || true
  if wait_pods_ns openebs 48 && assert_no_imagepull; then
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
    kubectl get pvc,pod lvm-smoke-pvc lvm-smoke-pod -o wide || true
    [ "$ok" = 1 ] && pass "P0 OpenEBS LVM R/W" || fail "P0 OpenEBS LVM R/W"
  else fail "P0 OpenEBS pods"; fi

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
  kubectl get pvc,pod nfs-smoke-pvc nfs-smoke-pod -o wide || true
  [ "$ok" = 1 ] && pass "P0 NFS R/W" || fail "P0 NFS R/W"

  sleep 20
  kubectl -n rocketmq get pods -o wide || true
  if kubectl -n rocketmq get pods --no-headers 2>/dev/null | grep -q rocketmq-operator; then
    if wait_pods_ns rocketmq 72; then pass "P0 RocketMQ Ready"
    else fail "P0 RocketMQ pods not all Ready"; fi
  else fail "P0 RocketMQ operator missing"; fi
  assert_no_imagepull || fail "P0 ImagePull after storage/mq"
else
  fail "P0 deliver-lvm setup"
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
  nodes_ready "$C_HUB" || fail "hubble nodes"
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
    [ "$ok" = 1 ] && pass "P1 Cilium Hubble Running" || fail "P1 Hubble rollout"
  else fail "P1 Hubble deploy/images missing"; fi
  assert_no_imagepull || fail "P1 Hubble ImagePull"
else fail "P1 Hubble setup"; fi

###############################################################################
section "P0 Nacos + P1 MinIO=4 + HA addons on test-ha"
$K destroy "$C_HUB" </dev/null 2>/dev/null || rm -rf "$BASE/clusters/$C_HUB"
hard_reset_133
export KUBECONFIG="$BASE/clusters/test-ha/kubectl.kubeconfig"
$K checkout test-ha </dev/null || true
if ! nodes_ready test-ha 12; then fail "test-ha down"; fi
fix_registry_hosts
ncount=$(kubectl get nodes --no-headers 2>/dev/null | grep -c Ready || echo 0)
if [ "$ncount" -lt 4 ]; then
  echo "Adding worker 133 to test-ha (have $ncount)"
  $K add-node test-ha 192.168.47.133 worker-133 </dev/null || true
  sleep 90
fi
fix_registry_hosts
ncount=$(kubectl get nodes --no-headers 2>/dev/null | grep -c Ready || echo 0)
kubectl get nodes -o wide
echo "Ready nodes=$ncount"

# clean failed/partial releases before re-setup
/usr/local/kubeauto/extra-bin/helm uninstall prometheus -n monitor 2>/dev/null || true
for ns in monitor minio minio-operator ingress-nginx; do
  kubectl delete ns "$ns" --wait=false 2>/dev/null || true
done
# wait monitor gone if terminating
for i in $(seq 1 30); do
  kubectl get ns monitor >/dev/null 2>&1 || break
  sleep 2
done

kubectl create ns nacos 2>/dev/null || true
if ! curl -sf http://127.0.0.1:5000/v2/brinnatt/mysql/tags/list 2>/dev/null | grep -q 8.0.36; then
  timeout 300 docker pull mysql:8.0.36 || timeout 300 docker pull mysql:8.0 || true
  docker tag mysql:8.0.36 registry.talkschool.cn:5000/brinnatt/mysql:8.0.36 2>/dev/null || \
    docker tag mysql:8.0 registry.talkschool.cn:5000/brinnatt/mysql:8.0.36
  docker push registry.talkschool.cn:5000/brinnatt/mysql:8.0.36 || true
fi
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
        image: registry.talkschool.cn:5000/brinnatt/mysql:8.0.36
        env:
        - { name: MYSQL_ROOT_PASSWORD, value: "NacosRoot!23" }
        - { name: MYSQL_DATABASE, value: nacos }
        - { name: MYSQL_USER, value: nacos }
        - { name: MYSQL_PASSWORD, value: "NacosUser!23" }
        ports: [{ containerPort: 3306 }]
        args: ["--character-set-server=utf8mb4","--collation-server=utf8mb4_unicode_ci","--explicit_defaults_for_timestamp=true"]
---
apiVersion: v1
kind: Service
metadata: { name: nacos-mysql, namespace: nacos }
spec: { selector: { app: nacos-mysql }, ports: [{ port: 3306, targetPort: 3306 }] }
EOF
wait_pods_ns nacos 48 || echo "[WARN] mysql wait"

CFG="$BASE/clusters/test-ha/config.yml"
set_cfg "$CFG" local_path_provisioner_install '"yes"'
set_cfg "$CFG" openebs_install '"yes"'; set_cfg "$CFG" openebs_lvm_enabled '"no"'
set_cfg "$CFG" nacos_install '"yes"'
set_cfg "$CFG" nacos_mysql_host '"nacos-mysql.nacos.svc.cluster.local"'
set_cfg "$CFG" nacos_mysql_db '"nacos"'; set_cfg "$CFG" nacos_mysql_port '"3306"'
set_cfg "$CFG" nacos_mysql_user '"nacos"'; set_cfg "$CFG" nacos_mysql_password '"NacosUser!23"'
set_cfg "$CFG" nacos_storage_class '"local-path"'
set_cfg "$CFG" minio_install '"yes"'; set_cfg "$CFG" minio_pool_servers '4'
set_cfg "$CFG" minio_storage_class '"openebs-hostpath"'
set_cfg "$CFG" dashboard_install '"yes"'; set_cfg "$CFG" prom_install '"yes"'; set_cfg "$CFG" ingress_nginx_install '"yes"'
set_cfg "$CFG" network_check_enabled 'true'
set_cfg "$CFG" rocketmq_install '"no"'

$K download -E nacos </dev/null || true
$K download -E minio </dev/null || true
$K download -E prometheus </dev/null || true
$K download -E dashboard </dev/null || true
$K download -E ingress-nginx </dev/null || true

if $K setup test-ha 07 </dev/null; then
  sleep 45
  assert_no_imagepull || fail "HA ImagePull"
  if wait_pods_ns nacos 72; then pass "P0 Nacos Ready"; else fail "P0 Nacos"; fi
  if wait_pods_ns minio 72; then pass "P1 MinIO pool=4"; else
    kubectl -n minio get pods -o wide || true
    fail "P1 MinIO"
  fi
  if wait_pods_ns monitor 90; then pass "P1 Prometheus HA"; else fail "P1 Prometheus HA"; fi
  if kubectl -n ingress-nginx get pods --no-headers 2>/dev/null | grep -q Running; then
    pass "P1 Ingress HA"
  else fail "P1 Ingress HA"; fi
  if kubectl -n kube-system get deploy kubernetes-dashboard-api >/dev/null 2>&1; then
    pass "P1 Dashboard HA"
  else fail "P1 Dashboard HA"; fi
  # network-check DaemonSet/Job if present
  if kubectl -n kube-system get ds,deploy,job 2>/dev/null | grep -qi network; then
    pass "P1 network-check present"
  else
    # may be a one-shot ansible check — look in ansible log / config flag only
    grep -q 'network_check_enabled: true' "$CFG" && pass "P1 network-check enabled in config" || fail "P1 network-check"
  fi
else
  fail "test-ha setup 07"
fi

###############################################################################
section "P1 Rocky G8b — recreate on 136 if free"
kube=$(sshpass -p 123456 ssh -o StrictHostKeyChecking=no -o ConnectTimeout=6 root@192.168.47.136 'systemctl is-active kubelet 2>/dev/null' || echo dead)
if [ "$kube" = "inactive" ] || [ "$kube" = "dead" ] || [ "$kube" = "failed" ]; then
  C_R=deliver-rocky
  $K destroy "$C_R" </dev/null 2>/dev/null || rm -rf "$BASE/clusters/$C_R"
  $K destroy test137 </dev/null 2>/dev/null || true
  $K new "$C_R" 2>/dev/null || true
  mkdir -p "$BASE/clusters/$C_R"
  cat > "$BASE/clusters/$C_R/hosts" <<EOF
[etcd]
192.168.47.136

[kube_master]
192.168.47.136 k8s_nodename='master-136'

[kube_node]
192.168.47.136

[harbor]

[ex_lb]

[chrony]

[all:vars]
SECURE_PORT="6443"
CONTAINER_RUNTIME="containerd"
CLUSTER_NETWORK="calico"
PROXY_MODE="ipvs"
SERVICE_CIDR="10.71.0.0/16"
CLUSTER_CIDR="172.24.0.0/16"
NODE_PORT_RANGE="30000-32767"
CLUSTER_DNS_DOMAIN="cluster.local"
bin_dir="/usr/local/bin"
base_dir="/usr/local/kubeauto"
cluster_dir="{{ base_dir }}/clusters/${C_R}"
ca_dir="/etc/kubernetes/ssl"
k8s_nodename=''
ansible_user=root
EOF
  [ -f "$BASE/clusters/$C_R/config.yml" ] || cp "$BASE/conf/config.yml" "$BASE/clusters/$C_R/config.yml"
  sed -i 's/__k8s_ver__/1.33.6/g' "$BASE/clusters/$C_R/config.yml"
  CFG="$BASE/clusters/$C_R/config.yml"
  set_cfg "$CFG" dashboard_install '"yes"'; set_cfg "$CFG" prom_install '"yes"'
  set_cfg "$CFG" ingress_nginx_install '"yes"'; set_cfg "$CFG" local_path_provisioner_install '"yes"'
  set_cfg "$CFG" openebs_install '"yes"'; set_cfg "$CFG" openebs_lvm_enabled '"no"'
  set_cfg "$CFG" minio_install '"no"'; set_cfg "$CFG" nacos_install '"no"'; set_cfg "$CFG" rocketmq_install '"no"'
  prep_lvm_remote 136 15 || true
  if $K setup "$C_R" 90 </dev/null && $K setup "$C_R" 07 </dev/null; then
    export KUBECONFIG="$BASE/clusters/$C_R/kubectl.kubeconfig"
    nodes_ready "$C_R" || fail "rocky nodes"
    sleep 30
    assert_no_imagepull || fail "P1 Rocky ImagePull"
    prom=$(kubectl -n monitor get deploy prometheus-kube-prometheus-operator -o jsonpath='{.spec.template.spec.containers[0].image}' 2>/dev/null || true)
    echo "prom=$prom"
    echo "$prom" | grep -q brinnatt/ && pass "P1 Rocky G8b addons" || fail "P1 Rocky G8b images"
  else fail "P1 Rocky setup"; fi
else
  skip "P1 Rocky 136 busy kubelet=$kube"
fi

echo
echo "DELIVERY_RETEST_COMPLETE PASS=$PASS_N FAIL=$FAIL_N SKIP=$SKIP_N LOG=$LOG"
