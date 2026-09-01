#!/usr/bin/env bash
# Verify every Prometheus branch image before a live cluster consumes it.
set -Eeuo pipefail

images=(
  kube-state-metrics:v2.18.0
  prometheus-webhook-certgen:1.8.5
  prometheus-admission-webhook:v0.93.0
  busybox:1.37
  grafana:13.1.1
  k8s-sidecar:1.30.5
  prometheus-config-reloader:v0.93.0
  prometheus-operator:v0.93.0
  alertmanager:v0.33.1
  node-exporter:v1.12.1
  prometheus:v3.13.1-distroless
  local-path-provisioner:v0.0.31
  json-mock:v1.3.1
  alpine-curl:v7.85.0
)

dockerhub_verify_prefixes_csv="${PROM_ARTIFACT_DOCKERHUB_VERIFY_PREFIX:-docker.io}"
[[ "$dockerhub_verify_prefixes_csv" =~ ^[A-Za-z0-9.-]+(:[0-9]+)?(,[A-Za-z0-9.-]+(:[0-9]+)?)*$ ]] || {
  echo "[FAIL] invalid Docker Hub verification prefix" >&2
  exit 2
}
IFS=, read -r -a dockerhub_verify_prefixes <<<"$dockerhub_verify_prefixes_csv"

command -v docker >/dev/null || {
  echo "[FAIL] Docker is required for Prometheus manifest verification" >&2
  exit 1
}
docker buildx version >/dev/null

inspect_manifest() {
  local reference="$1" output attempt digest
  for attempt in 1 2 3; do
    output="$(timeout 60s docker buildx imagetools inspect \
      --format '{{json .Manifest}}' "$reference" 2>/dev/null || true)"
    if digest="$(jq -er '
      if
        (.digest | type == "string" and test("^sha256:[0-9a-f]{64}$")) and
        (.manifests | type == "array") and
        (any(.manifests[]; .platform.os == "linux" and .platform.architecture == "amd64")) and
        (any(.manifests[]; .platform.os == "linux" and .platform.architecture == "arm64"))
      then .digest else empty end
    ' <<<"$output" 2>/dev/null)" && [[ "$digest" =~ ^sha256:[0-9a-f]{64}$ ]]; then
      printf '%s\n' "$digest"
      return 0
    fi
    echo "PROM_ARTIFACT_RETRY reference=${reference} attempt=${attempt}/3" >&2
    sleep 10
  done
  return 1
}

for image in "${images[@]}"; do
  talkedu="hub.talkedu.cn/kubeauto/${image}"
  talkedu_digest="$(inspect_manifest "$talkedu")" || {
    echo "[FAIL] TalkEdu manifest invalid or missing: $talkedu" >&2
    exit 1
  }
  dockerhub_digest=""
  dockerhub=""
  for dockerhub_verify_prefix in "${dockerhub_verify_prefixes[@]}"; do
    dockerhub="${dockerhub_verify_prefix}/brinnatt/${image}"
    if dockerhub_digest="$(inspect_manifest "$dockerhub")"; then
      break
    fi
  done
  [[ -n "$dockerhub_digest" ]] || {
    echo "[FAIL] Docker Hub manifest invalid or missing: brinnatt/$image" >&2
    exit 1
  }
  [[ "$talkedu_digest" == "$dockerhub_digest" ]] || {
    echo "[FAIL] dual-push digest mismatch image=$image talkedu=$talkedu_digest dockerhub=$dockerhub_digest" >&2
    exit 1
  }
  echo "PROM_ARTIFACT_OK image=${image} digest=${talkedu_digest} platforms=linux/amd64,linux/arm64"
done

echo "PROMETHEUS_ARTIFACT_GATE_PASS images=${#images[@]}"
