#!/bin/bash
# Independent Ceph delivery gate. All customer behavior enters through
# config.yml plus `kubecli setup <cluster> 90/08`; direct Ceph/Kubernetes calls
# below are fixture, fault-injection or read-only acceptance evidence.
set -euo pipefail

BASE="${KUBEAUTO_BASE:-/usr/local/kubeauto}"
MODE="${1:-full}"
CLUSTER="${CEPH_CLUSTER_NAME:-ceph-gate}"
CLUSTER_DIR="${BASE}/clusters/${CLUSTER}"
STATE_DIR="${CEPH_STATE_DIR:-/var/lib/kubeauto-ceph-test}"
ALLOWLIST="${CEPH_DISK_ALLOWLIST:-${STATE_DIR}/disk-allowlist}"
FSID_FILE="${STATE_DIR}/fsid"
MAPPER_FILE="${STATE_DIR}/mappers"
FAULT_TABLE_FILE="${STATE_DIR}/fault-tables"
OWNER_FILE="${STATE_DIR}/owner"
LEASE_FILE="${CEPH_LEASE_FILE:-/var/lock/kubeauto-ceph-priority.lock}"
COMPUTE_LEASE_FILE=/var/lock/kubeauto-ceph-compute.lock
CONTRACT_SCRIPT="${BASE}/tests/helpers/ceph_contract.py"
KUBECLI=("${BASE}/.venv/bin/python" "${BASE}/kubecli.py")
[[ -x "${KUBECLI[0]}" ]] || KUBECLI=(python3 "${BASE}/kubecli.py")
K=(kubectl "--kubeconfig=${CLUSTER_DIR}/kubectl.kubeconfig")
CEPH_BOOTSTRAP=192.168.122.135
CEPH_HOSTS=(
  192.168.122.135 192.168.122.40 192.168.122.72
  192.168.122.212 192.168.122.165 192.168.122.238
)
KUBE_HOSTS=(
  192.168.47.134 192.168.47.135 192.168.47.136
  192.168.47.131 192.168.47.132 192.168.47.137
)
SSH=(ssh -o BatchMode=yes -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null
  -o ConnectTimeout=10 -o ServerAliveInterval=5 -o ServerAliveCountMax=2)
SLOW_HOST=192.168.122.135
SLOW_MAPPER=kubeauto-ceph-slow-data0
BLUEFS_MAPPER=kubeauto-ceph-slow-db0
CEPH_CURRENT_IMAGE=hub.talkedu.cn/kubeauto/ceph:v20.2.4
CEPH_SOURCE_IMAGE=hub.talkedu.cn/kubeauto/ceph:v20.2.3
ARTIFACT_MODE="${CEPH_ARTIFACT_MODE:-dual}"
CEPH_CSI_CLIENT_KEY_TYPE="${CEPH_CSI_CLIENT_KEY_TYPE:-aes}"
[[ "$CEPH_CSI_CLIENT_KEY_TYPE" == aes || "$CEPH_CSI_CLIENT_KEY_TYPE" == aes256k ]] || {
  echo "invalid CEPH_CSI_CLIENT_KEY_TYPE" >&2
  exit 2
}
CEPHADM_SHA256=5b78c8d5772ef7c5c8619dac6ee0b36716b829338ea7a11c9f2b896626ab354f
CSI_NAMESPACE=ceph-csi
TEST_NAMESPACE=ceph-regression
RBD_POOL=kubernetes-rbd
RGW_ACCESS=
RGW_SECRET=
RGW_ENDPOINT=
START_EPOCH=$(date +%s)
INJECTOR_PID=
THROTTLE_UNIT=
THROTTLE_DEVICE=
THROTTLE_MAJOR_MINOR=
NETEM_IFACE=
OSD_ID=
RECOVERY_OSD_ID=
BASELINE_P99=
declare -A CASE_DONE=()

fail() {
  local id="$1" class="$2"
  shift 2
  echo "CEPH_STAGE_FAIL id=$id class=$class reason=$*" >&2
  return 1
}

stage() {
  echo "CEPH_STAGE_BEGIN id=$1 action=$2"
}

pass() {
  CASE_DONE["$1"]=1
  echo "CEPH_STAGE_PASS id=$1 evidence=$2"
}

assert_cases_complete() {
  local number id
  for number in $(seq -w 1 28); do
    id="CEPH-${number}"
    [[ "${CASE_DONE[$id]:-}" == 1 ]] || fail "$id" test-gate "scenario did not complete in this run"
  done
  echo "CEPH_CASE_LEDGER_PASS cases=${#CASE_DONE[@]}"
}

acquire_host_lease() {
  install -d -m 0700 "$STATE_DIR"
  exec 9>"$LEASE_FILE"
  flock -n 9 || fail CEPH-03 environment "Ceph-priority host pool is already leased"
  exec 7>"$COMPUTE_LEASE_FILE"
  flock -n 7 || fail CEPH-03 environment "Ceph compute host pool is already leased"
  printf 'pid=%s cluster=%s started=%s storage=%s compute=%s\n' \
    "$$" "$CLUSTER" "$(date -u +%FT%TZ)" "${CEPH_HOSTS[*]}" "${KUBE_HOSTS[*]}" >"${STATE_DIR}/lease-owner"
  echo "CEPH_HOST_LEASE_PASS file=$LEASE_FILE pid=$$"
}

ssh_node() {
  local host="$1"
  shift
  "${SSH[@]}" "root@${host}" "$@"
}

ceph_shell() {
  ssh_node "$CEPH_BOOTSTRAP" /usr/local/sbin/cephadm shell -- "$@"
}

require_commands() {
  local command
  for command in python3 ssh ssh-keygen openssl sha256sum flock; do
    command -v "$command" >/dev/null || fail CEPH-01 environment "missing command=$command"
  done
  [[ -r "$CONTRACT_SCRIPT" ]] || fail CEPH-01 test-gate "missing $CONTRACT_SCRIPT"
}

verify_matrix_contract() {
  python3 "$CONTRACT_SCRIPT" os-qualification --matrix "$BASE/tests/ceph-test-matrix.yaml"
  python3 - "$BASE/tests/ceph-test-matrix.yaml" <<'PY'
import sys, yaml
path = sys.argv[1]
data = yaml.safe_load(open(path, encoding="utf-8"))
cases = data.get("cases", [])
required = {"id", "status", "product_command", "expected_marker", "official_reference", "fixture_boundary", "cleanup_scope", "disproof_command"}
assert len(cases) == data["coverage_summary"]["total"] == 28
assert len({case["id"] for case in cases}) == len(cases)
assert all(required <= case.keys() for case in cases)
assert all(case["status"] in {"pending", "pass", "fail"} for case in cases)
print(f"CEPH_MATRIX_CONTRACT_PASS cases={len(cases)}")
PY
  pass CEPH-01 static_matrix_and_parser_contracts_verified
}

manifest_digest() {
  local image="$1"
  skopeo inspect --format '{{.Digest}}' "docker://${image}"
}

verify_supply_chain() {
  case "$ARTIFACT_MODE" in
    dual|manual-talkedu) ;;
    *) fail CEPH-02 supply-chain "invalid CEPH_ARTIFACT_MODE=$ARTIFACT_MODE"; return 1 ;;
  esac
  stage CEPH-02 "verify-artifacts-mode=$ARTIFACT_MODE"
  command -v skopeo >/dev/null || fail CEPH-02 supply-chain "skopeo is required"
  local cephadm="$BASE/extra-bin/cephadm" checksum version
  [[ -f "$cephadm" ]] || fail CEPH-02 supply-chain "missing staged cephadm"
  checksum="$(sha256sum "$cephadm")"
  [[ "${checksum%% *}" == "$CEPHADM_SHA256" ]] ||
    fail CEPH-02 supply-chain "cephadm SHA256 mismatch; refusing execution"
  version="$(python3 "$cephadm" version)" || fail CEPH-02 supply-chain "cephadm version command failed"
  [[ "$version" == 'cephadm version 20.2.4 '* && "$version" == *' tentacle (stable)' ]] ||
    fail CEPH-02 supply-chain "cephadm exact version mismatch"
  printf 'CEPHADM_ARTIFACT_PASS version=20.2.4 sha256=%s\n' "$CEPHADM_SHA256"
  local pins=(
    'ansible|2.20.9'
    'ceph|v20.2.4' 'ceph|v20.2.3' 'cephcsi|v3.17.1'
    'csi-node-driver-registrar|v2.16.0' 'csi-provisioner|v6.2.0'
    'csi-attacher|v4.11.0' 'csi-resizer|v2.1.0' 'csi-snapshotter|v8.5.0'
    'ceph-prometheus|v3.6.0' 'ceph-alertmanager|v0.28.1'
    'ceph-node-exporter|v1.9.1' 'ceph-grafana|12.3.1'
    'minio-mc|RELEASE.2025-04-08T15-39-49Z'
    'busybox|1.37' 'json-mock|v1.3.1'
  )
  local pin name tag talkedu dockerhub talkedu_digest dockerhub_digest
  for pin in "${pins[@]}"; do
    IFS='|' read -r name tag <<<"$pin"
    talkedu="hub.talkedu.cn/kubeauto/${name}:${tag}"
    dockerhub="docker.io/brinnatt/${name}:${tag}"
    talkedu_digest="$(manifest_digest "$talkedu")" || fail CEPH-02 supply-chain "missing $talkedu"
    [[ "$talkedu_digest" =~ ^sha256:[0-9a-f]{64}$ ]] || fail CEPH-02 supply-chain "invalid digest image=$name"
    if [[ "$ARTIFACT_MODE" == dual ]]; then
      dockerhub_digest="$(manifest_digest "$dockerhub")" || fail CEPH-02 supply-chain "missing $dockerhub"
      [[ "$talkedu_digest" == "$dockerhub_digest" ]] ||
        fail CEPH-02 supply-chain "digest mismatch image=$name tag=$tag"
    fi
    printf 'CEPH_IMAGE_MANIFEST_PASS mode=%s image=%s tag=%s digest=%s\n' "$ARTIFACT_MODE" "$name" "$tag" "$talkedu_digest"
  done
  if [[ "$ARTIFACT_MODE" == dual ]]; then
    local pack_talkedu pack_dockerhub
    pack_talkedu="$(manifest_digest hub.talkedu.cn/kubeauto/kubeauto-ext-bin:1.16.0)" || fail CEPH-02 supply-chain "missing TalkEdu ext-bin"
    pack_dockerhub="$(manifest_digest docker.io/brinnatt/kubeauto-ext-bin:1.16.0)" || fail CEPH-02 supply-chain "missing Docker Hub ext-bin"
    [[ "$pack_talkedu" =~ ^sha256:[0-9a-f]{64}$ && "$pack_talkedu" == "$pack_dockerhub" ]] ||
      fail CEPH-02 supply-chain "ext-bin digest mismatch"
    echo "CEPH_EXT_BIN_MANIFEST_PASS digest=$pack_talkedu"
  fi
  pass CEPH-02 "artifacts_verified_mode=$ARTIFACT_MODE"
  echo "CEPH_SUPPLY_CHAIN_PASS mode=$ARTIFACT_MODE images=${#pins[@]} cephadm_sha256=$CEPHADM_SHA256"
}

run_host_probe() {
  local host
  stage CEPH-03 read-only-host-disk-probe
  bash "$BASE/tests/helpers/ceph-host-probe.sh"
  for host in "${CEPH_HOSTS[@]}"; do
    ssh_node "$host" python3 - <"$BASE/tests/helpers/ceph_runtime_cleanup.py" ||
      fail CEPH-03 environment "standalone storage host has Kubernetes residue host=$host"
  done
  verify_compute_hosts
  pass CEPH-03 six_storage_and_six_compute_hosts_qualified
}

verify_compute_hosts() {
  local host
  for host in "${KUBE_HOSTS[@]}"; do
    stage CEPH-03 "read-only-compute-probe-host=$host"
    # Refuse an existing cluster or runtime before any product mutation.
    ssh_node "$host" 'if command -v python3 >/dev/null; then exec python3 -; else exec /usr/libexec/platform-python -; fi' \
      <"$BASE/tests/helpers/ceph_runtime_cleanup.py" ||
      fail CEPH-03 environment "compute node is not clean host=$host"
    ssh_node "$host" '
      set -euo pipefail
      test "$(nproc)" -ge 2
      test "$(awk '\''/MemTotal/{print $2}'\'' /proc/meminfo)" -ge 3145728
      test "$(timedatectl show -p NTPSynchronized --value)" = yes
      test ! -e /etc/ceph/kubeauto-owned-fsid
      if test -d /var/lib/ceph; then
        test -z "$(find /var/lib/ceph -mindepth 1 -maxdepth 1 -type d)"
      fi
    ' || fail CEPH-03 environment "compute capacity, clock or foreign Ceph state host=$host"
  done
  echo "CEPH_COMPUTE_PROBE_PASS hosts=${#KUBE_HOSTS[@]}"
}

verify_compute_kernel_clients() {
  local host
  for host in "${KUBE_HOSTS[@]}"; do
    stage CEPH-03 "kernel-client-preflight-host=$host"
    ssh_node "$host" "if command -v python3 >/dev/null; then exec python3 - $CEPH_CSI_CLIENT_KEY_TYPE; else exec /usr/libexec/platform-python - $CEPH_CSI_CLIENT_KEY_TYPE; fi" \
      <"$BASE/roles/ceph/files/kernel-client-check.py" || {
        fail CEPH-03 environment "compute kernel lacks required Ceph client support host=$host"
        return 1
      }
  done
  echo "CEPH_COMPUTE_KERNEL_CLIENT_PASS hosts=${#KUBE_HOSTS[@]} secure_msgr2=true key_type=$CEPH_CSI_CLIENT_KEY_TYPE"
}

bootstrap_compute_python() {
  local host
  for host in "${KUBE_HOSTS[@]}"; do
    stage CEPH-03 "native-compute-python-bootstrap-host=$host"
    ssh_node "$host" 'if command -v python3 >/dev/null; then exec python3 -; else exec /usr/libexec/platform-python -; fi' \
      <"$BASE/tests/helpers/ceph_runtime_cleanup.py" || {
        fail CEPH-03 environment "compute Python bootstrap requires a clean host=$host"
        return 1
      }
    if ! ssh_node "$host" bash -s <<'PYTHON_BOOTSTRAP'
set -euo pipefail
. /etc/os-release
[[ "$ID" == rocky && "$VERSION_ID" == 8.10 ]]
dnf install -y python3.12
rpm -q python3.12 python3.12-libs
rpm -qf /usr/bin/python3.12
rpm -V python3.12 python3.12-libs
system_python="$(readlink -f /usr/bin/python3)"
case "$system_python" in
  /usr/bin/python3.9|/usr/bin/python3.12) ;;
  *) echo 'system Python is not a distribution-owned lab interpreter' >&2; exit 1 ;;
esac
rpm -qf "$system_python"
printf 'CEPH_COMPUTE_NATIVE_PYTHON system=%s ansible=/usr/bin/python3.12\n' "$system_python"
for attempt in $(seq 1 100); do
  /usr/bin/python3.12 -c 'import json, pkgutil, re, runpy; assert pkgutil.resolve_name("json.decoder.JSONDecoder") is json.JSONDecoder'
done
/usr/bin/python3.12 --version
PYTHON_BOOTSTRAP
    then
      fail CEPH-03 environment "native compute Python bootstrap failed host=$host"
      return 1
    fi
  done
  # Prove the native Ansible module path, where the Python 3.9 crash reproduced.
  local inventory attempt
  inventory="$(IFS=,; printf '%s,' "${KUBE_HOSTS[*]}")"
  for attempt in $(seq 1 30); do
    ANSIBLE_HOST_KEY_CHECKING=False ansible all -i "$inventory" -u root \
      -e ansible_python_interpreter=/usr/bin/python3.12 -m ansible.builtin.ping || {
        fail CEPH-03 environment "native compute Ansible ping failed attempt=$attempt"
        return 1
      }
    echo "CEPH_COMPUTE_ANSIBLE_PING_PASS attempt=$attempt"
  done
  for host in "${KUBE_HOSTS[@]}"; do
    ssh_node "$host" /usr/bin/python3.12 - <"$BASE/tests/helpers/ceph_runtime_cleanup.py" || {
      fail CEPH-03 environment "compute Python bootstrap final clean check failed host=$host"
      return 1
    }
  done
  echo "CEPH_COMPUTE_PYTHON_BOOTSTRAP_PASS hosts=${#KUBE_HOSTS[@]} interpreter=/usr/bin/python3.12"
}

validate_allowlist_shape() {
  [[ -s "$ALLOWLIST" ]] || fail CEPH-04 test-gate "missing allowlist"
  [[ "$(wc -l <"$ALLOWLIST")" -eq 18 ]] || fail CEPH-04 environment "expected 18 allowlisted disks"
  awk -F'|' '
    NF != 7 {exit 1}
    $1 !~ /^192\.168\.122\.(135|40|72|212|165|238)$/ {exit 1}
    $3 !~ /^\/dev\/disk\/by-(id|path)\// {exit 1}
    $6 !~ /^[A-Za-z0-9_.:+-]+$/ {exit 1}
    {count[$1]++}
    END {for (host in count) if (count[host] != 3) exit 1}
  ' "$ALLOWLIST" || fail CEPH-04 test-gate "invalid allowlist schema"
  [[ "$(cut -d'|' -f1 "$ALLOWLIST" | sort -u | wc -l)" -eq 6 ]] || fail CEPH-04 test-gate "allowlist host count"
  pass CEPH-04 stable_disk_identity_allowlist_recorded
}

create_mapper() {
  local host="$1" name="$2" backing="$3"
  [[ "$name" =~ ^kubeauto-ceph-(slow|throttle|lab)-[A-Za-z0-9_.-]+$ ]] || fail CEPH-04 test-gate "invalid mapper name"
  [[ "$backing" == /dev/disk/by-id/* || "$backing" == /dev/disk/by-path/* ]] || fail CEPH-04 test-gate "invalid mapper backing"
  # Ceph accepts the physical disk behind a symlink, not an untyped dm target.
  # Delay is injected later into the real Ceph-owned LVM device.
  ssh_node "$host" "
    set -euo pipefail
    test -b '$backing'
    alias='/dev/mapper/$name'
    if test -e \"\$alias\" || test -L \"\$alias\"; then
      test -L \"\$alias\"
      test \"\$(readlink \"\$alias\")\" = '$backing'
    else
      ln -s '$backing' \"\$alias\"
    fi
  " </dev/null
  printf '%s|%s|%s\n' "$host" "$name" "$backing" >>"$MAPPER_FILE"
}

lab_mapper_name() {
  local host="$1" index="$2"
  printf 'kubeauto-ceph-lab-%s-%s' "${host##*.}" "$index"
}

prepare_fault_mappers() {
  stage CEPH-04 prepare-owned-device-mapper-fixtures
  if [[ -e "$OWNER_FILE" ]]; then
    [[ -f "$OWNER_FILE" && "$(<"$OWNER_FILE")" == kubeauto-ceph-regression ]] ||
      fail CEPH-04 test-gate "foreign or malformed owner marker"
  fi
  printf '%s\n' kubeauto-ceph-regression >"$OWNER_FILE"
  : >"$MAPPER_FILE"
  local host index kernel stable serial wwn identity size mapper_name
  while IFS='|' read -r host kernel stable serial wwn identity size; do
    index=0
    while IFS='|' read -r _ kernel stable serial wwn identity size; do
      if [[ "$host" == "$SLOW_HOST" && ( "$index" -eq 0 || "$index" -eq 2 ) ]]; then
        index=$((index + 1))
        continue
      fi
      mapper_name="$(lab_mapper_name "$host" "$index")"
      create_mapper "$host" "$mapper_name" "$stable"
      index=$((index + 1))
    done < <(awk -F'|' -v host="$host" '$1 == host' "$ALLOWLIST")
  done < <(awk -F'|' '!seen[$1]++ {print}' "$ALLOWLIST")
  local -a host_disks=()
  mapfile -t host_disks < <(awk -F'|' -v host="$SLOW_HOST" '$1 == host {print $3}' "$ALLOWLIST")
  [[ "${#host_disks[@]}" -eq 3 ]] || fail CEPH-04 test-gate "slow host disk count"
  create_mapper "$SLOW_HOST" "$SLOW_MAPPER" "${host_disks[0]}"
  create_mapper "$SLOW_HOST" "$BLUEFS_MAPPER" "${host_disks[2]}"
  sort -u -o "$MAPPER_FILE" "$MAPPER_FILE"
  [[ "$(wc -l <"$MAPPER_FILE")" -eq 18 ]] || fail CEPH-04 test-gate "expected exactly one mapping per allowlisted disk"
  pass CEPH-04 mapper_names_and_backing_recorded
}

inventory_line() {
  local host="$1" index=0 kernel stable serial wwn identity size
  local -a paths=() ids=()
  while IFS='|' read -r _ kernel stable serial wwn identity size; do
    if [[ "$host" == "$SLOW_HOST" && "$index" -eq 0 ]]; then
      paths+=("/dev/mapper/${SLOW_MAPPER}")
      ids+=("dm:${SLOW_MAPPER}")
    elif [[ "$host" == "$SLOW_HOST" && "$index" -eq 2 ]]; then
      paths+=("/dev/mapper/${BLUEFS_MAPPER}")
      ids+=("dm:${BLUEFS_MAPPER}")
    else
      local mapper_name
      mapper_name="$(lab_mapper_name "$host" "$index")"
      paths+=("/dev/mapper/${mapper_name}")
      ids+=("dm:${mapper_name}")
    fi
    index=$((index + 1))
  done < <(awk -F'|' -v host="$host" '$1 == host' "$ALLOWLIST")
  local hostname
  hostname="$(ssh_node "$host" 'hostname -s')"
  printf "%s ceph_hostname='%s' ceph_devices='%s,%s' ceph_device_ids='%s,%s' ceph_db_devices='%s' ceph_db_device_ids='%s'" \
    "$host" "$hostname" "${paths[0]}" "${paths[1]}" "${ids[0]}" "${ids[1]}" "${paths[2]}" "${ids[2]}"
}

write_inventory() {
  local destination="$1" host
  {
    echo '[etcd]'
    for host in "${KUBE_HOSTS[@]:0:3}"; do printf "%s k8s_nodename='ceph-master-%s'\n" "$host" "${host##*.}"; done
    echo '[kube_master]'
    for host in "${KUBE_HOSTS[@]:0:3}"; do printf "%s k8s_nodename='ceph-master-%s'\n" "$host" "${host##*.}"; done
    echo '[kube_node]'
    for host in "${KUBE_HOSTS[@]:3:3}"; do printf "%s k8s_nodename='ceph-worker-%s'\n" "$host" "${host##*.}"; done
    echo '[ceph]'
    for host in "${CEPH_HOSTS[@]}"; do inventory_line "$host"; echo; done
    echo '[ceph_bootstrap]'; echo "$CEPH_BOOTSTRAP"
    echo '[ceph_mon]'; printf '%s\n' "${CEPH_HOSTS[@]:0:3}"
    echo '[ceph_mgr]'; printf '%s\n' "${CEPH_HOSTS[@]:0:2}"
    echo '[ceph_osd]'; printf '%s\n' "${CEPH_HOSTS[@]}"
    echo '[ceph_rgw]'; printf '%s\n' "${CEPH_HOSTS[@]:0:2}"
    cat <<EOF
[harbor]
[ex_lb]
[chrony]
[all:vars]
SECURE_PORT="6443"
CONTAINER_RUNTIME="containerd"
CLUSTER_NETWORK="calico"
PROXY_MODE="ipvs"
SERVICE_CIDR="10.86.0.0/16"
CLUSTER_CIDR="172.31.0.0/16"
NODE_PORT_RANGE="30000-32767"
CLUSTER_DNS_DOMAIN="cluster.local"
bin_dir="/usr/local/bin"
base_dir="/usr/local/kubeauto"
cluster_dir="{{ base_dir }}/clusters/${CLUSTER}"
ca_dir="/etc/kubernetes/ssl"
k8s_nodename=''
ansible_user=root
EOF
  } >"$destination"
}

configure_product() {
  local image="$1" csi_install="${2:-yes}"
  python3 - "$CLUSTER_DIR/config.yml" "$image" "$csi_install" "${CEPH_CSI_CLIENT_KEY_TYPE:-aes}" <<'PY'
import sys, yaml
path, image, csi_install, key_type = sys.argv[1:]
with open(path, encoding="utf-8") as stream:
    data = yaml.safe_load(stream)
data.update({
    "KUBE_RESERVED_ENABLED": "no",
    "SYS_RESERVED_ENABLED": "no",
    "local_path_provisioner_install": "yes",
    "ceph_install": "yes",
    "ceph_public_network": "192.168.122.0/24",
    "ceph_cluster_network": "192.168.122.0/24",
    "ceph_replication_size": 3,
    "ceph_image": image,
    "ceph_monitoring_install": "yes",
    "ceph_dashboard_install": "yes",
    "ceph_rgw_install": "yes",
    "ceph_csi_install": csi_install,
    "ceph_csi_client_key_type": key_type,
    "ceph_csi_legacy_aes_risk_accepted": "yes" if key_type == "aes" else "no",
    "ceph_csi_rbd_pool": "kubernetes-rbd",
    "ceph_csi_cephfs_name": "kubernetes-cephfs",
    "ceph_csi_rbd_storage_class": "ceph-rbd",
    "ceph_csi_cephfs_storage_class": "cephfs",
})
with open(path, "w", encoding="utf-8") as stream:
    yaml.safe_dump(data, stream, sort_keys=False)
PY
}

verify_disabled_product() {
  local host log="${STATE_DIR}/default-disabled.log"
  "${KUBECLI[@]}" setup "$CLUSTER" 08 </dev/null | tee "$log"
  if grep -Fq 'Gathering Facts' "$log"; then
    fail CEPH-05 product "disabled Ceph play gathered facts"
  fi
  [[ ! -s "$FSID_FILE" ]] || fail CEPH-05 product "disabled Ceph created FSID state"
  for host in "${CEPH_HOSTS[@]}"; do
    ssh_node "$host" 'test ! -e /etc/ceph/kubeauto-owned-fsid' ||
      fail CEPH-05 product "disabled Ceph created ownership marker host=$host"
  done
  echo CEPH_DEFAULT_DISABLED_PASS
}

prepare_product_cluster() {
  stage CEPH-05 product-entry-bootstrap
  local image="$CEPH_CURRENT_IMAGE"
  if [[ "$MODE" == full || "$MODE" == --upgrade ]]; then
    image="$CEPH_SOURCE_IMAGE"
  fi
  install -d -m 0700 "$STATE_DIR"
  if [[ -e "$CLUSTER_DIR" ]]; then
    fail CEPH-05 test-gate "cluster directory already exists; inspect or clean the prior owned run"
  fi
  printf '%s\n' kubeauto-ceph-regression >"$OWNER_FILE"
  "${KUBECLI[@]}" download -D </dev/null
  "${KUBECLI[@]}" download -E ceph </dev/null
  "${KUBECLI[@]}" new "$CLUSTER" </dev/null
  write_inventory "$CLUSTER_DIR/hosts"
  ansible-inventory -i "$CLUSTER_DIR/hosts" --list |
    python3 "$CONTRACT_SCRIPT" lab-inventory \
      --compute "${KUBE_HOSTS[@]}" --storage "${CEPH_HOSTS[@]}" ||
    fail CEPH-05 test-gate "product inventory differs from the leased host pools"
  verify_disabled_product
  configure_product "$image" no
  # Prove standalone Ceph before the more expensive Kubernetes/CSI prerequisites.
  "${KUBECLI[@]}" setup "$CLUSTER" 08 -e ceph_allow_test_mappers=true </dev/null
  configure_product "$image" yes
  "${KUBECLI[@]}" setup "$CLUSTER" 90 -e ceph_allow_test_mappers=true </dev/null
  fsid="$(ceph_shell ceph fsid | tail -n1 | tr -d '[:space:]')"
  [[ "$fsid" =~ ^[0-9a-f-]{36}$ ]] || fail CEPH-05 product "invalid cluster FSID"
  printf '%s\n' "$fsid" >"$FSID_FILE"
  ceph_shell ceph versions --format json |
    python3 "$CONTRACT_SCRIPT" versions --expected "${image##*:v}" ||
    fail CEPH-05 product "installed daemons do not match the selected version"
  KUBECONFIG="${CLUSTER_DIR}/kubectl.kubeconfig" \
    PRODUCTION_SMOKE_IMAGE=registry.talkschool.cn:5000/brinnatt/json-mock:v1.3.1 \
    bash "$BASE/tests/helpers/kubernetes-production-smoke.sh"
  ceph_shell ceph orch ps --refresh --format json | python3 -c '
import json,sys
rows=json.load(sys.stdin)
assert sum(x["daemon_type"]=="mon" and x["status_desc"]=="running" for x in rows) >= 3
assert sum(x["daemon_type"]=="mgr" and x["status_desc"]=="running" for x in rows) >= 2
assert sum(x["daemon_type"]=="osd" and x["status_desc"]=="running" for x in rows) >= 12
' || fail CEPH-05 product "daemon topology not ready"
  pass CEPH-05 fsid_and_daemon_topology_verified
  echo CEPH_CLUSTER_INSTALL_PASS
}

wait_clean() {
  local attempts=0 status
  while ((attempts < 180)); do
    status="$(ceph_shell ceph pg stat --format json 2>/dev/null || true)"
    if python3 "$CONTRACT_SCRIPT" pg-clean <<<"$status" >/dev/null 2>&1; then
      return 0
    fi
    attempts=$((attempts + 1))
    ((attempts % 6 == 0)) && echo "CEPH_WAIT_HEARTBEAT id=pg-clean elapsed=$((attempts * 10)) state=$status"
    sleep 10
  done
  fail CEPH-05 product "PGs did not become active+clean"
}

verify_rados_data() {
  stage CEPH-07 rados-hash
  local expected actual
  expected="$(printf 'kubeauto-ceph-rados-v1' | sha256sum | awk '{print $1}')"
  printf 'kubeauto-ceph-rados-v1' | ceph_shell rados -p "$RBD_POOL" put kubeauto-marker -
  actual="$(ceph_shell rados -p "$RBD_POOL" get kubeauto-marker - | sha256sum | awk '{print $1}')"
  [[ "$actual" == "$expected" ]] || fail CEPH-07 product "RADOS hash mismatch"
  printf '%s\n' "$expected" >"$STATE_DIR/rados.sha256"
  pass CEPH-07 hash="$actual"
  echo CEPH_RADOS_DATA_PASS
}

deploy_business_fixtures() {
  stage CEPH-08 csi-rbd-cephfs-data-path
  ceph_shell ceph fs subvolumegroup ls kubernetes-cephfs --format json |
    python3 -c 'import json,sys; assert "csi" in {row["name"] for row in json.load(sys.stdin)}, "Ceph-CSI subvolume group missing"' ||
    fail CEPH-09 product "Ceph-CSI subvolume group csi is missing"
  "${K[@]}" create namespace "$TEST_NAMESPACE" --dry-run=client -o yaml | "${K[@]}" apply -f -
  "${K[@]}" apply -f - <<EOF
apiVersion: v1
kind: PersistentVolumeClaim
metadata: {name: rbd-data, namespace: ${TEST_NAMESPACE}}
spec:
  accessModes: [ReadWriteOnce]
  storageClassName: ceph-rbd
  resources: {requests: {storage: 2Gi}}
---
apiVersion: v1
kind: PersistentVolumeClaim
metadata: {name: cephfs-data, namespace: ${TEST_NAMESPACE}}
spec:
  accessModes: [ReadWriteMany]
  storageClassName: cephfs
  resources: {requests: {storage: 2Gi}}
---
apiVersion: v1
kind: Pod
metadata: {name: storage-client, namespace: ${TEST_NAMESPACE}, labels: {kubeauto.io/owner: ceph-regression}}
spec:
  containers:
    - name: client
      image: registry.talkschool.cn:5000/brinnatt/busybox:1.37
      command: [sh, -c, "sleep 86400"]
      volumeMounts:
        - {name: rbd, mountPath: /rbd}
        - {name: cephfs, mountPath: /cephfs}
  volumes:
    - name: rbd
      persistentVolumeClaim: {claimName: rbd-data}
    - name: cephfs
      persistentVolumeClaim: {claimName: cephfs-data}
EOF
  "${K[@]}" -n "$TEST_NAMESPACE" wait --for=condition=Ready pod/storage-client --timeout=15m
  marker="$(openssl rand -hex 32)"
  "${K[@]}" -n "$TEST_NAMESPACE" exec storage-client -- sh -ceu "printf '%s' '$marker' | tee /rbd/marker /cephfs/marker >/dev/null; sync"
  [[ "$("${K[@]}" -n "$TEST_NAMESPACE" exec storage-client -- sha256sum /rbd/marker | awk '{print $1}')" == "$(printf %s "$marker" | sha256sum | awk '{print $1}')" ]] || fail CEPH-08 product "RBD hash"
  [[ "$("${K[@]}" -n "$TEST_NAMESPACE" exec storage-client -- sha256sum /cephfs/marker | awk '{print $1}')" == "$(printf %s "$marker" | sha256sum | awk '{print $1}')" ]] || fail CEPH-09 product "CephFS hash"
  "${K[@]}" -n "$TEST_NAMESPACE" run cephfs-reader \
    --image=registry.talkschool.cn:5000/brinnatt/busybox:1.37 --restart=Never \
    --overrides='{"spec":{"containers":[{"name":"cephfs-reader","image":"registry.talkschool.cn:5000/brinnatt/busybox:1.37","command":["sh","-c","sleep 86400"],"volumeMounts":[{"name":"cephfs","mountPath":"/cephfs"}]}],"volumes":[{"name":"cephfs","persistentVolumeClaim":{"claimName":"cephfs-data"}}]}}'
  "${K[@]}" -n "$TEST_NAMESPACE" wait --for=condition=Ready pod/cephfs-reader --timeout=10m
  [[ "$("${K[@]}" -n "$TEST_NAMESPACE" exec cephfs-reader -- cat /cephfs/marker)" == "$marker" ]] || fail CEPH-09 product "second CephFS client readback"
  printf '%s\n' "$marker" >"$STATE_DIR/csi-marker"
  pass CEPH-08 rbd_pvc_hash_verified
  pass CEPH-09 cephfs_rwx_hash_verified
  echo CEPH_CSI_RBD_PASS
  echo CEPH_CSI_CEPHFS_PASS
}

deploy_rgw_fixture() {
  stage CEPH-10 rgw-s3-data-path
  local rgw_json rgw_host rgw_ip
  rgw_json="$(ceph_shell ceph orch ps --service_name rgw.kubeauto --format json)"
  rgw_host="$(python3 -c 'import json,sys; rows=json.load(sys.stdin); print(next(x["hostname"] for x in rows if x["status_desc"]=="running"))' <<<"$rgw_json")"
  rgw_ip="$(for host in "${CEPH_HOSTS[@]}"; do [[ "$(ssh_node "$host" 'hostname -s')" == "$rgw_host" ]] && echo "$host" && break; done)"
  [[ -n "$rgw_ip" ]] || fail CEPH-10 product "RGW endpoint host not found"
  RGW_ENDPOINT="${rgw_ip}:8080"
  local rgw_credentials
  rgw_credentials="$(ceph_shell radosgw-admin user create --uid kubeauto-regression --display-name kubeauto-regression --gen-access-key --gen-secret)"
  RGW_ACCESS="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["keys"][0]["access_key"])' <<<"$rgw_credentials")"
  RGW_SECRET="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["keys"][0]["secret_key"])' <<<"$rgw_credentials")"
  [[ -n "$RGW_ACCESS" && -n "$RGW_SECRET" ]] || fail CEPH-10 test-gate "RGW did not generate credentials"
  printf '%s\n%s\n%s\n' "$RGW_ACCESS" "$RGW_SECRET" "$RGW_ENDPOINT" |
    python3 -c 'import base64,json,sys; access,secret,endpoint=sys.stdin.read().splitlines(); data={"MC_HOST_ceph":f"http://{access}:{secret}@{endpoint}"}; print(json.dumps({"apiVersion":"v1","kind":"Secret","metadata":{"name":"rgw-client","namespace":"ceph-regression"},"type":"Opaque","data":{key:base64.b64encode(value.encode()).decode() for key,value in data.items()}}))' |
    "${K[@]}" apply -f - >/dev/null
  "${K[@]}" apply -f - <<EOF
apiVersion: v1
kind: Pod
metadata: {name: s3-client, namespace: ${TEST_NAMESPACE}, labels: {kubeauto.io/owner: ceph-regression}}
spec:
  containers:
    - name: s3-client
      image: registry.talkschool.cn:5000/brinnatt/minio-mc:RELEASE.2025-04-08T15-39-49Z
      command: [sleep, "86400"]
      env:
        - name: MC_HOST_ceph
          valueFrom:
            secretKeyRef: {name: rgw-client, key: MC_HOST_ceph}
EOF
  "${K[@]}" -n "$TEST_NAMESPACE" wait --for=condition=Ready pod/s3-client --timeout=10m
  "${K[@]}" -n "$TEST_NAMESPACE" exec -i s3-client -- sh -seu <<'S3_DATA_PATH'
printf kubeauto-ceph-s3-v1 >/tmp/marker
mc mb --ignore-existing ceph/kubeauto-ceph-test
S3_DATA_PATH
  s3_sigv4_put marker
  "${K[@]}" -n "$TEST_NAMESPACE" exec -i s3-client -- sh -seu <<'S3_DATA_PATH'
mc stat ceph/kubeauto-ceph-test/marker >/dev/null
mc cp ceph/kubeauto-ceph-test/marker /tmp/readback
expected="$(sha256sum </tmp/marker)"
actual="$(sha256sum </tmp/readback)"
test "$expected" = "$actual"
S3_DATA_PATH
  s3_sigv4_put delete-check
  "${K[@]}" -n "$TEST_NAMESPACE" exec -i s3-client -- sh -seu <<'S3_DATA_PATH'
mc rm ceph/kubeauto-ceph-test/delete-check
! mc stat ceph/kubeauto-ceph-test/delete-check >/dev/null 2>&1
S3_DATA_PATH
  "${K[@]}" -n "$TEST_NAMESPACE" exec s3-client -- mc ls --json ceph/kubeauto-ceph-test |
    python3 -c 'import json,sys; rows=[json.loads(line) for line in sys.stdin if line.strip()]; assert rows and all(row["status"]=="success" for row in rows); assert any(row.get("key")=="marker" for row in rows)'
  pass CEPH-10 put_get_list_hash_verified
  echo CEPH_RGW_S3_PASS
}

s3_sigv4_put() {
  local object="$1"
  [[ "$object" == marker || "$object" == delete-check ||
     ( ${#object} -le 128 && "$object" =~ ^latency-[a-z0-9-]+$ ) ]] || return 1
  local diagnostic
  if ! diagnostic="$(RGW_ACCESS="$RGW_ACCESS" RGW_SECRET="$RGW_SECRET" \
    python3 "$BASE/tests/helpers/ceph_s3_put.py" \
      --endpoint "http://${RGW_ENDPOINT}" --bucket kubeauto-ceph-test \
      --key "$object" 2>&1)"; then
    fail CEPH-10 test-gate "$diagnostic"
  fi
  echo "$diagnostic"
}

verify_security() {
  stage CEPH-11 negative-auth-and-caps
  if ceph_shell rados --id csi-rbd --key AQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA== -p "$RBD_POOL" ls >/dev/null 2>&1; then
    fail CEPH-11 product "invalid CephX key unexpectedly succeeded"
  fi
  local s3_error
  if s3_error="$("${K[@]}" -n "$TEST_NAMESPACE" exec s3-client -- env MC_HOST_bad="http://${RGW_ACCESS}:wrong@${RGW_ENDPOINT}" mc ls bad 2>&1)"; then
    fail CEPH-11 product "invalid S3 credential unexpectedly succeeded"
  fi
  [[ "$s3_error" =~ [Aa]ccess[[:space:]]*[Dd]enied|InvalidAccessKeyId|SignatureDoesNotMatch|403 ]] ||
    fail CEPH-11 test-gate "S3 negative test failed without an authentication denial"
  ceph_shell ceph osd pool create kubeauto-security-denied >/dev/null
  if ssh_node "$CEPH_BOOTSTRAP" \
    "/usr/local/sbin/cephadm shell -- bash -ceu 'key=\$(ceph auth get-key client.csi-rbd); rados --id csi-rbd --key \"\$key\" -p kubeauto-security-denied ls'" \
    >/dev/null 2>&1; then
    fail CEPH-11 product "pool-scoped CSI identity accessed another pool"
  fi
  ceph_shell ceph config set mon mon_allow_pool_delete true
  ceph_shell ceph osd pool rm kubeauto-security-denied kubeauto-security-denied --yes-i-really-really-mean-it >/dev/null
  ceph_shell ceph config set mon mon_allow_pool_delete false
  pass CEPH-11 invalid_credentials_and_cross_pool_access_rejected
  echo CEPH_SECURITY_PASS
}

run_upgrade() {
  stage CEPH-26 cephadm-20.2.3-to-20.2.4
  ceph_shell ceph versions --format json |
    python3 "$CONTRACT_SCRIPT" versions --expected 20.2.3 ||
    fail CEPH-26 test-gate "upgrade source daemons are not all 20.2.3"
  ceph_shell ceph osd set noout
  ceph_shell ceph orch upgrade start --image "$CEPH_CURRENT_IMAGE"
  local attempt versions status
  for attempt in $(seq 1 180); do
    status="$(ceph_shell ceph orch upgrade status --format json)"
    versions="$(ceph_shell ceph versions --format json)"
    if python3 "$CONTRACT_SCRIPT" versions --expected 20.2.4 <<<"$versions" >/dev/null 2>&1; then
      break
    fi
    ((attempt % 6 == 0)) && echo "CEPH_WAIT_HEARTBEAT id=CEPH-26 elapsed=$((attempt * 10)) state=$status"
    sleep 10
  done
  python3 "$CONTRACT_SCRIPT" versions --expected 20.2.4 <<<"$versions" >/dev/null || fail CEPH-26 product "not all daemons upgraded"
  ceph_shell ceph osd unset noout
  configure_product "$CEPH_CURRENT_IMAGE"
  "${KUBECLI[@]}" setup "$CLUSTER" 08 -e ceph_allow_test_mappers=true </dev/null
  verify_post_fault_hashes CEPH-26
  pass CEPH-26 noout_released_and_final_version_verified
  echo CEPH_UPGRADE_PASS
}

mapper_table() {
  local name="$1" target="$2" delay_ms="$3" host native original table
  [[ -s "$FAULT_TABLE_FILE" ]] || fail CEPH-13 test-gate "native OSD fault table is missing"
  IFS='|' read -r host _ native original < <(awk -F'|' -v name="$name" '$2 == name {print; exit}' "$FAULT_TABLE_FILE")
  [[ "$host" == "$SLOW_HOST" && "$native" =~ ^ceph--[A-Za-z0-9-]+$ ]] ||
    fail CEPH-13 test-gate "invalid native OSD mapping name=$name"
  case "$target" in
    linear) table="$original" ;;
    delay) table="$(python3 "$CONTRACT_SCRIPT" delay-table --delay-ms "$delay_ms" <<<"$original")" ;;
    *) fail CEPH-13 test-gate "invalid mapper target=$target" ;;
  esac
  ssh_node "$host" "set -euo pipefail; dmsetup suspend '$native'; trap 'dmsetup resume $native' EXIT; dmsetup reload '$native' --table '$table'; dmsetup resume '$native'; trap - EXIT"
}

business_once() {
  local suffix="$1"
  printf 'latency-%s' "$suffix" | ceph_shell rados -p "$RBD_POOL" put "latency-${suffix}" - || return 1
  "${K[@]}" -n "$TEST_NAMESPACE" exec storage-client -- sh -ceu "printf x >>/rbd/latency; printf y >>/cephfs/latency; sync" >/dev/null || return 1
  s3_sigv4_put "latency-${suffix}" >/dev/null
}

sample_business_latency() {
  local label="$1" count="${2:-12}" file i start end
  file="$STATE_DIR/${label}.latency"
  : >"$file"
  for i in $(seq 1 "$count"); do
    start=$(date +%s%N)
    business_once "${label}-${i}" || {
      fail CEPH-25 product "mixed workload failed phase=$label iteration=$i"
      return 1
    }
    end=$(date +%s%N)
    echo $(((end - start) / 1000000)) >>"$file"
  done
  python3 - "$file" <<'PY'
import math, statistics, sys
xs=sorted(int(x) for x in open(sys.argv[1]) if x.strip())
def q(p): return xs[max(0, math.ceil(len(xs)*p)-1)]
print(f"count={len(xs)} mean={statistics.fmean(xs):.2f} p95={q(.95)} p99={q(.99)} max={max(xs)}")
PY
}

latency_quantile() {
  local file="$1" percentile="$2"
  python3 - "$file" "$percentile" <<'PY'
import math,sys
xs=sorted(int(x) for x in open(sys.argv[1]) if x.strip())
print(xs[max(0, math.ceil(len(xs)*float(sys.argv[2]))-1)])
PY
}

slow_osd_id() {
  local backing
  backing="$(ssh_node "$SLOW_HOST" "readlink -f /dev/mapper/$SLOW_MAPPER")"
  ssh_node "$SLOW_HOST" '/usr/local/sbin/cephadm ceph-volume -- lvm list --format json' | \
    python3 "$CONTRACT_SCRIPT" osd-ids --device "$backing" --single
}

verify_post_fault_hashes() {
  local id="$1" expected actual marker
  expected="$(<"$STATE_DIR/rados.sha256")"
  actual="$(ceph_shell rados -p "$RBD_POOL" get kubeauto-marker - | sha256sum | awk '{print $1}')"
  [[ "$actual" == "$expected" ]] || fail "$id" product "post-fault RADOS hash mismatch"
  marker="$(<"$STATE_DIR/csi-marker")"
  [[ "$("${K[@]}" -n "$TEST_NAMESPACE" exec storage-client -- cat /rbd/marker)" == "$marker" ]] || fail "$id" product "post-fault RBD mismatch"
  [[ "$("${K[@]}" -n "$TEST_NAMESPACE" exec storage-client -- cat /cephfs/marker)" == "$marker" ]] || fail "$id" product "post-fault CephFS mismatch"
  "${K[@]}" -n "$TEST_NAMESPACE" exec s3-client -- sh -ceu 'mc cp ceph/kubeauto-ceph-test/marker /tmp/post-fault; expected="$(sha256sum </tmp/marker)"; actual="$(sha256sum </tmp/post-fault)"; test "$expected" = "$actual"' >/dev/null || fail "$id" product "post-fault S3 mismatch"
}

ensure_performance_baseline() {
  if [[ "${CASE_DONE[CEPH-25]:-}" == 1 ]]; then
    BASELINE_P99="$(latency_quantile "$STATE_DIR/healthy.latency" .99)"
    return
  fi
  stage CEPH-25 healthy-performance-baseline
  local summary
  summary="$(sample_business_latency healthy 15)"
  BASELINE_P99="$(latency_quantile "$STATE_DIR/healthy.latency" .99)"
  echo "CEPH_PERFORMANCE_SAMPLE phase=healthy $summary"
  pass CEPH-25 "$summary"
  echo CEPH_PERFORMANCE_BASELINE_PASS
}

prepare_slow_osd_context() {
  OSD_ID="$(slow_osd_id)"
  [[ "$OSD_ID" =~ ^[0-9]+$ ]] || fail CEPH-13 test-gate "cannot map slow device to OSD"
  if [[ ! -s "$FAULT_TABLE_FILE" ]]; then
    local volumes fixture type path backing native table fsid
    fsid="$(<"$FSID_FILE")"
    volumes="$(ssh_node "$SLOW_HOST" '/usr/local/sbin/cephadm ceph-volume -- lvm list --format json')"
    for type in block db; do
      fixture="$SLOW_MAPPER"
      [[ "$type" != db ]] || fixture="$BLUEFS_MAPPER"
      backing="$(ssh_node "$SLOW_HOST" "readlink -f /dev/mapper/$fixture")"
      path="$(python3 "$CONTRACT_SCRIPT" osd-lv --osd-id "$OSD_ID" --type "$type" --fsid "$fsid" --device "$backing" <<<"$volumes")"
      native="$(ssh_node "$SLOW_HOST" "set -euo pipefail; lsblk -srnp -o PATH '$path' | grep -Fx '$backing' >/dev/null; lsblk -dn -o NAME '$path'")"
      [[ "$native" =~ ^ceph--[A-Za-z0-9-]+$ ]] || fail CEPH-13 test-gate "unowned native OSD mapping"
      table="$(ssh_node "$SLOW_HOST" "dmsetup table '$native'")"
      python3 "$CONTRACT_SCRIPT" delay-table --delay-ms 1 <<<"$table" >/dev/null
      [[ "$table" != *$'\n'* ]] || fail CEPH-13 test-gate "multi-segment OSD mapping is outside the fixture boundary"
      printf '%s|%s|%s|%s\n' "$SLOW_HOST" "$fixture" "$native" "$table" >>"$FAULT_TABLE_FILE"
    done
  fi
  ceph_shell ceph config set global osd_op_complaint_time 2
}

read_osd_cgroup_io() {
  local file="$1"
  [[ "$file" == io.stat || "$file" == io.max ]] || return 1
  ssh_node "$SLOW_HOST" "set -euo pipefail; cg=\$(systemctl show '$THROTTLE_UNIT' --property=ControlGroup --value); test -n \"\$cg\"; cat \"/sys/fs/cgroup\$cg/$file\""
}

restore_throttle() {
  [[ -n "$THROTTLE_UNIT" ]] || return 0
  ssh_node "$SLOW_HOST" "systemctl set-property --runtime '$THROTTLE_UNIT' IOReadIOPSMax= IOWriteIOPSMax="
  read_osd_cgroup_io io.max | python3 "$CONTRACT_SCRIPT" io-limit \
    --device "$THROTTLE_MAJOR_MINOR" --expected max || fail CEPH-16 test-gate "kernel I/O throttle did not clear"
  # Keep the unit/device available for the identical recovered phase.
}

run_throttle_phase() {
  local phase="$1" before after summary file="$STATE_DIR/throttle-$1.bench.json"
  before="$(read_osd_cgroup_io io.stat | python3 "$CONTRACT_SCRIPT" io-snapshot --device "$THROTTLE_MAJOR_MINOR")"
  ceph_shell timeout 180 rados -p "$RBD_POOL" bench 30 write -b 4096 --object-size 4194304 -t 32 \
    --no-cleanup --run-name "kubeauto-throttle-$phase" --format json >"$file" &
  INJECTOR_PID=$!
  ssh_node "$SLOW_HOST" 'iostat -y -x 1 3'
  summary="$(sample_business_latency "throttle-$phase" 4)"
  kill -0 "$INJECTOR_PID" 2>/dev/null || fail CEPH-16 test-gate "benchmark did not span the mixed workload phase=$phase"
  ceph_shell ceph osd perf
  wait "$INJECTOR_PID" || {
    cat "$file"
    fail CEPH-16 product "RADOS demand workload failed phase=$phase"
    return 1
  }
  INJECTOR_PID=
  after="$(read_osd_cgroup_io io.stat | python3 "$CONTRACT_SCRIPT" io-snapshot --device "$THROTTLE_MAJOR_MINOR")"
  echo "CEPH_THROTTLE_SAMPLE phase=$phase before=$before after=$after mixed='$summary'"
  cat "$file"
  python3 "$CONTRACT_SCRIPT" throttle-phase --before "$before" --after "$after" \
    <"$file" >"$STATE_DIR/throttle-$phase.json" || fail CEPH-16 test-gate "invalid sustained I/O sample phase=$phase"
  echo "CEPH_THROTTLE_PHASE phase=$phase evidence=$(<"$STATE_DIR/throttle-$phase.json")"
  ceph_shell rados -p "$RBD_POOL" cleanup --run-name "kubeauto-throttle-$phase" >/dev/null
}

run_fixed_slow_osd() {
  ensure_performance_baseline
  stage CEPH-13 fixed-dm-delay
  mapper_table "$SLOW_MAPPER" delay 3000
  local summary p99 health observed_health=false
  sample_business_latency slow-fixed 15 >"$STATE_DIR/slow-fixed.summary" &
  INJECTOR_PID=$!
  while kill -0 "$INJECTOR_PID" 2>/dev/null; do
    health="$(ceph_shell ceph health detail)"
    printf 'CEPH_SLOW_HEALTH_SAMPLE id=CEPH-13 time=%s\n%s\n' "$(date -u +%FT%TZ)" "$health"
    if [[ "$health" == *"slow ops"* || "$health" == *"SLOW_OPS"* || "$health" == *"BLUESTORE_SLOW_OP_ALERT"* ]]; then
      observed_health=true
    fi
    ssh_node "$SLOW_HOST" "iostat -y -x 1 2"
    ceph_shell ceph osd perf
    sleep 2
  done
  wait "$INJECTOR_PID" || {
    fail CEPH-13 product "slow workload did not complete without errors"
    return 1
  }
  INJECTOR_PID=
  summary="$(<"$STATE_DIR/slow-fixed.summary")"
  unlink "$STATE_DIR/slow-fixed.summary"
  p99="$(latency_quantile "$STATE_DIR/slow-fixed.latency" .99)"
  ceph_shell ceph daemon "osd.${OSD_ID}" dump_historic_ops
  ceph_shell ceph osd dump | grep -Eq "^osd\.${OSD_ID} up[[:space:]].* in[[:space:]]" || fail CEPH-13 product "slow OSD did not remain up/in"
  ((p99 > BASELINE_P99 * 2 && p99 > 1000)) || fail CEPH-13 product "slow p99 did not cross threshold"
  [[ "$observed_health" == true ]] ||
    fail CEPH-13 product "Ceph did not expose a slow-operation health signal"
  echo "CEPH_SLOW_OSD_EVIDENCE device_await_queue=iostat osd_commit_latency_apply_latency=ceph_osd_perf pg_slow_ops=historic business_p95_p99='$summary' RBD=checked CephFS=checked S3=checked"
  mapper_table "$SLOW_MAPPER" linear 0
  wait_clean
  pass CEPH-13 "$summary osd=$OSD_ID remained=up/in"
  echo CEPH_SLOW_OSD_FIXED_PASS
}

run_intermittent_slow_osd() {
  ensure_performance_baseline
  stage CEPH-14 intermittent-dm-delay
  (
    for _ in 1 2 3; do
      mapper_table "$SLOW_MAPPER" delay 2500
      sleep 5
      mapper_table "$SLOW_MAPPER" linear 0
      sleep 5
    done
  ) &
  INJECTOR_PID=$!
  local summary p99
  summary="$(sample_business_latency slow-intermittent 18)"
  wait "$INJECTOR_PID"
  INJECTOR_PID=
  mapper_table "$SLOW_MAPPER" linear 0
  p99="$(latency_quantile "$STATE_DIR/slow-intermittent.latency" .99)"
  ((p99 > BASELINE_P99 * 2)) || fail CEPH-14 product "intermittent p99 not detected"
  pass CEPH-14 "$summary"
  echo CEPH_SLOW_OSD_INTERMITTENT_PASS
}

run_slow_vs_down() {
  ensure_performance_baseline
  stage CEPH-15 slow-versus-down
  mapper_table "$SLOW_MAPPER" delay 3000
  local slow_summary slow_p99 down_summary down_p99
  slow_summary="$(sample_business_latency compare-slow 12)"
  slow_p99="$(latency_quantile "$STATE_DIR/compare-slow.latency" .99)"
  mapper_table "$SLOW_MAPPER" linear 0
  ceph_shell ceph orch daemon stop "osd.${OSD_ID}"
  for _ in $(seq 1 30); do
    ceph_shell ceph osd dump --format json | python3 -c 'import json,sys; oid=int(sys.argv[1]); row=next(x for x in json.load(sys.stdin)["osds"] if x["osd"]==oid); raise SystemExit(0 if row["up"]==0 else 1)' "$OSD_ID" && break
    sleep 2
  done
  ceph_shell ceph osd dump --format json | python3 -c 'import json,sys; oid=int(sys.argv[1]); row=next(x for x in json.load(sys.stdin)["osds"] if x["osd"]==oid); assert row["up"]==0' "$OSD_ID" || fail CEPH-12 product "OSD did not become down"
  down_summary="$(sample_business_latency osd-down 12)"
  down_p99="$(latency_quantile "$STATE_DIR/osd-down.latency" .99)"
  ceph_shell ceph orch daemon start "osd.${OSD_ID}"
  wait_clean
  ((slow_p99 > down_p99)) || fail CEPH-15 product "slow OSD was not worse than down OSD"
  pass CEPH-12 "$down_summary"
  echo CEPH_OSD_DOWN_RECOVERY_PASS
  pass CEPH-15 "slow_p99=$slow_p99 down_p99=$down_p99 slow='$slow_summary'"
  echo CEPH_SLOW_VS_DOWN_PASS
}

run_osd_throttle() {
  stage CEPH-16 cgroup-io-throttling-classification
  THROTTLE_UNIT="ceph-$(<"$FSID_FILE")@osd.${OSD_ID}.service"
  THROTTLE_DEVICE="$(awk -F'|' -v name="$SLOW_MAPPER" '$2 == name {print $3}' "$MAPPER_FILE")"
  THROTTLE_MAJOR_MINOR="$(ssh_node "$SLOW_HOST" "lsblk -dn -o MAJ:MIN '$THROTTLE_DEVICE'")"
  read_osd_cgroup_io io.max | python3 "$CONTRACT_SCRIPT" io-limit \
    --device "$THROTTLE_MAJOR_MINOR" --expected max || fail CEPH-16 environment "OSD has a pre-existing IOPS ceiling"
  run_throttle_phase healthy
  ssh_node "$SLOW_HOST" "systemctl set-property --runtime '$THROTTLE_UNIT' 'IOReadIOPSMax=$THROTTLE_DEVICE 25' 'IOWriteIOPSMax=$THROTTLE_DEVICE 25'"
  read_osd_cgroup_io io.max | python3 "$CONTRACT_SCRIPT" io-limit \
    --device "$THROTTLE_MAJOR_MINOR" --expected 25 || fail CEPH-16 test-gate "kernel IOPS ceiling was not applied"
  run_throttle_phase limited
  restore_throttle
  run_throttle_phase recovered
  python3 "$CONTRACT_SCRIPT" throttle-effect --limit 25 \
    --healthy "$(<"$STATE_DIR/throttle-healthy.json")" \
    --limited "$(<"$STATE_DIR/throttle-limited.json")" \
    --recovered "$(<"$STATE_DIR/throttle-recovered.json")" </dev/null || fail CEPH-16 test-gate "IOPS impact/recovery was not demonstrated"
  THROTTLE_UNIT=
  THROTTLE_DEVICE=
  THROTTLE_MAJOR_MINOR=
  pass CEPH-16 "limit_iops=25 kernel_limit_and_device_IO_and_latency_effect_and_recovery_verified"
  echo CEPH_OSD_THROTTLE_CLASSIFICATION_PASS
}

sample_during_pg_activity() {
  local id="$1" label="$2" activity="$3" pool="$4" observed=false snapshot
  local -a assertion=(pg-activity --activity "$activity")
  [[ "$activity" != recovery ]] || assertion+=(--osd-id "$OSD_ID")
  sample_business_latency "$label" 12 >"$STATE_DIR/$label.summary" &
  INJECTOR_PID=$!
  while kill -0 "$INJECTOR_PID" 2>/dev/null; do
    snapshot="$(ceph_shell ceph pg ls-by-pool "$pool" --format json)"
    if kill -0 "$INJECTOR_PID" 2>/dev/null &&
       python3 "$CONTRACT_SCRIPT" "${assertion[@]}" <<<"$snapshot" >/dev/null 2>&1; then
      observed=true
      printf 'CEPH_PG_ACTIVITY_OVERLAP id=%s activity=%s time=%s snapshot=%s\n' "$id" "$activity" "$(date -u +%FT%TZ)" "$snapshot"
    fi
    sleep 1
  done
  wait "$INJECTOR_PID" || {
    fail "$id" product "mixed workload failed during $activity"
    return 1
  }
  INJECTOR_PID=
  cat "$STATE_DIR/$label.summary"
  [[ "$observed" == true ]] || fail "$id" test-gate "no PG activity overlapped the mixed business sample"
}

run_slow_recovery() {
  stage CEPH-17 slow-plus-recovery
  ceph_shell timeout 180 rados -p "$RBD_POOL" bench 120 write -b 4194304 -t 16 \
    --max-objects 128 --no-cleanup --run-name kubeauto-recovery --format json
  wait_clean
  RECOVERY_OSD_ID="$(ceph_shell ceph pg ls-by-pool "$RBD_POOL" --format json |
    python3 "$CONTRACT_SCRIPT" recovery-target --osd-id "$OSD_ID")"
  mapper_table "$SLOW_MAPPER" delay 2500
  ceph_shell ceph osd out "$RECOVERY_OSD_ID"
  sample_during_pg_activity CEPH-17 slow-recovery recovery "$RBD_POOL"
  ceph_shell ceph osd dump | grep -Eq "^osd\.${OSD_ID} up[[:space:]].* in[[:space:]]" ||
    fail CEPH-17 product "delayed OSD did not remain up/in during recovery"
  mapper_table "$SLOW_MAPPER" linear 0
  ceph_shell ceph osd in "$RECOVERY_OSD_ID"
  wait_clean
  pass CEPH-17 "$(<"$STATE_DIR/slow-recovery.summary") slow_osd=$OSD_ID recovery_osd=$RECOVERY_OSD_ID overlap_verified=true"
  RECOVERY_OSD_ID=
  ceph_shell rados -p "$RBD_POOL" cleanup --run-name kubeauto-recovery >/dev/null
  echo CEPH_SLOW_OSD_RECOVERY_PASS
}

run_scrub_contention() {
  stage CEPH-18 scrub-contention
  local pool=kubeauto-scrub-fixture pgid
  ceph_shell ceph osd pool ls --format json | python3 -c 'import json,sys; assert "kubeauto-scrub-fixture" not in json.load(sys.stdin)' ||
    fail CEPH-18 test-gate "scrub fixture pool already exists"
  ceph_shell ceph osd pool create "$pool" 1 1 replicated
  ceph_shell ceph osd pool set "$pool" pg_autoscale_mode off
  ceph_shell ceph osd pool application enable "$pool" rados
  ceph_shell timeout 600 rados -p "$pool" bench 480 write -b 4194304 -t 16 \
    --max-objects 1024 --no-cleanup --run-name kubeauto-scrub --format json
  wait_clean
  pgid="$(ceph_shell ceph pg ls-by-pool "$pool" --format json | python3 -c 'import json,sys; rows=json.load(sys.stdin)["pg_stats"]; assert len(rows)==1 and rows[0]["stat_sum"]["num_bytes"]>=4294967296; print(rows[0]["pgid"])')"
  ceph_shell ceph pg deep-scrub "$pgid"
  sample_during_pg_activity CEPH-18 scrub-contention deep-scrub "$pool"
  wait_clean
  ceph_shell ceph config set mon mon_allow_pool_delete true
  ceph_shell ceph osd pool rm "$pool" "$pool" --yes-i-really-really-mean-it >/dev/null
  ceph_shell ceph config set mon mon_allow_pool_delete false
  pass CEPH-18 "$(<"$STATE_DIR/scrub-contention.summary") pg=$pgid overlap_verified=true"
  echo CEPH_SCRUB_CONTENTION_PASS
}

run_bluefs_slow() {
  ensure_performance_baseline
  stage CEPH-19 bluefs-db-delay
  local -a affected_osds=("$OSD_ID")
  mapper_table "$BLUEFS_MAPPER" delay 2500
  local summary health p99
  summary="$(sample_business_latency bluefs-slow 12)"
  p99="$(latency_quantile "$STATE_DIR/bluefs-slow.latency" .99)"
  health="$(ceph_shell ceph health detail || true)"
  mapper_table "$BLUEFS_MAPPER" linear 0
  wait_clean
  ((p99 > BASELINE_P99 * 2)) || fail CEPH-19 product "BlueFS DB delay did not affect business p99"
  pass CEPH-19 "$summary affected_osds=${affected_osds[*]} health_captured=$([[ -n "$health" ]] && echo yes || echo no)"
  echo CEPH_BLUEFS_SLOW_PASS
}

run_slow_differential() {
  stage CEPH-20 non-media-differential
  local memory swap
  ceph_shell ceph daemon "osd.${OSD_ID}" compact
  ceph_shell ceph daemon "osd.${OSD_ID}" perf dump
  memory="$(ssh_node "$SLOW_HOST" "systemctl show 'ceph-$(<"$FSID_FILE")@osd.${OSD_ID}.service' -p MemoryCurrent -p MemoryHigh -p MemoryMax --value")"
  swap="$(ssh_node "$SLOW_HOST" 'swapon --show --noheadings || true')"
  printf 'CEPH_OSD_RESOURCE_SAMPLE osd=%s memory=%s swap=%s\n' "$OSD_ID" "$memory" "$swap"
  pass CEPH-20 "rocksdb_compaction=complete memory='$memory' swap_present=$([[ -n "$swap" ]] && echo yes || echo no)"
  echo CEPH_SLOW_OSD_DIFFERENTIAL_PASS
}

run_hot_pg_differential() {
  stage CEPH-21 hot-pg-differential
  local mapping
  mapping="$(ceph_shell ceph osd map "$RBD_POOL" kubeauto-hot-object)"
  for _ in $(seq 1 100); do printf x | ceph_shell rados -p "$RBD_POOL" put kubeauto-hot-object - >/dev/null; done
  [[ "$mapping" == *"kubeauto-hot-object"* && "$mapping" == *"acting"* ]] || fail CEPH-21 product "hot object PG mapping is incomplete"
  ceph_shell ceph osd perf
  pass CEPH-21 "same_object_writes=100 object_mapping='$mapping' device_latency_sampled=true"
  echo CEPH_HOT_PG_DIFFERENTIAL_PASS
}

clear_netem() {
  [[ -n "$NETEM_IFACE" ]] || return 0
  local state
  state="$(ssh_node "$SLOW_HOST" "tc qdisc show dev '$NETEM_IFACE'")"
  [[ "$state" == *'qdisc netem 1:'* ]] || fail CEPH-22 test-gate "owned qdisc missing before cleanup"
  ssh_node "$SLOW_HOST" "tc qdisc del dev '$NETEM_IFACE' root"
  state="$(ssh_node "$SLOW_HOST" "tc qdisc show dev '$NETEM_IFACE'")"
  [[ "$state" != *'qdisc netem 1:'* ]] || fail CEPH-22 test-gate "owned qdisc remains after cleanup"
  NETEM_IFACE=
}

run_network_differential() {
  stage CEPH-22 network-vs-disk
  NETEM_IFACE="$(ssh_node "$SLOW_HOST" "ip route get '${CEPH_HOSTS[1]}' | awk '{for(i=1;i<=NF;i++) if(\$i==\"dev\") {print \$(i+1); exit}}'")"
  [[ "$NETEM_IFACE" =~ ^[A-Za-z0-9_.:-]+$ && "$NETEM_IFACE" != lo ]] || fail CEPH-22 test-gate "invalid or loopback interface"
  ssh_node "$SLOW_HOST" "! tc qdisc show dev '$NETEM_IFACE' | grep -Eq 'qdisc (netem|tbf|htb) '" || fail CEPH-22 environment "foreign root qdisc exists"
  ssh_node "$SLOW_HOST" "tc qdisc add dev '$NETEM_IFACE' root handle 1: netem delay 100ms loss 0.1%"
  local summary qdisc
  summary="$(sample_business_latency network-delay 10)"
  qdisc="$(ssh_node "$SLOW_HOST" "tc -s qdisc show dev '$NETEM_IFACE'")"
  printf 'CEPH_NETEM_SAMPLE interface=%s stats=%s\n' "$NETEM_IFACE" "$qdisc"
  [[ "$qdisc" == *"qdisc netem 1:"* ]] || fail CEPH-22 test-gate "owned netem qdisc missing"
  clear_netem
  pass CEPH-22 "$summary netem_packets_captured=true"
  echo CEPH_NETWORK_VS_DISK_PASS
}

run_slow_remediation() {
  stage CEPH-23 safe-slow-osd-remediation
  ceph_shell ceph df
  ceph_shell ceph osd df tree
  ceph_shell ceph osd out "$OSD_ID"
  wait_clean
  ceph_shell ceph osd safe-to-destroy "$OSD_ID" >/dev/null || fail CEPH-23 product "OSD not safe to stop after backfill"
  ceph_shell ceph orch daemon stop "osd.${OSD_ID}"
  ceph_shell ceph orch daemon start "osd.${OSD_ID}"
  ceph_shell ceph osd in "$OSD_ID"
  wait_clean
  pass CEPH-23 capacity_CRUSH_safe_to_destroy_and_reintegration_complete
  echo CEPH_SLOW_OSD_REMEDIATION_PASS
}

run_post_fault_integrity() {
  verify_post_fault_hashes CEPH-24
  pass CEPH-24 RADOS_RBD_CephFS_S3_hashes_preserved
  echo CEPH_POST_FAULT_INTEGRITY_PASS
}

run_slow_device_scenario() {
  local mode="$1"
  [[ "$mode" == --performance || "$mode" == --upgrade ]] || prepare_slow_osd_context
  case "$mode" in
    --upgrade) run_upgrade ;;
    --slow-osd-fixed) run_fixed_slow_osd ;;
    --slow-osd-intermittent) run_intermittent_slow_osd ;;
    --slow-vs-down) run_slow_vs_down ;;
    --osd-throttle) run_osd_throttle ;;
    --slow-osd-recovery) run_slow_recovery ;;
    --scrub-contention) run_scrub_contention ;;
    --bluefs-slow) run_bluefs_slow ;;
    --slow-osd-differential) run_slow_differential ;;
    --hot-pg-differential) run_hot_pg_differential ;;
    --network-vs-disk) run_network_differential ;;
    --slow-osd-remediate) run_slow_remediation ;;
    --performance) ensure_performance_baseline ;;
    *) fail CEPH-13 test-gate "unknown focused mode=$mode" ;;
  esac
  ceph_shell ceph config rm global osd_op_complaint_time >/dev/null 2>&1 || true
}

run_all_slow_device_scenarios() {
  local mode
  for mode in --performance --slow-osd-fixed --slow-osd-intermittent --slow-vs-down \
    --osd-throttle --slow-osd-recovery --scrub-contention --bluefs-slow \
    --slow-osd-differential --hot-pg-differential --network-vs-disk --slow-osd-remediate; do
    run_slow_device_scenario "$mode"
  done
  run_post_fault_integrity
}

verify_observability() {
  stage CEPH-27 monitoring-dashboard
  ceph_shell ceph mgr module ls --format json | python3 -c 'import json,sys; d=json.load(sys.stdin); assert "dashboard" in d["enabled_modules"]; assert "prometheus" in d["enabled_modules"]'
  for service in prometheus alertmanager node-exporter grafana; do
    ceph_shell ceph orch ps --service_name "$service" --format json | python3 -c 'import json,sys; rows=json.load(sys.stdin); assert rows and all(x["status_desc"]=="running" for x in rows)'
  done
  local metrics_url initial_metric warning_metric recovered_metric health
  metrics_url="$(ceph_shell ceph mgr services --format json | python3 -c 'import json,sys; print(json.load(sys.stdin)["prometheus"].rstrip("/") + "/metrics")')"
  initial_metric="$(python3 - "$metrics_url" <<'PY'
import re, sys, urllib.request
body = urllib.request.urlopen(sys.argv[1], timeout=10).read().decode()
match = re.search(r"^ceph_health_status(?:\{[^}]*\})?\s+([0-9.]+)$", body, re.M)
assert match
print(int(float(match.group(1))))
PY
)"
  [[ "$initial_metric" -eq 0 ]] || fail CEPH-27 product "cluster was not healthy before observability injection"
  ceph_shell ceph osd pool create kubeauto-observability 1 >/dev/null
  ceph_shell ceph osd pool set kubeauto-observability size 1 --yes-i-really-mean-it >/dev/null
  ceph_shell ceph config set mon mon_warn_on_pool_no_redundancy true
  warning_metric=0
  for _ in $(seq 1 30); do
    health="$(ceph_shell ceph health detail || true)"
    warning_metric="$(python3 - "$metrics_url" <<'PY'
import re, sys, urllib.request
body = urllib.request.urlopen(sys.argv[1], timeout=10).read().decode()
match = re.search(r"^ceph_health_status(?:\{[^}]*\})?\s+([0-9.]+)$", body, re.M)
print(int(float(match.group(1))) if match else -1)
PY
)"
    [[ "$health" == *"POOL_NO_REDUNDANCY"* && "$warning_metric" -gt 0 ]] && break
    sleep 2
  done
  [[ "$health" == *"POOL_NO_REDUNDANCY"* && "$warning_metric" -gt 0 ]] || fail CEPH-27 product "controlled health warning was not exported"
  ceph_shell ceph osd pool set kubeauto-observability size 3 >/dev/null
  wait_clean
  recovered_metric=-1
  for _ in $(seq 1 30); do
    recovered_metric="$(python3 - "$metrics_url" <<'PY'
import re, sys, urllib.request
body = urllib.request.urlopen(sys.argv[1], timeout=10).read().decode()
match = re.search(r"^ceph_health_status(?:\{[^}]*\})?\s+([0-9.]+)$", body, re.M)
print(int(float(match.group(1))) if match else -1)
PY
)"
    [[ "$recovered_metric" -eq 0 ]] && break
    sleep 2
  done
  [[ "$recovered_metric" -eq 0 ]] || fail CEPH-27 product "health metric did not recover"
  ceph_shell ceph config set mon mon_allow_pool_delete true
  ceph_shell ceph osd pool rm kubeauto-observability kubeauto-observability --yes-i-really-really-mean-it >/dev/null
  ceph_shell ceph config set mon mon_allow_pool_delete false
  ceph_shell ceph config rm mon mon_warn_on_pool_no_redundancy >/dev/null 2>&1 || true
  pass CEPH-27 dashboard_monitoring_health_metric_trigger_and_recovery_verified
  echo CEPH_OBSERVABILITY_PASS
}

verify_idempotence() {
  stage CEPH-06 second-product-run
  local before after before_osds after_osds before_auth after_auth
  before="$(ceph_shell ceph orch ps --format json | python3 -c 'import json,sys; rows=json.load(sys.stdin); stable=sorted((x.get("daemon_name"),x.get("service_name"),x.get("hostname")) for x in rows); print(stable)')"
  before_osds="$(ceph_shell ceph osd ls --format json)"
  before_auth="$(ceph_shell ceph auth ls --format json | python3 -c 'import json,sys; d=json.load(sys.stdin); print(sorted(x["entity"] for x in d["auth_dump"] if x["entity"].startswith("client.csi-")))')"
  "${KUBECLI[@]}" setup "$CLUSTER" 08 -e ceph_allow_test_mappers=true </dev/null
  after="$(ceph_shell ceph orch ps --format json | python3 -c 'import json,sys; rows=json.load(sys.stdin); stable=sorted((x.get("daemon_name"),x.get("service_name"),x.get("hostname")) for x in rows); print(stable)')"
  after_osds="$(ceph_shell ceph osd ls --format json)"
  after_auth="$(ceph_shell ceph auth ls --format json | python3 -c 'import json,sys; d=json.load(sys.stdin); print(sorted(x["entity"] for x in d["auth_dump"] if x["entity"].startswith("client.csi-")))')"
  [[ -n "$before" && "$before" == "$after" ]] || fail CEPH-06 product "daemon placement or identity changed"
  [[ "$before_osds" == "$after_osds" ]] || fail CEPH-06 product "OSD IDs changed"
  [[ "$before_auth" == "$after_auth" ]] || fail CEPH-06 product "CSI auth identities changed"
  [[ "$(ceph_shell ceph fsid | tail -n1 | tr -d '[:space:]')" == "$(<"$FSID_FILE")" ]] || fail CEPH-06 product "FSID changed"
  verify_post_fault_hashes CEPH-06
  pass CEPH-06 fsid_OSD_auth_placement_and_business_data_preserved
  echo CEPH_IDEMPOTENCE_PASS
}

cleanup_business_fixtures() {
  if [[ -f "${CLUSTER_DIR}/kubectl.kubeconfig" ]]; then
    "${K[@]}" get --raw=/readyz >/dev/null || return 1
    "${K[@]}" delete namespace "$TEST_NAMESPACE" --ignore-not-found --wait=true --timeout=10m >/dev/null || return 1
    "${K[@]}" delete storageclass ceph-rbd cephfs --ignore-not-found >/dev/null || return 1
    "${K[@]}" get namespace "$TEST_NAMESPACE" >/dev/null 2>&1 && return 1
    "${K[@]}" get storageclass ceph-rbd >/dev/null 2>&1 && return 1
    "${K[@]}" get storageclass cephfs >/dev/null 2>&1 && return 1
  fi
  if [[ -s "$FSID_FILE" ]]; then
    [[ "$(ceph_shell ceph fsid | tail -n1 | tr -d '[:space:]')" == "$(<"$FSID_FILE")" ]] || return 1
    if ceph_shell radosgw-admin bucket stats --bucket kubeauto-ceph-test >/dev/null 2>&1; then
      ceph_shell radosgw-admin bucket rm --bucket kubeauto-ceph-test --purge-objects >/dev/null || return 1
    fi
    if ceph_shell radosgw-admin user info --uid kubeauto-regression >/dev/null 2>&1; then
      ceph_shell radosgw-admin user rm --uid kubeauto-regression --purge-data >/dev/null || return 1
    fi
    ! ceph_shell radosgw-admin bucket stats --bucket kubeauto-ceph-test >/dev/null 2>&1 || return 1
    ! ceph_shell radosgw-admin user info --uid kubeauto-regression >/dev/null 2>&1 || return 1
  fi
}

failure_cleanup() {
  local rc=$?
  trap - EXIT INT TERM
  if [[ ! -f "$OWNER_FILE" ]]; then
    exit "$rc"
  fi
  [[ "$(<"$OWNER_FILE")" == kubeauto-ceph-regression ]] || {
    echo 'CEPH_STAGE_FAIL id=CEPH-28 class=test-gate reason=foreign cleanup owner' >&2
    exit 1
  }
  set +e
  if [[ -n "$INJECTOR_PID" ]]; then
    kill "$INJECTOR_PID" >/dev/null 2>&1
    wait "$INJECTOR_PID" >/dev/null 2>&1
  fi
  clear_netem >/dev/null 2>&1
  restore_throttle >/dev/null 2>&1
  if [[ -n "$RECOVERY_OSD_ID" ]]; then
    ceph_shell ceph osd in "$RECOVERY_OSD_ID" >/dev/null 2>&1
  fi
  if [[ -n "$OSD_ID" ]]; then
    ceph_shell ceph orch daemon start "osd.${OSD_ID}" >/dev/null 2>&1
    ceph_shell ceph osd in "$OSD_ID" >/dev/null 2>&1
  fi
  ceph_shell ceph config rm global osd_op_complaint_time >/dev/null 2>&1
  ceph_shell ceph osd unset noout >/dev/null 2>&1
  mapper_table "$SLOW_MAPPER" linear 0 >/dev/null 2>&1
  mapper_table "$BLUEFS_MAPPER" linear 0 >/dev/null 2>&1
  cleanup_business_fixtures
  bash "$BASE/tests/helpers/ceph-cleanup.sh"
  cleanup_rc=$?
  set -e
  ((rc == 0)) && rc=$cleanup_rc
  exit "$rc"
}

prepare_focused_environment() {
  if [[ -s "$FSID_FILE" ]]; then
    [[ -s "$OWNER_FILE" && "$(<"$OWNER_FILE")" == kubeauto-ceph-regression ]] ||
      fail CEPH-28 test-gate "focused cluster has no matching owner marker"
    bash "$BASE/tests/helpers/ceph-cleanup.sh"
  fi
  verify_compute_kernel_clients
  verify_supply_chain
  run_host_probe
  validate_allowlist_shape
  bash "$BASE/tests/helpers/ceph-cleanup.sh" --verify
  prepare_fault_mappers
  prepare_product_cluster
  wait_clean
  verify_rados_data
  deploy_business_fixtures
  deploy_rgw_fixture
}

main() {
  require_commands
  verify_matrix_contract
  case "$MODE" in
    --compute-python-bootstrap)
      acquire_host_lease
      bootstrap_compute_python
      return
      ;;
    --supply-chain-only)
      verify_supply_chain
      return
      ;;
    --disk-preflight-only)
      acquire_host_lease
      verify_compute_kernel_clients
      run_host_probe
      validate_allowlist_shape
      return
      ;;
    --slow-osd-fixed|--slow-osd-intermittent|--slow-vs-down|--osd-throttle|--slow-osd-recovery|--scrub-contention|--bluefs-slow|--slow-osd-differential|--hot-pg-differential|--network-vs-disk|--slow-osd-remediate|--performance|--upgrade)
      acquire_host_lease
      trap failure_cleanup EXIT
      trap 'exit 130' INT
      trap 'exit 143' TERM
      prepare_focused_environment
      run_slow_device_scenario "$MODE"
      run_post_fault_integrity
      cleanup_business_fixtures
      bash "$BASE/tests/helpers/ceph-cleanup.sh"
      trap - EXIT INT TERM
      echo "CEPH_FOCUSED_BRANCH_PASS mode=$MODE"
      return
      ;;
    full) ;;
    *) echo "usage: $0 [--compute-python-bootstrap|--supply-chain-only|--disk-preflight-only|--slow-osd-fixed|--slow-osd-intermittent|--slow-vs-down|--osd-throttle|--slow-osd-recovery|--scrub-contention|--bluefs-slow|--slow-osd-differential|--hot-pg-differential|--network-vs-disk|--slow-osd-remediate|--performance|--upgrade]" >&2; return 2 ;;
  esac

  acquire_host_lease
  verify_compute_kernel_clients
  verify_supply_chain
  run_host_probe
  validate_allowlist_shape
  bash "$BASE/tests/helpers/ceph-cleanup.sh" --verify
  trap failure_cleanup EXIT
  trap 'exit 130' INT
  trap 'exit 143' TERM
  prepare_fault_mappers
  prepare_product_cluster
  wait_clean
  verify_rados_data
  deploy_business_fixtures
  deploy_rgw_fixture
  verify_security
  run_upgrade
  run_all_slow_device_scenarios
  verify_observability
  verify_idempotence
  cleanup_business_fixtures
  bash "$BASE/tests/helpers/ceph-cleanup.sh"
  pass CEPH-28 owned_resources_removed_and_verified
  assert_cases_complete
  trap - EXIT INT TERM
  elapsed=$(( $(date +%s) - START_EPOCH ))
  echo "CEPH_DELIVERY_PASS cases=28 elapsed_seconds=$elapsed"
}

main "$@"
