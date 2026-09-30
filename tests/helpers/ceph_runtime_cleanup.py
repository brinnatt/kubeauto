#!/usr/bin/env python3
"""Remove stopped Kubernetes fixtures; support Rocky 8's native Python 3.6."""

import argparse
import json
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Dict, List, Set, Tuple


CNI_LINK = re.compile(r"^(nodelocaldns|kube-ipvs0|cali[0-9a-f]+|vxlan\.calico)$")


def read_processes() -> Dict[int, Tuple[int, List[str]]]:
    processes = {}
    for entry in Path("/proc").glob("[0-9]*"):
        try:
            argv = entry.joinpath("cmdline").read_bytes().decode().strip("\0").split("\0")
            status = entry.joinpath("status").read_text()
        except (FileNotFoundError, ProcessLookupError):
            continue
        parent = re.search(r"^PPid:\s+(\d+)$", status, re.M)
        if parent and argv[0]:
            processes[int(entry.name)] = int(parent[1]), argv
    return processes


def fixture_processes(processes: Dict[int, Tuple[int, List[str]]]) -> Set[int]:
    selected = set()
    for pid, (_, argv) in processes.items():
        if Path(argv[0]).name != "containerd-shim-runc-v2":
            continue
        if "-namespace" not in argv or argv[argv.index("-namespace") + 1:argv.index("-namespace") + 2] != ["k8s.io"]:
            raise ValueError("foreign containerd shim on the leased node; refusing cleanup")
        selected.add(pid)
    while True:
        descendants = {pid for pid, (parent, _) in processes.items() if parent in selected}
        expanded = selected | descendants
        if expanded == selected:
            return selected
        selected = expanded


def cni_links() -> List[str]:
    rows = json.loads(subprocess.check_output(["ip", "-j", "link", "show"], universal_newlines=True))
    return [row["ifname"] for row in rows if CNI_LINK.fullmatch(row["ifname"])]


def send_signal(pids: Set[int], sig: int) -> None:
    for pid in sorted(pids, reverse=True):
        try:
            os.kill(pid, sig)
        except ProcessLookupError:
            pass


def run(clean: bool) -> None:
    if Path("/etc/kubernetes").exists():
        raise ValueError("Kubernetes configuration remains; product destroy must run first")
    for unit in ("kubelet", "containerd"):
        state = subprocess.run(["systemctl", "is-active", unit], stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, universal_newlines=True)
        if state.stdout.strip() not in {"inactive", "unknown"} or state.returncode not in {3, 4}:
            raise ValueError(f"{unit} is not verifiably stopped")
    pids = fixture_processes(read_processes())
    if clean:
        try:
            send_signal(pids, signal.SIGSTOP)
            # Freeze children created between the snapshot and parent freeze.
            pids |= fixture_processes(read_processes())
            send_signal(pids, signal.SIGSTOP)
            send_signal(pids, signal.SIGKILL)
        finally:
            send_signal(pids, signal.SIGCONT)
        for _ in range(100):
            if not fixture_processes(read_processes()):
                break
            time.sleep(.1)
        for link in cni_links():
            subprocess.run(["ip", "link", "delete", link], check=True)
    remaining = fixture_processes(read_processes())
    links = cni_links()
    if remaining or links:
        raise ValueError(f"Kubernetes fixture residue: pids={sorted(remaining)} links={links}")
    print("CEPH_RUNTIME_CLEAN_VERIFY_PASS shims=0 cni_links=0")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--clean", action="store_true")
    args = parser.parse_args()
    try:
        run(args.clean)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print(f"CEPH_RUNTIME_CLEAN_FAIL reason={error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
