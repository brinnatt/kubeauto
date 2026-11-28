#!/usr/bin/env bash
# CalicoPolicyCli live gate on a runner-owned, leased CNI fixture.
set -Eeuo pipefail

TOOL=${CALICO_TOOL:?CALICO_TOOL is required}
CTX=${CALICO_CONTEXT:-context-cluster1}
PROBE_IMAGE=${CALICO_PROBE_IMAGE:-hub.talkedu.cn/kubeauto/busybox@sha256:3e0b302381acd9c4092a89b51ccc8727534f044b2b3db17f55575e27f62ec6cc}
NS=kubeauto-tools-calico-live
DENY_HOST=${CALICO_DENY_HOST:-192.168.122.193}
HOST_IP=${CALICO_HOST_IP:-192.168.122.243}
ALLOW_HOST=${CALICO_ALLOW_HOST:-$HOST_IP}
HOST_POLICY=kubeauto-delivery-cal-host-39091
BOTH_POLICY=kubeauto-delivery-cal-both-39093
HOST_PREFIX=kubeauto-delivery-cal-hep
HOST_NP=kubeauto-delivery-cal-pod-39092
BOTH_NP=kubeauto-delivery-cal-pod-39093
BACKUP=/tmp/kubeauto-calico-live-backup
POD=calico-delivery-target
PROBE=calico-delivery-probe
DENY_PROBE=calico-delivery-deny-probe
PORT_HOST=39091
PORT_POD=39092
PORT_BOTH=39093
HTTP_PIDS=()
NS_CREATED=0

cli() {
  if [[ "$TOOL" == *.py ]]; then
    python3 "$TOOL" --no-log-file --context "$CTX" "$@"
  else
    "$TOOL" --no-log-file --context "$CTX" "$@"
  fi
}

cleanup() (
  set +e
  for pid in "${HTTP_PIDS[@]}"; do kill "$pid" 2>/dev/null || true; done
  for pid in "${HTTP_PIDS[@]}"; do wait "$pid" 2>/dev/null || true; done
  cli --executor calicoctl delete --traffic-layer host --policy-name "$HOST_POLICY" \
    --delete-hostendpoints --port "$PORT_HOST" >/dev/null 2>&1 || true
  cli --executor calicoctl delete --traffic-layer both --policy-name "$BOTH_POLICY" \
    --delete-hostendpoints --port "$PORT_BOTH" -n "$NS" --k8s-np-name "$BOTH_NP" >/dev/null 2>&1 || true
  cli delete --traffic-layer pod --port "$PORT_POD" -n "$NS" --k8s-np-name "$HOST_NP" >/dev/null 2>&1 || true
  cli delete --traffic-layer pod --port "$PORT_BOTH" -n "$NS" --k8s-np-name "$BOTH_NP" >/dev/null 2>&1 || true
  kubectl delete pod "$POD" "$PROBE" "$DENY_PROBE" -n "$NS" --ignore-not-found >/dev/null 2>&1 || true
  if [[ "$NS_CREATED" -eq 1 ]]; then
    kubectl delete namespace "$NS" --ignore-not-found --wait=true >/dev/null 2>&1 || true
  fi
  rm -rf "$BACKUP" /tmp/calico-delivery-fail-calicoctl.sh /tmp/calico-delivery-fail-count \
    /tmp/calico-host-plan.yaml /tmp/calico-pod-plan.yaml /tmp/calico-host-http.log \
    /tmp/calico-both-http.log /tmp/calico-delivery-negative.out
)
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

monitor_clean() {
  local bad
  bad=$(kubectl get pods -n "$NS" --no-headers 2>/dev/null | awk '$3 != "Running" && $3 != "Completed" {n++} END {print n+0}')
  [[ "$bad" == 0 ]] || { echo "CALICO_MONITOR_UNHEALTHY count=$bad" >&2; return 1; }
}

expect_fail() {
  if "$@" >/tmp/calico-delivery-negative.out 2>&1; then
    echo "CALICO_EXPECTED_FAILURE_MISSING command=$*" >&2
    return 1
  fi
}

test_port() {
  local host=$1 port=$2 expected=$3
  if timeout 5 bash -c "</dev/tcp/$host/$port" >/dev/null 2>&1; then
    [[ "$expected" == allow ]] || { echo "CALICO_DENY_PATH_ALLOWED $host:$port" >&2; return 1; }
  else
    [[ "$expected" == deny ]] || { echo "CALICO_ALLOW_PATH_BLOCKED $host:$port" >&2; return 1; }
  fi
}

remote_port() {
  local port=$1 expected=$2 source=${3:-$DENY_HOST} output code
  local -a identity=()
  if [[ -n "${CALICO_PROBE_SSH_KEY:-}" ]]; then
    identity=(-i "$CALICO_PROBE_SSH_KEY" -o IdentitiesOnly=yes -o "UserKnownHostsFile=${CALICO_PROBE_KNOWN_HOSTS:?}")
  fi
  # Transport/API failures are not evidence that a network policy denied traffic.
  output=$(ssh "${identity[@]}" -o BatchMode=yes -o StrictHostKeyChecking=yes -o ConnectTimeout=8 root@"$source" \
    "timeout 5 bash -c '</dev/tcp/$HOST_IP/$port' >/dev/null 2>&1; code=\$?; printf 'CALICO_PROBE_RC=%s\\n' \"\$code\"") || return 1
  [[ "$output" =~ ^CALICO_PROBE_RC=([0-9]+)$ ]] || return 1
  code=${BASH_REMATCH[1]}
  case "$expected:$code" in allow:0|deny:1|deny:124) return 0;; esac
  echo "CALICO_HOST_PROBE_MISMATCH expected=$expected rc=$code" >&2
  return 1
}

pod_port() {
  local pod=$1 port=$2 expected=$3 output
  output=$(kubectl exec -n "$NS" "$pod" -- sh -c \
    'wget -T 3 -t 1 -qO- "$1" >/dev/null 2>&1; code=$?; printf "CALICO_PROBE_RC=%s\n" "$code"' \
    sh "http://$POD_IP:$port") || return 1
  case "$expected:$output" in allow:CALICO_PROBE_RC=0|deny:CALICO_PROBE_RC=1) return 0;; esac
  echo "CALICO_POD_PROBE_MISMATCH expected=$expected result=$output" >&2
  return 1
}

wait_host_listener() {
  local port=$1
  for _ in $(seq 1 10); do
    if remote_port "$port" allow && remote_port "$port" allow "$ALLOW_HOST"; then return 0; fi
    sleep 1
  done
  return 1
}

wait_data_path() {
  local operation=$1
  shift
  for _ in $(seq 1 10); do
    if "$operation" "$@"; then return 0; fi
    sleep 1
  done
  return 1
}

wait_pod_ready() {
  local pod=$1
  if ! kubectl wait --for=condition=Ready "pod/$pod" -n "$NS" --timeout=120s; then
    kubectl get pod "$pod" -n "$NS" -o wide >&2 || true
    kubectl describe pod "$pod" -n "$NS" >&2 || true
    kubectl get events -n "$NS" --sort-by=.lastTimestamp >&2 || true
    return 1
  fi
}

existing=$(kubectl get namespace "$NS" --ignore-not-found -o name)
if [[ -n "$existing" ]]; then
  echo "CALICO_LIVE_NAMESPACE_NOT_CLEAN namespace=$NS" >&2
  exit 1
fi
kubectl create namespace "$NS" >/dev/null
NS_CREATED=1
echo "CALICO_LIVE_NAMESPACE_CREATED namespace=$NS"
monitor_clean
rm -rf "$BACKUP"
mkdir -p "$BACKUP"

# Host layer: explicit calicoctl/etcd path, staged apply, backup, post-verify.
cli --executor calicoctl nodes >/dev/null
cli --executor calicoctl plan --traffic-layer host --interface '*' --policy-name "$HOST_POLICY" \
  --hep-prefix "$HOST_PREFIX" -a "$HOST_IP/32" --port "$PORT_HOST" >/tmp/calico-host-plan.yaml
grep -q 'kind: GlobalNetworkPolicy' /tmp/calico-host-plan.yaml
cli --executor calicoctl validate --traffic-layer host --interface '*' \
  -a "$HOST_IP/32" -a "$ALLOW_HOST/32" --port "$PORT_HOST"
python3 -m http.server "$PORT_HOST" --bind 0.0.0.0 >/tmp/calico-host-http.log 2>&1 &
HTTP_PIDS+=("$!")
wait_host_listener "$PORT_HOST"
cli --executor calicoctl apply --traffic-layer host --interface '*' --policy-name "$HOST_POLICY" \
  --hep-prefix "$HOST_PREFIX" -a "$HOST_IP/32" -a "$ALLOW_HOST/32" --port "$PORT_HOST" --confirm --backup \
  --backup-dir "$BACKUP" --apply-staged --post-verify
test_port "$HOST_IP" "$PORT_HOST" allow
wait_data_path remote_port "$PORT_HOST" allow "$ALLOW_HOST"
wait_data_path remote_port "$PORT_HOST" deny
calicoctl get globalnetworkpolicy "$HOST_POLICY" >/dev/null
cli --executor calicoctl apply --traffic-layer host --interface '*' --policy-name "$HOST_POLICY" \
  --hep-prefix "$HOST_PREFIX" -a "$HOST_IP/32" -a "$ALLOW_HOST/32" --port "$PORT_HOST" --confirm --backup \
  --backup-dir "$BACKUP" --apply-staged --post-verify
[[ -d "$BACKUP" ]] && find "$BACKUP" -type f -print -quit | grep -q .
cli --executor calicoctl delete --traffic-layer host --policy-name "$HOST_POLICY" \
  --delete-hostendpoints --port "$PORT_HOST"
wait_data_path remote_port "$PORT_HOST" allow
echo CALICO_HOST_DATAPLANE_PASS

# Pod layer: temporary monitor workload, NetworkPolicy data-path and dry-run contracts.
cat <<YAML | kubectl apply -f -
apiVersion: v1
kind: Pod
metadata:
  name: $POD
  namespace: $NS
  labels:
    kubeauto-calico-delivery: target
spec:
  containers:
  - name: http
    image: $PROBE_IMAGE
    command: ["sh", "-c", "echo calico-delivery > /tmp/index.html; httpd -f -p $PORT_POD -h /tmp & httpd -f -p $PORT_BOTH -h /tmp; wait"]
YAML
wait_pod_ready "$POD"
POD_IP=$(kubectl get pod "$POD" -n "$NS" -o jsonpath='{.status.podIP}')
cat <<YAML | kubectl apply -f -
apiVersion: v1
kind: Pod
metadata:
  name: $PROBE
  namespace: $NS
spec:
  containers:
  - name: probe
    image: $PROBE_IMAGE
    command: ["sh", "-c", "sleep 3600"]
YAML
wait_pod_ready "$PROBE"
PROBE_IP=$(kubectl get pod "$PROBE" -n "$NS" -o jsonpath='{.status.podIP}')
cat <<YAML | kubectl apply -f -
apiVersion: v1
kind: Pod
metadata:
  name: $DENY_PROBE
  namespace: $NS
spec:
  containers:
  - name: probe
    image: $PROBE_IMAGE
    command: ["sh", "-c", "sleep 3600"]
YAML
wait_pod_ready "$DENY_PROBE"
wait_data_path pod_port "$PROBE" "$PORT_POD" allow
wait_data_path pod_port "$DENY_PROBE" "$PORT_POD" allow
cli plan --traffic-layer pod -n "$NS" --pod-label kubeauto-calico-delivery=target \
  --k8s-np-name "$HOST_NP" -a "$PROBE_IP/32" --port "$PORT_POD" >/tmp/calico-pod-plan.yaml
grep -q 'kind: NetworkPolicy' /tmp/calico-pod-plan.yaml
cli apply --traffic-layer pod -n "$NS" --pod-label kubeauto-calico-delivery=target \
  --k8s-np-name "$HOST_NP" -a "$PROBE_IP/32" --port "$PORT_POD" --dry-run=client
cli apply --traffic-layer pod -n "$NS" --pod-label kubeauto-calico-delivery=target \
  --k8s-np-name "$HOST_NP" -a "$PROBE_IP/32" --port "$PORT_POD" --dry-run=server
cli validate --traffic-layer pod -n "$NS" --pod-label kubeauto-calico-delivery=target \
  --k8s-np-name "$HOST_NP" -a "$PROBE_IP/32" --port "$PORT_POD"
cli apply --traffic-layer pod -n "$NS" --pod-label kubeauto-calico-delivery=target \
  --k8s-np-name "$HOST_NP" -a "$PROBE_IP/32" --port "$PORT_POD" --confirm --post-verify
kubectl exec -n "$NS" "$PROBE" -- wget -qO- "http://$POD_IP:$PORT_POD" | grep -q calico-delivery
wait_data_path pod_port "$DENY_PROBE" "$PORT_POD" deny
expect_fail cli apply --traffic-layer pod -n "$NS" --pod-label kubeauto-calico-delivery=target \
  --k8s-np-name "$HOST_NP" -a "$PROBE_IP/32" --port "$PORT_POD"
expect_fail cli plan --traffic-layer host --executor calicoctl --interface 'eth0;touch /tmp/calico-injected' \
  -a "$HOST_IP/32" --port 39094
[[ ! -e /tmp/calico-injected ]]
cli delete --traffic-layer pod -n "$NS" --k8s-np-name "$HOST_NP" --port "$PORT_POD"
wait_data_path pod_port "$DENY_PROBE" "$PORT_POD" allow

echo CALICO_POD_DATAPLANE_PASS

# Both layer: staged GNP -> HEP -> NetworkPolicy, then idempotent delete.
python3 -m http.server "$PORT_BOTH" --bind 0.0.0.0 >/tmp/calico-both-http.log 2>&1 &
HTTP_PIDS+=("$!")
wait_host_listener "$PORT_BOTH"
wait_data_path pod_port "$PROBE" "$PORT_BOTH" allow
wait_data_path pod_port "$DENY_PROBE" "$PORT_BOTH" allow
cli --executor calicoctl apply --traffic-layer both --interface '*' --policy-name "$BOTH_POLICY" \
  --hep-prefix "$HOST_PREFIX" -n "$NS" --pod-label kubeauto-calico-delivery=target \
  --k8s-np-name "$BOTH_NP" -a "$HOST_IP/32" -a "$ALLOW_HOST/32" -a "$PROBE_IP/32" --port "$PORT_BOTH" \
  --confirm --apply-staged --post-verify
test_port "$HOST_IP" "$PORT_BOTH" allow
wait_data_path remote_port "$PORT_BOTH" allow "$ALLOW_HOST"
wait_data_path remote_port "$PORT_BOTH" deny
kubectl exec -n "$NS" "$PROBE" -- wget -qO- "http://$POD_IP:$PORT_BOTH" | grep -q calico-delivery
wait_data_path pod_port "$DENY_PROBE" "$PORT_BOTH" deny
cli --executor calicoctl delete --traffic-layer both --policy-name "$BOTH_POLICY" \
  --delete-hostendpoints --port "$PORT_BOTH" -n "$NS" --k8s-np-name "$BOTH_NP"
wait_data_path remote_port "$PORT_BOTH" allow
wait_data_path pod_port "$DENY_PROBE" "$PORT_BOTH" allow
cli --executor calicoctl delete --traffic-layer both --policy-name "$BOTH_POLICY" \
  --delete-hostendpoints --port "$PORT_BOTH" -n "$NS" --k8s-np-name "$BOTH_NP"

echo CALICO_BOTH_DATAPLANE_PASS

# Stage-stop recovery: GNP is accepted, HEP is rejected, and no later stage runs.
REAL_CALICOCTL=$(command -v calicoctl)
cat >/tmp/calico-delivery-fail-calicoctl.sh <<WRAP
#!/usr/bin/env bash
set -euo pipefail
real="$REAL_CALICOCTL"
if [[ " \$* " == *" apply -f - "* ]]; then
  payload=\$(cat)
  if grep -q 'kind: HostEndpoint' <<<"\$payload"; then
    echo HEP >>/tmp/calico-delivery-fail-count
    echo injected-hep-failure >&2
    exit 42
  fi
  echo GNP >>/tmp/calico-delivery-fail-count
  printf '%s' "\$payload" | exec "\$real" "\$@"
  exit \$?
fi
exec "\$real" "\$@"
WRAP
chmod 0755 /tmp/calico-delivery-fail-calicoctl.sh
expect_fail cli --calicoctl /tmp/calico-delivery-fail-calicoctl.sh --executor calicoctl apply \
  --traffic-layer host --interface '*' --policy-name kubeauto-delivery-cal-fail \
  --hep-prefix kubeauto-delivery-cal-fail-hep -a "$HOST_IP/32" --port 39095 \
  --confirm --apply-staged
grep -q injected-hep-failure /tmp/calico-delivery-negative.out
[[ "$(cat /tmp/calico-delivery-fail-count)" == $'GNP\nHEP' ]]
calicoctl get globalnetworkpolicy kubeauto-delivery-cal-fail >/dev/null
calicoctl delete globalnetworkpolicy kubeauto-delivery-cal-fail --skip-not-exists >/dev/null
echo CALICO_STAGED_FAILURE_RECOVERY_PASS

monitor_clean
cleanup
for p in "$PORT_HOST" "$PORT_POD" "$PORT_BOTH"; do
  if ss -ltn "sport = :$p" | tail -n +2 | grep -q .; then
    echo "CALICO_LISTENER_REMAINS port=$p" >&2; exit 1
  fi
done
remaining=$(kubectl get namespace "$NS" --ignore-not-found -o name)
if [[ -n "$remaining" ]]; then
  echo "CALICO_NAMESPACE_REMAINS namespace=$NS" >&2; exit 1
fi
calicoctl get globalnetworkpolicies -o json | python3 -c 'import json,sys; items=json.load(sys.stdin).get("items",[]); assert not any(x["metadata"]["name"].startswith("kubeauto-delivery-cal-") for x in items)'
calicoctl get hostendpoints -o json | python3 -c 'import json,sys; items=json.load(sys.stdin).get("items",[]); assert not any(x["metadata"]["name"].startswith("kubeauto-delivery-cal-") for x in items)'
echo CALICO_BUSINESS_SCOPED_CLEAN_PASS
echo "CALICO_LIVE_REGRESSION_PASS context=$CTX host=${HOST_IP} deny_source=$DENY_HOST"
