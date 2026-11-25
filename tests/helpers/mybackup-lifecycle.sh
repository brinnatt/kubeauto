#!/usr/bin/env bash
# Sourced only by mybackup-cli-live-regression.sh after its owned fixture is ready.
# Database clients seed/observe data; every backup and restore uses the frozen CLI.
query() { mysql --user=root --password="$PASSWORD" --socket="$ROOT/mysql/mysql.sock" --batch --skip-column-names "$@"; }
mysql_running() { [[ "$(docker inspect --format '{{.State.Running}}' "$MYSQL_CTN")" == true ]]; }
snapshot() {
  query -e "SELECT id,HEX(payload) FROM $MARKER.items ORDER BY id;
    SELECT id,amount,HEX(payload),HEX(bytes),CAST(doc AS CHAR) FROM $MARKER.ledger ORDER BY id;"
}
check_lsn_chain() {
  local checkpoint from_lsn to_lsn previous_lsn=''
  for checkpoint in "$@"; do
    from_lsn="$(awk '$1=="from_lsn" {print $3}' "$checkpoint")"
    to_lsn="$(awk '$1=="to_lsn" {print $3}' "$checkpoint")"
    [[ "$from_lsn" == "${previous_lsn:-0}" && "$to_lsn" =~ ^[0-9]+$ ]]
    previous_lsn="$to_lsn"
  done
}
refuse() {
  local name="$1"; shift
  if run_tool "$@" >"$ROOT/$name.refusal.log" 2>&1; then
    echo "MYBACKUP_LIFECYCLE_FALSE_SUCCESS case=$name" >&2; exit 1
  fi
  mysql_running
  snapshot >"$ROOT/$name.actual"
  cmp "$ROOT/healthy.expected" "$ROOT/$name.actual"
  echo "MYBACKUP_REFUSAL_PASS case=$name"
}
query <<SQL
USE $MARKER;
SET NAMES utf8mb4;
CREATE TABLE $MARKER.ledger(id INT PRIMARY KEY,amount DECIMAL(20,6),payload VARCHAR(100) CHARACTER SET utf8mb4,bytes BLOB,doc JSON) ENGINE=InnoDB;
CREATE TABLE digits(n INT);
INSERT INTO digits VALUES(0),(1),(2),(3),(4),(5),(6),(7),(8),(9);
INSERT INTO $MARKER.ledger SELECT a.n+10*b.n+100*c.n,123456.123456+a.n,CONCAT('物理恢复-',a.n,':',b.n,':',c.n),UNHEX('0001FF7F'),JSON_OBJECT('n',a.n,'ok',TRUE) FROM digits a,digits b,digits c;
DROP TABLE digits;
SQL
snapshot >"$ROOT/full.expected"
echo "MYBACKUP_LIFECYCLE_STAGE version=$EXPECTED_VERSION gtid=$GTID step=full-only"
export MYBACKUP_INJECT_ENOSPC=1
if run_tool full -c "$CONFIG" >"$ROOT/enospc.refusal.log" 2>&1; then
  echo MYBACKUP_ENOSPC_FALSE_SUCCESS >&2; exit 1
fi
unset MYBACKUP_INJECT_ENOSPC
[[ -z "$(find "$ROOT/backup/full" -name .backup_ok -print -quit)" ]]
echo MYBACKUP_ENOSPC_RECOVERY_PASS
query -e "START TRANSACTION; INSERT INTO $MARKER.items VALUES(777,'uncommitted'); SELECT SLEEP(120); ROLLBACK;" >"$ROOT/transaction.log" 2>&1 &
transaction_pid=$!
transaction_id=''
for _ in {1..30}; do
  transaction_id="$(query -e "SELECT ID FROM information_schema.PROCESSLIST WHERE INFO='SELECT SLEEP(120)' AND ID<>CONNECTION_ID()")"
  [[ "$transaction_id" =~ ^[0-9]+$ ]] && break
  sleep 1
done
[[ "$transaction_id" =~ ^[0-9]+$ ]]
run_tool full -c "$CONFIG" --native-option xtrabackup-backup=--throttle=100
query -e "KILL $transaction_id"
wait "$transaction_pid" || true
query -e 'FLUSH LOGS;'
run_tool binlog -c "$CONFIG"
run_tool checksum -c "$CONFIG"
run_tool restore-dry-run -c "$CONFIG"
query -e "DELETE FROM $MARKER.items; DELETE FROM $MARKER.ledger;"
[[ "$(query -e "SELECT COUNT(*) FROM $MARKER.ledger")" == 0 ]]
run_tool restore -c "$CONFIG" --confirm-destructive-restore --binlog-start-time '2099-01-01 00:00:00'
snapshot >"$ROOT/full.actual"
cmp "$ROOT/full.expected" "$ROOT/full.actual"
[[ "$(query -e "SELECT COUNT(*) FROM $MARKER.items WHERE id=777")" == 0 ]]
echo "MYBACKUP_UNCOMMITTED_ROLLBACK_PASS version=$EXPECTED_VERSION"
echo "MYBACKUP_FULL_RESTORE_PASS version=$EXPECTED_VERSION rows=1002"

# Use a separate chain: prepare mutates backups, and full-only evidence must not
# be mistaken for a second restoration of the same prepared incremental chain.
sed -i 's@/backup"@/backup-chain"@' "$CONFIG"
echo "MYBACKUP_LIFECYCLE_STAGE version=$EXPECTED_VERSION gtid=$GTID step=two-incrementals"
run_tool full -c "$CONFIG"
query -e "INSERT INTO $MARKER.items VALUES(3,'incremental-one'); UPDATE $MARKER.ledger SET amount=amount+1 WHERE id=1;"
run_tool incr -c "$CONFIG"
sleep 1
query -e "INSERT INTO $MARKER.items VALUES(4,'incremental-two'); UPDATE $MARKER.ledger SET bytes=UNHEX('FF00FF') WHERE id=2; DELETE FROM $MARKER.ledger WHERE id=3;"
run_tool incr -c "$CONFIG"
snapshot >"$ROOT/chain.expected"
mapfile -t checkpoint_files < <(find "$ROOT/backup-chain" -name xtrabackup_checkpoints -type f | sort)
[[ "${#checkpoint_files[@]}" == 3 ]]
check_lsn_chain "${checkpoint_files[@]}"
for checkpoint in "${checkpoint_files[@]}"; do
  printf '\n%s\n' "$checkpoint" >>"$ROOT/lsn-chain.txt"
  cat "$checkpoint" >>"$ROOT/lsn-chain.txt"
  find "$(dirname "$checkpoint")" -maxdepth 1 -name 'xtrabackup_binlog_info*' -exec sha256sum {} + >>"$ROOT/lsn-chain.txt"
done
echo "MYBACKUP_INCREMENTAL_LSN_CHAIN_PASS version=$EXPECTED_VERSION increments=2"

# Separate the snapshot, wanted transaction and excluded transaction by actual
# server seconds. The stop boundary is exclusive (mysqlbinlog semantics).
sleep 2
pitr_start="$(query -e "SELECT DATE_FORMAT(NOW(),'%Y-%m-%d %H:%i:%s')")"
query -e "INSERT INTO $MARKER.items VALUES(5,'pitr-include'); UPDATE $MARKER.ledger SET amount=999999.999999 WHERE id=4;"
snapshot >"$ROOT/pitr.expected"
sleep 2
pitr_stop="$(query -e "SELECT DATE_FORMAT(NOW(),'%Y-%m-%d %H:%i:%s')")"
printf 'start=%s\nstop=%s\nserver_time_zone=%s\n' "$pitr_start" "$pitr_stop" "$(query -e 'SELECT @@session.time_zone')" >"$ROOT/binlog-boundary.txt"
query -e "INSERT INTO $MARKER.items VALUES(6,'pitr-exclude'); UPDATE $MARKER.ledger SET amount=-1 WHERE id=4; FLUSH LOGS;"
run_tool binlog -c "$CONFIG"
run_tool checksum -c "$CONFIG"
snapshot >"$ROOT/healthy.expected"
refuse unconfirmed restore -c "$CONFIG" --binlog-start-time "$pitr_start" --binlog-stop-time "$pitr_stop"
refuse no-plan restore -c "$CONFIG" --confirm-destructive-restore --binlog-start-time "$pitr_start"
run_tool restore-dry-run -c "$CONFIG"

# The prepare interrupt must preserve the live source and release the CLI lock.
export MYBACKUP_INJECT_SIGINT=1
run_tool restore -c "$CONFIG" --confirm-destructive-restore --binlog-start-time "$pitr_start" >"$ROOT/restore.sigint.log" 2>&1 &
interrupt_pid=$!
if wait "$interrupt_pid"; then echo MYBACKUP_SIGINT_FALSE_SUCCESS >&2; exit 1; fi
unset MYBACKUP_INJECT_SIGINT
mysql_running
snapshot >"$ROOT/after-interrupt.actual"
cmp "$ROOT/healthy.expected" "$ROOT/after-interrupt.actual"
echo MYBACKUP_SIGINT_RECOVERY_PASS
run_tool checksum -c "$CONFIG"
query -e "DELETE FROM $MARKER.items; DELETE FROM $MARKER.ledger;"
[[ "$(query -e "SELECT COUNT(*) FROM $MARKER.items")" == 0 ]]
echo "MYBACKUP_LIFECYCLE_STAGE version=$EXPECTED_VERSION gtid=$GTID step=prepare-copyback-pitr"
run_tool restore -c "$CONFIG" --confirm-destructive-restore --binlog-start-time "$pitr_start" --binlog-stop-time "$pitr_stop"
snapshot >"$ROOT/pitr.actual"
cmp "$ROOT/pitr.expected" "$ROOT/pitr.actual"
[[ "$(query -e "SELECT COUNT(*) FROM $MARKER.items WHERE id=5")" == 1 ]]
[[ "$(query -e "SELECT COUNT(*) FROM $MARKER.items WHERE id=6")" == 0 ]]
echo "MYBACKUP_PITR_BOUNDARY_PASS version=$EXPECTED_VERSION include=5 exclude=6 start=$pitr_start stop=$pitr_stop"
echo "MYBACKUP_RESTORE_READBACK_PASS version=$EXPECTED_VERSION"

( flock -n 9 || exit 1; sleep 3 ) 9>"$ROOT/backup-chain/lock/mysqlbackup.lock" &
lock_pid=$!; sleep 1
if run_tool checksum -c "$CONFIG" >"$ROOT/lock.refusal.log" 2>&1; then echo MYBACKUP_LOCK_CONFLICT_FALSE_SUCCESS >&2; exit 1; fi
wait "$lock_pid"
run_tool checksum -c "$CONFIG"
echo MYBACKUP_LOCK_CONFLICT_PASS

# A second full backup starts a new chain in the same configured repository.
# This is the normal scheduled lifecycle, including multiple backups per day.
echo "MYBACKUP_LIFECYCLE_STAGE version=$EXPECTED_VERSION gtid=$GTID step=new-full-cycle"
query -e "INSERT INTO $MARKER.items VALUES(7,'next-full');"
run_tool full -c "$CONFIG"
next_full="$(readlink -f "$ROOT/backup-chain/full/latest_raw")"
query -e "INSERT INTO $MARKER.items VALUES(8,'next-incremental');"
run_tool incr -c "$CONFIG"
next_incremental="$(readlink -f "$ROOT/backup-chain/incr/latest")"
check_lsn_chain "$next_full/xtrabackup_checkpoints" "$next_incremental/xtrabackup_checkpoints"
query -e "INSERT INTO $MARKER.items VALUES(9,'new-cycle-pitr');"
snapshot >"$ROOT/next-cycle.expected"
query -e 'FLUSH LOGS;'
run_tool binlog -c "$CONFIG"
run_tool restore-dry-run -c "$CONFIG"
query -e "DELETE FROM $MARKER.items; DELETE FROM $MARKER.ledger;"
run_tool restore -c "$CONFIG" --confirm-destructive-restore --binlog-start-time '1970-01-01 00:00:00'
snapshot >"$ROOT/next-cycle.actual"
cmp "$ROOT/next-cycle.expected" "$ROOT/next-cycle.actual"
echo "MYBACKUP_NEW_FULL_CYCLE_PASS version=$EXPECTED_VERSION"
mkdir -p "$ROOT/backup-chain/full/2000-01-01"
touch -d '2000-01-01' "$ROOT/backup-chain/full/2000-01-01"
run_tool purge -c "$CONFIG" --backup-days 15 --binlog-days 30
test ! -e "$ROOT/backup-chain/full/2000-01-01"
test -e "$ROOT/backup-chain/full/latest_raw"
test -e "$ROOT/backup-chain/incr/latest"
echo "MYBACKUP_RETENTION_PASS version=$EXPECTED_VERSION"
grep -q -- '--throttle=100' "$ROOT/mysqlbackup.log"
grep -q -- '--use-memory=64M' "$ROOT/mysqlbackup.log"
grep -q -- '--lock-ddl=ON' "$ROOT/mysqlbackup.log"
echo MYBACKUP_NATIVE_BACKUP_PASS
echo MYBACKUP_NATIVE_PREPARE_PASS
echo "MYBACKUP_LIFECYCLE_PASS version=$EXPECTED_VERSION gtid=$GTID mode=$EXECUTION_MODE full=incr2=decompress=prepare=copyback=pitr=retention enospc=sigint=refusal=lock"
