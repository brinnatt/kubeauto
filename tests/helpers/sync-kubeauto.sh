#!/bin/bash
# Sync kubeauto source tree to control host (preserve clusters + downloaded binaries).
# Usage: sync-kubeauto.sh <user@host> (SSH key authentication)
set -euo pipefail

require_command() {
  command -v "$1" >/dev/null 2>&1 || {
    echo "ERROR: required command '$1' is not installed." >&2
    exit 127
  }
}

require_command rsync
TARGET="${1:?user@host}"
SRC="$(cd "$(dirname "$0")/../.." && pwd)"
REMOTE_BASE="/usr/local/kubeauto"

RSYNC_SSH="ssh -o BatchMode=yes -o StrictHostKeyChecking=no -o ConnectTimeout=10 -o ServerAliveInterval=5 -o ServerAliveCountMax=2"
rsync -az \
  --delete --exclude '.git/' --exclude '.venv/' --exclude '.idea/' --exclude 'logs/' \
  --exclude 'dist/' --exclude 'build/' --exclude '__pycache__/' --exclude '*.pyc' \
  --exclude 'kube-bin/' --exclude 'extra-bin/' --exclude 'docker-bin/' --exclude 'down/' \
  --exclude 'clusters/' \
  -e "$RSYNC_SSH" \
  "$SRC/" "${TARGET}:${REMOTE_BASE}/"

ssh -o BatchMode=yes -o StrictHostKeyChecking=no -o ConnectTimeout=10 \
  -o ServerAliveInterval=5 -o ServerAliveCountMax=2 "$TARGET" "bash -s" <<REMOTE
set -euo pipefail
cat > /usr/local/bin/kubecli <<'WRAP'
#!/bin/bash
cd /usr/local/kubeauto
export PYTHONPATH=/usr/local/kubeauto
PY="\$(command -v python3.12 || command -v python3)"
exec "\$PY" /usr/local/kubeauto/kubecli.py "\$@"
WRAP
chmod +x /usr/local/bin/kubecli
test -f /usr/local/kubeauto/kubecli.py
test -f /usr/local/kubeauto/common/ansible_python.py
# Ensure Python runtime deps for source-based kubecli (Ubuntu aio 138 / jumper 130).
# Prefer python3.12 on Rocky jumper (system python3 may be 3.6).
PY="\$(command -v python3.12 || command -v python3)"
if ! \$PY -c "import docker, jinja2, psutil, taskflow" 2>/dev/null; then
  if ! \$PY -m pip --version >/dev/null 2>&1; then
    \$PY -m ensurepip --upgrade >/dev/null 2>&1 || true
  fi
  if \$PY -m pip --version >/dev/null 2>&1; then
    \$PY -m pip install -q -r /usr/local/kubeauto/requirements-control.txt
  else
    echo "WARN: cannot bootstrap pip for \$PY" >&2
  fi
fi
\$PY -c "import docker, jinja2, psutil, taskflow"
echo sync_ok
REMOTE

echo "Synced to ${TARGET}:${REMOTE_BASE}"
