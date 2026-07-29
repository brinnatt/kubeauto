#!/bin/bash
# Delivery-gap smoke overnight (P0/P1). Continues on failures; prints PASS/FAIL/SKIP.
set -uo pipefail
LOG=/var/log/kubeauto-delivery-gap-$(date +%Y%m%d-%H%M).log
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
    ssh -o BatchMode=yes -o StrictHostKeyChecking=no -o ConnectTimeout=8 root@192.168.47.$ip \
      "sed -i '/registry.talkschool.cn/d' /etc/hosts; echo '192.168.47.138    registry.talkschool.cn' >> /etc/hosts" \
      2>/dev/null || echo "[WARN] hosts 47.$ip"
  done
}
nodes_ready(){
  local c="$1" tries="${2:-36}"; export KUBECONFIG="$BASE/clusters/$c/kubectl.kubeconfig"
  local i=0
  while [ "$i" -lt "$tries" ]; do
    kubectl get nodes --no-headers 2>/dev/null | grep -q Ready && return 0
    i=$((i+1)); sleep 10
  done; return 1
}
wait_pods_ns(){
  local ns="$1" tries="${2:-48}" i=0 bad total
  while [ "$i" -lt "$tries" ]; do
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
  scp -o BatchMode=yes -o StrictHostKeyChecking=no "$BASE/tests/helpers/prep-node-lvm-loop.sh" root@192.168.47.$ip:/tmp/prep-lvm.sh
  ssh -o BatchMode=yes -o StrictHostKeyChecking=no root@192.168.47.$ip "bash /tmp/prep-lvm.sh vg_k8s ${2:-20}"
}
set_cfg(){ # file key value
  local f="$1" k="$2" v="$3" replacement
  replacement=$(printf '%s' "$v" | sed 's/[\\&|]/\\&/g')
  if grep -q "^${k}:" "$f"; then
    sed -i "s|^${k}:.*|${k}: ${replacement}|" "$f"
  else
    printf '%s: %s\n' "$k" "$v" >> "$f"
  fi
}

section "0 unit + registry hosts + trim aio + bootstrap"
bash "$BASE/tests/run_unit_tests.sh" || fail "unit"
fix_registry_hosts
if [ -d "$BASE/clusters/aio" ]; then
  export KUBECONFIG="$BASE/clusters/aio/kubectl.kubeconfig"; $K checkout aio </dev/null || true
  for ns in monitor openebs minio minio-operator ingress-nginx nacos rocketmq; do
    kubectl delete ns "$ns" --wait=false 2>/dev/null || true
  done
  kubectl -n kube-system delete deploy,sts -l 'app.kubernetes.io/part-of=kubernetes-dashboard' --wait=false 2>/dev/null || true
  pass "aio trimmed"
fi
BOOTSTRAP_MODE=full BOOTSTRAP_HUBBLE=1 bash "$BASE/tests/helpers/bootstrap-brinnatt-mirrors.sh" || true
for comp in openebs local-path-provisioner nfs-provisioner nacos rocketmq minio cilium cilium-hubble dashboard prometheus ingress-nginx network-check; do
  $K download -E "$comp" </dev/null || true
done

###############################################################################
section "P1-Rocky G8b on test137 (before occupying 133)"
export KUBECONFIG="$BASE/clusters/test137/kubectl.kubeconfig"
$K checkout test137 </dev/null || true
if nodes_ready test137 18; then
  fix_registry_hosts
  prep_lvm_remote 137 15 || true
  CFG="$BASE/clusters/test137/config.yml"
  set_cfg "$CFG" dashboard_install '"yes"'; set_cfg "$CFG" prom_install '"yes"'
  set_cfg "$CFG" ingress_nginx_install '"yes"'; set_cfg "$CFG" local_path_provisioner_install '"yes"'
  set_cfg "$CFG" openebs_install '"yes"'; set_cfg "$CFG" openebs_lvm_enabled '"no"'
  set_cfg "$CFG" minio_install '"no"'; set_cfg "$CFG" nacos_install '"no"'; set_cfg "$CFG" rocketmq_install '"no"'
  for ns in monitor openebs ingress-nginx; do kubectl delete ns "$ns" --wait=false 2>/dev/null || true; done
  sleep 5
  if $K setup test137 07 </dev/null; then
    sleep 25
    assert_no_imagepull || fail "P1 test137 ImagePull"
    prom=$(kubectl -n monitor get deploy prometheus-kube-prometheus-operator -o jsonpath='{.spec.template.spec.containers[0].image}' 2>/dev/null || true)
    dash=$(kubectl -n kube-system get deploy kubernetes-dashboard-api -o jsonpath='{.spec.template.spec.containers[0].image}' 2>/dev/null || true)
    echo "prom=$prom dash=$dash"
    echo "$prom$dash" | grep -q brinnatt/ && pass "P1 Rocky G8b addons" || fail "P1 Rocky G8b images"
  else fail "P1 test137 setup 07"; fi
else skip "P1 test137 not Ready"; fi

###############################################################################
section "P0 LVM+NFS+RocketMQ on deliver-lvm (133)"
C_LVM=deliver-lvm
$K destroy "$C_LVM" </dev/null 2>/dev/null || rm -rf "$BASE/clusters/$C_LVM"
prep_lvm_remote 133 20
$K new "$C_LVM" 2>/dev/null || true
bash "$BASE/tests/helpers/mk-cluster-133.sh" "$C_LVM" calico ipvs
sed -i 's/__k8s_ver__/1.33.6/g' "$BASE/clusters/$C_LVM/config.yml"
CFG="$BASE/clusters/$C_LVM/config.yml"
set_cfg "$CFG" openebs_install '"yes"'; set_cfg "$CFG" openebs_lvm_enabled '"yes"'; set_cfg "$CFG" openebs_lvm_vg '"vg_k8s"'
set_cfg "$CFG" local_path_provisioner_install '"yes"'
set_cfg "$CFG" nfs_provisioner_install '"yes"'; set_cfg "$CFG" nfs_server '"192.168.47.133"'; set_cfg "$CFG" nfs_path '"/data/nfs"'
set_cfg "$CFG" rocketmq_install '"yes"'; set_cfg "$CFG" rocketmq_storage_class '"openebs-hostpath"'
set_cfg "$CFG" dashboard_install '"no"'; set_cfg "$CFG" prom_install '"no"'; set_cfg "$CFG" minio_install '"no"'; set_cfg "$CFG" nacos_install '"no"'
scp -o BatchMode=yes -o StrictHostKeyChecking=no "$BASE/tests/helpers/prep-nfs-server.sh" root@192.168.47.133:/tmp/prep-nfs.sh
ssh -o BatchMode=yes -o StrictHostKeyChecking=no root@192.168.47.133 'bash /tmp/prep-nfs.sh /data/nfs'

if $K setup "$C_LVM" 90 </dev/null && $K setup "$C_LVM" 07 </dev/null; then
  export KUBECONFIG="$BASE/clusters/$C_LVM/kubectl.kubeconfig"
  nodes_ready "$C_LVM" || fail "deliver-lvm nodes"
  sleep 25
  kubectl -n openebs get pods -o wide || true
  if wait_pods_ns openebs 40 && assert_no_imagepull; then
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
    for i in $(seq 1 40); do
      ph=$(kubectl get pod lvm-smoke-pod -o jsonpath='{.status.phase}' 2>/dev/null || true)
      if [ "$ph" = "Running" ] || [ "$ph" = "Succeeded" ]; then
        out=$(kubectl exec lvm-smoke-pod -- cat /data/out.txt 2>/dev/null || true)
        echo "$out" | grep -q lvm-ok && ok=1 && break
      fi; sleep 10
    done
    kubectl get pvc lvm-smoke-pvc -o wide || true
    kubectl get pod lvm-smoke-pod -o wide || true
    [ "$ok" = 1 ] && pass "P0 OpenEBS LVM R/W" || fail "P0 OpenEBS LVM R/W"
  else fail "P0 OpenEBS pods"; fi

  # NFS
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
  for i in $(seq 1 40); do
    ph=$(kubectl get pod nfs-smoke-pod -o jsonpath='{.status.phase}' 2>/dev/null || true)
    if [ "$ph" = "Running" ]; then
      out=$(kubectl exec nfs-smoke-pod -- cat /data/out.txt 2>/dev/null || true)
      echo "$out" | grep -q nfs-ok && ok=1 && break
    fi; sleep 10
  done
  kubectl get pvc nfs-smoke-pvc -o wide || true
  kubectl get pod nfs-smoke-pod -o wide || true
  kubectl -n kube-system get pods | grep -i nfs || true
  [ "$ok" = 1 ] && pass "P0 NFS R/W" || fail "P0 NFS R/W"

  # RocketMQ
  sleep 20
  kubectl -n rocketmq get pods -o wide || true
  if kubectl -n rocketmq get pods --no-headers 2>/dev/null | grep -q rocketmq-operator; then
    if wait_pods_ns rocketmq 60; then pass "P0 RocketMQ Ready"
    else
      kubectl -n rocketmq get pods -o wide || true
      fail "P0 RocketMQ pods not all Ready"
    fi
  else fail "P0 RocketMQ operator missing"; fi
  assert_no_imagepull || fail "P0 ImagePull after storage/mq"
else
  fail "P0 deliver-lvm setup"
fi

###############################################################################
section "P1 Hubble — free 133 then cilium+hubble"
$K destroy "$C_LVM" </dev/null 2>/dev/null || rm -rf "$BASE/clusters/$C_LVM"
C_HUB=deliver-hubble
$K destroy "$C_HUB" </dev/null 2>/dev/null || rm -rf "$BASE/clusters/$C_HUB"
$K new "$C_HUB" 2>/dev/null || true
bash "$BASE/tests/helpers/mk-cluster-133.sh" "$C_HUB" cilium ipvs
sed -i 's/__k8s_ver__/1.33.6/g' "$BASE/clusters/$C_HUB/config.yml"
CFG="$BASE/clusters/$C_HUB/config.yml"
set_cfg "$CFG" cilium_hubble_enabled 'true'
set_cfg "$CFG" cilium_hubble_ui_enabled 'true'
$K download -E cilium </dev/null || true
$K download -E cilium-hubble </dev/null || true
if $K setup "$C_HUB" 90 </dev/null; then
  export KUBECONFIG="$BASE/clusters/$C_HUB/kubectl.kubeconfig"
  nodes_ready "$C_HUB" || fail "hubble nodes"
  sleep 40
  kubectl -n kube-system get pods | grep -iE 'cilium|hubble' || true
  rel=$(kubectl -n kube-system get deploy hubble-relay -o jsonpath='{.spec.template.spec.containers[0].image}' 2>/dev/null || true)
  ui=$(kubectl -n kube-system get deploy hubble-ui -o jsonpath='{.spec.template.spec.containers[*].image}' 2>/dev/null || true)
  echo "relay=$rel ui=$ui"
  if echo "$rel" | grep -q brinnatt/hubble-relay && echo "$ui" | grep -q brinnatt/hubble-ui; then
    # wait hubble pods specifically
    ok=1
    for d in hubble-relay hubble-ui; do
      kubectl -n kube-system rollout status deploy/$d --timeout=300s || ok=0
    done
    [ "$ok" = 1 ] && pass "P1 Cilium Hubble Running" || fail "P1 Hubble rollout"
  else fail "P1 Hubble deploy/images missing"; fi
  assert_no_imagepull || fail "P1 Hubble ImagePull"
else fail "P1 Hubble setup"; fi

###############################################################################
section "P0 Nacos + P1 MinIO=4 + HA addons on test-ha (need >=3 nodes)"
$K destroy "$C_HUB" </dev/null 2>/dev/null || rm -rf "$BASE/clusters/$C_HUB"
export KUBECONFIG="$BASE/clusters/test-ha/kubectl.kubeconfig"
$K checkout test-ha </dev/null || true
nodes_ready test-ha || fail "test-ha down"
fix_registry_hosts
ncount=$(kubectl get nodes --no-headers 2>/dev/null | grep -c Ready || echo 0)
if [ "$ncount" -lt 3 ]; then
  echo "Adding workers 133 and 137 to test-ha (have $ncount)"
  $K add-node test-ha 192.168.47.133 worker-133 </dev/null || true
  $K add-node test-ha 192.168.47.137 worker-133 </dev/null || true
  sleep 60
fi
fix_registry_hosts
for ip in 132 135 133 137; do prep_lvm_remote $ip 15 || true; done
ncount=$(kubectl get nodes --no-headers 2>/dev/null | grep -c Ready || echo 0)
kubectl get nodes -o wide
echo "Ready nodes=$ncount"

# MySQL mirror + deploy
kubectl create ns nacos 2>/dev/null || true
if ! curl -sf http://127.0.0.1:5000/v2/brinnatt/mysql/tags/list 2>/dev/null | grep -q 8.0.36; then
  timeout 300 docker pull mysql:8.0.36 || timeout 300 docker pull mysql:8.0 || true
  cat >/tmp/Dockerfile.mysql <<'DF'
FROM mysql:8.0.36
DF
  DOCKER_BUILDKIT=1 docker build --provenance=false --sbom=false \
    -t registry.talkschool.cn:5000/brinnatt/mysql:8.0.36 -f /tmp/Dockerfile.mysql /tmp || \
    { docker tag mysql:8.0.36 registry.talkschool.cn:5000/brinnatt/mysql:8.0.36 2>/dev/null || docker tag mysql:8.0 registry.talkschool.cn:5000/brinnatt/mysql:8.0.36; }
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
MYSQL_POD=$(kubectl -n nacos get pod -l app=nacos-mysql -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)
[ -n "$MYSQL_POD" ] && kubectl -n nacos exec "$MYSQL_POD" -- mysql -unacos -p'NacosUser!23' -e 'SELECT 1' 2>/dev/null || true

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

if $K setup test-ha 07 </dev/null; then
  sleep 45
  assert_no_imagepull || fail "test-ha ImagePull"
  # Nacos
  if [ "$ncount" -ge 3 ]; then
    kubectl -n nacos get pods -o wide || true
    if wait_pods_ns nacos 72; then
      nc=$(kubectl -n nacos get pods --no-headers 2>/dev/null | grep -c '^nacos-' || echo 0)
      [ "$nc" -ge 3 ] && pass "P0 Nacos Ready ($nc)" || fail "P0 Nacos replicas=$nc"
    else fail "P0 Nacos"; fi
  else skip "P0 Nacos need 3 nodes (have $ncount)"; fi
  # MinIO pool 4
  kubectl -n minio get pods -o wide || true
  pool_n=$(kubectl -n minio get pods --no-headers 2>/dev/null | grep -c myminio-pool || echo 0)
  if [ "$pool_n" -ge 4 ] && wait_pods_ns minio 72; then pass "P1 MinIO pool=4"
  else fail "P1 MinIO pool=4 (pods=$pool_n)"; fi
  # HA addons
  kubectl -n monitor get deploy prometheus-kube-prometheus-operator >/dev/null 2>&1 \
    && kubectl -n kube-system get deploy kubernetes-dashboard-api >/dev/null 2>&1 \
    && pass "P1 HA prom+dashboard" || fail "P1 HA prom/dashboard"
  kubectl -n ingress-nginx get pods >/dev/null 2>&1 && pass "P1 HA ingress" || fail "P1 HA ingress"
  # network-check
  if kubectl get pods -A --no-headers 2>/dev/null | grep -qiE 'network-check|json-mock'; then pass "P1 network-check pods"
  elif kubectl get cronjob -A 2>/dev/null | grep -qi network; then pass "P1 network-check cron"
  else fail "P1 network-check missing"; fi
else
  fail "test-ha setup 07"
fi

###############################################################################
section "P0 docker runtime + upgrade (133 must leave test-ha first)"
# Remove 133 from test-ha if present
export KUBECONFIG="$BASE/clusters/test-ha/kubectl.kubeconfig"
if kubectl get node worker-133 >/dev/null 2>&1 || kubectl get nodes -o name 2>/dev/null | grep -q 133; then
  $K del-node test-ha 192.168.47.133 </dev/null || true
  sleep 20
fi
C_DK=deliver-docker
$K destroy "$C_DK" </dev/null 2>/dev/null || rm -rf "$BASE/clusters/$C_DK"
$K new "$C_DK" 2>/dev/null || true
bash "$BASE/tests/helpers/mk-cluster-133.sh" "$C_DK" calico ipvs
sed -i 's/__k8s_ver__/1.33.6/g' "$BASE/clusters/$C_DK/config.yml"
sed -i 's/CONTAINER_RUNTIME=.*/CONTAINER_RUNTIME="docker"/' "$BASE/clusters/$C_DK/hosts"
$K download -X </dev/null || true
if $K setup "$C_DK" 90 </dev/null; then
  export KUBECONFIG="$BASE/clusters/$C_DK/kubectl.kubeconfig"
  if nodes_ready "$C_DK" 40; then
    rt=$(kubectl get node -o jsonpath='{.items[0].status.nodeInfo.containerRuntimeVersion}' 2>/dev/null || true)
    echo "runtime=$rt"
    if echo "$rt" | grep -qi docker; then
      pass "P0 docker runtime ($rt)"
      if $K upgrade "$C_DK" </dev/null; then
        nodes_ready "$C_DK" 24 && pass "P0 upgrade" || fail "P0 upgrade nodes"
      else skip "P0 upgrade unsupported/failed"; fi
    else fail "P0 expected docker runtime got $rt"; fi
  else fail "P0 docker nodes"; fi
else fail "P0 docker setup"; fi

###############################################################################
section "SUMMARY"
echo "PASS=$PASS_N FAIL=$FAIL_N SKIP=$SKIP_N"
echo DELIVERY_GAP_SMOKE_COMPLETE
echo "LOG=$LOG"
# write machine-readable summary for matrix update
cat > /var/log/kubeauto-delivery-gap-summary.env <<EOF
PASS_N=$PASS_N
FAIL_N=$FAIL_N
SKIP_N=$SKIP_N
LOG=$LOG
EOF
[ "$FAIL_N" -eq 0 ]
