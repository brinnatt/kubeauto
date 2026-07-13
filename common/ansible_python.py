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

import functools
import platform
import re
import shlex
import shutil
from dataclasses import dataclass
from typing import Optional, Sequence, Tuple

from common.exceptions import AnsibleCoreDetectionError
from common.logger import setup_logger
from common.utils import run_command

logger = setup_logger(__name__)

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


@dataclass(frozen=True)
class AnsiblePythonPolicy:
    """Resolved Python requirements for a detected ansible-core release."""

    core_version: AnsibleCoreVersion
    control_min: tuple[int, int]
    control_max: Optional[tuple[int, int]]
    target_documented_min: tuple[int, int]
    target_documented_max: Optional[tuple[int, int]]
    target_module_runtime_min: tuple[int, int]

    @property
    def core_label(self) -> str:
        major, minor = self.core_version
        return f"{major}.{minor}"


@dataclass(frozen=True)
class AnsibleCoreProbeAttempt:
    command: str
    exit_code: Optional[int] = None
    stdout: str = ""
    stderr: str = ""
    error: str = ""


@dataclass(frozen=True)
class AnsibleCoreProbeResult:
    version: Optional[AnsibleCoreVersion]
    attempts: tuple[AnsibleCoreProbeAttempt, ...]


def parse_ansible_core_version(version_output: str) -> Optional[AnsibleCoreVersion]:
    """Parse ``ansible --version`` / ``ansible-core --version`` text."""
    match = re.search(r"core\s+(\d+)\.(\d+)", version_output)
    if not match:
        return None
    return int(match.group(1)), int(match.group(2))


def probe_installed_ansible_core() -> AnsibleCoreProbeResult:
    """Probe ansible-core on the control node PATH with full diagnostics."""
    attempts: list[AnsibleCoreProbeAttempt] = []
    commands = [c for c in (shutil.which("ansible"), shutil.which("ansible-core")) if c]
    if not commands:
        return AnsibleCoreProbeResult(
            version=None,
            attempts=(
                AnsibleCoreProbeAttempt(
                    command="(PATH lookup)",
                    error="ansible and ansible-core not found in PATH",
                ),
            ),
        )

    for cmd in commands:
        try:
            result = run_command([cmd, "--version"], check=False)
            attempt = AnsibleCoreProbeAttempt(
                command=cmd,
                exit_code=result.returncode,
                stdout=(result.stdout or "").strip(),
                stderr=(result.stderr or "").strip(),
            )
            attempts.append(attempt)
            if result.returncode == 0:
                version = parse_ansible_core_version(result.stdout or "")
                if version:
                    return AnsibleCoreProbeResult(version=version, attempts=tuple(attempts))
        except Exception as exc:
            attempts.append(AnsibleCoreProbeAttempt(command=cmd, error=str(exc)))

    return AnsibleCoreProbeResult(version=None, attempts=tuple(attempts))


def format_ansible_core_detection_failure(result: AnsibleCoreProbeResult) -> str:
    """Human-readable failure text when ansible-core cannot be detected."""
    lines = [
        "Cannot detect ansible-core version on the control node.",
        "kubeauto selects target Python requirements from the official ansible-core support matrix.",
        "",
        "Install Ansible on this host (e.g. kubecli download -a) and verify:",
        "  ansible --version",
        "",
        "Probe details:",
    ]
    for attempt in result.attempts:
        lines.append(f"  - command: {attempt.command}")
        if attempt.error:
            lines.append(f"    error: {attempt.error}")
        if attempt.exit_code is not None:
            lines.append(f"    exit code: {attempt.exit_code}")
        if attempt.stderr:
            lines.append(f"    stderr: {attempt.stderr[:300]}")
        if attempt.stdout:
            parsed = parse_ansible_core_version(attempt.stdout)
            if parsed:
                lines.append(f"    parsed core: {parsed[0]}.{parsed[1]}")
            else:
                lines.append(f"    stdout (unparsed): {attempt.stdout[:300]}")
    return "\n".join(lines)


def read_installed_ansible_core_version() -> Optional[AnsibleCoreVersion]:
    """Return (major, minor) for ansible-core on the control node PATH."""
    return probe_installed_ansible_core().version


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
    logger.warning(
        "ansible-core %s.%s is not in kubeauto matrix; using conservative policy from %s.%s.",
        core[0],
        core[1],
        latest[0],
        latest[1],
    )
    return AnsiblePythonPolicy(
        core_version=core,
        control_min=base.control_min,
        control_max=base.control_max,
        target_documented_min=base.target_documented_min,
        target_documented_max=base.target_documented_max,
        target_module_runtime_min=base.target_module_runtime_min,
    )


@functools.lru_cache(maxsize=1)
def ansible_python_policy() -> AnsiblePythonPolicy:
    """Build policy from ansible-core detected on the control node.

    Raises ``AnsibleCoreDetectionError`` when detection fails. There is no silent
    fallback version — guessing the matrix would mis-bootstrap managed hosts.
    """
    result = probe_installed_ansible_core()
    if result.version is None:
        message = format_ansible_core_detection_failure(result)
        logger.error(message)
        raise AnsibleCoreDetectionError(message)
    policy = _lookup_matrix(result.version)
    logger.debug(
        "Resolved ansible Python policy for ansible-core %s: target module runtime >= %s.%s",
        policy.core_label,
        policy.target_module_runtime_min[0],
        policy.target_module_runtime_min[1],
    )
    return policy


def clear_ansible_python_policy_cache() -> None:
    """Clear cached policy (unit tests / ansible reinstall)."""
    ansible_python_policy.cache_clear()


def format_policy_summary(policy: AnsiblePythonPolicy) -> str:
    """One-line summary for setup logs."""
    rt = policy.target_module_runtime_min
    ctrl = policy.control_min
    return (
        f"Ansible Python policy (ansible-core {policy.core_label}): "
        f"control node Python >={ctrl[0]}.{ctrl[1]}, "
        f"target module runtime Python >={rt[0]}.{rt[1]}"
    )


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


def python_read_version_shell(path: str) -> str:
    """Shell snippet: print ``major.minor`` for an interpreter path (remote/local)."""
    py = shlex.quote(path)
    return (
        f'{py} -c "import sys; print(f\'{{sys.version_info[0]}}.{{sys.version_info[1]}}\')" 2>/dev/null'
    )


def parse_python_version_text(output: str) -> Optional[tuple[int, int]]:
    text = output.strip().splitlines()[-1].strip() if output.strip() else ""
    if not text or "." not in text:
        return None
    major, minor = text.split(".", 1)[:2]
    try:
        return int(major), int(minor)
    except ValueError:
        return None


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


def validate_local_interpreter(path: str, policy: Optional[AnsiblePythonPolicy] = None) -> bool:
    """Validate an interpreter on the control node (local filesystem)."""
    policy = policy or ansible_python_policy()
    if not interpreter_allowed_on_rhel8(path, policy):
        return False
    try:
        out = run_command([path, "-c", "import sys; print(f'{sys.version_info[0]}.{sys.version_info[1]}')"]).stdout
        version = parse_python_version_text(out)
        if not version:
            return False
        return python_meets_spec(
            version[0],
            version[1],
            policy.target_module_runtime_min,
            policy.target_documented_max,
        )
    except Exception as exc:
        logger.debug("Local interpreter validation failed for %s: %s", path, exc)
        return False


def validate_remote_interpreter_version(
    version_output: str,
    policy: AnsiblePythonPolicy,
) -> bool:
    """Validate interpreter version text returned from a remote SSH probe."""
    version = parse_python_version_text(version_output)
    if not version:
        return False
    return python_meets_spec(
        version[0],
        version[1],
        policy.target_module_runtime_min,
        policy.target_documented_max,
    )


def validate_detected_interpreter(path: str, policy: Optional[AnsiblePythonPolicy] = None) -> bool:
    """Backward-compatible alias for local interpreter validation."""
    return validate_local_interpreter(path, policy)


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
    if detected and validate_local_interpreter(detected, policy):
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
