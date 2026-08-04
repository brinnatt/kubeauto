#!/bin/bash
# Prove that a newly installed cluster can run and serve an application.
#
# This is intentionally stronger than checking Node/Pod Ready.  It schedules a
# real HTTP service, discovers it through cluster DNS from another node, then
# performs an application write/read round trip through the ClusterIP data path.
set -euo pipefail

KUBECTL="${KUBECTL:-kubectl}"
NAMESPACE="${PRODUCTION_SMOKE_NAMESPACE:-kubeauto-production-smoke}"
IMAGE="${PRODUCTION_SMOKE_IMAGE:?PRODUCTION_SMOKE_IMAGE is required}"
SERVER_NODE="${PRODUCTION_SMOKE_SERVER_NODE:-}"
CLIENT_NODE="${PRODUCTION_SMOKE_CLIENT_NODE:-}"

if [[ -z "$SERVER_NODE" ]]; then
  SERVER_NODE=$(
    "$KUBECTL" get nodes --no-headers \
      | awk '$2 == "Ready" {print $1; exit}'
  )
fi
if [[ -z "$SERVER_NODE" ]]; then
  echo "No Ready schedulable node is available for the production smoke server" >&2
  exit 1
fi

if [[ -z "$CLIENT_NODE" ]]; then
  CLIENT_NODE=$(
    "$KUBECTL" get nodes --no-headers \
      | awk -v server="$SERVER_NODE" '$1 != server && $2 ~ /^Ready/ {print $1; exit}'
  )
fi
CLIENT_NODE="${CLIENT_NODE:-$SERVER_NODE}"

cleanup() {
  "$KUBECTL" delete namespace "$NAMESPACE" \
    --ignore-not-found --wait=true --timeout=180s >/dev/null 2>&1 || true
}

diagnose() {
  rc=$?
  echo "[DIAG] production smoke failed rc=$rc server_node=$SERVER_NODE client_node=$CLIENT_NODE" >&2
  "$KUBECTL" -n "$NAMESPACE" get all,endpoints -o wide 2>/dev/null || true
  "$KUBECTL" -n "$NAMESPACE" describe pods 2>/dev/null || true
  "$KUBECTL" -n "$NAMESPACE" logs job/production-client --all-containers --tail=100 2>/dev/null || true
  exit "$rc"
}

trap cleanup EXIT
trap diagnose ERR
cleanup

"$KUBECTL" create namespace "$NAMESPACE"
"$KUBECTL" -n "$NAMESPACE" apply -f - <<EOF
apiVersion: apps/v1
kind: Deployment
metadata:
  name: production-server
spec:
  replicas: 1
  selector:
    matchLabels:
      app: production-server
  template:
    metadata:
      labels:
        app: production-server
    spec:
      nodeSelector:
        kubernetes.io/hostname: ${SERVER_NODE}
      containers:
        - name: server
          image: ${IMAGE}
          imagePullPolicy: IfNotPresent
          env:
            - name: PORT
              value: "8080"
          ports:
            - name: http
              containerPort: 8080
          readinessProbe:
            httpGet:
              path: /public
              port: http
            periodSeconds: 3
            timeoutSeconds: 2
          livenessProbe:
            httpGet:
              path: /public
              port: http
            periodSeconds: 10
            timeoutSeconds: 2
---
apiVersion: v1
kind: Service
metadata:
  name: production-server
spec:
  selector:
    app: production-server
  ports:
    - name: http
      port: 8080
      targetPort: http
EOF

"$KUBECTL" -n "$NAMESPACE" rollout status deployment/production-server --timeout=300s
endpoint_ip=$(
  "$KUBECTL" -n "$NAMESPACE" get endpointslice \
    -l kubernetes.io/service-name=production-server \
    -o jsonpath='{.items[0].endpoints[0].addresses[0]}'
)
test -n "$endpoint_ip"
echo "PRODUCTION_SMOKE_ENDPOINT_READY endpoint=$endpoint_ip server_node=$SERVER_NODE"

"$KUBECTL" -n "$NAMESPACE" apply -f - <<EOF
apiVersion: batch/v1
kind: Job
metadata:
  name: production-client
spec:
  backoffLimit: 0
  template:
    metadata:
      labels:
        app: production-client
    spec:
      restartPolicy: Never
      nodeName: ${CLIENT_NODE}
      containers:
        - name: client
          image: ${IMAGE}
          imagePullPolicy: IfNotPresent
          command: ["bash", "-ceu"]
          args:
            - |
              url=http://production-server.${NAMESPACE}.svc.cluster.local:8080
              for attempt in \$(seq 1 30); do
                if curl -fsS --connect-timeout 5 "\$url/public" | grep -q 'public information'; then
                  break
                fi
                test "\$attempt" -lt 30
                sleep 2
              done
              curl -fsS --connect-timeout 5 \
                -H 'Content-Type: application/json' \
                -d '{"body":"kubeauto-production-write"}' \
                "\$url/public" | grep -q 'kubeauto-production-write'
              curl -fsS --connect-timeout 5 "\$url/public/2" \
                | grep -q 'kubeauto-production-write'
              echo PRODUCTION_SMOKE_HTTP_RW_OK
EOF

"$KUBECTL" -n "$NAMESPACE" wait --for=condition=complete \
  job/production-client --timeout=300s
client_log=$("$KUBECTL" -n "$NAMESPACE" logs job/production-client)
printf '%s\n' "$client_log"
grep -q '^PRODUCTION_SMOKE_HTTP_RW_OK$' <<<"$client_log"

echo "KUBERNETES_PRODUCTION_SMOKE_PASS server_node=$SERVER_NODE client_node=$CLIENT_NODE"
