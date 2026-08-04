#!/bin/bash
# Restore key-only access after an authorized lab snapshot/reinstall.
# The fallback password is runtime-only via LAB_SSH_PASSWORD and is never logged.
set -euo pipefail

KEY="${LAB_SSH_PUBLIC_KEY:-$HOME/.ssh/id_ed25519.pub}"
PASSWORD="${LAB_SSH_PASSWORD:-}"

if [[ ! -r "$KEY" ]]; then
  echo "ERROR: SSH public key is not readable: $KEY" >&2
  exit 2
fi
if ! command -v sshpass >/dev/null 2>&1; then
  echo "ERROR: sshpass is required for snapshot key bootstrap" >&2
  exit 127
fi

for target in "$@"; do
  ssh_error="$(mktemp)"
  trap 'rm -f "$ssh_error"' EXIT
  if ssh -o BatchMode=yes -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
      -o ConnectTimeout=10 \
      "$target" true >/dev/null 2>"$ssh_error"; then
    rm -f "$ssh_error"
    trap - EXIT
    echo "SSH_KEY_READY target=$target source=existing"
    continue
  fi
  if grep -Eq 'Connection refused|No route to host|Connection timed out' "$ssh_error"; then
    sed 's/^/ERROR: /' "$ssh_error" >&2
    rm -f "$ssh_error"
    trap - EXIT
    exit 1
  fi
  rm -f "$ssh_error"
  trap - EXIT
  if [[ -z "$PASSWORD" ]]; then
    if [[ -t 0 ]]; then
      read -r -s -p "LAB_SSH_PASSWORD: " PASSWORD
      echo
    else
      echo "ERROR: key authentication failed for $target and LAB_SSH_PASSWORD is unset" >&2
      exit 1
    fi
  fi
  SSHPASS="$PASSWORD" sshpass -e ssh \
    -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=10 \
    "$target" \
    'umask 077; mkdir -p "$HOME/.ssh"; touch "$HOME/.ssh/authorized_keys"; while IFS= read -r key; do grep -qxF "$key" "$HOME/.ssh/authorized_keys" || printf "%s\n" "$key" >> "$HOME/.ssh/authorized_keys"; done' \
    < "$KEY"
  ssh -o BatchMode=yes -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
    -o ConnectTimeout=10 \
    "$target" true
  echo "SSH_KEY_READY target=$target source=snapshot-bootstrap"
done

echo "LAB_SSH_BOOTSTRAP_PASS"
