#!/usr/bin/env bash
# Private mount namespace: production roles may write ~/.kube on the controller.
# Keep the control host's home/kubeconfig intact, retaining read-only SSH access.
set -euo pipefail
[[ "$#" -ge 2 && "$1" == /var/tmp/kubeauto-v13510-lab ]] || exit 2
state="$1"; shift
[[ "$(id -u)" == 0 && -d "$state" && ! -L "$state" ]]
[[ "$(stat -c '%u:%a' "$state")" == 0:700 ]]
mkdir -p "$state/control-home/.ssh" "$state/control-origin"
chmod 0700 "$state/control-home" "$state/control-home/.ssh" "$state/control-origin"
exec unshare --mount --propagation private bash -s -- "$state" "$@" <<'ISOLATED'
set -euo pipefail
state="$1"; shift
mount --bind /root "$state/control-origin"
mount --bind "$state/control-home" /root
mount --bind "$state/control-origin/.ssh" /root/.ssh
mount -o remount,bind,ro /root/.ssh
exec "$@"
ISOLATED
