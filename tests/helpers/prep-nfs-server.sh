#!/bin/bash
# Install NFS server exporting /data/nfs (Chinese yum/apt mirrors preferred).
# Usage: prep-nfs-server.sh [export_path]
set -euo pipefail
EXPORT="${1:-/data/nfs}"
mkdir -p "$EXPORT"
chmod 777 "$EXPORT"

if command -v yum >/dev/null 2>&1; then
  # Rocky/RHEL: prefer Huawei mirror if available
  if [ -f /etc/yum.repos.d/Rocky-BaseOS.repo ] && ! grep -q huaweicloud /etc/yum.repos.d/Rocky-BaseOS.repo 2>/dev/null; then
    sed -e 's|^mirrorlist=|#mirrorlist=|g' \
        -e 's|^#baseurl=http://dl.rockylinux.org/$contentdir|baseurl=https://mirrors.huaweicloud.com/rocky|g' \
        -i /etc/yum.repos.d/Rocky-*.repo 2>/dev/null || true
  fi
  yum install -y nfs-utils
  systemctl enable --now nfs-server rpcbind
elif command -v apt-get >/dev/null 2>&1; then
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq
  apt-get install -y nfs-kernel-server
  systemctl enable --now nfs-kernel-server
fi

# export to lab subnet
if ! grep -q "^${EXPORT} " /etc/exports 2>/dev/null; then
  echo "${EXPORT} 192.168.47.0/24(rw,sync,no_subtree_check,no_root_squash)" >> /etc/exports
fi
exportfs -ra
exportfs -v
showmount -e localhost || true
echo "[ok] NFS export ${EXPORT}"
