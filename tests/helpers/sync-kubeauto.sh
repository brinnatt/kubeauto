#!/bin/bash
# Sync kubeauto source tree to control host (preserve clusters + downloaded binaries).
# Usage: sync-kubeauto.sh <user@host> [password]
set -euo pipefail

TARGET="${1:?user@host}"
PASS="${2:-123456}"
SRC="$(cd "$(dirname "$0")/../.." && pwd)"
REMOTE_BASE="/usr/local/kubeauto"

RSYNC_SSH="sshpass -p ${PASS} ssh -o StrictHostKeyChecking=no"
rsync -az \
  --delete --exclude '.git/' --exclude '.venv/' --exclude '.idea/' --exclude 'logs/' \
  --exclude 'dist/' --exclude 'build/' --exclude '__pycache__/' --exclude '*.pyc' \
  --exclude 'kube-bin/' --exclude 'extra-bin/' --exclude 'docker-bin/' --exclude 'down/' \
  --exclude 'clusters/' \
  -e "$RSYNC_SSH" \
  "$SRC/" "${TARGET}:${REMOTE_BASE}/"

sshpass -p "$PASS" ssh -o StrictHostKeyChecking=no "$TARGET" "bash -s" <<REMOTE
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
# Ensure Python runtime deps for source-based kubecli (147/136 control nodes)
# Prefer python3.12 on Rocky jumper (system python3 may be 3.6).
PY="\$(command -v python3.12 || command -v python3)"
if ! \$PY -c "import taskflow" 2>/dev/null; then
  if ! \$PY -m pip --version >/dev/null 2>&1; then
    \$PY -m ensurepip --upgrade >/dev/null 2>&1 || true
  fi
  if \$PY -m pip --version >/dev/null 2>&1; then
    \$PY -m pip install -q -r /usr/local/kubeauto/requirements-control.txt
  else
    echo "WARN: cannot bootstrap pip for \$PY" >&2
  fi
fi
\$PY -c "import taskflow"
echo sync_ok
REMOTE

echo "Synced to ${TARGET}:${REMOTE_BASE}"
