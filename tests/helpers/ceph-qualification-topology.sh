#!/bin/bash
# Fixed, test-only qualification inventory. OSD disk authority stays unchanged.
CEPH_OSD_HOSTS=(192.168.122.135 192.168.122.40 192.168.122.72
  192.168.122.212 192.168.122.165 192.168.122.238)
QUALIFICATION_HOSTS=()

qualification_topology() {
  case "$1" in
    legacy)
      CEPH_HOSTS+=(192.168.47.134 192.168.47.138)
      KUBE_HOSTS+=(192.168.47.138)
      QUALIFICATION_HOSTS=(192.168.47.134 192.168.47.138)
      ;;
    modern)
      CEPH_HOSTS+=(192.168.47.145 192.168.47.146 192.168.47.147)
      # The product supports the same hosts in master and worker groups.
      # Keep all Kubernetes traffic within this reachable lab subnet.
      KUBE_HOSTS=(192.168.47.145 192.168.47.146 192.168.47.147
        192.168.47.145 192.168.47.146 192.168.47.147)
      QUALIFICATION_HOSTS=(192.168.47.145 192.168.47.146 192.168.47.147)
      ;;
    rocky9)
      # RGW and the official RBD node metrics both bind host TCP/8080.
      # Keep RGW on the first two hosts and Kubernetes on the last three.
      KUBE_HOSTS=("${CEPH_OSD_HOSTS[@]:3}" "${CEPH_OSD_HOSTS[@]:3}")
      QUALIFICATION_HOSTS=(192.168.122.238)
      ;;
    *) echo 'Invalid Ceph qualification topology' >&2; return 1 ;;
  esac
}
