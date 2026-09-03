#!/usr/bin/env bash
# Dedicated Prometheus delivery gate. The control/registry host is not a
# Kubernetes node; the six-node cluster is disposable and branch-owned.
set -Eeuo pipefail

BASE="${KUBEAUTO_BASE:-/usr/local/kubeauto}"
CLUSTER="${PROM_CLUSTER:-prometheus-gate}"
NAMESPACE="${PROM_NAMESPACE:-monitor}"
K8S_VERSION="${PROM_K8S_VERSION:-1.33.6}"
CHART_VERSION="${PROM_CHART_VERSION:-88.0.0}"
BASELINE_CHART_VERSION="${PROM_BASELINE_CHART_VERSION:-75.7.0}"
MARKER="PROMETHEUS_FULL_GATE_PASS"
K="${BASE}/.venv/bin/kubecli"
[[ -x "$K" ]] || K="$(command -v kubecli)"
HELM="${BASE}/extra-bin/helm"
[[ -x "$HELM" ]] || HELM="$(command -v helm)"
KC="${BASE}/clusters/${CLUSTER}/kubectl.kubeconfig"
CHART="${BASE}/roles/cluster-addon/files/kube-prometheus-stack-${CHART_VERSION}.tgz"
VALUES="${BASE}/clusters/${CLUSTER}/yml/prom-values.yaml"
RUN_DIR="${PROM_RUN_DIR:-$(mktemp -d /tmp/kubeauto-prometheus-gate.XXXXXX)}"
RUN_DIR_OWNED="${PROM_RUN_DIR:+no}"
RUN_DIR_OWNED="${RUN_DIR_OWNED:-yes}"
mkdir -p "$RUN_DIR"
PORT_FORWARD_PIDS=()
LAB_NODES=(
  192.168.122.243 192.168.122.246 192.168.122.217
  192.168.122.210 192.168.122.216 192.168.122.193
)

pass() { echo "[PASS] $*"; }
fail() { echo "[FAIL] $*" >&2; exit 1; }

chart_member() {
  local archive="$1" member="$2"
  python3 - "$archive" "$member" <<'PY'
import sys
import tarfile

archive, member = sys.argv[1:]
with tarfile.open(archive, "r:gz") as bundle:
    entry = bundle.extractfile(member)
    if entry is None:
        raise SystemExit(1)
    sys.stdout.buffer.write(entry.read())
PY
}

extract_chart_crds() {
  local archive="$1" output="$2"
  python3 - "$archive" "$output" <<'PY'
import pathlib
import sys
import tarfile

archive, output = sys.argv[1:]
root = pathlib.Path(output).resolve()
with tarfile.open(archive, "r:gz") as bundle:
    for entry in bundle.getmembers():
        name = entry.name
        if not entry.isfile() or not name.startswith("kube-prometheus-stack/charts/crds/crds/"):
            continue
        target = (root / pathlib.Path(name).name).resolve()
        if root not in target.parents:
            raise SystemExit(f"unsafe chart member: {name}")
        target.parent.mkdir(parents=True, exist_ok=True)
        source = bundle.extractfile(entry)
        if source is None:
            raise SystemExit(f"unable to read chart member: {name}")
        target.write_bytes(source.read())
PY
}

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
    for log in "$RUN_DIR"/*port-forward.log "$RUN_DIR"/prometheus-alert-mock.log; do
      [[ -f "$log" ]] && { echo "--- $log ---"; tail -80 "$log"; }
    done
  fi
  [[ "$RUN_DIR_OWNED" != yes ]] || rm -rf -- "$RUN_DIR"
  exit "$rc"
}
trap on_exit EXIT

namespace_wait_diagnostics() {
  local reason="$1"
  echo "========== PROM_NAMESPACE_NOT_READY reason=${reason} =========="
  kubectl --kubeconfig="$KC" -n "$NAMESPACE" get pods -o wide 2>&1 || true
  kubectl --kubeconfig="$KC" -n "$NAMESPACE" get pods -o json 2>/dev/null |
    jq -r '.items[] |
      select(.metadata.deletionTimestamp == null) |
      select(.status.phase != "Running" or any(.status.containerStatuses[]?; .ready != true)) |
      {pod: .metadata.name, phase: .status.phase,
       containers: [(.status.initContainerStatuses[]?, .status.containerStatuses[]?) |
         {name, ready, restartCount, waiting: (.state.waiting // {}),
          terminated: (.state.terminated // {}), lastState}]}' 2>&1 || true
  kubectl --kubeconfig="$KC" -n "$NAMESPACE" get events --sort-by=.lastTimestamp 2>&1 |
    tail -80 || true
  for pod in $(kubectl --kubeconfig="$KC" -n "$NAMESPACE" get pods -o json 2>/dev/null |
    jq -r '.items[] |
      select(.metadata.deletionTimestamp == null) |
      select(.status.phase != "Running" or any(.status.containerStatuses[]?; .ready != true)) |
      .metadata.name'); do
    echo "--- pod/${pod} describe ---"
    kubectl --kubeconfig="$KC" -n "$NAMESPACE" describe "pod/${pod}" 2>&1 || true
    kubectl --kubeconfig="$KC" -n "$NAMESPACE" logs "pod/${pod}" --all-containers --tail=120 2>&1 || true
  done
}

wait_namespace_ready() {
  local tries=120 total ready attempt=0
  while (( tries-- > 0 )); do
    ((attempt+=1))
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
    if (( tries % 6 == 0 )); then
      echo "PROM_WAIT_HEARTBEAT ready=${ready:-0}/${total:-0} remaining=${tries}"
      namespace_wait_diagnostics "wait-attempt=${attempt}"
    fi
    sleep 10
  done
  namespace_wait_diagnostics "timeout"
  return 1
}

start_port_forward() {
  local resource="$1" local_port="$2" remote_port="$3" name="$4" ready_path="${5:-/-/ready}"
  kubectl --kubeconfig="$KC" -n "$NAMESPACE" port-forward \
    "$resource" "${local_port}:${remote_port}" >"${RUN_DIR}/prometheus-${name}-port-forward.log" 2>&1 &
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
    # Service port-forwards terminate when their selected backend Pod or node
    # disappears. Re-establish the forward so recovery checks observe the
    # replacement backend instead of polling a dead local listener.
    if [[ "$url" == "${PROM_URL:-}" ]] &&
      ! kill -0 "${PROM_PF_PID:-}" 2>/dev/null; then
      start_port_forward svc/prometheus-operated 19090 9090 prometheus-recover || true
      PROM_PF_PID="${PF_PID:-}"
    fi
    if curl -fsS --max-time 10 "${url}/api/v1/targets" -o "${RUN_DIR}/prometheus-targets.json"; then
      read -r total bad < <(jq -r '[ (.data.activeTargets | length),
        ([.data.activeTargets[] | select(.health != "up" or .lastError != "")] | length) ] | @tsv' \
        "${RUN_DIR}/prometheus-targets.json")
      if [[ "$total" -gt 0 && "$bad" -eq 0 ]]; then
        pass "Prometheus targets healthy=${total}/${total}"
        return 0
      fi
    fi
    (( tries % 6 == 0 )) && echo "PROM_TARGET_HEARTBEAT healthy=$((total-bad))/${total} remaining=${tries}"
    sleep 10
  done
  jq -r '.data.activeTargets[] | select(.health != "up" or .lastError != "") |
    [.labels.job, .scrapeUrl, .health, .lastError] | @tsv' "${RUN_DIR}/prometheus-targets.json" 2>/dev/null || true
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
sed -i "s|^prom_chart_ver:.*|prom_chart_ver: \"${BASELINE_CHART_VERSION}\"|" "$CFG"

echo "PROM_STAGE_BEGIN id=artifact-stage"
[[ "$(sha256sum "$CHART" | awk '{print $1}')" == \
  f96cb0a0999f7375b2899e4b60e6ec0e8f7133e5847f39ac87d53baea765eb32 ]] ||
  fail "current chart SHA256 mismatch"
chart_member "$CHART" kube-prometheus-stack/Chart.yaml | grep -qx 'version: 88.0.0' ||
  fail "current chart metadata version mismatch"
old_chart_for_lock="${BASE}/roles/cluster-addon/files/kube-prometheus-stack-${BASELINE_CHART_VERSION}.tgz"
[[ "$(sha256sum "$old_chart_for_lock" | awk '{print $1}')" == \
  754aeaefaf64352116e1cd0993b8c8ffc97a88f86adcedf16a6db6e8a462a791 ]] ||
  fail "baseline chart SHA256 mismatch"
chart_member "$old_chart_for_lock" kube-prometheus-stack/Chart.yaml | grep -qx 'version: 75.7.0' ||
  fail "baseline chart metadata version mismatch"
"$K" download -D </dev/null
"$K" download -E local-path-provisioner </dev/null
"$K" download -E prometheus </dev/null
"$K" download -E prometheus-optional </dev/null
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

echo "PROM_STAGE_BEGIN id=chart-upgrade"
old_chart="${BASE}/roles/cluster-addon/files/kube-prometheus-stack-${BASELINE_CHART_VERSION}.tgz"
[[ -s "$old_chart" ]] || fail "baseline chart artifact missing: ${old_chart}"
old_chart_deployed="$($HELM -n "$NAMESPACE" list --kubeconfig "$KC" -o json |
  jq -r '.[] | select(.name == "prometheus") | .chart')"
[[ "$old_chart_deployed" == "kube-prometheus-stack-${BASELINE_CHART_VERSION}" ]] ||
  fail "baseline chart mismatch: ${old_chart_deployed:-missing}"
chart88_extract="$(mktemp -d "${RUN_DIR}/prometheus-chart88-crds.XXXXXX")"
extract_chart_crds "$CHART" "$chart88_extract"
crd_dir="$chart88_extract"
[[ -d "$crd_dir" ]] || fail "current chart CRD directory missing"
kubectl --kubeconfig="$KC" apply --server-side --force-conflicts -f "$crd_dir"
sed -i "s|^prom_chart_ver:.*|prom_chart_ver: \"${CHART_VERSION}\"|" "$CFG"
sed -i \
  -e 's|repository: brinnatt/kube-webhook-certgen|repository: brinnatt/prometheus-webhook-certgen|' \
  -e 's|tag: v1.6.0|tag: 1.8.5|' "$VALUES"
"$HELM" upgrade prometheus "$CHART" -n "$NAMESPACE" --kubeconfig "$KC" \
  -f "$VALUES" --atomic --timeout 20m
wait_namespace_ready || fail "Prometheus namespace did not recover after chart upgrade"
new_chart_deployed="$($HELM -n "$NAMESPACE" list --kubeconfig "$KC" -o json |
  jq -r '.[] | select(.name == "prometheus") | .chart')"
[[ "$new_chart_deployed" == "kube-prometheus-stack-${CHART_VERSION}" ]] ||
  fail "upgraded chart mismatch: ${new_chart_deployed:-missing}"
rm -rf "$chart88_extract"
pass "official chart ${BASELINE_CHART_VERSION}->${CHART_VERSION} CRD/server-side upgrade"

echo "PROM_STAGE_BEGIN id=helm-render"
"$HELM" lint "$CHART" -f "$VALUES"
"$HELM" template prometheus "$CHART" -n "$NAMESPACE" -f "$VALUES" \
  --include-crds --kube-version "$K8S_VERSION" >"${RUN_DIR}/prometheus-rendered.yaml"
rendered_images="$(awk '$1 == "image:" {gsub(/^"|"$/, "", $2); print $2}' \
  "${RUN_DIR}/prometheus-rendered.yaml" | sort -u)"
[[ -n "$rendered_images" ]] || fail "Helm render produced no images"
nonlocal_images="$(grep -v '^registry\.talkschool\.cn:5000/' <<<"$rendered_images" || true)"
[[ -z "$nonlocal_images" ]] || {
  echo "$nonlocal_images"
  fail "Helm render contains images outside the delivery registry"
}
pass "Helm lint/template and final image registry contract"
set +e
kubectl --kubeconfig="$KC" diff --server-side --force-conflicts \
  --field-manager=prometheus-delivery \
  -f "${RUN_DIR}/prometheus-rendered.yaml" >"${RUN_DIR}/prometheus-resource-diff.txt" 2>&1
resource_diff_rc=$?
set -e
[[ "$resource_diff_rc" -eq 0 || "$resource_diff_rc" -eq 1 ]] ||
  fail "server-side resource diff failed rc=${resource_diff_rc}"
pass "server-side rendered resource diff reviewed rc=${resource_diff_rc}"

chart_deployed="$($HELM -n "$NAMESPACE" list --kubeconfig "$KC" -o json |
  jq -r '.[] | select(.name == "prometheus") | .chart')"
[[ "$chart_deployed" == "kube-prometheus-stack-${CHART_VERSION}" ]] ||
  fail "deployed chart mismatch: ${chart_deployed:-missing}"
baseline_revision="$($HELM -n "$NAMESPACE" history prometheus --kubeconfig "$KC" -o json |
  jq -r '[.[] | select(.status == "deployed")][-1].revision')"
[[ "$baseline_revision" -ge 2 ]] || fail "chart upgrade expected Helm revision >=2, got $baseline_revision"

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
up_ready=0
for up_try in $(seq 1 36); do
  if curl -fsS --max-time 10 -G "$PROM_URL/api/v1/query" \
    --data-urlencode 'query=up' -o "${RUN_DIR}/prometheus-up.json" &&
    jq -e '.status == "success" and (.data.result | length > 0) and
      all(.data.result[]; .value[1] == "1")' "${RUN_DIR}/prometheus-up.json" >/dev/null; then
    up_ready=1
    break
  fi
  if (( up_try % 6 == 0 )); then
    zero_series="$(jq -r '.data.result[]? | select(.value[1] != "1") |
      [.metric.job, .metric.instance, .value[1]] | @tsv' "${RUN_DIR}/prometheus-up.json" 2>/dev/null || true)"
    printf 'PROM_UP_HEARTBEAT remaining=%s zero_series=%s\n' "$((36 - up_try))" \
      "${zero_series:-none}"
  fi
  sleep 10
done
[[ "$up_ready" -eq 1 ]] || {
  jq -r '.data.result[]? | select(.value[1] != "1") |
    [.metric.job, .metric.instance, .value[1]] | @tsv' "${RUN_DIR}/prometheus-up.json" 2>/dev/null || true
  fail "Prometheus up query contains down targets"
}
pass "Prometheus up query returned only healthy series"

for job_pattern in 'apiserver' 'etcd' 'kubelet' 'coredns' 'node-exporter' \
  'kube-state-metrics' 'kube-controller-manager' 'kube-scheduler' 'kube-proxy'; do
  jq -e --arg pattern "$job_pattern" '[.data.activeTargets[].labels.job |
    select(test($pattern; "i"))] | length > 0' "${RUN_DIR}/prometheus-targets.json" >/dev/null ||
    fail "required scrape job missing: ${job_pattern}"
done
assert_endpoint_count kube-controller-manager 3
assert_endpoint_count kube-scheduler 3
assert_endpoint_count kube-proxy 6

rules_ready=0
for rule_try in $(seq 1 30); do
  if curl -fsS --max-time 10 "$PROM_URL/api/v1/rules" -o "${RUN_DIR}/prometheus-rules.json" &&
    jq -e '.status == "success" and
      ([.data.groups[].rules[]] | length > 0) and
      (all(.data.groups[].rules[]; .health == "ok" and (.lastError // "") == ""))' \
      "${RUN_DIR}/prometheus-rules.json" >/dev/null; then
    rules_ready=1
    break
  fi
  (( rule_try % 6 == 0 )) && echo "PROM_RULE_HEARTBEAT remaining=$((30 - rule_try))"
  sleep 10
done
[[ "$rules_ready" -eq 1 ]] || fail "Prometheus rule health failed"

for alert_try in $(seq 1 30); do
  curl -fsS --max-time 10 "$PROM_URL/api/v1/alerts" -o "${RUN_DIR}/prometheus-alerts.json"
  # Chart 88.0.0 intentionally gives PrometheusRuleFailures a 15m `for`
  # period.  A startup/reload evaluation blip can therefore remain pending
  # during this 5m gate without being an active production alert.  Only an
  # unexpected firing alert is a failure; the direct counter query below
  # still rejects an ongoing rule-evaluation error.
  bad_alerts="$(jq '[.data.alerts[] | select(.state == "firing") |
    select(.labels.alertname != "Watchdog" and .labels.alertname != "InfoInhibitor")] | length' \
    "${RUN_DIR}/prometheus-alerts.json")"
  evaluation_failures="$(curl -fsS --max-time 10 -G "$PROM_URL/api/v1/query" \
    --data-urlencode 'query=increase(prometheus_rule_evaluation_failures_total[5m])' |
    jq -r 'if .status == "success" then ([.data.result[]?.value[1] | tonumber] | add // 0) else "query_error" end')"
  if [[ "$bad_alerts" -eq 0 && "$evaluation_failures" =~ ^0+(\.0+)?$ ]]; then
    break
  fi
  (( alert_try % 6 == 0 )) &&
    echo "PROM_ALERT_HEARTBEAT firing=${bad_alerts} evaluation_failures=${evaluation_failures} remaining=$((30 - alert_try))"
  sleep 10
done
[[ "$bad_alerts" -eq 0 && "$evaluation_failures" =~ ^0+(\.0+)?$ ]] || {
  jq -r '.data.alerts[] | select(.state == "firing" or .state == "pending") |
    [.labels.alertname, .labels.severity, .state] | @tsv' "${RUN_DIR}/prometheus-alerts.json"
  fail "unexpected firing alerts or rule evaluation failures remain"
}
pass "targets/rules/default-alert health verified"

echo "PROM_STAGE_BEGIN id=node-failure-recovery"
failure_node_ip="192.168.122.210"
failure_node_name="$(kubectl --kubeconfig="$KC" get nodes -o wide --no-headers |
  awk -v ip="$failure_node_ip" '$6 == ip && name == "" {name = $1} END {print name}')"
[[ -n "$failure_node_name" ]] || fail "failure-test node not found: ${failure_node_ip}"
ssh -o BatchMode=yes -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
  -o ConnectTimeout=10 "root@${failure_node_ip}" 'systemctl stop kubelet'
node_not_ready=0
for node_failure_try in $(seq 1 90); do
  node_ready="$(kubectl --kubeconfig="$KC" get node "$failure_node_name" \
    -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}' 2>/dev/null || true)"
  if [[ "$node_ready" != True ]]; then
    node_not_ready=1
    break
  fi
  (( node_failure_try % 10 == 0 )) && echo "PROM_NODE_FAILURE_HEARTBEAT node=${failure_node_name} remaining=$((90 - node_failure_try))"
  sleep 2
done
[[ "$node_not_ready" -eq 1 ]] || fail "node did not become NotReady after kubelet stop"
ssh -o BatchMode=yes -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
  -o ConnectTimeout=10 "root@${failure_node_ip}" 'systemctl start kubelet'
kubectl --kubeconfig="$KC" wait --for=condition=Ready "node/${failure_node_name}" --timeout=600s
wait_targets_healthy "$PROM_URL" || fail "targets did not recover after node failure"
pass "controlled node failure and recovery node=${failure_node_name}"

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
  tee "${RUN_DIR}/grafana-datasources.json" |
  jq -e 'any(.[]; .type == "prometheus" and (.url | test("prometheus")))' >/dev/null ||
  fail "Grafana Prometheus datasource missing"
grafana_datasource_uid="$(jq -r 'map(select(.type == "prometheus"))[0].uid // empty' "${RUN_DIR}/grafana-datasources.json")"
[[ -n "$grafana_datasource_uid" ]] || fail "Grafana Prometheus datasource UID missing"
grafana_health_response="${RUN_DIR}/grafana-datasource-health.json"
grafana_health_http="$(curl -sS --max-time 10 -u "$grafana_user:$grafana_password" \
  -o "$grafana_health_response" -w '%{http_code}' \
  "$GRAFANA_URL/api/datasources/uid/${grafana_datasource_uid}/health" || true)"
if ! jq -e '.status == "OK"' "$grafana_health_response" >/dev/null 2>&1; then
  echo "[FAILURE EVIDENCE] Grafana datasource health HTTP=${grafana_health_http}"
  jq . "$grafana_health_response" 2>/dev/null || sed -n '1,120p' "$grafana_health_response"
  fail "Grafana datasource Save & Test failed"
fi
cat >"${RUN_DIR}/grafana-ds-query.json" <<EOF
{"queries":[{"refId":"A","expr":"up","format":"time_series","datasource":{"uid":"${grafana_datasource_uid}","type":"prometheus"}}],"from":"now-5m","to":"now"}
EOF
curl -fsS --max-time 10 -u "$grafana_user:$grafana_password" \
  -H 'Content-Type: application/json' -X POST "$GRAFANA_URL/api/ds/query" \
  --data-binary @"${RUN_DIR}/grafana-ds-query.json" |
  jq -e '.results.A.frames | length > 0' >/dev/null || fail "Grafana Dashboard query returned no data"
dashboard_count="$(curl -fsS --max-time 10 -u "$grafana_user:$grafana_password" \
  "$GRAFANA_URL/api/search?type=dash-db" | jq 'length')"
[[ "$dashboard_count" -gt 0 ]] || fail "Grafana built-in dashboards missing"
pass "Grafana login/datasource/dashboards count=${dashboard_count}"

echo "PROM_STAGE_BEGIN id=admission-and-failure-recovery"
cat >"${RUN_DIR}/prometheus-invalid-rule.yaml" <<'EOF'
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
if kubectl --kubeconfig="$KC" apply -f "${RUN_DIR}/prometheus-invalid-rule.yaml" >"${RUN_DIR}/prometheus-invalid.out" 2>&1; then
  kubectl --kubeconfig="$KC" -n "$NAMESPACE" delete prometheusrule delivery-invalid --ignore-not-found
  fail "admission webhook accepted an invalid PrometheusRule"
fi
kubectl --kubeconfig="$KC" -n "$NAMESPACE" get deploy \
  prometheus-kube-prometheus-operator-webhook -o json |
  jq -e '.status.readyReplicas == 2' >/dev/null || fail "admission webhook HA deployment not ready"

if "$HELM" upgrade prometheus "$CHART" -n "$NAMESPACE" --kubeconfig "$KC" \
  -f "$VALUES" --set prometheusOperator.image.tag=delivery-missing \
  --atomic --timeout 90s >"${RUN_DIR}/prometheus-atomic-failure.log" 2>&1; then
  fail "expected atomic bad-image upgrade to fail"
fi
wait_namespace_ready || fail "stack did not recover after atomic failed upgrade"
stable_revision="$($HELM -n "$NAMESPACE" history prometheus --kubeconfig "$KC" -o json |
  jq -r '[.[] | select(.status == "deployed")][-1].revision')"
[[ -n "$stable_revision" && "$stable_revision" -gt "$baseline_revision" ]] ||
  fail "atomic failure did not produce a deployed recovery revision"
pass "admission rejection and atomic failed-upgrade recovery revision=${stable_revision}"

echo "PROM_STAGE_BEGIN id=alertmanager-notification"
cat >"${RUN_DIR}/prometheus-alert-mock.yaml" <<'EOF'
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
kubectl --kubeconfig="$KC" apply -f "${RUN_DIR}/prometheus-alert-mock.yaml"
kubectl --kubeconfig="$KC" -n "$NAMESPACE" rollout status \
  deployment/delivery-alert-mock --timeout=300s

cat >"${RUN_DIR}/prometheus-alert-values.yaml" <<EOF
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
  -f "$VALUES" -f "${RUN_DIR}/prometheus-alert-values.yaml" --wait --timeout 20m
mock_revision="$($HELM -n "$NAMESPACE" history prometheus --kubeconfig "$KC" -o json |
  jq -r '[.[] | select(.status == "deployed")][-1].revision')"
[[ "$mock_revision" -gt "$stable_revision" ]] || fail "Alertmanager config upgrade revision did not advance"
wait_namespace_ready || fail "stack not ready after Alertmanager config upgrade"

cat >"${RUN_DIR}/prometheus-delivery-rule.yaml" <<'EOF'
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
cat >"${RUN_DIR}/prometheus-delivery-warning-rule.yaml" <<'EOF'
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
kubectl --kubeconfig="$KC" apply -f "${RUN_DIR}/prometheus-delivery-rule.yaml" \
  -f "${RUN_DIR}/prometheus-delivery-warning-rule.yaml"

start_port_forward svc/alertmanager-operated 19092 9093 alertmanager || fail "Alertmanager port-forward failed"
AM_PF_PID="$PF_PID"
AM_URL=http://127.0.0.1:19092
start_port_forward svc/delivery-alert-mock 19094 8080 alert-mock /alerts || fail "alert mock port-forward failed"
MOCK_PF_PID="$PF_PID"
MOCK_URL=http://127.0.0.1:19094/alerts
curl -fsS --max-time 10 "$AM_URL/api/v2/status" -o "${RUN_DIR}/alertmanager-status.json"
jq -e '.cluster.status == "ready" and (.cluster.peers | length >= 2)' \
  "${RUN_DIR}/alertmanager-status.json" >/dev/null || fail "Alertmanager HA cluster status failed"
for am_alert_try in $(seq 1 60); do
  curl -fsS --max-time 10 "$AM_URL/api/v2/alerts" -o "${RUN_DIR}/alertmanager-alerts.json"
  critical="$(jq '[.[] | select(.labels.delivery_id == "prometheus-gate" and
    .labels.severity == "critical")] | length' "${RUN_DIR}/alertmanager-alerts.json")"
  inhibited="$(jq '[.[] | select(.labels.alertname == "DeliveryWarning" and
    (.status.inhibitedBy | length > 0))] | length' "${RUN_DIR}/alertmanager-alerts.json")"
  [[ "$critical" -eq 2 && "$inhibited" -eq 1 ]] && break
  sleep 2
done
[[ "$critical" -eq 2 && "$inhibited" -eq 1 ]] || fail "Alertmanager grouping/inhibition state failed"

for firing_try in $(seq 1 60); do
  if curl -fsS --max-time 10 "$MOCK_URL" -o "${RUN_DIR}/prometheus-alert-webhook.json" &&
    jq -e 'any(.[]; .status == "firing" and
    ([.alerts[] | select(.labels.delivery_id == "prometheus-gate" and
      .labels.severity == "critical")] | length == 2) and
    ([.alerts[] | select(.labels.severity == "warning")] | length == 0))' \
      "${RUN_DIR}/prometheus-alert-webhook.json" >/dev/null 2>&1; then
    break
  fi
  sleep 2
done
jq -e 'any(.[]; .status == "firing" and
  ([.alerts[] | select(.labels.delivery_id == "prometheus-gate" and
    .labels.severity == "critical")] | length == 2))' \
  "${RUN_DIR}/prometheus-alert-webhook.json" >/dev/null ||
  fail "Alertmanager firing notification payload missing"
firing_notifications_before="$(jq 'length' "${RUN_DIR}/prometheus-alert-webhook.json")"
sleep 10
curl -fsS --max-time 10 "$MOCK_URL" -o "${RUN_DIR}/prometheus-alert-webhook-repeat.json"
firing_notifications_after="$(jq 'length' "${RUN_DIR}/prometheus-alert-webhook-repeat.json")"
[[ "$firing_notifications_after" -eq "$firing_notifications_before" ]] ||
  fail "Alertmanager repeated firing notification was not suppressed"

sed 's/expr: vector(1)/expr: vector(0) > 0/g' "${RUN_DIR}/prometheus-delivery-rule.yaml" \
  >"${RUN_DIR}/prometheus-delivery-rule-resolved.yaml"
kubectl --kubeconfig="$KC" apply -f "${RUN_DIR}/prometheus-delivery-rule-resolved.yaml"
critical_remaining=2
for resolved_try in $(seq 1 120); do
  if curl -fsS --max-time 10 "$PROM_URL/api/v1/alerts" -o "${RUN_DIR}/prometheus-alerts-resolved.json"; then
    critical_remaining="$(jq '[.data.alerts[]? | select(.labels.delivery_id == "prometheus-gate" and
      .labels.severity == "critical" and .state == "firing")] | length' \
      "${RUN_DIR}/prometheus-alerts-resolved.json")"
  fi
  [[ "$critical_remaining" -eq 0 ]] && break
  sleep 2
done
[[ "$critical_remaining" -eq 0 ]] || fail "Prometheus critical alerts did not resolve after rule update"

for resolved_try in $(seq 1 90); do
  if curl -fsS --max-time 10 "$MOCK_URL" -o "${RUN_DIR}/prometheus-alert-webhook.json" &&
    jq -e 'any(.[]; .status == "resolved" and
      any(.alerts[]; .labels.delivery_id == "prometheus-gate"))' \
      "${RUN_DIR}/prometheus-alert-webhook.json" >/dev/null 2>&1; then
    break
  fi
  sleep 2
done
jq -e 'any(.[]; .status == "resolved" and
  any(.alerts[]; .labels.delivery_id == "prometheus-gate"))' \
  "${RUN_DIR}/prometheus-alert-webhook.json" >/dev/null ||
  fail "Alertmanager resolved notification payload missing"
pass "Alertmanager HA/grouping/inhibition/firing/resolved webhook chain"

alertmanager_pod="$(kubectl --kubeconfig="$KC" -n "$NAMESPACE" get pod \
  -l app.kubernetes.io/name=alertmanager -o json | jq -r '.items | sort_by(.metadata.name) | .[0].metadata.name')"
[[ -n "$alertmanager_pod" ]] || fail "Alertmanager member pod missing"
kubectl --kubeconfig="$KC" -n "$NAMESPACE" delete pod "$alertmanager_pod" --wait=false
kubectl --kubeconfig="$KC" -n "$NAMESPACE" wait --for=condition=Ready \
  pod "$alertmanager_pod" --timeout=600s
stop_port_forward "$AM_PF_PID"
start_port_forward svc/alertmanager-operated 19092 9093 alertmanager-recover ||
  fail "Alertmanager port-forward did not recover after member replacement"
AM_PF_PID="$PF_PID"
AM_URL=http://127.0.0.1:19092
am_recovery_status_ok=0
for am_recovery_try in $(seq 1 60); do
  if curl -fsS --max-time 10 "$AM_URL/api/v2/status" \
    -o "${RUN_DIR}/alertmanager-status-after-member-failure.json" &&
    jq -e '.cluster.status == "ready" and (.cluster.peers | length >= 2)' \
      "${RUN_DIR}/alertmanager-status-after-member-failure.json" >/dev/null; then
    am_recovery_status_ok=1
    break
  fi
  sleep 2
done
if [[ "$am_recovery_status_ok" -ne 1 ]]; then
  echo "[FAILURE EVIDENCE] Alertmanager recovery status:"
  jq . "${RUN_DIR}/alertmanager-status-after-member-failure.json" 2>/dev/null || true
  kubectl --kubeconfig="$KC" -n "$NAMESPACE" logs "$alertmanager_pod" \
    --all-containers --tail=120 2>&1 || true
  fail "Alertmanager member failure did not recover HA cluster"
fi
pass "Alertmanager member failure and cluster recovery"

kubectl --kubeconfig="$KC" -n "$NAMESPACE" delete prometheusrule \
  delivery-notification delivery-notification-warning --wait=true

stop_port_forward "$MOCK_PF_PID"
kubectl --kubeconfig="$KC" delete -f "${RUN_DIR}/prometheus-alert-mock.yaml" --wait=true

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
[[ "$revision_after_repeat" -ge "$revision_before_repeat" ]] ||
  fail "idempotent addon rerun produced an invalid Helm revision"
wait_namespace_ready || fail "namespace not ready after idempotent addon rerun"
[[ "$grafana_secret_after" == "$grafana_secret_before" ]] || fail "idempotent addon rerun rotated Grafana password"

prom_pod="$(kubectl --kubeconfig="$KC" -n "$NAMESPACE" get pod \
  -l app.kubernetes.io/name=prometheus -o json | jq -r '.items | sort_by(.metadata.name) | .[0].metadata.name')"
prom_pod_uid_before="$(kubectl --kubeconfig="$KC" -n "$NAMESPACE" get pod "$prom_pod" \
  -o jsonpath='{.metadata.uid}')"
start_port_forward "pod/${prom_pod}" 19091 9090 prometheus-pod || fail "Prometheus pod port-forward failed"
POD_PF_PID="$PF_PID"
POD_PROM_URL=http://127.0.0.1:19091
curl -fsS --max-time 10 -G "$POD_PROM_URL/api/v1/query" \
  --data-urlencode 'query=prometheus_build_info' -o "${RUN_DIR}/prometheus-before-restart.json"
sample_ts="$(jq -r '.data.result[0].value[0]' "${RUN_DIR}/prometheus-before-restart.json")"
[[ "$sample_ts" != null && -n "$sample_ts" ]] || fail "pre-restart Prometheus sample missing"
stop_port_forward "$POD_PF_PID"

kubectl --kubeconfig="$KC" -n "$NAMESPACE" delete pod "$prom_pod" --wait=false
# A service port-forward is tied to the selected backend Pod.  When that Pod is
# deleted, kubectl exits; recreate the forward before probing the replacement.
stop_port_forward "$PROM_PF_PID" || true
start_port_forward svc/prometheus-operated 19090 9090 prometheus-after-restart ||
  fail "Prometheus service port-forward failed after replica restart"
PROM_PF_PID="$PF_PID"
for service_query_try in $(seq 1 60); do
  if curl -fsS --max-time 10 -G "$PROM_URL/api/v1/query" \
    --data-urlencode 'query=up' -o "${RUN_DIR}/prometheus-up-during-restart.json" &&
    jq -e '.status == "success" and (.data.result | length > 0)' \
      "${RUN_DIR}/prometheus-up-during-restart.json" >/dev/null; then
    break
  fi
  sleep 2
done
jq -e '.status == "success" and (.data.result | length > 0)' \
  "${RUN_DIR}/prometheus-up-during-restart.json" >/dev/null ||
  fail "Prometheus service query unavailable during replica restart"
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
  --data-urlencode 'step=15s' -o "${RUN_DIR}/prometheus-after-restart.json"
jq -e --argjson before "$sample_ts" '.status == "success" and
  any(.data.result[]?.values[]?; .[0] <= ($before + 30))' "${RUN_DIR}/prometheus-after-restart.json" >/dev/null ||
  fail "historical Prometheus samples missing after pod/PVC restart"
stop_port_forward "$POD_PF_PID"
pass "idempotency, Grafana Secret, PVC UID and TSDB history preserved"

echo "PROM_STAGE_BEGIN id=promql-performance"
stop_port_forward "$PROM_PF_PID"
start_port_forward svc/prometheus-operated 19090 9090 prometheus-performance || fail "Prometheus performance port-forward failed"
PROM_PF_PID="$PF_PID"
rm -f "${RUN_DIR}/prometheus-performance-results"
export PROM_URL
performance_start="$(date +%s)"
seq 1 200 | xargs -P 20 -I '{}' bash -c '
  curl -fsS --max-time 5 -G "$PROM_URL/api/v1/query" \
    --data-urlencode "query=sum(rate(prometheus_http_requests_total[5m]))" >/dev/null && echo ok
' >>"${RUN_DIR}/prometheus-performance-results"
performance_seconds="$(( $(date +%s) - performance_start ))"
performance_ok="$(grep -cx ok "${RUN_DIR}/prometheus-performance-results" || true)"
[[ "$performance_ok" -eq 200 && "$performance_seconds" -le 60 ]] ||
  fail "PromQL performance failed ok=${performance_ok}/200 seconds=${performance_seconds}"
pass "PromQL concurrency=20 requests=200 errors=0 seconds=${performance_seconds}"
stop_port_forward "$PROM_PF_PID"

echo "PROM_STAGE_BEGIN id=optional-branches"
BASE="$BASE" KC="$KC" NAMESPACE="$NAMESPACE" HELM="$HELM" PROM_RUN_DIR="$RUN_DIR" \
  bash "${BASE}/tests/helpers/prometheus-optional-regression.sh"

echo "PROM_STAGE_BEGIN id=destroy"
"$K" destroy "$CLUSTER" </dev/null
rm -rf "${BASE}/clusters/${CLUSTER}"
rm -f "${BASE}/clusters/${CLUSTER}.hosts"
pass "${MARKER} chart=${CHART_VERSION} prometheus_replicas=${prom_replicas} alertmanager_replicas=${am_replicas} pvc_bound=${pvc_bound}"
echo "$MARKER"
