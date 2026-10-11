#!/bin/bash
# Sourced only by focused gates; normal/full/OS gates retain their own boundary.

verify_focused_business_hashes() {
  local diagnostic rc=0
  diagnostic="$(verify_post_fault_hashes CEPH-28 2>&1)" || rc=$?
  # A caller's if/|| context disables Bash errexit inside nested functions.
  # Preserve every explicit assertion failure even if a later hash succeeds.
  if [[ "$rc" != 0 || "$diagnostic" == *CEPH_STAGE_FAIL* ]]; then
    printf '%s\n' "$diagnostic" >&2
    return 1
  fi
}

verify_focused_fault_boundary() {
  local host alias native original current
  [[ -z "$INJECTOR_PID" && -z "$NETEM_IFACE" && -z "$THROTTLE_UNIT" && -z "$RECOVERY_OSD_ID" ]] || {
    fail CEPH-28 test-gate "module still owns an injector, network/IO limit or recovery operation"
    return 1
  }
  if [[ -s "$FAULT_TABLE_FILE" ]]; then
    while IFS='|' read -r host alias native original; do
      [[ "$host" == "$SLOW_HOST" && "$native" =~ ^ceph--[A-Za-z0-9-]+$ ]] || {
        fail CEPH-28 test-gate "unowned baseline fault table"
        return 1
      }
      current="$(ssh_node "$host" "dmsetup table '$native'")" || return 1
      [[ "$current" == "$original" ]] || {
        fail CEPH-28 test-gate "module dm-delay residue remains"
        return 1
      }
    done <"$FAULT_TABLE_FILE"
  fi
  wait_clean || return 1
  ssh_node "$SLOW_HOST" "ps -eo comm,args | awk '\$1 == \"rados\" && / bench / {exit 1}'" || return 1
  ceph_shell ceph orch upgrade status --format json | python3 "$CONTRACT_SCRIPT" upgrade-complete || return 1
  verify_focused_business_hashes || return 1
  python3 "$BASE/tests/helpers/ceph_baseline.py" verify --root "$BASE" --state "$STATE_DIR" || return 1
  echo CEPH_MODULE_CLEAN_VERIFY_PASS
}

load_focused_baseline() {
  [[ -s "$STATE_DIR/focused-baseline.json" ]] || {
    fail CEPH-28 test-gate "missing owned focused baseline"
    return 1
  }
  python3 "$BASE/tests/helpers/ceph_baseline.py" verify --root "$BASE" --state "$STATE_DIR" || return 1
  local -a credentials
  mapfile -t credentials < <("${K[@]}" -n "$TEST_NAMESPACE" get secret rgw-client -o json |
    python3 -c 'import base64,json,sys; from urllib.parse import urlsplit; d=json.load(sys.stdin); u=urlsplit(base64.b64decode(d["data"]["MC_HOST_ceph"]).decode()); assert u.scheme=="http" and u.username and u.password; print(u.username); print(u.password); print(u.netloc.rsplit("@",1)[-1])')
  [[ "${#credentials[@]}" == 3 && "${credentials[2]}" =~ ^192\.168\.122\.(135|40|72|212|165|238):8080$ ]] || {
    fail CEPH-28 test-gate "invalid owned RGW baseline credentials"
    return 1
  }
  RGW_ACCESS="${credentials[0]}"
  RGW_SECRET="${credentials[1]}"
  RGW_ENDPOINT="${credentials[2]}"
  verify_focused_fault_boundary
}

retain_focused_baseline() {
  if [[ ! -e "$STATE_DIR/focused-baseline.json" ]]; then
    wait_clean || return 1
    verify_focused_business_hashes || return 1
    python3 "$BASE/tests/helpers/ceph_baseline.py" create --root "$BASE" --state "$STATE_DIR" || return 1
  fi
  verify_focused_fault_boundary || return 1
  echo CEPH_FOCUSED_BASELINE_RETAINED
}

prepare_or_reuse_focused_environment() {
  if [[ -s "$STATE_DIR/focused-baseline.json" && "$MODE" != --upgrade ]]; then
    # A rejected reuse probe is a diagnostic, not a successful module result.
    if load_focused_baseline >"$STATE_DIR/baseline-reuse.log" 2>&1; then
      cat "$STATE_DIR/baseline-reuse.log"
      echo CEPH_FOCUSED_BASELINE_REUSED
      return 0
    fi
    echo CEPH_FOCUSED_BASELINE_REBUILD_REQUIRED
  fi
  if [[ -e "$STATE_DIR/focused-baseline.json" ]]; then
    [[ -s "$OWNER_FILE" && "$(<"$OWNER_FILE")" == kubeauto-ceph-regression ]] || return 1
    unlink "$STATE_DIR/focused-baseline.json" || return 1
  fi
  prepare_focused_environment
  # Capture the original native LVM tables before any owning fault scenario.
  prepare_slow_osd_context
  ceph_shell ceph config rm global osd_op_complaint_time
  if [[ "$MODE" != --upgrade ]]; then
    retain_focused_baseline
  fi
}

restore_focused_module() {
  local alias name
  if [[ -n "$INJECTOR_PID" ]]; then
    kill "$INJECTOR_PID" 2>/dev/null || true
    wait "$INJECTOR_PID" 2>/dev/null || true
    INJECTOR_PID=
  fi
  clear_netem || return 1
  restore_throttle || return 1
  THROTTLE_UNIT=
  THROTTLE_DEVICE=
  THROTTLE_MAJOR_MINOR=
  if [[ -n "$RECOVERY_OSD_ID" ]]; then
    ceph_shell ceph osd in "$RECOVERY_OSD_ID" || return 1
    RECOVERY_OSD_ID=
  fi
  if [[ -n "$OSD_ID" ]]; then
    ceph_shell ceph orch daemon start "osd.${OSD_ID}" || return 1
    ceph_shell ceph osd in "$OSD_ID" || return 1
  fi
  case "$MODE" in
    --observability|--slow-vs-down) ceph_shell ceph osd unset noout || return 1 ;;
  esac
  for alias in "$SLOW_MAPPER" "$BLUEFS_MAPPER"; do
    mapper_table "$alias" linear 0 || return 1
  done
  ceph_shell ceph config rm global osd_op_complaint_time || return 1
  case "$MODE" in
    --osd-throttle)
      for name in healthy limited recovered; do
        ceph_shell rados -p "$RBD_POOL" cleanup --run-name "kubeauto-throttle-$name" || return 1
      done ;;
    --slow-osd-recovery) ceph_shell rados -p "$RBD_POOL" cleanup --run-name kubeauto-recovery || return 1 ;;
    # These branches may change auth/pools. A failed branch is rebuilt unless
    # its original checkpoint proves those changes have already been removed.
  esac
  verify_focused_fault_boundary
}

focused_failure_cleanup() {
  local rc=$?
  trap - EXIT INT TERM
  ((rc != 0)) || rc=1
  if [[ -s "$STATE_DIR/focused-baseline.json" ]]; then
    if restore_focused_module >"$STATE_DIR/module-recovery.log" 2>&1; then
      cat "$STATE_DIR/module-recovery.log"
      echo CEPH_FOCUSED_BASELINE_RETAINED_AFTER_FAILURE
      exit "$rc"
    fi
    echo CEPH_FOCUSED_RECOVERY_REBUILD_REQUIRED
  fi
  [[ ! -e "$STATE_DIR/focused-baseline.json" ]] || unlink "$STATE_DIR/focused-baseline.json"
  # Reuse the established FSID-owned fallback when isolation is unproven.
  set +e
  (exit "$rc")
  failure_cleanup
}
