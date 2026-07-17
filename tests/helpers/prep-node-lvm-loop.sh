#!/bin/bash
# Prepare OpenEBS LVM VG via loop device when no spare disk exists.
# Usage: prep-node-lvm-loop.sh [vg_name] [size_gb]
# Idempotent. Safe for lab smoke only.
set -euo pipefail
VG="${1:-vg_k8s}"
SIZE_GB="${2:-20}"
IMG="/var/lib/kubeauto-lvm/${VG}.img"
mkdir -p /var/lib/kubeauto-lvm

if vgs "$VG" >/dev/null 2>&1; then
  echo "[ok] VG $VG already exists"
  vgs "$VG"
  exit 0
fi

# Prefer unused whole disk if present
for d in /dev/sdb /dev/sdc /dev/nvme1n1 /dev/vdb; do
  if [ -b "$d" ] && ! lsblk -no MOUNTPOINT "$d" | grep -q .; then
    # no partitions?
    if ! lsblk -no NAME "$d" | tail -n +2 | grep -q .; then
      echo "[info] using spare disk $d for VG $VG"
      yum install -y lvm2 2>/dev/null || apt-get install -y lvm2 2>/dev/null || true
      pvcreate -y "$d"
      vgcreate "$VG" "$d"
      vgs "$VG"
      exit 0
    fi
  fi
done

echo "[info] no spare disk; creating ${SIZE_GB}G loop-backed VG $VG at $IMG"
yum install -y lvm2 2>/dev/null || apt-get update && apt-get install -y lvm2 2>/dev/null || true
if [ ! -f "$IMG" ]; then
  truncate -s "${SIZE_GB}G" "$IMG"
fi
LOOP=$(losetup -f --show "$IMG")
pvcreate -y "$LOOP"
vgcreate "$VG" "$LOOP"
# persist loop on reboot (best-effort)
grep -q "$IMG" /etc/rc.local 2>/dev/null || {
  touch /etc/rc.local
  chmod +x /etc/rc.local
  echo "losetup -f $IMG || true" >> /etc/rc.local
}
vgs "$VG"
echo "[ok] loop VG ready on $LOOP"
