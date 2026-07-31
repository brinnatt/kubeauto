"""Huawei Cloud software mirrors.

OS scope matches roles/prepare/tasks/main.yml:
  rhel  — RedHat/CentOS/Rocky/Alma/RHEL + openEuler + Anolis
  debian — Debian + Ubuntu (ansible_distribution_file_variety Debian)
  suse  — SUSE family
"""

from __future__ import annotations

import os
import platform
import shutil
from pathlib import Path

from common.ansible_python import (
    AnsiblePythonPolicy,
    ansible_core_probe_is_compatible,
    ansible_python_policy_for_core,
    probe_installed_ansible_core,
    python_meets_spec,
)
from common.exceptions import CommandExecutionError
from common.utils import get_resource_path, run_command

HUAWEI_REPO = "https://repo.huaweicloud.com"
HUAWEI_MIRRORS = "https://mirrors.huaweicloud.com"
HUAWEI_PYPI = "https://repo.huaweicloud.com/repository/pypi/simple"
UPSTREAM_PYPI = "https://pypi.org/simple"
PREFERRED_ANSIBLE_CORE = (2, 17)
ANSIBLE_CORE_SPEC = "ansible-core>=2.17,<2.18"

_CONTROL_PYTHON_NAMES = ("python3.12", "python3.11", "python3.10", "python3")

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


def _find_preferred_control_python(policy: AnsiblePythonPolicy) -> str:
    """Find a host Python supported by the pinned ansible-core control matrix."""
    checked: set[str] = set()
    for name in _CONTROL_PYTHON_NAMES:
        python = shutil.which(name)
        if not python or python in checked:
            continue
        checked.add(python)
        result = run_command(
            [python, "-c", "import sys; print(f'{sys.version_info[0]}.{sys.version_info[1]}')"],
            check=False,
        )
        if result.returncode != 0:
            continue
        try:
            major, minor = (int(part) for part in result.stdout.strip().split(".", 1))
        except (AttributeError, TypeError, ValueError):
            continue
        if python_meets_spec(major, minor, policy.control_min, policy.control_max):
            return python

    maximum = (
        f"{policy.control_max[0]}.{policy.control_max[1]}"
        if policy.control_max
        else "unbounded"
    )
    raise RuntimeError(
        f"ansible-core {policy.core_label} requires control Python "
        f"{policy.control_min[0]}.{policy.control_min[1]}-{maximum}; "
        "no compatible python3 executable was found"
    )


def _python_has_pip(python: str) -> bool:
    return run_command([python, "-m", "pip", "--version"], check=False).returncode == 0


def _ensure_pip(python: str, family: str, package_manager: str) -> None:
    """Ensure pip belongs to the selected control Python, not an unrelated default."""
    if _python_has_pip(python):
        return

    run_command([python, "-m", "ensurepip", "--upgrade"], check=False, capture_output=False)
    if _python_has_pip(python):
        return

    name = Path(python).name
    if family == "rhel":
        packages = (f"{name}-pip", "python3-pip")
        command = lambda package: [package_manager, "-y", "install", package]
    elif family == "debian":
        packages = ("python3-pip",)
        command = lambda package: ["apt-get", "-y", "install", package]
    else:
        compact_name = name.replace(".", "")
        packages = (f"{compact_name}-pip", "python3-pip")
        command = lambda package: ["zypper", "--non-interactive", "install", package]

    for package in dict.fromkeys(packages):
        try:
            run_command(command(package), capture_output=False)
        except CommandExecutionError:
            continue
        if _python_has_pip(python):
            return
    raise RuntimeError(f"pip is unavailable for the compatible control interpreter {python}")


def _install_supported_ansible_core(family: str, package_manager: str) -> None:
    """Install one audited ansible-core line on every supported OS family."""
    policy = ansible_python_policy_for_core(PREFERRED_ANSIBLE_CORE)
    python = _find_preferred_control_python(policy)
    _ensure_pip(python, family, package_manager)
    env = os.environ.copy()
    # Required by externally-managed Python installations (newer Debian); old
    # pip versions safely ignore the environment variable.
    env["PIP_BREAK_SYSTEM_PACKAGES"] = "1"

    def install(index_url: str) -> None:
        run_command(
            [
                python,
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                "--upgrade",
                "--index-url",
                index_url,
                ANSIBLE_CORE_SPEC,
            ],
            capture_output=False,
            env=env,
        )

    try:
        install(HUAWEI_PYPI)
    except CommandExecutionError:
        install(UPSTREAM_PYPI)


def _install_debian_ansible_core() -> None:
    """Backward-compatible wrapper for the former Debian-only implementation."""
    _install_supported_ansible_core("debian", "apt-get")


def _installed_ansible_is_compatible() -> bool:
    return ansible_core_probe_is_compatible(probe_installed_ansible_core())


def install_ansible_with_system_pm() -> None:
    """Install a matrix-compatible Ansible runtime, using native packages first."""
    apply_huawei_mirror()
    info = platform.freedesktop_os_release()
    os_id = info.get("ID", "").lower()
    id_like = info.get("ID_LIKE", "").lower()
    version_id = info.get("VERSION_ID", "")
    major = version_id.split(".")[0] if version_id else ""

    if os_id in _RHEL_IDS or "rhel" in id_like or "centos" in id_like:
        pm = "dnf" if major != "7" else "yum"
        if os_id not in {"openeuler", "anolis"}:
            try:
                run_command([pm, "-y", "install", "epel-release"], capture_output=False)
            except CommandExecutionError:
                pass
            # epel-release may write repo files after the first mirror pass.
            apply_huawei_mirror()
        try:
            run_command([pm, "-y", "install", "ansible"], capture_output=False)
        except CommandExecutionError:
            pass
        if not _installed_ansible_is_compatible():
            _install_supported_ansible_core("rhel", pm)
        return

    if os_id in _DEBIAN_IDS or "debian" in id_like:
        run_command(["apt-get", "update"], capture_output=False)
        _install_supported_ansible_core("debian", "apt-get")
        return

    if os_id in _SUSE_IDS or "suse" in id_like:
        try:
            run_command(["zypper", "--non-interactive", "install", "ansible"], capture_output=False)
        except CommandExecutionError:
            pass
        if not _installed_ansible_is_compatible():
            _install_supported_ansible_core("suse", "zypper")
        return

    raise RuntimeError(f"Unsupported distribution for ansible install: {os_id}")
