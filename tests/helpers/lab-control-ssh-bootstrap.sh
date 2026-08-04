#!/bin/bash
# Give a remote control account key-only access to lab targets without moving
# private keys or persisting the lab fallback password.
set -euo pipefail

CONTROL_SUDO=false
if [[ "${1:-}" == "--sudo-control" ]]; then
  CONTROL_SUDO=true
  shift
fi

CONTROL="${1:?control user@host is required}"
shift
(( $# > 0 )) || {
  echo "ERROR: at least one target user@host is required" >&2
  exit 2
}

SSH_OPTIONS=(
  -o BatchMode=yes
  -o StrictHostKeyChecking=no
  -o UserKnownHostsFile=/dev/null
  -o ConnectTimeout=10
)
PUBLIC_KEY="$(mktemp)"
trap 'rm -f "$PUBLIC_KEY"' EXIT

read_control_public_key() {
  if [[ "$CONTROL_SUDO" == true ]]; then
    ssh "${SSH_OPTIONS[@]}" "$CONTROL" 'sudo -n bash -s'
  else
    ssh "${SSH_OPTIONS[@]}" "$CONTROL" 'bash -s'
  fi <<'REMOTE'
set -euo pipefail
home_dir="$HOME"
if [[ "$(id -u)" -eq 0 ]]; then
  home_dir=/root
fi
key="$home_dir/.ssh/id_ed25519"
umask 077
mkdir -p "$home_dir/.ssh"
if [[ ! -s "$key" ]]; then
  ssh-keygen -q -t ed25519 -N '' -f "$key"
elif [[ ! -s "$key.pub" ]]; then
  ssh-keygen -y -f "$key" > "$key.pub"
fi
chmod 600 "$key"
chmod 644 "$key.pub"
cat "$key.pub"
REMOTE
}

read_control_public_key > "$PUBLIC_KEY"
ssh-keygen -lf "$PUBLIC_KEY" >/dev/null

for target in "$@"; do
  case "$target" in
    *[!A-Za-z0-9@._:-]*|'')
      echo "ERROR: invalid SSH target: $target" >&2
      exit 2
      ;;
  esac

  ssh "${SSH_OPTIONS[@]}" "$target" '
set -euo pipefail
umask 077
mkdir -p "$HOME/.ssh"
touch "$HOME/.ssh/authorized_keys"
while IFS= read -r key; do
  grep -qxF "$key" "$HOME/.ssh/authorized_keys" || printf "%s\n" "$key" >> "$HOME/.ssh/authorized_keys"
done
chmod 700 "$HOME/.ssh"
chmod 600 "$HOME/.ssh/authorized_keys"
' < "$PUBLIC_KEY"

  if [[ "$CONTROL_SUDO" == true ]]; then
    ssh "${SSH_OPTIONS[@]}" "$CONTROL" \
      "sudo -n ssh -o BatchMode=yes -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=10 '$target' true"
  else
    ssh "${SSH_OPTIONS[@]}" "$CONTROL" \
      "ssh -o BatchMode=yes -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=10 '$target' true"
  fi
  echo "CONTROL_SSH_KEY_READY control=$CONTROL target=$target"
done

echo "LAB_CONTROL_SSH_BOOTSTRAP_PASS control=$CONTROL targets=$#"
