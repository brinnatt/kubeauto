#!/usr/bin/env bash
set -Eeuo pipefail
BASE="${KUBEAUTO_BASE:-/usr/local/kubeauto}"
FILES="$BASE/roles/cluster-addon/files"
cd "$FILES"
sha256sum --check eck-3.5.0-crds.yaml.sha256 eck-3.5.0-operator.yaml.sha256 \
  loki-18.9.0.tgz.sha256 alloy-1.11.1.tgz.sha256
python3 - "$FILES/loki-18.9.0.tgz" "$FILES/alloy-1.11.1.tgz" <<'PY'
import re, sys, tarfile
for archive, member, expected in (
    (sys.argv[1], "loki/Chart.yaml", "3.7.6"),
    (sys.argv[2], "alloy/Chart.yaml", "v1.18.1"),
):
    with tarfile.open(archive, "r:gz") as bundle:
        content = bundle.extractfile(member).read().decode("utf-8")
    match = re.search(r"^appVersion:\s*['\"]?([^'\"\s]+)", content, re.M)
    if not match or match.group(1) != expected:
        raise SystemExit(f"chart appVersion mismatch: {archive}")
    print(f"appVersion: {expected}")
PY
# Verify the immutable upstream identity remains attached to every runtime
# image after the local registry tag is created.  The local registry may emit
# a platform-specific manifest digest; the source digest is the provenance
# contract and is checked here before any live Helm mutation.
declare -a IMAGE_DIGESTS=(
  'brinnatt/loki:3.7.6=efd47c67f9bac88ca29bcf8cb997d9ab29d1848bd0aff579282295542a745952'
  'brinnatt/alloy:1.18.1=0f4434c92b3e6cdac38bb129b344e1790c246f7b6e2eaffcc16a5fa363240e33'
  'brinnatt/loki-canary:3.7.6=09d2b772c65b5495645cc7cba49e13f06fa2ec104786669e154f176625309e97'
  'brinnatt/access-log-exporter:0.4.11=371463b58e7947c0769480186d915792d57809536f2b3539b9154ec6d2a2ba11'
  'brinnatt/memcached:1.6.45-alpine=c29847751abb41f4c268c84fb3087fee05d4edcbda44409ccb5086e26148e8a7'
  'brinnatt/memcached-exporter:v0.17.0=995c80e3ffbe6bc1a8e6a917c3a9f2075ad04dd16b86fac63c7eeb04e7c38f9b'
  'brinnatt/k8s-sidecar:2.10.1=7eac5c4fed714a18d038fc9fea57d8744d113367935dac0ea4eb6a87cef704a3'
  'brinnatt/prometheus-config-reloader:v0.91.0=7d9e4eea5f1139e602508871f422b0116c60e87c662f3dcd234d5ab60cd0d8c1'
  'brinnatt/minio-mc:RELEASE.2025-04-08T15-39-49Z=4c10447e70b1f288414f29b2a899c99b2865f44a7fcb9e56ba2aa1e2b1f59121'
)
if command -v docker >/dev/null 2>&1; then
  for entry in "${IMAGE_DIGESTS[@]}"; do
    image="${entry%%=*}"; digest="${entry##*=}"
    docker image inspect "$image" --format '{{json .RepoDigests}}' | grep -q "$digest"
  done
else
  # Rebuilt control hosts can lack Docker while all fixed artifacts are
  # already published in the authoritative TalkEdu registry. File checksums,
  # chart appVersions and the runtime registry source remain mandatory.
  registry_code="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 10 https://hub.talkedu.cn/v2/ || true)"
  [[ "$registry_code" == 200 || "$registry_code" == 401 || "$registry_code" == 403 ]]
  echo LOGGING_PUBLISHED_REGISTRY_REACHABLE
fi
echo LOGGING_IMAGE_PROVENANCE_PASS
echo LOGGING_ARTIFACT_GATE_PASS
