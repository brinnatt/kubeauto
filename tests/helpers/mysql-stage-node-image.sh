#!/usr/bin/env bash
# Stage one PXC image through a runtime-only source on a Kubernetes test node.
set -Eeuo pipefail

SOURCE_REF="${MYSQL_STAGE_SOURCE_REF:?MYSQL_STAGE_SOURCE_REF is required}"
TARGET_REF="${MYSQL_STAGE_TARGET_REF:?MYSQL_STAGE_TARGET_REF is required}"
EXPECTED_DIGEST="${MYSQL_STAGE_EXPECTED_DIGEST:?MYSQL_STAGE_EXPECTED_DIGEST is required}"
PLATFORM="${MYSQL_STAGE_PLATFORM:-linux/amd64}"
STATE_DIR="${MYSQL_STAGE_STATE_DIR:-/var/tmp}"
OWNERSHIP_FILE="${STATE_DIR}/kubeauto-mysql-stage-owned"
ACTIVE_BASELINE_FILE="${STATE_DIR}/kubeauto-mysql-stage-active-baseline"
ACTIVE_SOURCE_FILE="${STATE_DIR}/kubeauto-mysql-stage-active-source"

[[ "$SOURCE_REF" =~ ^[a-zA-Z0-9][a-zA-Z0-9._/:@+-]*$ ]] || {
  echo "[FAIL] invalid staging source image reference" >&2
  exit 2
}
[[ "$TARGET_REF" =~ ^[a-zA-Z0-9][a-zA-Z0-9._/:@+-]*$ ]] || {
  echo "[FAIL] invalid staging target image reference" >&2
  exit 2
}
[[ "$EXPECTED_DIGEST" =~ ^sha256:[0-9a-f]{64}$ ]] || {
  echo "[FAIL] expected image digest is invalid" >&2
  exit 2
}
[[ "$PLATFORM" =~ ^[a-z0-9]+/[a-z0-9]+$ ]] || {
  echo "[FAIL] staging platform is invalid" >&2
  exit 2
}
[[ -d "$STATE_DIR" ]] || {
  echo "[FAIL] staging state directory does not exist" >&2
  exit 2
}

source_existed=true
if ! ctr -n k8s.io images inspect "$SOURCE_REF" >/dev/null 2>&1; then
  source_existed=false
fi

[[ ! -e "$ACTIVE_BASELINE_FILE" && ! -e "$ACTIVE_SOURCE_FILE" ]] || {
  echo "[FAIL] stale node-stage content transaction" >&2
  exit 1
}
ctr -n k8s.io content active | awk 'NR > 1 && NF {print $1}' | sort -u >"$ACTIVE_BASELINE_FILE"
printf '%s\n' "$SOURCE_REF" >"$ACTIVE_SOURCE_FILE"

echo "[WAIT] pulling runtime image target=${TARGET_REF##*/} platform=${PLATFORM}"
ctr -n k8s.io images pull --platform "$PLATFORM" "$SOURCE_REF" >/dev/null
rm -f -- "$ACTIVE_BASELINE_FILE" "$ACTIVE_SOURCE_FILE"
inspect_output="$(ctr -n k8s.io images inspect "$SOURCE_REF")"
actual_digest="$(sed -n \
  '/@sha256:[0-9a-f]\{64\}/ { s/.*@\(sha256:[0-9a-f]\{64\}\).*/\1/; p; q; }' \
  <<<"$inspect_output")"
[[ "$actual_digest" == "$EXPECTED_DIGEST" ]] || {
  echo "[FAIL] staged image digest mismatch target=${TARGET_REF##*/}" >&2
  exit 1
}
root_media_type="$(ctr -n k8s.io content get "$actual_digest" \
  | python3 -c 'import json, sys; print(json.load(sys.stdin)["mediaType"])')"
case "$root_media_type" in
  application/vnd.oci.image.manifest.v1+json | \
  application/vnd.docker.distribution.manifest.v2+json)
    platform_digest="$actual_digest"
    platform_media_type="$root_media_type"
    ;;
  application/vnd.oci.image.index.v1+json | \
  application/vnd.docker.distribution.manifest.list.v2+json)
    platform_manifest_line="$(awk -v platform="$PLATFORM" \
      '/@sha256:/ { manifest = $0 } index($0, "Platform: " platform) { print manifest; exit }' \
      <<<"$inspect_output")"
    platform_digest="$(sed -n 's/.*@\(sha256:[0-9a-f]\{64\}\).*/\1/p' \
      <<<"$platform_manifest_line")"
    [[ "$platform_digest" =~ ^sha256:[0-9a-f]{64}$ ]] || {
      echo "[FAIL] ${PLATFORM} manifest digest is missing" >&2
      exit 1
    }
    platform_media_type="$(ctr -n k8s.io content get "$platform_digest" \
      | python3 -c 'import json, sys; print(json.load(sys.stdin)["mediaType"])')"
    ;;
  *)
    echo "[FAIL] unsupported root image media type: ${root_media_type}" >&2
    exit 1
    ;;
esac
case "$platform_media_type" in
  application/vnd.oci.image.manifest.v1+json | \
  application/vnd.docker.distribution.manifest.v2+json) ;;
  *)
    echo "[FAIL] unsupported platform manifest media type: ${platform_media_type}" >&2
    exit 1
    ;;
esac

if [[ "$source_existed" == false ]]; then
  printf '%s\n' "$SOURCE_REF" >>"$OWNERSHIP_FILE"
fi
ctr -n k8s.io images rm "$TARGET_REF" >/dev/null 2>&1 || true
ctr -n k8s.io images tag "$SOURCE_REF" "$TARGET_REF" >/dev/null
ctr -n k8s.io images push --local --plain-http --platform "$PLATFORM" \
  --manifest "$platform_digest" \
  --manifest-type "$platform_media_type" \
  "$TARGET_REF" >/dev/null
registry="${TARGET_REF%%/*}"
repository_tag="${TARGET_REF#*/}"
repository="${repository_tag%:*}"
tag="${repository_tag##*:}"
put_status="$(ctr -n k8s.io content get "$platform_digest" \
  | curl -sS -o /dev/null -w '%{http_code}' -X PUT \
      -H "Content-Type: ${platform_media_type}" \
      --data-binary @- "http://${registry}/v2/${repository}/manifests/${tag}")"
[[ "$put_status" == 201 ]] || {
  echo "[FAIL] registry tag binding failed status=${put_status}" >&2
  exit 1
}
echo "STAGED_NODE_IMAGE_READY target=${TARGET_REF##*/} index_digest=${actual_digest} platform_digest=${platform_digest} media_type=${platform_media_type}"
