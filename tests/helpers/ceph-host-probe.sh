#!/bin/bash
# Read-only Ceph host and disk qualification. The output allowlist is the sole
# input accepted by later destructive fixtures.
set -euo pipefail

STATE_DIR="${CEPH_STATE_DIR:-/var/lib/kubeauto-ceph-test}"
ALLOWLIST="${CEPH_DISK_ALLOWLIST:-${STATE_DIR}/disk-allowlist}"
EXPECTED_DISKS_PER_HOST="${CEPH_EXPECTED_DISKS_PER_HOST:-3}"
CEPH_HOSTS=(
  192.168.122.135 192.168.122.40 192.168.122.72
  192.168.122.212 192.168.122.165 192.168.122.238
)
declare -A CEPH_HOSTNAMES=(
  [192.168.122.135]=ceph-01 [192.168.122.40]=ceph-02 [192.168.122.72]=ceph-03
  [192.168.122.212]=mceph-01 [192.168.122.165]=mceph-02 [192.168.122.238]=mceph-03
)
CONTRACT_SCRIPT="$(cd "$(dirname "$0")" && pwd)/ceph_contract.py"
SSH=(ssh -o BatchMode=yes -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null
  -o ConnectTimeout=10 -o ServerAliveInterval=5 -o ServerAliveCountMax=2)

fail() {
  echo "CEPH_STAGE_FAIL id=CEPH-03 class=environment reason=$*" >&2
  exit 1
}

[[ "$EXPECTED_DISKS_PER_HOST" =~ ^[1-9][0-9]*$ ]] || fail "invalid expected disk count"
[[ " ${CEPH_HOSTS[*]} " != *" 192.168.122.1 "* ]] || fail "forbidden host in Ceph pool"
[[ -r "$CONTRACT_SCRIPT" ]] || fail "missing parser $CONTRACT_SCRIPT"
install -d -m 0700 "$STATE_DIR"
tmp_allowlist="$(mktemp "${STATE_DIR}/disk-allowlist.XXXXXX")"
trap 'unlink "$tmp_allowlist" 2>/dev/null || true' EXIT

for host in "${CEPH_HOSTS[@]}"; do
  echo "CEPH_STAGE_BEGIN id=CEPH-03 action=probe-host host=$host"
  probe="$(${SSH[@]} "root@${host}" 'set -euo pipefail
    . /etc/os-release
    printf "HOST_META|%s|%s|%s|%s|%s\n" "$(hostname -s)" "${ID:-}" "${VERSION_ID:-}" "$(nproc)" "$(awk '\''/MemTotal/{print $2}'\'' /proc/meminfo)"
    printf "TIME_SYNC|%s\n" "$(timedatectl show -p NTPSynchronized --value 2>/dev/null || echo unknown)"
    printf "RUNTIME|%s\n" "$(command -v podman 2>/dev/null || command -v docker 2>/dev/null || echo missing)"
    root_source=$(findmnt -n -o SOURCE /)
    root_real=$(readlink -f "$root_source")
    while read -r root_device; do
      test -z "$root_device" || printf "ROOT_DEVICE|%s\n" "$root_device"
    done < <(lsblk -srnp -o PATH "$root_real" 2>/dev/null)
    for link in /dev/disk/by-id/*; do
      test -L "$link" || continue
      printf "BYID|%s|%s\n" "$(readlink -f "$link")" "$link"
    done
    for link in /dev/disk/by-path/pci-*; do
      test -L "$link" || continue
      case "$link" in *-part*) continue ;; esac
      target=$(readlink -f "$link")
      path_id=$(udevadm info --query=property --name="$target" | sed -n "s/^ID_PATH=//p")
      test -n "$path_id" || continue
      printf "BYPATH|%s|%s|%s\n" "$target" "$link" "$path_id"
    done
    lsblk -J -b -O' 2>/dev/null)" || fail "ssh or host inventory failed host=$host"

  printf '%s\n' "$probe" | python3 "$CONTRACT_SCRIPT" host-probe \
    --host "$host" --hostname "${CEPH_HOSTNAMES[$host]}" \
    --expected-disks "$EXPECTED_DISKS_PER_HOST" --allow-test-paths >>"$tmp_allowlist" ||
    fail "disk qualification failed host=$host"

  ${SSH[@]} "root@${host}" "
    set -euo pipefail
    test \"\$(timedatectl show -p NTPSynchronized --value 2>/dev/null || echo no)\" = yes
    test -e /dev/mapper/control
    command -v python3 >/dev/null
    command -v lvm >/dev/null
    if test '$host' != '${CEPH_HOSTS[1]}'; then
      ip route get '${CEPH_HOSTS[1]}' | grep -qv ' dev lo '
    fi
  " || fail "time sync, LVM, network, device-mapper or Python prerequisite failed host=$host"
done

expected_lines=$((${#CEPH_HOSTS[@]} * EXPECTED_DISKS_PER_HOST))
actual_lines=$(wc -l <"$tmp_allowlist")
[[ "$actual_lines" -eq "$expected_lines" ]] || fail "allowlist count=$actual_lines expected=$expected_lines"
chmod 0600 "$tmp_allowlist"
mv -f "$tmp_allowlist" "$ALLOWLIST"
trap - EXIT
echo "CEPH_DISK_ALLOWLIST_PASS hosts=${#CEPH_HOSTS[@]} disks=$actual_lines file=$ALLOWLIST"
echo "CEPH_HOST_PROBE_PASS"
