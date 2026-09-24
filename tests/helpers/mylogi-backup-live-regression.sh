#!/usr/bin/env bash
# One-host MyLogiBackupCli regression. The top-level tools runner owns cleanup.
set -Eeuo pipefail

TOOL="${MYLOGI_TOOL:?MYLOGI_TOOL is required}"
IMAGE_REF="${MYLOGI_IMAGE_REF:?MYLOGI_IMAGE_REF is required}"
VERSION="${MYLOGI_VERSION:?MYLOGI_VERSION is required}"
COMPRESSION="${MYLOGI_COMPRESSION:?MYLOGI_COMPRESSION is required}"
HOST_DATA="${MYLOGI_BACKUP_HOST_DIR:-/data/kubeauto-tools/mylogi-backup-${VERSION}}"
RUN_ROOT="${HOST_DATA}/run"
DB_CTN="mylogi-${VERSION//./-}-db"
TOOL_CTN="mylogi-${VERSION//./-}-tool"
RESTORE_CTN="mylogi-${VERSION//./-}-restore"
SIGNAL_CTN="mylogi-${VERSION//./-}-signal"
PASSWORD='MyLogiFixture_9f6e2a'
BACKUP_PASSWORD='MyLogiBackup_7c2d1b'

cleanup() {
  set +e
  docker rm -f "$SIGNAL_CTN" "$RESTORE_CTN" "$TOOL_CTN" "$DB_CTN" >/dev/null 2>&1 || true
  rm -rf "$RUN_ROOT" "$HOST_DATA"
}
trap cleanup EXIT INT TERM
trap 'rc=$?; echo "MYLOGI_LIVE_COMMAND_FAILED line=$LINENO rc=$rc command=$BASH_COMMAND" >&2; exit "$rc"' ERR

fail_expected() {
  local label="$1"
  shift
  if "$@" >"/tmp/mylogi-${VERSION}-negative.out" 2>&1; then
    echo "MYLOGI_EXPECTED_FAILURE_MISSED case=$label" >&2
    cat "/tmp/mylogi-${VERSION}-negative.out" >&2
    return 1
  fi
  rm -f "/tmp/mylogi-${VERSION}-negative.out"
}

docker rm -f "$SIGNAL_CTN" "$RESTORE_CTN" "$TOOL_CTN" "$DB_CTN" >/dev/null 2>&1 || true
rm -rf "$RUN_ROOT" "$HOST_DATA"
mkdir -p "$RUN_ROOT/backup" "$RUN_ROOT/locks"
mount_target="$(findmnt -T "$HOST_DATA" -rn -o TARGET 2>/dev/null || true)"
[[ -n "$mount_target" && "$mount_target" != "/" ]] || {
  echo "MYLOGI_LIVE_BLOCKED_BACKUP_DISK path=$HOST_DATA" >&2
  exit 2
}

docker pull "$IMAGE_REF" >"/tmp/mylogi-${VERSION}-pull.out"
repo_digests="$(docker image inspect "$IMAGE_REF" --format '{{json .RepoDigests}}')"
expected_digest="${IMAGE_REF##*@}"
printf '%s\n' "$repo_digests" | grep -Fq "$expected_digest" || {
  echo "MYLOGI_FIXTURE_DIGEST_MISMATCH image=$IMAGE_REF actual=$repo_digests" >&2
  exit 3
}
echo "MYLOGI_FIXTURE_PROVENANCE version=$VERSION image=$IMAGE_REF"

docker run --rm --entrypoint sh -v "$TOOL:/tmp/MyLogiBackupCli:ro" "$IMAGE_REF" -ec '
  test -x /tmp/MyLogiBackupCli
  command -v mysql >/dev/null
  command -v mysqldump >/dev/null
  test -x /usr/libexec/mysqlsh/mysql_config_editor
  /usr/libexec/mysqlsh/mysql_config_editor --version
  /tmp/MyLogiBackupCli --version
' | grep -Fq 'MyLogiBackupCli 1.0.0'
echo "MYLOGI_FIXTURE_RUNTIME_CONTRACT_PASS version=$VERSION"

docker run -d --name "$DB_CTN" -e MYSQL_ROOT_PASSWORD="$PASSWORD" \
  "$IMAGE_REF" --performance-schema=OFF --server-id=81 --log-bin=mysql-bin \
  --binlog-format=ROW >/dev/null
ready=0
for _ in $(seq 1 90); do
  if docker exec "$DB_CTN" env MYSQL_PWD="$PASSWORD" mysql -uroot -e 'SELECT 1' >/dev/null 2>&1; then
    ready=1
    break
  fi
  sleep 2
done
[[ "$ready" -eq 1 ]] || { docker logs "$DB_CTN" >&2; exit 1; }

docker exec -i "$DB_CTN" env MYSQL_PWD="$PASSWORD" mysql -uroot <<SQL
CREATE DATABASE IF NOT EXISTS app;
CREATE DATABASE IF NOT EXISTS audit;
CREATE DATABASE IF NOT EXISTS excluded_db;
CREATE USER IF NOT EXISTS 'mysql_backup'@'%' IDENTIFIED BY '$BACKUP_PASSWORD';
GRANT SELECT, SHOW VIEW, TRIGGER, EVENT, PROCESS, RELOAD, REPLICATION CLIENT ON *.* TO 'mysql_backup'@'%';
FLUSH PRIVILEGES;
USE app;
CREATE TABLE IF NOT EXISTS blob_data(id INT PRIMARY KEY, payload BLOB, note VARCHAR(128));
REPLACE INTO blob_data VALUES(1, UNHEX('00010203FEFF'), 'blob fixture');
CREATE TABLE IF NOT EXISTS myisam_data(id INT PRIMARY KEY, value VARCHAR(64)) ENGINE=MyISAM;
REPLACE INTO myisam_data VALUES(1, 'non-innodb fixture');
DROP TRIGGER IF EXISTS app.trg_blob;
CREATE TRIGGER app.trg_blob BEFORE INSERT ON app.blob_data FOR EACH ROW SET NEW.note=COALESCE(NEW.note,'triggered');
DROP PROCEDURE IF EXISTS app.fixture_proc;
CREATE PROCEDURE app.fixture_proc() SELECT COUNT(*) FROM app.blob_data;
DROP EVENT IF EXISTS app.fixture_event;
CREATE EVENT app.fixture_event ON SCHEDULE EVERY 1 DAY DO INSERT INTO app.blob_data VALUES(99, UNHEX('00'), 'event');
USE audit;
CREATE TABLE IF NOT EXISTS records(id INT PRIMARY KEY, value VARCHAR(64));
REPLACE INTO records VALUES(1, 'audit fixture');
SQL

docker run -d --name "$TOOL_CTN" --network "container:$DB_CTN" \
  -v "$RUN_ROOT/backup:/backups" -v "$RUN_ROOT/locks:/locks" \
  -v "$TOOL:/tmp/MyLogiBackupCli:ro" \
  "$IMAGE_REF" sleep 3600 >/dev/null

client_path() {
  docker exec "$TOOL_CTN" sh -c "command -v '$1'"
}
MYSQL_BIN="$(client_path mysql)"
MYSQLDUMP_BIN="$(client_path mysqldump)"
EDITOR_BIN=/usr/libexec/mysqlsh/mysql_config_editor
docker exec "$TOOL_CTN" test -x "$EDITOR_BIN"

tool() {
  docker exec -i "$TOOL_CTN" env HOME=/root \
    MYLOGI_MYSQL_BIN="$MYSQL_BIN" MYLOGI_MYSQLDUMP_BIN="$MYSQLDUMP_BIN" \
    MYLOGI_MYSQL_CONFIG_EDITOR="$EDITOR_BIN" MYLOGI_BACKUP_MOUNT=/backups \
    MYLOGI_LOGIN_FILE=/root/.mylogin.cnf MYLOGI_LOCK_FILE=/tmp/mysql_backup.lock \
    MYLOGI_LOG_FILE=/tmp/mysql_backup.log /tmp/MyLogiBackupCli "$@"
}

latest_archive() {
  find "$RUN_ROOT/backup" -type f -name "$1" -printf '%T@ %p\n' | sort -n | tail -n 1 | cut -d' ' -f2-
}

archive_contains() {
  tar -xOf "$1" '*.sql' | awk -v pattern="$2" '$0 ~ pattern { found=1 } END { exit !found }'
}

printf '%s\n' "$BACKUP_PASSWORD" | tool setup-login --host 127.0.0.1 --port 3306 --user mysql_backup
tool preflight
docker exec "$TOOL_CTN" grep -Fq \
  '非 InnoDB 表不受 --single-transaction 一致性保护：app.myisam_data(MyISAM)' \
  /tmp/mysql_backup.log
echo "MYLOGI_NON_INNODB_WARNING_PASS version=$VERSION"
echo "MYLOGI_PREFLIGHT_OK version=$VERSION"

tool backup --database app audit --compression "$COMPRESSION"
archive="$(latest_archive "mysql-selected-*.tar.${COMPRESSION}")"
test -s "$archive"
tar -tf "$archive" | grep -Eq '\.sql$'
tar -tf "$archive" | grep -Eq '\.sql\.sha256$'
archive_contains "$archive" '-- Dump completed'
archive_contains "$archive" 'CHANGE (REPLICATION SOURCE TO|MASTER TO)'
echo "MYLOGI_BACKUP_OK version=$VERSION compression=$COMPRESSION archive=$(basename "$archive")"

tool backup --exclude-database excluded_db --compression gz
excluded_archive="$(latest_archive 'mysql-selected-*.tar.gz')"
archive_contains "$excluded_archive" 'CREATE DATABASE.*app'
if archive_contains "$excluded_archive" 'CREATE DATABASE.*excluded_db'; then
  echo "MYLOGI_EXCLUDE_FALSE_SUCCESS version=$VERSION" >&2
  exit 1
fi
tool backup --compression gz
full_archive="$(latest_archive 'mysql-all-*.tar.gz')"
archive_contains "$full_archive" 'CREATE DATABASE.*app'
if archive_contains "$full_archive" 'CREATE DATABASE.*information_schema'; then
  echo "MYLOGI_SYSTEM_DATABASE_FALSE_SUCCESS version=$VERSION" >&2
  exit 1
fi
echo "MYLOGI_SCOPE_FILTER_PASS version=$VERSION"

for compression in gz bz2 xz; do
  tool backup --database app --compression "$compression"
done
app_archive="$(latest_archive 'mysql-selected-*.tar.xz')"
app_scope="$(basename "$app_archive")"
app_scope="${app_scope#mysql-}"
app_scope="${app_scope%%-20*}"
[[ "$app_scope" == selected-* ]]
for _ in 1 2 3 4; do
  tool backup --database app --compression gz
done
test "$(find "$RUN_ROOT/backup" -type f -name "mysql-${app_scope}-*.tar.*" | wc -l)" -eq 3
test "$(find "$RUN_ROOT/backup" -type f -name "mysql-${app_scope}-*.tar.gz" | wc -l)" -eq 3
echo "MYLOGI_COMPRESSION_RETENTION_PASS version=$VERSION"

docker run -d --name "$RESTORE_CTN" -e MYSQL_ROOT_PASSWORD="$PASSWORD" \
  "$IMAGE_REF" --performance-schema=OFF >/dev/null
restore_ready=0
for _ in $(seq 1 90); do
  if docker exec "$RESTORE_CTN" env MYSQL_PWD="$PASSWORD" mysql -uroot -e 'SELECT 1' >/dev/null 2>&1; then
    restore_ready=1
    break
  fi
  sleep 2
done
[[ "$restore_ready" -eq 1 ]] || { docker logs "$RESTORE_CTN" >&2; exit 1; }
tar -xOf "$archive" '*.sql' |
  docker exec -i "$RESTORE_CTN" env MYSQL_PWD="$PASSWORD" mysql -uroot
docker exec "$RESTORE_CTN" env MYSQL_PWD="$PASSWORD" mysql -uroot -N -e \
  "SELECT HEX(payload),note FROM app.blob_data WHERE id=1" | grep -Fxq $'00010203FEFF\tblob fixture'
docker exec "$RESTORE_CTN" env MYSQL_PWD="$PASSWORD" mysql -uroot -N -e \
  "SELECT COUNT(*) FROM information_schema.routines WHERE routine_schema='app' AND routine_name='fixture_proc'" | grep -Fxq 1
docker exec "$RESTORE_CTN" env MYSQL_PWD="$PASSWORD" mysql -uroot -N -e \
  "SELECT COUNT(*) FROM information_schema.triggers WHERE trigger_schema='app' AND trigger_name='trg_blob'" | grep -Fxq 1
docker exec "$RESTORE_CTN" env MYSQL_PWD="$PASSWORD" mysql -uroot -N -e \
  "SELECT COUNT(*) FROM information_schema.events WHERE event_schema='app' AND event_name='fixture_event'" | grep -Fxq 1
echo "MYLOGI_RESTORE_READBACK_PASS version=$VERSION"

docker exec "$TOOL_CTN" sh -c 'cat > /tmp/fail-mysqldump <<"EOF"
#!/bin/sh
echo injected-mysqldump-failure >&2
exit 42
EOF
chmod 0755 /tmp/fail-mysqldump'
fail_expected mysqldump-failure docker exec -i "$TOOL_CTN" env HOME=/root \
  MYLOGI_MYSQL_BIN="$MYSQL_BIN" MYLOGI_MYSQLDUMP_BIN=/tmp/fail-mysqldump \
  MYLOGI_MYSQL_CONFIG_EDITOR="$EDITOR_BIN" MYLOGI_BACKUP_MOUNT=/backups \
  MYLOGI_LOGIN_FILE=/root/.mylogin.cnf MYLOGI_LOCK_FILE=/tmp/fail.lock \
  MYLOGI_LOG_FILE=/tmp/fail.log /tmp/MyLogiBackupCli backup --database app
test "$(find "$RUN_ROOT/backup" -type f -name '*.partial' | wc -l)" -eq 0

docker exec "$TOOL_CTN" sh -c 'cat > /tmp/no-footer-mysqldump <<"EOF"
#!/bin/sh
for arg in "$@"; do case "$arg" in --result-file=*) out=${arg#--result-file=} ;; esac; done
printf "SELECT 1;\n" > "$out"
EOF
chmod 0755 /tmp/no-footer-mysqldump'
fail_expected missing-footer docker exec -i "$TOOL_CTN" env HOME=/root \
  MYLOGI_MYSQL_BIN="$MYSQL_BIN" MYLOGI_MYSQLDUMP_BIN=/tmp/no-footer-mysqldump \
  MYLOGI_MYSQL_CONFIG_EDITOR="$EDITOR_BIN" MYLOGI_BACKUP_MOUNT=/backups \
  MYLOGI_LOGIN_FILE=/root/.mylogin.cnf MYLOGI_LOCK_FILE=/tmp/footer.lock \
  MYLOGI_LOG_FILE=/tmp/footer.log /tmp/MyLogiBackupCli backup --database app
test "$(find "$RUN_ROOT/backup" -type f -name '*.partial' | wc -l)" -eq 0

fail_expected unmounted-backups docker exec -i "$TOOL_CTN" env HOME=/root \
  MYLOGI_MYSQL_BIN="$MYSQL_BIN" MYLOGI_MYSQLDUMP_BIN="$MYSQLDUMP_BIN" \
  MYLOGI_MYSQL_CONFIG_EDITOR="$EDITOR_BIN" MYLOGI_BACKUP_MOUNT=/tmp/not-mounted \
  MYLOGI_LOGIN_FILE=/root/.mylogin.cnf MYLOGI_LOCK_FILE=/tmp/mount.lock \
  MYLOGI_LOG_FILE=/tmp/mount.log /tmp/MyLogiBackupCli preflight
docker exec "$TOOL_CTN" chmod 0644 /root/.mylogin.cnf
fail_expected login-permission tool preflight
docker exec "$TOOL_CTN" chmod 0600 /root/.mylogin.cnf

docker cp "$TOOL_CTN:/root/.mylogin.cnf" "$RUN_ROOT/signal-login.cnf"
chmod 0600 "$RUN_ROOT/signal-login.cnf"
cat >"$RUN_ROOT/slow-mysqldump" <<EOF
#!/bin/sh
if [ "\$#" -eq 1 ] && [ "\$1" = --version ]; then
  exec "$MYSQLDUMP_BIN" "\$@"
fi
sleep 60
exec "$MYSQLDUMP_BIN" "\$@"
EOF
chmod 0755 "$RUN_ROOT/slow-mysqldump"
docker run -d --name "$SIGNAL_CTN" --network "container:$DB_CTN" \
  -v "$RUN_ROOT/backup:/backups" -v "$RUN_ROOT/locks:/locks" \
  -v "$RUN_ROOT/signal-login.cnf:/tmp/signal-login.cnf:ro" \
  -v "$RUN_ROOT/slow-mysqldump:/tmp/slow-mysqldump:ro" \
  -v "$TOOL:/tmp/MyLogiBackupCli:ro" \
  -e HOME=/root -e MYLOGI_MYSQL_BIN="$MYSQL_BIN" -e MYLOGI_MYSQLDUMP_BIN=/tmp/slow-mysqldump \
  -e MYLOGI_MYSQL_CONFIG_EDITOR="$EDITOR_BIN" -e MYLOGI_BACKUP_MOUNT=/backups \
  -e MYLOGI_LOGIN_FILE=/root/.mylogin.cnf -e MYLOGI_LOCK_FILE=/locks/signal.lock \
  -e MYLOGI_LOG_FILE=/tmp/signal.log --entrypoint sh "$IMAGE_REF" -ec \
  'cp /tmp/signal-login.cnf /root/.mylogin.cnf
  chmod 0600 /root/.mylogin.cnf
  exec /tmp/MyLogiBackupCli backup --database app' >/dev/null
signal_target=""
for _ in $(seq 1 30); do
  signal_target="$(docker logs "$SIGNAL_CTN" 2>&1 | awk '/MYSQL_BACKUP_LOCK_ACQUIRED lock=\/locks\/signal.lock/ { for (i=1; i<=NF; i++) if ($i ~ /^pid=/) { sub(/^pid=/, "", $i); pid=$i } } END { print pid }')"
  if [[ "$signal_target" =~ ^[0-9]+$ ]] && docker exec "$SIGNAL_CTN" sh -c \
      "ls -l /proc/$signal_target/fd 2>/dev/null | grep -q '/locks/signal.lock'"; then
    break
  fi
  sleep 1
done
[[ "$signal_target" =~ ^[0-9]+$ ]] && docker exec "$SIGNAL_CTN" sh -c \
  "ls -l /proc/$signal_target/fd 2>/dev/null | grep -q '/locks/signal.lock'" || {
    echo "MYLOGI_SIGNAL_LOCK_TARGET_MISSING pid=$signal_target" >&2
    docker logs "$SIGNAL_CTN" >&2 || true
    exit 1
  }
lock_inode="$(docker exec "$SIGNAL_CTN" stat -Lc %i /locks/signal.lock)"
docker exec "$SIGNAL_CTN" sh -c "grep -q ':$lock_inode ' /proc/locks"
echo "MYLOGI_LOCK_HELD_PASS version=$VERSION pid=$signal_target inode=$lock_inode"
fail_expected lock-contention docker exec -i "$TOOL_CTN" env HOME=/root \
  MYLOGI_MYSQL_BIN="$MYSQL_BIN" MYLOGI_MYSQLDUMP_BIN="$MYSQLDUMP_BIN" \
  MYLOGI_MYSQL_CONFIG_EDITOR="$EDITOR_BIN" MYLOGI_BACKUP_MOUNT=/backups \
  MYLOGI_LOGIN_FILE=/root/.mylogin.cnf MYLOGI_LOCK_FILE=/locks/signal.lock \
  MYLOGI_LOG_FILE=/tmp/signal.log /tmp/MyLogiBackupCli backup --database app
docker kill --signal TERM "$SIGNAL_CTN" >/dev/null
signal_rc="$(docker wait "$SIGNAL_CTN")"
test "$signal_rc" -ne 0
sleep 1
docker logs "$SIGNAL_CTN" 2>&1 | grep -q 'MYSQL_BACKUP_FAILED error=操作被信号 SIGTERM 终止'
test "$(find "$RUN_ROOT/backup" -type f -name '*.partial' | wc -l)" -eq 0
echo "MYLOGI_SIGNAL_CLEANUP_PASS version=$VERSION"

docker exec "$TOOL_CTN" sh -c 'cat > /tmp/fake-systemctl <<"EOF"
#!/bin/sh
case "$1" in is-enabled|is-active) echo "$1"; exit 0 ;; *) exit 0 ;; esac
EOF
cat > /tmp/fake-analyze <<"EOF"
#!/bin/sh
exit 0
EOF
chmod 0755 /tmp/fake-systemctl /tmp/fake-analyze'
docker exec "$TOOL_CTN" env HOME=/root MYLOGI_SYSTEMCTL=/tmp/fake-systemctl \
  MYLOGI_SYSTEMD_ANALYZE=/tmp/fake-analyze MYLOGI_SYSTEMD_UNIT_DIR=/tmp/systemd \
  /tmp/MyLogiBackupCli timer install --at 01:01 --exec-path /tmp/MyLogiBackupCli --compression gz
docker exec "$TOOL_CTN" grep -q 'OnCalendar=\*-\*-\* 01:01:00' /tmp/systemd/mysql-logi-backup.timer
docker exec "$TOOL_CTN" env HOME=/root MYLOGI_SYSTEMCTL=/tmp/fake-systemctl \
  /tmp/MyLogiBackupCli timer status
docker exec "$TOOL_CTN" env HOME=/root MYLOGI_SYSTEMCTL=/tmp/fake-systemctl \
  MYLOGI_SYSTEMD_UNIT_DIR=/tmp/systemd /tmp/MyLogiBackupCli timer remove
docker exec "$TOOL_CTN" test ! -e /tmp/systemd/mysql-logi-backup.timer
echo "MYLOGI_TIMER_PASS version=$VERSION"

docker exec "$TOOL_CTN" grep -q 'MYSQL_BACKUP_OK' /tmp/mysql_backup.log
echo "MYLOGI_LIVE_REGRESSION_PASS version=$VERSION compression=all restore=readback failure=cleanup timer=ok"
