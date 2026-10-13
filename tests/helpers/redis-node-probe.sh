#!/usr/bin/env bash
# Read-only diagnostic for the Redis lab's failed Ansible prerequisite.
set -uo pipefail
hostname
rpm -q python39 python39-libs
rpm -qf /usr/lib64/python3.9/enum.py /usr/lib64/python3.9/re.py
rpm -V python39 python39-libs
for path in /usr/lib64/python3.9/enum.py /usr/lib64/python3.9/re.py; do
  stat -c '%a %s %y %n' "$path"
  sha256sum "$path"
done
/usr/bin/python3.9 -I - <<'PY'
import sys
print('version=',sys.version)
print('path=',sys.path)
import base64,re,enum
print('enum=',enum.__file__,'re=',re.__file__)
print('REDIS_NODE_PYTHON_IMPORT_PASS')
PY
rc=$?
printf 'REDIS_NODE_PYTHON_PROBE_EXIT rc=%s\n' "$rc"
exit "$rc"
