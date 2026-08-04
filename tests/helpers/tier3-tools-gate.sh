#!/usr/bin/env bash
# Execute each frozen tool on a real Rocky 8 / glibc 2.28 host.
set -euo pipefail

TOOL_DIR="${1:-/tmp/kubeauto-tier3-tools}"
TOOLS=(CalicoPolicyCli NetCheckCli KafkaCli MyBackupCli MigrationCli StarCli KubeBackupCli KubePublishCli OvpnUserCli)

cleanup() {
  rm -rf "$TOOL_DIR"
}
trap cleanup EXIT

test "$(getconf GNU_LIBC_VERSION)" = "glibc 2.28"
for tool in "${TOOLS[@]}"; do
  test -x "$TOOL_DIR/$tool"
  "$TOOL_DIR/$tool" --help >/dev/null
  echo "[PASS] Tier3 $tool frozen binary"
done

cleanup
trap - EXIT
echo "TIER3_TOOLS_GATE_PASS count=${#TOOLS[@]} glibc=2.28"
