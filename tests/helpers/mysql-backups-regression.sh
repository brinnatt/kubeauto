#!/usr/bin/env bash
# Scoped MySQL backup release on the authorized Ubuntu 138 control host.
set -Eeuo pipefail
stage="${1:?physical|logical|full}"
[[ "$stage" == physical || "$stage" == logical || "$stage" == portability || "$stage" == full ]]
release=/tmp/kubeauto-mysql-backup-release
cd "$release"
exec 9>/run/lock/kubeauto-mysql-backup-release.lock
flock -n 9 || { echo MYSQL_BACKUP_HOST_LEASE_BUSY >&2; exit 2; }
# Refuse foreign/stale fixtures; never clean an unknown previous run.
! docker ps -a --format '{{.Names}}' | grep -Eq '^(tools-mbk-mysql|mylogi-.*-(db|tool|restore|signal))$'
test ! -e /tmp/tools-mbk-live
test ! -e /tmp/kubeauto-mysql-backup-disk
mkdir evidence
cleanup() {
  local rc=$?
  trap - EXIT
  set +e
  cp /tmp/tools-mybackup-file.log evidence/physical-product.log 2>/dev/null
  rm -f /tmp/tools-mybackup-file.log /tmp/mybackup-enospc.out /tmp/mybackup-9x.out
  if mountpoint -q /tmp/kubeauto-mysql-backup-disk; then umount /tmp/kubeauto-mysql-backup-disk || rc=1; fi
  if mountpoint -q /tmp/kubeauto-mysql-backup-disk; then
    echo MYSQL_BACKUP_CLEANUP_FAILED >&2
    rc=1
  else
    rm -rf /tmp/kubeauto-mysql-backup-disk
    rm -f backup.ext4
  fi
  if docker ps -a --format '{{.Names}}' | grep -Eq '^(tools-mbk-mysql|mylogi-.*-(db|tool|restore|signal))$' || test -e /tmp/tools-mbk-live; then
    echo MYSQL_BACKUP_FIXTURE_CLEANUP_FAILED >&2
    rc=1
  fi
  if [[ "$stage" == logical || "$stage" == full ]] && { test -e /etc/systemd/system/mysql-logi-backup.service || test -e /etc/systemd/system/mysql-logi-backup.timer; }; then
    echo MYSQL_BACKUP_TIMER_CLEANUP_FAILED >&2
    rc=1
  fi
  tar -czf release-evidence.tar.gz evidence
  chmod 0644 release-evidence.tar.gz
  if [[ "$rc" -eq 0 ]]; then
    echo 'TOOLS_CLEAN_VERIFY_PASS scope=mysql-backup-release host=138'
    echo "MYSQL_BACKUP_RELEASE_PASS stage=$stage"
  fi
  exit "$rc"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
physical() {
  env MYBACKUP_TOOL="$release/MyBackupCli-rocky8" bash ./mybackup-cli-live-regression.sh | tee evidence/physical.log
  grep -q '^MYBACKUP_NATIVE_BACKUP_PASS$' evidence/physical.log
  grep -q '^MYBACKUP_NATIVE_PREPARE_PASS$' evidence/physical.log
  grep -q '^MYBACKUP_RESTORE_READBACK_PASS$' evidence/physical.log
}
logical() {
  # A runner-owned loop filesystem exercises mount refusal and archive safety.
  # It is a fixture, not a storage-performance qualification or a host disk wipe.
  test ! -e /etc/systemd/system/mysql-logi-backup.service
  test ! -e /etc/systemd/system/mysql-logi-backup.timer
  df --output=avail -B1 /tmp | tail -1 | awk '{exit !($1 > 4000000000)}'
  truncate -s 512M backup.ext4
  mkfs.ext4 -q -F backup.ext4
  mkdir /tmp/kubeauto-mysql-backup-disk
  mount -o loop backup.ext4 /tmp/kubeauto-mysql-backup-disk
  local version image compression
  for version in 8.0.46 8.4.4 9.2.0; do
    case "$version" in
      8.0.46) image='hub.talkedu.cn/kubeauto/mysql@sha256:2f27838ce14a31d6e434efb442658c4ce19a7cd0ec834e329ca213569faa7d3c'; compression=gz;;
      8.4.4) image='hub.talkedu.cn/kubeauto/mysql-8.4@sha256:c26ba5d7363cdae3f0a31665b2ab9106397324dde56ed536364776901b924b83'; compression=bz2;;
      9.2.0) image='hub.talkedu.cn/kubeauto/mysql-9.2@sha256:308515a860be3b21aa44ced3d39f7f91d800efe7ff3719c249f19a89a8480740'; compression=xz;;
    esac
    env MYLOGI_TOOL="$release/MyLogiBackupCli-rocky8" MYLOGI_IMAGE_REF="$image" \
      MYLOGI_VERSION="$version" MYLOGI_COMPRESSION="$compression" \
      MYLOGI_EVIDENCE_DIR="$release/evidence" MYLOGI_REAL_TIMER=yes MYLOGI_OWNED_MOUNT=yes MYLOGI_BACKUP_HOST_DIR=/tmp/kubeauto-mysql-backup-disk \
      bash ./mylogi-backup-live-regression.sh | tee "evidence/logical-$version.log"
    grep -q '^MYLOGI_NATIVE_ARCHIVE_DIAGNOSTICS_PASS ' "evidence/logical-$version.log"
    rm -f "/tmp/mylogi-$version-pull.out" "/tmp/mylogi-$version-negative.out"
  done
}
portability() {
  local image label
  mkdir -p portability
  for label in rocky8 debian12 ubuntu24; do
    case "$label" in
      rocky8) image='docker.sparkcr.cn/rockylinux/rockylinux@sha256:e8a49c5403b687db05d4d67333fa45808fbe74f36e683cec7abb1f7d0f2338c6';;
      debian12) image='swr.cn-north-4.myhuaweicloud.com/ddn-k8s/docker.io/library/debian@sha256:9df39a1d5bfac0249f0eef61a3ff74fcf7576c5947f23127c57e92267ad98ced';;
      ubuntu24) image='swr.cn-north-4.myhuaweicloud.com/ddn-k8s/docker.io/library/ubuntu@sha256:b0c08a4b639b5fca9aa4943ecec614fe241a0cebd1a7b460093ccaeae70df698';;
    esac
    docker pull "$image"
    mkdir "portability/$label"
    docker run --rm -v "$release:/tools:ro" -v "$release/portability/$label:/work" --entrypoint /bin/sh "$image" -ec '
      cd /work
      /tools/MyBackupCli-rocky8 --help >physical.help 2>physical.err & first=$!
      /tools/MyLogiBackupCli-rocky8 --help >logical.help 2>logical.err & second=$!
      wait "$first"; wait "$second"
      test -s physical.help; test -s logical.help
      test ! -s physical.err; test ! -s logical.err
      set -- ./*
      test "$#" -eq 4
    '
    echo "MYSQL_BACKUP_PORTABILITY_PASS runtime=$label tools=2"
  done
}
case "$stage" in
  physical) physical;;
  logical) logical;;
  portability) portability;;
  full) portability; physical; logical;;
esac
