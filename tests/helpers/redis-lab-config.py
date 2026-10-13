#!/usr/bin/env python3
"""Render fixtures for the dedicated Redis lab; never touch another cluster."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import tempfile

import yaml

NODES = tuple(f'192.168.47.{i}' for i in range(131, 137))
CLUSTER = 'redis-gate'
NAMESPACE = 'redis-gate'
OPERATOR_NAMESPACE = 'redis-gate-operator'


def inventory():
    groups = {'etcd': NODES[3:], 'kube_master': NODES[3:], 'kube_node': NODES[:3]}
    lines = []
    for group, nodes in groups.items():
        lines.append(f'[{group}]')
        lines.extend(f"{ip} k8s_nodename='redis-node-{ip.rsplit('.', 1)[1]}'" for ip in nodes)
    lines.extend(['[all:vars]', 'SECURE_PORT="6443"', 'CONTAINER_RUNTIME="containerd"',
                  'CLUSTER_NETWORK="calico"', 'PROXY_MODE="ipvs"', 'SERVICE_CIDR="10.84.0.0/16"',
                  'CLUSTER_CIDR="172.30.0.0/16"', 'NODE_PORT_RANGE="30000-32767"',
                  'CLUSTER_DNS_DOMAIN="cluster.local"', 'bin_dir="/usr/local/bin"',
                  'base_dir="/usr/local/kubeauto"',
                  'cluster_dir="{{ base_dir }}/clusters/redis-gate"',
                  'ca_dir="/etc/kubernetes/ssl"', "k8s_nodename=''", 'ansible_user=root'])
    return '\n'.join(lines) + '\n'


def configure(path, mode, core_only=False):
    """Only update config produced by kubecli new; keep version resolution real."""
    config = yaml.safe_load(path.read_text())
    if not isinstance(config, dict) or config.get('redis_ver') != '8.10.2':
        raise ValueError('configuration must first be resolved by kubecli new')
    config.update(K8S_VER='1.33.6', CLUSTER_NAME=CLUSTER, KUBE_RESERVED_ENABLED='no',
                  SYS_RESERVED_ENABLED='no', redis_install='no' if core_only else 'yes', redis_mode=mode,
                  redis_namespace=NAMESPACE, redis_operator_namespace=OPERATOR_NAMESPACE,
                  redis_name='redis-gate-data', redis_storage_class='redis-longhorn',
                  redis_pvc_size='2Gi', redis_password_secret='redis-gate-auth',
                  redis_tls_secret='redis-gate-tls', redis_acl_secret='redis-gate-acl',
                  redis_cpu_request='100m', redis_memory_request='256Mi',
                  redis_cpu_limit='1', redis_memory_limit='512Mi',
                  redis_exporter_cpu_request='50m', redis_exporter_memory_request='64Mi',
                  redis_snapshot_requested=False, redis_backup_enabled=False)
    # This smaller lab proves behavior, not the customer's sizing baseline.
    path.write_text(yaml.safe_dump(config, sort_keys=False))
    path.chmod(0o600)
    path.with_name('hosts').write_text(inventory())


def namespace_object(name, component='redis'):
    return {'apiVersion': 'v1', 'kind': 'Namespace', 'metadata': {'name': name, 'labels': {
        'kubeauto.io/component': component, 'kubeauto.io/managed-by': 'kubeauto'}}}


def verify_core_baseline(nodes, namespaces, owner, allow_missing=False):
    """Prove the boundary before resuming the one failed core worker."""
    if owner.get('cluster') != CLUSTER or set(owner.get('nodes', [])) != set(NODES):
        raise ValueError('Redis baseline ownership does not match the fixed pool')
    seen = set()
    for node in nodes:
        ips = [a['address'] for a in node['status']['addresses'] if a['type'] == 'InternalIP']
        if len(ips) != 1 or ips[0] not in NODES or ips[0] in seen:
            raise ValueError('foreign or duplicate node in Redis baseline')
        expected_name = 'redis-node-' + ips[0].rsplit('.', 1)[1]
        if node['metadata']['name'] != expected_name or node['status']['nodeInfo']['kubeletVersion'] != 'v1.33.6':
            raise ValueError('Redis baseline node identity/version changed')
        if not any(c['type'] == 'Ready' and c['status'] == 'True' for c in node['status']['conditions']):
            raise ValueError('unexplained unhealthy node in Redis baseline')
        seen.add(ips[0])
    expected = (set(NODES), set(NODES) - {'192.168.47.133'}) if allow_missing else (set(NODES),)
    if seen not in expected:
        raise ValueError('Redis baseline differs from the narrowly failed worker boundary')
    allowed = {'default', 'kube-system', 'kube-public', 'kube-node-lease'}
    for namespace in namespaces:
        name = namespace['metadata']['name']
        if name in allowed:
            continue
        labels = namespace['metadata'].get('labels', {})
        if name == 'redis-lab-storage' and labels.get('kubeauto.io/component') == 'redis-test-storage' and labels.get('kubeauto.io/managed-by') == 'kubeauto':
            continue
        raise ValueError('foreign workload namespace in dedicated Redis core baseline')
    uid = next(n['metadata']['uid'] for n in namespaces if n['metadata']['name'] == 'kube-system')
    if owner.get('cluster_uid', uid) != uid:
        raise ValueError('Redis baseline cluster UID changed')
    return uid


def secret_object(name, values):
    return {'apiVersion': 'v1', 'kind': 'Secret', 'metadata': {'name': name, 'namespace': NAMESPACE,
        'labels': {'kubeauto.io/component': 'redis-test', 'kubeauto.io/managed-by': 'kubeauto'}},
        'type': 'Opaque', 'stringData': values}


def credentials(base_name="redis-gate-data"):
    """Private fixture material travels only through the runner's stdin pipe."""
    import secrets
    if not re.fullmatch(r'[a-z0-9]([-a-z0-9]*[a-z0-9])?', base_name) or len(base_name) > 63:
        raise ValueError('fixture certificate requires a valid Redis resource name')
    password, app_password = secrets.token_hex(32), secrets.token_hex(32)
    with tempfile.TemporaryDirectory(prefix='redis-gate-tls-') as tmp:
        root = Path(tmp)

        def openssl(*argv):
            subprocess.run(['openssl', *argv], cwd=root, check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        openssl('req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '2',
                '-subj', '/CN=Redis gate CA ' + secrets.token_hex(8), '-keyout', 'ca.key', '-out', 'ca.crt')
        openssl('req', '-newkey', 'rsa:2048', '-nodes', '-subj', '/CN=' + base_name,
                '-keyout', 'tls.key', '-out', 'tls.csr')
        (root / 'extensions').write_text(
            'basicConstraints=CA:FALSE\nkeyUsage=digitalSignature,keyEncipherment\n'
            'subjectKeyIdentifier=hash\nauthorityKeyIdentifier=keyid:always,issuer\n'
            'extendedKeyUsage=serverAuth,clientAuth\n'
            'subjectAltName=DNS:localhost,DNS:*.redis-gate.svc.cluster.local,'
            f'DNS:*.redis-gate.svc,DNS:{base_name}-master,IP:127.0.0.1,'
            f'DNS:*.{base_name}-headless.redis-gate.svc.cluster.local,'
            f'DNS:*.{base_name}-sentinel-sentinel-headless.redis-gate.svc.cluster.local,'
            f'DNS:*.{base_name}-leader-headless.redis-gate.svc.cluster.local,'
            f'DNS:*.{base_name}-follower-headless.redis-gate.svc.cluster.local\n')
        openssl('x509', '-req', '-in', 'tls.csr', '-CA', 'ca.crt', '-CAkey', 'ca.key',
                '-CAcreateserial', '-days', '2', '-extfile', 'extensions', '-out', 'tls.crt')
        digest = lambda value: hashlib.sha256(value.encode()).hexdigest()
        acl = (f'user default on #{digest(password)} ~* &* +@all\n'
               f'user gate-app on #{digest(app_password)} ~kubeauto:redis:* '
               '+ping +get +set +ttl +pttl +del +exists +type +multi +exec +discard +watch +unwatch\n')
        return [secret_object('redis-gate-auth', {'password': password, 'app-password': app_password}),
                secret_object('redis-gate-acl', {'user.acl': acl}),
                secret_object('redis-gate-tls', {key: (root / key).read_text()
                                               for key in ('ca.crt', 'tls.crt', 'tls.key')})]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=('configure', 'credentials', 'inventory', 'verify-core'))
    parser.add_argument('--config', type=Path)
    parser.add_argument('--redis-mode', choices=('sentinel', 'cluster'), default='sentinel')
    parser.add_argument('--core-only', action='store_true')
    parser.add_argument('--nodes-json', type=Path)
    parser.add_argument('--namespaces-json', type=Path)
    parser.add_argument('--owner-json', type=Path)
    parser.add_argument('--allow-missing-worker', action='store_true')
    args = parser.parse_args()
    if args.mode == 'configure':
        if not args.config or args.config.parent.name != CLUSTER or args.config.name != 'config.yml':
            parser.error('only clusters/redis-gate/config.yml is owned by this fixture')
        configure(args.config, args.redis_mode, args.core_only)
        print('REDIS_LAB_CONFIG_PASS kubernetes=1.33.6 resources=lab-only')
    elif args.mode == 'verify-core':
        uid = verify_core_baseline(json.loads(args.nodes_json.read_text())['items'],
                                   json.loads(args.namespaces_json.read_text())['items'],
                                   json.loads(args.owner_json.read_text()), args.allow_missing_worker)
        print('REDIS_CORE_BASELINE_VERIFIED uid=' + uid)
    elif args.mode == 'credentials':
        print(json.dumps({'apiVersion': 'v1', 'kind': 'List', 'items': credentials()}))
    else:
        print(inventory(), end='')


if __name__ == '__main__':
    main()
