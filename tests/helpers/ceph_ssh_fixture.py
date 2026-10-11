#!/usr/bin/env python3
"""Runner-owned management/Registry relay for the NAT-separated qualification lab."""

from __future__ import annotations

import argparse
import fcntl
import json
import shlex
import signal
import subprocess
import tempfile
import threading
import time
from pathlib import Path


STORAGE = tuple("192.168.122." + x for x in ("135", "40", "72", "212", "165", "238"))
TARGETS = {"legacy": ("192.168.47.134", "192.168.47.138"),
           "modern": ("192.168.47.145", "192.168.47.146", "192.168.47.147"), "rocky9": ()}
SSH = ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=no",
       "-o", "ConnectTimeout=10", "-o", "ServerAliveInterval=5", "-o", "ServerAliveCountMax=2"]
COMMENT = "kubeauto-ceph-qualification-ssh"
REGISTRY = "192.168.47.130"
REGISTRY_PORT = 23500
REGISTRY_HOSTS = STORAGE[3:]


def endpoint(target: str, port: int) -> tuple[str, int, str]:
    if (target, port) == (REGISTRY, REGISTRY_PORT):
        return "127.0.0.1", 5000, "kubeauto-ceph-qualification-registry"
    if (target, port) in {(host, 23400 + i) for hosts in TARGETS.values() for i, host in enumerate(hosts)}:
        return target, 22, COMMENT
    raise ValueError("foreign relay target or port")


def execute(argv: list[str], check: bool = True) -> subprocess.CompletedProcess:
    if "-f" in argv:
        # OpenSSH's background master retains output descriptors. Pipes wait
        # for that long-lived child even after the launcher successfully exits.
        with tempfile.TemporaryFile(mode="w+") as errors:
            result = subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                    stderr=errors, text=True, timeout=40)
            errors.seek(0)
            result.stdout, result.stderr = "", errors.read()
            if check:
                result.check_returncode()
            return result
    return subprocess.run(argv, stdin=subprocess.DEVNULL, text=True, capture_output=True,
                          check=check, timeout=40)


def remote(host: str, command: str, check: bool = True) -> subprocess.CompletedProcess:
    if host not in STORAGE:
        raise ValueError("foreign relay host")
    return execute(SSH + ["root@" + host, command], check)


def rule(target: str, port: int, operation: str) -> str:
    _, destination_port, comment = endpoint(target, port)
    if operation not in {"-C", "-A", "-D"}:
        raise ValueError("invalid NAT operation")
    return shlex.join(["iptables", "-w", "5", "-t", "nat", operation, "OUTPUT", "-p", "tcp",
                       "-d", target + "/32", "--dport", str(destination_port), "-m", "comment", "--comment", comment,
                       "-j", "REDIRECT", "--to-ports", str(port)])


def probe(host: str, target: str) -> bool:
    # Read only; neither a connection from the control host nor a route entry
    # proves that the orchestrator's management path can reach its peer.
    if target == REGISTRY:
        command = shlex.join(["curl", "--fail", "--silent", "--show-error", "--noproxy", "*",
                              "--max-time", "5", f"http://{REGISTRY}:5000/v2/"])
        return remote(host, command, check=False).returncode == 0
    if target not in {peer for peers in TARGETS.values() for peer in peers}:
        raise ValueError("foreign probe target")
    command = shlex.join(["timeout", "5", "bash", "-c",
                          f"exec 3<>/dev/tcp/{target}/22; IFS= read -r banner <&3; [[ $banner == SSH-* ]]"])
    return remote(host, command, check=False).returncode == 0


def save(path: Path, rows: list[dict]) -> None:
    temporary = path.with_suffix(".part")
    temporary.write_text(json.dumps(rows) + "\n")
    temporary.replace(path)


def connect(row: dict) -> None:
    forwards = []
    for target, port in row["targets"]:
        destination, destination_port, _ = endpoint(target, port)
        forwards.extend(("-R", f"127.0.0.1:{port}:{destination}:{destination_port}"))
    execute(SSH + ["-M", "-S", row["socket"], "-o", "ExitOnForwardFailure=yes", "-f", "-N"]
            + forwards + ["root@" + row["host"]])


def journal(path: Path) -> list[dict]:
    rows = json.loads(path.read_text())
    # Validate every entry before inspecting or changing any remote resource.
    for row in rows:
        if row["host"] not in STORAGE or row["socket"] != str(path.parent / ("ssh-relay-" + row["host"] + ".sock")):
            raise ValueError("foreign relay journal")
        for target, port in row["targets"]:
            rule(target, port, "-C")
            if target == REGISTRY and row["host"] not in REGISTRY_HOSTS:
                raise ValueError("foreign Registry relay host")
    return rows


def refresh(path: Path) -> None:
    for row in journal(path):
        host = row["host"]
        if execute(SSH + ["-S", row["socket"], "-O", "check", "root@" + host], check=False).returncode == 0:
            continue
        # A lost master leaves its journaled NAT rules behind. Do not alter
        # rules or take over an unjournaled listener while restoring transport.
        for target, port in row["targets"]:
            remote(host, rule(target, port, "-C"))
        socket = Path(row["socket"])
        if socket.exists():
            socket.unlink()
        connect(row)
        for target, _ in row["targets"]:
            if not probe(host, target):
                raise RuntimeError("restored relay connection failed: " + host + " -> " + target)
        print("CEPH_QUALIFICATION_RELAY_RECONNECTED host=" + host, flush=True)


def maintain(path: Path) -> None:
    stopped = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stopped.set())
    signal.signal(signal.SIGINT, lambda *_: stopped.set())
    while not stopped.is_set():
        refresh(path)
        print("CEPH_QUALIFICATION_RELAY_HEARTBEAT", flush=True)
        stopped.wait(10)
    # The runner stops this process before its journal-owned cleanup begins.
    print("CEPH_QUALIFICATION_RELAY_WATCH_STOPPED", flush=True)


def verify_reconnect(path: Path) -> None:
    rows = journal(path)
    if not rows:
        print("CEPH_QUALIFICATION_RELAY_NOT_REQUIRED", flush=True)
        return
    row = rows[0]
    execute(SSH + ["-S", row["socket"], "-O", "exit", "root@" + row["host"]])
    for _ in range(50):
        if not Path(row["socket"]).exists():
            break
        time.sleep(.1)
    if Path(row["socket"]).exists():
        raise RuntimeError("owned relay master did not stop for recovery probe")
    for _, port in row["targets"]:
        remote(row["host"], f"test -z \"$(ss -H -lnt 'sport = :{port}')\"")
    refresh(path)
    print("CEPH_QUALIFICATION_RELAY_RECOVERY_PASS", flush=True)


def setup(path: Path, profile: str) -> None:
    if path.exists():
        raise ValueError("unfinished SSH fixture; clean it before reuse")
    if path.parent.resolve() != Path("/var/lib/kubeauto-ceph-test"):
        raise ValueError("relay requires the fixed runner state directory")
    rows = []
    # Journal ownership before creating any listener or NAT rule. Cleanup is
    # valid at every partial boundary, including interruption during ssh -f.
    save(path, rows)
    for host in STORAGE:
        targets = [(target, 23400 + i) for i, target in enumerate(TARGETS[profile]) if not probe(host, target)]
        if profile == "rocky9" and host in REGISTRY_HOSTS and not probe(host, REGISTRY):
            targets.append((REGISTRY, REGISTRY_PORT))
        if not targets:
            continue
        socket = str(path.parent / ("ssh-relay-" + host + ".sock"))
        if Path(socket).exists():
            raise ValueError("foreign SSH control socket")
        for target, port in targets:
            if remote(host, rule(target, port, "-C"), check=False).returncode == 0:
                raise ValueError("pre-existing relay rule")
        row = {"host": host, "socket": socket, "targets": targets}
        rows.append(row)
        save(path, rows)
        connect(row)
        for target, port in targets:
            remote(host, rule(target, port, "-A"))
            if not probe(host, target):
                raise RuntimeError("relayed qualification connection failed: " + host + " -> " + target)
        marker = "CEPH_QUALIFICATION_REGISTRY_RELAY_PASS" if profile == "rocky9" else "CEPH_QUALIFICATION_SSH_RELAY_PASS"
        print(f"{marker} host={host} targets={len(targets)}", flush=True)
    print("CEPH_QUALIFICATION_MANAGEMENT_NETWORK_PASS", flush=True)


def cleanup(path: Path) -> None:
    if not path.exists():
        return
    rows = journal(path)
    for row in rows:
        host = row["host"]
        for target, port in row["targets"]:
            command = rule(target, port, "-C")
            result = remote(host, command, check=False)
            if result.returncode == 0:
                remote(host, rule(target, port, "-D"))
            elif result.returncode != 1:
                raise RuntimeError("cannot inspect relay rule: " + result.stderr)
            if remote(host, command, check=False).returncode != 1:
                raise RuntimeError("relay rule remains")
        execute(SSH + ["-S", row["socket"], "-O", "exit", "root@" + host], check=False)
        # The mux command acknowledges the request before the master finishes
        # closing channels and removing its socket. Verify eventual shutdown.
        for _ in range(50):
            if not Path(row["socket"]).exists():
                break
            time.sleep(0.1)
        if Path(row["socket"]).exists():
            raise RuntimeError("SSH relay control socket remains")
        for _, port in row["targets"]:
            remote(host, f"test -z \"$(ss -H -lnt 'sport = :{port}')\"")
    path.unlink()
    print("CEPH_QUALIFICATION_SSH_CLEAN_PASS", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("setup", "cleanup", "probe", "maintain"))
    parser.add_argument("--path", type=Path, required=True)
    parser.add_argument("--profile", choices=TARGETS)
    args = parser.parse_args()
    if args.path.name != "ssh-relay.json" or args.path.parent.resolve() != Path("/var/lib/kubeauto-ceph-test"):
        raise ValueError("foreign relay state path")
    if args.mode == "maintain":
        maintain(args.path)
    elif args.mode in {"setup", "probe"}:
        if not args.profile:
            parser.error("setup requires --profile")
        if args.mode == "setup":
            setup(args.path, args.profile)
        else:
            def interrupted(signum, frame):
                raise KeyboardInterrupt("relay probe interrupted")
            signal.signal(signal.SIGTERM, interrupted)
            with open("/var/lock/kubeauto-ceph-priority.lock", "a") as lease, \
                    open("/var/lock/kubeauto-ceph-compute.lock", "a") as compute:
                fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(compute, fcntl.LOCK_EX | fcntl.LOCK_NB)
                # Never clean a previous run's journal from this probe.
                if args.path.exists():
                    raise ValueError("unfinished SSH fixture; clean it before reuse")
                try:
                    setup(args.path, args.profile)
                    verify_reconnect(args.path)
                finally:
                    cleanup(args.path)
            print("CEPH_QUALIFICATION_NETWORK_FOCUSED_PASS", flush=True)
    else:
        cleanup(args.path)


if __name__ == "__main__":
    main()
