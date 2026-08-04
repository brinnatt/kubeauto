#!/usr/bin/env bash
# Verify the customer kubecli native-package Ansible path and restore the host.
set -euo pipefail

KUBECLI="${1:-/tmp/kubecli-ansible-native-gate}"
SOURCE="${2:-/tmp/kubeauto-ansible-native-source}"
cleanup_started=0

. /etc/os-release
OS_ID="${ID,,}"
case "$OS_ID" in
  openeuler)
    PACKAGE_REMOVE=(dnf -y remove ansible)
    REPO_DIR=/etc/yum.repos.d
    ;;
  opensuse-leap|opensuse|sles|suse)
    PACKAGE_REMOVE=(zypper --non-interactive remove --clean-deps ansible)
    REPO_DIR=/etc/zypp/repos.d
    ;;
  *)
    echo "[FAIL] unsupported native-package gate OS: $OS_ID" >&2
    exit 2
    ;;
esac

echo "========== native Ansible package gate: $PRETTY_NAME =========="
test -x "$KUBECLI"
test -d "$SOURCE/playbooks"
if [[ -e /usr/local/bin/ansible || -e /usr/local/bin/ansible-playbook ]]; then
  echo "[FAIL] pre-existing /usr/local Ansible contamination" >&2
  exit 1
fi
if command -v ansible >/dev/null 2>&1; then
  echo "[FAIL] clean-snapshot precondition violated: Ansible already installed" >&2
  exit 1
fi

WORK="$(mktemp -d /tmp/kubeauto-ansible-native.XXXXXX)"
BASELINE="$WORK/rpm.before"
REPO_BACKUP="$WORK/repos.before"

rpm -qa | sort > "$BASELINE"
mkdir -p "$REPO_BACKUP"
cp -a "$REPO_DIR/." "$REPO_BACKUP/"

cleanup() {
  local initial_rc="${1:-0}" cleanup_rc=0
  (( cleanup_started == 0 )) || return "$initial_rc"
  cleanup_started=1
  set +e

  if command -v ansible >/dev/null 2>&1; then
    echo ">>> cleanup native Ansible package"
    "${PACKAGE_REMOVE[@]}"
  fi

  find "$REPO_DIR" -maxdepth 1 -type f -name '*.repo' -delete
  cp -a "$REPO_BACKUP/." "$REPO_DIR/"

  rpm -qa | sort > "$WORK/rpm.after"
  if ! cmp -s "$BASELINE" "$WORK/rpm.after"; then
    comm -13 "$BASELINE" "$WORK/rpm.after" > "$WORK/rpm.added"
    comm -23 "$BASELINE" "$WORK/rpm.after" > "$WORK/rpm.removed"
    echo "[CLEANUP] package delta after package-manager removal"
    sed 's/^/added=/' "$WORK/rpm.added"
    sed 's/^/removed=/' "$WORK/rpm.removed"
    if [[ -s "$WORK/rpm.added" && ! -s "$WORK/rpm.removed" ]]; then
      mapfile -t added_packages < "$WORK/rpm.added"
      rpm -e "${added_packages[@]}" || cleanup_rc=1
      rpm -qa | sort > "$WORK/rpm.after-rpm-clean"
      cmp -s "$BASELINE" "$WORK/rpm.after-rpm-clean" || cleanup_rc=1
    else
      cleanup_rc=1
    fi
  fi

  rm -rf "$SOURCE"
  rm -f "$KUBECLI"
  if (( cleanup_rc == 0 )); then
    echo ANSIBLE_NATIVE_CLEAN_PASS
  else
    echo "[FAIL] native Ansible gate did not restore the RPM baseline" >&2
  fi
  rm -rf "$WORK"
  set -e
  (( initial_rc == 0 && cleanup_rc == 0 ))
}

trap 'rc=$?; cleanup "$rc"; final_rc=$?; trap - EXIT; exit "$final_rc"' EXIT

run_customer_download() {
  local label="$1" log rc
  log="$WORK/$label.log"
  set +e
  "$KUBECLI" download -a </dev/null 2>&1 | tee "$log"
  rc="${PIPESTATUS[0]}"
  set -e
  (( rc == 0 )) || return "$rc"
  if grep -Eq '/tmp/_MEI[^:[:space:]]*/lib(tinfo|crypto|ssl)' "$log"; then
    echo "[FAIL] frozen kubecli leaked bundled libraries into a system subprocess" >&2
    return 1
  fi
}

echo ">>> customer binary: kubecli download -a"
run_customer_download first-download
echo ">>> idempotency: kubecli download -a"
run_customer_download idempotent-download
echo PYINSTALLER_EXTERNAL_LIB_CLEAN_PASS

ansible_path="$(command -v ansible)"
test "$ansible_path" = /usr/bin/ansible
owner="$(rpm -qf "$ansible_path")"
printf 'ansible_path=%s owner=%s\n' "$ansible_path" "$owner"
ansible --version
ansible localhost -i 'localhost,' -c local -m ping

echo ">>> kubeauto playbook syntax contract"
export ANSIBLE_ROLES_PATH="$SOURCE/roles"
for playbook in "$SOURCE"/playbooks/*.yml; do
  echo "syntax_check=$(basename "$playbook")"
  ansible-playbook -i "$SOURCE/conf/hosts.allinone" \
    -e "@$SOURCE/conf/config.yml" \
    -e NODE_TO_ADD=192.168.1.1 \
    -e NODE_TO_DEL=192.168.1.1 \
    -e CLUSTER=syntax-check \
    --syntax-check "$playbook"
done

cleanup 0
trap - EXIT
echo "ANSIBLE_NATIVE_PACKAGE_GATE_PASS os=$OS_ID owner=$owner"
