#!/usr/bin/env bash
# Prove the dual-pushed Ansible execution image and ansible-runner container path.
set -euo pipefail

PRIVATE_IMAGE=hub.talkedu.cn/kubeauto/ansible:2.18.6
FALLBACK_IMAGE=brinnatt/ansible:2.18.6
LABEL=kubeauto.ansible-ee-probe=true
private_before="$(docker image inspect -f '{{.Id}}' "$PRIVATE_IMAGE" 2>/dev/null || true)"
fallback_before="$(docker image inspect -f '{{.Id}}' "$FALLBACK_IMAGE" 2>/dev/null || true)"

cleanup() {
  docker ps -aq --filter "label=$LABEL" | xargs -r docker rm -f >/dev/null 2>&1 || true
  if [[ -z "$fallback_before" ]]; then
    docker image rm "$FALLBACK_IMAGE" >/dev/null 2>&1 || true
  fi
  if [[ -z "$private_before" ]]; then
    docker image rm "$PRIVATE_IMAGE" >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT

pull_with_retry() {
  local image="$1" attempt
  for attempt in 1 2 3; do
    echo "[WAIT] image=$image pull_attempt=$attempt/3"
    docker pull "$image" && return 0
  done
  return 1
}

docker info >/dev/null
if pull_with_retry "$PRIVATE_IMAGE"; then
  runtime_image="$PRIVATE_IMAGE"
  pull_source=talkedu
else
  pull_with_retry "$FALLBACK_IMAGE"
  runtime_image="$FALLBACK_IMAGE"
  pull_source=dockerhub
fi

docker image inspect -f \
  'image_id={{.Id}} entrypoint={{json .Config.Entrypoint}} cmd={{json .Config.Cmd}}' \
  "$runtime_image"
docker run --rm --label "$LABEL" "$runtime_image" ansible --version
docker run --rm --network host --label "$LABEL" \
  -e ANSIBLE_HOST_KEY_CHECKING=False \
  -e HOME=/runner \
  -v /root/.ssh:/root/.ssh:ro \
  "$runtime_image" ansible all -i '192.168.47.128,' -u brinnatt \
  -e ansible_python_interpreter=/usr/bin/python3.13 \
  -m ansible.builtin.ping
docker run --rm --network host --label "$LABEL" \
  -e ANSIBLE_HOST_KEY_CHECKING=False \
  -e HOME=/runner \
  -v /root/.ssh:/root/.ssh:ro \
  "$runtime_image" ansible all -i '192.168.47.128,' -u brinnatt \
  -e ansible_python_interpreter=/usr/bin/python3.13 \
  -m ansible.builtin.setup -a 'filter=ansible_python*'

echo ">>> ansible-runner official execution-environment integration"
cd /usr/local/kubeauto
ANSIBLE_EE_PROBE_IMAGE="$runtime_image" \
  .venv/bin/python tests/helpers/ansible-ee-debian-probe.py

cleanup
trap - EXIT
test -z "$(docker ps -aq --filter "label=$LABEL")"
echo "ANSIBLE_EE_DEBIAN_PROBE_PASS source=$pull_source"
