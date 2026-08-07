#!/usr/bin/env bash
# Verify that local_registry survives a real control-host reboot without kubecli intervention.
# The top-level runner invokes prepare before reboot and verify after SSH returns.
set -euo pipefail

BASE="${KUBEAUTO_BASE:-/usr/local/kubeauto}"
PROJECT_PY="$BASE/.venv/bin/python"
STATE_FILE=/var/tmp/kubeauto-registry-reboot.expected
FIXTURE_REPOSITORY=kubeauto/registry-reboot-fixture
FIXTURE_TAG=2
MODE="${1:?usage: registry-reboot-gate.sh prepare|verify}"

pass() { echo "[PASS] $*"; }
fail() { echo "[FAIL] $*" >&2; exit 1; }

wait_for_docker() {
  local attempt
  for attempt in $(seq 1 60); do
    if docker info >/dev/null 2>&1; then
      return 0
    fi
    echo "[WAIT] docker daemon attempt=$attempt/60 next_check=2s"
    sleep 2
  done
  fail "Docker daemon did not become ready"
}

wait_for_registry() {
  local attempt
  for attempt in $(seq 1 60); do
    if curl -fsS --max-time 2 http://127.0.0.1:5000/v2/ >/dev/null; then
      return 0
    fi
    echo "[WAIT] local_registry HTTP attempt=$attempt/60 next_check=2s"
    sleep 2
  done
  docker ps -a --filter name=local_registry || true
  fail "local_registry did not recover automatically"
}

manifest_digest() {
  curl -fsS -D - -o /dev/null --max-time 10 \
    -H 'Accept: application/vnd.docker.distribution.manifest.v2+json' \
    "http://127.0.0.1:5000/v2/${FIXTURE_REPOSITORY}/manifests/${FIXTURE_TAG}" |
    awk 'BEGIN {IGNORECASE=1} /^Docker-Content-Digest:/ {gsub("\r", "", $2); print $2}' |
    tail -n 1
}

assert_restart_policy() {
  local policy
  policy="$(docker inspect local_registry --format '{{.HostConfig.RestartPolicy.Name}}')"
  echo "local_registry_restart_policy=$policy"
  [[ "$policy" == always ]] || fail "expected RestartPolicy=always, got $policy"
}

case "$MODE" in
  prepare)
    [[ -x "$PROJECT_PY" ]] || fail "project Python missing: $PROJECT_PY"
    export PYTHONPATH="$BASE" PATH="/usr/local/bin:/usr/bin:$PATH"
    wait_for_docker
    kubecli download -D </dev/null
    wait_for_registry
    assert_restart_policy

    registry_version="$($PROJECT_PY -c 'from common.constants import KubeConstant; print(KubeConstant().v_docker_registry)')"
    source_image="brinnatt/registry:${registry_version}"
    docker image inspect "$source_image" >/dev/null
    docker tag "$source_image" "127.0.0.1:5000/${FIXTURE_REPOSITORY}:${FIXTURE_TAG}"
    docker push "127.0.0.1:5000/${FIXTURE_REPOSITORY}:${FIXTURE_TAG}"
    expected_digest="$(manifest_digest)"
    [[ "$expected_digest" == sha256:* ]] || fail "fixture manifest digest missing"
    printf '%s\n' "$expected_digest" > "${STATE_FILE}.tmp"
    mv "${STATE_FILE}.tmp" "$STATE_FILE"
    echo "registry_fixture_digest=$expected_digest"
    pass "local_registry prepared for host reboot"
    echo REGISTRY_REBOOT_PREP_PASS
    ;;
  verify)
    [[ -s "$STATE_FILE" ]] || fail "reboot evidence state missing: $STATE_FILE"
    wait_for_docker
    # Deliberately do not invoke kubecli here: Docker must restore the container itself.
    wait_for_registry
    assert_restart_policy
    running="$(docker inspect local_registry --format '{{.State.Running}}')"
    [[ "$running" == true ]] || fail "local_registry is not running after reboot"
    expected_digest="$(cat "$STATE_FILE")"
    actual_digest="$(manifest_digest)"
    echo "registry_fixture_digest_expected=$expected_digest"
    echo "registry_fixture_digest_actual=$actual_digest"
    [[ "$actual_digest" == "$expected_digest" ]] || fail "registry fixture changed across reboot"
    pass "local_registry automatically recovered with persistent content"
    echo REGISTRY_HOST_REBOOT_PASS
    ;;
  *)
    fail "unknown mode: $MODE"
    ;;
esac
