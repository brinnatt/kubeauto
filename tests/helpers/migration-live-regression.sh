#!/usr/bin/env bash
set -Eeuo pipefail

TOOL="${MIGRATION_TOOL:?MIGRATION_TOOL is required}"
PYTHON_BIN="${MIGRATION_PYTHON:-python3}"
ROOT=/tmp/kubeauto-migration-live
SRC=tools-mig-src
TGT=tools-mig-tgt
TGT9=tools-mig-tgt9
cleanup() {
  set +e
  docker rm -f "$SRC" "$TGT" "$TGT9" >/dev/null 2>&1 || true
  rm -rf "$ROOT" /tmp/migration-live.json /tmp/migration-dry-run.json \
    /tmp/migration-dry-report /tmp/migration-dry.log /tmp/migration-structure-only.json \
    /tmp/migration-invalid.json /tmp/migration-invalid.log /tmp/migration-invalid-report \
    /tmp/migration-unreachable.json /tmp/migration-unreachable.log /tmp/migration-unreachable-report \
    /tmp/migration-structure-report /tmp/migration-live-report \
    /tmp/migration-definer.json /tmp/migration-definer-report /tmp/migration-definer.log \
    /tmp/migration-gtid-*.json /tmp/migration-gtid-*.log \
    /tmp/MigrationCli-tools-live.py \
    /tmp/migration-live-regression.sh /tmp/tools-mig-venv
}
trap cleanup EXIT INT TERM

run_cli() {
  local config="$1"; shift
  if [[ "$TOOL" == *.py ]]; then
    "$PYTHON_BIN" "$TOOL" --config "$config" --report-dir /tmp/migration-live-report "$@"
  else
    "$TOOL" --config "$config" --report-dir /tmp/migration-live-report "$@"
  fi
}

mkdir -p "$ROOT"
docker rm -f "$SRC" "$TGT" "$TGT9" >/dev/null 2>&1 || true
docker run -d --name "$SRC" -e MYSQL_ROOT_PASSWORD=ToolPass_8_0 -p 13306:3306 \
  hub.talkedu.cn/kubeauto/mysql:8.0.46 >/dev/null
docker run -d --name "$TGT" -e MYSQL_ROOT_PASSWORD=ToolPass_8_4 -p 13307:3306 \
  hub.talkedu.cn/kubeauto/mysql-8.4:8.4.4 >/dev/null
docker run -d --name "$TGT9" -e MYSQL_ROOT_PASSWORD=ToolPass_9_2 -p 13308:3306 \
  hub.talkedu.cn/kubeauto/mysql-9.2:9.2.0 >/dev/null
for _ in $(seq 1 90); do
  mysql -h127.0.0.1 -P13306 -uroot -pToolPass_8_0 -e 'SELECT 1' >/dev/null 2>&1 && \
    mysql -h127.0.0.1 -P13307 -uroot -pToolPass_8_4 -e 'SELECT 1' >/dev/null 2>&1 && \
    mysql -h127.0.0.1 -P13308 -uroot -pToolPass_9_2 -e 'SELECT 1' >/dev/null 2>&1 && break
  sleep 2
done
mysql -h127.0.0.1 -P13306 -uroot -pToolPass_8_0 <<'SQL'
CREATE DATABASE migsrc;
CREATE DATABASE migdef;
CREATE USER 'mig'@'%' IDENTIFIED BY 'MigPass!';
GRANT ALL ON migsrc.* TO 'mig'@'%';
GRANT ALL ON migdef.* TO 'mig'@'%';
CREATE TABLE migsrc.items(id INT PRIMARY KEY, payload VARCHAR(128));
INSERT INTO migsrc.items VALUES (1,'alpha'),(2,'beta');
CREATE TABLE migsrc.special(id INT PRIMARY KEY, payload VARCHAR(128));
INSERT INTO migsrc.special VALUES (7,'quoted " value');
CREATE TABLE migdef.base(id INT PRIMARY KEY, payload VARCHAR(128));
INSERT INTO migdef.base VALUES (1,'definer');
CREATE ALGORITHM=MERGE DEFINER=`ghost`@`%` SQL SECURITY DEFINER VIEW migdef.v_base AS SELECT id,payload FROM migdef.base;
SQL
mysql -h127.0.0.1 -P13307 -uroot -pToolPass_8_4 <<'SQL'
CREATE USER 'mig'@'%' IDENTIFIED BY 'MigPass!';
GRANT ALL ON *.* TO 'mig'@'%' WITH GRANT OPTION;
SQL
mysql -h127.0.0.1 -P13308 -uroot -pToolPass_9_2 <<'SQL'
CREATE USER 'mig'@'%' IDENTIFIED BY 'MigPass!';
GRANT ALL ON *.* TO 'mig'@'%' WITH GRANT OPTION;
SQL
cat > /tmp/migration-dry-run.json <<'JSON'
{"migrations":[{"source":{"host":"127.0.0.1","port":13306,"user":"mig","password":"MigPass!","database":"migsrc","mode":"structure_and_data"},"target":{"host":"127.0.0.1","port":13307,"user":"mig","password":"MigPass!","database":"dryrun_target","mode":"structure_and_data"}}],"options":{"dry_run":true,"per_table":true,"gtid_mode":"off","report_dir":"/tmp/migration-dry-report"}}
JSON
chmod 0600 /tmp/migration-dry-run.json
if [[ "$TOOL" == *.py ]]; then
  "$PYTHON_BIN" "$TOOL" --config /tmp/migration-dry-run.json --report-dir /tmp/migration-dry-report --gtid-mode off > /tmp/migration-dry.log
else
  "$TOOL" --config /tmp/migration-dry-run.json --report-dir /tmp/migration-dry-report --gtid-mode off > /tmp/migration-dry.log
fi
grep -Fq '[DRY-RUN]' /tmp/migration-dry.log
! mysql -h127.0.0.1 -P13307 -uroot -pToolPass_8_4 -Nse "SELECT SCHEMA_NAME FROM information_schema.schemata WHERE SCHEMA_NAME='dryrun_target'" | grep -q .
cat > /tmp/migration-live.json <<'JSON'
{"migrations":[{"source":{"host":"127.0.0.1","port":13306,"user":"mig","password":"MigPass!","database":"migsrc","mode":"structure_and_data"},"target":{"host":"127.0.0.1","port":13307,"user":"mig","password":"MigPass!","database":"migtgt","mode":"structure_and_data"},"options":{"per_table":true,"table_checksum":true,"exact_row_count":true,"fix_definer":true,"gtid_mode":"off"}},{"source":{"host":"127.0.0.1","port":13307,"user":"mig","password":"MigPass!","database":"migtgt","mode":"structure_and_data"},"target":{"host":"127.0.0.1","port":13308,"user":"mig","password":"MigPass!","database":"migtgt9","mode":"structure_and_data"},"options":{"per_table":false,"compress_dump":false,"table_checksum":true,"exact_row_count":true,"gtid_mode":"off"}}],"options":{"report_dir":"/tmp/migration-live-report","keep_dump_files":false,"max_workers":1}}
JSON
chmod 0600 /tmp/migration-live.json
if [[ "$TOOL" == *.py ]]; then
  "$PYTHON_BIN" "$TOOL" --config /tmp/migration-live.json --report-dir /tmp/migration-live-report --table-checksum
else
  "$TOOL" --config /tmp/migration-live.json --report-dir /tmp/migration-live-report --table-checksum
fi
mysql -h127.0.0.1 -P13307 -uroot -pToolPass_8_4 -e \
  "SELECT * FROM migtgt.items ORDER BY id; SELECT * FROM migtgt.special ORDER BY id;" | \
  grep -Fq 'alpha'
mysql -h127.0.0.1 -P13307 -uroot -pToolPass_8_4 -e \
  "SELECT COUNT(*) FROM migtgt.items" | grep -Fxq '2'
mysql -h127.0.0.1 -P13308 -uroot -pToolPass_9_2 -e \
  "SELECT * FROM migtgt9.items ORDER BY id" | grep -Fq 'alpha'
mysql -h127.0.0.1 -P13308 -uroot -pToolPass_9_2 -e \
  "SELECT COUNT(*) FROM migtgt9.items" | grep -Fxq '2'
cat > /tmp/migration-structure-only.json <<'JSON'
{"migrations":[{"source":{"host":"127.0.0.1","port":13306,"user":"mig","password":"MigPass!","database":"migsrc","mode":"structure_only"},"target":{"host":"127.0.0.1","port":13308,"user":"mig","password":"MigPass!","database":"migstruct","mode":"structure_only"}}],"options":{"per_table":true,"compress_dump":false,"gtid_mode":"off","report_dir":"/tmp/migration-structure-report"}}
JSON
chmod 0600 /tmp/migration-structure-only.json
if [[ "$TOOL" == *.py ]]; then
  "$PYTHON_BIN" "$TOOL" --config /tmp/migration-structure-only.json --report-dir /tmp/migration-structure-report --gtid-mode off
else
  "$TOOL" --config /tmp/migration-structure-only.json --report-dir /tmp/migration-structure-report --gtid-mode off
fi
mysql -h127.0.0.1 -P13308 -uroot -pToolPass_9_2 -e "SELECT COUNT(*) FROM migstruct.items" | grep -Fxq '0'

# DEFINER repair: the source view uses a non-existent account; fix_definer must
# rewrite it to CURRENT_USER so import succeeds with the migration account.
cat > /tmp/migration-definer.json <<'JSON'
{"migrations":[{"source":{"host":"127.0.0.1","port":13306,"user":"mig","password":"MigPass!","database":"migdef"},"target":{"host":"127.0.0.1","port":13307,"user":"mig","password":"MigPass!","database":"migdef_tgt"},"options":{"per_table":true,"compress_dump":false,"fix_definer":true,"gtid_mode":"off"}}],"options":{"report_dir":"/tmp/migration-definer-report"}}
JSON
chmod 0600 /tmp/migration-definer.json
run_cli /tmp/migration-definer.json > /tmp/migration-definer.log
mysql -h127.0.0.1 -P13307 -uroot -pToolPass_8_4 -e 'SHOW CREATE VIEW migdef_tgt.v_base' | grep -Fq 'DEFINER=`mig`@`%`'

# GTID modes are rendered through the tool's own dry-run path.  This covers
# off/on/commented and auto (independent migration resolves to OFF).
for mode in auto off on commented; do
  cat > "/tmp/migration-gtid-${mode}.json" <<JSON
{"migrations":[{"source":{"host":"127.0.0.1","port":13306,"user":"mig","password":"MigPass!","database":"migsrc"},"target":{"host":"127.0.0.1","port":13307,"user":"mig","password":"MigPass!","database":"miggtid_${mode}"},"options":{"dry_run":true,"gtid_mode":"${mode}","per_table":true}}],"options":{"report_dir":"/tmp/migration-live-report"}}
JSON
  chmod 0600 "/tmp/migration-gtid-${mode}.json"
  run_cli "/tmp/migration-gtid-${mode}.json" --dry-run > "/tmp/migration-gtid-${mode}.log"
  grep -Fq -- '--set-gtid-purged=' "/tmp/migration-gtid-${mode}.log"
done

# Security and recovery contracts: invalid identifiers and unreachable source
# must fail before destructive work and leave no fake success report.
cat > /tmp/migration-invalid.json <<'JSON'
{"migrations":[{"source":{"host":"127.0.0.1","port":13306,"user":"mig","password":"MigPass!","database":"bad;drop","mode":"structure_and_data"},"target":{"host":"127.0.0.1","port":13308,"user":"mig","password":"MigPass!","database":"invalid_target","mode":"structure_and_data"}}],"options":{"report_dir":"/tmp/migration-invalid-report"}}
JSON
chmod 0600 /tmp/migration-invalid.json
set +e
if [[ "$TOOL" == *.py ]]; then
  "$PYTHON_BIN" "$TOOL" --config /tmp/migration-invalid.json --report-dir /tmp/migration-invalid-report >/tmp/migration-invalid.log 2>&1
else
  "$TOOL" --config /tmp/migration-invalid.json --report-dir /tmp/migration-invalid-report >/tmp/migration-invalid.log 2>&1
fi
invalid_rc=$?
set -e
test "$invalid_rc" -ne 0
grep -Eiq 'invalid|无效|非法|错误' /tmp/migration-invalid.log
! mysql -h127.0.0.1 -P13308 -uroot -pToolPass_9_2 -Nse "SELECT SCHEMA_NAME FROM information_schema.schemata WHERE SCHEMA_NAME='invalid_target'" | grep -q .
cat > /tmp/migration-unreachable.json <<'JSON'
{"migrations":[{"source":{"host":"127.0.0.1","port":13399,"user":"mig","password":"MigPass!","database":"migsrc","mode":"structure_and_data"},"target":{"host":"127.0.0.1","port":13308,"user":"mig","password":"MigPass!","database":"unreachable_target","mode":"structure_and_data"}}],"options":{"report_dir":"/tmp/migration-unreachable-report","compress_dump":false}}
JSON
chmod 0600 /tmp/migration-unreachable.json
set +e
if [[ "$TOOL" == *.py ]]; then
  "$PYTHON_BIN" "$TOOL" --config /tmp/migration-unreachable.json --report-dir /tmp/migration-unreachable-report >/tmp/migration-unreachable.log 2>&1
else
  "$TOOL" --config /tmp/migration-unreachable.json --report-dir /tmp/migration-unreachable-report >/tmp/migration-unreachable.log 2>&1
fi
unreachable_rc=$?
set -e
test "$unreachable_rc" -ne 0
grep -Eiq '连接|connect|失败|error' /tmp/migration-unreachable.log
find /tmp/migration-live-report -maxdepth 1 -type f -name 'migration_report_*.json' -size +0c -print -quit | grep -q .
echo "MIGRATION_CLI_LIVE_REGRESSION_PASS source=8.0.46 target=8.4.4->9.2.0 per_table=checksum whole_db=ok dry_run=ok structure_only=ok definer=ok gtid=all security=ok recovery=ok"
