#!/usr/bin/env bash
# CalicoPolicyCli live gate. Runs on the authorized .243 control node only.
set -Eeuo pipefail

TOOL=${CALICO_TOOL:?CALICO_TOOL is required}
CTX=${CALICO_CONTEXT:-context-cluster1}
NS=monitor
DENY_HOST=${CALICO_DENY_HOST:-192.168.122.193}
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

cli() {
  python3 "$TOOL" --no-log-file --context "$CTX" "$@"
}

cleanup() {
  set +e
  for pid in "${HTTP_PIDS[@]}"; do kill "$pid" 2>/dev/null || true; done
  cli --executor calicoctl delete --traffic-layer host --policy-name "$HOST_POLICY" \
    --delete-hostendpoints --port "$PORT_HOST" >/dev/null 2>&1 || true
  cli --executor calicoctl delete --traffic-layer both --policy-name "$BOTH_POLICY" \
    --delete-hostendpoints --port "$PORT_BOTH" -n "$NS" --k8s-np-name "$BOTH_NP" >/dev/null 2>&1 || true
  cli delete --traffic-layer pod --port "$PORT_POD" -n "$NS" --k8s-np-name "$HOST_NP" >/dev/null 2>&1 || true
  cli delete --traffic-layer pod --port "$PORT_BOTH" -n "$NS" --k8s-np-name "$BOTH_NP" >/dev/null 2>&1 || true
  kubectl delete pod "$POD" "$PROBE" "$DENY_PROBE" -n "$NS" --ignore-not-found >/dev/null 2>&1 || true
  rm -rf "$BACKUP" /tmp/calico-delivery-fail-calicoctl.sh /tmp/calico-delivery-fail-count
}
trap cleanup EXIT INT TERM

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

monitor_clean
rm -rf "$BACKUP"
mkdir -p "$BACKUP"

# Host layer: explicit calicoctl/etcd path, staged apply, backup, post-verify.
cli --executor calicoctl nodes >/dev/null
cli --executor calicoctl plan --traffic-layer host --interface '*' --policy-name "$HOST_POLICY" \
  --hep-prefix "$HOST_PREFIX" -a 192.168.122.243/32 --port "$PORT_HOST" >/tmp/calico-host-plan.yaml
grep -q 'kind: GlobalNetworkPolicy' /tmp/calico-host-plan.yaml
cli --executor calicoctl validate --traffic-layer host --interface '*' \
  -a 192.168.122.243/32 --port "$PORT_HOST"
python3 -m http.server "$PORT_HOST" --bind 0.0.0.0 >/tmp/calico-host-http.log 2>&1 &
HTTP_PIDS+=("$!")
cli --executor calicoctl apply --traffic-layer host --interface '*' --policy-name "$HOST_POLICY" \
  --hep-prefix "$HOST_PREFIX" -a 192.168.122.243/32 --port "$PORT_HOST" --confirm --backup \
  --backup-dir "$BACKUP" --apply-staged --post-verify
test_port 192.168.122.243 "$PORT_HOST" allow
ssh -o BatchMode=yes -o StrictHostKeyChecking=no -o ConnectTimeout=8 root@"$DENY_HOST" \
  "timeout 5 bash -c '</dev/tcp/192.168.122.243/$PORT_HOST'" >/dev/null 2>&1 && {
    echo "CALICO_HOST_DENY_SOURCE_CONNECTED" >&2; exit 1;
  } || true
calicoctl get globalnetworkpolicy "$HOST_POLICY" >/dev/null
cli --executor calicoctl apply --traffic-layer host --interface '*' --policy-name "$HOST_POLICY" \
  --hep-prefix "$HOST_PREFIX" -a 192.168.122.243/32 --port "$PORT_HOST" --confirm --backup \
  --backup-dir "$BACKUP" --apply-staged --post-verify
[[ -d "$BACKUP" ]] && find "$BACKUP" -type f -print -quit | grep -q .
cli --executor calicoctl delete --traffic-layer host --policy-name "$HOST_POLICY" \
  --delete-hostendpoints --port "$PORT_HOST"

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
    image: registry.talkschool.cn:5000/brinnatt/busybox:1.37
    command: ["sh", "-c", "echo calico-delivery > /tmp/index.html; httpd -f -p $PORT_POD -h /tmp & httpd -f -p $PORT_BOTH -h /tmp; wait"]
YAML
kubectl wait --for=condition=Ready pod/"$POD" -n "$NS" --timeout=120s
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
    image: registry.talkschool.cn:5000/brinnatt/busybox:1.37
    command: ["sh", "-c", "sleep 3600"]
YAML
kubectl wait --for=condition=Ready pod/"$PROBE" -n "$NS" --timeout=120s
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
    image: registry.talkschool.cn:5000/brinnatt/busybox:1.37
    command: ["sh", "-c", "sleep 3600"]
YAML
kubectl wait --for=condition=Ready pod/"$DENY_PROBE" -n "$NS" --timeout=120s
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
if kubectl exec -n "$NS" "$DENY_PROBE" -- timeout 5 wget -qO- "http://$POD_IP:$PORT_POD" >/dev/null 2>&1; then
  echo "CALICO_POD_DENY_SOURCE_CONNECTED" >&2
  exit 1
fi
expect_fail cli apply --traffic-layer pod -n "$NS" --pod-label kubeauto-calico-delivery=target \
  --k8s-np-name "$HOST_NP" -a "$PROBE_IP/32" --port "$PORT_POD"
expect_fail cli plan --traffic-layer host --executor calicoctl --interface 'eth0;touch /tmp/calico-injected' \
  -a 192.168.122.243/32 --port 39094
[[ ! -e /tmp/calico-injected ]]
cli delete --traffic-layer pod -n "$NS" --k8s-np-name "$HOST_NP" --port "$PORT_POD"

# Both layer: staged GNP -> HEP -> NetworkPolicy, then idempotent delete.
python3 -m http.server "$PORT_BOTH" --bind 0.0.0.0 >/tmp/calico-both-http.log 2>&1 &
HTTP_PIDS+=("$!")
cli --executor calicoctl apply --traffic-layer both --interface '*' --policy-name "$BOTH_POLICY" \
  --hep-prefix "$HOST_PREFIX" -n "$NS" --pod-label kubeauto-calico-delivery=target \
  --k8s-np-name "$BOTH_NP" -a 192.168.122.243/32 -a "$PROBE_IP/32" --port "$PORT_BOTH" \
  --confirm --apply-staged --post-verify
test_port 192.168.122.243 "$PORT_BOTH" allow
if ssh -o BatchMode=yes -o StrictHostKeyChecking=no -o ConnectTimeout=8 root@"$DENY_HOST" \
  "timeout 5 bash -c '</dev/tcp/192.168.122.243/$PORT_BOTH'" >/dev/null 2>&1; then
  echo "CALICO_BOTH_HOST_DENY_SOURCE_CONNECTED" >&2
  exit 1
fi
kubectl exec -n "$NS" "$PROBE" -- wget -qO- "http://$POD_IP:$PORT_BOTH" | grep -q calico-delivery
if kubectl exec -n "$NS" "$DENY_PROBE" -- timeout 5 wget -qO- "http://$POD_IP:$PORT_BOTH" >/dev/null 2>&1; then
  echo "CALICO_BOTH_POD_DENY_SOURCE_CONNECTED" >&2
  exit 1
fi
cli --executor calicoctl delete --traffic-layer both --policy-name "$BOTH_POLICY" \
  --delete-hostendpoints --port "$PORT_BOTH" -n "$NS" --k8s-np-name "$BOTH_NP"
cli --executor calicoctl delete --traffic-layer both --policy-name "$BOTH_POLICY" \
  --delete-hostendpoints --port "$PORT_BOTH" -n "$NS" --k8s-np-name "$BOTH_NP"

# Stage-stop recovery: GNP is accepted, HEP is rejected, and no later stage runs.
REAL_CALICOCTL=$(command -v calicoctl)
cat >/tmp/calico-delivery-fail-calicoctl.sh <<WRAP
#!/usr/bin/env bash
set -euo pipefail
real="$REAL_CALICOCTL"
if [[ " \$* " == *" apply -f - "* ]]; then
  payload=\$(cat)
  if grep -q 'kind: HostEndpoint' <<<"\$payload"; then
    echo injected-hep-failure >&2
    exit 42
  fi
  printf '%s' "\$payload" | exec "\$real" "\$@"
fi
exec "\$real" "\$@"
WRAP
chmod 0755 /tmp/calico-delivery-fail-calicoctl.sh
expect_fail cli --calicoctl /tmp/calico-delivery-fail-calicoctl.sh --executor calicoctl apply \
  --traffic-layer host --interface '*' --policy-name kubeauto-delivery-cal-fail \
  --hep-prefix kubeauto-delivery-cal-fail-hep -a 192.168.122.243/32 --port 39095 \
  --confirm --apply-staged
calicoctl get globalnetworkpolicy kubeauto-delivery-cal-fail >/dev/null
calicoctl delete globalnetworkpolicy kubeauto-delivery-cal-fail --skip-not-exists >/dev/null

monitor_clean
for p in "$PORT_HOST" "$PORT_POD" "$PORT_BOTH"; do
  ! ss -ltn "sport = :$p" | tail -n +2 | grep -q .
done
echo "CALICO_LIVE_REGRESSION_PASS context=$CTX host=192.168.122.243 deny_source=$DENY_HOST"
