#!/usr/bin/env bash
set -Eeuo pipefail

TOOL="${MIGRATION_TOOL:?MIGRATION_TOOL is required}"
PYTHON_BIN="${MIGRATION_PYTHON:-python3}"
STAGE="${MIGRATION_STAGE:-full}"
[[ "$STAGE" == native || "$STAGE" == full ]]
ROOT=/tmp/kubeauto-migration-live
RELEASE=/tmp/kubeauto-migration-release
exec 8>/run/lock/kubeauto-mysql-backup-release.lock
flock -n 8 || { echo MIGRATION_HOST_LEASE_BUSY >&2; exit 2; }
exec 9>/run/lock/kubeauto-tools-migration.lock
flock -n 9 || { echo MIGRATION_HOST_LEASE_BUSY >&2; exit 2; }
SRC=tools-mig-src
TGT=tools-mig-tgt
TGT9=tools-mig-tgt9
CLIENT80=tools-mig-client80
CLIENT84=tools-mig-client84
CLIENT92=tools-mig-client92
export CLIENT80 CLIENT84 CLIENT92
# Refuse unowned fixtures before installing any destructive cleanup.
if docker ps -a --format '{{.Names}}' | grep -Eq '^tools-mig-(src|tgt|tgt9|client80|client84|client92|portability-.*)$'; then echo MIGRATION_ASSERTION_FAILED_check22 >&2; exit 1; fi
test ! -e "$ROOT"
for fixture in /tmp/migration-{live,dry-run,structure-only,definer,invalid,unreachable}.json /tmp/migration-*-report /tmp/migration-native-trace.jsonl; do
  test ! -e "$fixture"
done
for port in 13306 13307 13308; do
  if ss -H -ltn "sport = :$port" | grep -q .; then echo MIGRATION_ASSERTION_FAILED_check28 >&2; exit 1; fi
done
mkdir -p "$RELEASE/evidence"
cleanup() {
  local rc=$?
  trap - EXIT
  set +e
  cp /tmp/migration-native-trace.jsonl "$RELEASE/evidence/" 2>/dev/null
  cp /tmp/migration-*.log "$RELEASE/evidence/" 2>/dev/null
  for report in /tmp/migration-*-report; do
    [[ ! -d "$report" ]] || cp -a "$report" "$RELEASE/evidence/"
  done
  docker logs "$SRC" >"$RELEASE/evidence/source-server.log" 2>&1
  docker logs "$TGT" >"$RELEASE/evidence/target-server.log" 2>&1
  docker rm -f "$SRC" "$TGT" "$TGT9" "$CLIENT80" "$CLIENT84" "$CLIENT92" tools-mig-portability-rocky8 tools-mig-portability-debian12 tools-mig-portability-ubuntu24 >/dev/null 2>&1 || true
  rm -rf "$ROOT" /tmp/migration-live.json /tmp/migration-dry-run.json \
    /tmp/migration-dry-report /tmp/migration-dry.log /tmp/migration-structure-only.json \
    /tmp/migration-invalid.json /tmp/migration-invalid.log /tmp/migration-invalid-report \
    /tmp/migration-unreachable.json /tmp/migration-unreachable.log /tmp/migration-unreachable-report \
    /tmp/migration-structure-report /tmp/migration-live-report \
    /tmp/migration-definer.json /tmp/migration-definer-report /tmp/migration-definer.log \
    /tmp/migration-gtid-*.json /tmp/migration-gtid-*.log \
    /tmp/MigrationCli-tools-live.py \
    /tmp/migration-live-regression.sh /tmp/tools-mig-venv /tmp/migration-native-trace.jsonl /tmp/migration-native-rejected.log /tmp/migration-native-failure.log /tmp/migration-native-import-failure.log /tmp/migration-native-retry.log /tmp/migration-native-concurrent.log
  if docker ps -a --format '{{.Names}}' | grep -Eq '^tools-mig-(src|tgt|tgt9|client80|client84|client92|portability-.*)$' || test -e "$ROOT"; then
    echo MIGRATION_CLEANUP_FAILED >&2
    rc=1
  fi
  tar -czf "$RELEASE/release-evidence.tar.gz" -C "$RELEASE" evidence
  chmod 0644 "$RELEASE/release-evidence.tar.gz"
  if [[ "$rc" -eq 0 ]]; then echo "TOOLS_CLEAN_VERIFY_PASS scope=migration stage=$STAGE"; fi
  exit "$rc"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

# The same frozen binaries must start and reject unsafe options on all runtimes.
for runtime in rocky8 debian12 ubuntu24; do
  case "$runtime" in
    rocky8) image='docker.sparkcr.cn/rockylinux/rockylinux@sha256:e8a49c5403b687db05d4d67333fa45808fbe74f36e683cec7abb1f7d0f2338c6';;
    debian12) image='swr.cn-north-4.myhuaweicloud.com/ddn-k8s/docker.io/library/debian@sha256:9df39a1d5bfac0249f0eef61a3ff74fcf7576c5947f23127c57e92267ad98ced';;
    ubuntu24) image='swr.cn-north-4.myhuaweicloud.com/ddn-k8s/docker.io/library/ubuntu@sha256:b0c08a4b639b5fca9aa4943ecec614fe241a0cebd1a7b460093ccaeae70df698';;
  esac
  docker run --rm --name "tools-mig-portability-$runtime" -v "$RELEASE:/release:ro" --entrypoint /bin/sh "$image" -ec '
    for tool in MyBackupCli MyLogiBackupCli MigrationCli; do
      /release/$tool-rocky8 --help >/dev/null
      /release/$tool-rocky8 --help-native > /tmp/native-help
      grep -q -- --ignore-error /tmp/native-help
      case "$tool" in
        MyBackupCli) set -- full --native-option mysql=--ignore-error=1146;;
        MyLogiBackupCli) set -- backup --native-option mysqldump=--ignore-error=1146;;
        MigrationCli) set -- --native-option mysql=--ignore-error=1146;;
      esac
      if /release/$tool-rocky8 "$@" >/tmp/rejected 2>&1; then exit 1; fi
      grep -q -- --help-native /tmp/rejected
    done
  '
  echo "MYSQLTOOLS_NATIVE_PORTABILITY_PASS runtime=$runtime tools=3"
done

run_cli() {
  local config="$1"; shift
  if [[ "$TOOL" == *.py ]]; then
    "$PYTHON_BIN" "$TOOL" --config "$config" --report-dir /tmp/migration-live-report "$@"
  else
    "$TOOL" --config "$config" --report-dir /tmp/migration-live-report "$@"
  fi
}

mysql_cli() {
  "$ROOT/bin/mysql" "$@"
}

export MYSQL_MIGRATION_LOG_DIR="$RELEASE/evidence/product-logs"
mkdir -p "$ROOT/bin"
docker run -d --memory=640m --name "$SRC" -e MYSQL_ROOT_PASSWORD=ToolPass_8_0 -p 127.0.0.1:13306:3306 \
  hub.talkedu.cn/kubeauto/mysql@sha256:2f27838ce14a31d6e434efb442658c4ce19a7cd0ec834e329ca213569faa7d3c --innodb-buffer-pool-size=64M --performance-schema=OFF --max-connections=30 >/dev/null
docker run -d --memory=640m --name "$TGT" -e MYSQL_ROOT_PASSWORD=ToolPass_8_4 -p 127.0.0.1:13307:3306 \
  hub.talkedu.cn/kubeauto/mysql-8.4@sha256:c26ba5d7363cdae3f0a31665b2ab9106397324dde56ed536364776901b924b83 --innodb-buffer-pool-size=64M --performance-schema=OFF --max-connections=30 >/dev/null
docker run -d --memory=640m --name "$TGT9" -e MYSQL_ROOT_PASSWORD=ToolPass_9_2 -p 127.0.0.1:13308:3306 \
  hub.talkedu.cn/kubeauto/mysql-9.2@sha256:308515a860be3b21aa44ced3d39f7f91d800efe7ff3719c249f19a89a8480740 --innodb-buffer-pool-size=64M --performance-schema=OFF --max-connections=30 >/dev/null
docker run -d --name "$CLIENT80" --network host -v /tmp:/tmp \
  --entrypoint sleep hub.talkedu.cn/kubeauto/mysql@sha256:2f27838ce14a31d6e434efb442658c4ce19a7cd0ec834e329ca213569faa7d3c 3600 >/dev/null
docker run -d --name "$CLIENT84" --network host -v /tmp:/tmp \
  --entrypoint sleep hub.talkedu.cn/kubeauto/mysql-8.4@sha256:c26ba5d7363cdae3f0a31665b2ab9106397324dde56ed536364776901b924b83 3600 >/dev/null
docker run -d --name "$CLIENT92" --network host -v /tmp:/tmp \
  --entrypoint sleep hub.talkedu.cn/kubeauto/mysql-9.2@sha256:308515a860be3b21aa44ced3d39f7f91d800efe7ff3719c249f19a89a8480740 3600 >/dev/null
cat >"$ROOT/bin/mysql" <<'EOF'
#!/usr/bin/env bash
set -Eeuo pipefail
container="$CLIENT92"
for arg in "$@"; do
  case "$arg" in
    -P13306) container="$CLIENT80" ;;
    -P13307) container="$CLIENT84" ;;
    -P13308) container="$CLIENT92" ;;
  esac
done
for arg in "$@"; do
  if [[ "$arg" == --defaults-extra-file=* ]]; then
    python3 - "$0" "$@" <<'PYTRACE'
import json, sys
with open('/tmp/migration-native-trace.jsonl', 'a') as output:
    output.write(json.dumps(sys.argv[1:])+'\n')
PYTRACE
    break
  fi
done
exec docker exec -i "$container" mysql "$@"
EOF
cat >"$ROOT/bin/mysqldump" <<'EOF'
#!/usr/bin/env bash
set -Eeuo pipefail
container="$CLIENT92"
for arg in "$@"; do
  case "$arg" in
    -P13306) container="$CLIENT80" ;;
    -P13307) container="$CLIENT84" ;;
    -P13308) container="$CLIENT92" ;;
  esac
done
for arg in "$@"; do
  if [[ "$arg" == --defaults-extra-file=* ]]; then
    python3 - "$0" "$@" <<'PYTRACE'
import json, sys
with open('/tmp/migration-native-trace.jsonl', 'a') as output:
    output.write(json.dumps(sys.argv[1:])+'\n')
PYTRACE
    break
  fi
done
exec docker exec -i "$container" mysqldump "$@"
EOF
chmod 0755 "$ROOT/bin/mysql" "$ROOT/bin/mysqldump"
export PATH="$ROOT/bin:$PATH"
for _ in $(seq 1 90); do
  mysql_cli -h127.0.0.1 -P13306 -uroot -pToolPass_8_0 -e 'SELECT 1' >/dev/null 2>&1 && \
    mysql_cli -h127.0.0.1 -P13307 -uroot -pToolPass_8_4 -e 'SELECT 1' >/dev/null 2>&1 && \
    mysql_cli -h127.0.0.1 -P13308 -uroot -pToolPass_9_2 -e 'SELECT 1' >/dev/null 2>&1 && break
  sleep 2
done
mysql_cli -h127.0.0.1 -P13306 -uroot -pToolPass_8_0 <<'SQL'
CREATE DATABASE migsrc;
CREATE DATABASE migdef;
CREATE USER 'mig'@'%' IDENTIFIED BY 'MigPass!';
GRANT ALL ON migsrc.* TO 'mig'@'%';
GRANT ALL ON migdef.* TO 'mig'@'%';
GRANT PROCESS ON *.* TO 'mig'@'%';
CREATE TABLE migsrc.items(id INT PRIMARY KEY, payload VARCHAR(128));
INSERT INTO migsrc.items VALUES (1,'alpha'),(2,'beta');
CREATE TABLE migsrc.special(id INT PRIMARY KEY, payload VARCHAR(128));
INSERT INTO migsrc.special VALUES (7,'quoted " value');
CREATE TABLE migdef.base(id INT PRIMARY KEY, payload VARCHAR(128));
INSERT INTO migdef.base VALUES (1,'definer');
CREATE ALGORITHM=MERGE DEFINER=`ghost`@`%` SQL SECURITY DEFINER VIEW migdef.v_base AS SELECT id,payload FROM migdef.base;
SQL
mysql_cli -h127.0.0.1 -P13307 -uroot -pToolPass_8_4 <<'SQL'
CREATE USER 'mig'@'%' IDENTIFIED BY 'MigPass!';
GRANT ALL ON *.* TO 'mig'@'%' WITH GRANT OPTION;
SQL
mysql_cli -h127.0.0.1 -P13308 -uroot -pToolPass_9_2 <<'SQL'
CREATE USER 'mig'@'%' IDENTIFIED BY 'MigPass!';
GRANT ALL ON *.* TO 'mig'@'%' WITH GRANT OPTION;
SQL
# The exact CLI path must forward both targets and complete a real readback.
cat > /tmp/migration-live.json <<'JSON'
{"options":{"gtid_mode":"off","exact_row_count":true,"native_options":{"mysqldump":["--net-buffer-length=16384"],"mysql":["--connect-timeout=40"]}},"migrations":[{"source":{"host":"127.0.0.1","port":13306,"user":"mig","password":"MigPass!","database":"migsrc"},"target":{"host":"127.0.0.1","port":13307,"user":"mig","password":"MigPass!","database":"mignative"},"options":{"native_options":{"mysqldump":["--net-buffer-length=32768"]}}}]}
JSON
chmod 0600 /tmp/migration-live.json
run_cli /tmp/migration-live.json --native-option mysql=--connect-timeout=45
mysql_cli -h127.0.0.1 -P13307 -uroot -pToolPass_8_4 -Nse "SELECT COUNT(*) FROM mignative.items" | grep -Fxq 2
python3 - <<'PYNATIVE'
import json
rows = [json.loads(line) for line in open('/tmp/migration-native-trace.jsonl')]
dumps = [r for r in rows if r[0].endswith('/mysqldump')]
imports = [r for r in rows if r[0].endswith('/mysql')]
assert dumps and imports
assert all('--net-buffer-length=32768' in row and '--net-buffer-length=16384' not in row for row in dumps)
assert all('--connect-timeout=40' in row and '--connect-timeout=45' in row and row.index('--connect-timeout=40') < row.index('--connect-timeout=45') for row in imports)
assert all('MigPass!' not in ' '.join(row) for row in rows)
print('MIGRATION_NATIVE_ARGV_READBACK_PASS')
PYNATIVE
# A rejected extension must fail before touching a target. A client-side
# unsupported extension must fail the product task and emit a failed report.
if run_cli /tmp/migration-live.json --native-option mysql=--force > /tmp/migration-native-rejected.log 2>&1; then
  echo MIGRATION_NATIVE_REFUSAL_FAILED >&2; exit 1
fi
# New target so the failure is caused by mysqldump, not the empty-target gate.
python3 - <<'PYFAILCFG'
import json
p='/tmp/migration-live.json'
x=json.load(open(p)); x['migrations'][0]['target']['database']='mignative_failure'
json.dump(x,open(p,'w'))
PYFAILCFG
if run_cli /tmp/migration-live.json --native-option mysqldump=--kubeauto-invalid-option > /tmp/migration-native-failure.log 2>&1; then
  echo MIGRATION_NATIVE_FAILURE_PROPAGATION_FAILED >&2; exit 1
fi
grep -Eq 'unknown (variable|option)' /tmp/migration-native-failure.log
if mysql_cli -h127.0.0.1 -P13307 -uroot -pToolPass_8_4 -Nse "SELECT TABLE_NAME FROM information_schema.tables WHERE TABLE_SCHEMA='mignative_failure'" | grep -q .; then echo MIGRATION_ASSERTION_FAILED_check203 >&2; exit 1; fi
if find /tmp -maxdepth 1 -name 'mysql_migration_*' -print -quit | grep -q .; then echo MIGRATION_ASSERTION_FAILED_check204 >&2; exit 1; fi
echo MIGRATION_NATIVE_FAILURE_RECOVERY_PASS
# Import failure must propagate, optional rollback may only remove its own target,
# and a subsequent valid task must recover from that clean boundary.
python3 - <<'PYIMPORTCFG'
import json
p='/tmp/migration-live.json'; x=json.load(open(p))
x['migrations'][0]['target']['database']='mignative_rollback'
x['options']['rollback_on_failure']=True
json.dump(x,open(p,'w'))
PYIMPORTCFG
if run_cli /tmp/migration-live.json --native-option mysql=--kubeauto-invalid-option > /tmp/migration-native-import-failure.log 2>&1; then
  echo MIGRATION_IMPORT_FAILURE_PROPAGATION_FAILED >&2; exit 1
fi
grep -Eq 'unknown (variable|option)' /tmp/migration-native-import-failure.log
remaining="$(mysql_cli -h127.0.0.1 -P13307 -uroot -pToolPass_8_4 -Nse "SELECT SCHEMA_NAME FROM information_schema.schemata WHERE SCHEMA_NAME='mignative_rollback'")"
test -z "$remaining"
run_cli /tmp/migration-live.json > /tmp/migration-native-retry.log
mysql_cli -h127.0.0.1 -P13307 -uroot -pToolPass_8_4 -Nse 'SELECT COUNT(*) FROM mignative_rollback.items' | grep -Fxq 2
echo MIGRATION_NATIVE_IMPORT_ROLLBACK_RETRY_PASS

# Concurrent independent tasks have different native options and import targets.
python3 - <<'PYCONCURRENT'
import copy, json
p='/tmp/migration-live.json'; x=json.load(open(p)); first=x['migrations'][0]
first['target']['database']='mignative_a'
first['options']['native_options']={'mysql':['--connect-timeout=41'],'mysqldump':['--net-buffer-length=16384']}
second=copy.deepcopy(first); second['target']['database']='mignative_b'
second['options']['native_options']={'mysql':['--connect-timeout=42'],'mysqldump':['--net-buffer-length=32768']}
x['migrations']=[first,second]; x['options'].update(max_workers=2,max_workers_per_target_host=2,rollback_on_failure=False)
json.dump(x,open(p,'w'))
PYCONCURRENT
run_cli /tmp/migration-live.json > /tmp/migration-native-concurrent.log
for target in mignative_a mignative_b; do
  mysql_cli -h127.0.0.1 -P13307 -uroot -pToolPass_8_4 -Nse "SELECT COUNT(*) FROM $target.items" | grep -Fxq 2
done
python3 - <<'PYCONCURRENTCHECK'
import json
rows=[json.loads(line) for line in open('/tmp/migration-native-trace.jsonl')]
for target, expected, other in [('mignative_a',41,42),('mignative_b',42,41)]:
    imports=[r for r in rows if r[0].endswith('/mysql') and r[-1]==target]
    assert imports and all('--connect-timeout='+str(expected) in r and '--connect-timeout='+str(other) not in r for r in imports)
print('MIGRATION_NATIVE_CONCURRENT_ISOLATION_PASS')
PYCONCURRENTCHECK
if [[ "$STAGE" == native ]]; then
  echo 'MIGRATION_CLI_LIVE_REGRESSION_PASS stage=native'
  exit 0
fi
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
if mysql_cli -h127.0.0.1 -P13307 -uroot -pToolPass_8_4 -Nse "SELECT SCHEMA_NAME FROM information_schema.schemata WHERE SCHEMA_NAME='dryrun_target'" | grep -q .; then echo MIGRATION_ASSERTION_FAILED_check220 >&2; exit 1; fi
cat > /tmp/migration-live.json <<'JSON'
{"migrations":[{"source":{"host":"127.0.0.1","port":13306,"user":"mig","password":"MigPass!","database":"migsrc","mode":"structure_and_data"},"target":{"host":"127.0.0.1","port":13307,"user":"mig","password":"MigPass!","database":"migtgt","mode":"structure_and_data"},"options":{"per_table":true,"table_checksum":true,"exact_row_count":true,"fix_definer":true,"gtid_mode":"off"}},{"source":{"host":"127.0.0.1","port":13307,"user":"mig","password":"MigPass!","database":"migtgt","mode":"structure_and_data"},"target":{"host":"127.0.0.1","port":13308,"user":"mig","password":"MigPass!","database":"migtgt9","mode":"structure_and_data"},"options":{"per_table":false,"compress_dump":false,"table_checksum":true,"exact_row_count":true,"gtid_mode":"off"}}],"options":{"report_dir":"/tmp/migration-live-report","keep_dump_files":false,"max_workers":1}}
JSON
chmod 0600 /tmp/migration-live.json
if [[ "$TOOL" == *.py ]]; then
  "$PYTHON_BIN" "$TOOL" --config /tmp/migration-live.json --report-dir /tmp/migration-live-report --table-checksum
else
  "$TOOL" --config /tmp/migration-live.json --report-dir /tmp/migration-live-report --table-checksum
fi
mysql_cli -h127.0.0.1 -P13307 -uroot -pToolPass_8_4 -e \
  "SELECT * FROM migtgt.items ORDER BY id; SELECT * FROM migtgt.special ORDER BY id;" | \
  grep -Fq 'alpha'
mysql_cli -h127.0.0.1 -P13307 -uroot -pToolPass_8_4 -e \
  "SELECT COUNT(*) FROM migtgt.items" | grep -Fxq '2'
mysql_cli -h127.0.0.1 -P13308 -uroot -pToolPass_9_2 -e \
  "SELECT * FROM migtgt9.items ORDER BY id" | grep -Fq 'alpha'
mysql_cli -h127.0.0.1 -P13308 -uroot -pToolPass_9_2 -e \
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
mysql_cli -h127.0.0.1 -P13308 -uroot -pToolPass_9_2 -e "SELECT COUNT(*) FROM migstruct.items" | grep -Fxq '0'

# DEFINER repair: the source view uses a non-existent account; fix_definer must
# rewrite it to CURRENT_USER so import succeeds with the migration account.
cat > /tmp/migration-definer.json <<'JSON'
{"migrations":[{"source":{"host":"127.0.0.1","port":13306,"user":"mig","password":"MigPass!","database":"migdef"},"target":{"host":"127.0.0.1","port":13307,"user":"mig","password":"MigPass!","database":"migdef_tgt"},"options":{"per_table":true,"compress_dump":false,"fix_definer":true,"gtid_mode":"off"}}],"options":{"report_dir":"/tmp/migration-definer-report"}}
JSON
chmod 0600 /tmp/migration-definer.json
run_cli /tmp/migration-definer.json > /tmp/migration-definer.log
mysql_cli -h127.0.0.1 -P13307 -uroot -pToolPass_8_4 -e 'SHOW CREATE VIEW migdef_tgt.v_base' | grep -Fq 'DEFINER=`mig`@`%`'

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
if mysql_cli -h127.0.0.1 -P13308 -uroot -pToolPass_9_2 -Nse "SELECT SCHEMA_NAME FROM information_schema.schemata WHERE SCHEMA_NAME='invalid_target'" | grep -q .; then echo MIGRATION_ASSERTION_FAILED_check286 >&2; exit 1; fi
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
