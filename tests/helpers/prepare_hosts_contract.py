#!/usr/bin/env python3
"""Exercise the production delegated hosts task on a private temporary file."""

import copy
import subprocess
import tempfile
from pathlib import Path

import yaml


def hosts_task(root: Path) -> dict:
    tasks = yaml.safe_load((root / "roles/prepare/tasks/main.yml").read_text())
    task = next(task for group in tasks for task in group.get("block", [])
                if task.get("name", "").startswith("the master[0] resolves all hosts"))
    if task.get("throttle") != 1 or task.get("run_once"):
        raise ValueError("every node must write the delegated hosts file serially")
    return copy.deepcopy(task)


def main() -> None:
    root = Path(__file__).resolve().parents[2]
    task = hosts_task(root)
    names = ["fixture" + str(index) for index in range(8)]
    with tempfile.TemporaryDirectory(prefix="kubeauto-prepare-hosts-") as directory:
        state = Path(directory)
        destination = state / "hosts"
        destination.write_text("preserve-unrelated-entry\n")
        task["lineinfile"]["dest"] = str(destination)
        inventory = state / "inventory.yml"
        inventory.write_text(yaml.safe_dump({"all": {"hosts": {name: {
            "ansible_connection": "local", "ansible_python_interpreter": "/usr/bin/python3",
            "K8S_NODENAME": name} for name in names}, "children": {
            "kube_master": {"hosts": {"localhost": {"ansible_connection": "local"}}}}}}))
        playbook = state / "playbook.yml"
        playbook.write_text(yaml.safe_dump([{"hosts": ":".join(names), "gather_facts": False,
                                            "tasks": [task]}], sort_keys=False))
        for iteration in range(2):
            subprocess.run(["ansible-playbook", "-i", str(inventory), str(playbook), "--forks", "8"],
                           check=True, timeout=180)
            expected = {"preserve-unrelated-entry"} | {
                name + "    " + name + " # flag by kubeauto" for name in names}
            lines = destination.read_text().splitlines()
            if set(lines) != expected or len(lines) != len(expected):
                raise ValueError("delegated hosts update lost or duplicated a node")
            print(f"PREPARE_HOSTS_CONCURRENCY_PASS iteration={iteration + 1} nodes=8", flush=True)
    print("PREPARE_HOSTS_CONTRACT_PASS", flush=True)


if __name__ == "__main__":
    main()
