#!/bin/bash
# Huawei Cloud apt mirror — Debian + Ubuntu (see roles/prepare/tasks/main.yml).
# Ref: https://repo.huaweicloud.com/
set -euo pipefail

HUAWEI="https://repo.huaweicloud.com"

[ -f /etc/os-release ] && . /etc/os-release || exit 0
ID="${ID,,}"
CODENAME="${VERSION_CODENAME:-}"
UBUNTU_CODENAME="${UBUNTU_CODENAME:-$CODENAME}"

case "$ID" in
  debian)
    [ -n "$CODENAME" ] || exit 0
    cat > /etc/apt/sources.list <<EOF
deb ${HUAWEI}/debian/ ${CODENAME} main contrib non-free non-free-firmware
deb ${HUAWEI}/debian/ ${CODENAME}-updates main contrib non-free non-free-firmware
deb ${HUAWEI}/debian-security/ ${CODENAME}-security main contrib non-free non-free-firmware
EOF
    rm -f /etc/apt/sources.list.d/debian.sources
    ;;
  ubuntu)
    [ -n "$UBUNTU_CODENAME" ] || exit 0
    cat > /etc/apt/sources.list <<EOF
deb ${HUAWEI}/ubuntu/ ${UBUNTU_CODENAME} main restricted universe multiverse
deb ${HUAWEI}/ubuntu/ ${UBUNTU_CODENAME}-updates main restricted universe multiverse
deb ${HUAWEI}/ubuntu/ ${UBUNTU_CODENAME}-backports main restricted universe multiverse
deb ${HUAWEI}/ubuntu/ ${UBUNTU_CODENAME}-security main restricted universe multiverse
EOF
    rm -f /etc/apt/sources.list.d/ubuntu.sources 2>/dev/null || true
    ;;
  *)
    exit 0
    ;;
esac

find /etc/apt/sources.list.d -maxdepth 1 -name '*.list' -delete 2>/dev/null || true
