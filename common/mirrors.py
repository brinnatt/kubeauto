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

# Huawei mirrors of dl.k8s.io / k8s release binaries (verified 2026-07-08).
K8S_BIN_MIRROR_BASES = (
    f"{HUAWEI_MIRRORS}/dl.k8s.io/release",
    f"{HUAWEI_MIRRORS}/k8s/release",
    f"{HUAWEI_MIRRORS}/cncf/kubernetes/release",
)
K8S_BIN_FALLBACK_BASE = "https://dl.k8s.io/release"

K8S_BIN_NAMES = (
    "kube-apiserver",
    "kube-controller-manager",
    "kube-scheduler",
    "kubelet",
    "kube-proxy",
    "kubectl",
)

_ARCH_TO_GOARCH = {"x86_64": "amd64", "aarch64": "arm64"}

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


def normalize_k8s_version(version: str) -> str:
    version = version.strip()
    return version if version.startswith("v") else f"v{version}"


def k8s_goarch(machine: str) -> str:
    return _ARCH_TO_GOARCH.get(machine, machine)


def k8s_bin_download_url(version: str, machine: str, name: str, base_index: int = 0) -> str:
    ver = normalize_k8s_version(version)
    goarch = k8s_goarch(machine)
    base = K8S_BIN_MIRROR_BASES[base_index]
    return f"{base}/{ver}/bin/linux/{goarch}/{name}"


def download_k8s_bins(version: str, dest_dir: Path, machine: str) -> None:
    """Download Kubernetes server/client binaries (Huawei mirrors first, dl.k8s.io fallback)."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    ver = normalize_k8s_version(version)
    goarch = k8s_goarch(machine)
    for name in K8S_BIN_NAMES:
        dest = dest_dir / name
        floor = 500_000 if name in ("kubectl", "kube-proxy") else 1_000_000
        last_err = None
        for base in (*K8S_BIN_MIRROR_BASES, K8S_BIN_FALLBACK_BASE):
            url = f"{base}/{ver}/bin/linux/{goarch}/{name}"
            try:
                tmp = dest.with_suffix(".tmp")
                run_command(["curl", "-fsSL", "-o", str(tmp), url], capture_output=False)
                if tmp.stat().st_size < floor:
                    raise RuntimeError(f"unexpected size {tmp.stat().st_size} from {url}")
                tmp.chmod(0o755)
                tmp.replace(dest)
                last_err = None
                break
            except Exception as exc:
                last_err = exc
                dest.unlink(missing_ok=True)
        if last_err:
            raise RuntimeError(f"Failed to download {name} for {ver}: {last_err}")

