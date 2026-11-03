#!/usr/bin/env bash
# One fixed full route, using existing live modules, leases and durable receipts.
set -Eeuo pipefail
umask 077
BASE="$(cd "$(dirname "$0")/../.." && pwd)"
PY="$BASE/.venv/bin/python"
EVIDENCE="$BASE/tests/helpers/longhorn-full-evidence.py"
cd "$BASE"
export PYTHONPATH="$BASE" PATH="$BASE/kube-bin:$BASE/extra-bin:$PATH"
MODE="${1:?mode required}"

stage() {
  local mode="$1" rc=0 tee_rc=0
  local prefix="$work/stages/$mode"
  "$PY" "$EVIDENCE" check-inputs
  if [[ "${LONGHORN_FULL_RESUME:-no}" == yes && -f "$work/accepted/$mode.json" ]]; then
    "$PY" "$BASE/tests/helpers/longhorn-full-stage-verify.py" "$mode" >/dev/null
    echo "LONGHORN_FULL_STAGE_REUSED stage=$mode"
    return 0
  fi
  if [[ -e "$prefix.log" || -e "$prefix.exit" || -e "$prefix.finalized" ]]; then
    local previous
    mkdir -p "$work/failed-attempts"
    previous="$(mktemp -d "$work/failed-attempts/$mode.XXXXXXXX")"
    for suffix in log exit finalized pid; do
      [[ ! -e "$prefix.$suffix" ]] || mv -- "$prefix.$suffix" "$previous/"
    done
  fi
  echo "LONGHORN_FULL_STAGE_START stage=$mode"
  set +e
  bash "$BASE/tests/helpers/run-durable-gate.sh" "$prefix" LONGHORN_FULL_CHILD_EXIT \
    bash "$BASE/tests/helpers/longhorn-full.sh" child "$mode" 2>&1 | tee "$prefix.log"
  rc=${PIPESTATUS[0]} tee_rc=${PIPESTATUS[1]}
  set -e
  [[ "$rc" == 0 && "$tee_rc" == 0 ]] || return 1
  "$PY" "$EVIDENCE" accept "$mode" --prefix "$prefix"
}

case "$MODE" in
  local)
    work="${2:?new private work directory required}"
    [[ "$work" == "$BASE"/logs/longhorn-full/run.* && -d "$work" && ! -L "$work" ]]
    mkdir -m 0700 "$work/preflight" "$work/evidence"
    "$PY" "$EVIDENCE" freeze --directory "$work"
    for mode in static disabled artifacts documentation; do
      prefix="$work/preflight/$mode"
      # These original gates require the development-side sibling artifact Git
      # repository. Their original durable bytes travel with this one run.
      bash "$BASE/tests/helpers/run-durable-gate.sh" "$prefix" LONGHORN_FULL_CHILD_EXIT \
        bash "$BASE/tests/helpers/longhorn-full.sh" local-child "$mode" 2>&1 | tee "$prefix.log"
    done
    "$PY" "$EVIDENCE" verify-preflight --directory "$work"
    echo "LONGHORN_FULL_LOCAL_PREFLIGHT_PASS directory=$work"
    ;;
  local-child)
    case "${2:?stage required}" in
      static) exec bash "$BASE/tests/helpers/longhorn-static.sh" ;;
      disabled)
        "$PY" -m unittest tests.unit.test_longhorn_delivery.InputContracts.test_disabled_cli_never_uses_cluster_or_helm -v
        echo LONGHORN_DISABLED_SCOPED_CLEAN_PASS
        echo LONGHORN_DISABLED_PASS
        ;;
      artifacts) exec "$PY" "$BASE/tests/helpers/longhorn-artifacts.py" ;;
      documentation) exec "$PY" "$BASE/tests/helpers/longhorn-documentation.py" ;;
      *) exit 2 ;;
    esac
    ;;
  child)
    [[ "${LONGHORN_PARENT_LEASE_HELD:-no}" == yes ]]
    if [[ "${2:?stage required}" == v2-cleanup-target && "${LONGHORN_FULL_RESUME:-no}" == yes ]]; then
      journal="$("$PY" "$EVIDENCE" retirement-resume)"
      if [[ -n "$journal" ]]; then
        export LONGHORN_V2_RETIREMENT_JOURNAL="$journal"
        exec bash "$BASE/tests/helpers/longhorn-live.sh" v2-cleanup-target-recovery
      fi
      exec bash "$BASE/tests/helpers/longhorn-live.sh" "$2"
    elif [[ "$2" == v2-clone ]]; then
      exec "$PY" "$EVIDENCE" clone-accept
    elif [[ "$2" == final-clean ]]; then
      # Product retirement already released both V2 scopes and V1 uninstall
      # restored its filesystems. Only the owned V1 Kubernetes core remains.
      "$PY" "$BASE/tests/helpers/longhorn-lab.py" verify-core
      bash "$BASE/tests/helpers/lab-wipe-nodes.sh" --rocky-only \
        192.168.122.135 192.168.122.40 192.168.122.72 \
        192.168.122.212 192.168.122.165 192.168.122.238
      bash "$BASE/tests/helpers/lab-wipe-nodes.sh" --verify --rocky-only \
        192.168.122.135 192.168.122.40 192.168.122.72 \
        192.168.122.212 192.168.122.165 192.168.122.238 \
        192.168.47.146 192.168.47.147 192.168.47.148 192.168.47.149
      "$PY" "$BASE/tests/helpers/longhorn-lab.py" archive-core
      for name in longhorn-gate longhorn-v2-gate longhorn-v2-dr-target longhorn-node-gate; do
        [[ ! -e "$BASE/clusters/$name" && ! -L "$BASE/clusters/$name" ]]
      done
      "$PY" "$BASE/tests/helpers/longhorn-parallel.py" context-verify \
        --record "${LONGHORN_FULL_EVIDENCE_DIR:?}/../control-context.json"
      echo LONGHORN_FULL_FINAL_CLEAN_PASS
    else
      exec bash "$BASE/tests/helpers/longhorn-live.sh" "$2"
    fi
    ;;
  full)
    [[ "${LONGHORN_PARENT_LEASE_HELD:-no}" == yes ]]
    for entry in '9 kubeauto-ceph-priority.lock' '7 kubeauto-ceph-compute.lock' '8 kubeauto-redis-core-pool.lock'; do
      read -r fd filename <<< "$entry"
      [[ "/proc/self/fd/$fd" -ef "/var/lock/$filename" ]]
      flock -n "$fd"
    done
    work="${LONGHORN_FULL_EVIDENCE_DIR:?}/.."
    work="$(cd "$work" && pwd)"
    if [[ "${LONGHORN_FULL_RESUME:-no}" == yes ]]; then
      [[ -d "$work/stages" && -d "$work/accepted" && -f "$work/frozen-inputs.json" ]]
      "$PY" "$EVIDENCE" verify-preflight --directory "$work"
    else
      "$PY" "$EVIDENCE" init
    fi
    phase=preflight
    full_exit() {
      local rc=$?
      trap - EXIT INT TERM
      if [[ "$rc" != 0 ]]; then
        if [[ ! -e "$work/first-failure.status" ]]; then
          printf 'rc=%s phase=%s\n' "$rc" "$phase" > "$work/first-failure.status"
        fi
        echo "LONGHORN_FIRST_FAILURE phase=$phase rc=$rc diagnostics=$work"
      fi
      echo "LONGHORN_FULL_DURABLE_DIRECTORY path=$work"
      exit "$rc"
    }
    trap full_exit EXIT
    trap 'exit 130' INT
    trap 'exit 143' TERM
    if [[ "${LONGHORN_FULL_RESUME:-no}" == yes ]]; then
      "$PY" "$BASE/tests/helpers/longhorn-parallel.py" context-verify --record "$work/control-context.json"
    else
      "$PY" "$BASE/tests/helpers/longhorn-parallel.py" context-fingerprint > "$work/control-context.json"
    fi
    if [[ "${LONGHORN_FULL_RESUME:-no}" == yes ]]; then
      # A resumed run may retain runner-owned cluster inputs from the accepted
      # core/recovery stages.  Later stage readers bind their owner journals,
      # identities and cleanup receipts; rejecting the directories here would
      # discard a valid continuation before those checks can run.
      for name in longhorn-gate longhorn-v2-gate longhorn-v2-dr-target longhorn-node-gate; do
        if [[ -e "$BASE/clusters/$name" || -L "$BASE/clusters/$name" ]]; then
          [[ -d "$BASE/clusters/$name" && ! -L "$BASE/clusters/$name" ]]
        fi
      done
    else
      for name in longhorn-gate longhorn-v2-gate longhorn-v2-dr-target longhorn-node-gate; do
        [[ ! -e "$BASE/clusters/$name" && ! -L "$BASE/clusters/$name" ]]
      done
    fi
    if [[ "${LONGHORN_FULL_RESUME:-no}" == yes ]]; then
      if [[ -f "$work/accepted/core.json" ]]; then
        "$PY" "$EVIDENCE" resume-baseline
      else
        # Before V1 core installation, the independent V1 pool must be clean.
        bash "$BASE/tests/helpers/lab-wipe-nodes.sh" --verify --rocky-only \
          192.168.122.135 192.168.122.40 192.168.122.72 \
          192.168.122.212 192.168.122.165 192.168.122.238
      fi
    else
      bash "$BASE/tests/helpers/lab-wipe-nodes.sh" --verify --rocky-only \
        192.168.122.135 192.168.122.40 192.168.122.72 \
        192.168.122.212 192.168.122.165 192.168.122.238 \
        192.168.47.146 192.168.47.147 192.168.47.148 192.168.47.149
    fi
    for mode in static disabled artifacts documentation; do
      if [[ "${LONGHORN_FULL_RESUME:-no}" == yes && -f "$work/accepted/$mode.json" ]]; then
        "$PY" "$BASE/tests/helpers/longhorn-full-stage-verify.py" "$mode" >/dev/null
        echo "LONGHORN_FULL_STAGE_REUSED stage=$mode"
      else
        "$PY" "$EVIDENCE" accept "$mode" --prefix "$work/preflight/$mode"
      fi
    done
    # Fixed dependencies only. Original parallel parents supervise disjoint
    # V1/V2 scopes. Restore/upgrade borrowing .149 wait for V2 protection;
    # unchanged accepted full-copy scopes are consumed once by canonical49.
    for mode in v2-core v2-host-qualification v2-resources-qualification \
        v2-aio-install-qualification v2-dr-target-core v2-dr-target-install-qualification \
        core core-smoke disk-qualification v1-ownership-v2-linked-acceptance \
        platform v1-operations-v2-platform-install idempotence rwo rwx reclaim expansion snapshot clone \
        v2-volumes v2-backup-store-qualification v2-recurring-selector-qualification \
        v2-dr-store-qualification v2-dr-standby-qualification v2-dr-activation-qualification \
        v1-backup-v2-data-protection system-restore disaster-recovery v1-upgrade-lifecycle \
        v2-interrupt-qualification v2-failure v2-upgrade-lifecycle v2-clone \
        replica-scale replica-failure engine-failure \
        trim encryption security network-policy \
        observability performance v1-stability-v2-observability v2-middleware cleanup cleanup-qualification \
        v2-cleanup-target; do
      phase="$mode"
      stage "$mode"
    done
    # Only finalized release receipts authorize reuse within the V2 subnet.
    export LONGHORN_V2_TARGET_RELEASE_PROOF
    LONGHORN_V2_TARGET_RELEASE_PROOF="$("$PY" "$EVIDENCE" release-proof target 192.168.47.149)"
    for mode in v2-disk-lifecycle v2-cleanup; do
      phase="$mode"
      stage "$mode"
    done
    export LONGHORN_V2_RELEASE_PROOF
    LONGHORN_V2_RELEASE_PROOF="$("$PY" "$EVIDENCE" release-proof source 192.168.47.148)"
    for mode in node-lifecycle uninstall final-clean; do
      phase="$mode"
      stage "$mode"
    done
    phase=finalize
    "$PY" "$EVIDENCE" finalize
    ;;
  *) exit 2 ;;
esac
