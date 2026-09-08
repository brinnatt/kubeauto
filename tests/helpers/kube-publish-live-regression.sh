#!/usr/bin/env bash
# KubePublishCli live gate. Runs on the authorized Docker control host only.
set -Eeuo pipefail

TOOL=${KUBE_PUBLISH_TOOL:?KUBE_PUBLISH_TOOL is required}
TARGETS=${KUBE_PUBLISH_TARGETS:-192.168.122.217:22 192.168.122.210-210:22 192.168.122.216:22}
NAMESPACE=kubeauto-kp-live
SOURCE_IMAGE=brinnatt/json-mock:v1.3.1
TEST_IMAGE=127.0.0.1:5000/kubeauto-kp-live:v1
BASE=/tmp/kubeauto-kp-live
PACK_DIR="$BASE/pack"
RECOVERY_DIR="$BASE/recovery"
USER_FILE="$RECOVERY_DIR/customer-kept.txt"

cli() { python3 "$TOOL" "$@"; }

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

# Fixture ownership: tag and publish a pre-existing local image under a unique test tag.
docker image inspect "$SOURCE_IMAGE" >/dev/null
docker tag "$SOURCE_IMAGE" "$TEST_IMAGE"
docker push "$TEST_IMAGE" >/dev/null
SOURCE_ID=$(docker image inspect "$TEST_IMAGE" --format '{{.Id}}')

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
cli --config "$BASE/config.json" --pack "$SOURCE_IMAGE" --output-dir "$CONFIG_DIR"
CONFIG_TAR=$(find "$CONFIG_DIR" -maxdepth 1 -type f -name 'images_batch_*.tar' -print -quit)
tar -xOf "$CONFIG_TAR" manifest.json | grep -F "$SOURCE_IMAGE" >/dev/null
! tar -xOf "$CONFIG_TAR" manifest.json | grep -F "$TEST_IMAGE" >/dev/null

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
