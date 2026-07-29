#!/bin/bash
# Enterprise regression driver — executes tests/enterprise-test-matrix.yaml coverage.
#
# This is the *only* command the development host needs to invoke for lab work:
# it owns SSH, source sync, cleanup, remote execution and log collection.  Keep
# remote commands inside this script so a delivery regression is autonomous
# rather than requiring a per-host operator confirmation.
#
# Run:    bash tests/run_enterprise_regression.sh
# Status: bash tests/run_enterprise_regression.sh --status
set -euo pipefail

require_command() {
  command -v "$1" >/dev/null 2>&1 || {
    echo "ERROR: required command '$1' is not installed." >&2
    exit 127
  }
}

require_command rsync

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
HOST138="ubuntu@192.168.47.138"
HOST130="root@192.168.47.130"
LOG="${ROOT}/logs/enterprise-regression-$(date +%Y%m%d-%H%M).log"
MODE="${1:-run}"
mkdir -p "${ROOT}/logs"
# Preserve the invoking terminal before later phases redirect their audit log.
# The final remote tail is deliberately written to these descriptors.
exec 3>&1 4>&2

ssh138() { ssh -o BatchMode=yes -o StrictHostKeyChecking=no -o ConnectTimeout=10 -o ServerAliveInterval=5 -o ServerAliveCountMax=2 "$HOST138" "$@"; }
ssh130() { ssh -o BatchMode=yes -o StrictHostKeyChecking=no -o ConnectTimeout=10 -o ServerAliveInterval=5 -o ServerAliveCountMax=2 "$HOST130" "$@"; }
scp138() { scp -o BatchMode=yes -o StrictHostKeyChecking=no -o ConnectTimeout=10 -o ServerAliveInterval=5 -o ServerAliveCountMax=2 "$@"; }

remote_job_summary() {
  local ssh_function="$1" privilege="$2" state_prefix="$3" remote_log="$4"
  local remote_script quoted_script
  remote_script="
pid=missing
rc=missing
state=starting
test -r '${state_prefix}.pid' && pid=\$(cat '${state_prefix}.pid')
if test -r '${state_prefix}.exit'; then
  rc=\$(cat '${state_prefix}.exit')
  state=exited
elif test \"\$pid\" != missing && kill -0 \"\$pid\" 2>/dev/null; then
  state=running
elif test \"\$pid\" != missing; then
  state=lost
fi
bytes=0
if test -r '${remote_log}'; then
  bytes=\$(wc -c < '${remote_log}')
  # Logs created before durable state files were introduced still contain the
  # wrapper's authoritative terminal marker.  Report those as exited rather
  # than leaving an obviously completed historical run in "starting" state.
  if test \"\$state\" = starting; then
    legacy_rc=\$(sed -n 's/^REGRESSION_.*_EXIT rc=\([0-9][0-9]*\)$/\1/p' '${remote_log}' | tail -n 1)
    if test -n \"\$legacy_rc\"; then
      rc=\$legacy_rc
      state=exited
    elif test \"\$pid\" = missing && test -s '${remote_log}' && test -n \"\$(find '${remote_log}' -mmin +1 -print 2>/dev/null)\"; then
      state=orphaned
    fi
  fi
fi
passes=0
failures=0
if test -r '${remote_log}'; then
  passes=\$(grep -c '^\\[PASS\\]' '${remote_log}' 2>/dev/null || true)
  # Ansible prints fatal FAILED even for tasks immediately followed by an
  # ignoring marker. Final recap failed>0 is authoritative; counting raw fatal
  # lines turns an ignored cleanup probe into a false regression.
  failures=\$(grep -Ec '^\\[FAIL\\]|Traceback|REGRESSION_.*EXIT rc=[1-9]|^[^ ]+[[:space:]]+:[[:space:]].*failed=[1-9]' '${remote_log}' 2>/dev/null || true)
  latest=\$(grep -E '^==========|^>>> |^\\[PASS\\]|^\\[FAIL\\]|^\\[WAIT\\]|^REGRESSION_' '${remote_log}' 2>/dev/null | tail -n 1 || true)
else
  latest='log-not-created'
fi
printf 'state=%s pid=%s rc=%s bytes=%s pass_markers=%s failure_markers=%s latest=%s\\n' \\
  \"\$state\" \"\$pid\" \"\$rc\" \"\$bytes\" \"\$passes\" \"\$failures\" \"\$latest\"
"
  printf -v quoted_script '%q' "$remote_script"
  if [[ -n "$privilege" ]]; then
    "$ssh_function" "$privilege bash -lc $quoted_script"
  else
    "$ssh_function" "bash -lc $quoted_script"
  fi
}

remote_log_tail() {
  local ssh_function="$1" privilege="$2" remote_log="$3" lines="${4:-100}"
  if [[ -n "$privilege" ]]; then
    "$ssh_function" "$privilege tail -n '$lines' '$remote_log' 2>/dev/null || true"
  else
    "$ssh_function" "tail -n '$lines' '$remote_log' 2>/dev/null || true"
  fi
}

remote_process_tree() {
  local ssh_function="$1" privilege="$2" state_prefix="$3"
  local remote_script quoted_script
  remote_script="
root=missing
test -r '${state_prefix}.pid' && root=\$(cat '${state_prefix}.pid')
echo process_tree_root=\"\$root\"
if test \"\$root\" != missing && command -v pstree >/dev/null 2>&1; then
  pstree -ap \"\$root\" || true
else
  ps -eo pid,ppid,etime,stat,wchan:24,cmd --forest | head -n 160
fi
"
  printf -v quoted_script '%q' "$remote_script"
  if [[ -n "$privilege" ]]; then
    "$ssh_function" "$privilege bash -lc $quoted_script"
  else
    "$ssh_function" "bash -lc $quoted_script"
  fi
}

remote_diagnostics() {
  local ssh_function="$1" privilege="$2" remote_log="$3"
  echo "========== AUTOMATIC FAILURE DIAGNOSTICS =========="
  remote_log_tail "$ssh_function" "$privilege" "$remote_log" 160
  "$ssh_function" "ps -eo pid,ppid,etime,stat,cmd | grep -E '[r]egression|[k]ubecli|[a]nsible-playbook' || true"
}

cancel_remote_job() {
  local label="$1" ssh_function="$2" privilege="$3" state_prefix="$4"
  local remote_script quoted_script
  remote_script="
set -euo pipefail
pid=missing
test -r '${state_prefix}.pid' && pid=\$(cat '${state_prefix}.pid')
if test \"\$pid\" = missing || ! kill -0 \"\$pid\" 2>/dev/null; then
  echo '[CANCEL] label=${label} state=not-running pid='\"\$pid\"
  exit 0
fi
case \"\$pid\" in
  *[!0-9]*|'') echo '[FAIL] invalid durable pid for ${label}: '\"\$pid\" >&2; exit 1 ;;
esac
echo '[CANCEL] label=${label} state=terminating pid='\"\$pid\"
descendants() {
  local parent=\"\$1\" child
  for child in \$(ps -eo pid=,ppid= | awk -v parent=\"\$parent\" '\$2 == parent {print \$1}'); do
    descendants \"\$child\"
    printf '%s\\n' \"\$child\"
  done
}
targets=\$(descendants \"\$pid\")
for child in \$targets; do kill -TERM \"\$child\" 2>/dev/null || true; done
kill -TERM \"\$pid\" 2>/dev/null || true
for attempt in \$(seq 1 15); do
  alive=
  for candidate in \$targets \"\$pid\"; do
    kill -0 \"\$candidate\" 2>/dev/null && alive=\"\$alive \$candidate\"
  done
  test -z \"\$alive\" && break
  sleep 1
done
for candidate in \$targets \"\$pid\"; do kill -KILL \"\$candidate\" 2>/dev/null || true; done
for candidate in \$targets \"\$pid\"; do
  if kill -0 \"\$candidate\" 2>/dev/null; then
    echo '[FAIL] label=${label} process-survived pid='\"\$candidate\" >&2
    exit 1
  fi
done
echo '[CANCEL] label=${label} state=stopped pid='\"\$pid\"
"
  printf -v quoted_script '%q' "$remote_script"
  if [[ -n "$privilege" ]]; then
    "$ssh_function" "$privilege bash -lc $quoted_script"
  else
    "$ssh_function" "bash -lc $quoted_script"
  fi
}

matrix_counts() {
  local matrix="$ROOT/tests/enterprise-test-matrix.yaml"
  printf 'matrix_pass=%s matrix_pending=%s matrix_fail=%s' \
    "$(grep -c 'status: pass' "$matrix" || true)" \
    "$(grep -c 'status: pending' "$matrix" || true)" \
    "$(grep -c 'status: fail' "$matrix" || true)"
}

monitor_remote_job() {
  local label="$1" ssh_function="$2" privilege="$3" state_prefix="$4" remote_log="$5" success_marker="$6"
  local monitor_started_at monitor_now last_heartbeat_at last_change_at previous_bytes summary bytes state rc failures
  local ssh_failures=0 stream_pid=''
  monitor_started_at=$(date +%s)
  last_heartbeat_at=0
  last_change_at=$monitor_started_at
  previous_bytes=-1

  echo "[MONITOR] label=$label event=attached interval=10s heartbeat=30s silence_diagnostic=60s"
  remote_log_tail "$ssh_function" "$privilege" "$remote_log" 30

  # Stream every remote log line to this terminal.  The polling loop below is
  # independent, so an SSH/tail disconnect is detected and restarted instead
  # of silently disabling supervision.
  if [[ -n "$privilege" ]]; then
    "$ssh_function" "$privilege bash -lc \"exec tail --pid=\\\$(cat '${state_prefix}.pid') -n 0 -F '$remote_log'\"" &
  else
    "$ssh_function" "bash -lc \"exec tail --pid=\\\$(cat '${state_prefix}.pid') -n 0 -F '$remote_log'\"" &
  fi
  stream_pid=$!

  while true; do
    monitor_now=$(date +%s)
    if ! summary=$(remote_job_summary "$ssh_function" "$privilege" "$state_prefix" "$remote_log"); then
      ssh_failures=$((ssh_failures+1))
      echo "[MONITOR] label=$label event=ssh-poll-failed consecutive=$ssh_failures"
      if (( ssh_failures >= 3 )); then
        kill "$stream_pid" 2>/dev/null || true
        wait "$stream_pid" 2>/dev/null || true
        echo "[FAIL] monitor lost SSH connectivity to $label for three consecutive polls"
        return 1
      fi
      sleep 10
      continue
    fi
    ssh_failures=0
    state=$(sed -n 's/^state=\([^ ]*\).*/\1/p' <<<"$summary")
    rc=$(sed -n 's/.* rc=\([^ ]*\).*/\1/p' <<<"$summary")
    bytes=$(sed -n 's/.* bytes=\([^ ]*\).*/\1/p' <<<"$summary")
    failures=$(sed -n 's/.* failure_markers=\([^ ]*\).*/\1/p' <<<"$summary")

    if [[ "$bytes" != "$previous_bytes" ]]; then
      previous_bytes="$bytes"
      last_change_at=$monitor_now
    elif (( monitor_now - last_change_at >= 60 )); then
      echo "[MONITOR] label=$label event=no-new-log-for-$((monitor_now-last_change_at))s action=diagnostic-snapshot"
      echo "[MONITOR] $summary"
      remote_process_tree "$ssh_function" "$privilege" "$state_prefix"
      last_change_at=$monitor_now
    fi

    if (( monitor_now - last_heartbeat_at >= 30 )); then
      echo "[HEARTBEAT] time=$(date '+%F %T %Z') label=$label elapsed=$((monitor_now-monitor_started_at))s $summary $(matrix_counts)"
      last_heartbeat_at=$monitor_now
    fi

    if ! kill -0 "$stream_pid" 2>/dev/null && [[ "$state" == running ]]; then
      wait "$stream_pid" 2>/dev/null || true
      echo "[MONITOR] label=$label event=log-stream-disconnected action=reattach"
      if [[ -n "$privilege" ]]; then
        "$ssh_function" "$privilege bash -lc \"exec tail --pid=\\\$(cat '${state_prefix}.pid') -n 20 -F '$remote_log'\"" &
      else
        "$ssh_function" "bash -lc \"exec tail --pid=\\\$(cat '${state_prefix}.pid') -n 20 -F '$remote_log'\"" &
      fi
      stream_pid=$!
    fi

    case "$state" in
      running|starting)
        ;;
      exited)
        kill "$stream_pid" 2>/dev/null || true
        wait "$stream_pid" 2>/dev/null || true
        remote_log_tail "$ssh_function" "$privilege" "$remote_log" 100
        if [[ "$rc" == 0 && "$failures" == 0 ]] && "$ssh_function" "grep -q '$success_marker' '$remote_log'"; then
          echo "[PASS] supervised job $label rc=0 failure_markers=0 marker=$success_marker"
          return 0
        fi
        remote_diagnostics "$ssh_function" "$privilege" "$remote_log"
        echo "[FAIL] supervised job $label rc=$rc failure_markers=$failures missing_or_failed_marker=$success_marker"
        return 1
        ;;
      lost|*)
        kill "$stream_pid" 2>/dev/null || true
        wait "$stream_pid" 2>/dev/null || true
        remote_diagnostics "$ssh_function" "$privilege" "$remote_log"
        echo "[FAIL] supervised job $label state=$state without durable exit record"
        return 1
        ;;
    esac
    sleep 10
  done
}

if [[ "$MODE" == "--status" ]]; then
  echo "========== 138 full regression =========="
  remote_job_summary ssh138 sudo /tmp/kubeauto-regression-aio /tmp/kubeauto-regression-live.log || true
  remote_log_tail ssh138 sudo /tmp/kubeauto-regression-live.log 30
  echo "========== 130 jumper regression =========="
  remote_job_summary ssh130 '' /tmp/kubeauto-regression-jumper /tmp/kubeauto-jumper-live.log || true
  remote_log_tail ssh130 '' /tmp/kubeauto-jumper-live.log 30
  echo "========== 138 nerdctl gate =========="
  remote_job_summary ssh138 sudo /tmp/kubeauto-nerdctl-gate /tmp/kubeauto-nerdctl-live.log || true
  remote_log_tail ssh138 sudo /tmp/kubeauto-nerdctl-live.log 30
  echo "========== 138 Docker delivery gate =========="
  remote_job_summary ssh138 sudo /tmp/kubeauto-docker-gate /tmp/kubeauto-docker-live.log || true
  remote_log_tail ssh138 sudo /tmp/kubeauto-docker-live.log 30
  echo "========== 138 Kubernetes upgrade gate =========="
  remote_job_summary ssh138 sudo /tmp/kubeauto-upgrade-gate /tmp/kubeauto-upgrade-live.log || true
  remote_log_tail ssh138 sudo /tmp/kubeauto-upgrade-live.log 30
  echo "========== 138 remaining delivery gaps gate =========="
  remote_job_summary ssh138 sudo /tmp/kubeauto-gaps-gate /tmp/kubeauto-gaps-live.log || true
  remote_log_tail ssh138 sudo /tmp/kubeauto-gaps-live.log 30
  echo "========== matrix =========="
  matrix_counts
  echo
  echo "========== process state =========="
  ssh138 "pgrep -af '[r]egression-full|[k]ubecli|[a]nsible-playbook' || true"
  ssh130 "pgrep -af '[r]egression-jumper|[k]ubecli|[a]nsible-playbook' || true"
  exit 0
fi

if [[ "$MODE" == "--follow" ]]; then
  echo "Following 138 enterprise-regression log (Ctrl-C stops viewing only; remote regression continues)."
  ssh138 "sudo tail -n 60 -F /tmp/kubeauto-regression-live.log"
  exit 0
fi

if [[ "$MODE" == "--follow-jumper" ]]; then
  echo "Following 130 jumper-regression log (Ctrl-C stops viewing only; remote regression continues)."
  ssh130 "tail -n 60 -F /tmp/kubeauto-jumper-live.log"
  exit 0
fi

if [[ "$MODE" == "--cancel-jumper" ]]; then
  cancel_remote_job jumper-130 ssh130 '' /tmp/kubeauto-regression-jumper
  exit 0
fi

if [[ "$MODE" == "--cancel-gaps" ]]; then
  cancel_remote_job gaps-138 ssh138 sudo /tmp/kubeauto-gaps-gate
  exit 0
fi

if [[ "$MODE" == "--diagnose-test137" ]]; then
  # Kubernetes official node troubleshooting evidence: node conditions/events,
  # then kubelet and container runtime journals on the affected host.
  ssh138 "sudo KUBECONFIG=/usr/local/kubeauto/clusters/test137/kubectl.kubeconfig kubectl describe node master-137 || true"
  ssh138 "sudo KUBECONFIG=/usr/local/kubeauto/clusters/test137/kubectl.kubeconfig kubectl get pods -n kube-system -o wide; sudo KUBECONFIG=/usr/local/kubeauto/clusters/test137/kubectl.kubeconfig kubectl describe pod -n kube-system -l k8s-app=calico-node; sudo KUBECONFIG=/usr/local/kubeauto/clusters/test137/kubectl.kubeconfig kubectl describe pod -n kube-system -l k8s-app=calico-kube-controllers; sudo KUBECONFIG=/usr/local/kubeauto/clusters/test137/kubectl.kubeconfig kubectl logs -n kube-system ds/calico-node --tail=240 || true; sudo KUBECONFIG=/usr/local/kubeauto/clusters/test137/kubectl.kubeconfig kubectl logs -n kube-system deploy/calico-kube-controllers --tail=160 || true"
  ssh138 "sudo bash -lc '/usr/local/kubeauto/extra-bin/containerd-bin/containerd --version 2>/dev/null || true; stat -c \"mtime=%y ctime=%z size=%s %n\" /usr/local/kubeauto/extra-bin/containerd-bin/containerd /usr/local/kubeauto/extra-bin/etcdctl /usr/local/kubeauto/images/ext_bin_1.15.0.tar 2>/dev/null || true; sha256sum /usr/local/kubeauto/extra-bin/containerd-bin/containerd 2>/dev/null || true; echo ===controller-containerd-default===; /usr/local/kubeauto/extra-bin/containerd-bin/containerd config default 2>/dev/null | grep -A3 -B3 -E \"sandbox_image|pinned_images\" || true; docker image inspect brinnatt/kubeauto-ext-bin:1.15.0 --format \"{{.Id}} {{.RepoDigests}} {{.Created}}\" 2>/dev/null || true'"
  # Capture the runtime's effective/default CRI sandbox configuration too. A
  # pod stuck before init containers have Container IDs is a sandbox/runtime
  # failure, not an init-container failure.
  ssh138 "sudo ssh -o BatchMode=yes -o StrictHostKeyChecking=no root@192.168.47.137 '/usr/local/bin/containerd --version; stat -c \"%y %s %n\" /usr/local/bin/containerd; sha256sum /usr/local/bin/containerd; lsattr /usr/local/bin/containerd 2>/dev/null || true; systemctl cat containerd; sed -n \"1,125p\" /etc/containerd/config.toml; echo ===effective-cri-config===; crictl info 2>/dev/null | grep -A3 -B3 -E \"sandboxImage|sandbox_image\" || true; echo ===containerd-default===; containerd config default 2>/dev/null | grep -A2 -B2 -E \"sandbox_image|pinned_images\" || true; echo ===pause-images===; crictl images | grep -E \"pause|IMAGE\" || true'"
  exit 0
fi

if [[ "$MODE" == "--diagnose-debian128" ]]; then
  ssh -o BatchMode=yes -o StrictHostKeyChecking=no brinnatt@192.168.47.128 \
    'sudo systemctl --no-pager --full status kubelet containerd || true; sudo journalctl -u kubelet -u containerd -n 240 --no-pager || true; sudo crictl ps -a || true'
  exit 0
fi

if [[ "$MODE" == "--diagnose-ded-etcd" ]]; then
  ssh138 "sudo bash -lc 'cd /usr/local/kubeauto && ANSIBLE_HOST_KEY_CHECKING=False ansible -i clusters/test-ded-etcd/hosts all -m ping -vvv'"
  exit 0
fi

if [[ "$MODE" == "--verify-ded-etcd-access" ]]; then
  ssh138 "sudo bash -lc 'cd /usr/local/kubeauto && ANSIBLE_HOST_KEY_CHECKING=False ansible -i clusters/test-ded-etcd/hosts all -m ping'"
  exit 0
fi

if [[ "$MODE" == "--repair-lab-access" ]]; then
  ssh138 "sudo bash -lc 'set -e; for ip in 192.168.47.131 192.168.47.132 192.168.47.133 192.168.47.134 192.168.47.135 192.168.47.136 192.168.47.137; do ssh-keygen -R \"\$ip\" 2>/dev/null || true; done; cd /usr/local/kubeauto; kubecli system -a --user root --password 123456 192.168.47.131-137 </dev/null; for ip in 192.168.47.131 192.168.47.132 192.168.47.133 192.168.47.134 192.168.47.135 192.168.47.136 192.168.47.137; do ssh -o BatchMode=yes -o StrictHostKeyChecking=no root@\"\$ip\" \"command -v python3.9 >/dev/null 2>&1 || dnf install -y python39; python3.9 --version\"; done'"
  exit 0
fi

if [[ "$MODE" == "--diagnose-harbor137" ]]; then
  ssh -o BatchMode=yes -o StrictHostKeyChecking=no root@192.168.47.137 \
    'systemctl --no-pager --full status docker harbor 2>/dev/null || true; ss -ltnp | grep -E ":(443|8443)" || true; docker ps -a || true; find /var -maxdepth 4 -type f -name "*.log" -path "*harbor*" -print -exec tail -n 80 {} \; 2>/dev/null || true'
  exit 0
fi

if [[ "$MODE" == "--diagnose-rocketmq-image" ]]; then
  # Read-only evidence from the 138 image cache, local registry, and both
  # delivery registries. Keep this behind the fixed runner entry so operators
  # do not need to compose ad-hoc SSH commands during a supervised regression.
  ssh138 "sudo bash -lc 'echo ===local-registry-tags===; curl -fsS --max-time 5 http://127.0.0.1:5000/v2/brinnatt/rocketmq-console/tags/list || true; echo; echo ===local-images===; docker image inspect brinnatt/rocketmq-console:2.0.0 --format \"id={{.Id}} architecture={{.Architecture}} repo_digests={{json .RepoDigests}} rootfs_diff_ids={{json .RootFS.Layers}}\" 2>&1 || true; docker image inspect 127.0.0.1:5000/brinnatt/rocketmq-console:2.0.0 --format \"id={{.Id}} architecture={{.Architecture}} repo_digests={{json .RepoDigests}} rootfs_diff_ids={{json .RootFS.Layers}}\" 2>&1 || true; echo ===private-manifest===; timeout --signal=TERM --kill-after=5s 20s docker manifest inspect hub.talkedu.cn/kubeauto/rocketmq-console:2.0.0 >/dev/null && echo PRIVATE_MANIFEST_OK || echo PRIVATE_MANIFEST_MISSING_OR_TIMEOUT; echo ===dockerhub-manifest===; timeout --signal=TERM --kill-after=5s 20s docker manifest inspect brinnatt/rocketmq-console:2.0.0 >/dev/null && echo DOCKERHUB_MANIFEST_OK || echo DOCKERHUB_MANIFEST_MISSING_OR_TIMEOUT; echo ===download-log===; grep -n -C 8 rocketmq-console /tmp/kubeauto-gaps-live.log | tail -n 180 || true'"
  exit 0
fi

if [[ "$MODE" == "--rocketmq-image-integrity" ]]; then
  # OCI Distribution content-addressability gate: rebuild the disposable local
  # registry, upload the RocketMQ bundle, then read and hash every console
  # manifest descriptor before a Kubernetes runtime is allowed to consume it.
  ssh138 "sudo bash -lc 'set -euo pipefail; cd /usr/local/kubeauto; env PYTHONPATH=/usr/local/kubeauto PATH=/usr/local/bin:/usr/bin:/bin kubecli download -E rocketmq </dev/null; python3 tests/helpers/registry_blob_integrity.py http://127.0.0.1:5000 brinnatt/rocketmq-console 2.0.0'"
  exit 0
fi

if [[ "$MODE" == "--diagnose-local-registry-storage" ]]; then
  # Snap Docker can resolve bind-mount source paths inside dockerd's mount
  # namespace.  Compare all three views before changing cleanup semantics.
  ssh138 "sudo bash -lc 'echo ===container-mounts===; docker inspect local_registry --format \"{{json .Mounts}}\" 2>&1 || true; echo ===host-view===; find /data/registry -maxdepth 4 -type d -o -type f 2>/dev/null | head -n 80; du -sh /data/registry 2>/dev/null || true; echo ===container-view===; docker exec local_registry sh -c \"find /var/lib/registry -maxdepth 4 -type d -o -type f | head -n 80; du -sh /var/lib/registry\" 2>&1 || true; pid=\$(pgrep -fo \"/snap/docker/.*/dockerd\" || true); echo dockerd_pid=\$pid; if test -n \"\$pid\"; then echo ===dockerd-mount-namespace-view===; nsenter -t \"\$pid\" -m -- sh -c \"readlink -f /data/registry; find /data/registry -maxdepth 4 -type d -o -type f 2>/dev/null | head -n 80; du -sh /data/registry 2>/dev/null || true\"; fi; cpid=\$(docker inspect local_registry --format \"{{.State.Pid}}\" 2>/dev/null || true); echo container_pid=\$cpid; if test -n \"\$cpid\" && test \"\$cpid\" != 0; then echo ===container-mountinfo===; grep -E \"/var/lib/registry|/data/registry|snap/docker\" /proc/\"\$cpid\"/mountinfo || true; echo ===container-root-view===; du -sh /proc/\"\$cpid\"/root/var/lib/registry 2>/dev/null || true; fi'"
  exit 0
fi

if [[ "$MODE" == "--official-registry-storage-contracts" ]]; then
  # Read the upstream contracts used by the diagnosis.  Keep URLs explicit and
  # bounded so this remains a reproducible, read-only evidence command.
  ssh138 "sudo bash -lc 'set -u; tmp=\$(mktemp -d); trap \"rm -rf \\\"\$tmp\\\"\" EXIT; echo ===oci-distribution-spec===; curl -fsSL --max-time 30 https://raw.githubusercontent.com/opencontainers/distribution-spec/main/spec.md -o \"\$tmp/distribution-spec.md\" && grep -n -C 3 -E \"Docker-Content-Digest|digest.*verified|content.*digest\" \"\$tmp/distribution-spec.md\" | head -n 100; echo ===containerd-source===; for path in core/content/local/store.go core/content/local/writer.go content/local/store.go; do url=https://raw.githubusercontent.com/containerd/containerd/main/\$path; if curl -fsSL --max-time 20 \"\$url\" -o \"\$tmp/containerd.go\"; then grep -n -C 5 \"unexpected commit digest\" \"\$tmp/containerd.go\" && echo source=\$url && break; fi; done; echo ===snap-data-locations===; curl -fsSL --max-time 30 https://snapcraft.io/docs/data-locations -o \"\$tmp/snap-data.html\" && grep -o -E -m 8 \"SNAP_COMMON.{0,240}|/var/snap/[^< ]+/common.{0,160}\" \"\$tmp/snap-data.html\" || true; echo ===installed-snap-contract===; snap info docker | head -n 80; snap run --shell docker.docker -c \"printf \\\"SNAP_COMMON=%s\\\\nSNAP_DATA=%s\\\\n\\\" \\\"\\\$SNAP_COMMON\\\" \\\"\\\$SNAP_DATA\\\"\"'"
  exit 0
fi

if [[ "$MODE" == "--diagnose-nacos-last" ]]; then
  # Read-only post-failure evidence kept behind the fixed runner entry.  This
  # avoids ad-hoc SSH commands (and per-command operator approval) while
  # preserving the failed lab exactly as recorded before the mandatory wipe.
  ssh138 "sudo bash -lc 'echo ===focused-retest-logs===; ls -lt /var/log/kubeauto-delivery-retest-*.log 2>/dev/null | head -n 5 || true; latest=\$(ls -t /var/log/kubeauto-delivery-retest-*.log 2>/dev/null | head -n 1); if test -n \"\$latest\"; then echo log=\$latest; grep -n -C 120 -E \"nacos_ready=|P0 Nacos replicas/external-MySQL|P0 Nacos official mysql-schema.sql|nacos_schema_tables=\" \"\$latest\" || true; fi; echo ===test-ha-config===; grep -E \"^nacos_(install|replicas|mysql_|storage_)|^CLUSTER_DNS_DOMAIN\" /usr/local/kubeauto/clusters/test-ha/config.yml 2>/dev/null || true; echo ===rendered-nacos-statefulset===; sed -n \"55,210p\" /usr/local/kubeauto/clusters/test-ha/yml/nacos-sts.yaml 2>/dev/null || true; echo ===durable-gaps-tail===; tail -n 400 /tmp/kubeauto-gaps-live.log 2>/dev/null || true'"
  exit 0
fi

if [[ "$MODE" == "--diagnose-nacos-images" ]]; then
  # The mirrored delivery images contain the upstream entrypoint sources that
  # define readiness and startup ordering.  Inspect those sources locally so
  # this evidence remains available even when the lab cannot reach GitHub.
  ssh138 "sudo bash -lc 'echo ===mysql-official-image===; docker image inspect mysql:8.0.36 --format \"id={{.Id}} entrypoint={{json .Config.Entrypoint}} cmd={{json .Config.Cmd}}\" 2>&1 || true; docker run --rm --entrypoint sed mysql:8.0.36 -n 1,260p /usr/local/bin/docker-entrypoint.sh 2>&1 || true; echo ===nacos-official-image===; docker image inspect brinnatt/nacos-server:v2.4.3 --format \"id={{.Id}} entrypoint={{json .Config.Entrypoint}} cmd={{json .Config.Cmd}} env={{json .Config.Env}} source={{index .Config.Labels \\\"org.opencontainers.image.source\\\"}}\" 2>/dev/null || true; docker run --rm --entrypoint sh brinnatt/nacos-server:v2.4.3 -c \"echo ---docker-startup.sh---; sed -n 1,260p /home/nacos/bin/docker-startup.sh; echo ---application.properties---; sed -n 1,120p /home/nacos/conf/application.properties\" 2>&1 || true; echo ===nacos-peer-finder-official-image===; docker image inspect brinnatt/nacos-peer-finder-plugin:1.1 --format \"id={{.Id}} entrypoint={{json .Config.Entrypoint}} cmd={{json .Config.Cmd}} env={{json .Config.Env}} source={{index .Config.Labels \\\"org.opencontainers.image.source\\\"}}\" 2>/dev/null || true; docker run --rm --entrypoint sh brinnatt/nacos-peer-finder-plugin:1.1 -c \"for file in /install.sh /plugin.sh /on-start.sh; do echo ---\\\$file---; sed -n 1,260p \\\"\\\$file\\\"; done; ls -l /peer-finder; sha256sum /peer-finder\" 2>&1 || true'"
  exit 0
fi

if [[ "$MODE" == "--nerdctl-only" ]]; then
  echo "========== NERDCTL focused delivery gate =========="
  echo ">>> bash $ROOT/tests/helpers/lab-wipe-nodes.sh"
  bash "$ROOT/tests/helpers/lab-wipe-nodes.sh"
  echo ">>> bash $ROOT/tests/helpers/lab-wipe-nodes.sh --verify"
  bash "$ROOT/tests/helpers/lab-wipe-nodes.sh" --verify
  echo ">>> bash $ROOT/tests/helpers/sync-kubeauto.sh $HOST138"
  bash "$ROOT/tests/helpers/sync-kubeauto.sh" "$HOST138"
  ssh138 "sudo rm -f /tmp/kubeauto-nerdctl-live.log /tmp/kubeauto-nerdctl-gate.pid /tmp/kubeauto-nerdctl-gate.exit; sudo nohup env NERDCTL_SKIP_SYNC=1 NERDCTL_SKIP_LAB_WIPE=1 bash /usr/local/kubeauto/tests/helpers/run-durable-gate.sh /tmp/kubeauto-nerdctl-gate NERDCTL_GATE_EXIT bash /usr/local/kubeauto/tests/helpers/nerdctl-gate.sh >/tmp/kubeauto-nerdctl-live.log 2>&1 </dev/null &"
  nerdctl_rc=0
  monitor_remote_job \
    nerdctl-138 ssh138 sudo /tmp/kubeauto-nerdctl-gate \
    /tmp/kubeauto-nerdctl-live.log NERDCTL_GATE_PASS || nerdctl_rc=$?
  bash "$ROOT/tests/helpers/lab-wipe-nodes.sh"
  bash "$ROOT/tests/helpers/lab-wipe-nodes.sh" --verify
  if [[ "$nerdctl_rc" -ne 0 ]]; then
    echo "[FAIL] nerdctl delivery gate failed (rc=$nerdctl_rc); lab cleanup verified" >&2
    exit 1
  fi
  echo "[PASS] nerdctl-delivery-gate"
  exit 0
fi

if [[ "$MODE" == "--docker-only" ]]; then
  echo "========== Docker focused delivery gate =========="
  cancel_remote_job docker-138 ssh138 sudo /tmp/kubeauto-docker-gate
  echo ">>> bash $ROOT/tests/helpers/lab-wipe-nodes.sh"
  bash "$ROOT/tests/helpers/lab-wipe-nodes.sh"
  echo ">>> bash $ROOT/tests/helpers/lab-wipe-nodes.sh --verify"
  bash "$ROOT/tests/helpers/lab-wipe-nodes.sh" --verify
  echo ">>> bash $ROOT/tests/helpers/sync-kubeauto.sh $HOST138"
  bash "$ROOT/tests/helpers/sync-kubeauto.sh" "$HOST138"
  ssh138 "sudo rm -f /tmp/kubeauto-docker-live.log /tmp/kubeauto-docker-gate.pid /tmp/kubeauto-docker-gate.exit; sudo nohup env DOCKER_GATE_NODE=192.168.47.137 bash /usr/local/kubeauto/tests/helpers/run-durable-gate.sh /tmp/kubeauto-docker-gate DOCKER_GATE_EXIT bash /usr/local/kubeauto/tests/helpers/delivery-docker-gate.sh >/tmp/kubeauto-docker-live.log 2>&1 </dev/null &"
  docker_rc=0
  monitor_remote_job \
    docker-138 ssh138 sudo /tmp/kubeauto-docker-gate \
    /tmp/kubeauto-docker-live.log DOCKER_GATE_PASS || docker_rc=$?
  bash "$ROOT/tests/helpers/lab-wipe-nodes.sh"
  bash "$ROOT/tests/helpers/lab-wipe-nodes.sh" --verify
  if [[ "$docker_rc" -ne 0 ]]; then
    echo "[FAIL] Docker delivery gate failed (rc=$docker_rc); lab cleanup verified" >&2
    exit 1
  fi
  echo "[PASS] docker-delivery-gate"
  exit 0
fi

if [[ "$MODE" == "--upgrade-only" ]]; then
  echo "========== Kubernetes patch-upgrade delivery gate =========="
  cancel_remote_job upgrade-138 ssh138 sudo /tmp/kubeauto-upgrade-gate
  echo ">>> bash $ROOT/tests/helpers/lab-wipe-nodes.sh"
  bash "$ROOT/tests/helpers/lab-wipe-nodes.sh"
  echo ">>> bash $ROOT/tests/helpers/lab-wipe-nodes.sh --verify"
  bash "$ROOT/tests/helpers/lab-wipe-nodes.sh" --verify
  echo ">>> bash $ROOT/tests/helpers/sync-kubeauto.sh $HOST138"
  bash "$ROOT/tests/helpers/sync-kubeauto.sh" "$HOST138"
  ssh138 "sudo rm -f /tmp/kubeauto-upgrade-live.log /tmp/kubeauto-upgrade-gate.pid /tmp/kubeauto-upgrade-gate.exit; sudo nohup env UPGRADE_GATE_NODE=192.168.47.137 bash /usr/local/kubeauto/tests/helpers/run-durable-gate.sh /tmp/kubeauto-upgrade-gate UPGRADE_GATE_EXIT bash /usr/local/kubeauto/tests/helpers/delivery-upgrade-smoke.sh >/tmp/kubeauto-upgrade-live.log 2>&1 </dev/null &"
  upgrade_rc=0
  monitor_remote_job \
    upgrade-138 ssh138 sudo /tmp/kubeauto-upgrade-gate \
    /tmp/kubeauto-upgrade-live.log UPGRADE_GATE_PASS || upgrade_rc=$?
  bash "$ROOT/tests/helpers/lab-wipe-nodes.sh"
  bash "$ROOT/tests/helpers/lab-wipe-nodes.sh" --verify
  if [[ "$upgrade_rc" -ne 0 ]]; then
    echo "[FAIL] Kubernetes upgrade gate failed (rc=$upgrade_rc); lab cleanup verified" >&2
    exit 1
  fi
  echo "[PASS] kubernetes-upgrade-gate"
  exit 0
fi

if [[ "$MODE" == "--gaps-only" ]]; then
  echo "========== Remaining G5/G10 delivery gates =========="
  cancel_remote_job gaps-138 ssh138 sudo /tmp/kubeauto-gaps-gate
  echo ">>> bash $ROOT/tests/helpers/lab-wipe-nodes.sh"
  bash "$ROOT/tests/helpers/lab-wipe-nodes.sh"
  echo ">>> bash $ROOT/tests/helpers/lab-wipe-nodes.sh --verify"
  bash "$ROOT/tests/helpers/lab-wipe-nodes.sh" --verify
  echo ">>> bash $ROOT/tests/helpers/sync-kubeauto.sh $HOST138"
  bash "$ROOT/tests/helpers/sync-kubeauto.sh" "$HOST138"
  ssh138 "sudo rm -f /tmp/kubeauto-gaps-live.log /tmp/kubeauto-gaps-gate.pid /tmp/kubeauto-gaps-gate.exit; sudo nohup bash /usr/local/kubeauto/tests/helpers/run-durable-gate.sh /tmp/kubeauto-gaps-gate DELIVERY_GAPS_EXIT bash /usr/local/kubeauto/tests/helpers/delivery-gaps-fullchain.sh >/tmp/kubeauto-gaps-live.log 2>&1 </dev/null &"
  gaps_rc=0
  monitor_remote_job \
    gaps-138 ssh138 sudo /tmp/kubeauto-gaps-gate \
    /tmp/kubeauto-gaps-live.log DELIVERY_GAPS_FULLCHAIN_PASS || gaps_rc=$?
  bash "$ROOT/tests/helpers/lab-wipe-nodes.sh"
  bash "$ROOT/tests/helpers/lab-wipe-nodes.sh" --verify
  if [[ "$gaps_rc" -ne 0 ]]; then
    echo "[FAIL] Remaining delivery gaps gate failed (rc=$gaps_rc); lab cleanup verified" >&2
    exit 1
  fi
  echo "[PASS] remaining-delivery-gaps-gate"
  exit 0
fi

if [[ "$MODE" != "run" && "$MODE" != "--aio-only" && "$MODE" != "--jumper-only" ]]; then
  echo "Usage: $0 [--status|--follow|--follow-jumper|--cancel-jumper|--cancel-gaps|--aio-only|--jumper-only|--nerdctl-only|--docker-only|--upgrade-only|--gaps-only|--diagnose-test137|--diagnose-debian128|--diagnose-ded-etcd|--verify-ded-etcd-access|--diagnose-harbor137|--diagnose-rocketmq-image|--diagnose-nacos-last|--diagnose-nacos-images|--repair-lab-access]" >&2
  exit 2
fi

# Mirror every local phase to both the terminal and the auditable log.  The
# previous log-only preflight made a running cleanup/sync look like a stalled
# regression until the remote tail was attached.
exec > >(tee -a "$LOG" >&3) 2>&1

pass() { echo "[PASS] $*"; }
fail() { echo "[FAIL] $*"; exit 1; }
run()  { echo ">>> $*"; "$@"; }

echo "========== PHASE 0: local unit tests (G0) =========="
run bash "$ROOT/tests/run_unit_tests.sh"
pass G0-local-unit

echo "========== PHASE 0b: clean stale lab state =========="
# A previous local supervisor may have been interrupted while its durable
# remote job kept running. Stop that exact recorded process tree before wiping
# nodes; cleanup racing an active Ansible play makes both runs invalid.
if [[ "$MODE" != "--aio-only" ]]; then
  run cancel_remote_job jumper-130 ssh130 '' /tmp/kubeauto-regression-jumper
fi
run bash "$ROOT/tests/helpers/lab-wipe-nodes.sh"
run bash "$ROOT/tests/helpers/lab-wipe-nodes.sh" --verify
pass G0-lab-clean

echo "========== PHASE 1: restore jumper control prerequisites + sync source =========="
# Rocky 8's platform Python is 3.6.  ansible-core 2.16 officially supports
# Python 3.10-3.12 on the control node, so restore Python 3.12 if a previous
# cleanup removed it before syncing source requirements.
ssh130 'if ! command -v python3.12 >/dev/null 2>&1; then dnf install -y python3.12 python3.12-pip; fi'
run bash "$ROOT/tests/helpers/sync-kubeauto.sh" "$HOST138"
run bash "$ROOT/tests/helpers/sync-kubeauto.sh" "$HOST130"
pass G0-deploy-sync

echo "========== PHASE 2: Ubuntu aio 138 full cluster regression (G0-G6, Tier2/3) =========="
if [[ "$MODE" != "--jumper-only" ]]; then
  scp138 "$ROOT/tests/helpers/regression-full.sh" "$HOST138:/tmp/regression-full.sh"
  scp138 "$ROOT/tests/helpers/regression-aio-prep-full.sh" "$HOST138:/tmp/regression-aio-prep-full.sh"
  # Remove followers left by the pre-supervisor implementation. The current
  # stream uses tail --pid=<job>, so it exits with the job and is not matched.
  ssh138 "sudo pkill -f '^tail -n (0|60) -[fF] /tmp/kubeauto-regression-live.log$' || true"
  ssh138 "sudo rm -f /tmp/kubeauto-regression-live.log /tmp/kubeauto-regression-aio.pid /tmp/kubeauto-regression-aio.exit; sudo nohup bash /tmp/regression-aio-prep-full.sh >/tmp/kubeauto-regression-live.log 2>&1 </dev/null &"
  monitor_remote_job \
    aio-138 ssh138 sudo /tmp/kubeauto-regression-aio \
    /tmp/kubeauto-regression-live.log REGRESSION_FULL_COMPLETE
  pass G2-G6-aio-138
fi

echo "========== PHASE 3: Rocky jumper 130 regression (G7) =========="
if [[ "$MODE" != "--aio-only" ]]; then
  # 130 and 138 both allocate 131-136.  They must never run concurrently.
  # When running the complete suite, prove the AIO lab has been cleaned before
  # the jumper is allowed to build its independent cluster.
  if [[ "$MODE" == run ]]; then
    run bash "$ROOT/tests/helpers/lab-wipe-nodes.sh"
    run bash "$ROOT/tests/helpers/lab-wipe-nodes.sh" --verify
    run bash "$ROOT/tests/helpers/sync-kubeauto.sh" "$HOST130"
  fi
  scp -o BatchMode=yes -o StrictHostKeyChecking=no \
    "$ROOT/tests/helpers/regression-jumper.sh" "$HOST130:/tmp/regression-jumper.sh"
  scp -o BatchMode=yes -o StrictHostKeyChecking=no \
    "$ROOT/tests/helpers/regression-jumper-prep.sh" "$HOST130:/tmp/regression-jumper-prep.sh"
  ssh130 "rm -f /tmp/kubeauto-jumper-live.log /tmp/kubeauto-regression-jumper.pid /tmp/kubeauto-regression-jumper.exit; nohup bash /tmp/regression-jumper-prep.sh >/tmp/kubeauto-jumper-live.log 2>&1 </dev/null &"
  jumper_rc=0
  monitor_remote_job \
    jumper-130 ssh130 '' /tmp/kubeauto-regression-jumper \
    /tmp/kubeauto-jumper-live.log G7_JUMPER_PASS || jumper_rc=$?

  # Diagnostics are emitted by monitor_remote_job before it returns.  Always
  # clean afterward so a failed G7 attempt cannot contaminate the next run.
  run bash "$ROOT/tests/helpers/lab-wipe-nodes.sh"
  run bash "$ROOT/tests/helpers/lab-wipe-nodes.sh" --verify
  [[ "$jumper_rc" -eq 0 ]] || fail "G7 jumper regression failed (rc=$jumper_rc); lab cleanup verified"
  pass G7-jumper-130
fi

echo "========== REGRESSION COMPLETE =========="
echo "Log: $LOG"
matrix_counts
echo
