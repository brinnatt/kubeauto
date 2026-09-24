#!/usr/bin/env bash
# KubePublishCli live gate. Runs on the authorized Docker control host only.
set -Eeuo pipefail

TOOL=${KUBE_PUBLISH_TOOL:?KUBE_PUBLISH_TOOL is required}
TARGETS=${KUBE_PUBLISH_TARGETS:-192.168.122.217:22 192.168.122.210-210:22 192.168.122.216:22}
NAMESPACE=kubeauto-kp-live
SOURCE_IMAGE=hub.talkedu.cn/kubeauto/pause@sha256:1d048b53f4285cc9d20fbb8d7be785c50e9e4ccf4cf1194d9b176001862d900a
SOURCE_PACK_IMAGE=hub.talkedu.cn/kubeauto/pause:3.10
TEST_IMAGE=127.0.0.1:5000/kubeauto-kp-live:v1
BASE=/tmp/kubeauto-kp-live
PACK_DIR="$BASE/pack"
RECOVERY_DIR="$BASE/recovery"
USER_FILE="$RECOVERY_DIR/customer-kept.txt"

cli() { python3 "$TOOL" "$@"; }

manifest_contains() {
  python3 - "$1" "$2" <<'PY'
import sys
import tarfile

with tarfile.open(sys.argv[1], "r") as archive:
    stream = archive.extractfile("manifest.json")
    if stream is None:
        raise SystemExit("manifest.json is not a regular file")
    manifest = stream.read().decode("utf-8")
raise SystemExit(0 if sys.argv[2] in manifest else 1)
PY
}

remote_cleanup() {
  local host
  for host in 192.168.122.217 192.168.122.210 192.168.122.216; do
    ssh -o BatchMode=yes -o StrictHostKeyChecking=no -o ConnectTimeout=8 "root@$host" \
      "nerdctl -n '$NAMESPACE' rmi '$TEST_IMAGE' >/dev/null 2>&1 || true" || true
  done
}

cleanup() {
  set +e
  remote_cleanup
  docker image rm "$TEST_IMAGE" >/dev/null 2>&1 || true
  rm -rf "$BASE"
}
trap cleanup EXIT INT TERM

expect_failure() {
  if "$@"; then
    echo "KUBE_PUBLISH_EXPECTED_FAILURE_MISSING command=$*" >&2
    return 1
  fi
}

assert_remote_image() {
  local host=$1 expected_id=$2 got_id
  got_id=$(ssh -o BatchMode=yes -o StrictHostKeyChecking=no -o ConnectTimeout=8 "root@$host" \
    "nerdctl -n '$NAMESPACE' image inspect '$TEST_IMAGE' --format '{{.Id}}'")
  [[ "$got_id" == "$expected_id" ]] || {
    echo "KUBE_PUBLISH_IMAGE_ID_MISMATCH host=$host expected=$expected_id got=$got_id" >&2
    return 1
  }
  ssh -o BatchMode=yes -o StrictHostKeyChecking=no -o ConnectTimeout=8 "root@$host" \
    "nerdctl -n '$NAMESPACE' images --format '{{.Repository}}:{{.Tag}}' | grep -Fx '$TEST_IMAGE'"
}

mkdir -p "$PACK_DIR" "$RECOVERY_DIR"
printf '%s\n' customer-data > "$USER_FILE"
remote_cleanup

# Fixture boundary: consume only the matrix-pinned TalkEdu artifact, then own the
# temporary registry tag and every copy distributed by this run.
SOURCE_REPO_DIGESTS=$(docker image inspect "$SOURCE_IMAGE" --format '{{json .RepoDigests}}')
[[ "$SOURCE_REPO_DIGESTS" == *"\"$SOURCE_IMAGE\""* ]] || {
  echo "KUBE_PUBLISH_FIXTURE_DIGEST_MISMATCH image=$SOURCE_IMAGE" >&2
  exit 3
}
SOURCE_ID=$(docker image inspect "$SOURCE_IMAGE" --format '{{.Id}}')
SOURCE_PACK_ID=$(docker image inspect "$SOURCE_PACK_IMAGE" --format '{{.Id}}')
[[ "$SOURCE_PACK_ID" == "$SOURCE_ID" ]] || {
  echo "KUBE_PUBLISH_FIXTURE_TAG_MISMATCH image=$SOURCE_PACK_IMAGE" >&2
  exit 3
}
docker tag "$SOURCE_IMAGE" "$TEST_IMAGE"
docker push "$TEST_IMAGE" >/dev/null
[[ "$(docker image inspect "$TEST_IMAGE" --format '{{.Id}}')" == "$SOURCE_ID" ]]

# download -> pack -> distribute validates Docker input, nerdctl namespace, host:port and range expansion.
printf 'yes\n' | cli --delete "$TEST_IMAGE"
! docker image inspect "$TEST_IMAGE" >/dev/null 2>&1
cli --download "$TEST_IMAGE"
docker image inspect "$TEST_IMAGE" >/dev/null
cli --pack "$TEST_IMAGE" --output-dir "$PACK_DIR" --distribute $TARGETS \
  --remote-runtime nerdctl --namespace "$NAMESPACE" --disable-ssh-host-check
find "$PACK_DIR" -maxdepth 1 -type f -name 'images_batch_*.tar' -print -quit | grep -q .
for host in 192.168.122.217 192.168.122.210 192.168.122.216; do
  assert_remote_image "$host" "$SOURCE_ID"
  ! ssh -o BatchMode=yes -o StrictHostKeyChecking=no -o ConnectTimeout=8 "root@$host" \
    "find /tmp -maxdepth 1 -type d -name 'images_*' -mmin -5 -print -quit | grep -q ."
done

# JSON config is accepted and explicit CLI pack takes precedence over config pack.
cat >"$BASE/config.json" <<EOF
{"pack":["$TEST_IMAGE"]}
EOF
CONFIG_DIR="$BASE/config-pack"
mkdir -p "$CONFIG_DIR"
cli --config "$BASE/config.json" --pack "$SOURCE_PACK_IMAGE" --output-dir "$CONFIG_DIR"
CONFIG_TAR=$(find "$CONFIG_DIR" -maxdepth 1 -type f -name 'images_batch_*.tar' -print -quit)
manifest_contains "$CONFIG_TAR" "$SOURCE_PACK_IMAGE"
! manifest_contains "$CONFIG_TAR" "$TEST_IMAGE"

# Security checks must fail before runtime/SSH execution and not create the sentinel.
SENTINEL=/tmp/kubeauto-kp-live-injected
! test -e "$SENTINEL"
expect_failure cli --download "demo;touch $SENTINEL"
expect_failure cli --distribute "bad;touch $SENTINEL" --tar "$CONFIG_TAR"
expect_failure cli --distribute 192.168.122.217 --tar "$BASE/bad;name.tar"
! test -e "$SENTINEL"

# Delete is restricted to the test tag. A source image remains after local and remote test-tag deletion.
printf 'yes\n' | cli --delete "$TEST_IMAGE" --delete-hosts $TARGETS --remote-runtime nerdctl \
  --namespace "$NAMESPACE" --disable-ssh-host-check
! docker image inspect "$TEST_IMAGE" >/dev/null 2>&1
docker image inspect "$SOURCE_IMAGE" >/dev/null
for host in 192.168.122.217 192.168.122.210 192.168.122.216; do
  ! ssh -o BatchMode=yes -o StrictHostKeyChecking=no -o ConnectTimeout=8 "root@$host" \
    "nerdctl -n '$NAMESPACE' image inspect '$TEST_IMAGE' >/dev/null 2>&1"
done

# A failed second target cleans only this session's generated tar and preserves user data; retry succeeds.
docker pull "$TEST_IMAGE" >/dev/null
expect_failure cli --pack "$TEST_IMAGE" --output-dir "$RECOVERY_DIR" --distribute 192.168.122.217 192.168.122.246:1 \
  --remote-runtime nerdctl --namespace "$NAMESPACE" --disable-ssh-host-check
test -f "$USER_FILE"
! find "$RECOVERY_DIR" -maxdepth 1 -type f -name 'images_batch_*.tar' -print -quit | grep -q .
cli --pack "$TEST_IMAGE" --output-dir "$RECOVERY_DIR" --distribute 192.168.122.217 \
  --remote-runtime nerdctl --namespace "$NAMESPACE" --disable-ssh-host-check
assert_remote_image 192.168.122.217 "$SOURCE_ID"

echo "KUBE_PUBLISH_LIVE_REGRESSION_PASS source=$SOURCE_IMAGE image=$TEST_IMAGE targets=3 namespace=$NAMESPACE"
