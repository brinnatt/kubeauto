"""Huawei Cloud software mirrors.

OS scope matches roles/prepare/tasks/main.yml:
  rhel  — RedHat/CentOS/Rocky/Alma/RHEL + openEuler + Anolis
  debian — Debian + Ubuntu (ansible_distribution_file_variety Debian)
  suse  — SUSE family
"""

from __future__ import annotations

import platform
from pathlib import Path

from common.utils import get_resource_path, run_command

HUAWEI_REPO = "https://repo.huaweicloud.com"
HUAWEI_MIRRORS = "https://mirrors.huaweicloud.com"

_RHEL_IDS = frozenset({"rhel", "redhat", "rocky", "almalinux", "centos", "openeuler", "anolis"})
_DEBIAN_IDS = frozenset({"debian", "ubuntu"})
_SUSE_IDS = frozenset({"opensuse", "opensuse-leap", "sles", "suse"})


def mirror_family() -> str | None:
    """Map local OS to mirror script family, or None if unsupported."""
    info = platform.freedesktop_os_release()
    os_id = info.get("ID", "").lower()
    id_like = info.get("ID_LIKE", "").lower()

    if os_id in _RHEL_IDS or "rhel" in id_like or "centos" in id_like:
        return "rhel"
    if os_id in _DEBIAN_IDS or "debian" in id_like:
        return "debian"
    if os_id in _SUSE_IDS or "suse" in id_like:
        return "suse"
    return None


def mirror_script(family: str) -> Path:
    return Path(get_resource_path("roles", "prepare", "files", f"huawei-mirror-{family}.sh"))


def apply_huawei_mirror() -> None:
    """Switch system package sources to Huawei Cloud (idempotent)."""
    family = mirror_family()
    if not family:
        return
    script = mirror_script(family)
    run_command(["bash", str(script)], capture_output=False)


def install_ansible_with_system_pm() -> None:
    """Install ansible after Huawei mirror is configured."""
    apply_huawei_mirror()
    info = platform.freedesktop_os_release()
    os_id = info.get("ID", "").lower()
    id_like = info.get("ID_LIKE", "").lower()
    version_id = info.get("VERSION_ID", "")
    major = version_id.split(".")[0] if version_id else ""

    if os_id in _RHEL_IDS or "rhel" in id_like or "centos" in id_like:
        pm = "dnf" if major != "7" else "yum"
        if pm == "dnf":
            run_command(["dnf", "-y", "install", "epel-release"], capture_output=False)
        else:
            run_command(["yum", "-y", "install", "epel-release"], capture_output=False)
        # epel-release writes repo files after the first mirror pass; re-apply so
        # EPEL baseurl points at Huawei (metalink-only epel.repo on EL8+).
        apply_huawei_mirror()
        run_command([pm, "-y", "install", "ansible"], capture_output=False)
        return

    if os_id in _DEBIAN_IDS or "debian" in id_like:
        run_command(["apt-get", "update"], capture_output=False)
        run_command(["apt-get", "-y", "install", "ansible"], capture_output=False)
        return

    if os_id in _SUSE_IDS or "suse" in id_like:
        run_command(["zypper", "--non-interactive", "install", "ansible"], capture_output=False)
        return

    raise RuntimeError(f"Unsupported distribution for ansible install: {os_id}")

