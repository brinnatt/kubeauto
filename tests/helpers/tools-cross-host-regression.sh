#!/usr/bin/env bash
# One-host portability and isolation probe for every frozen tools CLI.
set -Eeuo pipefail

ROOT="${TOOLS_CROSS_ROOT:?TOOLS_CROSS_ROOT is required}"
WORK="${TOOLS_CROSS_WORK:?TOOLS_CROSS_WORK is required}"
HOST_LABEL="${TOOLS_CROSS_HOST_LABEL:?TOOLS_CROSS_HOST_LABEL is required}"
TOOLS=(CalicoPolicyCli NetCheckCli KafkaCli MyBackupCli MyLogiBackupCli MigrationCli StarCli KubeBackupCli KubePublishCli OvpnUserCli)
shopt -s nullglob

mkdir -p "$WORK"
mkdir -p "$WORK/help-runtime" "$WORK/net-runtime"
printf '%s\n' keep-customer-input >"$WORK/customer-input.keep"

for tool in "${TOOLS[@]}"; do
  test -x "$ROOT/$tool"
done

pids=()
protected_pid=
cleanup() {
  local pid
  for pid in "${pids[@]}" ${protected_pid:+"$protected_pid"}; do
    kill "$pid" 2>/dev/null || true
    wait "$pid" 2>/dev/null || true
  done
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
for tool in "${TOOLS[@]}"; do
  TMPDIR="$WORK/help-runtime" "$ROOT/$tool" --help >"$WORK/$tool.stdout" 2>"$WORK/$tool.stderr" &
  pids+=("$!")
done

TMPDIR="$WORK/net-runtime" NETCHECK_LOG_DIR="$WORK/net-log" "$ROOT/NetCheckCli" \
  --no-default-nodes --duration 8 --no-log-file --no-report-file 127.0.0.1:1 \
  >"$WORK/net-running.stdout" 2>"$WORK/net-running.stderr" &
protected_pid=$!
for _ in $(seq 1 50); do
  runtimes=("$WORK/net-runtime"/_MEI*)
  [[ "${#runtimes[@]}" -gt 0 ]] && break
  kill -0 "$protected_pid"
  sleep .1
done
[[ "${#runtimes[@]}" -eq 1 ]]
runtime="${runtimes[0]}"

for pid in "${pids[@]}"; do
  wait "$pid"
done
kill -0 "$protected_pid"
test -d "$runtime"
test "$(cat "$WORK/customer-input.keep")" = keep-customer-input
remaining=("$WORK/help-runtime"/*)
[[ "${#remaining[@]}" -eq 0 ]]
if wait "$protected_pid"; then probe_rc=0; else probe_rc=$?; fi
[[ "$probe_rc" == 0 || "$probe_rc" == 2 ]]
remaining=("$WORK/net-runtime"/*)
[[ "${#remaining[@]}" -eq 0 ]]
echo "TOOLS_CROSS_ISOLATION_PASS host=$HOST_LABEL active-runtime=preserved all-runtimes=removed customer-input=preserved"

for tool in "${TOOLS[@]}"; do
  test -s "$WORK/$tool.stdout"
  test ! -s "$WORK/$tool.stderr"
  test -x "$ROOT/$tool"
  sha256sum "$ROOT/$tool"
  echo "TOOLS_CROSS_CLI_PASS host=$HOST_LABEL tool=$tool rc=0"
done

echo "TOOLS_CROSS_HOST_PASS host=$HOST_LABEL tools=${#TOOLS[@]}"
