#!/bin/bash
# Sync kubeauto source tree to control host (preserve clusters + downloaded binaries).
# Usage: sync-kubeauto.sh <user@host> (SSH key authentication)
# Optional: KUBEAUTO_SSH_JUMP=<user@jumper> for a ProxyJump-only target.
set -euo pipefail

require_command() {
  command -v "$1" >/dev/null 2>&1 || {
    echo "ERROR: required command '$1' is not installed." >&2
    exit 127
  }
}

require_command rsync
TARGET="${1:?user@host}"
CLEAN_LEGACY_GLOBAL_PIP="${2:-}"
SRC="$(cd "$(dirname "$0")/../.." && pwd)"
REMOTE_BASE="/usr/local/kubeauto"

SSH_OPTIONS=(
  -o BatchMode=yes
  -o StrictHostKeyChecking=no
  -o ConnectTimeout=10
  -o ServerAliveInterval=5
  -o ServerAliveCountMax=2
)
if [[ -n "${KUBEAUTO_SSH_JUMP:-}" ]]; then
  SSH_OPTIONS+=(-J "$KUBEAUTO_SSH_JUMP")
fi
printf -v RSYNC_SSH 'ssh'
printf -v SSH_OPTIONS_QUOTED ' %q' "${SSH_OPTIONS[@]}"
RSYNC_SSH+="$SSH_OPTIONS_QUOTED"
# A rebuilt control host may not have the deployment directory yet. Bootstrap
# only its ownership; rsync still preserves runtime artifacts and clusters via
# the exclusions below.
ssh "${SSH_OPTIONS[@]}" "$TARGET" \
  'if [ "$(id -u)" -eq 0 ]; then install -d /usr/local/kubeauto; else sudo -n install -d -o "$(id -un)" -g "$(id -gn)" /usr/local/kubeauto; fi'
rsync -az \
  --delete --exclude '.git/' --exclude '.venv/' --exclude '.idea/' --exclude 'logs/' \
  --exclude 'dist/' --exclude 'build/' --exclude '__pycache__/' --exclude '*.pyc' \
  --exclude 'kube-bin/' --exclude 'extra-bin/' --exclude 'docker-bin/' --exclude 'down/' \
  --exclude 'clusters/' \
  -e "$RSYNC_SSH" \
  "$SRC/" "${TARGET}:${REMOTE_BASE}/"

ssh "${SSH_OPTIONS[@]}" "$TARGET" "bash -s" <<REMOTE
set -euo pipefail
if [[ "${KUBEAUTO_SYNC_SKIP_CONTROL_SETUP:-0}" == 1 ]]; then
  test -f /usr/local/kubeauto/kubecli.py
  echo sync_source_only_ok
  exit 0
fi
if [[ "\$(id -u)" -eq 0 ]]; then
  SUDO=()
else
  SUDO=(sudo -n)
fi
PY="\$(command -v python3.12 || command -v python3)"
VENV=/usr/local/kubeauto/.venv
if [[ ! -x "\$VENV/bin/python" ]]; then
  "\$PY" -m venv "\$VENV"
fi
if ! "\$VENV/bin/python" -c "import docker, jinja2, psutil, taskflow" 2>/dev/null; then
  "\$VENV/bin/python" -m pip install -q -r /usr/local/kubeauto/requirements-control.txt
fi
"\$VENV/bin/python" -c "import docker, jinja2, psutil, taskflow"
if [[ "$CLEAN_LEGACY_GLOBAL_PIP" == "--clean-legacy-global-pip" ]]; then
  SYSTEM_PY=/usr/bin/python3
  GLOBAL_SITE="\$("\$SYSTEM_PY" -c 'import site; print(next(path for path in site.getsitepackages() if path.startswith("/usr/local/")))')"
  mapfile -t GLOBAL_PACKAGES < <(
    "\$SYSTEM_PY" -m pip list --path "\$GLOBAL_SITE" --format=freeze 2>/dev/null \
      | sed 's/[=@<].*//' | sed '/^$/d'
  )
  if (( \${#GLOBAL_PACKAGES[@]} > 0 )); then
    "\${SUDO[@]}" "\$SYSTEM_PY" -m pip uninstall -y "\${GLOBAL_PACKAGES[@]}"
  fi
  PYTHONNOUSERSITE=1 "\$SYSTEM_PY" -c 'from ansible.modules import apt'
  echo LEGACY_GLOBAL_PIP_CLEAN_PASS
fi
WRAPPER=\$(mktemp)
cat > "\$WRAPPER" <<'WRAP'
#!/bin/bash
cd /usr/local/kubeauto
export PYTHONPATH=/usr/local/kubeauto
exec /usr/local/kubeauto/.venv/bin/python /usr/local/kubeauto/kubecli.py "\$@"
WRAP
"\${SUDO[@]}" install -m 0755 "\$WRAPPER" /usr/local/bin/kubecli
rm -f "\$WRAPPER"
test -f /usr/local/kubeauto/kubecli.py
test -f /usr/local/kubeauto/common/ansible_python.py
echo sync_ok
REMOTE

echo "Synced to ${TARGET}:${REMOTE_BASE}"
