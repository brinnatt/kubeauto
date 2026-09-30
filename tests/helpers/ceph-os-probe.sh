#!/bin/bash
# Read-only preflight for real OS qualification hosts; not delivery evidence.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
PYTHON="${ROOT}/.venv/bin/python"
MATRIX="${ROOT}/tests/ceph-test-matrix.yaml"
KERNEL_CHECK="${ROOT}/roles/ceph/files/kernel-client-check.py"
SSH=(ssh -T -o BatchMode=yes -o StrictHostKeyChecking=no
  -o UserKnownHostsFile=/dev/null -o ConnectTimeout=10)

fail() {
  echo "CEPH_OS_PROBE_FAIL id=$1 reason=$2" >&2
  exit 1
}

[[ -x "$PYTHON" && -r "$KERNEL_CHECK" ]] || fail preflight missing_local_dependencies
"$PYTHON" "$ROOT/tests/helpers/ceph_contract.py" os-qualification --matrix "$MATRIX" >/dev/null
profiles="$("$PYTHON" - "$MATRIX" <<'PY'
import sys
import yaml

rows = yaml.safe_load(open(sys.argv[1], encoding="utf-8"))["os_qualification"]["profiles"]
for row in rows:
    assert row["ssh_user"] in {"root", "ubuntu", "ly"}
    assert row["client_key_type"] in {"aes", "aes256k"}
    print("|".join(str(row[key]) for key in (
        "id", "os_id", "major", "host", "ssh_user", "client_key_type")))
PY
)" || fail preflight invalid_matrix_profiles

count=0
while IFS='|' read -r id os_id major host ssh_user key_type; do
  [[ "$id" =~ ^CEPH-OS-[A-Z0-9]+$ && "$host" =~ ^[0-9.]+$ ]] ||
    fail preflight invalid_profile_identity
  echo "CEPH_OS_PROBE_BEGIN id=$id host=$host key_type=$key_type"
  metadata="$("${SSH[@]}" "${ssh_user}@${host}" \
    'set -eu; . /etc/os-release; printf "OS_META|%s|%s|%s|%s\n" "${ID:-}" "${VERSION_ID:-}" "$(uname -r)" "$(python3 --version 2>&1)"' </dev/null)" ||
    fail "$id" remote_os_probe_failed
  features="$("${SSH[@]}" "${ssh_user}@${host}" "python3 - '$key_type'" <"$KERNEL_CHECK")" ||
    fail "$id" remote_kernel_probe_failed
  output="$metadata"$'\n'"$features"
  if ! CEPH_OS_PROBE_OUTPUT="$output" "$PYTHON" - "$id" "$os_id" "$major" "$key_type" <<'PY'
import json
import os
import sys

profile, expected_os, expected_major, key_type = sys.argv[1:]
lines = os.environ["CEPH_OS_PROBE_OUTPUT"].splitlines()
assert len(lines) == 2 and lines[0].startswith("OS_META|"), profile
_, actual_os, version, kernel, python = lines[0].split("|", 4)
features = json.loads(lines[1])
assert (actual_os, version.split(".")[0]) == (expected_os, expected_major), profile
assert features["kernel"] == kernel and features["secure_msgr2"] is True, profile
assert key_type == "aes" or features["aes256k"] is True, profile
print(f"CEPH_OS_HOST_PROBE_PASS id={profile} os={actual_os}-{version} "
      f"kernel={kernel} python={python} key_type={key_type}")
PY
  then
    fail "$id" actual_os_or_kernel_mismatch
  fi
  count=$((count + 1))
done <<<"$profiles"
[[ "$count" -eq 6 ]] || fail preflight incomplete_profile_set
echo "CEPH_OS_PROBE_PASS profiles=$count qualification=pending"
