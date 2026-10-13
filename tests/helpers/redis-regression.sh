#!/usr/bin/env bash
set -Eeuo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
PY="${REDIS_PYTHON:-$ROOT/.venv/bin/python}"
[[ -x "$PY" ]] || PY="$(command -v python3)"
MODE="${1:---full}"
CLUSTER="${KUBEAUTO_CLUSTER:-redis-gate}"
CONFIG="${KUBEAUTO_CONFIG:-$ROOT/clusters/$CLUSTER/config.yml}"
CLIENT_POD="${REDIS_CLIENT_POD:-redis-gate-client}"
KC="${KUBECONFIG:-$ROOT/clusters/$CLUSTER/kubectl.kubeconfig}"
K=(kubectl --kubeconfig="$KC")
config_value() { "$PY" "$ROOT/tests/helpers/redis-config.py" "$CONFIG" "$1"; }
if [[ "$MODE" == --config-check ]]; then
  "$PY" "$ROOT/tests/helpers/redis-config.py" "$CONFIG" --check
  exit
fi
if [[ "$MODE" == --preflight || "$MODE" == --full ]]; then
  export KUBEAUTO_BASE="$ROOT" REDIS_PYTHON="$PY"
  exec bash "$ROOT/tests/helpers/redis-full-gate.sh" "$MODE"
fi
# A basic installation smoke cannot sign off the 24-scenario GA matrix.
[[ "$MODE" == --install-only ]] || {
  echo 'REDIS_PREFLIGHT_FAIL class=test-gate reason=unsupported-mode' >&2
  exit 1
}
echo "REDIS_STAGE_BEGIN stage=preflight mode=$MODE"
command -v kubectl >/dev/null
[[ -f "$CONFIG" ]]
REDIS_NS="${REDIS_NAMESPACE:-$(config_value redis_namespace)}"
REDIS_NAME="${REDIS_NAME:-$(config_value redis_name)}"
[[ -n "$REDIS_NS" && -n "$REDIS_NAME" ]]
"$PY" "$ROOT/tests/helpers/redis-config.py" "$CONFIG" --check
"${K[@]}" get nodes -o wide
"${K[@]}" get storageclass
echo "REDIS_STAGE_BEGIN stage=product-entry mode=$MODE"
kubecli download -E redis
kubecli setup "${KUBEAUTO_CLUSTER:?KUBEAUTO_CLUSTER is required}" 07 --tags redis

create_client_pod() {
  local redis_ver password_secret tls_secret
  redis_ver="$(config_value redis_ver)"
  password_secret="$(config_value redis_password_secret)"
  tls_secret="$(config_value redis_tls_secret)"
  if [[ -n "$tls_secret" ]]; then
    REDIS_CLIENT_TLS_ARGS=(--tls --cacert /tls/ca.crt --cert /tls/tls.crt --key /tls/tls.key)
  else
    REDIS_CLIENT_TLS_ARGS=()
  fi
  {
  cat <<EOF
apiVersion: v1
kind: Pod
metadata:
  name: ${CLIENT_POD}
  namespace: ${REDIS_NS}
  labels:
    kubeauto.io/component: redis-test
    kubeauto.io/managed-by: kubeauto
spec:
  restartPolicy: Never
  containers:
    - name: client
      image: registry.talkschool.cn:5000/brinnatt/redis:${redis_ver}
      imagePullPolicy: IfNotPresent
      command: ["sh", "-c", "trap : TERM INT; sleep 86400 & wait"]
EOF
  if [[ -n "$password_secret" ]]; then
    cat <<EOF
      env:
        - name: REDISCLI_AUTH
          valueFrom:
            secretKeyRef:
              name: ${password_secret}
              key: $(config_value redis_password_secret_key)
EOF
  fi
  if [[ -n "$tls_secret" ]]; then
    cat <<EOF
      volumeMounts:
        - name: redis-tls
          mountPath: /tls
          readOnly: true
  volumes:
    - name: redis-tls
      secret:
        secretName: ${tls_secret}
EOF
  fi
  } | "${K[@]}" apply -f - >/dev/null
  "${K[@]}" -n "$REDIS_NS" wait --for=condition=Ready pod/"$CLIENT_POD" --timeout=120s
}

redis_cli() {
  "${K[@]}" -n "$REDIS_NS" exec "$CLIENT_POD" -- redis-cli -e --raw --no-auth-warning "${REDIS_CLIENT_TLS_ARGS[@]}" "$@"
}

run_business_smoke() {
  local mode="$1" key value readback ttl digest service cross slot_ok
  # OpsTree Redis Operator v0.27.0 creates the replication master Service as
  # <RedisReplication name>-master.  The plain <name> Service is a general
  # endpoint and may include replicas, so it is not a safe write target.
  service="$REDIS_NAME-master"
  key="kubeauto:redis:${RUN_ID:-manual}"
  value="redis-business-${RUN_ID:-manual}"
  if [[ "$mode" == cluster ]]; then
    service="$REDIS_NAME-leader"
    redis_cli -c -h "$service" -p 6379 SET "${key}{slot}" "$value" EX 300 | grep -qx OK
    readback="$(redis_cli -c -h "$service" -p 6379 GET "${key}{slot}")"
    ttl="$(redis_cli -c -h "$service" -p 6379 TTL "${key}{slot}")"
    slot_ok="$(redis_cli -c -h "$service" -p 6379 CLUSTER INFO | awk -F: '$1=="cluster_slots_ok" {print $2}' | tr -d '\r')"
    [[ "$slot_ok" == 16384 ]]
    cross="$(redis_cli -c -h "$service" -p 6379 MGET kubeauto:cross:a kubeauto:cross:b 2>&1 || true)"
    grep -q 'CROSSSLOT' <<<"$cross"
    echo "REDIS_CLUSTER_SLOT_PASS slots_ok=$slot_ok"
    redis_cli -c -h "$service" -p 6379 DEL "${key}{slot}" | grep -qx 1
  else
    redis_cli -h "$service" -p 6379 SET "$key" "$value" EX 300 | grep -qx OK
    readback="$(redis_cli -h "$service" -p 6379 GET "$key")"
    ttl="$(redis_cli -h "$service" -p 6379 TTL "$key")"
  fi
  [[ "$readback" == "$value" && "$ttl" -gt 0 ]]
  digest="$(printf '%s' "$readback" | sha256sum | awk '{print $1}')"
  echo "REDIS_DATA_DIGEST_PASS mode=$mode sha256=$digest ttl=$ttl"
  if [[ "$mode" == sentinel ]]; then
    # A standalone RedisSentinel CR creates <CR name>-sentinel on port 26379.
    service="$REDIS_NAME-sentinel-sentinel"
    master="$(redis_cli -h "$service" -p 26379 SENTINEL get-master-addr-by-name mymaster | head -n 1)"
    [[ -n "$master" ]]
    echo "REDIS_SENTINEL_DISCOVERY_PASS master=$master"
    redis_cli -h "$REDIS_NAME-master" -p 6379 DEL "$key" | grep -qx 1
  fi
}

RUN_ID="redis-$(date +%s)"
create_client_pod
echo "REDIS_STAGE_BEGIN stage=control-data"
"${K[@]}" -n "$REDIS_NS" get redis,redisreplication,redissentinel,rediscluster,pod,pvc,svc -o wide
"${K[@]}" -n "$REDIS_NS" get events --sort-by=.lastTimestamp | tail -80 || true
mode="$(config_value redis_mode)"
if [[ "$mode" == sentinel ]]; then
  master="$("${K[@]}" -n "$REDIS_NS" get redisreplication "$REDIS_NAME" -o jsonpath='{.status.masterNode}')"
  [[ -n "$master" ]]
  "${K[@]}" -n "$REDIS_NS" get redissentinel "$REDIS_NAME-sentinel" >/dev/null
else
  state="$("${K[@]}" -n "$REDIS_NS" get rediscluster "$REDIS_NAME" -o jsonpath='{.status.state}')"
  [[ "$state" =~ ^(Ready|ready|Running)$ ]]
fi
run_business_smoke "$mode"
echo "REDIS_BUSINESS_DATA_PASS mode=$mode"
echo "REDIS_STAGE_PASS stage=control-data mode=$mode"
echo REDIS_INSTALL_SMOKE_PASS
