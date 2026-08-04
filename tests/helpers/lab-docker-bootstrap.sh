#!/usr/bin/env bash
# Restore the Ubuntu control host's Docker baseline through kubeauto itself.
set -euo pipefail

SOURCE="${1:-/tmp/kubeauto-rocky8-build-source}"
VENV=/tmp/kubeauto-docker-bootstrap-venv

[[ "$SOURCE" == /tmp/kubeauto-rocky8-build-source ]]
test -f "$SOURCE/kubecli.py"
test -f "$SOURCE/requirements-control.txt"

ensure_docker_group() {
  getent group docker >/dev/null 2>&1 || groupadd -r docker
  if id ubuntu >/dev/null 2>&1; then
    usermod -aG docker ubuntu
  fi
}

if docker info >/dev/null 2>&1; then
  ensure_docker_group
  echo LAB_DOCKER_BOOTSTRAP_PASS source=existing
  exit 0
fi

cleanup() {
  rm -rf "$VENV"
}
trap cleanup EXIT

bash "$SOURCE/roles/prepare/files/huawei-mirror-debian.sh"
apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y python3-venv ca-certificates
rm -rf "$VENV"
python3 -m venv "$VENV"
"$VENV/bin/pip" install \
  --index-url https://repo.huaweicloud.com/repository/pypi/simple \
  --trusted-host repo.huaweicloud.com \
  -r "$SOURCE/requirements-control.txt"

env PYTHONPATH="$SOURCE" PATH="$VENV/bin:/usr/local/bin:/usr/bin:/bin:/usr/local/sbin:/usr/sbin:/sbin" \
  "$VENV/bin/python" "$SOURCE/kubecli.py" download -d </dev/null
docker info >/dev/null
ensure_docker_group
cleanup
trap - EXIT
echo LAB_DOCKER_BOOTSTRAP_PASS source=kubeauto-huawei
