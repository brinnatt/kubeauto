#!/usr/bin/env bash
# Exercise the Anolis fallback through the customer binary, then restore Docker.
set -euo pipefail

KUBECLI="${1:-/tmp/kubecli-anolis-container-gate}"
PRIVATE_IMAGE=hub.talkedu.cn/kubeauto/ansible:2.18.6
FALLBACK_IMAGE=brinnatt/ansible:2.18.6
CLEANUP_STARTED=0
SELINUX_CONFIG_BACKUP=
SELINUX_MODE=

test -x "$KUBECLI"

clean_kubeauto_docker() {
  local link target plugin
  systemctl disable --now docker >/dev/null 2>&1 || true
  systemctl disable --now cri-dockerd >/dev/null 2>&1 || true
  pkill -TERM dockerd >/dev/null 2>&1 || true
  for link in /usr/local/bin/*; do
    [[ -L "$link" ]] || continue
    target="$(readlink -f "$link" 2>/dev/null || true)"
    [[ "$target" == /usr/local/kubeauto/docker-bin/* ]] && rm -f "$link"
  done
  for plugin in docker-compose docker-buildx; do
    link="/usr/local/lib/docker/cli-plugins/$plugin"
    [[ -L "$link" ]] || continue
    target="$(readlink -f "$link" 2>/dev/null || true)"
    [[ "$target" == /usr/local/kubeauto/* ]] && rm -f "$link"
  done
  rm -rf /usr/local/kubeauto /data/docker \
    /usr/local/libexec/docker \
    /etc/systemd/system/docker.service /etc/systemd/system/docker.service.d \
    /etc/systemd/system/cri-dockerd.service /etc/docker/daemon.json
  rm -f /root/.local/share/bash-completion/completions/docker
  rmdir --ignore-fail-on-non-empty /etc/docker \
    /root/.local/share/bash-completion/completions \
    /root/.local/share/bash-completion /root/.local/share /root/.local \
    >/dev/null 2>&1 || true
  rm -f /var/run/docker.sock
  while iptables -C FORWARD -s 0.0.0.0/0 -j ACCEPT >/dev/null 2>&1; do
    iptables -D FORWARD -s 0.0.0.0/0 -j ACCEPT || break
  done
  groupdel docker >/dev/null 2>&1 || true
  systemctl daemon-reload
}

# ENV-141 is a clean-snapshot control. Recover residue from an interrupted
# earlier gate before recording the baseline, then require a clean boundary.
clean_kubeauto_docker
if docker info >/dev/null 2>&1; then
  echo "[FAIL] Anolis clean-snapshot Docker precondition was not restored" >&2
  exit 1
fi
SELINUX_MODE="$(getenforce 2>/dev/null || true)"
if [[ -f /etc/selinux/config ]]; then
  SELINUX_CONFIG_BACKUP="$(mktemp /tmp/kubeauto-anolis-selinux.XXXXXX)"
  cp -a /etc/selinux/config "$SELINUX_CONFIG_BACKUP"
fi
private_before=
fallback_before=

cleanup() {
  local initial_rc="${1:-0}" cleanup_rc=0
  (( CLEANUP_STARTED == 0 )) || return "$initial_rc"
  CLEANUP_STARTED=1
  set +e

  if command -v docker >/dev/null 2>&1; then
    docker ps -aq --filter label=kubeauto.ansible-probe=true \
      | xargs -r docker rm -f >/dev/null 2>&1 || cleanup_rc=1
    if [[ -z "$fallback_before" ]]; then
      docker image rm "$FALLBACK_IMAGE" >/dev/null 2>&1 || true
    fi
    if [[ -z "$private_before" ]]; then
      docker image rm "$PRIVATE_IMAGE" >/dev/null 2>&1 || true
    fi
  fi

  clean_kubeauto_docker
  if [[ -n "$SELINUX_CONFIG_BACKUP" && -f "$SELINUX_CONFIG_BACKUP" ]]; then
    cp -a "$SELINUX_CONFIG_BACKUP" /etc/selinux/config || cleanup_rc=1
  fi
  if [[ "$SELINUX_MODE" == Enforcing ]]; then
    setenforce 1 >/dev/null 2>&1 || cleanup_rc=1
  elif [[ "$SELINUX_MODE" == Permissive ]]; then
    setenforce 0 >/dev/null 2>&1 || cleanup_rc=1
  fi
  rm -f "$SELINUX_CONFIG_BACKUP"
  if docker info >/dev/null 2>&1; then
    echo "[FAIL] Anolis gate left a Docker daemon running" >&2
    cleanup_rc=1
  fi

  if (( cleanup_rc == 0 )); then
    echo ANSIBLE_ANOLIS_CONTAINER_CLEAN_PASS
  else
    echo "[FAIL] Anolis container gate cleanup did not restore its baseline" >&2
  fi
  set -e
  (( initial_rc == 0 && cleanup_rc == 0 ))
}

trap 'rc=$?; cleanup "$rc"; final_rc=$?; trap - EXIT; exit "$final_rc"' EXIT

echo ">>> customer binary: kubecli download -d"
"$KUBECLI" download -d </dev/null
docker info >/dev/null
docker compose version
docker buildx version

if docker pull "$PRIVATE_IMAGE"; then
  docker tag "$PRIVATE_IMAGE" "$FALLBACK_IMAGE"
  pull_source=talkedu
else
  docker pull "$FALLBACK_IMAGE"
  pull_source=dockerhub
fi
docker image inspect -f \
  'image_id={{.Id}} digests={{json .RepoDigests}} entrypoint={{json .Config.Entrypoint}} cmd={{json .Config.Cmd}}' \
  "$FALLBACK_IMAGE"
docker run --rm --label kubeauto.ansible-probe=true \
  "$FALLBACK_IMAGE" ansible --version
docker run --rm --label kubeauto.ansible-probe=true \
  "$FALLBACK_IMAGE" ansible localhost -c local -m ping

cleanup 0
trap - EXIT
echo "ANSIBLE_ANOLIS_CONTAINER_GATE_PASS source=$pull_source"
