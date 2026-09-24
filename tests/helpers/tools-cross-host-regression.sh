#!/usr/bin/env bash
# One-host portability and isolation probe for every frozen tools CLI.
set -Eeuo pipefail

ROOT="${TOOLS_CROSS_ROOT:?TOOLS_CROSS_ROOT is required}"
WORK="${TOOLS_CROSS_WORK:?TOOLS_CROSS_WORK is required}"
HOST_LABEL="${TOOLS_CROSS_HOST_LABEL:?TOOLS_CROSS_HOST_LABEL is required}"
TOOLS=(CalicoPolicyCli NetCheckCli KafkaCli MyBackupCli MyLogiBackupCli MigrationCli StarCli KubeBackupCli KubePublishCli OvpnUserCli)

mkdir -p "$WORK"

for tool in "${TOOLS[@]}"; do
  test -x "$ROOT/$tool"
done

pids=()
for tool in "${TOOLS[@]}"; do
  "$ROOT/$tool" --help >"$WORK/$tool.stdout" 2>"$WORK/$tool.stderr" &
  pids+=("$!")
done

for pid in "${pids[@]}"; do
  wait "$pid"
done

for tool in "${TOOLS[@]}"; do
  test -s "$WORK/$tool.stdout"
  test ! -s "$WORK/$tool.stderr"
  test -x "$ROOT/$tool"
  sha256sum "$ROOT/$tool"
  echo "TOOLS_CROSS_CLI_PASS host=$HOST_LABEL tool=$tool rc=0"
done

echo "TOOLS_CROSS_HOST_PASS host=$HOST_LABEL tools=${#TOOLS[@]}"
