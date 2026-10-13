#!/usr/bin/env python3
"""Export independently verified RDB files without writing to source PVCs.

Cluster exports are per-instance snapshots, never a global transactional backup.
The caller owns offsite retention and an independent restore verification.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import uuid

AUTH_CLI_PREFIX = [
    'sh', '-c',
    'REDISCLI_AUTH="${REDISCLI_AUTH:-${REDIS_PASSWORD:-}}"; '
    'if [ -n "${REDISCLI_AUTH:-}" ]; then '
    'REDISCLI_AUTH="$(printf %s "$REDISCLI_AUTH" | tr -d "\\r\\n")"; '
    'export REDISCLI_AUTH; fi; exec redis-cli "$@"', 'redis-cli',
]


def seed_service(name, mode):
    # v0.27.0 creates a replication master Service and a Cluster leader Service.
    return name + ('-leader' if mode == 'cluster' else '-master')


def publish_file(directory, filename, producer, expected_digest):
    """Keep an incomplete/incorrect transfer from becoming a backup artifact."""
    target = directory / filename
    if target.exists():
        raise ValueError("refusing to overwrite an existing snapshot")
    temporary = directory / (filename + ".part")
    try:
        with temporary.open("xb") as out:
            os.chmod(temporary, 0o600)
            producer(out)
            out.flush()
            os.fsync(out.fileno())
        checksum = hashlib.sha256()
        with temporary.open("rb") as stream:
            header = stream.read(5)
            stream.seek(0)
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                checksum.update(chunk)
        digest = checksum.hexdigest()
        if temporary.stat().st_size < 16 or header != b"REDIS" or digest != expected_digest:
            raise ValueError("RDB transfer header, size or digest mismatch")
        # link() refuses a concurrently created target instead of overwriting it.
        os.link(temporary, target)
        descriptor = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        return {"file": filename, "size": target.stat().st_size, "sha256": digest}
    finally:
        temporary.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kubeconfig", required=True)
    parser.add_argument("--namespace", required=True)
    parser.add_argument("--name", required=True)
    parser.add_argument("--mode", choices=("sentinel", "cluster"), required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--password-secret", required=True)
    parser.add_argument("--password-key", default="password")
    parser.add_argument("--tls-secret", required=True)
    parser.add_argument("--directory", required=True)
    args = parser.parse_args()
    for value in (args.namespace, args.name, args.password_secret, args.tls_secret):
        if not re.fullmatch(r"[a-z0-9]([-a-z0-9]*[a-z0-9])?", value):
            parser.error("invalid Kubernetes name")
    if not re.fullmatch(r'[A-Za-z0-9._-]+', args.password_key):
        parser.error('invalid Secret key')
    if not re.fullmatch(r"registry\.talkschool\.cn:5000/brinnatt/redis:8\.[0-9]+\.[0-9]+(?:-kubeauto\.[1-9][0-9]*)?", args.image):
        parser.error("snapshot must use the pinned product Redis image")
    directory = Path(args.directory)
    if not directory.is_absolute() or directory == Path('/'):
        parser.error('snapshot requires a new absolute request directory')
    pod = "redis-snapshot-" + uuid.uuid4().hex[:12]
    kubectl = ["kubectl", "--kubeconfig=" + args.kubeconfig, "-n", args.namespace]
    tls = ["--tls", "--cacert", "/tls/ca.crt", "--cert", "/tls/tls.crt", "--key", "/tls/tls.key"]
    redis_cli = [*AUTH_CLI_PREFIX, "-e", "--raw", "--no-auth-warning", *tls]
    started = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    def run(argv, *, input_text=None, timeout=60):
        result = subprocess.run(kubectl + argv, input=input_text, text=True, capture_output=True, timeout=timeout)
        if result.returncode:
            raise ValueError("Kubernetes/Redis command failed; inspect the source and snapshot Pod")
        return result.stdout.strip()

    def execute(argv, timeout=60):
        return run(["exec", pod, "--", *argv], timeout=timeout)

    source_ns = json.loads(run(["get", "namespace", args.namespace, "-o", "json"]))
    labels = source_ns["metadata"].get("labels", {})
    if labels.get("kubeauto.io/component") != "redis" or labels.get("kubeauto.io/managed-by") != "kubeauto":
        raise ValueError("refusing to snapshot a foreign Redis namespace")
    directory.mkdir(mode=0o700, parents=True, exist_ok=False)
    spec = {"apiVersion": "v1", "kind": "Pod", "metadata": {"name": pod, "labels": {
        "kubeauto.io/component": "redis-snapshot", "kubeauto.io/managed-by": "kubeauto"}},
        "spec": {"restartPolicy": "Never", "automountServiceAccountToken": False,
                 "securityContext": {"runAsUser": 999, "runAsGroup": 999, "fsGroup": 999},
                 "containers": [{"name": "client", "image": args.image,
                                 "command": ["sh", "-c", "sleep 86400"],
                                 "resources": {"requests": {"cpu": "100m", "memory": "128Mi"},
                                               "limits": {"cpu": "1", "memory": "512Mi"}},
                                 "securityContext": {"allowPrivilegeEscalation": False,
                                                     "capabilities": {"drop": ["ALL"]}},
                                 "env": [{"name": "REDISCLI_AUTH", "valueFrom": {"secretKeyRef": {
                                     "name": args.password_secret, "key": args.password_key}}}],
                                 "volumeMounts": [{"name": "tls", "mountPath": "/tls", "readOnly": True},
                                                  {"name": "work", "mountPath": "/work"}]}],
                 "volumes": [{"name": "tls", "secret": {"secretName": args.tls_secret}},
                             {"name": "work", "emptyDir": {}}]}}
    files = []
    failure = None
    try:
        run(["create", "-f", "-"], input_text=json.dumps(spec))
        run(["wait", "--for=condition=Ready", "pod/" + pod, "--timeout=180s"], timeout=200)
        seed = seed_service(args.name, args.mode)
        topology_before = None
        if args.mode == "sentinel":
            sources = [{"id": "replication", "host": seed, "slots": []}]
        else:
            topology = execute([*redis_cli, "-h", seed, "CLUSTER", "NODES"])
            sources = []
            for line in topology.splitlines():
                row = line.split()
                flags = row[2].split(",")
                if "master" in flags:
                    if not re.fullmatch(r"[0-9a-f]{40}", row[0]):
                        raise ValueError("invalid Cluster node ID")
                    if any(f in flags for f in ("fail", "fail?", "handshake", "noaddr")) or row[7] != "connected":
                        raise ValueError("Cluster primary is not healthy")
                    if any(slot.startswith("[") for slot in row[8:]):
                        raise ValueError("snapshot refused while slots are migrating")
                    # IPv4/DNS delivery path; IPv6 is not silently misparsed.
                    host = row[1].split("@")[0].rsplit(":", 1)[0]
                    sources.append({"id": row[0], "host": host, "slots": row[8:]})
            if len(sources) < 3:
                raise ValueError("Cluster has fewer than three primary shards")
            topology_before = sorted((s['id'], s['host'], tuple(s['slots'])) for s in sources)
        for index, source in enumerate(sources, 1):
            print(f"REDIS_SNAPSHOT_STAGE index={index} total={len(sources)} source={source['id']}", flush=True)
            remote_file = "/work/snapshot.rdb"
            role = execute([*redis_cli, '-h', source['host'], 'ROLE']).splitlines()
            if not role or role[0] != 'master':
                raise ValueError('snapshot source is no longer a primary')
            identity_before = execute([*redis_cli, '-h', source['host'], 'INFO', 'server'])
            run_id = next(line for line in identity_before.splitlines() if line.startswith('run_id:'))
            execute([*redis_cli, "-h", source["host"], "--rdb", remote_file], timeout=1800)
            execute(["redis-check-rdb", remote_file], timeout=300)
            expected = execute(["sha256sum", remote_file]).split()[0]

            def producer(out):
                result = subprocess.run(kubectl + ["exec", pod, "--", "cat", remote_file],
                                        stdout=out, stderr=subprocess.PIPE, timeout=1800)
                if result.returncode:
                    raise ValueError("RDB transfer failed")

            record = publish_file(directory, source["id"] + ".rdb", producer, expected)
            identity_after = execute([*redis_cli, '-h', source['host'], 'INFO', 'server'])
            if run_id not in identity_after.splitlines() or execute(
                    [*redis_cli, '-h', source['host'], 'ROLE']).splitlines()[0] != 'master':
                raise ValueError('snapshot primary changed or restarted during export')
            files.append({**source, **record})
            execute(["rm", "--", remote_file])
        if topology_before is not None:
            current = []
            for line in execute([*redis_cli, '-h', seed, 'CLUSTER', 'NODES']).splitlines():
                row = line.split()
                if 'master' in row[2].split(','):
                    if row[7] != 'connected' or any(s.startswith('[') for s in row[8:]):
                        raise ValueError('Cluster primary or slots changed during snapshot')
                    current.append((row[0], row[1].split('@')[0].rsplit(':', 1)[0], tuple(row[8:])))
            if sorted(current) != topology_before:
                raise ValueError('Cluster topology changed during snapshot')
        image_id = json.loads(run(['get', 'pod', pod, '-o', 'json']))['status']['containerStatuses'][0]['imageID']
        manifest = {"format": 1, "mode": args.mode, "image": args.image, "source": args.name,
                    "started": started, "completed": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "consistency": "per-instance; Cluster global consistency requires externally quiesced writes",
                    "files": files, "image_id": image_id, "restore_verified": False}
        with (directory / "manifest.json.part").open("x") as out:
            os.chmod(directory / 'manifest.json.part', 0o600)
            json.dump(manifest, out, indent=2)
            out.flush()
            os.fsync(out.fileno())
        (directory / "manifest.json.part").rename(directory / "manifest.json")
        descriptor = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except BaseException as exc:
        failure = exc
        raise
    finally:
        # A new random name belongs exclusively to this invocation; no source CR/PVC is deleted.
        try:
            run(["delete", "pod", pod, "--ignore-not-found", "--wait=true", "--timeout=120s"], timeout=140)
        except (ValueError, OSError, subprocess.TimeoutExpired):
            if failure is None:
                raise
            print('REDIS_SNAPSHOT_CLEANUP_FAIL; original export failure retained', file=sys.stderr)
    print(f"REDIS_SNAPSHOT_EXPORTED files={len(files)} directory={directory}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
