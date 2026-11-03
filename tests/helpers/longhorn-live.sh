#!/usr/bin/env bash
# All remote Longhorn work is runner-owned, leased and durably supervised.
set -Eeuo pipefail
MODE="${1:?mode required}"
BASE=/usr/local/kubeauto
NODES=(192.168.122.135 192.168.122.40 192.168.122.72 192.168.122.212 192.168.122.165 192.168.122.238)
[[ "${LONGHORN_PARENT_LEASE_HELD:-no}" == yes ]] || {
  echo 'LONGHORN_PREFLIGHT_FAIL class=test-gate reason=missing-parent-lease' >&2; exit 1;
}
for entry in '9 kubeauto-ceph-priority.lock' '7 kubeauto-ceph-compute.lock' '8 kubeauto-redis-core-pool.lock'; do
  read -r fd filename <<< "$entry"
  [[ "/proc/self/fd/$fd" -ef "/var/lock/$filename" ]] || exit 1
  flock -n "$fd" || exit 1
done
if [[ -n "${LONGHORN_V2_CONTINUATION_REQUEST:-}" ]]; then
  # Recovery reads its immutable continuation reference from the preparation
  # journal; it must not try to create the existing runtime material again.
  [[ "$MODE" == v2-upgrade-lifecycle && "$LONGHORN_V2_CONTINUATION_REQUEST" =~ ^logs/longhorn-cases/v2-continuation\.[A-Za-z0-9_]+/request\.json$ ]] || exit 2
  "$BASE/.venv/bin/python" "$BASE/tests/helpers/longhorn-v2-runtime-continuation.py" \
    create --request "$LONGHORN_V2_CONTINUATION_REQUEST"
  export LONGHORN_V2_RUNTIME_CONTINUATION="$BASE/${LONGHORN_V2_CONTINUATION_REQUEST%/request.json}/runtime.json"
fi
case "$MODE" in
  full)
    exec bash "$BASE/tests/helpers/longhorn-full.sh" full
    ;;
  cleanup|cleanup-qualification)
    exec bash "$BASE/tests/helpers/longhorn-cleanup.sh" "$MODE"
    ;;
  v2-incomplete-clone-qualification|v2-incomplete-clone-cleanup)
    exec bash "$BASE/tests/helpers/longhorn-v2-incomplete-clone.sh" "$MODE"
    ;;
  v2-disk-lifecycle|v2-disk-lifecycle-cleanup)
    exec bash "$BASE/tests/helpers/longhorn-v2-disk-lifecycle.sh" "$MODE"
    ;;
  v2-upgrade-lifecycle|v2-upgrade-cleanup)
    exec bash "$BASE/tests/helpers/longhorn-v2-upgrade-lifecycle.sh" "$MODE"
    ;;
  v2-middleware|v2-middleware-cleanup)
    exec bash "$BASE/tests/helpers/longhorn-v2-middleware.sh" "$MODE"
    ;;
  middleware|middleware-business|middleware-cleanup|middleware-reentry-cleanup)
    exec bash "$BASE/tests/helpers/longhorn-v1-middleware.sh" "$MODE"
    ;;
  v2-runtime-resources|v2-runtime-resources-cleanup)
    exec bash "$BASE/tests/helpers/longhorn-v2-runtime-resources.sh" "$MODE"
    ;;
  last-replica|last-replica-cleanup)
    exec bash "$BASE/tests/helpers/longhorn-last-replica.sh" "$MODE"
    ;;
  disk-failure|disk-failure-cleanup|network-failure|network-failure-cleanup)
    exec bash "$BASE/tests/helpers/longhorn-v1-storage-faults.sh" "$MODE"
    ;;
  system-restore|system-restore-cleanup|system-restore-prepared-recovery)
    exec bash "$BASE/tests/helpers/longhorn-system-recovery.sh" "$MODE"
    ;;
  disaster-recovery|disaster-recovery-cleanup)
    exec bash "$BASE/tests/helpers/longhorn-v1-disaster.sh" "$MODE"
    ;;
  node-failure|node-failure-cleanup)
    exec bash "$BASE/tests/helpers/longhorn-node-failure.sh" "$MODE"
    ;;
  observability|observability-business|observability-support|observability-image-cache|observability-cleanup)
    exec bash "$BASE/tests/helpers/longhorn-v1-observability.sh" "$MODE"
    ;;
  v1-upgrade-lifecycle|v1-upgrade-cleanup)
    exec bash "$BASE/tests/helpers/longhorn-v1-upgrade-lifecycle.sh" "$MODE"
    ;;
  v2-maintenance-drain-qualification|v2-maintenance-drain-cleanup)
    exec bash "$BASE/tests/helpers/longhorn-v2-maintenance-drain.sh" "$MODE"
    ;;
  v2-coexist-qualification|v2-coexist-cleanup)
    exec bash "$BASE/tests/helpers/longhorn-v2-coexist.sh" "$MODE"
    ;;
  v2-observability|v2-observability-monitoring|v2-observability-business)
    exec bash "$BASE/tests/helpers/longhorn-v2-observability.sh" "$MODE"
    ;;
  v2-cleanup|v2-cleanup-target|v2-cleanup-recovery|v2-cleanup-target-recovery)
    exec bash "$BASE/tests/helpers/longhorn-v2-cleanup.sh" "$MODE"
    ;;
  stability|stability-cleanup)
    exec bash "$BASE/tests/helpers/longhorn-v1-stability.sh" "$MODE"
    ;;
  node-lifecycle)
    exec bash "$BASE/tests/helpers/longhorn-node-local.sh" "$MODE"
    ;;
  node-lifecycle-cleanup)
    if [[ -e "$BASE/clusters/longhorn-node-gate" || -L "$BASE/clusters/longhorn-node-gate" ]]; then
      exec bash "$BASE/tests/helpers/longhorn-node-local.sh" "$MODE"
    fi
    exec bash "$BASE/tests/helpers/longhorn-node-lifecycle.sh" "$MODE"
    ;;
  platform|platform-propagation-qualification)
    exec bash "$BASE/tests/helpers/longhorn-platform.sh" "$MODE"
    ;;
  maintenance|maintenance-cleanup)
    exec bash "$BASE/tests/helpers/longhorn-maintenance.sh" "$MODE"
    ;;
  capacity|capacity-cleanup)
    exec bash "$BASE/tests/helpers/longhorn-capacity.sh" "$MODE"
    ;;
  integrity|integrity-cleanup)
    exec bash "$BASE/tests/helpers/longhorn-integrity.sh" "$MODE"
    ;;
  disk-lifecycle|disk-lifecycle-cleanup)
    exec bash "$BASE/tests/helpers/longhorn-disk-lifecycle.sh" "$MODE"
    ;;
  ownership)
    exec bash "$BASE/tests/helpers/longhorn-ownership-aggregate.sh" "$MODE"
    ;;
  v1-im-revision-3-adopt-qualification|v2-im-revision-3-adopt-qualification|v2-dr-target-im-revision-3-adopt-qualification)
    exec bash "$BASE/tests/helpers/longhorn-im-revision-3-adopt.sh" "$MODE"
    ;;
  v1-im-revision-2-adopt-qualification|v2-im-revision-2-adopt-qualification|v2-dr-target-im-revision-2-adopt-qualification)
    exec bash "$BASE/tests/helpers/longhorn-v2-im-adopt.sh" "$MODE"
    ;;
  v1-im-adopt-qualification|v2-im-adopt-qualification|v2-dr-target-im-adopt-qualification)
    exec bash "$BASE/tests/helpers/longhorn-v2-im-adopt.sh" "$MODE"
    ;;
  v1-backup-v2-data-protection|v1-operations-v2-disk-tail|v1-operations-v2-worker-tail|v1-ownership-tail-v2-failure-tail|v1-ownership-operations-v2-failure-platform|v1-ownership-v2-failure-remaining|v1-operations-v2-platform-install|v1-ownership-v2-linked-acceptance|v1-post-revision-backups-v2-failure-acceptance|v1-operations-remaining-v2-monitoring|v1-system-restore-v2-runtime-resources|v1-recovery-tail-v2-install-tail|v1-dr-tail-v2-install-tail|v1-upgrade-tail-v2-install-tail|v1-monitoring-v2-resource-tail|v1-upgrade-tail-v2-maintenance-tail|v1-stability-v2-observability|v2-source-target-im-revision-3-adopt-qualification|v2-source-target-im-revision-2-adopt-qualification|v1-backups-v2-linked-failure-acceptance|v1-backups-v2-linked-acceptance|v1-platform-propagation-v2-im-adopt-qualification|v1-remaining-v2-linked-acceptance|v1-backups-remaining|v1-platform-v2-environment-recovery|v2-source-target-environment-recovery|v1-backups|v2-source-target-reboot-recovery|v2-source-target-im-adopt-qualification|v1-v2-control-qualification|v1-backups-v2-linked-qualification|v1-v2-im-adopt-qualification)
    exec bash "$BASE/tests/helpers/longhorn-parallel.sh" "$MODE"
    ;;
  v2-broken-restore-qualification|v2-broken-restore-cleanup)
    exec bash "$BASE/tests/helpers/longhorn-v2-broken-restore.sh" "$MODE"
    ;;
  v2-failure-remaining|v2-failure|v2-failure-cleanup|v2-failure-replica-im|v2-failure-manager|v2-failure-attached-spdk|v2-failure-remote-spdk|v2-failure-replica-network|v2-failure-worker-reboot|v2-failure-single-disk|v2-failure-rebuild-qos|v2-failure-all-replicas)
    exec bash "$BASE/tests/helpers/longhorn-v2-failure.sh" "$MODE"
    ;;
  v2-linked-acceptance|v2-linked-failure-acceptance)
    exec bash "$BASE/tests/helpers/longhorn-v2-linked-acceptance.sh" "$MODE"
    ;;
  v2-linked-lifecycle-qualification|v2-linked-lifecycle-cleanup)
    exec bash "$BASE/tests/helpers/longhorn-v2-linked-lifecycle.sh" "$MODE"
    ;;
  v2-linked-clone-qualification|v2-linked-clone-cleanup)
    exec bash "$BASE/tests/helpers/longhorn-v2-linked-clones.sh" "$MODE"
    ;;
  v2-interrupt-manager-qualification)
    exec bash "$BASE/tests/helpers/longhorn-v2-interrupt-manager.sh" "$MODE"
    ;;
  v2-data-protection)
    exec bash "$BASE/tests/helpers/longhorn-v2-data-protection.sh" "$MODE"
    ;;
  v2-dr-activation-qualification|v2-dr-failback-qualification)
    exec bash "$BASE/tests/helpers/longhorn-v2-dr-failover.sh" "$MODE"
    ;;
  v2-dr-standby-qualification)
    exec bash "$BASE/tests/helpers/longhorn-v2-disaster.sh" "$MODE"
    ;;
  v2-dr-store-qualification)
    exec bash "$BASE/tests/helpers/longhorn-v2-dr-store.sh" "$MODE"
    ;;
  v2-dr-target-install-qualification|v2-dr-target-install-recovery)
    exec bash "$BASE/tests/helpers/longhorn-v2-dr-target-install.sh" "$MODE"
    ;;
  v2-dr-target-registry-qualification|v2-dr-target-core)
    exec bash "$BASE/tests/helpers/longhorn-v2-dr-target.sh" "$MODE"
    ;;
  v2-dr-target-core-recovery)
    exec bash "$BASE/tests/helpers/longhorn-v2-dr-target-core-recovery.sh" "$MODE"
    ;;
  v2-detached-backup-qualification|v2-detached-backup-recovery)
    exec bash "$BASE/tests/helpers/longhorn-v2-detached-backup.sh" "$MODE"
    ;;
  v2-recurring-selector-qualification|v2-recurring-snapshot-qualification|v2-recurring-backup-qualification|v2-recurring-rotation-qualification)
    exec bash "$BASE/tests/helpers/longhorn-v2-recurring.sh" "$MODE"
    ;;
  v2-systemrestore-qualification|v2-systemrestore-recovery)
    exec bash "$BASE/tests/helpers/longhorn-v2-systemrestore.sh" "$MODE"
    ;;
  v2-systembackup-qualification|v2-systembackup-recovery|v2-systembackup-cleanup)
    exec bash "$BASE/tests/helpers/longhorn-v2-systembackup.sh" "$MODE"
    ;;
  v2-backup-refusal-qualification|v2-backup-store-qualification|v2-backup-qualification)
    exec bash "$BASE/tests/helpers/longhorn-v2-backups.sh" "$MODE"
    ;;
  v2-manager-loss-cleanup|v2-manager-loss-qualification|v2-interrupt-cleanup|v2-interrupt-qualification|v2-replica-im-loss-cleanup|v2-replica-im-loss-qualification|v2-full-copy-lifecycle-qualification|v2-full-copy-clone-qualification|v2-integrity-qualification|v2-snapshot-qualification|v2-volumes|v2-volume-modes-qualification|v2-expansion-qualification|v2-reclaim-qualification|v2-encryption-qualification|v2-trim-qualification)
    exec bash "$BASE/tests/helpers/longhorn-v2-volumes.sh" "$MODE"
    ;;
  v2-aio-install-qualification|v2-host-qualification|v2-host-recovery)
    exec bash "$BASE/tests/helpers/longhorn-v2-install.sh" "$MODE"
    ;;
  v2-post-reboot-environment-recovery|v2-dr-target-post-reboot-environment-recovery)
    exec bash "$BASE/tests/helpers/longhorn-v2-post-reboot-environment.sh" "$MODE"
    ;;
  v2-resources-reboot-recovery|v2-dr-target-resources-reboot-recovery)
    exec bash "$BASE/tests/helpers/longhorn-v2-resources-reboot.sh" "$MODE"
    ;;
  v2-resources-qualification|v2-resources-recovery)
    exec bash "$BASE/tests/helpers/longhorn-v2-resources.sh" "$MODE"
    ;;
  v2-core)
    exec bash "$BASE/tests/helpers/longhorn-v2-core.sh" "$MODE"
    ;;
  v2-registry-qualification)
    exec bash "$BASE/tests/helpers/longhorn-v2-live.sh" "$MODE"
    ;;
  core-host-preflight|core-host-recover)
    cd "$BASE"
    PY="$BASE/.venv/bin/python"
    bash "$BASE/tests/helpers/lab-wipe-nodes.sh" --verify --rocky-only "${NODES[@]}"
    "$PY" "$BASE/tests/helpers/longhorn-lab.py" "$MODE"
    "$PY" "$BASE/tests/helpers/longhorn-lab.py" core-host-preflight
    bash "$BASE/tests/helpers/lab-wipe-nodes.sh" --verify --rocky-only "${NODES[@]}"
    echo "LONGHORN_${MODE^^}_SCOPED_CLEAN_PASS" | tr '-' '_' | tr '-' '_'
    echo "LONGHORN_${MODE^^}_PASS" | tr '-' '_' | tr '-' '_'
    ;;
  pool-probe)
    # This branch never formats disks, installs packages or creates a cluster.
    # Reuse the existing disk qualifier with an independent temporary output.
    work="$(mktemp -d /var/tmp/kubeauto-longhorn-probe.XXXXXX)"
    trap 'rm -rf -- "$work"' EXIT
    bash "$BASE/tests/helpers/lab-wipe-nodes.sh" --verify --rocky-only "${NODES[@]}"
    CEPH_STATE_DIR="$work" bash "$BASE/tests/helpers/ceph-host-probe.sh"
    cat "$work/disk-allowlist"
    bash "$BASE/tests/helpers/lab-wipe-nodes.sh" --verify --rocky-only "${NODES[@]}"
    echo LONGHORN_POOL_PROBE_SCOPED_CLEAN_PASS
    echo LONGHORN_POOL_PROBE_PASS
    ;;
  registry-qualification)
    # No cluster setup, disk preparation, packages or Registry mutations. The
    # existing control-host Registry is a read-only shared prerequisite.
    cd "$BASE"
    export PYTHONPATH="$BASE" PATH="$BASE/kube-bin:$BASE/extra-bin:$PATH"
    PY="$BASE/.venv/bin/python"
    DIR="$BASE/clusters/longhorn-gate"
    [[ ! -e "$DIR" && ! -L "$DIR" ]] || {
      echo 'LONGHORN_PREFLIGHT_FAIL class=environment reason=existing-lab-directory' >&2; exit 1;
    }
    curl --fail --silent --show-error --noproxy '*' --max-time 5 http://127.0.0.1:5000/v2/ >/dev/null
    bash "$BASE/tests/helpers/lab-wipe-nodes.sh" --verify --rocky-only "${NODES[@]}"
    phase=configure
    owned=no
    qualification_exit() {
      local rc=$? cleanup_rc=0
      trap - EXIT INT TERM
      if [[ "$rc" != 0 ]]; then
        echo "LONGHORN_FIRST_FAILURE phase=$phase rc=$rc" >&2
      fi
      if [[ "$owned" == yes ]]; then
        "$PY" "$BASE/tests/helpers/longhorn-lab.py" relay-clean || cleanup_rc=$?
        if [[ "$cleanup_rc" == 0 ]]; then
          "$PY" "$BASE/tests/helpers/longhorn-lab.py" qualification-clean || cleanup_rc=$?
        fi
      fi
      bash "$BASE/tests/helpers/lab-wipe-nodes.sh" --verify --rocky-only "${NODES[@]}" || cleanup_rc=$?
      if [[ "$cleanup_rc" != 0 ]]; then
        echo "LONGHORN_CLEANUP_FAIL phase=$phase rc=$cleanup_rc" >&2
        [[ "$rc" != 0 ]] || rc=$cleanup_rc
      elif [[ "$rc" == 0 ]]; then
        echo LONGHORN_REGISTRY_QUALIFICATION_SCOPED_CLEAN_PASS
        echo LONGHORN_REGISTRY_QUALIFICATION_PASS
      fi
      exit "$rc"
    }
    trap qualification_exit EXIT
    trap 'exit 130' INT
    trap 'exit 143' TERM
    "$PY" "$BASE/kubecli.py" new longhorn-gate </dev/null
    "$PY" "$BASE/tests/helpers/longhorn-lab.py" configure
    owned=yes
    phase=registry-qualification
    "$PY" "$BASE/tests/helpers/longhorn-lab.py" relay-qualification
    # The setup helper exits here, exactly as it does before core setup. Prove
    # that its forwarded connections survive separate Python invocations.
    phase=registry-lifetime
    "$PY" "$BASE/tests/helpers/longhorn-lab.py" relay-ensure
    for sample in {1..8}; do
      sleep 15
      "$PY" "$BASE/tests/helpers/longhorn-lab.py" relay-ensure
      echo "LONGHORN_REGISTRY_LIFETIME_SAMPLE sample=$sample/8"
    done
    echo LONGHORN_REGISTRY_LIFETIME_PASS
    ;;
  registry-recover)
    cd "$BASE"
    PY="$BASE/.venv/bin/python"
    "$PY" "$BASE/tests/helpers/longhorn-lab.py" relay-recover
    bash "$BASE/tests/helpers/lab-wipe-nodes.sh" --rocky-only "${NODES[@]}"
    bash "$BASE/tests/helpers/lab-wipe-nodes.sh" --verify --rocky-only "${NODES[@]}"
    "$PY" "$BASE/tests/helpers/longhorn-lab.py" archive-core
    echo LONGHORN_REGISTRY_RECOVERY_SCOPED_CLEAN_PASS
    echo LONGHORN_REGISTRY_RECOVERY_PASS
    ;;
  disk-qualification)
    cd "$BASE"
    PY="$BASE/.venv/bin/python"
    "$PY" "$BASE/tests/helpers/longhorn-lab.py" verify-core
    "$PY" "$BASE/tests/helpers/longhorn-lab.py" recover-owned-disk-residue
    work="$(mktemp -d /var/tmp/kubeauto-longhorn-disks.XXXXXX)"
    phase=qualify
    disk_exit() {
      local rc=$? cleanup_rc=0
      trap - EXIT INT TERM
      if [[ "$rc" != 0 ]]; then echo "LONGHORN_FIRST_FAILURE phase=$phase rc=$rc diagnostics=$work" >&2; fi
      "$PY" "$BASE/tests/helpers/longhorn-lab.py" cleanup-disks || cleanup_rc=$?
      if [[ "$cleanup_rc" == 0 ]]; then
        CEPH_STATE_DIR="$work" bash "$BASE/tests/helpers/ceph-host-probe.sh" || cleanup_rc=$?
      fi
      "$PY" "$BASE/tests/helpers/longhorn-lab.py" verify-core || cleanup_rc=$?
      if [[ "$cleanup_rc" != 0 ]]; then
        echo "LONGHORN_CLEANUP_FAIL phase=$phase rc=$cleanup_rc" >&2
        [[ "$rc" != 0 ]] || rc=$cleanup_rc
      elif [[ "$rc" == 0 ]]; then
        "$PY" "$BASE/tests/helpers/longhorn-lab.py" record-disk-qualification
        rm -rf -- "$work"
        echo LONGHORN_DISK_QUALIFICATION_SCOPED_CLEAN_PASS
        echo LONGHORN_DISK_QUALIFICATION_PASS
      fi
      exit "$rc"
    }
    trap disk_exit EXIT
    trap 'exit 130' INT
    trap 'exit 143' TERM
    CEPH_STATE_DIR="$work" bash "$BASE/tests/helpers/ceph-host-probe.sh"
    phase=prepare-disks
    "$PY" "$BASE/tests/helpers/longhorn-lab.py" prepare-disks --allowlist "$work/disk-allowlist"
    ;;
  business-clean|idempotence|rwo|rwx|performance|replica-scale|replica-failure|engine-failure|encryption|encryption-block|encryption-backup|network-policy|security|clone|expansion|snapshot|reclaim|trim|backup|backup-refusal|core-smoke|cache-qualification|preflight-qualification|backup-store-qualification|backup-accounts-qualification|recurring-declarations-qualification|recurring-snapshot-qualification|recurring-selector-qualification|recurring-backup-qualification|recurring-rotation-qualification|recurring|uninstall-refusal-qualification|uninstall-backup-qualification|uninstall|recover|ownership-csi-probe|ownership-csi-refusal-qualification|ownership-preinstall-refusal-qualification|ownership-request-refusal-qualification|ownership-controller-refusal-qualification|ownership-class-refusal-qualification|ownership-interruption-qualification)
    cd "$BASE"
    PY="$BASE/.venv/bin/python"
    DIR="$BASE/clusters/longhorn-gate"
    export PYTHONPATH="$BASE" PATH="$BASE/kube-bin:$BASE/extra-bin:$PATH"
    export KUBECONFIG="$DIR/kubectl.kubeconfig"
    health_mode=health
    [[ "$MODE" != recover && "$MODE" != core-smoke && "$MODE" != cache-qualification && "$MODE" != preflight-qualification && "$MODE" != ownership-csi-probe && "$MODE" != ownership-csi-refusal-qualification && "$MODE" != ownership-preinstall-refusal-qualification ]] || health_mode=verify-core
    if [[ "$MODE" == business-clean && ( -e "$DIR/longhorn-ownership-csi.json" || -L "$DIR/longhorn-ownership-csi.json" || -e "$DIR/longhorn-ownership-preinstall.json" || -L "$DIR/longhorn-ownership-preinstall.json" ) ]]; then
      # Interrupted first-install refusal can leave only a healthy core and
      # an exact canary journal, with no storage installation to check yet.
      health_mode=verify-core
    fi
    preflight_mode="$health_mode"
    if [[ "$MODE" == business-clean && ( -e "$DIR/longhorn-security-tls.json" || -L "$DIR/longhorn-security-tls.json" ) ]]; then
      # TLS recovery verifies its escrow/parents before mutations. Requiring
      # storage health first could prevent restoration of interrupted trust.
      preflight_mode=verify-core
    fi
    "$PY" "$BASE/tests/helpers/longhorn-lab.py" "$preflight_mode"
    [[ "$MODE" == recover || "$MODE" == business-clean || ! -e "$DIR/longhorn-install-business.json" ]] || exit 1
    work="$(mktemp -d /var/tmp/kubeauto-longhorn-operation.XXXXXX)"
    chmod 0700 "$work"
    control_config=/root/.kube/config
    parallel_context=no
    if [[ "${LONGHORN_PARALLEL_CONTEXT:-}" == explicit-kubeconfig ]]; then
      case "$MODE" in idempotence|security|backup|backup-refusal|recurring|encryption|uninstall) ;; *) exit 2 ;; esac
      parallel_context=yes
      "$PY" "$BASE/tests/helpers/longhorn-parallel.py" context-fingerprint > "$work/control-context.json"
      chmod 0600 "$work/control-context.json"
    else
      [[ -z "${LONGHORN_PARALLEL_CONTEXT:-}" && ! -L "$control_config" ]] || exit 1
      [[ ! -f "$control_config" ]] || cp -a "$control_config" "$work/control-kubeconfig"
    fi
    phase=transport
    relay_pid=
    idempotence_exit() {
      local rc=$? cleanup_rc=0
      trap - EXIT INT TERM
      if [[ "$rc" != 0 ]]; then
        echo "LONGHORN_FIRST_FAILURE phase=$phase rc=$rc diagnostics=$work" >&2
        printf 'rc=%s phase=%s\n' "$rc" "$phase" > "$work/first-failure.status"
        kubectl --request-timeout=5s get pods -A -o wide > "$work/pods.txt" 2>&1 || true
        kubectl --request-timeout=5s get events -A --sort-by=.lastTimestamp > "$work/events.txt" 2>&1 || true
        "$PY" "$BASE/tests/helpers/longhorn-lab.py" business-clean --relay-pid "${relay_pid:-0}" || cleanup_rc=$?
      fi
      if [[ "$MODE" == recurring-declarations-qualification ]]; then
        "$PY" "$BASE/tests/helpers/longhorn-lab.py" recurring-declarations-clean --relay-pid "$relay_pid" || cleanup_rc=$?
      fi
      if [[ "$MODE" == uninstall || "$MODE" == uninstall-backup-qualification || "$MODE" == backup-store-qualification || "$MODE" == backup-accounts-qualification || "$MODE" == recurring-backup-qualification || "$MODE" == recurring-rotation-qualification || "$MODE" == recurring || "$MODE" == backup || "$MODE" == backup-refusal || "$MODE" == encryption || "$MODE" == encryption-backup ]]; then
        "$PY" "$BASE/tests/helpers/longhorn-lab.py" backup-store-clean || cleanup_rc=$?
      fi
      if [[ -n "$relay_pid" ]]; then
        if [[ "$rc" == 0 ]] && ! kill -0 "$relay_pid" 2>/dev/null; then rc=1; fi
        kill -TERM "$relay_pid" 2>/dev/null || true
        wait "$relay_pid" || { [[ "$rc" != 0 ]] || rc=1; }
      fi
      "$PY" "$BASE/tests/helpers/longhorn-lab.py" relay-clean || cleanup_rc=$?
      if [[ "$parallel_context" == yes ]]; then
        "$PY" "$BASE/tests/helpers/longhorn-parallel.py" context-verify --record "$work/control-context.json" || cleanup_rc=$?
      elif [[ -f "$work/control-kubeconfig" ]]; then
        cp -a "$work/control-kubeconfig" "$control_config" || cleanup_rc=$?
      else
        rm -f -- "$control_config" || cleanup_rc=$?
      fi
      "$PY" "$BASE/tests/helpers/longhorn-lab.py" "$health_mode" || cleanup_rc=$?
      if [[ "$cleanup_rc" != 0 ]]; then
        echo "LONGHORN_CLEANUP_FAIL phase=$phase rc=$cleanup_rc journal=retained" >&2
        [[ "$rc" != 0 ]] || rc=$cleanup_rc
      elif [[ "$rc" == 0 ]]; then
        rm -rf -- "$work"
        echo "LONGHORN_${MODE^^}_SCOPED_CLEAN_PASS" | tr '-' '_'
        echo "LONGHORN_${MODE^^}_PASS" | tr '-' '_'
      fi
      exit "$rc"
    }
    trap idempotence_exit EXIT
    trap 'exit 130' INT
    trap 'exit 143' TERM
    "$PY" "$BASE/tests/helpers/longhorn-lab.py" relay-ensure
    "$PY" "$BASE/tests/helpers/longhorn-lab.py" relay-maintain &
    relay_pid=$!
    if [[ "$MODE" == business-clean ]]; then
      phase=business-scoped-recovery
      "$PY" "$BASE/tests/helpers/longhorn-lab.py" business-clean --relay-pid "${relay_pid:-0}" --recover-recurring-selector
      # Retire off-cluster fixtures only after native backup/volume consumers
      # are gone; Store.cleanup validates its original cluster/run identities.
      phase=backup-fixture-scoped-recovery
      "$PY" "$BASE/tests/helpers/longhorn-lab.py" backup-store-clean
    elif [[ "$MODE" == ownership-request-refusal-qualification ]]; then
      phase=ownership-request-product-refusal
      "$PY" "$BASE/tests/helpers/longhorn-lab.py" business-ownership-request --relay-pid "$relay_pid" | tee "$work/ownership-request.log"
      for scenario in foreign immutable; do
        grep -Fxq "LONGHORN_OWNERSHIP_REQUEST_PRODUCT_REFUSAL_VERIFIED scenario=$scenario" "$work/ownership-request.log"
        grep -Fxq "LONGHORN_OWNERSHIP_REQUEST_SNAPSHOT_BOUND_FOR_CLEANUP scenario=$scenario" "$work/ownership-request.log"
      done
      for marker in LONGHORN_OWNERSHIP_REQUEST_REFUSAL_VERIFIED LONGHORN_BUSINESS_FIXTURES_CLEAN_PASS LONGHORN_OWNERSHIP_REQUEST_BUSINESS_PASS; do
        grep -Fxq "$marker" "$work/ownership-request.log"
      done
      grep -Eq '^LONGHORN_FSYNC_REMOUNT_VERIFIED bytes=[1-9][0-9]* sha256=[a-f0-9]{64} replica_nodes=' "$work/ownership-request.log"
    elif [[ "$MODE" == ownership-controller-refusal-qualification ]]; then
      phase=ownership-controller-product-refusal
      "$PY" "$BASE/tests/helpers/longhorn-lab.py" ownership-controller-refusal-qualification --relay-pid "$relay_pid" | tee "$work/ownership-controller.log"
      for marker in LONGHORN_OWNERSHIP_CONTROLLER_PRODUCT_REFUSAL_VERIFIED LONGHORN_OWNERSHIP_CONTROLLER_CANARY_CLEAN_PASS LONGHORN_OWNERSHIP_CONTROLLER_REFUSAL_BUSINESS_PASS; do
        grep -Fxq "$marker" "$work/ownership-controller.log"
      done
    elif [[ "$MODE" == ownership-class-refusal-qualification ]]; then
      phase=ownership-class-product-refusal
      "$PY" "$BASE/tests/helpers/longhorn-lab.py" ownership-class-refusal-qualification --relay-pid "$relay_pid" | tee "$work/ownership-class.log"
      for marker in LONGHORN_OWNERSHIP_CLASS_PRODUCT_REFUSAL_VERIFIED LONGHORN_OWNERSHIP_CLASS_CANARY_CLEAN_PASS LONGHORN_OWNERSHIP_CLASS_REFUSAL_BUSINESS_PASS; do
        grep -Fxq "$marker" "$work/ownership-class.log"
      done
    elif [[ "$MODE" == ownership-interruption-qualification ]]; then
      phase=ownership-interruption-recovery
      "$PY" "$BASE/tests/helpers/longhorn-lab.py" ownership-interruption-qualification | tee "$work/ownership-interruption.log"
      grep -Eq '^LONGHORN_OWNERSHIP_INTERRUPTION_OBSERVED signal=term rc=143 uid=[a-z0-9-]+$' "$work/ownership-interruption.log"
      grep -Eq '^LONGHORN_OWNERSHIP_INTERRUPTION_OBSERVED signal=kill rc=-9 uid=[a-z0-9-]+$' "$work/ownership-interruption.log"
      grep -Fxq LONGHORN_OWNERSHIP_INTERRUPTION_BUSINESS_PASS "$work/ownership-interruption.log"
    elif [[ "$MODE" == ownership-preinstall-refusal-qualification ]]; then
      phase=ownership-preinstall-product-refusal
      "$PY" "$BASE/tests/helpers/longhorn-lab.py" ownership-preinstall-refusal-qualification --relay-pid "$relay_pid" | tee "$work/ownership-preinstall.log"
      for scenario in namespace release crd; do
        grep -Fxq "LONGHORN_OWNERSHIP_PREINSTALL_PRODUCT_REFUSAL_VERIFIED scenario=$scenario" "$work/ownership-preinstall.log"
        grep -Fxq "LONGHORN_OWNERSHIP_PREINSTALL_CANARY_CLEAN_PASS scenario=$scenario" "$work/ownership-preinstall.log"
      done
      grep -Fxq LONGHORN_OWNERSHIP_PREINSTALL_REFUSAL_BUSINESS_PASS "$work/ownership-preinstall.log"
    elif [[ "$MODE" == ownership-csi-refusal-qualification ]]; then
      phase=ownership-csi-product-refusal
      "$PY" "$BASE/tests/helpers/longhorn-lab.py" ownership-csi-refusal-qualification --relay-pid "$relay_pid" | tee "$work/ownership-csi.log"
      for marker in LONGHORN_OWNERSHIP_CSI_CANARY_CLEAN_PASS LONGHORN_OWNERSHIP_CSI_PRODUCT_REFUSAL_VERIFIED LONGHORN_OWNERSHIP_CSI_REFUSAL_BUSINESS_PASS; do
        grep -Fxq "$marker" "$work/ownership-csi.log"
      done
    elif [[ "$MODE" == ownership-csi-probe ]]; then
      phase=ownership-csi-readonly-subcheck
      "$PY" "$BASE/tests/helpers/longhorn-lab.py" ownership-csi-probe | tee "$work/ownership-csi.log"
      grep -Fxq LONGHORN_OWNERSHIP_CSI_CANARY_CLEAN_PASS "$work/ownership-csi.log"
      grep -Fxq LONGHORN_OWNERSHIP_CSI_PROBE_BUSINESS_PASS "$work/ownership-csi.log"
    elif [[ "$MODE" == preflight-qualification ]]; then
      phase=product-preflight
      "$PY" "$BASE/roles/longhorn/files/longhorn.py" preflight --config "$DIR/yml/longhorn/input.json" \
        --kubeconfig "$KUBECONFIG" --helm "$BASE/extra-bin/helm" | tee "$work/product-preflight.log"
      grep -Fxq LONGHORN_PREFLIGHT_PASS "$work/product-preflight.log"
      phase=node-bootstrap-dry-run
      "$PY" "$BASE/tests/helpers/longhorn-lab.py" node-bootstrap-dry-run | tee "$work/node-bootstrap.log"
      grep -Eq '^LONGHORN_NODE_BOOTSTRAP_DRY_RUN_PASS nodes=6 patches=[0-6]$' "$work/node-bootstrap.log"
    elif [[ "$MODE" == recurring-declarations-qualification ]]; then
      phase=recurring-declarations-qualification
      "$PY" "$BASE/tests/helpers/longhorn-lab.py" recurring-declarations-qualify --relay-pid "$relay_pid" | tee "$work/recurring-declarations.log"
      grep -Fxq 'LONGHORN_RECURRING_DECLARATIONS_QUALIFIED identity=preserved schedule=updated removal=native' "$work/recurring-declarations.log"
    elif [[ "$MODE" == recurring || "$MODE" == recurring-rotation-qualification ]]; then
      stages=(rotation)
      [[ "$MODE" != recurring ]] || stages=(snapshot backup rotation)
      for stage in "${stages[@]}"; do
        phase="recurring-$stage"
        "$PY" "$BASE/tests/helpers/longhorn-lab.py" "business-recurring-$stage" --relay-pid "$relay_pid" | tee "$work/recurring-$stage.log"
        prefix="LONGHORN_RECURRING_${stage^^}"
        stop_prefix="$prefix"
        [[ "$stage" != rotation ]] || stop_prefix=LONGHORN_RECURRING_BACKUP
        grep -Fxq "${stop_prefix}_SCHEDULES_STOPPED_PASS" "$work/recurring-$stage.log"
        grep -Fxq "${prefix}_BUSINESS_PASS" "$work/recurring-$stage.log"
        grep -Fxq "${prefix}_CLEAN_PASS" "$work/recurring-$stage.log"
        if [[ "$stage" == rotation ]]; then
          grep -Fxq LONGHORN_RECURRING_CREDENTIALS_CLEAN_PASS "$work/recurring-$stage.log"
        fi
      done
    elif [[ "$MODE" == uninstall ]]; then
      phase=uninstall
      health_mode=verify-core
      "$PY" "$BASE/tests/helpers/longhorn-lab.py" business-uninstall --relay-pid "$relay_pid" | tee "$work/uninstall.log"
      for marker in LONGHORN_UNINSTALL_REFUSAL_BOUND_PASS LONGHORN_UNINSTALL_REFUSAL_RELEASED_PASS LONGHORN_RETAIN_REBIND_VERIFIED LONGHORN_UNINSTALL_REFUSAL_BUSINESS_PASS LONGHORN_UNINSTALL_BACKUP_RETENTION_BUSINESS_PASS LONGHORN_NATIVE_UNINSTALL_RECOVERY_VERIFIED LONGHORN_UNINSTALL_EXTERNAL_BACKUP_PRESERVED_PASS LONGHORN_UNINSTALL_BUSINESS_PASS; do
        grep -Eq "^${marker}( |$)" "$work/uninstall.log"
      done
    elif [[ "$MODE" == uninstall-backup-qualification ]]; then
      phase=uninstall-backup-qualification
      "$PY" "$BASE/tests/helpers/longhorn-lab.py" business-uninstall-backup-qualification --relay-pid "$relay_pid" | tee "$work/uninstall-backup.log"
      for marker in LONGHORN_UNINSTALL_BACKUP_TARGET_DISCONNECTED_PASS LONGHORN_UNINSTALL_BACKUP_RETENTION_BUSINESS_PASS LONGHORN_UNINSTALL_BACKUP_BUSINESS_PASS; do
        grep -Fxq "$marker" "$work/uninstall-backup.log"
      done
    elif [[ "$MODE" == uninstall-refusal-qualification ]]; then
      phase=uninstall-refusal-qualification
      "$PY" "$BASE/tests/helpers/longhorn-lab.py" business-uninstall-refusal-qualification --relay-pid "$relay_pid" | tee "$work/uninstall-refusal.log"
      for marker in LONGHORN_UNINSTALL_REFUSAL_BOUND_PASS LONGHORN_UNINSTALL_REFUSAL_RELEASED_PASS LONGHORN_RETAIN_REBIND_VERIFIED LONGHORN_UNINSTALL_REFUSAL_BUSINESS_PASS; do
        grep -Eq "^${marker}( |$)" "$work/uninstall-refusal.log"
      done
    elif [[ "$MODE" == recurring-backup-qualification ]]; then
      phase=recurring-backup-qualification
      "$PY" "$BASE/tests/helpers/longhorn-lab.py" business-recurring-backup --relay-pid "$relay_pid" | tee "$work/recurring-backup.log"
      grep -Fxq LONGHORN_RECURRING_BACKUP_SCHEDULES_STOPPED_PASS "$work/recurring-backup.log"
      grep -Fxq LONGHORN_RECURRING_BACKUP_BUSINESS_PASS "$work/recurring-backup.log"
      grep -Fxq LONGHORN_RECURRING_BACKUP_CLEAN_PASS "$work/recurring-backup.log"
    elif [[ "$MODE" == recurring-selector-qualification ]]; then
      phase=recurring-selector-qualification
      "$PY" "$BASE/tests/helpers/longhorn-lab.py" business-recurring-selector --relay-pid "$relay_pid" | tee "$work/recurring-selector.log"
      grep -Fxq LONGHORN_RECURRING_SNAPSHOT_SCHEDULES_STOPPED_PASS "$work/recurring-selector.log"
      grep -Fxq LONGHORN_RECURRING_SELECTOR_BUSINESS_PASS "$work/recurring-selector.log"
      grep -Fxq LONGHORN_RECURRING_SELECTOR_CLEAN_PASS "$work/recurring-selector.log"
    elif [[ "$MODE" == recurring-snapshot-qualification ]]; then
      phase=recurring-snapshot-qualification
      "$PY" "$BASE/tests/helpers/longhorn-lab.py" business-recurring-snapshot --relay-pid "$relay_pid" | tee "$work/recurring-snapshot.log"
      grep -Fxq LONGHORN_RECURRING_SNAPSHOT_SCHEDULES_STOPPED_PASS "$work/recurring-snapshot.log"
      grep -Fxq LONGHORN_RECURRING_SNAPSHOT_BUSINESS_PASS "$work/recurring-snapshot.log"
      grep -Fxq LONGHORN_RECURRING_SNAPSHOT_CLEAN_PASS "$work/recurring-snapshot.log"
    elif [[ "$MODE" == backup-accounts-qualification ]]; then
      phase=backup-accounts-qualification
      "$PY" "$BASE/tests/helpers/longhorn-lab.py" backup-accounts-qualify | tee "$work/backup-accounts.log"
      grep -Eq '^LONGHORN_BACKUP_ACCOUNTS_QUALIFIED accounts=2 policy=bucket-only authorization_refusals=4 revoked_key_refusals=1 sha256=[a-f0-9]{64}$' "$work/backup-accounts.log"
      grep -Fxq LONGHORN_BACKUP_ACCOUNTS_CLEAN_PASS "$work/backup-accounts.log"
    elif [[ "$MODE" == backup-store-qualification ]]; then
      phase=backup-store-qualification
      "$PY" "$BASE/tests/helpers/longhorn-lab.py" backup-store-qualify | tee "$work/backup-store.log"
      grep -Fxq 'LONGHORN_BACKUP_STORE_QUALIFIED clients=6 tls=verified object_readback=exact' "$work/backup-store.log"
    elif [[ "$MODE" == cache-qualification ]]; then
      phase=artifacts
      "$PY" "$BASE/tests/helpers/longhorn-lab.py" cache
      "$PY" "$BASE/tests/helpers/longhorn-lab.py" supervise-core --relay-pid "$relay_pid" --step longhorn-download
      "$PY" "$BASE/tests/helpers/longhorn-lab.py" cache
    elif [[ "$MODE" != idempotence && "$MODE" != recover && "$MODE" != core-smoke && "$MODE" != backup && "$MODE" != backup-refusal && "$MODE" != encryption-block && "$MODE" != encryption-backup ]]; then
      phase=product-reconcile
      "$PY" "$BASE/tests/helpers/longhorn-lab.py" supervise-core --relay-pid "$relay_pid" --step longhorn-setup
    fi
    if [[ "$MODE" != ownership-request-refusal-qualification && "$MODE" != ownership-controller-refusal-qualification && "$MODE" != ownership-class-refusal-qualification && "$MODE" != ownership-interruption-qualification && "$MODE" != ownership-preinstall-refusal-qualification && "$MODE" != ownership-csi-refusal-qualification && "$MODE" != ownership-csi-probe && "$MODE" != business-clean && "$MODE" != cache-qualification && "$MODE" != preflight-qualification && "$MODE" != backup-store-qualification && "$MODE" != backup-accounts-qualification && "$MODE" != recurring-declarations-qualification && "$MODE" != recurring-snapshot-qualification && "$MODE" != recurring-selector-qualification && "$MODE" != recurring-backup-qualification && "$MODE" != recurring-rotation-qualification && "$MODE" != recurring && "$MODE" != uninstall-refusal-qualification && "$MODE" != uninstall-backup-qualification && "$MODE" != uninstall ]]; then
      phase="business-$MODE"
      if [[ "$MODE" == backup || "$MODE" == backup-refusal || "$MODE" == encryption || "$MODE" == encryption-block || "$MODE" == encryption-backup || "$MODE" == network-policy || "$MODE" == security ]]; then
        "$PY" "$BASE/tests/helpers/longhorn-lab.py" "business-$MODE" --relay-pid "$relay_pid" | tee "$work/backup.log"
        expected="LONGHORN_${MODE^^}_BUSINESS_PASS"
        grep -Fxq "${expected//-/_}" "$work/backup.log"
      else
        "$PY" "$BASE/tests/helpers/longhorn-lab.py" "business-$MODE" --relay-pid "$relay_pid"
      fi
    fi
    ;;
  install)
    cd "$BASE"
    PY="$BASE/.venv/bin/python"
    DIR="$BASE/clusters/longhorn-gate"
    export PYTHONPATH="$BASE" PATH="$BASE/kube-bin:$BASE/extra-bin:$PATH"
    export KUBECONFIG="$DIR/kubectl.kubeconfig"
    "$PY" "$BASE/tests/helpers/longhorn-lab.py" verify-core
    "$PY" "$BASE/tests/helpers/longhorn-lab.py" verify-core-smoke
    "$PY" "$BASE/tests/helpers/longhorn-lab.py" verify-disk-qualification
    work="$(mktemp -d /var/tmp/kubeauto-longhorn-install.XXXXXX)"
    chmod 0700 "$work"
    control_config=/root/.kube/config
    [[ ! -L "$control_config" ]] || exit 1
    [[ ! -f "$control_config" ]] || cp -a "$control_config" "$work/control-kubeconfig"
    phase=transport
    relay_pid=
    host_owned=no
    install_exit() {
      local rc=$? cleanup_rc=0
      trap - EXIT INT TERM
      if [[ "$rc" != 0 ]]; then
        echo "LONGHORN_FIRST_FAILURE phase=$phase rc=$rc diagnostics=$work" >&2
        printf 'rc=%s phase=%s\n' "$rc" "$phase" > "$work/first-failure.status"
        kubectl --request-timeout=5s get nodes -o wide > "$work/nodes.txt" 2>&1 || true
        kubectl --request-timeout=5s get pods -A -o wide > "$work/pods.txt" 2>&1 || true
        kubectl --request-timeout=5s get events -A --sort-by=.lastTimestamp > "$work/events.txt" 2>&1 || true
        kubectl --request-timeout=5s -n longhorn-system describe pods > "$work/pod-descriptions.txt" 2>&1 || true
        kubectl --request-timeout=5s -n longhorn-system logs -l app=longhorn-manager --all-containers --tail=100 --max-log-requests=6 > "$work/manager.log" 2>&1 || true
        "$PY" "$BASE/tests/helpers/longhorn-lab.py" business-clean --relay-pid "${relay_pid:-0}" || cleanup_rc=$?
        # Disks/services are recoverable only after the product's own guard
        # proves there are no Longhorn resources. Otherwise retain the journal
        # for native uninstall; never bypass a finalizer or wipe live replicas.
        "$PY" "$BASE/tests/helpers/longhorn-lab.py" cleanup-disks || cleanup_rc=$?
        if [[ "$host_owned" == yes && "$cleanup_rc" == 0 ]]; then
          "$PY" "$BASE/tests/helpers/longhorn-lab.py" restore-hosts || cleanup_rc=$?
        fi
      fi
      if [[ -n "$relay_pid" ]]; then
        if [[ "$rc" == 0 ]] && ! kill -0 "$relay_pid" 2>/dev/null; then rc=1; fi
        kill -TERM "$relay_pid" 2>/dev/null || true
        wait "$relay_pid" || { [[ "$rc" != 0 ]] || rc=1; }
      fi
      "$PY" "$BASE/tests/helpers/longhorn-lab.py" relay-clean || cleanup_rc=$?
      if [[ -f "$work/control-kubeconfig" ]]; then
        cp -a "$work/control-kubeconfig" "$control_config" || cleanup_rc=$?
      else
        rm -f -- "$control_config" || cleanup_rc=$?
      fi
      if [[ "$cleanup_rc" != 0 ]]; then
        echo "LONGHORN_CLEANUP_FAIL phase=$phase rc=$cleanup_rc journal=retained" >&2
        [[ "$rc" != 0 ]] || rc=$cleanup_rc
      elif [[ "$rc" == 0 ]]; then
        rm -rf -- "$work"
        echo LONGHORN_INSTALL_SCOPED_CLEAN_PASS
        echo LONGHORN_INSTALL_PASS
      fi
      exit "$rc"
    }
    trap install_exit EXIT
    trap 'exit 130' INT
    trap 'exit 143' TERM
    "$PY" "$BASE/tests/helpers/longhorn-lab.py" relay-ensure
    "$PY" "$BASE/tests/helpers/longhorn-lab.py" relay-maintain &
    relay_pid=$!
    phase=artifacts
    "$PY" "$BASE/tests/helpers/longhorn-lab.py" cache
    "$PY" "$BASE/tests/helpers/longhorn-lab.py" supervise-core --relay-pid "$relay_pid" --step longhorn-download
    "$PY" "$BASE/tests/helpers/longhorn-lab.py" cache
    phase=host-journal
    "$PY" "$BASE/tests/helpers/longhorn-lab.py" capture-hosts
    host_owned=yes
    CEPH_STATE_DIR="$work" bash "$BASE/tests/helpers/ceph-host-probe.sh"
    phase=prepare-disks
    "$PY" "$BASE/tests/helpers/longhorn-lab.py" prepare-disks --allowlist "$work/disk-allowlist"
    phase=credentials
    "$PY" "$BASE/tests/helpers/longhorn-lab.py" credentials
    phase=product-install
    "$PY" "$BASE/tests/helpers/longhorn-lab.py" supervise-core --relay-pid "$relay_pid" --step longhorn-setup
    phase=business
    "$PY" "$BASE/tests/helpers/longhorn-lab.py" business
    ;;
  core)
    cd "$BASE"
    PY="$BASE/.venv/bin/python"
    DIR="$BASE/clusters/longhorn-gate"
    export PYTHONPATH="$BASE" PATH="$BASE/kube-bin:$BASE/extra-bin:$PATH"
    export KUBECONFIG="$DIR/kubectl.kubeconfig"
    [[ ! -e "$DIR" && ! -L "$DIR" ]] || {
      echo 'LONGHORN_PREFLIGHT_FAIL class=environment reason=core-requires-clean-start' >&2; exit 1;
    }
    "$PY" "$BASE/tests/helpers/longhorn-lab.py" verify-transport
    bash "$BASE/tests/helpers/lab-wipe-nodes.sh" --verify --rocky-only "${NODES[@]}"
    "$PY" "$BASE/tests/helpers/longhorn-lab.py" core-host-preflight
    curl --fail --silent --show-error --noproxy '*' --max-time 5 http://127.0.0.1:5000/v2/ >/dev/null
    work="$(mktemp -d /var/tmp/kubeauto-longhorn-core.XXXXXX)"
    chmod 0700 "$work"
    control_config=/root/.kube/config
    [[ ! -L "$control_config" ]] || exit 1
    [[ ! -f "$control_config" ]] || cp -a "$control_config" "$work/control-kubeconfig"
    phase=configure
    owned=no
    relay_pid=
    core_exit() {
      local rc=$? cleanup_rc=0
      trap - EXIT INT TERM
      if [[ "$rc" != 0 ]]; then
        echo "LONGHORN_FIRST_FAILURE phase=$phase rc=$rc diagnostics=$work" >&2
        printf 'rc=%s phase=%s\n' "$rc" "$phase" > "$work/first-failure.status"
        if [[ -f "$KUBECONFIG" ]]; then
          kubectl --request-timeout=5s get nodes -o wide > "$work/nodes.txt" 2>&1 || true
          kubectl --request-timeout=5s get pods -A -o wide > "$work/pods.txt" 2>&1 || true
          kubectl --request-timeout=5s get events -A --sort-by=.lastTimestamp > "$work/events.txt" 2>&1 || true
        fi
      fi
      if [[ -n "$relay_pid" ]]; then
        if [[ "$rc" == 0 ]] && ! kill -0 "$relay_pid" 2>/dev/null; then rc=1; fi
        kill -TERM "$relay_pid" 2>/dev/null || true
        wait "$relay_pid" || { [[ "$rc" != 0 ]] || rc=1; }
      fi
      if [[ "$owned" == yes ]]; then
        "$PY" "$BASE/tests/helpers/longhorn-lab.py" relay-clean || cleanup_rc=$?
        if [[ "$rc" != 0 && "$cleanup_rc" == 0 ]]; then
          bash "$BASE/tests/helpers/lab-wipe-nodes.sh" --rocky-only "${NODES[@]}" || cleanup_rc=$?
          if [[ "$cleanup_rc" == 0 ]]; then
            bash "$BASE/tests/helpers/lab-wipe-nodes.sh" --verify --rocky-only "${NODES[@]}" || cleanup_rc=$?
          fi
          if [[ "$cleanup_rc" == 0 ]]; then
            "$PY" "$BASE/tests/helpers/longhorn-lab.py" archive-core || cleanup_rc=$?
          fi
        fi
      fi
      if [[ -f "$work/control-kubeconfig" ]]; then
        cp -a "$work/control-kubeconfig" "$control_config" || cleanup_rc=$?
      else
        rm -f -- "$control_config" || cleanup_rc=$?
      fi
      if [[ "$cleanup_rc" != 0 ]]; then
        echo "LONGHORN_CLEANUP_FAIL phase=$phase rc=$cleanup_rc" >&2
        [[ "$rc" != 0 ]] || rc=$cleanup_rc
      elif [[ "$rc" == 0 ]]; then
        rm -rf -- "$work"
        echo LONGHORN_CORE_SCOPED_CLEAN_PASS
        echo LONGHORN_CORE_PASS
      fi
      exit "$rc"
    }
    trap core_exit EXIT
    trap 'exit 130' INT
    trap 'exit 143' TERM
    "$PY" "$BASE/kubecli.py" new longhorn-gate </dev/null
    "$PY" "$BASE/tests/helpers/longhorn-lab.py" configure
    owned=yes
    phase=transport
    "$PY" "$BASE/tests/helpers/longhorn-lab.py" relay-ensure
    "$PY" "$BASE/tests/helpers/longhorn-lab.py" relay-maintain &
    relay_pid=$!
    phase=core-artifacts
    "$PY" "$BASE/tests/helpers/longhorn-lab.py" supervise-core --relay-pid "$relay_pid" --step download
    phase=core-setup
    "$PY" "$BASE/tests/helpers/longhorn-lab.py" supervise-core --relay-pid "$relay_pid" --step setup
    kubectl wait --for=condition=Ready nodes --all --timeout=10m
    "$PY" "$BASE/tests/helpers/longhorn-lab.py" core-host-preflight
    "$PY" "$BASE/tests/helpers/longhorn-lab.py" verify-core
    phase=production-smoke
    "$PY" "$BASE/tests/helpers/longhorn-lab.py" business-core-smoke
    ;;
  *) echo 'LONGHORN_PREFLIGHT_FAIL class=test-gate reason=unimplemented-live-mode' >&2; exit 2 ;;
esac
