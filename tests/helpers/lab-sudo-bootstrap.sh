#!/usr/bin/env bash
# Restore the lab's passwordless-sudo contract after a snapshot reset.
set -euo pipefail

TARGET="${1:?user@host}"
PASSWORD="${LAB_SUDO_PASSWORD:-}"
USER_NAME="${TARGET%@*}"

SSH_OPTIONS=(
  -o BatchMode=yes
  -o StrictHostKeyChecking=no
  -o ConnectTimeout=10
  -o ServerAliveInterval=5
  -o ServerAliveCountMax=2
)

if ssh "${SSH_OPTIONS[@]}" "$TARGET" 'sudo -n true' >/dev/null 2>&1; then
  echo "SUDO_READY target=$TARGET source=existing"
  exit 0
fi

if [[ -z "$PASSWORD" ]]; then
  if [[ -t 0 ]]; then
    read -r -s -p "LAB_SUDO_PASSWORD: " PASSWORD
    echo
  else
    echo "ERROR: passwordless sudo is unavailable for $TARGET and LAB_SUDO_PASSWORD is unset" >&2
    exit 1
  fi
fi

case "$USER_NAME" in
  ubuntu|brinnatt) ;;
  *) echo "ERROR: unsupported sudo bootstrap account: $USER_NAME" >&2; exit 2 ;;
esac

rule_path="/etc/sudoers.d/90-kubeauto-lab-$USER_NAME"
remote_script="printf '%s\\n' '$USER_NAME ALL=(ALL) NOPASSWD:ALL' > '$rule_path' && chmod 0440 '$rule_path'"
printf '%s\n' "$PASSWORD" \
  | ssh "${SSH_OPTIONS[@]}" "$TARGET" "sudo -S -p '' sh -c $(printf '%q' "$remote_script")"

ssh "${SSH_OPTIONS[@]}" "$TARGET" 'sudo -n true'
echo "LAB_SUDO_BOOTSTRAP_PASS target=$TARGET"
