#!/bin/bash
# Scoped cleanup for the runner-owned Ceph FSID and fault-injection mappings.
set -euo pipefail

MODE="${1:-clean}"
[[ "$MODE" == clean || "$MODE" == --verify ]] || {
  echo "usage: $0 [--verify]" >&2
  exit 2
}

STATE_DIR="${CEPH_STATE_DIR:-/var/lib/kubeauto-ceph-test}"
BASE="${KUBEAUTO_BASE:-/usr/local/kubeauto}"
CLUSTER_DIR="${BASE}/clusters/ceph-gate"
ALLOWLIST="${CEPH_DISK_ALLOWLIST:-${STATE_DIR}/disk-allowlist}"
FSID_FILE="${STATE_DIR}/fsid"
MAPPER_FILE="${STATE_DIR}/mappers"
OWNER_FILE="${STATE_DIR}/owner"
OWNER_VALUE=kubeauto-ceph-regression
CEPH_HOSTS=(
  192.168.122.135 192.168.122.40 192.168.122.72
  192.168.122.212 192.168.122.165 192.168.122.238
)
KUBE_HOSTS=(
  192.168.47.134 192.168.47.135 192.168.47.136
  192.168.47.131 192.168.47.132 192.168.47.137
)
SSH_BIN="${CEPH_SSH_BIN:-ssh}"
SSH=("$SSH_BIN" -o BatchMode=yes -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null
  -o ConnectTimeout=10 -o ServerAliveInterval=5 -o ServerAliveCountMax=2)

# The regression EXIT handler and outer supervisor may both request cleanup.
if [[ "$MODE" == clean && -d "$STATE_DIR" ]]; then
  exec 8>"${STATE_DIR}/cleanup.lock"
  echo CEPH_CLEAN_LOCK_WAIT
  flock -w 1200 8 || {
    echo 'CEPH_STAGE_FAIL id=CEPH-28 class=test-gate reason=cleanup lock timeout' >&2
    exit 1
  }
fi

fail() {
  echo "CEPH_STAGE_FAIL id=CEPH-28 class=test-gate reason=$*" >&2
  exit 1
}

validate_host() {
  local host="$1" allowed
  allowed=false
  for candidate in "${CEPH_HOSTS[@]}"; do
    [[ "$host" == "$candidate" ]] && allowed=true
  done
  [[ "$allowed" == true && "$host" != 192.168.122.1 ]] || fail "unapproved host=$host"
}

validate_allowlist() {
  [[ -s "$ALLOWLIST" ]] || fail "missing disk allowlist $ALLOWLIST"
  local host kernel_path stable_path serial wwn identity size current_serial current_wwn current_path
  local lines=0
  declare -A host_counts=() paths=() identities=()
  while IFS='|' read -r host kernel_path stable_path serial wwn identity size; do
    validate_host "$host"
    [[ "$kernel_path" =~ ^/dev/[A-Za-z0-9_.:+/-]+$ ]] || fail "invalid kernel path host=$host"
    [[ "$stable_path" =~ ^/dev/disk/by-id/[A-Za-z0-9_.:+-]+$ ||
       "$stable_path" =~ ^/dev/disk/by-path/pci-[A-Za-z0-9_.:+-]+$ ]] ||
      fail "unstable allowlist path=$stable_path"
    [[ "$identity" =~ ^[A-Za-z0-9_.:+-]+$ ]] || fail "invalid disk identity host=$host"
    [[ "$size" =~ ^[1-9][0-9]*$ ]] || fail "invalid disk size host=$host path=$stable_path"
    [[ -z "${paths["$host|$stable_path"]:-}" ]] || fail "duplicate disk path host=$host path=$stable_path"
    [[ -z "${identities["$host|$identity"]:-}" ]] || fail "duplicate disk identity host=$host identity=$identity"
    paths["$host|$stable_path"]=1
    identities["$host|$identity"]=1
    host_counts["$host"]=$(( ${host_counts["$host"]:-0} + 1 ))
    lines=$((lines + 1))
    if [[ "$stable_path" == /dev/disk/by-path/* ]]; then
      current_path="$("${SSH[@]}" "root@${host}" "test -b '$stable_path'; udevadm info --query=property --name='$stable_path' | sed -n 's/^ID_PATH=//p'" </dev/null)" ||
        fail "cannot re-read allowlisted disk host=$host path=$stable_path"
      [[ "$identity" == "path:$current_path" && "${stable_path##*/}" == "$current_path" ]] ||
        fail "disk path identity changed host=$host path=$stable_path"
    else
      read -r current_serial current_wwn < <(
        "${SSH[@]}" "root@${host}" "test -b '$stable_path'; lsblk -dn -o SERIAL,WWN '$stable_path' | xargs" </dev/null
      ) || fail "cannot re-read allowlisted disk host=$host path=$stable_path"
      [[ "$identity" == "$current_serial" || "$identity" == "$current_wwn" ]] ||
        fail "disk identity changed host=$host path=$stable_path"
    fi
  done <"$ALLOWLIST"
  [[ "$lines" -eq 18 ]] || fail "allowlist line count=$lines expected=18"
  for host in "${CEPH_HOSTS[@]}"; do
    [[ "${host_counts[$host]:-0}" -eq 3 ]] || fail "allowlist disk count host=$host count=${host_counts[$host]:-0}"
  done
}

cleanup_kubernetes_runtime() {
  local host
  for host in "${KUBE_HOSTS[@]}"; do
    "${SSH[@]}" "root@${host}" 'if command -v python3 >/dev/null; then exec python3 - --clean; else exec /usr/libexec/platform-python - --clean; fi' \
      <"$BASE/tests/helpers/ceph_runtime_cleanup.py" || fail "stopped Kubernetes fixture cleanup failed host=$host"
  done
}

verify_clean() {
  local fsid="${CEPH_TEST_FSID:-}" host mapper_host mapper_name backing
  [[ ! -e "$CLUSTER_DIR" ]] || fail "runner-owned Kubernetes cluster directory remains"
  [[ -n "$fsid" ]] || [[ ! -s "$FSID_FILE" ]] || fsid="$(<"$FSID_FILE")"
  if [[ -n "$fsid" ]]; then
    [[ "$fsid" =~ ^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$ ]] || fail "invalid FSID"
  fi
  for host in "${CEPH_HOSTS[@]}"; do
    validate_host "$host"
    "${SSH[@]}" "root@${host}" "
      set -euo pipefail
      test -z '$fsid' || test ! -e '/var/lib/ceph/$fsid'
      units=\$(systemctl list-unit-files --no-legend) || exit 1
      if test -n '$fsid' && grep -Fq 'ceph-$fsid.target' <<<\"\$units\"; then exit 1; fi
      mappings=\$(dmsetup ls --noheadings) || exit 1
      if awk '{print \$1}' <<<\"\$mappings\" | grep -Eq '^kubeauto-ceph-(slow|throttle|lab)-'; then exit 1; fi
      for alias in /dev/mapper/kubeauto-ceph-*; do
        if test -L \"\$alias\"; then exit 1; fi
      done
      if test -f /root/.ssh/authorized_keys && grep -Fq ' kubeauto-cephadm' /root/.ssh/authorized_keys; then exit 1; fi
      test ! -e /etc/ceph/kubeauto-owned-ssh-user
      test ! -e /etc/sudoers.d/kubeauto-cephadm
      test ! -e /home/cephadm
      if test '${CEPH_CLEAN_PREVERIFY:-0}' = 1 && test '$host' = '${CEPH_HOSTS[0]}'; then
        test \"\$(cat /etc/ceph/kubeauto-owned-fsid 2>/dev/null)\" = '$fsid'
      else
        test ! -e /etc/ceph/kubeauto-owned-fsid
      fi
      test ! -e /etc/kubernetes
      if systemctl is-active --quiet kubelet; then exit 1; fi
      if test -d /var/lib/ceph; then
        directories=\$(find /var/lib/ceph -mindepth 1 -maxdepth 1 -type d \
          -regextype posix-extended \
          -regex '.*/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}' \
          -print -quit) || exit 1
        test -z \"\$directories\" || exit 1
      fi
    " || fail "Ceph residue remains host=$host"
  done
  for host in "${KUBE_HOSTS[@]}"; do
    "${SSH[@]}" "root@${host}" 'if command -v python3 >/dev/null; then exec python3 -; else exec /usr/libexec/platform-python -; fi' \
      <"$BASE/tests/helpers/ceph_runtime_cleanup.py" || fail "Kubernetes runtime residue remains host=$host"
  done
  if [[ -s "$ALLOWLIST" ]]; then
    validate_allowlist
    local kernel_path stable_path serial wwn identity size
    while IFS='|' read -r host kernel_path stable_path serial wwn identity size; do
      remote_real="$("${SSH[@]}" "root@${host}" "readlink -f '$stable_path'" </dev/null)"
      "${SSH[@]}" "root@${host}" "
        set -euo pipefail
        if blkid '$stable_path' >/dev/null 2>&1; then exit 1; fi
        volumes=\$(pvs --noheadings -o pv_name) || exit 1
        if xargs -r readlink -f <<<\"\$volumes\" | grep -Fxq '$remote_real'; then exit 1; fi
      " </dev/null || fail "storage signature remains host=$host path=$stable_path"
    done <"$ALLOWLIST"
  fi
  if [[ "${CEPH_CLEAN_PREVERIFY:-0}" != 1 ]]; then
    echo CEPH_CLEAN_VERIFY_PASS
    echo LAB_CLEAN_VERIFY_PASS
  fi
}

quiesce_owned_managers() {
  local fsid="$1" host
  # Stop every owned MGR before purging hosts, including incomplete bootstraps
  # where the Ceph API cannot disable the orchestrator module.
  for host in "${CEPH_HOSTS[@]}"; do
    echo "CEPH_STAGE_BEGIN id=CEPH-28 action=stop-owned-mgr host=$host fsid=$fsid"
    "${SSH[@]}" "root@${host}" "
      set -euo pipefail
      pattern='ceph-$fsid@mgr.*.service'
      units=\$(systemctl list-units --all --plain --no-legend \"\$pattern\")
      while read -r unit rest; do
        test -z \"\$unit\" || systemctl stop \"\$unit\"
      done <<<\"\$units\"
      active=\$(systemctl list-units --state=active --plain --no-legend \"\$pattern\")
      test -z \"\$active\"
    " </dev/null || fail "cannot stop owned managers host=$host"
  done
}

if [[ "$MODE" == --verify ]]; then
  verify_clean
  exit 0
fi

if [[ ! -f "$OWNER_FILE" ]]; then
  validate_allowlist
  verify_clean
  exit 0
fi
[[ "$(<"$OWNER_FILE")" == "$OWNER_VALUE" ]] || fail "owner marker mismatch"
validate_allowlist

if [[ -e "$CLUSTER_DIR" ]]; then
  [[ -d "$CLUSTER_DIR" && ! -L "$CLUSTER_DIR" ]] || fail "unexpected cluster directory type"
  ansible-inventory -i "$CLUSTER_DIR/hosts" --list |
    python3 "$BASE/tests/helpers/ceph_contract.py" lab-inventory \
      --compute "${KUBE_HOSTS[@]}" --storage "${CEPH_HOSTS[@]}" ||
    fail "refusing cluster destroy outside the leased host pools"
  kubecli=(python3 "${BASE}/kubecli.py")
  [[ ! -x "${BASE}/.venv/bin/python" ]] || kubecli=("${BASE}/.venv/bin/python" "${BASE}/kubecli.py")
  "${kubecli[@]}" destroy ceph-gate </dev/null || fail "product cluster destroy failed"
  [[ ! -e "$CLUSTER_DIR" ]] || fail "product destroy left cluster directory"
fi
cleanup_kubernetes_runtime

fsid="${CEPH_TEST_FSID:-}"
[[ -n "$fsid" ]] || [[ ! -s "$FSID_FILE" ]] || fsid="$(<"$FSID_FILE")"
remote_fsid="$("${SSH[@]}" "root@${CEPH_HOSTS[0]}" 'test ! -f /etc/ceph/kubeauto-owned-fsid || cat /etc/ceph/kubeauto-owned-fsid' </dev/null)" ||
  fail "cannot inspect bootstrap ownership marker"
if [[ -n "$remote_fsid" ]]; then
  [[ "$remote_fsid" =~ ^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$ ]] || fail "invalid remote owner FSID"
  [[ -z "$fsid" || "$fsid" == "$remote_fsid" ]] || fail "local and remote owner FSIDs differ"
  fsid="$remote_fsid"
else
  [[ -z "$fsid" ]] || fail "local FSID exists without remote ownership marker"
fi
if [[ -n "$fsid" ]]; then
  [[ "$fsid" =~ ^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$ ]] || fail "invalid FSID"
  quiesce_owned_managers "$fsid"
  for host in "${CEPH_HOSTS[@]}"; do
    echo "CEPH_STAGE_BEGIN id=CEPH-28 action=remove-fsid host=$host fsid=$fsid"
    "${SSH[@]}" "root@${host}" "
      if test -e '/var/lib/ceph/$fsid' && test -x /usr/local/sbin/cephadm; then
        /usr/local/sbin/cephadm rm-cluster --force --zap-osds --fsid '$fsid'
      elif test -e '/var/lib/ceph/$fsid'; then
        echo 'cephadm unavailable for owned FSID cleanup' >&2
        exit 1
      fi
    "
  done
fi

if [[ -s "$MAPPER_FILE" ]]; then
  while IFS='|' read -r mapper_host mapper_name backing; do
    validate_host "$mapper_host"
    [[ "$mapper_name" =~ ^kubeauto-ceph-(slow|throttle|lab)-[A-Za-z0-9_.-]+$ ]] || fail "invalid mapper name=$mapper_name"
    [[ "$backing" =~ ^/dev/disk/by-(id|path)/[A-Za-z0-9_.:+-]+$ ]] || fail "invalid alias backing=$backing"
    awk -F'|' -v host="$mapper_host" -v path="$backing" '$1 == host && $3 == path {found=1} END {exit !found}' "$ALLOWLIST" ||
      fail "alias backing is outside the allowlist"
    "${SSH[@]}" "root@${mapper_host}" "
      set -euo pipefail
      alias='/dev/mapper/$mapper_name'
      if test -L \"\$alias\"; then
        test \"\$(readlink \"\$alias\")\" = '$backing'
        unlink \"\$alias\"
      elif dmsetup info '$mapper_name' >/dev/null 2>&1; then
        dmsetup remove '$mapper_name'
      fi
    " </dev/null || fail "failed to remove mapper host=$mapper_host name=$mapper_name"
  done <"$MAPPER_FILE"
fi

for host in "${CEPH_HOSTS[@]}"; do
  "${SSH[@]}" "root@${host}" '
    set -euo pipefail
    if test -f /root/.ssh/authorized_keys; then
      sed -i "/ kubeauto-cephadm$/d" /root/.ssh/authorized_keys
    fi
    if test -e /etc/ceph/kubeauto-owned-ssh-user; then
      test "$(cat /etc/ceph/kubeauto-owned-ssh-user)" = cephadm:/home/cephadm
      if account="$(getent passwd cephadm)"; then
        test "$(cut -d: -f6 <<<"$account")" = /home/cephadm
        test "$(cut -d: -f7 <<<"$account")" = /bin/bash
        userdel cephadm
      fi
      if getent group cephadm >/dev/null; then groupdel cephadm; fi
      # The marker and passwd checks bind this exact disposable account home.
      rm -rf /home/cephadm
      test ! -e /etc/sudoers.d/kubeauto-cephadm || unlink /etc/sudoers.d/kubeauto-cephadm
      unlink /etc/ceph/kubeauto-owned-ssh-user
    fi
    for path in /etc/ceph/kubeauto-cephadm /etc/ceph/kubeauto-cephadm.pub \
      /etc/ceph/kubeauto-bootstrap.conf /etc/ceph/kubeauto-cluster-spec.yml \
      /etc/ceph/kubeauto-monitoring-spec.yml; do
      test ! -e "$path" || unlink "$path"
    done
  '
done
CEPH_CLEAN_PREVERIFY=1 CEPH_TEST_FSID="$fsid" verify_clean
"${SSH[@]}" "root@${CEPH_HOSTS[0]}" 'test ! -e /etc/ceph/kubeauto-owned-fsid || unlink /etc/ceph/kubeauto-owned-fsid' </dev/null

for state_file in "$FSID_FILE" "$MAPPER_FILE" "$OWNER_FILE" "${STATE_DIR}/lease-owner" "${STATE_DIR}/fault-tables"; do
  [[ ! -e "$state_file" ]] || unlink "$state_file"
done
for state_file in "${STATE_DIR}"/*.latency "${STATE_DIR}/rados.sha256" "${STATE_DIR}/csi-marker" "${STATE_DIR}/slow-fixed.summary" "${STATE_DIR}/slow-recovery.summary" "${STATE_DIR}/scrub-contention.summary"; do
  [[ ! -e "$state_file" ]] || unlink "$state_file"
done
for phase in healthy limited recovered; do
  for state_file in "${STATE_DIR}/throttle-$phase.json" "${STATE_DIR}/throttle-$phase.bench.json"; do
    [[ ! -e "$state_file" ]] || unlink "$state_file"
  done
done
CEPH_TEST_FSID="$fsid" verify_clean
