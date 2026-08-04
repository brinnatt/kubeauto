#!/usr/bin/env bash
# Build the customer kubecli in the oldest supported glibc environment.
set -euo pipefail

SOURCE="${1:-/tmp/kubeauto-rocky8-build-source}"
OUTPUT="${2:-/tmp/kubeauto-rocky8-build-output}"
PRIMARY_IMAGE="docker.sparkcr.cn/rockylinux/rockylinux:8.10"
HUAWEI_IMAGE="swr.cn-north-4.myhuaweicloud.com/ddn-k8s/docker.io/rockylinux/rockylinux:8.10"
FALLBACK_IMAGE="rockylinux/rockylinux:8.10"
CONTAINER="kubeauto-rocky8-kubecli-build"

[[ "$SOURCE" == /tmp/kubeauto-rocky8-build-source ]]
[[ "$OUTPUT" == /tmp/kubeauto-rocky8-build-output ]]
test -f "$SOURCE/kubecli-onefile.spec"
test -f "$SOURCE/build.py"

primary_before="$(docker image inspect --format '{{.Id}}' "$PRIMARY_IMAGE" 2>/dev/null || true)"
huawei_before="$(docker image inspect --format '{{.Id}}' "$HUAWEI_IMAGE" 2>/dev/null || true)"
fallback_before="$(docker image inspect --format '{{.Id}}' "$FALLBACK_IMAGE" 2>/dev/null || true)"
cleanup() {
  docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
  [[ -n "$primary_before" ]] || docker image rm "$PRIMARY_IMAGE" >/dev/null 2>&1 || true
  [[ -n "$huawei_before" ]] || docker image rm "$HUAWEI_IMAGE" >/dev/null 2>&1 || true
  [[ -n "$fallback_before" ]] || docker image rm "$FALLBACK_IMAGE" >/dev/null 2>&1 || true
}
trap cleanup EXIT

rm -rf "$OUTPUT"
install -d -m 0755 "$OUTPUT"
if docker pull "$PRIMARY_IMAGE"; then
  IMAGE="$PRIMARY_IMAGE"
  echo "ROCKY8_BUILD_IMAGE source=sparkcr image=$IMAGE"
elif docker pull "$HUAWEI_IMAGE"; then
  IMAGE="$HUAWEI_IMAGE"
  echo "ROCKY8_BUILD_IMAGE source=huawei image=$IMAGE"
else
  docker pull "$FALLBACK_IMAGE"
  IMAGE="$FALLBACK_IMAGE"
  echo "ROCKY8_BUILD_IMAGE source=dockerhub image=$IMAGE"
fi
docker run --rm --name "$CONTAINER" \
  -e PIP_INDEX_URL=https://repo.huaweicloud.com/repository/pypi/simple \
  -e PIP_TRUSTED_HOST=repo.huaweicloud.com \
  -v "$SOURCE:/src:ro" -v "$OUTPUT:/output" \
  "$IMAGE" bash -lc '
    set -euo pipefail
    cp -a /src /work
    cd /work
    bash roles/prepare/files/huawei-mirror-rhel.sh
    dnf install -y epel-release
    bash roles/prepare/files/huawei-mirror-rhel.sh
    dnf install -y python3.12 python3.12-pip python3.12-devel gcc openssl-devel libffi-devel
    alternatives --install /usr/bin/python3 python3 /usr/bin/python3.12 1
    python3 build.py --kubecli-only
    test "$(getconf GNU_LIBC_VERSION)" = "glibc 2.28"
    dist/kubecli version
    install -m 0755 dist/kubecli /output/kubecli
    sha256sum /output/kubecli
  '

test -x "$OUTPUT/kubecli"
echo ROCKY8_KUBECLI_BUILD_PASS
