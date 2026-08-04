#!/usr/bin/env python3
"""Exercise ansible-runner's official container execution path against Debian 13."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

import ansible_runner


def main() -> int:
    image = os.environ["ANSIBLE_EE_PROBE_IMAGE"]
    inventory = {
        "all": {
            "hosts": {
                "192.168.47.128": {
                    "ansible_user": "brinnatt",
                    "ansible_python_interpreter": "/usr/bin/python3.13",
                }
            }
        }
    }
    playbook = [
        {
            "name": "kubeauto execution-environment target compatibility probe",
            "hosts": "all",
            "gather_facts": True,
            "tasks": [{"ansible.builtin.ping": {}}],
        }
    ]

    with tempfile.TemporaryDirectory(
        dir="/dev/shm", prefix="ansible-ee-probe-"
    ) as private_data_dir:
        private = Path(private_data_dir)
        project_dir = private / "project"
        inventory_dir = private / "inventory"
        project_dir.mkdir()
        inventory_dir.mkdir()
        # Runner mounts private_data_dir at /runner. Keeping playbook and
        # inventory under its documented layout avoids leaking host-only
        # /dev/shm paths into the execution container.
        (project_dir / "main.json").write_text(json.dumps(playbook), encoding="utf-8")
        (inventory_dir / "hosts.json").write_text(
            json.dumps(inventory), encoding="utf-8"
        )
        result = ansible_runner.run(
            private_data_dir=private_data_dir,
            inventory="hosts.json",
            playbook="main.json",
            process_isolation=True,
            process_isolation_executable="docker",
            container_image=image,
            container_volume_mounts=["/root/.ssh:/root/.ssh:ro"],
            container_options=[
                "--network",
                "host",
                "--label",
                "kubeauto.ansible-ee-probe=true",
            ],
            # Match the product path: HOME is explicit, while OpenSSH still
            # resolves default identities from getuid()'s passwd home.
            envvars={
                "ANSIBLE_HOST_KEY_CHECKING": "False",
                "HOME": "/runner",
            },
        )
    if result.rc != 0:
        return result.rc
    print("ANSIBLE_RUNNER_EE_DEBIAN_PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
