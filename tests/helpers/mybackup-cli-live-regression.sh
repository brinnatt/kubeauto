#!/usr/bin/env bash
set -Eeuo pipefail
TOOL="${MYBACKUP_TOOL:?MYBACKUP_TOOL is required}"
PYTHON_BIN="${MYBACKUP_PYTHON:-python3}"
ROOT=/tmp/tools-mbk-live
MYSQL_CTN=tools-mbk-mysql
PXB_IMAGE="${MYBACKUP_PXB_IMAGE:-hub.talkedu.cn/kubeauto/percona-xtrabackup:8.4.0-5.1}"
MYSQL_IMAGE="${MYBACKUP_MYSQL_IMAGE:-hub.talkedu.cn/kubeauto/mysql-8.4:8.4.4}"
PXB_DIGEST="${MYBACKUP_PXB_DIGEST:-sha256:add39f4f46a5f6712527d0cedff99b85f9867339d557acf2796563413b3865ea}"
MYSQL_DIGEST="${MYBACKUP_MYSQL_DIGEST:-sha256:0a3e659b9fb960330299e2a1847414f6185c573a3fd2cf1320221066904ea77d}"
PASSWORD='ToolPass_8_4'; CONFIG="$ROOT/backup.json"; MARKER='mybackup_fixture'
cleanup() {
  set +e
  # Preserve only this run's scoped product log for the caller; never merge a
  # host-level /var/log/mysqlbackup.log from an earlier gate into evidence.
  cp -f "$ROOT/mysqlbackup.log" /tmp/tools-mybackup-file.log 2>/dev/null || true
  docker rm -f "$MYSQL_CTN" >/dev/null 2>&1 || true
  rm -rf "$ROOT"
}
trap cleanup EXIT INT TERM
docker rm -f "$MYSQL_CTN" >/dev/null 2>&1 || true; rm -rf "$ROOT"; mkdir -p "$ROOT/mysql" "$ROOT/bin"; chown 999:999 "$ROOT/mysql"
pxb_repo_digests="$(docker image inspect "$PXB_IMAGE" --format '{{json .RepoDigests}}')"
mysql_repo_digests="$(docker image inspect "$MYSQL_IMAGE" --format '{{json .RepoDigests}}')"
[[ "$pxb_repo_digests" == *"hub.talkedu.cn/kubeauto/percona-xtrabackup@$PXB_DIGEST"* ]] || { echo "MYBACKUP_FIXTURE_DIGEST_MISMATCH kind=pxb expected=$PXB_DIGEST actual=$pxb_repo_digests" >&2; exit 3; }
[[ "$mysql_repo_digests" == *"hub.talkedu.cn/kubeauto/mysql-8.4@$MYSQL_DIGEST"* ]] || { echo "MYBACKUP_FIXTURE_DIGEST_MISMATCH kind=mysql expected=$MYSQL_DIGEST actual=$mysql_repo_digests" >&2; exit 3; }
echo "MYBACKUP_FIXTURE_PROVENANCE pxb=$PXB_IMAGE@$PXB_DIGEST mysql=$MYSQL_IMAGE@$MYSQL_DIGEST"
cat >"$ROOT/bin/mysql" <<'EOF'
#!/usr/bin/env bash
set -Eeuo pipefail
if [[ -f /tmp/tools-mbk-live/mysql-version.override ]] && printf '%s\n' "$*" | grep -q 'SELECT VERSION'; then cat /tmp/tools-mbk-live/mysql-version.override; exit 0; fi
args=(); for arg in "$@"; do if [[ "$arg" == --socket=* ]]; then args+=(--socket=/var/run/mysqld/mysqld.sock); else args+=("$arg"); fi; done
exec docker exec -i tools-mbk-mysql mysql "${args[@]}"
EOF
cat >"$ROOT/bin/xtrabackup" <<EOF
#!/usr/bin/env bash
set -Eeuo pipefail
if [[ "\${MYBACKUP_INJECT_ENOSPC:-0}" == 1 && " \$* " == *" --backup "* ]]; then
  echo 'No space left on device (injected fixture failure)' >&2
  exit 28
fi
if [[ "\${MYBACKUP_INJECT_SIGINT:-0}" == 1 && " \$* " == *" --prepare "* ]]; then
  # Deliver the interrupt from the real XtraBackup child to the Python CLI
  # parent, avoiding shell job-control differences in remote fixtures.
  kill -INT "\$PPID"
  exit 130
fi
args=(); for arg in "\$@"; do if [[ "\$arg" == --socket=* ]]; then args+=(--host=127.0.0.1 --port=3306); else args+=("\$arg"); fi; done
exec docker run --rm --user 0 --network container:tools-mbk-mysql -v '$ROOT:$ROOT' -v '$ROOT/mysql:/var/lib/mysql' '$PXB_IMAGE' xtrabackup "\${args[@]}"
EOF
cat >"$ROOT/bin/mysqlbinlog" <<EOF
#!/usr/bin/env bash
set -Eeuo pipefail
exec docker run --rm --user 0 -v '$ROOT:$ROOT' -v '$ROOT/mysql:/var/lib/mysql' -v '$ROOT/mysql:/var/run/mysqld' '$PXB_IMAGE' mysqlbinlog "\$@"
EOF
cat >"$ROOT/bin/chown" <<EOF
#!/usr/bin/env bash
set -Eeuo pipefail
if [[ "\$*" == *"$ROOT/mysql"* ]]; then exec docker run --rm --user 0 -v '$ROOT/mysql:/var/lib/mysql' --entrypoint chown '$PXB_IMAGE' -R 999:999 /var/lib/mysql; fi
exec /usr/bin/chown "\$@"
EOF
cat >"$ROOT/bin/systemctl" <<'EOF'
#!/usr/bin/env bash
set -Eeuo pipefail
state=/tmp/tools-mbk-live/mysql.state
action="${1:-}"
case "$action" in
  status) [[ -f "$state" ]] && cat "$state" || echo inactive; exit 0 ;;
  is-active) [[ -f "$state" ]] && cat "$state" || echo inactive; [[ -f "$state" ]] && [[ "$(cat "$state")" == active ]] && exit 0 || exit 3 ;;
  # Keep the fixture container alive so Docker can execute PXB helper
  # containers while the product believes the service is stopped.
  stop) echo inactive >"$state"; exit 0 ;;
  start|restart) docker start tools-mbk-mysql >/dev/null; echo active >"$state"; exit 0 ;;
  *) exit 1 ;;
esac
EOF
chmod 0755 "$ROOT/bin"/*; export PATH="$ROOT/bin:$PATH"
docker run -d --name "$MYSQL_CTN" -e MYSQL_ROOT_PASSWORD="$PASSWORD" -v "$ROOT/mysql:/var/lib/mysql" -v "$ROOT/mysql:/var/run/mysqld" "$MYSQL_IMAGE" --innodb-buffer-pool-size=64M --performance-schema=OFF --server-id=84 --log-bin=mysql-bin --binlog-format=ROW >/dev/null
echo active >"$ROOT/mysql.state"
for _ in $(seq 1 90); do mysql --user=root --password="$PASSWORD" --socket="$ROOT/mysql/mysql.sock" -e 'SELECT 1' >/dev/null 2>&1 && break; sleep 2; done
mysql --user=root --password="$PASSWORD" --socket="$ROOT/mysql/mysql.sock" -e 'SELECT 1' >/dev/null
mysql --user=root --password="$PASSWORD" --socket="$ROOT/mysql/mysql.sock" <<SQL
CREATE DATABASE IF NOT EXISTS $MARKER;
CREATE TABLE IF NOT EXISTS $MARKER.items(id INT PRIMARY KEY, payload VARCHAR(128));
INSERT INTO $MARKER.items VALUES (1, 'before-full'), (2, 'before-full-2');
FLUSH LOGS;
SQL
cat >"$CONFIG" <<JSON
{"backup_base":"$ROOT/backup","mysql_user":"root","mysql_password":"$PASSWORD","mysql_socket":"$ROOT/mysql/mysql.sock","mysql_service":"mysqld","mysql_datadir":"$ROOT/mysql","mysql_binlog_prefix":"mysql-bin","xtrabackup_parallel":1,"xtrabackup_compress_threads":1,"xtrabackup_use_memory":"32M","xtrabackup_lock_ddl":"AUTO"}
JSON
chmod 0600 "$CONFIG"
export MYSQLBACKUP_LOG_FILE="$ROOT/mysqlbackup.log"
run_tool() { if [[ "$TOOL" == *.py ]]; then "$PYTHON_BIN" "$TOOL" "$@"; else "$TOOL" "$@"; fi; }
echo MYBACKUP_STAGE full
export MYBACKUP_INJECT_ENOSPC=1
if run_tool full -c "$CONFIG" >/tmp/mybackup-enospc.out 2>&1; then
  echo MYBACKUP_ENOSPC_FALSE_SUCCESS >&2
  exit 1
fi
unset MYBACKUP_INJECT_ENOSPC
if find "$ROOT/backup/full" -type f -name .backup_ok -print -quit | grep -q .; then
  echo MYBACKUP_ENOSPC_LEFT_SUCCESS_MARKER >&2
  exit 1
fi
rm -f /tmp/mybackup-enospc.out
echo MYBACKUP_ENOSPC_RECOVERY_PASS
run_tool full -c "$CONFIG"
mysql --user=root --password="$PASSWORD" --socket="$ROOT/mysql/mysql.sock" -e "INSERT INTO $MARKER.items VALUES (3, 'after-full'); FLUSH LOGS;"
echo MYBACKUP_STAGE incr; run_tool incr -c "$CONFIG"
echo MYBACKUP_STAGE binlog; run_tool binlog -c "$CONFIG"
echo MYBACKUP_STAGE checksum; run_tool checksum -c "$CONFIG"
echo MYBACKUP_STAGE restore-dry-run; run_tool restore-dry-run -c "$CONFIG"
echo MYBACKUP_STAGE purge; run_tool purge -c "$CONFIG" --backup-days 15 --binlog-days 30
if run_tool restore -c "$CONFIG" --binlog-start-time '2026-01-01 00:00:00' >/dev/null 2>&1; then echo MYBACKUP_RESTORE_CONFIRMATION_BYPASS >&2; exit 1; fi
echo MYBACKUP_STAGE restore-interrupt
export MYBACKUP_INJECT_SIGINT=1
run_tool restore -c "$CONFIG" --confirm-destructive-restore --binlog-start-time '2099-01-01 00:00:00' >"$ROOT/restore.sigint.log" 2>&1 &
sig_pid=$!
set +e
wait "$sig_pid"
sig_rc=$?
set -e
unset MYBACKUP_INJECT_SIGINT
sleep 5
[[ "$sig_rc" -ne 0 ]] || { echo MYBACKUP_SIGINT_FALSE_SUCCESS >&2; exit 1; }
[[ "$(systemctl is-active mysqld 2>/dev/null || true)" == active ]] || { echo MYBACKUP_SIGINT_SERVICE_NOT_RECOVERED >&2; cat "$ROOT/restore.sigint.log" >&2; exit 1; }
echo MYBACKUP_SIGINT_RECOVERY_PASS
echo MYBACKUP_STAGE restore; run_tool restore -c "$CONFIG" --confirm-destructive-restore --binlog-start-time '2099-01-01 00:00:00'
mysql --user=root --password="$PASSWORD" --socket="$ROOT/mysql/mysql.sock" -N -e "SELECT COUNT(*) FROM $MARKER.items" | grep -Fxq '3'; echo MYBACKUP_RESTORE_READBACK_PASS
( flock -n 9 || exit 1; sleep 5 ) 9>"$ROOT/backup/lock/mysqlbackup.lock" & lock_pid=$!; sleep 1
if run_tool checksum -c "$CONFIG" >/dev/null 2>&1; then echo MYBACKUP_LOCK_CONFLICT_FALSE_SUCCESS >&2; kill "$lock_pid" 2>/dev/null || true; exit 1; fi
wait "$lock_pid"; echo MYBACKUP_LOCK_CONFLICT_PASS
printf '9.2.0\n' >"$ROOT/mysql-version.override"
if run_tool checksum -c "$CONFIG" >/tmp/mybackup-9x.out 2>&1; then echo MYBACKUP_9X_FALSE_SUCCESS >&2; exit 1; fi
rm -f "$ROOT/mysql-version.override" /tmp/mybackup-9x.out; echo MYBACKUP_9X_REJECTION_PASS
echo "MYBACKUP_CLI_LIVE_REGRESSION_PASS full=incr=binlog=checksum=prepare=restore=purge enospc=sigint lock=9x"
