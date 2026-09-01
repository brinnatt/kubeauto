#!/usr/bin/env bash
# Dedicated Prometheus delivery gate. The control/registry host is not a
# Kubernetes node; the six-node cluster is disposable and branch-owned.
set -Eeuo pipefail

BASE="${KUBEAUTO_BASE:-/usr/local/kubeauto}"
CLUSTER="${PROM_CLUSTER:-prometheus-gate}"
NAMESPACE="${PROM_NAMESPACE:-monitor}"
K8S_VERSION="${PROM_K8S_VERSION:-1.33.6}"
CHART_VERSION="${PROM_CHART_VERSION:-88.0.0}"
MARKER="PROMETHEUS_FULL_GATE_PASS"
K="${BASE}/.venv/bin/kubecli"
[[ -x "$K" ]] || K="$(command -v kubecli)"
HELM="${BASE}/extra-bin/helm"
[[ -x "$HELM" ]] || HELM="$(command -v helm)"
KC="${BASE}/clusters/${CLUSTER}/kubectl.kubeconfig"
CHART="${BASE}/roles/cluster-addon/files/kube-prometheus-stack-${CHART_VERSION}.tgz"
VALUES="${BASE}/clusters/${CLUSTER}/yml/prom-values.yaml"
PORT_FORWARD_PIDS=()
LAB_NODES=(
  192.168.122.243 192.168.122.246 192.168.122.217
  192.168.122.210 192.168.122.216 192.168.122.193
)

pass() { echo "[PASS] $*"; }
fail() { echo "[FAIL] $*" >&2; exit 1; }

cleanup_processes() {
  local pid
  for pid in "${PORT_FORWARD_PIDS[@]:-}"; do
    [[ -n "$pid" ]] && kill "$pid" 2>/dev/null || true
  done
}

on_exit() {
  local rc=$?
  trap - EXIT
  cleanup_processes
  if [[ "$rc" -ne 0 ]]; then
    echo "========== PROMETHEUS FAILURE DIAGNOSTICS =========="
    kubectl --kubeconfig="$KC" get nodes,pods -A -o wide 2>&1 || true
    kubectl --kubeconfig="$KC" -n "$NAMESPACE" get \
      prometheus,alertmanager,prometheusrule,pdb,pvc,svc,endpoints -o wide 2>&1 || true
    kubectl --kubeconfig="$KC" -n "$NAMESPACE" get events \
      --sort-by=.lastTimestamp 2>&1 | tail -160 || true
    "$HELM" -n "$NAMESPACE" history prometheus --kubeconfig "$KC" 2>&1 || true
    for log in /tmp/prometheus-*-port-forward.log /tmp/prometheus-alert-mock.log; do
      [[ -f "$log" ]] && { echo "--- $log ---"; tail -80 "$log"; }
    done
  fi
  exit "$rc"
}
trap on_exit EXIT

wait_namespace_ready() {
  local tries=120 total ready
  while (( tries-- > 0 )); do
    read -r total ready < <(
      kubectl --kubeconfig="$KC" -n "$NAMESPACE" get pods -o json 2>/dev/null |
        jq -r '[.items[] | select(.metadata.deletionTimestamp == null) |
          select(.status.phase != "Succeeded" and .status.phase != "Failed")] as $pods |
          [$pods | length,
           [$pods[] | select(.status.phase == "Running") |
             select(all(.status.containerStatuses[]?; .ready == true))] | length] | @tsv' || echo '0 0'
    )
    if [[ "$total" -gt 0 && "$ready" -eq "$total" ]]; then
      echo "PROM_NAMESPACE_READY namespace=${NAMESPACE} ready=${ready}/${total}"
      return 0
    fi
    (( tries % 6 == 0 )) && echo "PROM_WAIT_HEARTBEAT ready=${ready:-0}/${total:-0} remaining=${tries}"
    sleep 10
  done
  return 1
}

start_port_forward() {
  local resource="$1" local_port="$2" remote_port="$3" name="$4" ready_path="${5:-/-/ready}"
  kubectl --kubeconfig="$KC" -n "$NAMESPACE" port-forward \
    "$resource" "${local_port}:${remote_port}" >"/tmp/prometheus-${name}-port-forward.log" 2>&1 &
  PF_PID=$!
  PORT_FORWARD_PIDS+=("$PF_PID")
  for pf_try in $(seq 1 40); do
    kill -0 "$PF_PID" 2>/dev/null || return 1
    curl -fsS --max-time 2 "http://127.0.0.1:${local_port}${ready_path}" >/dev/null 2>&1 && return 0
    sleep 1
  done
  return 1
}

stop_port_forward() {
  local pid="$1"
  kill "$pid" 2>/dev/null || true
  wait "$pid" 2>/dev/null || true
}

assert_distinct_nodes() {
  local selector="$1" expected="$2" label="$3" count distinct
  read -r count distinct < <(
    kubectl --kubeconfig="$KC" -n "$NAMESPACE" get pod -l "$selector" -o json |
      jq -r '[.items[] | select(.metadata.deletionTimestamp == null)] as $pods |
        [$pods | length, ([$pods[].spec.nodeName] | unique | length)] | @tsv'
  )
  [[ "$count" -eq "$expected" && "$distinct" -eq "$expected" ]] ||
    fail "${label} placement expected=${expected} pods/nodes got=${count}/${distinct}"
  pass "${label} hard anti-affinity pods=${count} distinct_nodes=${distinct}"
}

assert_endpoint_count() {
  local suffix="$1" expected="$2" count
  count="$(kubectl --kubeconfig="$KC" -n kube-system get endpoints -o json |
    jq --arg suffix "$suffix" '[.items[] | select(.metadata.name | endswith($suffix)) |
      .subsets[]?.addresses[]?] | length')"
  [[ "$count" -eq "$expected" ]] ||
    fail "endpoint ${suffix} expected=${expected} got=${count}"
  pass "endpoint ${suffix} addresses=${count}"
}

wait_targets_healthy() {
  local url="$1" tries=60 bad=0 total=0
  while (( tries-- > 0 )); do
    if curl -fsS --max-time 10 "${url}/api/v1/targets" -o /tmp/prometheus-targets.json; then
      read -r total bad < <(jq -r '[ (.data.activeTargets | length),
        ([.data.activeTargets[] | select(.health != "up" or .lastError != "")] | length) ] | @tsv' \
        /tmp/prometheus-targets.json)
      if [[ "$total" -gt 0 && "$bad" -eq 0 ]]; then
        pass "Prometheus targets healthy=${total}/${total}"
        return 0
      fi
    fi
    (( tries % 6 == 0 )) && echo "PROM_TARGET_HEARTBEAT healthy=$((total-bad))/${total} remaining=${tries}"
    sleep 10
  done
  jq -r '.data.activeTargets[] | select(.health != "up" or .lastError != "") |
    [.labels.job, .scrapeUrl, .health, .lastError] | @tsv' /tmp/prometheus-targets.json 2>/dev/null || true
  return 1
}

echo "PROM_STAGE_BEGIN id=capacity-preflight"
for node in "${LAB_NODES[@]}"; do
  read -r os_id cpu mem_kib free_kib < <(
    ssh -o BatchMode=yes -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
      -o ConnectTimeout=10 "root@${node}" '
        . /etc/os-release
        printf "%s %s %s %s\n" "$ID" "$(nproc)" \
          "$(awk "/MemTotal/ {print \$2}" /proc/meminfo)" \
          "$(df -Pk / | awk "NR == 2 {print \$4}")"
      '
  )
  [[ "$os_id" == rocky && "$cpu" -ge 8 && "$mem_kib" -ge 14680064 && "$free_kib" -ge 41943040 ]] ||
    fail "lab capacity node=${node} os=${os_id} cpu=${cpu} mem_kib=${mem_kib} free_kib=${free_kib}"
  echo "PROM_LAB_CAPACITY_OK node=${node} os=${os_id} cpu=${cpu} memory_gib=$((mem_kib/1024/1024)) root_free_gib=$((free_kib/1024/1024))"
done

mkdir -p "${BASE}/clusters"
rm -rf "${BASE}/clusters/${CLUSTER}"
rm -f "${BASE}/clusters/${CLUSTER}.hosts"
cat > "${BASE}/clusters/${CLUSTER}.hosts" <<'EOF'
[etcd]
192.168.122.243 k8s_nodename='prom-master-243'
192.168.122.246 k8s_nodename='prom-master-246'
192.168.122.217 k8s_nodename='prom-master-217'
[kube_master]
192.168.122.243 k8s_nodename='prom-master-243'
192.168.122.246 k8s_nodename='prom-master-246'
192.168.122.217 k8s_nodename='prom-master-217'
[kube_node]
192.168.122.210 k8s_nodename='prom-node-210'
192.168.122.216 k8s_nodename='prom-node-216'
192.168.122.193 k8s_nodename='prom-node-193'
[all:vars]
SECURE_PORT="6443"
CONTAINER_RUNTIME="containerd"
CLUSTER_NETWORK="calico"
PROXY_MODE="ipvs"
SERVICE_CIDR="10.82.0.0/16"
CLUSTER_CIDR="172.29.0.0/16"
NODE_PORT_RANGE="30000-32767"
CLUSTER_DNS_DOMAIN="cluster.local"
bin_dir="/usr/local/bin"
base_dir="/usr/local/kubeauto"
cluster_dir="{{ base_dir }}/clusters/prometheus-gate"
ca_dir="/etc/kubernetes/ssl"
k8s_nodename=''
ansible_user=root
EOF

"$K" new "$CLUSTER" </dev/null
cp "${BASE}/clusters/${CLUSTER}.hosts" "${BASE}/clusters/${CLUSTER}/hosts"
CFG="${BASE}/clusters/${CLUSTER}/config.yml"
sed -i "s/__k8s_ver__/${K8S_VERSION}/g; s/^KUBE_RESERVED_ENABLED: \"yes\"/KUBE_RESERVED_ENABLED: \"no\"/; s/^SYS_RESERVED_ENABLED: \"yes\"/SYS_RESERVED_ENABLED: \"no\"/" "$CFG"
sed -i 's|^local_path_provisioner_install:.*|local_path_provisioner_install: "yes"|' "$CFG"
sed -i 's|^prom_install:.*|prom_install: "yes"|' "$CFG"
sed -i 's|^prom_namespace:.*|prom_namespace: "monitor"|' "$CFG"
sed -i 's|^prom_storage_class:.*|prom_storage_class: "local-path"|' "$CFG"
sed -i 's|^prom_chart_ver:.*|prom_chart_ver: "88.0.0"|' "$CFG"

echo "PROM_STAGE_BEGIN id=artifact-stage"
"$K" download -D </dev/null
"$K" download -E local-path-provisioner </dev/null
"$K" download -E prometheus </dev/null
"$K" download -E network-check </dev/null

echo "PROM_STAGE_BEGIN id=cluster-install"
"$K" setup "$CLUSTER" 90 </dev/null
export KUBECONFIG="$KC"
kubectl --kubeconfig="$KC" wait --for=condition=Ready nodes --all --timeout=600s
[[ "$(kubectl --kubeconfig="$KC" get nodes -o json | jq '.items | length')" -eq 6 ]] ||
  fail "expected six Kubernetes nodes"

echo "PROM_STAGE_BEGIN id=addon-install"
"$K" setup "$CLUSTER" 07 </dev/null
wait_namespace_ready || fail "Prometheus namespace did not become ready"

echo "PROM_STAGE_BEGIN id=helm-render"
"$HELM" lint "$CHART" -f "$VALUES"
"$HELM" template prometheus "$CHART" -n "$NAMESPACE" -f "$VALUES" \
  --include-crds --kube-version "$K8S_VERSION" >/tmp/prometheus-rendered.yaml
rendered_images="$(awk '$1 == "image:" {gsub(/^"|"$/, "", $2); print $2}' \
  /tmp/prometheus-rendered.yaml | sort -u)"
[[ -n "$rendered_images" ]] || fail "Helm render produced no images"
nonlocal_images="$(grep -v '^registry\.talkschool\.cn:5000/' <<<"$rendered_images" || true)"
[[ -z "$nonlocal_images" ]] || {
  echo "$nonlocal_images"
  fail "Helm render contains images outside the delivery registry"
}
pass "Helm lint/template and final image registry contract"

chart_deployed="$($HELM -n "$NAMESPACE" list --kubeconfig "$KC" -o json |
  jq -r '.[] | select(.name == "prometheus") | .chart')"
[[ "$chart_deployed" == "kube-prometheus-stack-${CHART_VERSION}" ]] ||
  fail "deployed chart mismatch: ${chart_deployed:-missing}"
baseline_revision="$($HELM -n "$NAMESPACE" history prometheus --kubeconfig "$KC" -o json |
  jq -r '[.[] | select(.status == "deployed")][-1].revision')"
[[ "$baseline_revision" == 1 ]] || fail "fresh install expected Helm revision 1, got $baseline_revision"

prom_replicas="$(kubectl --kubeconfig="$KC" -n "$NAMESPACE" get prometheus \
  -o jsonpath='{.items[0].spec.replicas}')"
[[ "$prom_replicas" == 2 ]] || fail "expected Prometheus replicas=2, got ${prom_replicas:-missing}"
am_replicas="$(kubectl --kubeconfig="$KC" -n "$NAMESPACE" get alertmanager \
  -o jsonpath='{.items[0].spec.replicas}')"
[[ "$am_replicas" == 3 ]] || fail "expected Alertmanager replicas=3, got ${am_replicas:-missing}"
for resource in prometheus alertmanager; do
  available="$(kubectl --kubeconfig="$KC" -n "$NAMESPACE" get "$resource" -o json |
    jq -r '.items[0].status.conditions[]? | select(.type == "Available") | .status')"
  [[ "$available" == True ]] || fail "${resource} Available condition=${available:-missing}"
done
assert_distinct_nodes 'app.kubernetes.io/name=prometheus' 2 Prometheus
assert_distinct_nodes 'app.kubernetes.io/name=alertmanager' 3 Alertmanager
assert_distinct_nodes 'app=kube-prometheus-stack-operator-webhook' 2 admission-webhook

pvc_total="$(kubectl --kubeconfig="$KC" -n "$NAMESPACE" get pvc -o json | jq '.items | length')"
pvc_bound="$(kubectl --kubeconfig="$KC" -n "$NAMESPACE" get pvc -o json |
  jq '[.items[] | select(.status.phase == "Bound")] | length')"
[[ "$pvc_total" -eq 6 && "$pvc_bound" -eq 6 ]] ||
  fail "expected six Bound Prometheus/Alertmanager/Grafana PVCs, got ${pvc_bound}/${pvc_total}"
pvc_uids_before="$(kubectl --kubeconfig="$KC" -n "$NAMESPACE" get pvc -o json |
  jq -r '.items | sort_by(.metadata.name) | map(.metadata.name + "=" + .metadata.uid) | join(",")')"

for pdb_suffix in prometheus alertmanager operator-webhook; do
  pdb_available="$(kubectl --kubeconfig="$KC" -n "$NAMESPACE" get pdb -o json |
    jq --arg suffix "$pdb_suffix" '[.items[] | select(.metadata.name | endswith($suffix))] | length')"
  [[ "$pdb_available" -eq 1 ]] || fail "PDB missing or duplicated suffix=${pdb_suffix}"
done
pass "HA/PDB/PVC contract replicas=2/3 pvc_bound=6"

required_images=(
  'brinnatt/prometheus:v3.13.1-distroless'
  'brinnatt/alertmanager:v0.33.1'
  'brinnatt/grafana:13.1.1'
  'brinnatt/kube-state-metrics:v2.18.0'
  'brinnatt/node-exporter:v1.12.1'
  'brinnatt/prometheus-operator:v0.93.0'
  'brinnatt/prometheus-config-reloader:v0.93.0'
  'brinnatt/prometheus-admission-webhook:v0.93.0'
)
running_images="$(kubectl --kubeconfig="$KC" -n "$NAMESPACE" get pod -o json |
  jq -r '.items[].spec | (.initContainers[]?.image, .containers[]?.image)' | sort -u)"
for image in "${required_images[@]}"; do
  grep -Fq "/${image}" <<<"$running_images" || fail "required runtime image missing: $image"
done
pass "runtime image pins verified count=${#required_images[@]}"

echo "PROM_STAGE_BEGIN id=prometheus-api"
start_port_forward svc/prometheus-operated 19090 9090 prometheus || fail "Prometheus port-forward failed"
PROM_PF_PID="$PF_PID"
PROM_URL=http://127.0.0.1:19090
wait_targets_healthy "$PROM_URL" || fail "Prometheus has unhealthy targets"

for job_pattern in 'apiserver' 'etcd' 'kubelet' 'coredns' 'node-exporter' \
  'kube-state-metrics' 'kube-controller-manager' 'kube-scheduler' 'kube-proxy'; do
  jq -e --arg pattern "$job_pattern" '[.data.activeTargets[].labels.job |
    select(test($pattern; "i"))] | length > 0' /tmp/prometheus-targets.json >/dev/null ||
    fail "required scrape job missing: ${job_pattern}"
done
assert_endpoint_count kube-controller-manager 3
assert_endpoint_count kube-scheduler 3
assert_endpoint_count kube-proxy 6

rules_ready=0
for rule_try in $(seq 1 30); do
  if curl -fsS --max-time 10 "$PROM_URL/api/v1/rules" -o /tmp/prometheus-rules.json &&
    jq -e '.status == "success" and
      ([.data.groups[].rules[]] | length > 0) and
      (all(.data.groups[].rules[]; .health == "ok" and (.lastError // "") == ""))' \
      /tmp/prometheus-rules.json >/dev/null; then
    rules_ready=1
    break
  fi
  (( rule_try % 6 == 0 )) && echo "PROM_RULE_HEARTBEAT remaining=$((30 - rule_try))"
  sleep 10
done
[[ "$rules_ready" -eq 1 ]] || fail "Prometheus rule health failed"

for alert_try in $(seq 1 30); do
  curl -fsS --max-time 10 "$PROM_URL/api/v1/alerts" -o /tmp/prometheus-alerts.json
  bad_alerts="$(jq '[.data.alerts[] | select(.state == "firing" or .state == "pending") |
    select(.labels.alertname != "Watchdog" and .labels.alertname != "InfoInhibitor")] | length' \
    /tmp/prometheus-alerts.json)"
  [[ "$bad_alerts" -eq 0 ]] && break
  sleep 10
done
[[ "$bad_alerts" -eq 0 ]] || {
  jq -r '.data.alerts[] | select(.state == "firing" or .state == "pending") |
    [.labels.alertname, .labels.severity, .state] | @tsv' /tmp/prometheus-alerts.json
  fail "unexpected default alerts remain"
}
pass "targets/rules/default-alert health verified"

echo "PROM_STAGE_BEGIN id=grafana-api"
start_port_forward svc/prometheus-grafana 19093 80 grafana /api/health || fail "Grafana port-forward failed"
GRAFANA_PF_PID="$PF_PID"
GRAFANA_URL=http://127.0.0.1:19093
grafana_user="$(kubectl --kubeconfig="$KC" -n "$NAMESPACE" get secret grafana-admin \
  -o jsonpath='{.data.admin-user}' | base64 -d)"
grafana_password="$(kubectl --kubeconfig="$KC" -n "$NAMESPACE" get secret grafana-admin \
  -o jsonpath='{.data.admin-password}' | base64 -d)"
[[ "$grafana_user" == admin && ${#grafana_password} -ge 32 ]] || fail "Grafana admin Secret invalid"
curl -fsS --max-time 10 "$GRAFANA_URL/api/health" |
  jq -e '.database == "ok"' >/dev/null || fail "Grafana health failed"
curl -fsS --max-time 10 -u "$grafana_user:$grafana_password" "$GRAFANA_URL/api/datasources" |
  jq -e 'any(.[]; .type == "prometheus" and (.url | test("prometheus")))' >/dev/null ||
  fail "Grafana Prometheus datasource missing"
dashboard_count="$(curl -fsS --max-time 10 -u "$grafana_user:$grafana_password" \
  "$GRAFANA_URL/api/search?type=dash-db" | jq 'length')"
[[ "$dashboard_count" -gt 0 ]] || fail "Grafana built-in dashboards missing"
pass "Grafana login/datasource/dashboards count=${dashboard_count}"

echo "PROM_STAGE_BEGIN id=admission-and-failure-recovery"
cat >/tmp/prometheus-invalid-rule.yaml <<'EOF'
apiVersion: monitoring.coreos.com/v1
kind: PrometheusRule
metadata:
  name: delivery-invalid
  namespace: monitor
  labels:
    release: prometheus
spec:
  groups:
    - name: delivery-invalid
      rules:
        - alert: DeliveryInvalid
          expr: sum(
EOF
if kubectl --kubeconfig="$KC" apply -f /tmp/prometheus-invalid-rule.yaml >/tmp/prometheus-invalid.out 2>&1; then
  kubectl --kubeconfig="$KC" -n "$NAMESPACE" delete prometheusrule delivery-invalid --ignore-not-found
  fail "admission webhook accepted an invalid PrometheusRule"
fi
kubectl --kubeconfig="$KC" -n "$NAMESPACE" get deploy \
  prometheus-kube-prometheus-operator-webhook -o json |
  jq -e '.status.readyReplicas == 2' >/dev/null || fail "admission webhook HA deployment not ready"

if "$HELM" upgrade prometheus "$CHART" -n "$NAMESPACE" --kubeconfig "$KC" \
  -f "$VALUES" --set prometheusOperator.image.tag=delivery-missing \
  --atomic --timeout 90s >/tmp/prometheus-atomic-failure.log 2>&1; then
  fail "expected atomic bad-image upgrade to fail"
fi
wait_namespace_ready || fail "stack did not recover after atomic failed upgrade"
stable_revision="$($HELM -n "$NAMESPACE" history prometheus --kubeconfig "$KC" -o json |
  jq -r '[.[] | select(.status == "deployed")][-1].revision')"
[[ -n "$stable_revision" && "$stable_revision" -gt "$baseline_revision" ]] ||
  fail "atomic failure did not produce a deployed recovery revision"
pass "admission rejection and atomic failed-upgrade recovery revision=${stable_revision}"

echo "PROM_STAGE_BEGIN id=alertmanager-notification"
cat >/tmp/prometheus-alert-mock.yaml <<'EOF'
apiVersion: v1
kind: ConfigMap
metadata:
  name: delivery-alert-mock
  namespace: monitor
data:
  db.json: '{"alerts":[]}'
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: delivery-alert-mock
  namespace: monitor
spec:
  replicas: 1
  selector:
    matchLabels: {app: delivery-alert-mock}
  template:
    metadata:
      labels: {app: delivery-alert-mock}
    spec:
      containers:
        - name: mock
          image: registry.talkschool.cn:5000/brinnatt/json-mock:v1.3.1
          imagePullPolicy: IfNotPresent
          command: ["bash", "-c"]
          args: ["cp /seed/db.json /data/db.json && exec bash /run.sh"]
          env:
            - {name: PORT, value: "8080"}
            - {name: FILE, value: /data/db.json}
          ports:
            - {name: http, containerPort: 8080}
          readinessProbe:
            httpGet: {path: /alerts, port: http}
          volumeMounts:
            - {name: seed, mountPath: /seed, readOnly: true}
            - {name: data, mountPath: /data}
      volumes:
        - name: seed
          configMap: {name: delivery-alert-mock}
        - name: data
          emptyDir: {}
---
apiVersion: v1
kind: Service
metadata:
  name: delivery-alert-mock
  namespace: monitor
spec:
  selector: {app: delivery-alert-mock}
  ports:
    - {name: http, port: 8080, targetPort: http}
EOF
kubectl --kubeconfig="$KC" apply -f /tmp/prometheus-alert-mock.yaml
kubectl --kubeconfig="$KC" -n "$NAMESPACE" rollout status \
  deployment/delivery-alert-mock --timeout=300s

cat >/tmp/prometheus-alert-values.yaml <<EOF
alertmanager:
  config:
    global:
      resolve_timeout: 5s
    inhibit_rules:
      - source_matchers: ['severity = critical']
        target_matchers: ['severity = warning']
        equal: ['delivery_id']
    route:
      group_by: ['delivery_id', 'severity']
      group_wait: 2s
      group_interval: 2s
      repeat_interval: 1h
      receiver: delivery-mock
      routes: []
    receivers:
      - name: delivery-mock
        webhook_configs:
          - url: http://delivery-alert-mock.monitor.svc.cluster.local:8080/alerts
            send_resolved: true
EOF
"$HELM" upgrade prometheus "$CHART" -n "$NAMESPACE" --kubeconfig "$KC" \
  -f "$VALUES" -f /tmp/prometheus-alert-values.yaml --wait --timeout 20m
mock_revision="$($HELM -n "$NAMESPACE" history prometheus --kubeconfig "$KC" -o json |
  jq -r '[.[] | select(.status == "deployed")][-1].revision')"
[[ "$mock_revision" -gt "$stable_revision" ]] || fail "Alertmanager config upgrade revision did not advance"
wait_namespace_ready || fail "stack not ready after Alertmanager config upgrade"

cat >/tmp/prometheus-delivery-rule.yaml <<'EOF'
apiVersion: monitoring.coreos.com/v1
kind: PrometheusRule
metadata:
  name: delivery-notification
  namespace: monitor
  labels:
    release: prometheus
spec:
  groups:
    - name: delivery-notification
      interval: 5s
      rules:
        - alert: DeliveryCriticalA
          expr: vector(1)
          for: 0s
          labels: {severity: critical, delivery_id: prometheus-gate}
          annotations: {summary: delivery critical A}
        - alert: DeliveryCriticalB
          expr: vector(1)
          for: 0s
          labels: {severity: critical, delivery_id: prometheus-gate}
          annotations: {summary: delivery critical B}
EOF
cat >/tmp/prometheus-delivery-warning-rule.yaml <<'EOF'
apiVersion: monitoring.coreos.com/v1
kind: PrometheusRule
metadata:
  name: delivery-notification-warning
  namespace: monitor
  labels:
    release: prometheus
spec:
  groups:
    - name: delivery-notification-warning
      interval: 5s
      rules:
        - alert: DeliveryWarning
          expr: vector(1)
          for: 0s
          labels: {severity: warning, delivery_id: prometheus-gate}
          annotations: {summary: delivery warning}
EOF
kubectl --kubeconfig="$KC" apply -f /tmp/prometheus-delivery-rule.yaml \
  -f /tmp/prometheus-delivery-warning-rule.yaml

start_port_forward svc/alertmanager-operated 19092 9093 alertmanager || fail "Alertmanager port-forward failed"
AM_PF_PID="$PF_PID"
AM_URL=http://127.0.0.1:19092
start_port_forward svc/delivery-alert-mock 19094 8080 alert-mock /alerts || fail "alert mock port-forward failed"
MOCK_PF_PID="$PF_PID"
MOCK_URL=http://127.0.0.1:19094/alerts
curl -fsS --max-time 10 "$AM_URL/api/v2/status" -o /tmp/alertmanager-status.json
jq -e '.cluster.status == "ready" and (.cluster.peers | length >= 2)' \
  /tmp/alertmanager-status.json >/dev/null || fail "Alertmanager HA cluster status failed"
for am_alert_try in $(seq 1 60); do
  curl -fsS --max-time 10 "$AM_URL/api/v2/alerts" -o /tmp/alertmanager-alerts.json
  critical="$(jq '[.[] | select(.labels.delivery_id == "prometheus-gate" and
    .labels.severity == "critical")] | length' /tmp/alertmanager-alerts.json)"
  inhibited="$(jq '[.[] | select(.labels.alertname == "DeliveryWarning" and
    (.status.inhibitedBy | length > 0))] | length' /tmp/alertmanager-alerts.json)"
  [[ "$critical" -eq 2 && "$inhibited" -eq 1 ]] && break
  sleep 2
done
[[ "$critical" -eq 2 && "$inhibited" -eq 1 ]] || fail "Alertmanager grouping/inhibition state failed"

for firing_try in $(seq 1 60); do
  if curl -fsS --max-time 10 "$MOCK_URL" -o /tmp/prometheus-alert-webhook.json &&
    jq -e 'any(.[]; .status == "firing" and
    ([.alerts[] | select(.labels.delivery_id == "prometheus-gate" and
      .labels.severity == "critical")] | length == 2) and
    ([.alerts[] | select(.labels.severity == "warning")] | length == 0))' \
      /tmp/prometheus-alert-webhook.json >/dev/null 2>&1; then
    break
  fi
  sleep 2
done
jq -e 'any(.[]; .status == "firing" and
  ([.alerts[] | select(.labels.delivery_id == "prometheus-gate" and
    .labels.severity == "critical")] | length == 2))' \
  /tmp/prometheus-alert-webhook.json >/dev/null ||
  fail "Alertmanager firing notification payload missing"

sed 's/expr: vector(1)/expr: vector(0) > 0/g' /tmp/prometheus-delivery-rule.yaml \
  >/tmp/prometheus-delivery-rule-resolved.yaml
kubectl --kubeconfig="$KC" apply -f /tmp/prometheus-delivery-rule-resolved.yaml
critical_remaining=2
for resolved_try in $(seq 1 120); do
  if curl -fsS --max-time 10 "$PROM_URL/api/v1/alerts" -o /tmp/prometheus-alerts-resolved.json; then
    critical_remaining="$(jq '[.data.alerts[]? | select(.labels.delivery_id == "prometheus-gate" and
      .labels.severity == "critical" and .state == "firing")] | length' \
      /tmp/prometheus-alerts-resolved.json)"
  fi
  [[ "$critical_remaining" -eq 0 ]] && break
  sleep 2
done
[[ "$critical_remaining" -eq 0 ]] || fail "Prometheus critical alerts did not resolve after rule update"

for resolved_try in $(seq 1 90); do
  if curl -fsS --max-time 10 "$MOCK_URL" -o /tmp/prometheus-alert-webhook.json &&
    jq -e 'any(.[]; .status == "resolved" and
      any(.alerts[]; .labels.delivery_id == "prometheus-gate"))' \
      /tmp/prometheus-alert-webhook.json >/dev/null 2>&1; then
    break
  fi
  sleep 2
done
jq -e 'any(.[]; .status == "resolved" and
  any(.alerts[]; .labels.delivery_id == "prometheus-gate"))' \
  /tmp/prometheus-alert-webhook.json >/dev/null ||
  fail "Alertmanager resolved notification payload missing"
pass "Alertmanager HA/grouping/inhibition/firing/resolved webhook chain"

kubectl --kubeconfig="$KC" -n "$NAMESPACE" delete prometheusrule \
  delivery-notification delivery-notification-warning --wait=true

stop_port_forward "$MOCK_PF_PID"
kubectl --kubeconfig="$KC" delete -f /tmp/prometheus-alert-mock.yaml --wait=true

"$HELM" rollback prometheus "$stable_revision" -n "$NAMESPACE" --kubeconfig "$KC" \
  --wait --timeout 20m
rollback_revision="$($HELM -n "$NAMESPACE" history prometheus --kubeconfig "$KC" -o json |
  jq -r '[.[] | select(.status == "deployed")][-1].revision')"
[[ "$rollback_revision" -gt "$mock_revision" ]] || fail "Helm rollback revision did not advance"
wait_namespace_ready || fail "stack not ready after Helm rollback"
pass "Helm controlled upgrade/rollback revision=${rollback_revision}"

echo "PROM_STAGE_BEGIN id=idempotency-and-persistence"
grafana_secret_before="$(kubectl --kubeconfig="$KC" -n "$NAMESPACE" get secret grafana-admin \
  -o jsonpath='{.data.admin-password}')"
revision_before_repeat="$rollback_revision"
"$K" setup "$CLUSTER" 07 </dev/null
revision_after_repeat="$($HELM -n "$NAMESPACE" history prometheus --kubeconfig "$KC" -o json |
  jq -r '[.[] | select(.status == "deployed")][-1].revision')"
grafana_secret_after="$(kubectl --kubeconfig="$KC" -n "$NAMESPACE" get secret grafana-admin \
  -o jsonpath='{.data.admin-password}')"
[[ "$revision_after_repeat" == "$revision_before_repeat" ]] || fail "idempotent addon rerun changed Helm revision"
[[ "$grafana_secret_after" == "$grafana_secret_before" ]] || fail "idempotent addon rerun rotated Grafana password"

prom_pod="$(kubectl --kubeconfig="$KC" -n "$NAMESPACE" get pod \
  -l app.kubernetes.io/name=prometheus -o json | jq -r '.items | sort_by(.metadata.name) | .[0].metadata.name')"
prom_pod_uid_before="$(kubectl --kubeconfig="$KC" -n "$NAMESPACE" get pod "$prom_pod" \
  -o jsonpath='{.metadata.uid}')"
stop_port_forward "$PROM_PF_PID"
start_port_forward "pod/${prom_pod}" 19091 9090 prometheus-pod || fail "Prometheus pod port-forward failed"
POD_PF_PID="$PF_PID"
POD_PROM_URL=http://127.0.0.1:19091
curl -fsS --max-time 10 -G "$POD_PROM_URL/api/v1/query" \
  --data-urlencode 'query=prometheus_build_info' -o /tmp/prometheus-before-restart.json
sample_ts="$(jq -r '.data.result[0].value[0]' /tmp/prometheus-before-restart.json)"
[[ "$sample_ts" != null && -n "$sample_ts" ]] || fail "pre-restart Prometheus sample missing"
stop_port_forward "$POD_PF_PID"

kubectl --kubeconfig="$KC" -n "$NAMESPACE" delete pod "$prom_pod" --wait=true --timeout=180s
kubectl --kubeconfig="$KC" -n "$NAMESPACE" wait --for=condition=Ready "pod/${prom_pod}" --timeout=600s
prom_pod_uid_after="$(kubectl --kubeconfig="$KC" -n "$NAMESPACE" get pod "$prom_pod" \
  -o jsonpath='{.metadata.uid}')"
[[ "$prom_pod_uid_after" != "$prom_pod_uid_before" ]] || fail "Prometheus pod UID did not change"
pvc_uids_after="$(kubectl --kubeconfig="$KC" -n "$NAMESPACE" get pvc -o json |
  jq -r '.items | sort_by(.metadata.name) | map(.metadata.name + "=" + .metadata.uid) | join(",")')"
[[ "$pvc_uids_after" == "$pvc_uids_before" ]] || fail "PVC identity changed after Prometheus pod restart"

start_port_forward "pod/${prom_pod}" 19091 9090 prometheus-pod-after || fail "recreated Prometheus port-forward failed"
POD_PF_PID="$PF_PID"
query_start="$(awk -v ts="$sample_ts" 'BEGIN {printf "%.0f", ts-60}')"
query_end="$(date +%s)"
curl -fsS --max-time 10 -G "$POD_PROM_URL/api/v1/query_range" \
  --data-urlencode 'query=prometheus_build_info' \
  --data-urlencode "start=${query_start}" --data-urlencode "end=${query_end}" \
  --data-urlencode 'step=15s' -o /tmp/prometheus-after-restart.json
jq -e --argjson before "$sample_ts" '.status == "success" and
  any(.data.result[]?.values[]?; .[0] <= ($before + 30))' /tmp/prometheus-after-restart.json >/dev/null ||
  fail "historical Prometheus samples missing after pod/PVC restart"
stop_port_forward "$POD_PF_PID"
pass "idempotency, Grafana Secret, PVC UID and TSDB history preserved"

echo "PROM_STAGE_BEGIN id=promql-performance"
start_port_forward svc/prometheus-operated 19090 9090 prometheus-performance || fail "Prometheus performance port-forward failed"
PROM_PF_PID="$PF_PID"
rm -f /tmp/prometheus-performance-results
export PROM_URL
performance_start="$(date +%s)"
seq 1 200 | xargs -P 20 -I '{}' bash -c '
  curl -fsS --max-time 5 -G "$PROM_URL/api/v1/query" \
    --data-urlencode "query=sum(rate(prometheus_http_requests_total[5m]))" >/dev/null && echo ok
' >>/tmp/prometheus-performance-results
performance_seconds="$(( $(date +%s) - performance_start ))"
performance_ok="$(grep -cx ok /tmp/prometheus-performance-results || true)"
[[ "$performance_ok" -eq 200 && "$performance_seconds" -le 60 ]] ||
  fail "PromQL performance failed ok=${performance_ok}/200 seconds=${performance_seconds}"
pass "PromQL concurrency=20 requests=200 errors=0 seconds=${performance_seconds}"
stop_port_forward "$PROM_PF_PID"

echo "PROM_STAGE_BEGIN id=destroy"
"$K" destroy "$CLUSTER" </dev/null
rm -rf "${BASE}/clusters/${CLUSTER}"
rm -f "${BASE}/clusters/${CLUSTER}.hosts"
pass "${MARKER} chart=${CHART_VERSION} prometheus_replicas=${prom_replicas} alertmanager_replicas=${am_replicas} pvc_bound=${pvc_bound}"
echo "$MARKER"
