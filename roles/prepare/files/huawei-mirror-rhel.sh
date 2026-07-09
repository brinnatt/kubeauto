#!/bin/bash
# Huawei Cloud yum/dnf mirror — RHEL family (see roles/prepare/tasks/main.yml).
# Ref: https://repo.huaweicloud.com/
set -euo pipefail

HUAWEI="https://repo.huaweicloud.com"

[ -f /etc/os-release ] && . /etc/os-release || exit 0
ID="${ID,,}"
MAJOR="${VERSION_ID%%.*}"

is_rhel_family() {
  case "$ID" in
    rhel|redhat|rocky|almalinux|centos|openeuler|anolis) return 0 ;;
  esac
  case "${ID_LIKE:-}" in
    *rhel*|*centos*) return 0 ;;
  esac
  return 1
}

is_rhel_family || exit 0

case "$ID" in
  rocky)     PREFIX="${HUAWEI}/rockylinux" ;;
  almalinux) PREFIX="${HUAWEI}/almalinux" ;;
  centos)
    if [ "$MAJOR" = "7" ]; then PREFIX="${HUAWEI}/centos"; else PREFIX="${HUAWEI}/centos-stream"; fi ;;
  rhel|redhat) PREFIX="${HUAWEI}/rhel" ;;
  openeuler) PREFIX="${HUAWEI}/openeuler" ;;
  anolis)    PREFIX="${HUAWEI}/anolis" ;;
  *)         PREFIX="${HUAWEI}/centos" ;;
esac

shopt -s nullglob
for f in /etc/yum.repos.d/*.repo; do
  sed -i 's|^mirrorlist=|#mirrorlist=|g' "$f"
  sed -i 's|^metalink=|#metalink=|g' "$f"
  sed -i 's|^#\?baseurl=http://dl\.[^/]*/\$contentdir/|baseurl='"${PREFIX}"'/|g' "$f"
  sed -i 's|^#\?baseurl=http://mirror\.centos\.org/|baseurl='"${PREFIX}"'/|g' "$f"
  sed -i 's|^#\?baseurl=http://vault\.centos\.org/|baseurl='"${PREFIX}"'/|g' "$f"
done
for f in /etc/yum.repos.d/epel*.repo; do
  [ -f "$f" ] || continue
  sed -i 's|^mirrorlist=|#mirrorlist=|g' "$f"
  sed -i 's|^metalink=|#metalink=|g' "$f"
  sed -i 's|^#\?baseurl=https\?\://download\.fedoraproject\.org/pub/epel/|baseurl='"${HUAWEI}"'/epel/|g' "$f"
done

if command -v dnf >/dev/null 2>&1; then
  dnf clean all >/dev/null 2>&1 || true
  dnf makecache -y >/dev/null 2>&1 || true
elif command -v yum >/dev/null 2>&1; then
  yum clean all >/dev/null 2>&1 || true
  yum makecache fast >/dev/null 2>&1 || true
fi
