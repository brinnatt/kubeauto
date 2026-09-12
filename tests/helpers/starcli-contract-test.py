#!/usr/bin/env python3
"""Focused contracts for the standalone StarCli entry point.

This gate deliberately does not fake a StarRocks server: it proves argument,
configuration, command construction and safety contracts before any live
fixture is allowed to mutate an authorized host.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "tools/starrocks/StarCli.py"
spec = importlib.util.spec_from_file_location("starcli_standalone", SCRIPT)
assert spec and spec.loader
star = importlib.util.module_from_spec(spec)
spec.loader.exec_module(star)


def expect(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def cli(*args: str) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env.setdefault("PYTHONPATH", str(ROOT))
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )


def main() -> int:
    v = star.InputValidator
    for port in (1, 22, 65535):
        expect(v.validate_port(port), f"valid port rejected: {port}")
    for port in (0, -1, 65536, 1.5):
        expect(not v.validate_port(port), f"invalid port accepted: {port}")
    for host in ("127.0.0.1", "node-1.example"):
        expect(v.validate_hostname(host), f"valid host rejected: {host}")
    for host in ("127.0.0.1;touch /tmp/starcli-injected", "node$(id)", "bad host", ""):
        expect(not v.validate_hostname(host), f"unsafe host accepted: {host}")
    expect(v.validate_ip_or_cidr("10.0.0.0/24"), "CIDR rejected")
    expect(not v.validate_ip_or_cidr("10.0.0.1;id"), "CIDR injection accepted")
    expect(v.validate_storage_path("/data1,medium:HDD;/data2,medium:SSD;/data3"), "storage list rejected")
    expect(not v.validate_storage_path("../outside"), "storage traversal accepted")

    fe = star.ConfigGenerator.generate_fe_config(
        "/var/lib/starrocks/fe/meta", http_port=18030, query_port=19030,
        priority_networks="10.0.0.0/24", java_home="/usr/lib/jvm/java-17",
        default_replication_num=1,
    )
    expect("meta_dir = /var/lib/starrocks/fe/meta" in fe, "FE meta missing")
    expect("http_port = 18030" in fe and "query_port = 19030" in fe, "FE ports missing")
    expect("priority_networks = 10.0.0.0/24" in fe, "priority network missing")
    expect("default_replication_num = 1" in fe, "replication setting missing")
    expect(len({18030, 19020, 19030, 19010}) == 4, "FE port fixture collision")
    be = star.ConfigGenerator.generate_be_config(
        "/data1,medium:HDD;/data2,medium:SSD", be_port=19060,
        priority_networks="10.0.0.0/24", java_home="/usr/lib/jvm/java-17",
    )
    expect("storage_root_path = /data1,medium:HDD;/data2,medium:SSD" in be, "BE storage missing")
    expect("be_port = 19060" in be and "priority_networks = 10.0.0.0/24" in be, "BE settings missing")
    cn = star.ConfigGenerator.generate_cn_config(brpc_port=19061)
    expect("brpc_port = 19061" in cn and "CN Ports" in cn, "CN settings missing")

    for raw, expected in (("node.example", ("node.example", 22)), ("node.example:2200", ("node.example", 2200))):
        expect(star._parse_target_host(raw, 22) == expected, f"target parse mismatch: {raw}")
    for raw in ("node.example:0", "node.example:65536", "node;id", "node.example:x", "[bad"):
        try:
            star._parse_target_host(raw, 22)
        except ValueError:
            pass
        else:
            raise AssertionError(f"unsafe target accepted: {raw}")
    expect(star._is_local_host("127.0.0.1"), "loopback not local")
    expect(not star._is_local_host("192.168.122.243"), "remote host treated as local")

    original = ["StarCli.py", "--deploy", "be", "--config", "/tmp/local.json", "--target-host", "192.168.122.217"]
    command = star._build_remote_command(original, "/tmp/remote/config.json", "/tmp/remote/starrocks.py")
    expect("/tmp/remote/starrocks.py" in command and "/tmp/remote/config.json" in command, "remote command paths missing")
    expect("--target-host" not in command, "target-host leaked into remote command")
    expect("/tmp/local.json" not in command, "local config leaked into remote command")
    expect(";" not in star._build_remote_command(["StarCli.py", "--deploy", "fe", "--starrocks-home", "/tmp/a b"], None, None), "command quoting missing")

    with tempfile.TemporaryDirectory(prefix="starcli-contract-") as td:
        config = Path(td) / "config.json"
        config.write_text(json.dumps({"deploy": "fe", "starrocks_home": "/opt/starrocks"}), encoding="utf-8")
        expect(star.load_json_config(str(config))["deploy"] == "fe", "JSON config load failed")
        malformed = Path(td) / "bad.json"
        malformed.write_text("[]", encoding="utf-8")
        try:
            star.load_json_config(str(malformed))
        except SystemExit:
            pass
        else:
            raise AssertionError("non-object JSON accepted")

    # Cleanup must target the configured heartbeat port and support a local
    # loopback FE, matching the deployment path used by single-host installs.
    for role, port, header in (
        ("be", 18050, "BackendId IP HeartbeatPort"),
        ("cn", 18052, "ComputeNodeId IP HeartbeatPort"),
    ):
        with tempfile.TemporaryDirectory(prefix=f"starcli-{role}-cleanup-") as td:
            home = Path(td)
            conf = home / "be" / "conf"
            conf.mkdir(parents=True)
            (conf / f"{role}.conf").write_text(
                f"heartbeat_service_port = {port}\n", encoding="utf-8"
            )
            deployer = star.StarRocksDeployer(str(home), user="root", group="root")
            deployer._get_local_ip = lambda _host, _query: "127.0.0.1"
            output = f"{header}\n1 127.0.0.1 {port} true\n"
            deployer._execute_sql = lambda _host, _query, _sql, password=None: (True, output)
            removed = []

            def record_remove(_host, _query, _ip, node_port, node_role, force=False, password=None):
                removed.append((node_port, node_role))
                return True

            deployer._remove_node_from_cluster = record_remove
            expect(
                deployer._remove_be_cn_from_cluster_before_cleanup(
                    role, "127.0.0.1", 19030, "secret"
                ),
                f"{role} cleanup rejected local loopback",
            )
            expect(removed == [(port, role)], f"{role} cleanup ignored configured heartbeat port")

    with tempfile.TemporaryDirectory(prefix="starcli-status-") as td:
        deployer = star.StarRocksDeployer(td, user="root", group="root")
        deployer._execute_sql = lambda *_args, **_kwargs: (False, "simulated query failure")
        expect(not deployer.show_cluster_status("127.0.0.1", 19030, "secret"),
               "status reported success after SQL query failure")

    help_result = cli("--help")
    expect(help_result.returncode == 0 and "--deploy TYPE" in help_result.stdout, "help contract failed")
    for args in (("--deploy", "invalid"), ("--deploy", "fe"), ("--status", "--fe-host", "bad;id"),
                 ("--deploy", "fe", "--starrocks-home", "/tmp/x", "--helper-address", "bad")):
        result = cli(*args)
        expect(result.returncode != 0, f"invalid CLI unexpectedly succeeded: {args}")
    secret = "starcli-contract-secret"
    result = cli("--status", "--fe-host", "127.0.0.1", "--root-password", secret)
    expect(secret not in result.stdout + result.stderr, "root password leaked in CLI output")

    print("STARCLI_CONTRACT_PASS cases=37")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
