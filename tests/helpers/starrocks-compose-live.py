#!/usr/bin/env python3
"""Run only the two StarRocks 3.5.7 Compose products on a leased lab host.

The remote half uses the standard library and a private, disposable Docker
daemon. Registry credentials arrive on stdin and are used only in API headers.
"""
from __future__ import annotations

import argparse
import base64
import contextlib
import fcntl
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import shutil
import shlex
import signal
import socket
import subprocess
import sys
import tarfile
import tempfile
import time
import uuid


ROOT = Path(__file__).resolve().parents[2]
PRODUCT = "tools/starrocks/starrocks-3.5.7"
SECTION = "starrocks_compose"
HOST = "root@192.168.122.216"
SSH = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
       "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=3"]
RUNTIME = Path("/tmp/kubeauto-starrocks-compose-runtime.tar.gz")
COMPOSE_SHA = "dba9d98e1ba5bfe11d88c99b9bd32fc4a0624a30fafe68eea34d61a3e42fd372"
IMAGE_PINS = {
    "allin1": ("starrocks-allin1", "3.5.7"),
    "fe": ("starrocks-fe", "3.5.7"),
    "cn": ("starrocks-cn", "3.5.7"),
    "rustfs": ("rustfs", "1.0.5"),
    "aws-cli": ("aws-cli", "2.27.49"),
}
IDS = {"preflight": "TL-SRC-01", "be": "TL-SRC-02", "cn": "TL-SRC-03", "full": "TL-SRC-04"}


def needed_images(stage):
    return {"allin1"} if stage == "be" else set(IMAGE_PINS) - {"allin1"} if stage == "cn" else set(IMAGE_PINS)


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def run(args, **kwargs):
    return subprocess.run([str(a) for a in args], check=True, **kwargs)


def inputs(stage="full"):
    product = ROOT / PRODUCT
    search = product / ("fe-" + stage) if stage in {"be", "cn"} else product
    files = [p for p in search.rglob("*") if p.is_file() and p.suffix != ".md"]
    files += [Path(__file__), ROOT / "tests/run_tools_regression.sh"]
    if stage == "preflight":
        files += [ROOT / "tests/unit/test_starrocks_compose_contract.py"]
    else:
        for key in needed_images(stage):
            publication = ROOT / f"tests/evidence/starrocks-compose-image-{key}.json"
            if publication.exists():
                files.append(publication)
    return {str(p.relative_to(ROOT)): sha(p) for p in sorted(files)
            if not any(x in p.parts for x in ("__pycache__", "data-be", "data-cn"))
            and (not p.name.startswith((".env.be", ".env.cn")) or p.name.endswith(".example"))}


def matrix():
    import yaml
    data = yaml.safe_load((ROOT / "tests/tools-test-matrix.yaml").read_text())
    section = data[SECTION]
    cases = section["test_cases"]
    if ({c["id"] for c in cases} != set(IDS.values()) or section["product"] != PRODUCT
            or section["host"] != HOST or section["review_status"] != "approved"):
        raise ValueError("Compose matrix scope or review boundary differs")
    for case in cases:
        if case["status"] not in {"pending", "pass", "fail"}:
            raise ValueError("Invalid Compose case status: " + case["id"])
        for field in ("command", "expected", "official", "fixture", "cleanup", "disproof"):
            if not case.get(field):
                raise ValueError(case["id"] + " missing " + field)
    return section


def evidence(case):
    path = ROOT / case.get("evidence", "missing-evidence")
    proof = json.loads(path.read_text())
    if proof["inputs"] != inputs(proof["scope"]) or proof["rc"] != 0:
        raise ValueError(case["id"] + " stale input or exit evidence")
    for name, expected in proof["files"].items():
        if sha(ROOT / name) != expected:
            raise ValueError(case["id"] + " changed evidence: " + name)
    for name in proof.get("durable", []):
        if (ROOT / name).read_text().strip() != "0":
            raise ValueError(case["id"] + " nonzero durable state")
    return proof


def status():
    cases = matrix()["test_cases"]
    for case in cases:
        if case["status"] == "pass":
            evidence(case)
    passed = sum(c["status"] == "pass" for c in cases)
    failed = sum(c["status"] == "fail" for c in cases)
    print(f"矩阵验收 {passed}/{len(cases)} PASS，待验收 {len(cases)-passed} 项（失败 {failed} 项）；能力阶段 {passed}/{len(cases)}")
    for case in cases:
        print(case["id"], case["scenario"], case["status"])


def preflight():
    matrix()
    import yaml
    for route in ("be", "cn"):
        directory = ROOT / PRODUCT / ("fe-" + route)
        data = yaml.safe_load((directory / f"docker-compose-{route}.yml").read_text())
        for service in data["services"].values():
            for dependency in service.get("depends_on", {}):
                if dependency not in data["services"]:
                    raise ValueError("Unknown Compose dependency")
            for mount in service.get("volumes", []):
                source = mount["source"] if isinstance(mount, dict) else mount.split(":")[0]
                if source.startswith("./") and not (directory / source).is_file():
                    raise ValueError("Missing bind source: " + source)
        for script in (directory / "scripts").glob("*.sh"):
            run(["bash", "-n", script])
    print("STARROCKS_COMPOSE_PREFLIGHT_PASS")


def qualify():
    preflight()
    with tempfile.TemporaryDirectory(prefix="starrocks-compose-contract-") as temporary:
        env = dict(os.environ, KUBEAUTO_LOG_DIR=temporary + "/logs", PYTHONPYCACHEPREFIX=temporary + "/pycache")
        result = subprocess.run([sys.executable, "-m", "unittest", "tests.unit.test_starrocks_compose_contract", "-v"],
                                cwd=ROOT, env=env, capture_output=True, text=True)
        if result.returncode:
            print(result.stdout + result.stderr, file=sys.stderr)
            raise ValueError("Focused Compose contract refused live launch")
        compose = Path(temporary) / "compose"
        with tarfile.open(RUNTIME) as archive:
            compose.write_bytes(archive.extractfile("docker-compose").read())
        if sha(compose) != COMPOSE_SHA:
            raise ValueError("Official Compose checksum mismatch")
        compose.chmod(0o755)
        for route in ("be", "cn"):
            source = ROOT / PRODUCT / ("fe-" + route)
            content = (source / f".env.{route}.example").read_text()
            content = content.replace("CN_RUSTFS_ACCESS_KEY=", "CN_RUSTFS_ACCESS_KEY=regression-access")
            content = content.replace("CN_RUSTFS_SECRET_KEY=", "CN_RUSTFS_SECRET_KEY=regression-secret")
            envfile = Path(temporary) / ("env-" + route)
            envfile.write_text(content)
            run([compose, "--env-file", envfile, "-f", source / f"docker-compose-{route}.yml", "config", "--quiet"], capture_output=True)
    directory = ROOT / "logs" / ("starrocks-compose-preflight-" + uuid.uuid4().hex)
    directory.mkdir(mode=0o700)
    log = directory / "contracts.log"
    log.write_text(result.stdout + result.stderr + "STARROCKS_COMPOSE_PREFLIGHT_PASS\n")
    record("preflight", directory, [log])
    print(result.stderr, end="")
    print("STARROCKS_COMPOSE_PREFLIGHT_ACCEPTED")


def admit(stage):
    section = matrix()
    required = [IDS["preflight"]]
    if stage == "full":
        required += [IDS["be"], IDS["cn"]]
    for case in section["test_cases"]:
        if case["id"] in required:
            if case["status"] != "pass":
                raise ValueError("STARROCKS_COMPOSE_PREREQUISITE_REFUSED " + case["id"])
            evidence(case)
    images = section["images"]
    if set(images) != set(IMAGE_PINS):
        raise ValueError("Unexpected image scope")
    for key, (name, tag) in IMAGE_PINS.items():
        if key not in needed_images(stage):
            continue
        record = images[key]
        if record["primary"] != f"hub.talkedu.cn/kubeauto/{name}:{tag}":
            raise ValueError("Wrong primary image")
        if not re.fullmatch(r"sha256:[a-f0-9]{64}", record.get("digest") or ""):
            raise ValueError("Dual-registry artifact evidence missing: " + key)
        if set(record.get("platforms", {})) != {"linux/amd64", "linux/arm64"}:
            raise ValueError("Dual-architecture artifact evidence missing: " + key)
        publication = json.loads((ROOT / record["evidence"]).read_text())
        if publication["image"] != record or publication["conclusion"] != "success":
            raise ValueError("Image publication evidence differs")
        for name, expected in publication["files"].items():
            if sha(ROOT / name) != expected:
                raise ValueError("Image publication artifact changed: " + name)
    return section


def record(stage, directory, files, durable=()):
    import yaml
    proof = {"scope": stage, "inputs": inputs(stage), "rc": 0,
             "files": {str(p.relative_to(ROOT)): sha(p) for p in files},
             "durable": [str(p.relative_to(ROOT)) for p in durable]}
    target = ROOT / "tests/evidence" / f"starrocks-compose-3.5.7-{stage}.json"
    target.write_text(json.dumps(proof, indent=2, ensure_ascii=False) + "\n")
    path = ROOT / "tests/tools-test-matrix.yaml"
    text = path.read_text()
    # Restrict the edit to this explicitly independent matrix section.
    head, body = text.split("\nstarrocks_compose:\n", 1)
    section = yaml.safe_load("starrocks_compose:\n" + body)[SECTION]
    for case in section["test_cases"]:
        if case["id"] == IDS[stage]:
            case.update(status="pass", evidence=str(target.relative_to(ROOT)))
    path.write_text(head + "\n" + yaml.safe_dump({SECTION: section}, sort_keys=False, allow_unicode=True))


def credential():
    fields = {}
    for line in (Path.home() / ".hub.talkedu.cn").read_text().splitlines():
        match = re.match(r"\s*(user|password)\s*[:=]\s*(.*?)\s*$", line)
        if match:
            fields[match[1]] = match[2].strip("\"'")
    if set(fields) != {"user", "password"} or not all(fields.values()):
        raise ValueError("Registry credential labels are missing")
    return fields


def launch(stage):
    section = admit(stage)  # Must refuse before any SSH or remote mutation.
    # The development controller reaches both pool authorities; worker VLANs
    # need not route to them. Keep leases for the complete supervised run.
    with control_leases():
        launch_approved(stage, section)


def launch_approved(stage, section):
    runtime_proof = json.loads(RUNTIME.with_suffix(".json").read_text())
    if sha(RUNTIME) != runtime_proof["archive_sha256"]:
        raise ValueError("Runtime archive changed")
    ident = uuid.uuid4().hex
    remote = "/tmp/kubeauto-starrocks-compose-" + ident
    directory = ROOT / "logs" / ("starrocks-compose-" + stage + "-" + ident)
    directory.mkdir(mode=0o700)
    plan = {"id": ident, "base": remote, "stage": stage, "images": section["images"],
            "runtime": runtime_proof, "input_hashes": inputs(stage)}
    (directory / "plan.json").write_text(json.dumps(plan, indent=2) + "\n")
    run([*SSH, HOST, "mkdir", "-m", "700", remote])
    run(["scp", "-q", "-o", "BatchMode=yes", "-r", ROOT / PRODUCT, HOST + ":" + remote + "/product"])
    for source, name in [(Path(__file__), "gate.py"), (RUNTIME, "runtime.tar.gz"),
                         (directory / "plan.json", "plan.json"),
                         (ROOT / "tests/helpers/run-durable-gate.sh", "durable.sh")]:
        run(["scp", "-q", "-o", "BatchMode=yes", source, HOST + ":" + remote + "/" + name])
    command = (f"cd {remote} || exit; exec 3<&0; nohup bash durable.sh state STARROCKS_COMPOSE_EXIT "
               f"python3 gate.py remote <&3 >console.log 2>&1 &")
    run([*SSH, HOST, command], input=json.dumps(credential()).encode())
    started = time.monotonic()
    try:
        while True:
            result = run([*SSH, HOST, f"tail -n 8 {remote}/console.log; if test -f {remote}/state.finalized; then cat {remote}/state.exit {remote}/state.finalized; fi"], capture_output=True, text=True)
            print(result.stdout, end="", flush=True)
            if result.stdout.splitlines()[-2:] in (["0", "0"],):
                break
            done = subprocess.run([*SSH, HOST, "test", "-f", remote + "/state.finalized"]).returncode == 0
            if done:
                break
            if time.monotonic() - started > 3600:
                raise TimeoutError("Live gate exceeds 3600s; retain durable remote run " + remote)
            time.sleep(15)
    except BaseException:
        cancel_remote(remote)
        for name in ("console.log", "state.exit", "state.finalized", "dockerd.log", "failure-compose.log"):
            subprocess.run(["scp", "-q", "-o", "BatchMode=yes", HOST + ":" + remote + "/" + name, directory / name], check=False)
        raise
    for name in ("console.log", "state.exit", "state.finalized"):
        run(["scp", "-q", "-o", "BatchMode=yes", HOST + ":" + remote + "/" + name, directory / name])
    if (directory / "state.exit").read_text().strip() != "0" or (directory / "state.finalized").read_text().strip() != "0":
        for name in ("dockerd.log", "failure-compose.log", "failure-service-logs.tar.gz", "firewall-before.rules", "firewall-after.rules"):
            subprocess.run(["scp", "-q", "-o", "BatchMode=yes", HOST + ":" + remote + "/" + name, directory / name], check=False)
        raise ValueError("Live gate failed; first failure: " + str(directory / "console.log"))
    run(["scp", "-q", "-o", "BatchMode=yes", HOST + ":" + remote + "/result.json", directory / "result.json"])
    for name in ("firewall-before.rules", "firewall-after.rules"):
        run(["scp", "-q", "-o", "BatchMode=yes", HOST + ":" + remote + "/" + name, directory / name])
    text = (directory / "console.log").read_text()
    markers = ["STARROCKS_COMPOSE_PASS stage=" + stage, "TOOLS_CLEAN_VERIFY_PASS scope=starrocks-compose"]
    if not all(m in text for m in markers) or re.search(r"Traceback|_FAIL(?:ED)?\b|STARROCKS_COMPOSE_EXIT rc=[1-9]", text):
        raise ValueError("Live markers refused")
    if inputs(stage) != plan["input_hashes"]:
        raise ValueError("Product inputs changed during live run; retain original run evidence")
    record(stage, directory, [directory / n for n in ("console.log", "state.exit", "state.finalized", "result.json", "plan.json", "firewall-before.rules", "firewall-after.rules")], [directory / "state.exit", directory / "state.finalized"])
    run([*SSH, HOST, "python3", "-c", "'import shutil; shutil.rmtree(\"" + remote + "\")'"])
    print("STARROCKS_COMPOSE_EVIDENCE_ACCEPTED", IDS[stage], "run_id=" + ident)


def cancel_remote(remote):
    if not re.fullmatch(r"/tmp/kubeauto-starrocks-compose-[a-f0-9]{32}", remote):
        raise ValueError("Refuse cancellation outside owned run")
    script = ("import os,signal; from pathlib import Path; "
              f"base=Path({remote!r}); "
              "pid=int((base/'worker.pid').read_text()); "
              "assert Path('/proc/'+str(pid)+'/cwd').resolve()==base; "
              "os.kill(pid,signal.SIGTERM)")
    subprocess.run([*SSH, HOST, "python3 -c " + shlex.quote(script)], check=False)
    deadline = time.monotonic() + 150
    while time.monotonic() < deadline:
        result = subprocess.run([*SSH, HOST, "test", "-f", remote + "/state.finalized"])
        if result.returncode == 0:
            print("STARROCKS_REMOTE_STOP_FINALIZED", flush=True)
            return
        print("STARROCKS_REMOTE_STOP_WAIT cleanup_pending", flush=True)
        time.sleep(5)
    raise TimeoutError("Remote cleanup did not finalize: " + remote)


class UnixHTTP(http.client.HTTPConnection):
    def __init__(self, socket_path, timeout=900):
        super().__init__("localhost", timeout=timeout)
        self.socket_path = socket_path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.socket_path)


class Worker:
    def __init__(self, plan):
        self.plan = plan
        self.base = Path(plan["base"])
        self.runtime = self.base / "runtime"
        self.work = self.base / "work"
        self.daemon = None
        self.project = None
        self.creds = None
        self.env = dict(os.environ)
        self.env.update(PATH=str(self.runtime) + ":" + os.environ["PATH"],
                        DOCKER_HOST="unix://" + str(self.base / "docker.sock"),
                        DOCKER_CONFIG=str(self.base / "config"))
        self.route_results = {}

    def docker(self, *args, **kwargs):
        return run([self.runtime / "docker", *args], env=self.env, **kwargs)

    def compose(self, *args, **kwargs):
        return run([self.runtime / "docker-compose", "--env-file", self.route / ".env.test",
                    "-f", self.route / self.filename, "-f", self.route / "images.json", *args],
                   env=self.env, **kwargs)

    def sql(self, sql):
        service = "starrocks-be" if self.mode == "be" else "starrocks-fe"
        try:
            return self.compose("exec", "-T", service, "mysql", "--protocol=TCP", "-h", "127.0.0.1", "-P", "9030", "-u", "root", "--batch", "--skip-column-names", input=sql, capture_output=True, text=True).stdout.strip()
        except subprocess.CalledProcessError as error:
            detail = error.stderr or error.stdout or "No client error output"
            secrets = [getattr(self, "password", ""), *(self.creds or {}).values()]
            secrets += [v for k, v in getattr(self, "values", {}).items() if "KEY" in k or "PASSWORD" in k]
            for secret in filter(None, secrets):
                detail = detail.replace(secret, "[redacted]")
            print("STARROCKS_SQL_ERROR rc=" + str(error.returncode) + " " + detail[-4096:], file=sys.stderr, flush=True)
            raise

    def pull(self, key):
        image = self.plan["images"][key]
        ref = image["primary"].rsplit(":", 1)[0] + "@" + image["digest"]
        auth = base64.b64encode(json.dumps({"username": self.creds["user"], "password": self.creds["password"], "serveraddress": "hub.talkedu.cn"}).encode()).decode()
        conn = UnixHTTP(str(self.base / "docker.sock"), timeout=900)
        conn.request("POST", "/v1.51/images/create?fromImage=" + ref, headers={"X-Registry-Auth": auth})
        response = conn.getresponse()
        if response.status != 200:
            detail = response.read(4096).decode(errors="replace")
            for secret in self.creds.values():
                detail = detail.replace(secret, "[redacted]")
            raise ValueError("Registry pull API returned " + str(response.status) + ": " + detail)
        last = 0
        for line in response:
            value = json.loads(line)
            if "error" in value:
                raise ValueError("Registry pull failed for " + key + ": " + value["error"])
            if time.monotonic() - last > 15:
                print("STARROCKS_IMAGE_PULL", key, value.get("id", ""), value.get("status", ""), value.get("progress", ""), flush=True)
                last = time.monotonic()
        conn.close()
        self.docker("image", "inspect", ref, "--format", "{{.Id}}")
        print("STARROCKS_IMAGE_PIN_PASS", key, image["digest"], flush=True)
        return ref

    def prepare(self):
        self.runtime.mkdir()
        self.work.mkdir()
        with tarfile.open(self.base / "runtime.tar.gz") as archive:
            for item in archive:
                if not item.isfile() or item.name not in self.plan["runtime"]["files"] or "/" in item.name:
                    raise ValueError("Unsafe runtime archive member")
                target = self.runtime / item.name
                target.write_bytes(archive.extractfile(item).read())
                target.chmod(0o755)
                if sha(target) != self.plan["runtime"]["files"][item.name]:
                    raise ValueError("Runtime identity differs")
        if sha(self.runtime / "docker-compose") != COMPOSE_SHA:
            raise ValueError("Compose release checksum differs")
        version = run([self.runtime / "dockerd", "--version"], capture_output=True, text=True).stdout
        if "28.5.2" not in version:
            raise ValueError("Unexpected Docker runtime: " + version)
        print("STARROCKS_RUNTIME_PIN_PASS", version.strip(), flush=True)
        self.firewall = self.firewall_rules()
        (self.base / "firewall-before.rules").write_bytes(self.firewall)
        for service in ("docker", "containerd", "kubelet"):
            if subprocess.run(["systemctl", "is-active", "--quiet", service]).returncode == 0:
                raise ValueError("Active foreign runtime: " + service)
        mem = int(re.search(r"MemAvailable:\s+(\d+)", Path("/proc/meminfo").read_text())[1])
        if mem < 12 * 1024 * 1024 or shutil.disk_usage(self.base).free < 35 * 1024**3:
            raise ValueError("Insufficient host capacity")
        for port in (8030, 8040, 9030, 9000, 9001):
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", port))
        log = (self.base / "dockerd.log").open("w")
        self.daemon = subprocess.Popen(self.daemon_args(), env=self.env, stdout=log, stderr=log)
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if self.daemon.poll() is not None:
                raise ValueError("Private Docker daemon exited; inspect dockerd.log")
            if subprocess.run([self.runtime / "docker", "info"], env=self.env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0:
                print("STARROCKS_PRIVATE_DAEMON_READY", flush=True)
                return
            time.sleep(1)
        raise TimeoutError("Private daemon readiness")

    def daemon_args(self):
        # containerd's debug endpoint has a 104-byte Unix socket limit.
        execution = self.base / "e"
        if len(os.fsencode(execution / "containerd/containerd-debug.sock")) > 104:
            raise ValueError("Private runtime socket path exceeds 104 bytes")
        return [self.runtime / "dockerd", "--host", self.env["DOCKER_HOST"],
                "--data-root", str(self.base / "docker-data"), "--exec-root", str(execution),
                "--pidfile", str(self.base / "dockerd.pid"), "--bridge=none", "--iptables=false",
                "--ip6tables=false", "--ip-forward=false", "--ip-masq=false"]

    def setup_route(self, mode, refs):
        self.mode = mode
        self.route = self.work / ("fe-" + mode)
        shutil.copytree(self.base / "product" / ("fe-" + mode), self.route)
        self.filename = "docker-compose-" + mode + ".yml"
        self.project = "sr357-" + self.plan["id"][:12] + "-" + mode
        self.data = self.work / (mode + "-data")
        self.password = uuid.uuid4().hex
        prefix = mode.upper()
        values = {prefix + "_COMPOSE_PROJECT": self.project, prefix + "_DATA_ROOT": str(self.data), prefix + "_ROOT_PASSWORD": ""}
        if mode == "cn":
            values.update(CN_RUSTFS_DATA_ROOT=str(self.data / "rustfs"), CN_RUSTFS_LOG_ROOT=str(self.data / "logs/rustfs"),
                          CN_RUSTFS_ACCESS_KEY="sr" + uuid.uuid4().hex[:18], CN_RUSTFS_SECRET_KEY=uuid.uuid4().hex)
            for path in (self.data / "rustfs/data", self.data / "logs/rustfs"):
                path.mkdir(parents=True, mode=0o750)
                os.chown(path, 10001, 10001)
        self.values = values
        self.write_env()
        services = {"starrocks-be": "allin1"} if mode == "be" else {"starrocks-fe": "fe", "starrocks-init": "fe", "starrocks-cn": "cn", "rustfs": "rustfs", "rustfs-init": "aws-cli"}
        override = {"services": {s: {"image": refs[k], "labels": {"kubeauto.starrocks.run": self.plan["id"]}} for s, k in services.items()},
                    "networks": {"default": {"labels": {"kubeauto.starrocks.run": self.plan["id"]}}}}
        (self.route / "images.json").write_text(json.dumps(override))

    def write_env(self):
        p = self.route / ".env.test"
        p.write_text("\n".join(k + "=" + v for k, v in self.values.items()) + "\n")
        p.chmod(0o600)

    def readback(self):
        result = self.sql("SELECT id,payload FROM compose_acceptance.probe ORDER BY id;")
        if result != "1\tfirst\n2\tsecond\n3\tstream-loaded":
            raise ValueError("Business readback differs: " + repr(result))

    def route_test(self, mode, refs):
        self.setup_route(mode, refs)
        print("STARROCKS_ROUTE_BEGIN route=" + mode, flush=True)
        self.compose("config", "--quiet")
        self.compose("up", "-d", "--pull", "never", "--wait", "--wait-timeout", "360")
        properties = ' PROPERTIES ("replication_num"="1")' if mode == "be" else ""
        self.sql('CREATE DATABASE compose_acceptance; CREATE TABLE compose_acceptance.probe (id BIGINT NOT NULL,payload VARCHAR(64)) DUPLICATE KEY(id) DISTRIBUTED BY HASH(id) BUCKETS 1' + properties + '; INSERT INTO compose_acceptance.probe VALUES (1,"first"),(2,"second");')
        csv = self.work / "probe.tsv"
        csv.write_text("3\tstream-loaded\n")
        result = run(["curl", "-fsS", "--location-trusted", "--resolve", "starrocks-cn:8040:127.0.0.1", "-u", "root:", "-H", "label:regression-" + uuid.uuid4().hex,
                      "-T", csv, "http://127.0.0.1:8030/api/compose_acceptance/probe/_stream_load"], capture_output=True, text=True)
        loaded = json.loads(result.stdout)
        if loaded.get("Status") != "Success" or loaded.get("NumberLoadedRows") != 1:
            raise ValueError("Stream Load failed: " + result.stdout)
        self.readback()
        print("STARROCKS_SQL_STREAM_READBACK_PASS route=" + mode, flush=True)
        if mode == "cn":
            out = self.compose("run", "--rm", "--no-deps", "--entrypoint", "aws", "rustfs-init", "--endpoint-url", "http://rustfs:9000", "s3api", "list-objects-v2", "--bucket", "starrocks", "--query", "length(Contents[])", "--output", "text", capture_output=True, text=True).stdout.strip()
            if not out.isdigit() or int(out) <= 0:
                raise ValueError("No S3 objects after SQL insert")
            print("STARROCKS_S3_OBJECTS_PASS count=" + out, flush=True)
        self.sql("SET PASSWORD FOR 'root'@'%' = PASSWORD('" + self.password + "');")
        self.values[mode.upper() + "_ROOT_PASSWORD"] = self.password
        self.write_env()
        self.compose("up", "-d", "--force-recreate", "--pull", "never", "--wait", "--wait-timeout", "360")
        self.readback()
        print("STARROCKS_RECREATE_PASSWORD_READBACK_PASS route=" + mode, flush=True)
        self.compose("down", "--volumes", "--remove-orphans")
        backup = self.work / (mode + "-backup")
        self.copy_data(self.data, backup)
        shutil.rmtree(self.data)
        self.copy_data(backup, self.data)
        self.compose("up", "-d", "--pull", "never", "--wait", "--wait-timeout", "360")
        self.readback()
        print("STARROCKS_COLD_RESTORE_READBACK_PASS route=" + mode, flush=True)
        self.compose("down", "--volumes", "--remove-orphans")
        self.project = None
        self.route_results[mode] = "pass"
        print("STARROCKS_ROUTE_PASS route=" + mode, flush=True)

    def copy_data(self, source, target):
        # Cold filesystem backups must preserve numeric owners, modes and links.
        for path in (source, target):
            if not path.is_relative_to(self.work) or path.is_symlink():
                raise ValueError("Backup path outside owned work directory")
        run(["cp", "-a", "--", source, target])

    def cleanup(self):
        if self.daemon is not None and self.daemon.poll() is None:
            if self.project:
                self.collect_failure_logs()
                with (self.base / "failure-compose.log").open("w") as log:
                    with contextlib.suppress(Exception):
                        self.compose("logs", "--no-color", "--tail", "160", stdout=log, stderr=log)
                self.compose("down", "--volumes", "--remove-orphans")
            remaining = self.docker("ps", "-aq", capture_output=True, text=True).stdout.strip()
            networks = self.docker("network", "ls", "--filter", "label=kubeauto.starrocks.run=" + self.plan["id"], "-q", capture_output=True, text=True).stdout.strip()
            if remaining or networks:
                raise ValueError("Owned container/network cleanup incomplete")
            self.daemon.terminate()
            self.daemon.wait(timeout=40)
        if hasattr(self, "firewall"):
            after = self.firewall_rules()
            (self.base / "firewall-after.rules").write_bytes(after)
            if after != self.firewall:
                raise ValueError("Host firewall changed")
        if str(self.base) in Path("/proc/self/mountinfo").read_text():
            raise ValueError("Owned mount remains; refuse directory cleanup")
        for path in (self.work, self.runtime, self.base / "docker-data", self.base / "docker-exec", self.base / "e", self.base / "config"):
            if path.exists():
                shutil.rmtree(path)
        print("TOOLS_CLEAN_VERIFY_PASS scope=starrocks-compose", flush=True)

    def collect_failure_logs(self):
        logs = self.data / "logs"
        if logs.exists():
            with tarfile.open(self.base / "failure-service-logs.tar.gz", "w:gz") as archive:
                archive.add(logs, arcname="logs")

    def cleanup_failed_start(self):
        log = (self.base / "dockerd.log").read_text()
        if "unix socket path too long (> 104)" not in log or "failed to start containerd" not in log:
            raise ValueError("Recovery is restricted to the proven pre-network startup failure")
        for process in Path("/proc").glob("[0-9]*/cmdline"):
            try:
                command = process.read_bytes()
            except (FileNotFoundError, ProcessLookupError, PermissionError):
                continue
            if str(self.runtime).encode() in command:
                raise ValueError("Owned runtime process still active; refuse recovery")
        self.firewall = self.firewall_rules()
        self.cleanup()

    @staticmethod
    def firewall_rules():
        return b"\n".join(re.sub(rb"\[\d+:\d+\]$", b"[0:0]", line) if line.startswith(b":") else line
                          for line in run(["iptables-save"], capture_output=True).stdout.splitlines()
                          if not line.startswith(b"#"))


@contextlib.contextmanager
def control_leases():
    processes = []
    try:
        for host in ("root@192.168.47.130", "root@192.168.122.2"):
            command = "exec 9>/var/lock/kubeauto-redis-core-pool.lock; flock -n 9 || exit 2; exec 8>/var/lock/kubeauto-ceph-compute.lock; flock -n 8 || exit 2; echo STARROCKS_POOL_LEASE_READY; read -r release"
            p = subprocess.Popen([*SSH, host, "bash", "-c", "'" + command + "'"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
            processes.append(p)
            if p.stdout.readline().strip() != "STARROCKS_POOL_LEASE_READY":
                raise ValueError("Shared pool lease busy: " + host)
        yield
    finally:
        for p in processes:
            if p.poll() is None:
                p.communicate("release\n", timeout=15)


def remote():
    plan = json.loads(Path("plan.json").read_text())
    if str(Path.cwd()) != plan["base"] or not re.fullmatch(r"[a-f0-9]{32}", plan["id"]):
        raise ValueError("Remote ownership mismatch")
    Path("worker.pid").write_text(str(os.getpid()) + "\n")
    worker = Worker(plan)
    def stop(signum, frame):
        raise InterruptedError("StarRocks gate interrupted by signal " + str(signum))
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    worker.creds = json.load(sys.stdin)
    with open("/var/lock/kubeauto-starrocks-compose.lock", "w") as lease:
        fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            worker.prepare()
            refs = {key: worker.pull(key) for key in sorted(needed_images(plan["stage"]))}
            for mode in (("be", "cn") if plan["stage"] == "full" else (plan["stage"],)):
                worker.route_test(mode, refs)
        except BaseException:
            # Preserve the first failure even if cleanup also needs diagnosis.
            import traceback
            traceback.print_exc()
            try:
                worker.cleanup()
            except Exception:
                print("STARROCKS_CLEANUP_FAILED", file=sys.stderr, flush=True)
                traceback.print_exc()
            raise
        else:
            worker.cleanup()
    Path("result.json").write_text(json.dumps({"id": plan["id"], "stage": plan["stage"], "routes": worker.route_results}, indent=2) + "\n")
    print("STARROCKS_COMPOSE_PASS stage=" + plan["stage"], flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("preflight", "status", "be", "cn", "full", "remote", "remote-clean"))
    args = parser.parse_args()
    if args.action == "remote":
        remote()
    elif args.action == "remote-clean":
        plan = json.loads(Path("plan.json").read_text())
        if str(Path.cwd()) != plan["base"] or not re.fullmatch(r"[a-f0-9]{32}", plan["id"]):
            raise ValueError("Remote recovery ownership mismatch")
        with open("/var/lock/kubeauto-starrocks-compose.lock", "w") as lease:
            fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
            Worker(plan).cleanup_failed_start()
    elif args.action == "preflight":
        qualify()
    elif args.action == "status":
        status()
    else:
        launch(args.action)


if __name__ == "__main__":
    main()
