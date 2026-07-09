"""Ansible-core Python compatibility for kubeauto (control node + managed targets).

Official matrix
---------------
Values are taken from the ansible-core "Support matrix" (control vs target columns):
https://docs.ansible.com/projects/ansible-core/devel/reference_appendices/release_and_maintenance.html

Module runtime floor (target)
-----------------------------
From ansible-core 2.17 onward, module payloads use a vendored ``six`` that imports
``from __future__ import annotations`` (ansible/ansible#81902). That requires
Python 3.9+ to *execute* modules on the target, even though the published matrix
still lists target Python 3.7–3.12 for 2.17. kubeauto uses the stricter
``target_module_runtime_min`` for interpreter discovery and bootstrap.

RHEL 8 / module respawn
-----------------------
``dnf`` / ``yum`` Ansible modules may *respawn* under the OS-bound interpreter
(``/usr/libexec/platform-python``, Python 3.6 on RHEL 8) to load python3-dnf.
That interpreter cannot run ansible-core 2.17+ module code. kubeauto avoids
``ansible.builtin.dnf`` in ``roles/prepare/tasks/redhat.yml`` and uses the
``dnf`` / ``yum`` CLI via ``shell`` instead. Inventory still sets
``ansible_python_interpreter`` to a 3.9+ distro Python when available.

Architecture (interpreter selection)::

    ┌──────────────────────┐     ansible --version      ┌─────────────────────────┐
    │  Installed ansible-  │ ─────────────────────────► │  Support matrix lookup  │
    │  core on jump host   │                            │  (control + target)     │
    └──────────────────────┘                            └───────────┬─────────────┘
                                                                      │
                    ┌─────────────────────────────────────────────────┘
                    ▼
    ┌───────────────────────────────┐     SSH / local probe    ┌──────────────────┐
    │  target_module_runtime_min  │ ◄─────────────────────── │  Candidate Pythons│
    │  (>= documented minimum)    │                          │  on each host     │
    └───────────────┬───────────────┘                          └──────────────────┘
                    │
         missing on RHEL 8? ──yes──► install python39 (Huawei mirror) ──► re-probe
                    │
                    no
                    ▼
         ansible_python_interpreter=…  written into inventory
"""

from __future__ import annotations

import platform
import re
import shutil
from dataclasses import dataclass
from typing import Optional, Sequence, Tuple

from common.utils import run_command

# Source: ansible-core support matrix (control / target columns), 2.12–2.19.
# Tuple form: (min_major, min_minor, max_major, max_minor); max None = open upper bound.
AnsibleCoreVersion = Tuple[int, int]

_PYTHON_CANDIDATES: Sequence[str] = (
    "/usr/bin/python3.13",
    "/usr/bin/python3.12",
    "/usr/bin/python3.11",
    "/usr/bin/python3.10",
    "/usr/bin/python3.9",
    "/usr/bin/python3",
    "/usr/libexec/platform-python",
    "python3",
)

_RHEL8_IDS = frozenset({"rhel", "redhat", "rocky", "almalinux", "centos", "anolis", "openeuler"})
RHEL8_PLATFORM_PYTHON = "/usr/libexec/platform-python"

# (core_major, core_minor) -> (control_min, control_max, target_doc_min, target_doc_max, target_runtime_min)
# target_runtime_min: effective floor for module execution (may exceed documented target min).
_MATRIX: dict[AnsibleCoreVersion, tuple[tuple[int, int], Optional[tuple[int, int]], tuple[int, int], Optional[tuple[int, int]], tuple[int, int]]] = {
    (2, 12): ((3, 8), (3, 10), (3, 5), (3, 10), (3, 8)),
    (2, 13): ((3, 8), (3, 10), (3, 5), (3, 10), (3, 8)),
    (2, 14): ((3, 9), (3, 11), (3, 5), (3, 11), (3, 8)),
    (2, 15): ((3, 9), (3, 11), (3, 5), (3, 11), (3, 8)),
    (2, 16): ((3, 10), (3, 12), (3, 6), (3, 12), (3, 8)),
    (2, 17): ((3, 10), (3, 12), (3, 7), (3, 12), (3, 9)),
    (2, 18): ((3, 11), (3, 13), (3, 8), (3, 13), (3, 9)),
    (2, 19): ((3, 11), (3, 13), (3, 8), (3, 13), (3, 9)),
}

_DEFAULT_RUNTIME_MIN = (3, 9)
_DEFAULT_CONTROL_MIN = (3, 10)


@dataclass(frozen=True)
class AnsiblePythonPolicy:
    """Resolved Python requirements for the installed ansible-core."""

    core_version: AnsibleCoreVersion
    control_min: tuple[int, int]
    control_max: Optional[tuple[int, int]]
    target_documented_min: tuple[int, int]
    target_documented_max: Optional[tuple[int, int]]
    target_module_runtime_min: tuple[int, int]

    @property
    def core_label(self) -> str:
        return f"{self.core_version[0]}.{self.core_version[1]}"


def parse_ansible_core_version(version_output: str) -> Optional[AnsibleCoreVersion]:
    """Parse ``ansible --version`` / ``ansible-core --version`` text."""
    match = re.search(r"core\s+(\d+)\.(\d+)", version_output)
    if not match:
        return None
    return int(match.group(1)), int(match.group(2))


def read_installed_ansible_core_version() -> Optional[AnsibleCoreVersion]:
    """Return (major, minor) for ansible-core on the control node PATH."""
    for cmd in filter(None, (shutil.which("ansible"), shutil.which("ansible-core"))):
        try:
            out = run_command([cmd, "--version"], capture=True).stdout
            ver = parse_ansible_core_version(out)
            if ver:
                return ver
        except Exception:
            continue
    return None


def _lookup_matrix(core: AnsibleCoreVersion) -> AnsiblePythonPolicy:
    if core in _MATRIX:
        control_min, control_max, t_doc_min, t_doc_max, t_runtime = _MATRIX[core]
        return AnsiblePythonPolicy(
            core_version=core,
            control_min=control_min,
            control_max=control_max,
            target_documented_min=t_doc_min,
            target_documented_max=t_doc_max,
            target_module_runtime_min=t_runtime,
        )
    # Unknown future release: use newest known row as conservative baseline.
    latest = max(_MATRIX.keys())
    base = _lookup_matrix(latest)
    return AnsiblePythonPolicy(
        core_version=core,
        control_min=base.control_min,
        control_max=base.control_max,
        target_documented_min=base.target_documented_min,
        target_documented_max=base.target_documented_max,
        target_module_runtime_min=base.target_module_runtime_min,
    )


def ansible_python_policy() -> AnsiblePythonPolicy:
    """Build policy from the ansible-core version installed on the control node."""
    core = read_installed_ansible_core_version()
    if core is None:
        return AnsiblePythonPolicy(
            core_version=(0, 0),
            control_min=_DEFAULT_CONTROL_MIN,
            control_max=None,
            target_documented_min=(3, 8),
            target_documented_max=None,
            target_module_runtime_min=_DEFAULT_RUNTIME_MIN,
        )
    return _lookup_matrix(core)


def _version_ge(a: tuple[int, int], b: tuple[int, int]) -> bool:
    return a[0] > b[0] or (a[0] == b[0] and a[1] >= b[1])


def _version_le(a: tuple[int, int], b: tuple[int, int]) -> bool:
    return a[0] < b[0] or (a[0] == b[0] and a[1] <= b[1])


def python_meets_spec(major: int, minor: int, minimum: tuple[int, int], maximum: Optional[tuple[int, int]]) -> bool:
    ver = (major, minor)
    if not _version_ge(ver, minimum):
        return False
    if maximum and not _version_le(ver, maximum):
        return False
    return True


def is_rhel8_platform_python(path: str) -> bool:
    return path.rstrip("/") == RHEL8_PLATFORM_PYTHON


def interpreter_allowed_on_rhel8(path: str, policy: AnsiblePythonPolicy) -> bool:
    """Reject platform-python on RHEL 8 when ansible-core module runtime needs 3.9+."""
    if not is_rhel8_platform_python(path):
        return True
    return not _version_ge(policy.target_module_runtime_min, (3, 9))


def python_detect_shell(min_major: int, min_minor: int) -> str:
    """Shell snippet: print the first executable Python >= (min_major, min_minor)."""
    candidates = " ".join(_PYTHON_CANDIDATES)
    return (
        f"for py in {candidates}; do "
        '[ -x "$py" ] || continue; '
        f'"$py" -c "import sys; sys.exit(0 if sys.version_info >= ({min_major}, {min_minor}) else 1)" '
        "2>/dev/null && echo \"$py\" && exit 0; "
        "done; exit 1"
    )


def detect_target_python_cmd(policy: Optional[AnsiblePythonPolicy] = None) -> str:
    """Remote/local probe command using ``target_module_runtime_min`` from the matrix."""
    policy = policy or ansible_python_policy()
    minimum = policy.target_module_runtime_min
    return python_detect_shell(minimum[0], minimum[1])


def parse_detected_python(output: str) -> Optional[str]:
    text = output.strip()
    if not text:
        return None
    return text.splitlines()[-1].strip()


def validate_detected_interpreter(path: str, policy: Optional[AnsiblePythonPolicy] = None) -> bool:
    """True if ``path`` satisfies module runtime min and RHEL 8 respawn rules."""
    policy = policy or ansible_python_policy()
    if not interpreter_allowed_on_rhel8(path, policy):
        return False
    try:
        out = run_command(
            [path, "-c", "import sys; print(f'{sys.version_info[0]}.{sys.version_info[1]}')"],
            capture=True,
        ).stdout.strip()
        major, minor = out.split(".")[:2]
        return python_meets_spec(
            int(major),
            int(minor),
            policy.target_module_runtime_min,
            policy.target_documented_max,
        )
    except Exception:
        return False


def local_is_rhel8_family() -> bool:
    info = platform.freedesktop_os_release()
    os_id = info.get("ID", "").lower()
    major = (info.get("VERSION_ID", "") or "").split(".")[0]
    return os_id in _RHEL8_IDS and major == "8"


def remote_is_rhel8_family(os_id: str, version_id: str) -> bool:
    return os_id.lower() in _RHEL8_IDS and (version_id or "").split(".")[0] == "8"


def should_bootstrap_python39_rhel8(
    os_id: str,
    version_id: str,
    detected: Optional[str],
    policy: Optional[AnsiblePythonPolicy] = None,
) -> bool:
    """Install python39 only on RHEL 8 family when no matrix-compatible interpreter exists."""
    if not remote_is_rhel8_family(os_id, version_id):
        return False
    if detected and validate_detected_interpreter(detected, policy):
        return False
    return True


def rhel8_prepare_uses_shell_for_package_manager() -> bool:
    """kubeauto prepare on RedHat family uses ``shell: dnf|yum``, not ``ansible.builtin.dnf``."""
    return True


def rhel8_python39_bootstrap_local_cmd(mirror_script: str) -> str:
    return (
        f"set -e; bash {mirror_script} && "
        "(command -v dnf >/dev/null && dnf install -y python39 || yum install -y python39)"
    )


def rhel8_python39_bootstrap_remote_cmd(mirror_script_b64: str) -> str:
    return (
        f"set -e; echo '{mirror_script_b64}' | base64 -d | bash && "
        "(command -v dnf >/dev/null && dnf install -y python39 || yum install -y python39)"
    )


# Backward-compatible aliases used by manager.py
def detect_python_cmd() -> str:
    return detect_target_python_cmd()


def ansible_core_min_target_python() -> tuple[int, int]:
    return ansible_python_policy().target_module_runtime_min
