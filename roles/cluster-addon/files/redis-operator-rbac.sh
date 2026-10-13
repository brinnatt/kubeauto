#!/usr/bin/env bash
# Helm must use the same dependency-qualified Python that runs Ansible.
set -Eeuo pipefail
[[ -n "${REDIS_POST_RENDER_PYTHON:-}" && -x "$REDIS_POST_RENDER_PYTHON" ]] || {
  echo 'Redis Operator post-render requires the Ansible Python interpreter' >&2
  exit 1
}
exec "$REDIS_POST_RENDER_PYTHON" "$(dirname "$0")/redis-operator-rbac.py"
