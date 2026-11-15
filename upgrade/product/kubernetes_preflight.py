#!/usr/bin/env python3
"""Observe a healthy, exact Kubernetes source before a single minor transition.

This read-only subcheck does not authorize mutation or establish UPG-05 PASS.
The caller still owns inventory discovery, leases, backups and combination gates.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from urllib.parse import urlsplit


VERSION = re.compile(r'v(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)')
NODE_NAME = re.compile(r'[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?')


class Refused(ValueError):
    """The observed source cannot satisfy this subcheck."""


def version(value: str) -> tuple[int, int, int]:
    match = VERSION.fullmatch(value)
    if not match:
        raise Refused('an exact upstream stable version vX.Y.Z is required')
    return tuple(map(int, match.groups()))


def endpoint(value: str) -> str:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        raise Refused('invalid API endpoint or port') from None
    if (parsed.scheme != 'https' or not parsed.hostname or port is None or port < 1 or
            parsed.username is not None or parsed.password is not None or
            parsed.path or parsed.query or parsed.fragment or
            any(c.isspace() for c in value)):
        raise Refused('API endpoints require HTTPS, explicit port and no credentials/path')
    host = parsed.hostname.lower()
    return f'https://[{host}]:{port}' if ':' in host else f'https://{host}:{port}'


def query(binary: Path, config: Path, server: str, arguments: list[str]) -> str:
    # No shell, SSH, context switch, insecure TLS or mutating kubectl verb.
    command = [str(binary), '--kubeconfig', str(config), '--server', server,
               '--insecure-skip-tls-verify=false', '--request-timeout=10s', *arguments]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        raise Refused('kubectl could not complete the read-only probe within 20s') from None
    if result.returncode:
        # exec plugins and server errors may contain credentials; never echo them.
        raise Refused(f'kubectl read-only probe failed (rc={result.returncode})')
    return result.stdout


def mapping(raw: str) -> dict:
    try:
        value = json.loads(raw)
    except ValueError:
        raise Refused('kubectl returned invalid JSON') from None
    if not isinstance(value, dict):
        raise Refused('kubectl JSON response must be an object')
    return value


def observe(args) -> dict:
    source, target = version(args.source), version(args.target)
    if target[0] != source[0] or target[1] != source[1] + 1:
        raise Refused('only the next minor is allowed; same minor, downgrade and skip are refused')
    servers = [endpoint(value) for value in args.api_server]
    if len(servers) != len(set(servers)):
        raise Refused('duplicate API backend')
    if (len(args.node) != len(set(args.node)) or
            not all(NODE_NAME.fullmatch(name) for name in args.node)):
        raise Refused('expected node names must be unique Kubernetes names')
    if args.output.exists() or args.output.is_symlink():
        raise Refused('receipt already exists; use a new run-owned output path')
    if not args.output.parent.is_dir() or not args.kubeconfig.is_file():
        raise Refused('existing output directory and kubeconfig are required')
    if (not re.fullmatch(r'[0-9a-f]{64}', args.kubectl_sha256) or
            not args.kubectl.is_file() or
            hashlib.sha256(args.kubectl.read_bytes()).hexdigest() != args.kubectl_sha256):
        raise Refused('kubectl does not match the pinned official SHA256')
    binary = args.kubectl.resolve()
    config = args.kubeconfig.resolve()
    client = mapping(query(binary, config, servers[0], ['version', '--client', '-o', 'json']))
    if client.get('clientVersion', {}).get('gitVersion') != args.target:
        raise Refused('kubectl client version differs from the pinned target')
    backends = []
    cluster_uid = None
    for server in servers:
        print(f'[observe] API backend {server}', flush=True)
        info = mapping(query(binary, config, server, ['get', '--raw=/version']))
        if info.get('gitVersion') != args.source:
            raise Refused('API backend differs from the exact source; mixed source requires journal recovery')
        # HTTP failures become kubectl failures. The readyz body is not a stable
        # machine interface; do not parse verbose checks or exclude etcd.
        query(binary, config, server, ['get', '--raw=/readyz'])
        namespace = mapping(query(binary, config, server,
                                  ['get', 'namespace', 'kube-system', '-o', 'json']))
        metadata = namespace.get('metadata', {})
        uid = metadata.get('uid')
        if (metadata.get('name') != 'kube-system' or
                not isinstance(uid, str) or not uid or metadata.get('deletionTimestamp') or
                namespace.get('status', {}).get('phase') != 'Active'):
            raise Refused('kube-system identity is absent, deleting or not Active')
        if cluster_uid is not None and uid != cluster_uid:
            raise Refused('API backends do not belong to the same observed cluster')
        cluster_uid = uid
        backends.append({'server': server, 'gitVersion': args.source, 'readyz_rc': 0})
    print('[observe] source node identities and readiness', flush=True)
    node_list = mapping(query(binary, config, servers[0], ['get', 'nodes', '-o', 'json']))
    items = node_list.get('items')
    if not isinstance(items, list) or not all(isinstance(item, dict) for item in items):
        raise Refused('invalid Kubernetes node list')
    nodes = []
    expected_schedulable = getattr(args, 'originally_schedulable', None)
    if expected_schedulable is not None and not isinstance(expected_schedulable, dict):
        raise Refused('original scheduling observation must be a complete mapping')
    for item in items:
        metadata = item.get('metadata', {})
        name, uid = metadata.get('name'), metadata.get('uid')
        status = item.get('status', {})
        conditions = status.get('conditions', [])
        ready = [c.get('status') for c in conditions if c.get('type') == 'Ready']
        schedulable = not item.get('spec', {}).get('unschedulable', False)
        if (not isinstance(name, str) or not NODE_NAME.fullmatch(name) or
                not isinstance(uid, str) or not uid or ready != ['True'] or
                metadata.get('deletionTimestamp') or
                (expected_schedulable is None and not schedulable) or
                (expected_schedulable is not None and
                 expected_schedulable.get(name) is not schedulable) or
                status.get('nodeInfo', {}).get('kubeletVersion') != args.source):
            raise Refused('node identity, readiness, scheduling or exact source kubelet differs')
        nodes.append({'name': name, 'uid': uid, 'kubeletVersion': args.source,
                      'Ready': True, 'originally_schedulable': schedulable})
    names = [node['name'] for node in nodes]
    if len(names) != len(set(names)) or set(names) != set(args.node):
        raise Refused('observed nodes differ from the complete expected inventory')
    if len({node['uid'] for node in nodes}) != len(nodes):
        raise Refused('observed node UIDs must be unique')
    return {'schema_version': 1, 'observed_at': datetime.now(timezone.utc).isoformat(),
            'source': args.source, 'target': args.target,
            'scope': 'exact-source-api-and-kubelet-readiness', 'mutation': 'none',
            'parent_acceptance': False, 'kubectl_sha256': args.kubectl_sha256,
            'cluster_identity': {'namespace': 'kube-system', 'uid': cluster_uid},
            'api_backends': backends, 'nodes': sorted(nodes, key=lambda node: node['name']),
            'remaining_gates': ['complete-backend-discovery', 'combination-and-artifact-admission',
                                'controller-and-proxy-identities', 'lease-and-capacity',
                                'business-ledger-and-verified-recovery-point']}


def write_receipt(path: Path, value: dict) -> None:
    # Publish a complete private receipt, exclusively; never replace old evidence.
    with tempfile.NamedTemporaryFile(mode='w', dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        try:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
            os.link(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', required=True)
    parser.add_argument('--target', required=True)
    parser.add_argument('--kubectl', type=Path, required=True)
    parser.add_argument('--kubectl-sha256', required=True)
    parser.add_argument('--kubeconfig', type=Path, required=True)
    parser.add_argument('--api-server', action='append', required=True)
    parser.add_argument('--node', action='append', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        write_receipt(args.output, observe(args))
    except (Refused, OSError, TypeError, AttributeError, KeyError) as error:
        # Input/schema and OS errors fail closed, without raw subprocess output.
        message = str(error) if isinstance(error, Refused) else 'invalid input or receipt I/O failure'
        print(f'UPGRADE_SOURCE_PREFLIGHT_REFUSED: {message}', file=sys.stderr)
        return 1
    print('UPGRADE_SOURCE_PREFLIGHT_OBSERVED_SUCCESS', flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
