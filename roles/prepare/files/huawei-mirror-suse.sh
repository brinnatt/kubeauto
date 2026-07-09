#!/bin/bash
# Huawei Cloud zypper mirror — openSUSE / SLES (see roles/prepare/tasks/main.yml).
# Ref: https://repo.huaweicloud.com/
set -euo pipefail

HUAWEI="https://repo.huaweicloud.com"

[ -f /etc/os-release ] && . /etc/os-release || exit 0
ID="${ID,,}"

case "$ID" in
  opensuse*|sles|suse)
    ;;
  *)
    case "${ID_LIKE:-}" in
      *suse*) ;;
      *) exit 0 ;;
    esac
    ;;
esac

if ! command -v zypper >/dev/null 2>&1; then
  exit 0
fi

# Replace known openSUSE download hosts with Huawei mirror base.
for repo in $(zypper repos 2>/dev/null | awk '/^[0-9]+/ {print $1}'); do
  uri=$(zypper repos -u 2>/dev/null | awk -v r="$repo" '$1 == r {print $2; exit}')
  [ -n "$uri" ] || continue
  case "$uri" in
    *download.opensuse.org*|*opensuse.org*)
      new_uri="${uri/download.opensuse.org/mirrors.huaweicloud.com/opensuse}"
      new_uri="${new_uri//http:\/\//https:\/\/}"
      zypper modifyrepo --uri "$new_uri" "$repo" 2>/dev/null || true
      ;;
  esac
done

zypper --non-interactive refresh >/dev/null 2>&1 || true
