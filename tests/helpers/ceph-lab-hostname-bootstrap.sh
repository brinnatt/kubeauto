#!/bin/bash
# Restore the authoritative Ceph lab hostnames after a snapshot reset. This
# helper refuses to rename a host carrying any identity other than localhost.
set -euo pipefail

CEPH_HOSTS=(
  192.168.122.135 192.168.122.40 192.168.122.72
  192.168.122.212 192.168.122.165 192.168.122.238
)
declare -A CEPH_HOSTNAMES=(
  [192.168.122.135]=ceph-01 [192.168.122.40]=ceph-02 [192.168.122.72]=ceph-03
  [192.168.122.212]=mceph-01 [192.168.122.165]=mceph-02 [192.168.122.238]=mceph-03
)
SSH=(ssh -o BatchMode=yes -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null
  -o ConnectTimeout=10 -o ServerAliveInterval=5 -o ServerAliveCountMax=2)

fail() {
  echo "CEPH_LAB_BOOTSTRAP_FAIL class=environment reason=$*" >&2
  exit 1
}

for host in "${CEPH_HOSTS[@]}"; do
  expected="${CEPH_HOSTNAMES[$host]}"
  current="$(${SSH[@]} "root@${host}" hostname -s)" ||
    fail "hostname read failed host=$host"
  case "$current" in
    "$expected")
      echo "CEPH_LAB_HOSTNAME_OK host=$host hostname=$expected changed=no"
      ;;
    localhost|localhost.localdomain)
      ${SSH[@]} "root@${host}" "hostnamectl set-hostname '$expected'" ||
        fail "hostname restore failed host=$host expected=$expected"
      echo "CEPH_LAB_HOSTNAME_OK host=$host hostname=$expected changed=yes"
      ;;
    *)
      fail "refusing identity overwrite host=$host expected=$expected actual=$current"
      ;;
  esac
  actual="$(${SSH[@]} "root@${host}" hostname -s)" ||
    fail "hostname verification failed host=$host"
  [[ "$actual" == "$expected" ]] ||
    fail "hostname mismatch after restore host=$host expected=$expected actual=$actual"
done

echo "CEPH_LAB_HOSTNAME_BOOTSTRAP_PASS hosts=${#CEPH_HOSTS[@]}"
