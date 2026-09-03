#!/usr/bin/env bash
# Independent live gates for Thanos, Prometheus Adapter and Blackbox Exporter.
set -Eeuo pipefail
: "${KC:?kubeconfig is required}"
: "${NAMESPACE:?namespace is required}"
: "${HELM:?helm is required}"
: "${BASE:?kubeauto base is required}"
: "${PROM_RUN_DIR:?prometheus run directory is required}"
RUN_DIR="$PROM_RUN_DIR"
[[ -d "$RUN_DIR" ]] || {
  echo "[FAIL] Prometheus run directory does not exist: $RUN_DIR" >&2
  exit 2
}
K=(kubectl --kubeconfig="$KC")
REGISTRY="${PROM_OPTIONAL_REGISTRY:-hub.talkedu.cn/kubeauto}"
CHART="$BASE/roles/cluster-addon/files/kube-prometheus-stack-88.0.0.tgz"
ADAPTER_CHART="$BASE/roles/cluster-addon/files/prometheus-adapter-5.3.0.tgz"
BLACKBOX_CHART="$BASE/roles/cluster-addon/files/prometheus-blackbox-exporter-11.3.1.tgz"
pass() { echo "[PASS] $*"; }
fail() { echo "[FAIL] $*" >&2; exit 1; }
ready() {
  local selector="$1" expected="$2" count
  for _ in $(seq 1 60); do
    count="$("${K[@]}" -n "$NAMESPACE" get pods -l "$selector" -o json 2>/dev/null |
      jq '[.items[] | select(.status.phase == "Running") | select(all(.status.containerStatuses[]?; .ready == true))] | length')"
    [[ "$count" -ge "$expected" ]] && return 0
    sleep 5
  done
  return 1
}

echo "PROM_OPTIONAL_STAGE_BEGIN id=artifact-stage"
bash "$BASE/tests/helpers/prometheus-optional-artifact-gate.sh"

echo "PROM_OPTIONAL_STAGE_BEGIN id=product-config"
CFG="$BASE/clusters/prometheus-gate/config.yml"
KUBECLI="$BASE/.venv/bin/kubecli"
[[ -x "$KUBECLI" ]] || KUBECLI="$(command -v kubecli)"
sed -i \
  -e 's|^prom_thanos_install:.*|prom_thanos_install: "yes"|' \
  -e 's|^prom_adapter_install:.*|prom_adapter_install: "yes"|' \
  -e 's|^prom_adapter_prometheus_url:.*|prom_adapter_prometheus_url: ""|' \
  -e 's|^prom_blackbox_install:.*|prom_blackbox_install: "yes"|' \
  -e 's|^prom_blackbox_probe_targets:.*|prom_blackbox_probe_targets: [{name: customer-http, module: http_2xx, url: http://blackbox-app.monitor.svc.cluster.local:8080/}]|' \
  "$CFG"
"$KUBECLI" setup prometheus-gate 07 </dev/null
ready 'app=thanos-querier' 2 || fail "Kubeauto Thanos Querier pods not ready"
ready 'app.kubernetes.io/name=prometheus' 2 || fail "Kubeauto Thanos Sidecar pods not ready"
"${K[@]}" -n "$NAMESPACE" get probe kubeauto-blackbox-customer-http >/dev/null ||
  fail "Kubeauto Blackbox Probe was not rendered from config"
"${K[@]}" -n "$NAMESPACE" get deployment prometheus-adapter -o json |
  jq -e 'any(.spec.template.spec.containers[0].args[]?; . == "--prometheus-url=http://prometheus-operated.monitor.svc.cluster.local:9090")' \
  >/dev/null || fail "empty prom_adapter_prometheus_url did not fall back to core Prometheus Service"
pass "Kubeauto optional configuration rendered Thanos, Adapter fallback and Blackbox Probe"

echo "PROM_OPTIONAL_STAGE_BEGIN id=thanos"
cat >"${RUN_DIR}/prometheus-thanos-values.yaml" <<EOF
prometheus:
  thanosService: {enabled: true, type: ClusterIP}
  thanosServiceMonitor: {enabled: true}
  prometheusSpec:
    thanos:
      image: ${REGISTRY}/thanos:v0.41.0
      version: v0.41.0
EOF
"$HELM" upgrade prometheus "$CHART" -n "$NAMESPACE" --kubeconfig "$KC" \
  -f "$BASE/clusters/prometheus-gate/yml/prom-values.yaml" \
  -f "${RUN_DIR}/prometheus-thanos-values.yaml" --wait --timeout 20m
ready 'app.kubernetes.io/name=prometheus' 2 || fail "Thanos Sidecar pods not ready"
discovery="$("${K[@]}" -n "$NAMESPACE" get svc -o json |
  jq -r '[.items[] | select(.metadata.name | endswith("-prometheus-thanos-discovery")) | .metadata.name] | .[0] // empty')"
[[ -n "$discovery" ]] || fail "Thanos discovery Service missing"
cat >"${RUN_DIR}/prometheus-thanos-querier.yaml" <<EOF
apiVersion: apps/v1
kind: Deployment
metadata: {name: thanos-querier, namespace: ${NAMESPACE}}
spec:
  replicas: 2
  selector: {matchLabels: {app: thanos-querier}}
  template:
    metadata: {labels: {app: thanos-querier}}
    spec:
      containers:
      - name: thanos
        image: ${REGISTRY}/thanos:v0.41.0
        args:
        - query
        - --endpoint=dnssrv+_grpc._tcp.${discovery}.${NAMESPACE}.svc.cluster.local
        # Keep a direct Service endpoint alongside SRV discovery.  This is
        # required on clusters whose DNS implementation does not publish SRV
        # records immediately after a headless-style discovery Service is
        # created; both endpoints resolve to the same Sidecar Store API.
        - --endpoint=dns+${discovery}.${NAMESPACE}.svc.cluster.local:10901
        - --query.replica-label=prometheus_replica
        - --http-address=0.0.0.0:9090
        ports: [{name: http, containerPort: 9090}]
        readinessProbe: {httpGet: {path: /-/ready, port: http}}
        resources: {requests: {cpu: 100m, memory: 256Mi}, limits: {cpu: 1, memory: 1Gi}}
---
apiVersion: v1
kind: Service
metadata: {name: thanos-querier, namespace: ${NAMESPACE}}
spec:
  selector: {app: thanos-querier}
  ports: [{name: http, port: 9090, targetPort: http}]
EOF
"${K[@]}" apply -f "${RUN_DIR}/prometheus-thanos-querier.yaml"
"${K[@]}" -n "$NAMESPACE" rollout status deployment/thanos-querier --timeout=10m ||
  fail "Thanos Querier rollout did not converge"
ready 'app=thanos-querier' 2 || fail "Thanos Querier pods not ready"
"${K[@]}" -n "$NAMESPACE" port-forward svc/thanos-querier 19095:9090 >"${RUN_DIR}/prometheus-thanos-port-forward.log" 2>&1 &
thanos_pf=$!
cleanup_thanos() { kill "$thanos_pf" 2>/dev/null || true; }
trap cleanup_thanos EXIT
for _ in $(seq 1 30); do curl -fsS http://127.0.0.1:19095/-/ready >/dev/null 2>&1 && break; sleep 1; done
stores_ready=0
for _ in $(seq 1 60); do
  if curl -fsS http://127.0.0.1:19095/api/v1/stores -o "${RUN_DIR}/prometheus-thanos-stores.json" &&
    jq -e '.status == "success" and ([((.data.stores // .data.sidecar)[]?)] |
      map(select((.state // "ready") != "down")) | length) >= 2' \
      "${RUN_DIR}/prometheus-thanos-stores.json" >/dev/null; then
    stores_ready=1
    break
  fi
  sleep 2
done
if [[ "$stores_ready" -ne 1 ]]; then
  echo "Thanos stores response:" >&2
  echo "--- raw response ---" >&2
  sed -n '1,80p' "${RUN_DIR}/prometheus-thanos-stores.json" >&2 || true
  jq -c '((.data.stores // .data.sidecar)[]? |
    {endpoint: (.endpoint // .name), state, lastCheck, lastError})' \
    "${RUN_DIR}/prometheus-thanos-stores.json" >&2 || true
  echo "--- discovery service ---" >&2
  "${K[@]}" -n "$NAMESPACE" get svc "$discovery" -o yaml >&2 || true
  echo "--- discovery endpoints ---" >&2
  "${K[@]}" -n "$NAMESPACE" get endpointslice -l "kubernetes.io/service-name=$discovery" -o yaml >&2 || true
  echo "--- prometheus sidecar containers ---" >&2
  "${K[@]}" -n "$NAMESPACE" get pods -l app.kubernetes.io/name=prometheus -o json |
    jq -r '.items[] | [.metadata.name, (.status.containerStatuses[]?.name // ""), (.status.containerStatuses[]?.ready // false)] | @tsv' >&2 || true
  echo "--- thanos querier logs ---" >&2
  "${K[@]}" -n "$NAMESPACE" logs deployment/thanos-querier --all-containers --tail=120 >&2 || true
  fail "Thanos Store API unhealthy"
fi
thanos_dedup_ready=0
raw=0
dedup=0
for attempt in $(seq 1 90); do
  curl -fsS -G http://127.0.0.1:19095/api/v1/query --data-urlencode 'query=up' \
    --data-urlencode 'dedup=false' -o "${RUN_DIR}/prometheus-thanos-raw.json"
  curl -fsS -G http://127.0.0.1:19095/api/v1/query --data-urlencode 'query=up' \
    --data-urlencode 'dedup=true' -o "${RUN_DIR}/prometheus-thanos-dedup.json"
  raw="$(jq -r 'if .status == "success" then [.data.result[]?] | length else 0 end' "${RUN_DIR}/prometheus-thanos-raw.json")"
  dedup="$(jq -r 'if .status == "success" then [.data.result[]?] | length else 0 end' "${RUN_DIR}/prometheus-thanos-dedup.json")"
  if [[ "$raw" -gt "$dedup" && "$dedup" -gt 0 ]]; then
    thanos_dedup_ready=1
    break
  fi
  if (( attempt % 10 == 0 )); then
    echo "Thanos query visibility pending attempt=${attempt} raw=${raw} dedup=${dedup}"
  fi
  sleep 2
done
if [[ "$thanos_dedup_ready" -ne 1 ]]; then
  echo "Thanos raw query response:" >&2
  sed -n '1,80p' "${RUN_DIR}/prometheus-thanos-raw.json" >&2 || true
  echo "Thanos deduplicated query response:" >&2
  sed -n '1,80p' "${RUN_DIR}/prometheus-thanos-dedup.json" >&2 || true
  echo "Thanos Store API response:" >&2
  jq -c '((.data.stores // .data.sidecar)[]? | {endpoint: (.endpoint // .name), state, lastError})' \
    "${RUN_DIR}/prometheus-thanos-stores.json" >&2 || true
  fail "Thanos dedup failed after query visibility wait raw=${raw} dedup=${dedup}"
fi
history_end="$(date +%s)"
history_start="$((history_end - 300))"
curl -fsS -G http://127.0.0.1:19095/api/v1/query_range --data-urlencode 'query=up' \
  --data-urlencode "start=${history_start}" --data-urlencode "end=${history_end}" \
  --data-urlencode 'step=30s' -o "${RUN_DIR}/prometheus-thanos-history.json"
jq -e '.status == "success" and ([.data.result[]?.values[]?] | length) > 0' "${RUN_DIR}/prometheus-thanos-history.json" >/dev/null || fail "Thanos history query empty"
pass "Thanos Store API, dedup and history stores>=2 raw=${raw} dedup=${dedup}"
cleanup_thanos; trap - EXIT

"${K[@]}" -n "$NAMESPACE" port-forward svc/prometheus-operated 19090:9090 >"${RUN_DIR}/prometheus-optional-prom-port-forward.log" 2>&1 &
prom_pf=$!
cleanup_prom() { kill "$prom_pf" 2>/dev/null || true; }
trap cleanup_prom EXIT
for _ in $(seq 1 30); do curl -fsS http://127.0.0.1:19090/-/ready >/dev/null 2>&1 && break; sleep 1; done

echo "PROM_OPTIONAL_STAGE_BEGIN id=adapter"
cat >"${RUN_DIR}/prometheus-adapter-values.yaml" <<EOF
image:
  repository: ${REGISTRY}/prometheus-adapter
  tag: v0.12.0
prometheus:
  url: http://thanos-querier.${NAMESPACE}.svc.cluster.local
  port: 9090
metricsRelistInterval: 15s
rules:
  default: false
  custom:
  - seriesQuery: 'kube_pod_info{namespace!="",pod!=""}'
    resources:
      overrides:
        namespace: {resource: namespace}
        pod: {resource: pod}
    name: {matches: '^kube_pod_info$', as: 'pod_info'}
    metricsQuery: 'count(<<.Series>>{<<.LabelMatchers>>}) by (<<.GroupBy>>)'
resources:
  requests: {cpu: 100m, memory: 128Mi}
  limits: {cpu: 500m, memory: 512Mi}
EOF
"$HELM" upgrade --install prometheus-adapter "$ADAPTER_CHART" -n "$NAMESPACE" --kubeconfig "$KC" \
  -f "${RUN_DIR}/prometheus-adapter-values.yaml" --wait --timeout 10m
"${K[@]}" -n "$NAMESPACE" wait --for=condition=Available deployment/prometheus-adapter --timeout=300s
api_available=0
for _ in $(seq 1 30); do
  api_available="$("${K[@]}" get apiservice v1beta1.custom.metrics.k8s.io -o json 2>/dev/null |
    jq -r '[.status.conditions[]? | select(.type == "Available" and .status == "True")] | length')"
  [[ "$api_available" == 1 ]] && break
  sleep 5
done
[[ "$api_available" == 1 ]] || fail "Custom Metrics APIService unavailable"
"${K[@]}" get --raw /apis/custom.metrics.k8s.io/v1beta1 >"${RUN_DIR}/prometheus-custom-metrics-api.json"
jq -e '.resources | any(.[]; .name == "pods/pod_info")' "${RUN_DIR}/prometheus-custom-metrics-api.json" >/dev/null || fail "Custom metric not published"
"${K[@]}" get --raw "/apis/custom.metrics.k8s.io/v1beta1/namespaces/${NAMESPACE}/pods/*/pod_info" >"${RUN_DIR}/prometheus-custom-metric-values.json"
jq -e '.kind == "MetricValueList" and ([.items[]? | select(.value != null)] | length) > 0' "${RUN_DIR}/prometheus-custom-metric-values.json" >/dev/null || fail "Custom metric values empty"
pass "Prometheus Adapter APIService and custom metric values verified"

echo "PROM_OPTIONAL_STAGE_BEGIN id=blackbox"
cat >"${RUN_DIR}/prometheus-blackbox-app.yaml" <<EOF
apiVersion: v1
kind: ConfigMap
metadata: {name: blackbox-app-content, namespace: ${NAMESPACE}}
data: {index.html: 'prometheus blackbox delivery ok'}
---
apiVersion: apps/v1
kind: Deployment
metadata: {name: blackbox-app, namespace: ${NAMESPACE}}
spec:
  replicas: 1
  selector: {matchLabels: {app: blackbox-app}}
  template:
    metadata: {labels: {app: blackbox-app}}
    spec:
      containers:
      - name: http
        image: ${REGISTRY}/busybox:1.37
        command: [sh, -c]
        args: ["httpd -f -p 8080 -h /www"]
        ports: [{name: http, containerPort: 8080}]
        volumeMounts: [{name: content, mountPath: /www}]
      volumes: [{name: content, configMap: {name: blackbox-app-content}}]
---
apiVersion: v1
kind: Service
metadata: {name: blackbox-app, namespace: ${NAMESPACE}}
spec:
  selector: {app: blackbox-app}
  ports: [{name: http, port: 8080, targetPort: http}]
EOF
"${K[@]}" apply -f "${RUN_DIR}/prometheus-blackbox-app.yaml"
"${K[@]}" rollout status deployment/blackbox-app -n "$NAMESPACE" --timeout=300s
"$HELM" upgrade --install blackbox-exporter "$BLACKBOX_CHART" -n "$NAMESPACE" --kubeconfig "$KC" \
  --set image.registry="$REGISTRY" --set image.repository=blackbox-exporter \
  --set image.tag=v0.27.0 --set fullnameOverride=blackbox-exporter \
  --set replicaCount=2 --wait --timeout 10m
cat >"${RUN_DIR}/prometheus-blackbox-probe.yaml" <<EOF
apiVersion: monitoring.coreos.com/v1
kind: Probe
metadata:
  name: prometheus-blackbox-app
  namespace: ${NAMESPACE}
  labels: {release: prometheus}
spec:
  jobName: prometheus-blackbox-app
  interval: 15s
  scrapeTimeout: 10s
  module: http_2xx
  prober: {url: blackbox-exporter.${NAMESPACE}.svc.cluster.local:9115, scheme: http, path: /probe}
  targets:
    staticConfig:
      static: [http://blackbox-app.${NAMESPACE}.svc.cluster.local:8080/]
EOF
"${K[@]}" apply -f "${RUN_DIR}/prometheus-blackbox-probe.yaml"
for _ in $(seq 1 40); do
  curl -fsS -G http://127.0.0.1:19090/api/v1/targets -o "${RUN_DIR}/prometheus-blackbox-targets.json" 2>/dev/null || true
  if jq -e '[.data.activeTargets[]? | select(.labels.job == "prometheus-blackbox-app" and .health == "up" and .lastError == "")] | length == 1' "${RUN_DIR}/prometheus-blackbox-targets.json" >/dev/null 2>&1; then break; fi
  sleep 5
done
jq -e '[.data.activeTargets[]? | select(.labels.job == "prometheus-blackbox-app" and .health == "up")] | length == 1' "${RUN_DIR}/prometheus-blackbox-targets.json" >/dev/null || fail "Blackbox Probe target not UP"
curl -fsS -G http://127.0.0.1:19090/api/v1/query --data-urlencode 'query=probe_success{job="prometheus-blackbox-app"}' -o "${RUN_DIR}/prometheus-blackbox-probe.json"
jq -e '[.data.result[]? | select(.value[1] == "1")] | length == 1' "${RUN_DIR}/prometheus-blackbox-probe.json" >/dev/null || fail "Blackbox probe_success != 1"
curl -fsS -G http://127.0.0.1:19090/api/v1/query \
  --data-urlencode 'query=probe_success{job="kubeauto-blackbox-customer-http"}' \
  -o "${RUN_DIR}/prometheus-kubeauto-blackbox-probe.json"
jq -e '[.data.result[]? | select(.value[1] == "1")] | length == 1' \
  "${RUN_DIR}/prometheus-kubeauto-blackbox-probe.json" >/dev/null ||
  fail "Kubeauto-configured Blackbox probe_success != 1"
pass "Blackbox HTTP Probe target and probe_success verified"

"${K[@]}" delete -f "${RUN_DIR}/prometheus-blackbox-probe.yaml" --ignore-not-found --wait=true
"${K[@]}" delete -f "${RUN_DIR}/prometheus-blackbox-app.yaml" --ignore-not-found --wait=true
sed -i 's|^prom_optional_uninstall:.*|prom_optional_uninstall: "yes"|' "$CFG"
"$KUBECLI" setup prometheus-gate 07 </dev/null
"${K[@]}" -n "$NAMESPACE" get deployment thanos-querier >/dev/null 2>&1 &&
  fail "Kubeauto optional uninstall left Thanos Querier behind"
"${K[@]}" -n "$NAMESPACE" get probe -l kubeauto.io/component=prometheus-optional -o json |
  jq -e '.items | length == 0' >/dev/null || fail "Kubeauto optional uninstall left owned Blackbox Probes behind"
"$HELM" -n "$NAMESPACE" status prometheus-adapter --kubeconfig "$KC" >/dev/null 2>&1 &&
  fail "Kubeauto optional uninstall left Adapter release behind"
"$HELM" -n "$NAMESPACE" status blackbox-exporter --kubeconfig "$KC" >/dev/null 2>&1 &&
  fail "Kubeauto optional uninstall left Blackbox release behind"
ready 'app.kubernetes.io/name=prometheus' 2 || fail "Prometheus core did not recover after optional cleanup"
pass "Kubeauto explicit optional uninstall removed owned resources and preserved core"
echo "PROMETHEUS_OPTIONAL_FULL_GATE_PASS"
