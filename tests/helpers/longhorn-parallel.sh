#!/usr/bin/env bash
# Fixed disjoint V1/V2 branches under one unchanged exclusive parent lease.
set -Eeuo pipefail
umask 077
BASE=/usr/local/kubeauto
cd "$BASE"
[[ "${LONGHORN_PARENT_LEASE_HELD:-no}" == yes ]] || exit 1
for entry in '9 kubeauto-ceph-priority.lock' '7 kubeauto-ceph-compute.lock' '8 kubeauto-redis-core-pool.lock'; do
  read -r fd filename <<< "$entry"
  [[ "/proc/self/fd/$fd" -ef "/var/lock/$filename" ]] || exit 1
  flock -n "$fd" || exit 1
done
PY="$BASE/.venv/bin/python"
case "${1:-}" in
  v1-ownership-operations|v2-failure-platform|v1-ownership-tail-operations|v2-failure-tail-platform|v1-operations-recovery|v2-worker-tail-platform|v1-operations-cleanup-recovery|v2-disk-tail-platform|v1-operations-remaining-recovery|v1-recovery-tail|v1-dr-tail|v1-upgrade-tail|v2-ready-tail|v2-install-ready-tail|v2-maintenance-tail|v1-monitoring-tail|v2-resource-tail)
    # Each pool advances through its existing owning gates independently.
    # Only V1 owns temporary writes to the shared control kubeconfig.
    profile=v1
    stages=(v1-ownership v1-operations system-restore disaster-recovery v1-upgrade-lifecycle observability)
    scripts=(longhorn-ownership-aggregate.sh longhorn-parallel.sh longhorn-live.sh longhorn-live.sh longhorn-live.sh longhorn-live.sh)
    modes=(ownership v1-operations system-restore disaster-recovery v1-upgrade-lifecycle observability)
    batch_marker=LONGHORN_PARALLEL_V1_OWNERSHIP_OPERATIONS
    if [[ "$1" == v2-failure-platform ]]; then
      profile=v2
      stages=(v2-failure-remaining v2-platform-install v2-middleware v2-interrupt-qualification)
      scripts=(longhorn-v2-failure.sh longhorn-parallel.sh longhorn-v2-middleware.sh longhorn-live.sh)
      modes=(v2-failure-remaining v2-platform-install v2-middleware v2-interrupt-qualification)
      batch_marker=LONGHORN_PARALLEL_V2_FAILURE_PLATFORM
    fi
    if [[ "$1" == v1-ownership-tail-operations ]]; then
      # Resume only the two ownership scopes that did not complete. Their
      # unchanged live entry points preserve the installed baseline.
      stages=(ownership-controller-refusal-qualification ownership-request-refusal-qualification v1-operations system-restore disaster-recovery v1-upgrade-lifecycle observability)
      scripts=(longhorn-live.sh longhorn-live.sh longhorn-parallel.sh longhorn-live.sh longhorn-live.sh longhorn-live.sh longhorn-live.sh)
      modes=("${stages[@]}")
      batch_marker=LONGHORN_PARALLEL_V1_OWNERSHIP_TAIL_OPERATIONS
    elif [[ "$1" == v2-failure-tail-platform ]]; then
      profile=v2
      # Each original focused fault validates its business and cleanup before
      # advancing. Accepted replica-im/manager/attached-spdk are not repeated.
      stages=(v2-failure-remote-spdk v2-failure-replica-network v2-failure-worker-reboot v2-failure-single-disk v2-failure-rebuild-qos v2-failure-all-replicas v2-platform-install v2-middleware v2-interrupt-qualification)
      scripts=(longhorn-v2-failure.sh longhorn-v2-failure.sh longhorn-v2-failure.sh longhorn-v2-failure.sh longhorn-v2-failure.sh longhorn-v2-failure.sh longhorn-parallel.sh longhorn-v2-middleware.sh longhorn-live.sh)
      modes=("${stages[@]}")
      batch_marker=LONGHORN_PARALLEL_V2_FAILURE_TAIL_PLATFORM
    fi
    if [[ "$1" == v1-operations-recovery ]]; then
      stages=(v1-operations system-restore disaster-recovery v1-upgrade-lifecycle observability)
      scripts=(longhorn-parallel.sh longhorn-live.sh longhorn-live.sh longhorn-live.sh longhorn-live.sh)
      modes=("${stages[@]}")
      batch_marker=LONGHORN_PARALLEL_V1_OPERATIONS_RECOVERY
    elif [[ "$1" == v2-worker-tail-platform ]]; then
      profile=v2
      # The first five fault scopes already have independent accepted evidence.
      stages=(v2-failure-cleanup v2-failure-worker-reboot v2-failure-single-disk v2-failure-rebuild-qos v2-failure-all-replicas v2-platform-install v2-middleware v2-interrupt-qualification)
      scripts=(longhorn-v2-failure.sh longhorn-v2-failure.sh longhorn-v2-failure.sh longhorn-v2-failure.sh longhorn-v2-failure.sh longhorn-parallel.sh longhorn-v2-middleware.sh longhorn-live.sh)
      modes=("${stages[@]}")
      batch_marker=LONGHORN_PARALLEL_V2_WORKER_TAIL_PLATFORM
    fi
    if [[ "$1" == v1-operations-cleanup-recovery ]]; then
      # Recover only the original maintenance fixture before re-entering operations.
      stages=(maintenance-cleanup v1-operations system-restore disaster-recovery v1-upgrade-lifecycle observability)
      scripts=(longhorn-live.sh longhorn-parallel.sh longhorn-live.sh longhorn-live.sh longhorn-live.sh longhorn-live.sh)
      modes=("${stages[@]}")
      batch_marker=LONGHORN_PARALLEL_V1_OPERATIONS_CLEANUP_RECOVERY
    elif [[ "$1" == v2-disk-tail-platform ]]; then
      profile=v2
      # Six faults are accepted and their journal is absent. Start at the next
      # original owning scope; do not repeat cleanup or the accepted reboot.
      stages=(v2-failure-single-disk v2-failure-rebuild-qos v2-failure-all-replicas v2-platform-install v2-middleware v2-interrupt-qualification)
      scripts=(longhorn-v2-failure.sh longhorn-v2-failure.sh longhorn-v2-failure.sh longhorn-parallel.sh longhorn-v2-middleware.sh longhorn-live.sh)
      modes=("${stages[@]}")
      batch_marker=LONGHORN_PARALLEL_V2_DISK_TAIL_PLATFORM
    fi
    if [[ "$1" == v1-operations-remaining-recovery ]]; then
      # Maintenance, capacity, integrity and disk lifecycle are accepted.
      stages=(v1-operations-remaining system-restore disaster-recovery v1-upgrade-lifecycle observability)
      scripts=(longhorn-parallel.sh longhorn-live.sh longhorn-live.sh longhorn-live.sh longhorn-live.sh)
      modes=("${stages[@]}")
      batch_marker=LONGHORN_PARALLEL_V1_OPERATIONS_REMAINING_RECOVERY
    fi
    if [[ "$1" == v1-recovery-tail ]]; then
      # Verify retained recovery material, finish the original restore, then advance.
      stages=(system-restore-prepared-recovery disaster-recovery v1-upgrade-lifecycle observability-support observability)
      scripts=(longhorn-live.sh longhorn-live.sh longhorn-live.sh longhorn-live.sh longhorn-live.sh)
      modes=("${stages[@]}")
      batch_marker=LONGHORN_PARALLEL_V1_RECOVERY_TAIL
    fi
    if [[ "$1" == v1-dr-tail ]]; then
      stages=(disaster-recovery v1-upgrade-lifecycle observability-support observability)
      scripts=(longhorn-live.sh longhorn-live.sh longhorn-live.sh longhorn-live.sh)
      modes=("${stages[@]}")
      batch_marker=LONGHORN_PARALLEL_V1_DR_TAIL
    fi
    if [[ "$1" == v1-upgrade-tail ]]; then
      # The owning prepare gate proves the old-ready, empty initial failure.
      stages=(v1-upgrade-lifecycle observability-support observability)
      scripts=(longhorn-live.sh longhorn-live.sh longhorn-live.sh)
      modes=("${stages[@]}")
      batch_marker=LONGHORN_PARALLEL_V1_UPGRADE_TAIL
    fi
    if [[ "$1" == v2-ready-tail || "$1" == v2-install-ready-tail ]]; then
      profile=v2
      # Continue independent source work while V1 owns the separate DR target.
      # The monitoring focus reuses accepted CPU evidence, then runs all business.
      stages=(v2-runtime-resources v2-install-tail v2-middleware v2-interrupt-qualification v2-observability-monitoring)
      scripts=(longhorn-live.sh longhorn-parallel.sh longhorn-v2-middleware.sh longhorn-live.sh longhorn-live.sh)
      modes=("${stages[@]}")
      batch_marker=LONGHORN_PARALLEL_V2_READY_TAIL
      if [[ "$1" == v2-install-ready-tail ]]; then
        # The memory-budget gap blocks runtime profiles, not these default-profile scopes.
        stages=("${stages[@]:1}"); scripts=("${scripts[@]:1}"); modes=("${modes[@]:1}")
        batch_marker=LONGHORN_PARALLEL_V2_INSTALL_READY_TAIL
      fi
    fi
    if [[ "$1" == v2-maintenance-tail ]]; then
      # Coexistence has its complete independent LONGHORN-46 evidence.
      profile=v2
      stages=(v2-maintenance-drain-qualification v2-middleware v2-interrupt-qualification v2-observability-monitoring)
      scripts=(longhorn-live.sh longhorn-v2-middleware.sh longhorn-live.sh longhorn-live.sh)
      modes=("${stages[@]}")
      batch_marker=LONGHORN_PARALLEL_V2_MAINTENANCE_TAIL
    fi
    if [[ "$1" == v1-monitoring-tail ]]; then
      stages=(observability-image-cache observability-cleanup observability)
      scripts=(longhorn-live.sh longhorn-live.sh longhorn-live.sh)
      modes=("${stages[@]}")
      batch_marker=LONGHORN_PARALLEL_V1_MONITORING_TAIL
    elif [[ "$1" == v2-resource-tail ]]; then
      profile=v2
      stages=(v2-runtime-resources v2-middleware v2-interrupt-qualification v2-observability-monitoring)
      scripts=(longhorn-live.sh longhorn-v2-middleware.sh longhorn-live.sh longhorn-live.sh)
      modes=("${stages[@]}")
      batch_marker=LONGHORN_PARALLEL_V2_RESOURCE_TAIL
    fi
    work="$(mktemp -d /var/tmp/kubeauto-longhorn-parallel-sequence.XXXXXX)"
    chmod 0700 "$work"
    echo "${batch_marker}_DURABLE_DIRECTORY path=$work"
    if [[ "$profile" == v1 ]]; then
      "$PY" "$BASE/tests/helpers/longhorn-parallel.py" context-fingerprint > "$work/control-context.json"
    fi
    for index in "${!stages[@]}"; do
      stage="$work/${stages[$index]}"
      mkdir -m 0700 "$stage"
      echo "${batch_marker}_STAGE_START stage=${stages[$index]}"
      command=(bash "$BASE/tests/helpers/${scripts[$index]}" "${modes[$index]}")
      if [[ "$1" == v2-resource-tail && "${stages[$index]}" == v2-runtime-resources ]]; then
        "$PY" "$BASE/tests/helpers/longhorn-v2-platform-install.py" snapshot platform \
          --root "$BASE" --receipt "$stage/baseline-before.json"
        command=(bash "$BASE/tests/helpers/run-durable-gate.sh" "$stage/owning" LONGHORN_GATE_EXIT "${command[@]}")
      fi
      KUBEAUTO_LOG_DIR="$stage/product-logs" bash "$BASE/tests/helpers/run-durable-gate.sh" \
        "$stage/$profile" "LONGHORN_PARALLEL_${profile^^}_EXIT" \
        "${command[@]}" 2>&1 | tee "$stage/$profile.log"
      if [[ "$1" == v2-resource-tail && "${stages[$index]}" == v2-runtime-resources ]]; then
        cp "$stage/$profile.log" "$stage/gate.log"
        cp "$stage/owning.exit" "$stage/durable-exit"
        cp "$stage/owning.finalized" "$stage/durable-finalized"
        "$PY" "$BASE/tests/helpers/longhorn-v2-platform-install.py" snapshot platform \
          --root "$BASE" --receipt "$stage/baseline-after.json"
        "$PY" "$BASE/tests/helpers/longhorn-v2-platform-install.py" verify-stage v2-runtime-resources \
          --root "$BASE" --directory "$stage"
      fi
      "$PY" "$BASE/tests/helpers/longhorn-parallel.py" verify-branch --directory "$stage" --branch "${stages[$index]}"
      if [[ "$profile" == v1 ]]; then
        "$PY" "$BASE/tests/helpers/longhorn-parallel.py" context-verify --record "$work/control-context.json"
      fi
      echo "${batch_marker}_STAGE_PASS stage=${stages[$index]} durable_rc=0 finalized=0"
    done
    echo "${batch_marker}_SCOPED_CLEAN_PASS"
    echo "${batch_marker}_PASS"
    exit 0
    ;;
  v1-operations-v2-disk-tail)
    v1_script=longhorn-parallel.sh
    v1_mode=v1-operations-cleanup-recovery
    v2_script=longhorn-parallel.sh
    v2_mode=v2-disk-tail-platform
    v1_evidence=v1-operations-cleanup-recovery v2_evidence=v2-disk-tail-platform
    parent_marker=LONGHORN_V1_OPERATIONS_V2_DISK_TAIL
    ;;
  v1-operations-v2-worker-tail)
    v1_script=longhorn-parallel.sh
    v1_mode=v1-operations-recovery
    v2_script=longhorn-parallel.sh
    v2_mode=v2-worker-tail-platform
    v1_evidence=v1-operations-recovery v2_evidence=v2-worker-tail-platform
    parent_marker=LONGHORN_V1_OPERATIONS_V2_WORKER_TAIL
    ;;
  v1-ownership-tail-v2-failure-tail)
    v1_script=longhorn-parallel.sh
    v1_mode=v1-ownership-tail-operations
    v2_script=longhorn-parallel.sh
    v2_mode=v2-failure-tail-platform
    v1_evidence=v1-ownership-tail-operations v2_evidence=v2-failure-tail-platform
    parent_marker=LONGHORN_V1_OWNERSHIP_TAIL_V2_FAILURE_TAIL
    ;;
  v1-ownership-operations-v2-failure-platform)
    v1_script=longhorn-parallel.sh
    v1_mode=v1-ownership-operations
    v2_script=longhorn-parallel.sh
    v2_mode=v2-failure-platform
    v1_evidence=v1-ownership-operations v2_evidence=v2-failure-platform
    parent_marker=LONGHORN_V1_OWNERSHIP_OPERATIONS_V2_FAILURE_PLATFORM
    ;;
  v2-platform-install|v2-install-tail)
    # Insert in the existing longhorn-parallel.sh case. The parent retains its
    # existing lease and V1 branch; this owns only the V2 source cluster.
    export KUBECONFIG="$BASE/clusters/longhorn-v2-gate/kubectl.kubeconfig"
    work="$(mktemp -d /var/tmp/kubeauto-longhorn-v2-platform-install.XXXXXX)"
    chmod 0700 "$work"
    echo "LONGHORN_V2_PLATFORM_INSTALL_DURABLE_DIRECTORY path=$work"
    modes=(v2-runtime-resources v2-coexist-qualification v2-maintenance-drain-qualification)
    batch_marker=LONGHORN_V2_PLATFORM_INSTALL_BATCH
    if [[ "$1" == v2-install-tail ]]; then
      # The runtime resource scope is accepted; retain its exact result.
      modes=(v2-coexist-qualification v2-maintenance-drain-qualification)
      batch_marker=LONGHORN_V2_INSTALL_TAIL_BATCH
    fi
    for mode in "${modes[@]}"; do
      stage="$work/$mode"
      mkdir -m 0700 "$stage"
      echo "LONGHORN_V2_PLATFORM_INSTALL_STAGE_START stage=$mode"
      "$PY" "$BASE/tests/helpers/longhorn-v2-platform-install.py" snapshot platform \
        --root "$BASE" --receipt "$stage/baseline-before.json"
      bash "$BASE/tests/helpers/run-durable-gate.sh" "$stage/owning" LONGHORN_GATE_EXIT \
        bash "$BASE/tests/helpers/longhorn-live.sh" "$mode" 2>&1 | tee "$stage/gate.log"
      cp "$stage/owning.exit" "$stage/durable-exit"
      cp "$stage/owning.finalized" "$stage/durable-finalized"
      "$PY" "$BASE/tests/helpers/longhorn-v2-platform-install.py" snapshot platform \
        --root "$BASE" --receipt "$stage/baseline-after.json"
      "$PY" "$BASE/tests/helpers/longhorn-v2-platform-install.py" verify-stage "$mode" \
        --root "$BASE" --directory "$stage"
    done
    echo "${batch_marker}_SCOPED_CLEAN_PASS"
    echo "${batch_marker}_PASS"
    exit 0
    ;;
  v1|v1-backups|v1-backups-remaining|v1-post-revision-backups|v1-operations|v1-operations-remaining|v1-data-protection)
    # Each exact V1 mode uses its own explicit kubeconfig and verifies the
    # original shared context without saving or restoring it.
    export LONGHORN_PARALLEL_CONTEXT=explicit-kubeconfig
    work="$(mktemp -d /var/tmp/kubeauto-longhorn-parallel-v1.XXXXXX)"
    chmod 0700 "$work"
    echo "LONGHORN_PARALLEL_V1_DURABLE_DIRECTORY path=$work"
    batch_marker=LONGHORN_PARALLEL_V1
    modes=(idempotence security)
    if [[ "$1" == v1-backups || "$1" == v1-backups-remaining ]]; then
      batch_marker=LONGHORN_PARALLEL_V1_BACKUPS
      # Each original full module owns its fixtures and cleanup. Uninstall
      # must remain last because success removes the V1 storage installation.
      modes=(platform security backup backup-refusal recurring encryption uninstall)
      if [[ "$1" == v1-backups-remaining ]]; then
        batch_marker=LONGHORN_PARALLEL_V1_BACKUPS_REMAINING
        modes=(security backup backup-refusal recurring encryption uninstall)
      fi
    fi
    if [[ "$1" == v1-post-revision-backups ]]; then
      batch_marker=LONGHORN_PARALLEL_V1_POST_REVISION_BACKUPS
      modes=(v1-im-revision-2-adopt-qualification backup-refusal recurring encryption uninstall)
    fi
    if [[ "$1" == v1-data-protection ]]; then
      batch_marker=LONGHORN_PARALLEL_V1_DATA_PROTECTION
      modes=(backup backup-refusal recurring)
    fi
    if [[ "$1" == v1-operations ]]; then
      # Preserve the healthy installed baseline. Every original module owns
      # its fault/fixtures and must finish scoped cleanup before the next one.
      batch_marker=LONGHORN_PARALLEL_V1_OPERATIONS
      modes=(maintenance capacity integrity disk-lifecycle node-failure network-failure disk-failure last-replica middleware)
    fi
    if [[ "$1" == v1-operations-remaining ]]; then
      # Resume pending faults; recover only the original node-failure receipt.
      batch_marker=LONGHORN_PARALLEL_V1_OPERATIONS_REMAINING
      modes=(node-failure network-failure disk-failure last-replica middleware)
      journal="$BASE/clusters/longhorn-gate/longhorn-node-failure.json"
      if [[ -e "$journal" || -L "$journal" ]]; then
        modes=(node-failure-cleanup "${modes[@]}")
      fi
    fi
    for mode in "${modes[@]}"; do
      marker="LONGHORN_${mode^^}"
      marker="${marker//-/_}"
      echo "${batch_marker}_STAGE_START stage=$mode"
      bash "$BASE/tests/helpers/run-durable-gate.sh" "$work/$mode" "${marker}_EXIT" \
        bash "$BASE/tests/helpers/longhorn-live.sh" "$mode" 2>&1 | tee "$work/$mode.log"
      "$PY" "$BASE/tests/helpers/longhorn-parallel.py" verify-branch --directory "$work" --branch "$mode"
      echo "${batch_marker}_STAGE_PASS stage=$mode durable_rc=0 finalized=0"
    done
    echo "${batch_marker}_PASS"
    exit 0
    ;;
  v1-ownership-v2-linked-acceptance)
    # Ownership's fixed full gate includes V1 reinstall. V2 linked helpers use
    # only their explicit kubeconfig; ownership restores its shared context.
    v1_script=longhorn-ownership-aggregate.sh
    v1_mode=ownership
    v2_script=longhorn-v2-linked-acceptance.sh
    v2_mode=v2-linked-acceptance
    v1_evidence=v1-ownership v2_evidence=v2-linked-acceptance
    parent_marker=LONGHORN_V1_OWNERSHIP_V2_LINKED_ACCEPTANCE
    ;;
  v1-backup-v2-data-protection)
    # Distinct owned backup stores/endpoints; only V2 uses the DR target.
    # V1 restore/upgrade modules that borrow that target remain outside.
    v1_script=longhorn-parallel.sh
    v1_mode=v1-data-protection
    v2_script=longhorn-v2-data-protection.sh
    v2_mode=v2-data-protection
    v1_evidence=v1-data-protection v2_evidence=v2-data-protection
    parent_marker=LONGHORN_V1_BACKUP_V2_DATA_PROTECTION
    ;;
  v1-operations-v2-platform-install)
    v1_script=longhorn-parallel.sh
    v1_mode=v1-operations
    v2_script=longhorn-parallel.sh
    v2_mode=v2-platform-install
    v1_evidence=v1-operations v2_evidence=v2-platform-install
    parent_marker=LONGHORN_V1_OPERATIONS_V2_PLATFORM_INSTALL
    ;;
  v1-ownership-v2-failure-remaining)
    v1_script=longhorn-ownership-aggregate.sh
    v1_mode=ownership
    v2_script=longhorn-v2-failure.sh
    v2_mode=v2-failure-remaining
    v1_evidence=v1-ownership v2_evidence=v2-failure-remaining
    parent_marker=LONGHORN_V1_OWNERSHIP_V2_FAILURE_REMAINING
    ;;
  v1-post-revision-backups-v2-failure-acceptance)
    v1_script=longhorn-parallel.sh
    v1_mode=v1-post-revision-backups
    v2_script=longhorn-v2-failure.sh
    v2_mode=v2-failure
    v1_evidence=v1-post-revision-backups v2_evidence=v2-failure-acceptance
    parent_marker=LONGHORN_V1_POST_REVISION_BACKUPS_V2_FAILURE_ACCEPTANCE
    ;;
  v1-operations-remaining-v2-monitoring)
    v1_script=longhorn-parallel.sh
    v1_mode=v1-operations-remaining-recovery
    v2_script=longhorn-v2-observability.sh
    v2_mode=v2-observability-monitoring
    v1_evidence=v1-operations-remaining-recovery v2_evidence=v2-observability-monitoring
    parent_marker=LONGHORN_V1_OPERATIONS_REMAINING_V2_MONITORING
    ;;
  v1-dr-tail-v2-install-tail)
    v1_script=longhorn-parallel.sh
    v1_mode=v1-dr-tail
    v2_script=longhorn-parallel.sh
    v2_mode=v2-install-ready-tail
    v1_evidence=v1-dr-tail v2_evidence=v2-install-ready-tail
    parent_marker=LONGHORN_V1_DR_TAIL_V2_INSTALL_TAIL
    ;;
  v1-upgrade-tail-v2-install-tail)
    v1_script=longhorn-parallel.sh
    v1_mode=v1-upgrade-tail
    v2_script=longhorn-parallel.sh
    v2_mode=v2-install-ready-tail
    v1_evidence=v1-upgrade-tail v2_evidence=v2-install-ready-tail
    parent_marker=LONGHORN_V1_UPGRADE_TAIL_V2_INSTALL_TAIL
    ;;
  v1-monitoring-v2-resource-tail)
    v1_script=longhorn-parallel.sh
    v1_mode=v1-monitoring-tail
    v2_script=longhorn-parallel.sh
    v2_mode=v2-resource-tail
    v1_evidence=v1-monitoring-tail v2_evidence=v2-resource-tail
    parent_marker=LONGHORN_V1_MONITORING_V2_RESOURCE_TAIL
    ;;
  v1-upgrade-tail-v2-maintenance-tail)
    v1_script=longhorn-parallel.sh
    v1_mode=v1-upgrade-tail
    v2_script=longhorn-parallel.sh
    v2_mode=v2-maintenance-tail
    v1_evidence=v1-upgrade-tail v2_evidence=v2-maintenance-tail
    parent_marker=LONGHORN_V1_UPGRADE_TAIL_V2_MAINTENANCE_TAIL
    ;;
  v1-recovery-tail-v2-install-tail)
    v1_script=longhorn-parallel.sh
    v1_mode=v1-recovery-tail
    v2_script=longhorn-parallel.sh
    v2_mode=v2-ready-tail
    v1_evidence=v1-recovery-tail v2_evidence=v2-ready-tail
    parent_marker=LONGHORN_V1_RECOVERY_TAIL_V2_INSTALL_TAIL
    ;;
  v1-system-restore-v2-runtime-resources)
    # Source V1 plus independent target .149; V2 owns only .146-.148.
    v1_script=longhorn-live.sh
    v1_mode=system-restore
    v2_script=longhorn-live.sh
    v2_mode=v2-runtime-resources
    v1_evidence=system-restore v2_evidence=v2-runtime-resources
    parent_marker=LONGHORN_V1_SYSTEM_RESTORE_V2_RUNTIME_RESOURCES
    ;;
  v1-stability-v2-observability)
    v1_script=longhorn-v1-stability.sh
    v1_mode=stability
    v2_script=longhorn-v2-observability.sh
    v2_mode=v2-observability
    v1_evidence=v1-stability v2_evidence=v2-observability
    parent_marker=LONGHORN_V1_STABILITY_V2_OBSERVABILITY
    ;;
  v1-v2-control-qualification)
    v1_script=longhorn-parallel.sh
    v1_mode=v1
    v2_script=longhorn-v2-interrupt-manager.sh
    v2_mode=v2-interrupt-manager-qualification
    v1_evidence=v1 v2_evidence=v2
    parent_marker=LONGHORN_V1_V2_CONTROL_QUALIFICATION
    ;;
  v1-backups-v2-linked-qualification)
    v1_script=longhorn-parallel.sh
    v1_mode=v1-backups
    v2_script=longhorn-v2-linked-clones.sh
    v2_mode=v2-linked-clone-qualification
    v1_evidence=v1-backups v2_evidence=v2-linked
    parent_marker=LONGHORN_V1_BACKUPS_V2_LINKED_QUALIFICATION
    ;;
  v1-platform-propagation-v2-im-adopt-qualification)
    v1_script=longhorn-platform.sh
    v1_mode=platform-propagation-qualification
    v2_script=longhorn-parallel.sh
    v2_mode=v2-source-target-im-adopt-qualification
    v1_evidence=v1-propagation v2_evidence=v2-adoption
    parent_marker=LONGHORN_V1_PLATFORM_PROPAGATION_V2_IM_ADOPT_QUALIFICATION
    ;;
  v1-remaining-v2-linked-acceptance|v1-backups-v2-linked-acceptance|v1-backups-v2-linked-failure-acceptance)
    v1_script=longhorn-parallel.sh
    v1_mode=v1-backups-remaining
    v2_script=longhorn-v2-linked-acceptance.sh
    v2_mode=v2-linked-acceptance
    v1_evidence=v1-backups-remaining v2_evidence=v2-linked-acceptance
    parent_marker=LONGHORN_V1_REMAINING_V2_LINKED_ACCEPTANCE
    if [[ "$1" == v1-backups-v2-linked-acceptance || "$1" == v1-backups-v2-linked-failure-acceptance ]]; then
      v1_mode=v1-backups
      v1_evidence=v1-backups
      parent_marker=LONGHORN_V1_BACKUPS_V2_LINKED_ACCEPTANCE
      if [[ "$1" == v1-backups-v2-linked-failure-acceptance ]]; then
        v2_mode=v2-linked-failure-acceptance
        v2_evidence=v2-linked-failure-acceptance
        parent_marker=LONGHORN_V1_BACKUPS_V2_LINKED_FAILURE_ACCEPTANCE
      fi
    fi
    ;;
  v1-platform-v2-environment-recovery)
    v1_script=longhorn-platform.sh
    v1_mode=platform
    v2_script=longhorn-parallel.sh
    v2_mode=v2-source-target-environment-recovery
    v1_evidence=v1-platform v2_evidence=v2-environment
    parent_marker=LONGHORN_V1_PLATFORM_V2_ENVIRONMENT_RECOVERY
    ;;
  v2-source-target-environment-recovery)
    v1_script=longhorn-v2-post-reboot-environment.sh
    v2_script=longhorn-v2-post-reboot-environment.sh
    v1_mode=v2-post-reboot-environment-recovery
    v2_mode=v2-dr-target-post-reboot-environment-recovery
    v1_evidence=source-environment v2_evidence=target-environment
    parent_marker=LONGHORN_V2_SOURCE_TARGET_ENVIRONMENT_RECOVERY
    ;;
  v2-source-target-reboot-recovery)
    v1_script=longhorn-v2-resources-reboot.sh
    v2_script=longhorn-v2-resources-reboot.sh
    v1_mode=v2-resources-reboot-recovery
    v2_mode=v2-dr-target-resources-reboot-recovery
    v1_evidence=source-reboot v2_evidence=target-reboot
    parent_marker=LONGHORN_V2_SOURCE_TARGET_REBOOT_RECOVERY
    ;;
  v2-source-target-im-revision-3-adopt-qualification)
    v1_script=longhorn-im-revision-3-adopt.sh
    v2_script=longhorn-im-revision-3-adopt.sh
    v1_mode=v2-im-revision-3-adopt-qualification
    v2_mode=v2-dr-target-im-revision-3-adopt-qualification
    v1_evidence=source-revision-3-adopt v2_evidence=target-revision-3-adopt
    parent_marker=LONGHORN_V2_SOURCE_TARGET_IM_REVISION_3_ADOPT_QUALIFICATION
    for engine in v2 v2-dr-target; do
      "$PY" "$BASE/tests/helpers/longhorn-im-revision-3-adopt.py" preflight --engine "$engine"
    done
    "$PY" "$BASE/tests/helpers/longhorn-im-revision-3-adopt.py" cache --engine v2
    echo LONGHORN_PARALLEL_IM_CACHE_READY
    ;;
  v2-source-target-im-revision-2-adopt-qualification)
    v1_script=longhorn-v2-im-adopt.sh
    v2_script=longhorn-v2-im-adopt.sh
    v1_mode=v2-im-revision-2-adopt-qualification
    v2_mode=v2-dr-target-im-revision-2-adopt-qualification
    v1_evidence=source-revision-2-adopt v2_evidence=target-revision-2-adopt
    parent_marker=LONGHORN_V2_SOURCE_TARGET_IM_REVISION_2_ADOPT_QUALIFICATION
    "$PY" "$BASE/tests/helpers/longhorn-im-revision-adopt.py" cache --engine v2
    echo LONGHORN_PARALLEL_IM_CACHE_READY
    ;;
  v2-source-target-im-adopt-qualification)
    v1_script=longhorn-v2-im-adopt.sh
    v2_script=longhorn-v2-im-adopt.sh
    v1_mode=v2-im-adopt-qualification
    v2_mode=v2-dr-target-im-adopt-qualification
    v1_evidence=source-adopt v2_evidence=target-adopt
    parent_marker=LONGHORN_V2_SOURCE_TARGET_IM_ADOPT_QUALIFICATION
    "$PY" "$BASE/tests/helpers/longhorn-v2-im-adopt.py" cache --engine v2
    echo LONGHORN_PARALLEL_IM_CACHE_READY
    ;;
  v1-v2-im-adopt-qualification)
    v1_script=longhorn-v2-im-adopt.sh
    v1_mode=v1-im-adopt-qualification
    v2_script=longhorn-v2-im-adopt.sh
    v2_mode=v2-im-adopt-qualification
    v1_evidence=v1-adopt v2_evidence=v2-adopt
    parent_marker=LONGHORN_V1_V2_IM_ADOPT_QUALIFICATION
    # Fill the shared immutable registry once. Each child still verifies its
    # own cache before use, without two simultaneous cross-border downloads.
    "$PY" "$BASE/tests/helpers/longhorn-v2-im-adopt.py" cache --engine v2
    echo LONGHORN_PARALLEL_IM_CACHE_READY
    ;;
  *) exit 2 ;;
esac
work="$(mktemp -d /var/tmp/kubeauto-longhorn-parallel.XXXXXX)"
chmod 0700 "$work"
"$PY" "$BASE/tests/helpers/longhorn-parallel.py" context-fingerprint > "$work/control-context.json"
v1_pid= v2_pid=
parallel_exit() {
  local rc=$? child
  trap - EXIT INT TERM
  if [[ "$rc" != 0 ]]; then
    # Only the two process groups created by this parent are signalled. Each
    # original scenario owns recovery; preserve incomplete durable fences.
    for child in "${v1_pid:-}" "${v2_pid:-}"; do
      [[ -z "$child" ]] || kill -TERM -- "-$child" 2>/dev/null || true
    done
    for child in "${v1_pid:-}" "${v2_pid:-}"; do
      [[ -z "$child" ]] || wait "$child" || true
    done
    printf 'rc=%s\n' "$rc" > "$work/first-failure.status"
    echo "LONGHORN_FIRST_FAILURE phase=$parent_marker rc=$rc diagnostics=$work" >&2
  fi
  echo "LONGHORN_PARALLEL_DURABLE_DIRECTORY path=$work"
  exit "$rc"
}
trap parallel_exit EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
# The parent may stream before either background child opens its redirection.
: > "$work/v1.log"
: > "$work/v2.log"
setsid env KUBEAUTO_LOG_DIR="$work/v1-product-logs" \
  bash "$BASE/tests/helpers/run-durable-gate.sh" "$work/v1" LONGHORN_PARALLEL_V1_EXIT \
  bash "$BASE/tests/helpers/$v1_script" "$v1_mode" > "$work/v1.log" 2>&1 &
v1_pid=$!
setsid env KUBEAUTO_LOG_DIR="$work/v2-product-logs" \
  bash "$BASE/tests/helpers/run-durable-gate.sh" "$work/v2" LONGHORN_PARALLEL_V2_EXIT \
  bash "$BASE/tests/helpers/$v2_script" "$v2_mode" \
  > "$work/v2.log" 2>&1 &
v2_pid=$!
echo "LONGHORN_PARALLEL_STARTED v1_pid=$v1_pid v2_pid=$v2_pid directory=$work"
v1_lines=0 v2_lines=0
stream_branches() {
  local branch variable count
  for branch in v1 v2; do
    variable="${branch}_lines"
    count="$(wc -l < "$work/$branch.log")"
    if (( count > ${!variable} )); then
      sed -n "$(( ${!variable} + 1 )),$count p" "$work/$branch.log" | sed "s/^/[longhorn-$branch] /"
      printf -v "$variable" '%s' "$count"
    fi
  done
}
while kill -0 "$v1_pid" 2>/dev/null || kill -0 "$v2_pid" 2>/dev/null; do
  stream_branches
  echo "LONGHORN_PARALLEL_HEARTBEAT directory=$work"
  sleep 15
done
# Wait for both independent scopes. One failed branch does not discard the
# other branch's healthy evidence or start its own next scenario.
v1_rc=0 v2_rc=0
wait "$v1_pid" || v1_rc=$?
wait "$v2_pid" || v2_rc=$?
v1_pid= v2_pid=
stream_branches
# Exact original logs are also exposed once in serial blocks for evidence
# import; streaming lines above are prefixed and cannot collide with markers.
for branch in v1 v2; do
  echo "LONGHORN_PARALLEL_BRANCH_LOG_BEGIN branch=$branch"
  cat "$work/$branch.log"
  echo "LONGHORN_PARALLEL_BRANCH_LOG_END branch=$branch"
done
echo "LONGHORN_PARALLEL_CHILD_RESULTS v1_rc=$v1_rc v2_rc=$v2_rc"
[[ "$v1_rc" == 0 && "$v2_rc" == 0 ]]
"$PY" "$BASE/tests/helpers/longhorn-parallel.py" verify-branch --directory "$work" --branch "$v1_evidence"
"$PY" "$BASE/tests/helpers/longhorn-parallel.py" verify-branch --directory "$work" --branch "$v2_evidence"
"$PY" "$BASE/tests/helpers/longhorn-parallel.py" context-verify --record "$work/control-context.json"
echo "${parent_marker}_SCOPED_CLEAN_PASS"
echo "${parent_marker}_PASS"
