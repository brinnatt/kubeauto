#!/usr/bin/env bash
# Sourced by the fixed enterprise runner; preserve its supervision functions.
ssh_elasticsearch() {
  ssh -o BatchMode=yes -o ConnectTimeout=10 -o ServerAliveInterval=5 \
    -o ServerAliveCountMax=2 root@192.168.122.2 "$@"
}

ssh_elasticsearch_secondary() {
  ssh -o BatchMode=yes -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
    -o ConnectTimeout=10 -o ServerAliveInterval=5 -o ServerAliveCountMax=2 root@192.168.47.130 "$@"
}

elasticsearch_inventory() {
  # Read-only discovery is not a host lease or qualification. Keep this inside
  # the fixed entry point and never read Secret or private kubeconfig contents.
  ssh_elasticsearch 'set -eu
printf "ELASTICSEARCH_POOL_INVENTORY_BEGIN\n"
for tool in docker nerdctl skopeo kubecli helm ansible-playbook python3; do command -v "$tool" || true; done
if command -v docker >/dev/null; then docker ps --format "{{.Names}} {{.Image}}"; fi
for config in /usr/local/kubeauto/clusters/aio/kubectl.kubeconfig /usr/local/kubeauto/clusters/k8s-dev/kubectl.kubeconfig /usr/local/kubeauto/clusters/elasticsearch-gate/kubectl.kubeconfig; do
  if test -s "$config"; then
    printf "CONTROL_CONTEXT %s\n" "$config"
    kubectl --kubeconfig="$config" --request-timeout=8s get ns -o name || true
    kubectl --kubeconfig="$config" --request-timeout=8s get pv,pvc -A || true
    kubectl --kubeconfig="$config" --request-timeout=8s get nodes -o wide || true
    kubectl --kubeconfig="$config" --request-timeout=8s get pods -A -o wide || true
  fi
done
for ip in 192.168.122.2 192.168.122.217 192.168.122.243 192.168.122.246 192.168.122.193 192.168.122.210 192.168.122.216 192.168.122.212; do
  printf "HOST %s\n" "$ip"
  ssh -o BatchMode=yes -o ConnectTimeout=10 "root@$ip" '\''
    set -eu
    uname -r
    getconf _NPROCESSORS_ONLN
    awk "/MemTotal|MemAvailable/ {print}" /proc/meminfo
    df -h / /var/lib
    systemctl is-active kubelet || true
    cat /etc/os-release
    findmnt -rn -o TARGET,SOURCE,FSTYPE | awk "/^\\/(data|var\\/lib|run\\/(calico|cilium))/ {print}"
    ip -br address
    ip route
    for unit in kubelet kube-proxy kube-apiserver kube-controller-manager kube-scheduler kube-lb etcd containerd docker cri-dockerd; do
      systemctl show "$unit" -p Id -p ActiveState -p FragmentPath -p DropInPaths --no-pager || true
    done
    for directory in /etc/systemd/system /run/systemd/system /usr/lib/systemd/system; do
      find "$directory" -maxdepth 2 -name "*.conf" -path "*.d/*" -print 2>/dev/null || true
    done
    getent hosts registry.talkschool.cn || true
    curl --noproxy "*" --connect-timeout 3 --max-time 5 -fsS -o /dev/null -w "registry_http=%{http_code}\\n" http://192.168.122.2:5000/v2/ || true
    lslocks -n -o COMMAND,PID,PATH | awk "/kubeauto/ {print}"
    for file in /tmp/kubeauto-*.pid; do
      test -f "$file" || continue
      pid=$(cat "$file")
      case "$pid" in ""|*[!0-9]*) continue;; esac
      if kill -0 "$pid" 2>/dev/null; then printf "ACTIVE_JOB %s %s\n" "$file" "$pid"; fi
    done
    for inventory in /usr/local/kubeauto/clusters/*/hosts; do
      test -f "$inventory" || continue
      printf "CLUSTER_INVENTORY %s\n" "$inventory"
      awk "/^192[.]168[.]/ {print \$1}" "$inventory"
    done
    if test -s /root/.kube/config && command -v kubectl >/dev/null; then
      kubectl --request-timeout=8s get ns kube-system -o jsonpath="cluster_uid={.metadata.uid}" || true
      kubectl --request-timeout=8s get nodes -o wide || true
      kubectl --request-timeout=8s get pods -A -o wide || true
    fi
  '\''
done
printf "ELASTICSEARCH_POOL_INVENTORY_END read_only=true lease=false\n"'
}

elasticsearch_inventory_secondary() {
  # Capacity/ownership discovery only; no source or DR host is admitted here.
  ssh -o BatchMode=yes -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=10 root@192.168.47.130 bash -s <<'SECONDARY_INVENTORY'
set -eu
inspect_host() {
  hostname
  cat /etc/machine-id
  uname -r
  getconf _NPROCESSORS_ONLN
  awk '/MemTotal|MemAvailable/ {print}' /proc/meminfo
  df -h /
  for service in kubelet containerd docker ceph.target; do systemctl is-active "$service" || true; done
  if command -v docker >/dev/null; then docker ps --format '{{.Names}} {{.Image}}'; fi
  if command -v crictl >/dev/null; then timeout 15 crictl ps -a 2>/dev/null || true; fi
  findmnt -rn -o TARGET,SOURCE,FSTYPE | awk '$1 ~ /^\/(data|var\/lib\/(kubelet|rook|ceph)|mnt|srv)(\/|$)/ {print}'
  for directory in /etc/kubernetes /var/lib/etcd /var/lib/kubelet /var/lib/ceph /var/lib/rook /usr/local/kubeauto/clusters; do
    if test -d "$directory"; then
      printf 'RETAINED_DIRECTORY path=%s entries=' "$directory"
      find "$directory" -mindepth 1 -maxdepth 1 -printf '%f ' 2>/dev/null
      printf '\n'
    fi
  done
  lslocks -n -o COMMAND,PID,PATH | awk '/kubeauto/ {print}'
  for file in /tmp/kubeauto-*.pid; do
    test -f "$file" || continue
    pid=$(cat "$file")
    case "$pid" in ""|*[!0-9]*) continue;; esac
    if kill -0 "$pid" 2>/dev/null; then printf "ACTIVE_JOB %s %s\n" "$file" "$pid"; fi
  done
  if test -s /root/.kube/config && command -v kubectl >/dev/null; then
    kubectl --request-timeout=8s get namespace kube-system -o jsonpath="cluster_uid={.metadata.uid}" || true
    kubectl --request-timeout=8s get nodes -o wide || true
    kubectl --request-timeout=8s get ns,pv,pvc -A || true
  fi
}
for ip in 192.168.122.135 192.168.122.40 192.168.122.72 192.168.122.212 192.168.122.165 192.168.122.238; do
  printf "ELASTICSEARCH_SECONDARY_INVENTORY host=%s read_only=true lease=false\n" "$ip"
  if [[ "$ip" == 192.168.47.130 ]]; then
    inspect_host
  else
    printf -v command '%s\ninspect_host' "$(declare -f inspect_host)"
    ssh -o BatchMode=yes -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=10 "root@$ip" "$command"
  fi
done
SECONDARY_INVENTORY
}

elasticsearch_launch() {
  local module="$1" marker="$2" ready launcher_pid out_fd in_fd rc=0 publication_sha run_id durable_sha
  "$ROOT/.venv/bin/python" "$ROOT/tests/helpers/elasticsearch-evidence.py" refresh || return
  if [[ "$module" != only ]]; then
    if ! "$ROOT/.venv/bin/python" "$ROOT/tests/helpers/elasticsearch-evidence.py" require-current --scope contract; then
      elasticsearch_local_gate contract ELASTICSEARCH_CONTRACT_SCOPED_CLEAN_PASS bash "$ROOT/tests/helpers/elasticsearch-static.sh" || return
    fi
  fi
  if [[ "$module" != cleanup && "$module" != admission && "$module" != registry-recover ]]; then
    if [[ "$module" != only ]]; then "$ROOT/.venv/bin/python" "$ROOT/tests/helpers/elasticsearch-artifacts.py" || return; fi
    publication_sha=$(sha256sum "$ROOT/tests/evidence/elasticsearch-images-20261103.json")
    publication_sha=${publication_sha%% *}
  else
    publication_sha=none
  fi
  run_id=$("$ROOT/.venv/bin/python" -c 'import uuid; print(uuid.uuid4().hex)')
  local directory="$ROOT/logs/elasticsearch-cases/$module-$(date +%Y%m%d-%H%M%S)-$run_id"
  "$ROOT/.venv/bin/python" "$ROOT/tests/helpers/elasticsearch-evidence.py" freeze "$directory" --scope "$module" || return
  coproc ELASTICSEARCH_LEASE {
    ssh_elasticsearch 'set -eu
exec 9>/var/lock/kubeauto-large-pool.lock
flock -n 9
exec 8>/var/lock/kubeauto-control-source.lock
flock -n 8
for file in /tmp/kubeauto-*.pid; do
  test -f "$file" || continue
  pid=$(cat "$file")
  case "$pid" in ""|*[!0-9]*) exit 1;; esac
  if kill -0 "$pid" 2>/dev/null; then exit 1; fi
done
printf "ELASTICSEARCH_LEASE_READY\n"
IFS= read -r module
IFS= read -r publication_sha
IFS= read -r run_id
[[ "$run_id" =~ ^[a-f0-9]{32}$ ]]
case "$module" in only|admission|registry-recover|bootstrap|bootstrap-resume|storage|csi-probe|standard|compact|business|lifecycle|security|security-identity|security-certificates|security-network|security-s3|security-certificate-restore|faults|restore-faults|maintenance|backup|backup-source|backup-refusal|backup-restore|backup-features|dr-bootstrap|dr|upgrade|upgrade-platform|coexistence|coexistence-isolation|coexistence-restores|logging|logging-cleanup|cleanup-probes|observability|soak|rally|performance|cleanup) ;; *) exit 2;; esac
if test "$publication_sha" != none; then
  actual=$(sha256sum /usr/local/kubeauto/tests/evidence/elasticsearch-images-20261103.json)
  test "${actual%% *}" = "$publication_sha"
fi
if test -f /tmp/kubeauto-elasticsearch-live.log; then
  install -d -m 0700 /var/tmp/kubeauto-elasticsearch-history
  archive=$(mktemp -d /var/tmp/kubeauto-elasticsearch-history/run.XXXXXX)
  cp /tmp/kubeauto-elasticsearch-live.log "$archive/"
  for suffix in pid exit finalized; do
    if test -f "/tmp/kubeauto-elasticsearch-gate.$suffix"; then cp "/tmp/kubeauto-elasticsearch-gate.$suffix" "$archive/"; fi
  done
fi
nohup env ELASTICSEARCH_PARENT_LEASE_HELD=yes ELASTICSEARCH_RUN_ID="$run_id" bash /usr/local/kubeauto/tests/helpers/run-durable-gate.sh /tmp/kubeauto-elasticsearch-gate ELASTICSEARCH_GATE_EXIT bash /usr/local/kubeauto/tests/helpers/elasticsearch-gate.sh "$module" >/tmp/kubeauto-elasticsearch-live.log 2>&1 </dev/null &
printf "ELASTICSEARCH_DURABLE_LAUNCHED\n"'
  }
  launcher_pid=$ELASTICSEARCH_LEASE_PID
  out_fd=${ELASTICSEARCH_LEASE[0]}
  in_fd=${ELASTICSEARCH_LEASE[1]}
  if ! read -r -t 30 ready <&"$out_fd" || [[ "$ready" != ELASTICSEARCH_LEASE_READY ]]; then
    echo 'ELASTICSEARCH_PREFLIGHT_REFUSED reason=pool-lease-or-active-control-job' >&2
    wait "$launcher_pid" || true
    return 1
  fi
  durable_sha=$(sha256sum "$ROOT/tests/helpers/run-durable-gate.sh")
  durable_sha=${durable_sha%% *}
  if ! ready=$(ssh_elasticsearch_secondary bash -s -- "$run_id" "$durable_sha" < "$ROOT/tests/helpers/elasticsearch-secondary-lease.sh") || [[ "$ready" != ELASTICSEARCH_SECONDARY_LEASE_READY ]]; then
    printf 'CANCEL\nnone\n%s\n' "$run_id" >&"$in_fd"
    wait "$launcher_pid" || true
    echo ELASTICSEARCH_PREFLIGHT_REFUSED reason=secondary-pool-lease >&2
    return 1
  fi
  if ! bash "$ROOT/tests/helpers/lab-control-ssh-bootstrap.sh" root@192.168.122.2 root@192.168.122.212; then
    printf 'CANCEL\nnone\n%s\n' "$run_id" >&"$in_fd"
    wait "$launcher_pid" || true
    cancel_remote_job elasticsearch-secondary ssh_elasticsearch_secondary '' /tmp/kubeauto-elasticsearch-secondary || true
    return 1
  fi
  if [[ "$module" == only || "$module" == backup || "$module" == backup-restore || "$module" == backup-features || "$module" == dr-bootstrap || "$module" == upgrade || "$module" == upgrade-platform || "$module" == dr ]]; then
    local target
    for target in 192.168.122.135 192.168.122.40 192.168.122.72; do
      if ! bash "$ROOT/tests/helpers/lab-control-ssh-bootstrap.sh" root@192.168.122.2 "root@$target"; then
        printf 'CANCEL\nnone\n%s\n' "$run_id" >&"$in_fd"
        wait "$launcher_pid" || true
        cancel_remote_job elasticsearch-secondary ssh_elasticsearch_secondary '' /tmp/kubeauto-elasticsearch-secondary || true
        return 1
      fi
    done
  fi
  if ! bash "$ROOT/tests/helpers/sync-kubeauto.sh" root@192.168.122.2; then
    printf 'CANCEL\nnone\n%s\n' "$run_id" >&"$in_fd"
    wait "$launcher_pid" || true
    cancel_remote_job elasticsearch-secondary ssh_elasticsearch_secondary '' /tmp/kubeauto-elasticsearch-secondary || true
    return 1
  fi
  printf '%s\n' "$module" "$publication_sha" "$run_id" >&"$in_fd"
  read -r -t 30 ready <&"$out_fd" || return 1
  [[ "$ready" == ELASTICSEARCH_DURABLE_LAUNCHED ]] || return 1
  wait "$launcher_pid" || return
  monitor_remote_job elasticsearch ssh_elasticsearch '' /tmp/kubeauto-elasticsearch-gate \
    /tmp/kubeauto-elasticsearch-live.log "$marker" || rc=$?
  remote_job_summary ssh_elasticsearch '' /tmp/kubeauto-elasticsearch-gate /tmp/kubeauto-elasticsearch-live.log > "$directory/durable-status.txt"
  ssh_elasticsearch 'cat /tmp/kubeauto-elasticsearch-live.log' > "$directory/raw.log" || return
  ssh_elasticsearch 'cat /tmp/kubeauto-elasticsearch-gate.exit' > "$directory/exit" || return
  ssh_elasticsearch 'cat /tmp/kubeauto-elasticsearch-gate.finalized' > "$directory/finalized" || return
  find "$directory" -maxdepth 1 -type f -exec chmod 0600 {} +
  # The guardian consumes the source durable exit, then releases .47.130.
  # A running/unreachable guardian remains a real delivery failure/lease.
  monitor_remote_job elasticsearch-secondary ssh_elasticsearch_secondary '' /tmp/kubeauto-elasticsearch-secondary \
    /tmp/kubeauto-elasticsearch-secondary.log 'ELASTICSEARCH_SECONDARY_EXIT rc=0' || { (( rc != 0 )) || rc=1; }
  remote_job_summary ssh_elasticsearch_secondary '' /tmp/kubeauto-elasticsearch-secondary /tmp/kubeauto-elasticsearch-secondary.log > "$directory/secondary-durable-status.txt"
  ssh_elasticsearch_secondary 'cat /tmp/kubeauto-elasticsearch-secondary.log' > "$directory/secondary-raw.log" || return
  find "$directory" -maxdepth 1 -type f -exec chmod 0600 {} +
  if [[ "$module" == only ]]; then
    "$ROOT/.venv/bin/python" "$ROOT/tests/helpers/elasticsearch-evidence.py" recover-children "$directory" || return
  fi
  if (( rc == 0 )); then
    "$ROOT/.venv/bin/python" "$ROOT/tests/helpers/elasticsearch-evidence.py" publish "$directory" --marker "$marker" --scope "$module" || return
  fi
  return "$rc"
}

elasticsearch_local_gate() {
  local module="$1" marker="$2" rc=0 directory
  shift 2
  "$ROOT/.venv/bin/python" "$ROOT/tests/helpers/elasticsearch-evidence.py" refresh || return
  directory="$ROOT/logs/elasticsearch-cases/$module-$(date +%Y%m%d-%H%M%S)-$RANDOM"
  "$ROOT/.venv/bin/python" "$ROOT/tests/helpers/elasticsearch-evidence.py" freeze "$directory" --scope "$module" || return
  bash "$ROOT/tests/helpers/run-durable-gate.sh" "$directory/run" ELASTICSEARCH_LOCAL_GATE_EXIT "$@" 2>&1 | tee "$directory/raw.log" || rc=$?
  cp "$directory/run.exit" "$directory/exit"
  cp "$directory/run.finalized" "$directory/finalized"
  find "$directory" -maxdepth 1 -type f -exec chmod 0600 {} +
  (( rc == 0 )) || return "$rc"
  "$ROOT/.venv/bin/python" "$ROOT/tests/helpers/elasticsearch-evidence.py" publish "$directory" --marker "$marker" --scope "$module"
}

elasticsearch_dispatch() {
  local mode="$1"
  case "$mode" in
    --elasticsearch-contract)
      elasticsearch_local_gate contract ELASTICSEARCH_CONTRACT_SCOPED_CLEAN_PASS bash "$ROOT/tests/helpers/elasticsearch-static.sh" ;;
    --elasticsearch-documentation)
      elasticsearch_local_gate documentation ELASTICSEARCH_DOCUMENTATION_SCOPED_CLEAN_PASS "$ROOT/.venv/bin/python" "$ROOT/tests/helpers/elasticsearch-full.py" --documentation "$ROOT" ;;
    --elasticsearch-evidence-recover)
      local directory
      for directory in "$ROOT"/logs/elasticsearch-cases/only-*; do
        [[ -d "$directory" ]] || continue
        "$ROOT/.venv/bin/python" "$ROOT/tests/helpers/elasticsearch-evidence.py" recover-children "$directory" || return
      done ;;
    --elasticsearch-inventory)
      elasticsearch_inventory ;;
    --elasticsearch-inventory-secondary)
      elasticsearch_inventory_secondary ;;
    --elasticsearch-artifacts)
      elasticsearch_local_gate artifacts 'ELASTICSEARCH_ARTIFACTS_SCOPED_CLEAN_PASS registry_mutation=false offline_manifests=verified' "$ROOT/.venv/bin/python" "$ROOT/tests/helpers/elasticsearch-artifacts.py" ;;
    --elasticsearch-only)
      elasticsearch_local_gate contract ELASTICSEARCH_CONTRACT_SCOPED_CLEAN_PASS bash "$ROOT/tests/helpers/elasticsearch-static.sh" || return
      elasticsearch_local_gate artifacts 'ELASTICSEARCH_ARTIFACTS_SCOPED_CLEAN_PASS registry_mutation=false offline_manifests=verified' "$ROOT/.venv/bin/python" "$ROOT/tests/helpers/elasticsearch-artifacts.py" || return
      "$ROOT/.venv/bin/python" "$ROOT/tests/helpers/elasticsearch-evidence.py" prepare-full || return
      elasticsearch_launch only 'ELASTICSEARCH_MODULE_PASS module=only' ;;
    --elasticsearch-admission|--elasticsearch-registry-recover|--elasticsearch-bootstrap|--elasticsearch-bootstrap-resume|--elasticsearch-storage|--elasticsearch-csi-probe|--elasticsearch-standard|--elasticsearch-compact|--elasticsearch-business|--elasticsearch-lifecycle|--elasticsearch-security|--elasticsearch-security-identity|--elasticsearch-security-certificates|--elasticsearch-security-network|--elasticsearch-security-s3|--elasticsearch-security-certificate-restore|--elasticsearch-faults|--elasticsearch-restore-faults|--elasticsearch-maintenance|--elasticsearch-backup|--elasticsearch-backup-source|--elasticsearch-backup-refusal|--elasticsearch-backup-restore|--elasticsearch-backup-features|--elasticsearch-dr-bootstrap|--elasticsearch-dr|--elasticsearch-upgrade|--elasticsearch-upgrade-platform|--elasticsearch-coexistence|--elasticsearch-coexistence-isolation|--elasticsearch-coexistence-restores|--elasticsearch-logging|--elasticsearch-logging-cleanup|--elasticsearch-cleanup-probes|--elasticsearch-observability|--elasticsearch-soak|--elasticsearch-rally|--elasticsearch-performance|--elasticsearch-cleanup)
      elasticsearch_launch "${mode#--elasticsearch-}" "ELASTICSEARCH_MODULE_PASS module=${mode#--elasticsearch-}" ;;
    --elasticsearch-stop-after-current)
      ssh_elasticsearch python3 - < "$ROOT/tests/helpers/elasticsearch-stop.py" ;;
    --elasticsearch-status)
      remote_job_summary ssh_elasticsearch '' /tmp/kubeauto-elasticsearch-gate /tmp/kubeauto-elasticsearch-live.log
      remote_log_tail ssh_elasticsearch '' /tmp/kubeauto-elasticsearch-live.log 80
      remote_job_summary ssh_elasticsearch_secondary '' /tmp/kubeauto-elasticsearch-secondary /tmp/kubeauto-elasticsearch-secondary.log
      remote_log_tail ssh_elasticsearch_secondary '' /tmp/kubeauto-elasticsearch-secondary.log 12
      ssh_elasticsearch_secondary 'for file in /tmp/kubeauto-*.pid; do test -f "$file" || continue; pid=$(cat "$file"); case "$pid" in ""|*[!0-9]*) continue;; esac; printf "CONTROL_JOB %s " "$file"; ps -p "$pid" -o pid=,stat=,comm= || true; done; lslocks -n -o PID,PATH | awk "/kubeauto/ {print}"'
      matrix_counts elasticsearch ;;
    --elasticsearch-follow)
      ssh_elasticsearch 'tail -n 80 -F /tmp/kubeauto-elasticsearch-live.log' ;;
    *)
      echo "ELASTICSEARCH_GATE_REFUSED reason=runtime-prerequisites-not-qualified mode=$mode" >&2
      return 1 ;;
  esac
}
