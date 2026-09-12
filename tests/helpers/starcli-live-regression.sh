#!/usr/bin/env bash
set -euo pipefail

TOOL="${STARCLI_TOOL:?}"
ARCHIVE="${STARCLI_ARCHIVE:?}"
ARCHIVE_SHA256="${STARCLI_ARCHIVE_SHA256:?}"
ROOT=/tmp/starcli-live
HOME_SR="$ROOT/starrocks"
FE_META="$ROOT/fe-meta"
FE_HOST=127.0.0.1
FE_HTTP=18030
FE_RPC=19020
FE_QUERY=19030
FE_EDIT=19010
BE_PORT=18060
BE_HTTP=18040
BE_HEARTBEAT=18050
BE_BRPC=18061
CN_PORT=18062
CN_HTTP=18042
CN_HEARTBEAT=18052
CN_BRPC=18063
PASSWORD='starcli-live-password'

fail() { echo "STARCLI_LIVE_FAIL: $*" >&2; exit 1; }
run() { echo "+ $*"; "$@"; }
run_tool() { echo "+ StarCli $*"; "$TOOL" "$@"; }
cleanup() {
  set +e
  [[ -x "$TOOL" ]] && "$TOOL" --deploy cn --starrocks-home "$HOME_SR" --fe-host "$FE_HOST" --fe-query-port "$FE_QUERY" --root-password "$PASSWORD" --clean --force --user root --group root >/dev/null 2>&1 || true
  [[ -x "$TOOL" ]] && "$TOOL" --deploy be --starrocks-home "$HOME_SR" --fe-host "$FE_HOST" --fe-query-port "$FE_QUERY" --root-password "$PASSWORD" --clean --force --user root --group root >/dev/null 2>&1 || true
  [[ -x "$TOOL" ]] && "$TOOL" --deploy fe --starrocks-home "$HOME_SR" --fe-host "$FE_HOST" --fe-query-port "$FE_QUERY" --root-password "$PASSWORD" --clean --force --user root --group root >/dev/null 2>&1 || true
  rm -f /tmp/starcli-invalid.out
  rm -rf "$ROOT"
}
trap cleanup EXIT INT TERM

test -f "$ARCHIVE" || fail "fixed StarRocks archive missing: $ARCHIVE"
echo "$ARCHIVE_SHA256  $ARCHIVE" | sha256sum -c - >/dev/null || fail "archive SHA256 mismatch"
command -v java >/dev/null 2>&1 || fail "Java 17 prerequisite missing"
java -version 2>&1 | grep -Eq 'version "(17|[2-9][0-9])' || fail "Java 17+ prerequisite missing"
rm -rf "$ROOT"; mkdir -p "$ROOT"
run tar -xzf "$ARCHIVE" -C "$ROOT"
found_home="$(find "$ROOT" -mindepth 1 -maxdepth 2 -type d -name fe -printf '%h\n' | head -1)"
test -n "$found_home" || fail "archive has no StarRocks FE layout"
mv "$found_home" "$HOME_SR"
test -x "$HOME_SR/fe/bin/start_fe.sh" || fail "FE launcher missing"
test -x "$HOME_SR/be/bin/start_be.sh" || fail "BE launcher missing"
test -x "$HOME_SR/be/bin/start_cn.sh" || fail "CN launcher missing"

run_tool --deploy fe --starrocks-home "$HOME_SR" --meta-dir "$FE_META" \
  --http-port "$FE_HTTP" --rpc-port "$FE_RPC" --query-port "$FE_QUERY" --edit-log-port "$FE_EDIT" \
  --priority-networks 127.0.0.0/8 --default-replication-num 1 --verify --user root --group root
run_tool --deploy fe --starrocks-home "$HOME_SR" --meta-dir "$FE_META" \
  --http-port "$FE_HTTP" --rpc-port "$FE_RPC" --query-port "$FE_QUERY" --edit-log-port "$FE_EDIT" \
  --setup --root-password "$PASSWORD" --user root --group root
run_tool --status --fe-host "$FE_HOST" --fe-query-port "$FE_QUERY" --root-password "$PASSWORD"
(echo >/dev/tcp/127.0.0.1/$FE_HTTP) >/dev/null 2>&1 || fail "FE HTTP port not listening"
(echo >/dev/tcp/127.0.0.1/$FE_QUERY) >/dev/null 2>&1 || fail "FE query port not listening"

# CLI-over-config precedence and idempotent repeat deployment.
cat >"$ROOT/config.json" <<EOF
{"deploy":"fe","starrocks_home":"$HOME_SR","meta_dir":"$FE_META","http_port":18031,"rpc_port":18021,"query_port":18031,"edit_log_port":18011,"enable_systemd":true,"priority_networks":"127.0.0.0/8"}
EOF
run_tool --config "$ROOT/config.json" --deploy fe --starrocks-home "$HOME_SR" \
  --meta-dir "$FE_META" --http-port "$FE_HTTP" --rpc-port "$FE_RPC" --query-port "$FE_QUERY" --edit-log-port "$FE_EDIT" \
  --fe-host "$FE_HOST" --fe-query-port "$FE_QUERY" --root-password "$PASSWORD" --force --user root --group root
grep -q '^http_port = 18030$' "$HOME_SR/fe/conf/fe.conf" || fail "CLI did not override JSON config"
for _ in $(seq 1 60); do
  (echo >/dev/tcp/127.0.0.1/$FE_QUERY) >/dev/null 2>&1 && break
  sleep 1
done
(echo >/dev/tcp/127.0.0.1/$FE_QUERY) >/dev/null 2>&1 || fail "FE did not become query-ready after force redeploy"

run_tool --deploy be --starrocks-home "$HOME_SR" --storage-root-path "$ROOT/be-data,medium:SSD" \
  --be-port "$BE_PORT" --be-http-port "$BE_HTTP" --heartbeat-port "$BE_HEARTBEAT" --brpc-port "$BE_BRPC" \
  --fe-host "$FE_HOST" --fe-query-port "$FE_QUERY" --root-password "$PASSWORD" --priority-networks 127.0.0.0/8 \
  --user root --group root
run_tool --deploy be --starrocks-home "$HOME_SR" --storage-root-path "$ROOT/be-data,medium:SSD" \
  --be-port "$BE_PORT" --be-http-port "$BE_HTTP" --heartbeat-port "$BE_HEARTBEAT" --brpc-port "$BE_BRPC" \
  --fe-host "$FE_HOST" --fe-query-port "$FE_QUERY" --root-password "$PASSWORD" --user root --group root

run_tool --status --fe-host "$FE_HOST" --fe-query-port "$FE_QUERY" --root-password "$PASSWORD" >"$ROOT/status-be.out"
grep -q 'BE节点:' "$ROOT/status-be.out" || fail "BE status section missing"
grep -q "$BE_HEARTBEAT" "$ROOT/status-be.out" || fail "deployed BE missing from status"

# BE and CN are intentionally mutually exclusive on one host.  Exercise the
# supported lifecycle by cleaning BE through StarCli before deploying CN.
run_tool --deploy be --starrocks-home "$HOME_SR" --fe-host "$FE_HOST" --fe-query-port "$FE_QUERY" \
  --root-password "$PASSWORD" --clean --force --user root --group root
test ! -e /etc/systemd/system/starrocks-be.service || fail "BE systemd unit survived StarCli cleanup"

# A rejected deployment must not create a service or report success.  Run it
# while the host is role-free so storage-path validation is reached (the
# separate CN/BE conflict branch is checked below).
if "$TOOL" --deploy be --starrocks-home "$HOME_SR" --storage-root-path "../outside" \
  --fe-host "$FE_HOST" --fe-query-port "$FE_QUERY" --root-password "$PASSWORD" --user root --group root >/tmp/starcli-invalid.out 2>&1; then
  fail "invalid storage path unexpectedly succeeded"
fi
grep -qi '存储路径\|storage' /tmp/starcli-invalid.out || fail "invalid storage failure was not diagnosable"
rm -f /tmp/starcli-invalid.out

run_tool --deploy cn --starrocks-home "$HOME_SR" --be-port "$CN_PORT" --be-http-port "$CN_HTTP" \
  --heartbeat-port "$CN_HEARTBEAT" --brpc-port "$CN_BRPC" --fe-host "$FE_HOST" --fe-query-port "$FE_QUERY" \
  --root-password "$PASSWORD" --user root --group root
run_tool --deploy cn --starrocks-home "$HOME_SR" --be-port "$CN_PORT" --be-http-port "$CN_HTTP" \
  --heartbeat-port "$CN_HEARTBEAT" --brpc-port "$CN_BRPC" --fe-host "$FE_HOST" --fe-query-port "$FE_QUERY" \
  --root-password "$PASSWORD" --user root --group root
run_tool --status --fe-host "$FE_HOST" --fe-query-port "$FE_QUERY" --root-password "$PASSWORD" >"$ROOT/status-cn.out"
grep -q 'CN节点:' "$ROOT/status-cn.out" || fail "CN status section missing"
grep -q "$CN_HEARTBEAT" "$ROOT/status-cn.out" || fail "deployed CN missing from status"

# The opposite role must be rejected while CN is active; this proves the
# local coexistence guard without mutating the deployment.
if "$TOOL" --deploy be --starrocks-home "$HOME_SR" --storage-root-path "$ROOT/be-data" \
  --fe-host "$FE_HOST" --fe-query-port "$FE_QUERY" --root-password "$PASSWORD" --user root --group root >/tmp/starcli-invalid.out 2>&1; then
  fail "BE/CN coexistence unexpectedly succeeded"
fi
grep -Eqi '共存|coexist|不能同时|CN.*BE|BE.*CN' /tmp/starcli-invalid.out || fail "BE/CN conflict failure was not diagnosable"
rm -f /tmp/starcli-invalid.out

grep -q 'priority_networks = 127.0.0.0/8' "$HOME_SR/fe/conf/fe.conf" || fail "priority network not rendered"
run_tool --deploy cn --starrocks-home "$HOME_SR" --fe-host "$FE_HOST" --fe-query-port "$FE_QUERY" --root-password "$PASSWORD" --clean --force --user root --group root
run_tool --deploy be --starrocks-home "$HOME_SR" --fe-host "$FE_HOST" --fe-query-port "$FE_QUERY" --root-password "$PASSWORD" --clean --force --user root --group root
run_tool --deploy fe --starrocks-home "$HOME_SR" --fe-host "$FE_HOST" --fe-query-port "$FE_QUERY" --root-password "$PASSWORD" --clean --force --user root --group root
test ! -e /etc/systemd/system/starrocks-fe.service || fail "FE systemd unit leaked"
test ! -e /etc/systemd/system/starrocks-be.service || fail "BE systemd unit leaked"
test ! -e /etc/systemd/system/starrocks-cn.service || fail "CN systemd unit leaked"
echo "STARCLI_LIVE_REGRESSION_PASS fe=1 be=1 cn=1 config-override=1 idempotent=1 cleanup=1"
