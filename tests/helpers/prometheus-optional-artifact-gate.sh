#!/usr/bin/env bash
# Supply-chain gate for optional Prometheus observability branches.
set -Eeuo pipefail

BASE="${KUBEAUTO_BASE:-/usr/local/kubeauto}"
VERIFY_PREFIXES_CSV="${PROM_ARTIFACT_DOCKERHUB_VERIFY_PREFIX:-docker.io}"
IFS=, read -r -a VERIFY_PREFIXES <<<"$VERIFY_PREFIXES_CSV"
declare -A CHART_SHA=(
  [prometheus-adapter-5.3.0.tgz]=aa6752b6207ed788522714c3ef7f67f27d423eb4d9512fecb52dc641b6434f31
  [prometheus-blackbox-exporter-11.3.1.tgz]=603bafc3688bcc620a602b71f8941e32f8dae963a861da20df085af7864bb120
)
for chart in "${!CHART_SHA[@]}"; do
  path="$BASE/roles/cluster-addon/files/$chart"
  [[ -s "$path" ]] || { echo "[FAIL] optional chart missing: $path" >&2; exit 1; }
  [[ "$(sha256sum "$path" | awk '{print $1}')" == "${CHART_SHA[$chart]}" ]] || {
    echo "[FAIL] optional chart checksum mismatch: $chart" >&2
    exit 1
  }
done

command -v docker >/dev/null || { echo "[FAIL] Docker is required" >&2; exit 1; }
docker buildx version >/dev/null
inspect() {
  local ref="$1" output
  output="$(timeout 60s docker buildx imagetools inspect --format '{{json .Manifest}}' "$ref" 2>/dev/null || true)"
  jq -er '(.digest | type == "string" and test("^sha256:[0-9a-f]{64}$")) and
    (.manifests | type == "array") and
    any(.manifests[]; .platform.os == "linux" and .platform.architecture == "amd64") and
    any(.manifests[]; .platform.os == "linux" and .platform.architecture == "arm64")' <<<"$output" >/dev/null
  jq -er '.digest' <<<"$output"
}

for image in 'thanos:v0.41.0' 'prometheus-adapter:v0.12.0' 'blackbox-exporter:v0.27.0'; do
  talkedu_digest="$(inspect "hub.talkedu.cn/kubeauto/$image")" || {
    echo "[FAIL] TalkEdu optional image manifest missing: $image" >&2; exit 1;
  }
  dockerhub_digest=""
  for prefix in "${VERIFY_PREFIXES[@]}"; do
    if dockerhub_digest="$(inspect "$prefix/brinnatt/$image")"; then break; fi
  done
  [[ -n "$dockerhub_digest" && "$dockerhub_digest" == "$talkedu_digest" ]] || {
    echo "[FAIL] optional dual-push digest mismatch: $image" >&2; exit 1;
  }
  echo "PROM_OPTIONAL_ARTIFACT_OK image=$image digest=$talkedu_digest platforms=linux/amd64,arm64"
done
echo "PROMETHEUS_OPTIONAL_ARTIFACT_GATE_PASS images=3 charts=2"
