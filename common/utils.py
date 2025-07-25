"""
Utility functions for kubeauto
"""
import subprocess
import shutil
import ipaddress
from typing import List
from pathlib import Path
from .logger import setup_logger
from .exceptions import CommandExecutionError

logger = setup_logger(__name__)


def run_command(cmd: List[str] | str, check: bool = True, capture_output=True, allowed_exit_codes: List[int] = None,
                **kwargs):
    """Run a shell command with error handling"""
    logger.debug(f"Executing command: {' '.join(cmd)}")

    # [fix subprocess grammar] if SHELL enabled, CMD must be string, because LIST takes no effective in this case.
    if kwargs.get("shell") and not isinstance(cmd, str):
        raise CommandExecutionError(f"Command {cmd} must be a string when you enter a shell command!")

    # [fix subprocess grammar] Handle stdout/stderr and capture_output conflict
    if capture_output and ("stdout" in kwargs or "stderr" in kwargs):
        capture_output = False  # Disable capture_output if stdout/stderr is provided

    try:
        result = subprocess.run(cmd, check=check, capture_output=capture_output, text=True, **kwargs)
        return result
    except subprocess.CalledProcessError as e:
        if allowed_exit_codes and e.returncode in allowed_exit_codes:
            return e
        # Build detailed error message
        error_msg = (
            f"Command failed with exit code {e.returncode}: {' '.join(e.cmd)}\n"
            f"Error output: {e.stderr.strip() if e.stderr else '(empty)'}\n"
            f"Standard output: {e.stdout.strip() if e.stdout else '(empty)'}"
        )
        raise CommandExecutionError(error_msg)
    except Exception as e:
        raise CommandExecutionError(f"Command failed: {e}")


def rmrf(path: Path) -> None:
    try:
        if not path.exists():
            return
        elif path.is_symlink():
            path.unlink()
        elif path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink()
    except Exception as e:
        raise CommandExecutionError(f"Failed to remove {path}: {e}")


def validate_ip(ip: str) -> bool:
    """Validate an IP address"""
    try:
        ipaddress.ip_address(ip)
        return True
    except ValueError:
        return False


def get_host_ip() -> str:
    """Get host's primary IP address"""
    try:
        # Try using ip command
        result = run_command(["ip", "route", "get", "1"])
        interface = result.stdout.split("dev ")[1].split(" ")[0]
        result = run_command(["ip", "addr", "show", interface])
        ip_line = [line for line in result.stdout.split('\n') if "inet " in line][0]
        ip = ip_line.split("inet ")[1].split("/")[0]
        return ip
    except Exception as e:
        logger.warning(f"Failed to get host IP: {e}")
        return "127.0.0.1"


def ssh_localhost() -> None:
    """Setup SSH keys if they don't exist"""
    ssh_dir = Path.home() / ".ssh"
    ssh_dir.mkdir(exist_ok=True, mode=0o700)

    private_key = ssh_dir / "id_rsa"
    if not private_key.exists():
        logger.info("Generating SSH key pair", extra={"to_stdout": True})
        run_command(f"ssh-keygen -t rsa -b 2048 -N '' -f {private_key}", shell=True)

    authorized_keys = ssh_dir / "authorized_keys"
    authorized_keys.touch(mode=0o600)

    public_key = ssh_dir / "id_rsa.pub"
    if public_key.exists() and authorized_keys.exists():
        with open(public_key) as f:
            pub_key_content = f.read().strip()
        with open(authorized_keys) as f:
            auth_keys_content = f.read()
        if pub_key_content not in auth_keys_content:
            with open(authorized_keys, "a") as f:
                f.write(f"\n{pub_key_content}\n")

    # Add host to known_hosts
    host_ip = get_host_ip()
    known_hosts = ssh_dir / "known_hosts"
    run_command(["ssh-keyscan", "-t", "ecdsa", "-H", host_ip], stdout=known_hosts.open("a"))


def confirm_action(prompt: str, timeout: int = 5) -> bool:
    """Ask for confirmation with timeout"""
    import select
    import sys

    logger.warning(f"{prompt} (timeout: {timeout}s)")
    sys.stdout.write("Press any key to abort...")
    sys.stdout.flush()

    rlist, _, _ = select.select([sys.stdin], [], [], timeout)
    if rlist:
        sys.stdin.read(1)
        logger.warning("Action aborted by user")
        return False
    return True
