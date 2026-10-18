#!/usr/bin/env python3
"""MongoDB's leased lab inputs, immutable artifacts and fixture identities.

The six Ceph-priority hosts are used as compute only. This fixture never opens,
formats or mounts their additional disks, and never consumes a Ceph baseline.
"""
import argparse
import base64
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import secrets
import re
import subprocess
import sys
import tempfile
import time

import yaml

NODES = ('192.168.122.135', '192.168.122.40', '192.168.122.72',
         '192.168.122.212', '192.168.122.165', '192.168.122.238')
CLUSTER = 'mongodb-gate'
NAME = 'mongodb-gate-data'
LABELS = {'app.kubernetes.io/managed-by': 'kubeauto', 'kubeauto.io/component': 'mongodb'}
SYSTEM_ROLES = ('BACKUP', 'DATABASE_ADMIN', 'CLUSTER_ADMIN', 'CLUSTER_MONITOR', 'USER_ADMIN')
HELM_ROOTS = {'Deployment': 'mongodb-operator', 'Role': 'mongodb-operator',
              'RoleBinding': 'service-account-mongodb-operator', 'ServiceAccount': 'mongodb-operator'}
PRODUCT_ROOTS = ({('Secret', NAME + '-' + suffix) for suffix in ('users', 'key', 'tls', 'tls-internal', 'app')} |
                 {('Service', NAME + '-metrics'), ('NetworkPolicy', NAME + '-access')} | set(HELM_ROOTS.items()))


def cleanup_graph(objects, source_uid, claim_uids=(), root_uids=None):
    """Prove every namespace object belongs to the pinned product graph.

    Labels alone do not make an arbitrary workload ours. Controller children
    must reach a recognized root through exact kind/name/UID references.
    """
    source = [o for o in objects if o['kind'] == 'PerconaServerMongoDB' and o['metadata']['name'] == NAME]
    if source and (len(source) != 1 or source[0]['metadata'].get('uid') != source_uid or
            not all(source[0]['metadata'].get('labels', {}).get(k) == v for k, v in LABELS.items())):
        raise ValueError('MongoDB cleanup graph source identity differs from its journal')
    # Resume an interrupted deletion using its historical root UID. This does
    # not permit an unrelated replacement CR with the same name.
    roots = {source_uid} if source_uid and not source else set()
    identities = {source_uid: ('PerconaServerMongoDB', NAME)} if roots else {}
    pending = []
    for obj in objects:
        kind, meta = obj['kind'], obj['metadata']
        name, uid = meta['name'], meta.get('uid')
        if meta.get('namespace') != CLUSTER or not uid or uid in identities:
            raise ValueError('MongoDB cleanup graph has a foreign namespace or duplicate/missing UID')
        identities[uid] = (kind, name)
        labels, annotations = meta.get('labels', {}), meta.get('annotations', {})
        managed = all(labels.get(k) == v for k, v in LABELS.items())
        root = kind == 'PerconaServerMongoDB' and name == NAME and uid == source_uid
        root |= managed and (kind, name) in {
            ('Secret', NAME + '-' + suffix) for suffix in ('users', 'key', 'tls', 'tls-internal', 'app')}
        root |= managed and (kind, name) in {('Service', NAME + '-metrics'), ('NetworkPolicy', NAME + '-access')}
        root |= (kind in HELM_ROOTS and name == HELM_ROOTS[kind] and
                 labels.get('app.kubernetes.io/managed-by') == 'Helm' and
                 annotations.get('meta.helm.sh/release-name') == 'mongodb-operator' and
                 annotations.get('meta.helm.sh/release-namespace') == CLUSTER)
        root |= (kind == 'Secret' and bool(re.fullmatch(r'sh\.helm\.release\.v1\.mongodb-operator\.v[1-9][0-9]*', name)) and
                 labels.get('owner') == 'helm' and labels.get('name') == 'mongodb-operator' and
                 obj.get('type') == 'helm.sh/release.v1')
        root |= (kind == 'Secret' and name == 'internal-' + NAME + '-users' and
                 labels.get('app.kubernetes.io/instance') == NAME and
                 labels.get('app.kubernetes.io/managed-by') == 'percona-server-mongodb-operator')
        # Operator 1.23.1 physical.go:updatePBMConfigSecret deliberately creates
        # this database configuration without an owner reference.
        root |= (bool(source_uid) and kind == 'Secret' and name == NAME + '-pbm-config' and
                 obj.get('type') == 'Opaque' and set(obj.get('data', {})) == {'pbm_config.yaml'} and
                 labels == {'app.kubernetes.io/instance': NAME,
                            'app.kubernetes.io/managed-by': 'percona-server-mongodb-operator',
                            'app.kubernetes.io/name': 'percona-server-mongodb',
                            'app.kubernetes.io/part-of': 'percona-server-mongodb'})
        # Callers supply these only after validating the full PVC/PV claimRef.
        root |= (kind == 'PersistentVolumeClaim' and uid in claim_uids and
                 bool(re.fullmatch('mongod-data-' + NAME + r'-rs0-[0-9]+', name)) and
                 labels.get('app.kubernetes.io/instance') == NAME)
        root |= (kind, name) in {('ServiceAccount', 'default'), ('ConfigMap', 'kube-root-ca.crt')}
        if root:
            if root_uids is not None and (kind, name) in PRODUCT_ROOTS and root_uids.get(kind + '/' + name) != uid:
                raise ValueError('MongoDB cleanup product root UID changed')
            if meta.get('ownerReferences'):
                raise ValueError('MongoDB cleanup root has an unexpected owner')
            roots.add(uid)
        else:
            pending.append(obj)
    children = {'PerconaServerMongoDB': {'StatefulSet', 'ConfigMap', 'Service', 'Secret'},
                'StatefulSet': {'Pod', 'ControllerRevision', 'PodDisruptionBudget'},
                'Deployment': {'ReplicaSet'}, 'ReplicaSet': {'Pod'},
                'Service': {'EndpointSlice', 'Endpoints'}, 'Secret': {'Secret'}}
    while pending:
        remaining = []
        for obj in pending:
            references = obj['metadata'].get('ownerReferences', [])
            endpoint = (obj['kind'] == 'Endpoints' and not references and
                obj['metadata'].get('labels', {}).get('endpoints.kubernetes.io/managed-by') == 'endpoint-controller' and
                any(identities[uid] == ('Service', obj['metadata']['name']) for uid in roots))
            # The fixed manager uses controller-runtime's ownerless leader
            # Lease. Its holder must be a Pod in the verified Helm graph.
            holder = obj.get('spec', {}).get('holderIdentity', '')
            leader = (obj['kind'] == 'Lease' and obj['metadata']['name'] == '08db0feb.percona.com' and
                not references and obj.get('spec', {}).get('leaseDurationSeconds') == 15 and
                bool(re.fullmatch(r'mongodb-operator-[a-z0-9-]+_[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}', holder)) and
                any(p['kind'] == 'Pod' and p['metadata']['name'] == holder.rsplit('_', 1)[0] and
                    p['metadata']['uid'] in roots and any(
                        ref.get('kind') == 'ReplicaSet' and ref.get('uid') in roots
                        for ref in p['metadata'].get('ownerReferences', [])) for p in objects))
            if endpoint or leader or (references and all(ref.get('uid') in roots and
                    identities[ref['uid']] == (ref.get('kind'), ref.get('name')) and
                    obj['kind'] in children.get(ref['kind'], set()) for ref in references)):
                roots.add(obj['metadata']['uid'])
            else:
                remaining.append(obj)
        if len(remaining) == len(pending):
            raise ValueError('MongoDB cleanup graph contains a foreign or disconnected resource: ' +
                             ','.join(o['kind'] + '/' + o['metadata']['name'] for o in remaining))
        pending = remaining
    return roots


def inventory(deployment=CLUSTER):
    if deployment not in (CLUSTER, 'mongodb-restore-logical', 'mongodb-restore-physical', 'mongodb-sharding-gate', 'mongodb-ownership-gate'):
        raise ValueError('MongoDB fixture deployment is outside the runner allowlist')
    lines = []
    for group, nodes in (('etcd', NODES[:3]), ('kube_master', NODES[:3]), ('kube_node', NODES[3:])):
        lines.append('[' + group + ']')
        lines.extend(f"{ip} k8s_nodename='mongodb-node-{ip.rsplit('.', 1)[1]}'" for ip in nodes)
    lines.extend(['[all:vars]', 'SECURE_PORT="6443"', 'CONTAINER_RUNTIME="containerd"',
                  'CLUSTER_NETWORK="calico"', 'PROXY_MODE="ipvs"', 'SERVICE_CIDR="10.85.0.0/16"',
                  'CLUSTER_CIDR="172.29.0.0/16"', 'NODE_PORT_RANGE="30000-32767"',
                  'CLUSTER_DNS_DOMAIN="cluster.local"', 'bin_dir="/usr/local/bin"',
                  'base_dir="/usr/local/kubeauto"', 'cluster_dir="{{ base_dir }}/clusters/' + deployment + '"',
                  'ca_dir="/etc/kubernetes/ssl"', "k8s_nodename=''", 'ansible_user=root'])
    return '\n'.join(lines) + '\n'


def write_private(path, obj):
    with tempfile.NamedTemporaryFile('w', dir=path.parent, delete=False) as stream:
        json.dump(obj, stream, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
        temporary = Path(stream.name)
    temporary.chmod(0o600)
    temporary.replace(path)


def configure(base, core_only=False, upgrade=False):
    directory = base / 'clusters' / CLUSTER
    path = directory / 'config.yml'
    config = yaml.safe_load(path.read_text())
    if config.get('mongodb_operator_ver') != '1.23.1' or config.get('mongodb_ver') != '8.0.32-14':
        raise ValueError('lab configuration must be resolved by kubecli new')
    config.update(K8S_VER='1.33.6', CLUSTER_NAME=CLUSTER, ENABLE_SETTING_HOSTNAME=False,
                  KUBE_RESERVED_ENABLED='no', SYS_RESERVED_ENABLED='no',
                  mongodb_install='no' if core_only else 'yes', mongodb_namespace=CLUSTER, mongodb_name=NAME,
                  mongodb_storage_class='mongodb-longhorn', mongodb_pvc_size='4Gi',
                  mongodb_users_secret=NAME + '-users', mongodb_tls_secret=NAME + '-tls',
                  mongodb_tls_internal_secret=NAME + '-tls-internal', mongodb_encryption_key_secret=NAME + '-key',
                  mongodb_cpu_request='250m', mongodb_memory_request='1Gi',
                  mongodb_cpu_limit='2', mongodb_memory_limit='2Gi', mongodb_cache_size_gb=0.5,
                  mongodb_mongos_cpu_request='100m', mongodb_mongos_memory_request='256Mi',
                  mongodb_mongos_memory_limit='768Mi', mongodb_exporter_cpu_request='50m',
                  mongodb_exporter_memory_request='64Mi', mongodb_ready_timeout=1800,
                  mongodb_network_policy_enabled=True,
                  mongodb_logrotate={'interval_minutes':1,'max_size_mb':32,'retain':1},
                  mongodb_client_selector={'kubeauto.io/mongodb-client': 'true'}, mongodb_client_namespaces=[],
                  mongodb_users=[{'name': 'application', 'db': 'appdb',
                                  'passwordSecretRef': {'name': NAME + '-app', 'key': 'password'},
                                  'roles': [{'name': 'readWrite', 'db': 'appdb'}]}])
    if upgrade:
        if core_only:raise ValueError('MongoDB old-version fixture requires an existing healthy core')
        config.update(mongodb_operator_ver='1.23.0', mongodb_operator_chart_ver='1.23.1',
                      mongodb_ver='8.0.26-11', mongodb_fluentbit_ver='5.0.9-1')
    path.write_text(yaml.safe_dump(config, sort_keys=False))
    path.chmod(0o600)
    directory.joinpath('hosts').write_text(inventory())


def secret(name, values, kind='Opaque'):
    return {'apiVersion': 'v1', 'kind': 'Secret', 'type': kind,
            'metadata': {'name': name, 'namespace': CLUSTER, 'labels': LABELS},
            'stringData': values}


def fixture_cluster_name(cluster_name):
    if cluster_name not in (NAME, 'mongodb-gate-logical-restore', 'mongodb-gate-physical-restore', 'mongodb-gate-sharded'):
        raise ValueError('MongoDB fixture cluster is outside the runner allowlist')
    return cluster_name


def credentials(cluster_name=NAME):
    """Return private fixtures in memory; the CLI sends them only to kubectl stdin."""
    cluster_name = fixture_cluster_name(cluster_name)
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
    now = datetime.now(timezone.utc)
    ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'MongoDB gate CA ' + secrets.token_hex(8))])
    ca = (x509.CertificateBuilder().subject_name(ca_name).issuer_name(ca_name).public_key(ca_key.public_key())
          .serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(minutes=5))
          .not_valid_after(now + timedelta(days=7)).add_extension(x509.BasicConstraints(ca=True, path_length=0), True)
          .sign(ca_key, hashes.SHA256()))
    ca_pem = ca.public_bytes(serialization.Encoding.PEM).decode()
    dns = {'localhost'}
    for service in (cluster_name + '-rs0', cluster_name + '-rs1', cluster_name + '-rs2',
                    cluster_name + '-cfg', cluster_name + '-mongos'):
        for suffix in (CLUSTER, CLUSTER + '.svc', CLUSTER + '.svc.cluster.local'):
            dns.update({service + '.' + suffix, '*.' + service + '.' + suffix})
    objects = []
    for suffix in ('tls', 'tls-internal'):
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cluster_name + '-' + suffix),
                             x509.NameAttribute(NameOID.ORGANIZATION_NAME, 'Kubeauto MongoDB gate'),
                             x509.NameAttribute(NameOID.ORGANIZATIONAL_UNIT_NAME, 'MongoDB')])
        cert = (x509.CertificateBuilder().subject_name(subject).issuer_name(ca_name).public_key(key.public_key())
                .serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(minutes=5))
                .not_valid_after(now + timedelta(days=7)).add_extension(x509.BasicConstraints(ca=False, path_length=None), True)
                .add_extension(x509.SubjectAlternativeName([x509.DNSName(s) for s in sorted(dns)]), False)
                .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH, ExtendedKeyUsageOID.CLIENT_AUTH]), False)
                .sign(ca_key, hashes.SHA256()))
        objects.append(secret(cluster_name + '-' + suffix, {'ca.crt': ca_pem,
            'tls.crt': cert.public_bytes(serialization.Encoding.PEM).decode(),
            'tls.key': key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                         serialization.NoEncryption()).decode()}, 'kubernetes.io/tls'))
    users = {f'MONGODB_{role}_{field}': role.lower().replace('_', '') if field == 'USER' else secrets.token_hex(32)
             for role in SYSTEM_ROLES for field in ('USER', 'PASSWORD')}
    objects += [secret(cluster_name + '-users', users), secret(cluster_name + '-app', {'password': secrets.token_hex(32)}),
                secret(cluster_name + '-key', {'encryption-key': base64.b64encode(secrets.token_bytes(32)).decode()})]
    return {'apiVersion': 'v1', 'kind': 'List', 'items': objects}


def client_object(cluster_name=NAME, *, sharded=False):
    cluster_name = fixture_cluster_name(cluster_name)
    host = ','.join(f'{cluster_name}-rs0-{i}.{cluster_name}-rs0.{CLUSTER}.svc.cluster.local:27017' for i in range(3))
    if sharded:
        host = cluster_name + '-mongos.' + CLUSTER + '.svc.cluster.local:27017'
    topology = '' if sharded else 'replicaSet=rs0&'
    return {'apiVersion': 'v1', 'kind': 'Pod',
            'metadata': {'name': 'mongodb-gate-client', 'namespace': CLUSTER,
                         'labels': LABELS | {'kubeauto.io/fixture': 'mongodb-client', 'kubeauto.io/mongodb-client': 'true'}},
            'spec': {'restartPolicy': 'Never', 'containers': [{
                'name': 'client', 'image': 'registry.talkschool.cn:5000/brinnatt/percona-server-mongodb:8.0.32-14',
                'command': ['/bin/sh', '-c', 'trap "exit 0" TERM INT; sleep 86400 & wait'],
                'env': [{'name': 'MONGODB_URI', 'value': f'mongodb://{host}/admin?{topology}tls=true&tlsCAFile=/tls/ca.crt&serverSelectionTimeoutMS=10000'},
                        {'name': 'APP_PASSWORD', 'valueFrom': {'secretKeyRef': {'name': cluster_name + '-app', 'key': 'password'}}}],
                'envFrom': [{'secretRef': {'name': cluster_name + '-users'}}],
                'volumeMounts': [{'name': 'ca', 'mountPath': '/tls', 'readOnly': True}],
                'resources': {'requests': {'cpu': '50m', 'memory': '128Mi'}, 'limits': {'cpu': '1', 'memory': '512Mi'}}}],
                'volumes': [{'name': 'ca', 'secret': {'secretName': cluster_name + '-tls',
                                                    'items': [{'key': 'ca.crt', 'path': 'ca.crt'}]}}]}}


def publication_records(base, support=False):
    filename = 'mongodb-support-images-20261015.json' if support else 'mongodb-images-20261015.json'
    doc = json.loads((base / 'tests/evidence' / filename).read_text())
    if doc.get('workflow_conclusion') != 'success' or doc.get('durable_rc') != 0 or doc.get('failure_markers') != 0:
        raise ValueError('MongoDB publication was not accepted')
    records = doc['images']
    expected = ({'json-mock:v1.3.1', 'minio:RELEASE.2025-04-08T15-41-24Z',
                 'minio-mc:RELEASE.2025-04-08T15-39-49Z', 'prometheus:v3.13.1-distroless',
                 'kube-state-metrics:v2.18.0'} if support else
                {'mongodb-exporter:0.53.0', 'percona-backup-mongodb:2.15.0',
                 'percona-fluentbit:5.0.9-1', 'percona-fluentbit:5.1.2-1',
                 'percona-server-mongodb-operator:1.23.0', 'percona-server-mongodb-operator:1.23.1',
                 'percona-server-mongodb:8.0.26-11', 'percona-server-mongodb:8.0.32-14'})
    if len(records) != len(expected) or {r['key'] for r in records} != {'brinnatt/' + k for k in expected}:
        raise ValueError('MongoDB production and upgrade image coverage is incomplete')
    for row in records:
        if (row['talkedu_ref'] != 'hub.talkedu.cn/kubeauto/' + row['key'].removeprefix('brinnatt/') or
                row.get('talkedu_digest', row['digest']) != row['digest'] or row.get('dockerhub_digest', row['digest']) != row['digest'] or
                set(row['platforms']) != {'linux/amd64', 'linux/arm64'}):
            raise ValueError('MongoDB publication identity is inconsistent')
    return records


def artifacts(base):
    records = publication_records(base) + publication_records(base, support=True)
    for row in records:
        raw = subprocess.run(['skopeo', 'inspect', '--raw', 'docker://' + row['talkedu_ref']],
                             capture_output=True, timeout=90)
        if raw.returncode or 'sha256:' + hashlib.sha256(raw.stdout).hexdigest() != row['digest']:
            raise ValueError('MongoDB current delivery manifest is unavailable or changed: ' + row['key'])
        manifest = json.loads(raw.stdout)
        platforms = {m['platform']['os'] + '/' + m['platform']['architecture']: m['digest']
                     for m in manifest['manifests'] if m.get('platform', {}).get('os') == 'linux'}
        if any(platforms.get(k) != v for k, v in row['platforms'].items()):
            raise ValueError('MongoDB child manifest identity changed')
        print('MONGODB_ARTIFACT_MANIFEST_PASS image=' + row['key'] + ' digest=' + row['digest'], flush=True)
    print(f'MONGODB_ARTIFACTS_PASS images={len(records)} current-talkedu=true dual-registry-publication=verified', flush=True)


def equivalent_content(actual, expected):
    """Docker pull/push can translate OCI media types without changing content."""
    types = {'application/vnd.oci.image.manifest.v1+json', 'application/vnd.docker.distribution.manifest.v2+json'}
    if actual.get('mediaType') not in types or expected.get('mediaType') not in types:
        return False
    def identities(manifest):
        return [(item['digest'], item['size']) for item in [manifest['config'], *manifest['layers']]]
    return actual.get('schemaVersion') == expected.get('schemaVersion') == 2 and identities(actual) == identities(expected)


def cache_artifacts(base):
    production = publication_records(base)
    support = publication_records(base, support=True)
    for row in production + support:
        expected = row['platforms']['linux/amd64']
        target = 'docker://127.0.0.1:5000/' + ('mongodb-lab/' if row in support else '') + row['key']
        probe = subprocess.run(['skopeo', 'inspect', '--raw', '--tls-verify=false', target],
                               capture_output=True, timeout=60)
        if probe.returncode:
            if not any(v in probe.stderr.lower() for v in (b'manifest unknown', b'name unknown')):
                raise ValueError('MongoDB local immutable cache is unavailable')
            print('MONGODB_CACHE_HEARTBEAT image=' + row['key'] + ' action=copy-fixed-artifact', flush=True)
            for ref in (row['talkedu_ref'], 'docker.io/' + row['dockerhub_ref']):
                source = 'docker://' + ref.rsplit(':', 1)[0] + '@' + expected
                result = subprocess.run(['skopeo', 'copy', '--preserve-digests', '--dest-tls-verify=false', source, target],
                                        capture_output=True, timeout=900)
                if result.returncode == 0:
                    break
            else:
                raise ValueError('MongoDB published artifact cannot be staged: ' + row['key'])
            probe = subprocess.run(['skopeo', 'inspect', '--raw', '--tls-verify=false', target],
                                   capture_output=True, timeout=60)
        if probe.returncode:
            raise ValueError('MongoDB local artifact cache digest differs: ' + row['key'])
        local_digest = 'sha256:' + hashlib.sha256(probe.stdout).hexdigest()
        if local_digest != expected:
            source = 'docker://' + row['talkedu_ref'].rsplit(':', 1)[0] + '@' + expected
            upstream = subprocess.run(['skopeo', 'inspect', '--raw', source], capture_output=True, timeout=90)
            if (upstream.returncode or 'sha256:' + hashlib.sha256(upstream.stdout).hexdigest() != expected or
                    not equivalent_content(json.loads(probe.stdout), json.loads(upstream.stdout))):
                raise ValueError('MongoDB local artifact cache digest differs: ' + row['key'])
            print('MONGODB_CACHE_MEDIA_TRANSLATION_PASS image=' + row['key'] + ' local=' + local_digest +
                  ' published=' + expected + ' config-and-layer-digests=identical', flush=True)
        print('MONGODB_CACHE_PASS image=' + row['key'] + ' digest=' + expected, flush=True)


def core_identity(nodes, namespaces, owner, allow_unready=False):
    if owner.get('cluster') != CLUSTER or tuple(owner.get('nodes', [])) != NODES:
        raise ValueError('MongoDB lab ownership differs from leased nodes')
    seen = set()
    for node in nodes:
        addresses = [a['address'] for a in node['status']['addresses'] if a['type'] == 'InternalIP']
        if len(addresses) != 1 or addresses[0] not in NODES or addresses[0] in seen:
            raise ValueError('MongoDB lab contains foreign/duplicate nodes')
        if node['metadata']['name'] != 'mongodb-node-' + addresses[0].rsplit('.', 1)[1]:
            raise ValueError('MongoDB node name differs from the recorded inventory')
        if node['status']['nodeInfo']['kubeletVersion'] != 'v1.33.6':
            raise ValueError('MongoDB lab Kubernetes version differs from matrix')
        if not allow_unready and not any(c['type'] == 'Ready' and c['status'] == 'True' for c in node['status'].get('conditions', [])):
            raise ValueError('MongoDB lab node is not Ready')
        seen.add(addresses[0])
    if seen != set(NODES):
        raise ValueError('MongoDB lab is missing leased nodes')
    allowed = {'default', 'kube-system', 'kube-public', 'kube-node-lease'}
    for ns in namespaces:
        name = ns['metadata']['name']
        labels = ns['metadata'].get('labels', {})
        if name in allowed:
            continue
        if name == CLUSTER and all(labels.get(k) == v for k, v in LABELS.items()):
            continue
        if name == 'mongodb-lab-storage' and labels.get('kubeauto.io/component') == 'mongodb-test-storage' and labels.get('kubeauto.io/managed-by') == 'kubeauto':
            continue
        raise ValueError('MongoDB dedicated lab contains a foreign namespace')
    uid = next(n['metadata']['uid'] for n in namespaces if n['metadata']['name'] == 'kube-system')
    if owner.get('cluster_uid', uid) != uid:
        raise ValueError('MongoDB lab cluster UID changed')
    return uid


def worker_domains(nodes):
    workers = {'mongodb-node-' + ip.rsplit('.', 1)[1] for ip in NODES[3:]}
    domains = set()
    for node in nodes:
        meta, status = node['metadata'], node['status']
        conditions = {c['type']: c['status'] for c in status.get('conditions', [])}
        if any(conditions.get(k) != 'False' for k in ('MemoryPressure', 'DiskPressure', 'PIDPressure')):
            raise ValueError('MongoDB platform node pressure is present or unknown')
        if meta['name'] not in workers:
            continue
        spec = node['spec']
        if spec.get('unschedulable') or any(t['effect'] in ('NoSchedule', 'NoExecute') for t in spec.get('taints', [])):
            raise ValueError('MongoDB platform worker is not schedulable')
        domain = meta.get('labels', {}).get('kubernetes.io/hostname')
        if not domain or domain in domains:
            raise ValueError('MongoDB platform lacks independent worker hostname domains')
        domains.add(domain)
    if len(domains) != 3:
        raise ValueError('MongoDB platform requires all three leased worker domains')
    return domains


class Lab:
    def __init__(self, base):
        self.base = base
        self.directory = base / 'clusters' / CLUSTER
        self.owner_path = self.directory / 'mongodb-lab-owner.json'
        self.command = ['kubectl', '--kubeconfig=' + str(self.directory / 'kubectl.kubeconfig')]

    def run(self, *args, timeout=60):
        result = subprocess.run([*self.command, *args], capture_output=True, text=True, timeout=timeout)
        if result.returncode:
            raise ValueError('MongoDB lab API command failed; inspect preserved runner diagnostics')
        return result.stdout.strip()

    def get(self, kind, name='', ns='', *, all_namespaces=False):
        if all_namespaces and (ns or name):
            raise ValueError('MongoDB all-namespace query cannot select a namespace or object name')
        args = (['-n', ns] if ns else []) + ['get', kind, *([name, '--ignore-not-found'] if name else []),
            *(['--all-namespaces'] if all_namespaces else []), '-o', 'json']
        result = self.run(*args)
        return json.loads(result) if result else None

    def verify(self, allow_unready=False, independent_namespace=None):
        owner = json.loads(self.owner_path.read_text())
        if allow_unready and (owner.get('healthy') is not False or owner.get('clean_start_verified') is not True or
                              self.directory.joinpath('hosts').read_text() != inventory()):
            raise ValueError('unready core recovery requires the exact clean-start construction journal')
        namespaces = self.get('namespace')['items']
        if independent_namespace is not None:
            # A refusal fixture may own one separate namespace. Accept only
            # its creation receipt, never an arbitrary name or component label.
            name, uid, labels = independent_namespace
            if (not re.fullmatch(r'mongodb-owner-[0-9a-f]{16}', name) or not uid or
                    labels != {'app.kubernetes.io/managed-by':'independent-resource-owner',
                               'kubeauto.io/test-run':name.removeprefix('mongodb-owner-'),
                               'kubernetes.io/metadata.name':name}):
                raise ValueError('MongoDB independent namespace receipt is invalid')
            for namespace in namespaces:
                meta = namespace['metadata']
                if meta['name'] == name and (meta['uid'] != uid or meta.get('labels', {}) != labels):
                    raise ValueError('MongoDB independent namespace creation identity changed')
            namespaces = [n for n in namespaces if n['metadata']['name'] != name]
        if allow_unready and any(ns['metadata']['name'] in (CLUSTER, 'mongodb-lab-storage') for ns in namespaces):
            raise ValueError('unready core recovery refuses an existing database or storage workload')
        uid = core_identity(self.get('nodes')['items'], namespaces, owner, allow_unready=allow_unready)
        owner['cluster_uid'] = uid
        write_private(self.owner_path, owner)
        return owner

    def preflight(self):
        owner = self.verify()
        if owner.get('healthy') is not True or list(self.directory.glob('*fault*.json')):
            raise ValueError('MongoDB preflight requires a healthy owned baseline without faults')
        worker_domains(self.get('nodes')['items'])
        probe = 'import json,time,shutil;print(json.dumps({"now":time.time(),"free":shutil.disk_usage("/").free}))'
        for ip in NODES:
            before = time.time()
            result = subprocess.run(['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10',
                                     'root@' + ip, '/usr/bin/python3', '-'], input=probe,
                                    capture_output=True, text=True, timeout=30)
            if result.returncode:
                raise ValueError('MongoDB preflight host probe failed: ' + ip)
            state = json.loads(result.stdout)
            if not before - 3 <= state['now'] <= time.time() + 3 or state['free'] < 10 * 1024**3:
                raise ValueError('MongoDB preflight clock skew or system-disk headroom is outside the lab budget')
            print('MONGODB_PREFLIGHT_NODE_PASS node=' + ip + ' clock-skew<=3s system-free>=10GiB', flush=True)
        print('MONGODB_PREFLIGHT_SCOPED_CLEAN_PASS read-only=true', flush=True)
        print('MONGODB_PREFLIGHT_PASS owned=true worker-domains=3 pressure=none clock=bounded capacity=bounded', flush=True)

    def reset_incomplete_install(self):
        """Only reset the credential stage, before any database/controller exists."""
        owner = self.verify()
        if owner.get('healthy') is not False or list(self.directory.glob('*fault*.json')):
            raise ValueError('MongoDB incomplete-install reset refuses a healthy or faulted baseline')
        namespace = self.get('namespace', CLUSTER)
        if not namespace or any(namespace['metadata'].get('labels', {}).get(k) != v for k, v in LABELS.items()):
            raise ValueError('MongoDB incomplete-install reset requires the owned namespace')
        for crd in ('perconaservermongodbs', 'perconaservermongodbbackups',
                    'perconaservermongodbrestores', 'perconaservermongodbclustersyncs'):
            if self.get('crd', crd + '.psmdb.percona.com'):
                raise ValueError('MongoDB incomplete-install reset refuses an installed CRD; scoped recovery required')
        objects = self.get('all,pvc,networkpolicy,role,rolebinding,secret,configmap,serviceaccount', ns=CLUSTER)['items']
        for obj in objects:
            kind, meta = obj['kind'], obj['metadata']
            if kind == 'Secret' and meta['name'] in {NAME + '-' + suffix for suffix in ('tls', 'tls-internal', 'users', 'app', 'key')}:
                if all(meta.get('labels', {}).get(k) == v for k, v in LABELS.items()):
                    continue
            elif (kind, meta['name']) in (('ConfigMap', 'kube-root-ca.crt'), ('ServiceAccount', 'default')):
                continue
            raise ValueError('MongoDB incomplete-install reset refuses a foreign or active resource')
        result = subprocess.run([*self.command, 'delete', '--raw', '/api/v1/namespaces/' + CLUSTER, '-f', '-'],
            input=json.dumps({'apiVersion': 'v1', 'kind': 'DeleteOptions',
                              'preconditions': {'uid': namespace['metadata']['uid']}}),
            capture_output=True, text=True, timeout=30)
        if result.returncode:
            raise ValueError('MongoDB incomplete-install namespace deletion failed')
        self.run('wait', '--for=delete', 'namespace/' + CLUSTER, '--timeout=180s', timeout=200)
        if self.get('namespace', CLUSTER):
            raise ValueError('MongoDB incomplete-install namespace has not been removed')
        print('MONGODB_INSTALL_RESET_SCOPED_CLEAN_PASS credential-stage-only=true', flush=True)

    def cleanup_install_client(self):
        owner = self.verify()
        if owner.get('healthy') is not False or list(self.directory.glob('*fault*.json')):
            raise ValueError('MongoDB install-client recovery requires the incomplete owned installation')
        name = 'mongodb-gate-client'
        obj = self.get('pod', name, CLUSTER)
        if not obj:
            return
        desired = client_object()
        meta, spec = obj['metadata'], obj['spec']
        expected = desired['spec']['containers'][0]
        containers = spec.get('containers', [])
        if (any(meta.get('labels', {}).get(k) != v for k, v in desired['metadata']['labels'].items()) or
                'kubeauto.io/test-run' in meta.get('labels', {}) or len(containers) != 1 or
                any(containers[0].get(k) != expected[k] for k in ('name', 'image', 'command', 'env', 'envFrom')) or
                spec.get('restartPolicy') != 'Never'):
            raise ValueError('MongoDB install-client recovery refuses changed fixture identity')
        result = subprocess.run([*self.command, 'delete', '--raw', '/api/v1/namespaces/' + CLUSTER + '/pods/' + name, '-f', '-'],
            input=json.dumps({'apiVersion': 'v1', 'kind': 'DeleteOptions', 'preconditions': {'uid': meta['uid']}}),
            capture_output=True, text=True, timeout=30)
        if result.returncode:
            raise ValueError('MongoDB install-client recovery deletion failed')
        self.run('-n', CLUSTER, 'wait', '--for=delete', 'pod/' + name, '--timeout=120s', timeout=150)
        if self.get('pod', name, CLUSTER):
            raise ValueError('MongoDB install client remains after cleanup')
        print('MONGODB_INSTALL_CLIENT_CLEAN_PASS uid=verified', flush=True)

    def cleanup_product(self, *, qualify_only=False, independent_namespace=None):
        if independent_namespace is not None and not qualify_only:
            raise ValueError('MongoDB independent namespace receipt is read-only qualification only')
        owner = self.verify(independent_namespace=independent_namespace)
        if list(self.directory.glob('*fault*.json')):
            raise ValueError('MongoDB fault journal requires its owning scenario recovery before cleanup')
        # CRDs are cluster-scoped. Prove the external boundary before deleting
        # even the owned namespace; an implicit kubectl namespace is unsafe.
        created_crds = owner.get('created_crds', [])
        allowed_crds = {name + '.psmdb.percona.com' for name in (
            'perconaservermongodbs', 'perconaservermongodbbackups',
            'perconaservermongodbrestores', 'perconaservermongodbclustersyncs')}
        if not isinstance(created_crds, list) or len(set(created_crds)) != len(created_crds) or any(
                crd not in allowed_crds for crd in created_crds):
            raise ValueError('MongoDB cleanup CRD journal is invalid')
        for crd in created_crds:
            current = self.get('crd', crd)
            if current:
                if any(obj['metadata'].get('namespace') != CLUSTER
                       for obj in self.get(crd, all_namespaces=True)['items']):
                    raise ValueError('MongoDB cleanup CRD has surviving consumers in another namespace')
                bound = owner.get('product_identity', {}).get('crds', {}).get(crd, {})
                if bound != {'uid': current['metadata']['uid'],
                             'versions_sha256': hashlib.sha256(json.dumps(current['spec']['versions'], sort_keys=True).encode()).hexdigest()}:
                    raise ValueError('MongoDB cleanup CRD identity or schema changed')
        namespace = self.get('namespace', CLUSTER)
        if namespace:
            claim_uids = set()
            for pvc in self.get('pvc', ns=CLUSTER)['items']:
                labels = pvc['metadata'].get('labels', {})
                if labels.get('app.kubernetes.io/instance') != NAME or pvc['spec'].get('storageClassName') != 'mongodb-longhorn':
                    raise ValueError('MongoDB cleanup refuses a foreign PVC')
                volume_name = pvc['spec'].get('volumeName')
                volume = self.get('pv', volume_name) if volume_name else None
                ref = volume.get('spec', {}).get('claimRef', {}) if volume else {}
                if (not volume or volume['spec'].get('storageClassName') != 'mongodb-longhorn' or
                        (ref.get('namespace'), ref.get('name'), ref.get('uid')) !=
                        (CLUSTER, pvc['metadata']['name'], pvc['metadata']['uid'])):
                    raise ValueError('MongoDB cleanup refuses a missing or rebound volume')
                claim_uids.add(pvc['metadata']['uid'])
            identity = owner.get('product_identity', {})
            if identity.get('namespace_uid') != namespace['metadata'].get('uid'):
                raise ValueError('MongoDB cleanup namespace identity is not journal-bound')
            cleanup_graph(self.namespace_objects(), identity.get('source_uid'), claim_uids, identity.get('roots', {}))
            if qualify_only:
                print('MONGODB_CLEANUP_QUALIFIED_PASS graph=verified external-consumers=absent mutation=none', flush=True)
                return
            if self.get('crd', 'perconaservermongodbs.psmdb.percona.com'):
                current = self.get('psmdb', NAME, CLUSTER)
                if current:
                    if current['metadata']['uid'] != identity.get('source_uid'):
                        raise ValueError('MongoDB cleanup source identity changed')
                    self.delete_uid('/apis/psmdb.percona.com/v1/namespaces/' + CLUSTER +
                                    '/perconaservermongodbs/' + NAME, identity['source_uid'])
                    self.run('-n', CLUSTER, 'wait', '--for=delete', 'psmdb/' + NAME, '--timeout=10m', timeout=650)
            # Recheck the remaining graph immediately before namespace disposal.
            cleanup_graph(self.namespace_objects(), identity.get('source_uid'), claim_uids, identity.get('roots', {}))
            self.delete_uid('/api/v1/namespaces/' + CLUSTER, identity['namespace_uid'])
            self.run('wait', '--for=delete', 'namespace/' + CLUSTER, '--timeout=10m', timeout=650)
        if qualify_only:
            if any(p['spec'].get('storageClassName') == 'mongodb-longhorn' for p in self.get('pv')['items']):
                raise ValueError('MongoDB cleanup qualification has orphaned product volumes')
            print('MONGODB_CLEANUP_QUALIFIED_PASS namespace=absent mutation=none', flush=True)
            return
        deadline = time.monotonic() + 300
        while any(p['spec'].get('storageClassName') == 'mongodb-longhorn' for p in self.get('pv')['items']):
            if time.monotonic() >= deadline:
                raise ValueError('MongoDB cleanup has dependent PVs')
            print('MONGODB_CLEAN_HEARTBEAT stage=owned-pv-release', flush=True)
            time.sleep(3)
        for crd in created_crds:
            if self.get('crd', crd):
                if self.get(crd, all_namespaces=True)['items']:
                    raise ValueError('MongoDB cleanup CRD has surviving consumers')
                self.delete_uid('/apis/apiextensions.k8s.io/v1/customresourcedefinitions/' + crd,
                                owner['product_identity']['crds'][crd]['uid'])
                self.run('wait', '--for=delete', 'crd/' + crd, '--timeout=120s', timeout=150)
        owner.update(healthy=False, product_clean=True)
        write_private(self.owner_path, owner)
        print('MONGODB_PRODUCT_CLEAN_VERIFY_PASS', flush=True)

    def delete_uid(self, endpoint, uid):
        result = subprocess.run([*self.command, 'delete', '--raw', endpoint, '-f', '-'],
            input=json.dumps({'apiVersion': 'v1', 'kind': 'DeleteOptions', 'preconditions': {'uid': uid}}),
            capture_output=True, text=True, timeout=60)
        if result.returncode:
            raise ValueError('MongoDB UID-bound cleanup request failed; preserve runner diagnostics')

    def namespace_objects(self, namespace=CLUSTER):
        resources = self.run('api-resources', '--verbs=list', '--namespaced=true', '-o', 'name').splitlines()
        if not resources or any(not re.fullmatch(r'[a-z0-9][a-z0-9.-]*', resource) for resource in resources):
            raise ValueError('MongoDB cleanup API discovery is incomplete')
        objects = []
        for resource in sorted(set(resources)):
            # Events and virtual metrics are observations, not stored workloads.
            if resource in ('events', 'events.events.k8s.io') or resource.endswith('.metrics.k8s.io'):
                continue
            objects.extend(self.get(resource, ns=namespace)['items'])
        return objects

    def bind_product(self):
        """Persist cleanup roots from construction intent or retained source evidence."""
        owner = self.verify()
        namespace = self.get('namespace', CLUSTER)
        if not namespace:
            return
        if any(namespace['metadata'].get('labels', {}).get(k) != v for k, v in LABELS.items()):
            raise ValueError('MongoDB product identity refuses a foreign namespace')
        source = self.get('psmdb', NAME, CLUSTER) if self.get('crd', 'perconaservermongodbs.psmdb.percona.com') else None
        source_uid = source['metadata']['uid'] if source else None
        if source and any(source['metadata'].get('labels', {}).get(k) != v for k, v in LABELS.items()):
            raise ValueError('MongoDB product identity refuses a foreign source')
        identity = owner.get('product_identity')
        if identity and owner.get('product_clean') is True:
            # A completed retirement may retain its receipt for diagnostics.
            # Only our independently recorded NEW namespace construction can
            # replace it; labels or a same-name replacement are insufficient.
            if (owner.get('namespace_uid') != namespace['metadata']['uid'] or
                    identity.get('namespace_uid') == namespace['metadata']['uid']):
                raise ValueError('MongoDB retired product requires a new construction namespace')
            identity = None
        if identity:
            if (identity.get('namespace_uid') != namespace['metadata']['uid'] or
                    (source and identity.get('source_uid') not in (None, source_uid))):
                raise ValueError('MongoDB product root identity changed')
            if source and identity.get('source_uid') is None:
                if owner.get('namespace_uid') != namespace['metadata']['uid'] or owner.get('healthy') is not False:
                    raise ValueError('MongoDB product source registration requires its original construction intent')
                identity['source_uid'] = source_uid
                write_private(self.owner_path, owner)
            return
        if owner.get('namespace_uid') != namespace['metadata']['uid']:
            # Older healthy baselines predate root journaling. A retained backup
            # records the original source UID; names/labels alone cannot adopt it.
            anchors = set()
            for path in self.directory.glob('mongodb-backup-*.json'):
                state = json.loads(path.read_text())
                if state.get('cluster_uid') == owner['cluster_uid'] and state.get('source_cluster_uid'):
                    anchors.add(state['source_cluster_uid'])
            if not source_uid or anchors != {source_uid}:
                raise ValueError('MongoDB product identity requires original construction or source UID evidence')
        crds = {}
        for name in owner.get('created_crds', []):
            current = self.get('crd', name)
            if current:
                crds[name] = {'uid': current['metadata']['uid'],
                    'versions_sha256': hashlib.sha256(json.dumps(current['spec']['versions'], sort_keys=True).encode()).hexdigest()}
        roots = {}
        for kind, name in sorted(PRODUCT_ROOTS):
            current = self.get(kind.lower(), name, CLUSTER)
            if current:
                roots[kind + '/' + name] = current['metadata']['uid']
        owner['product_identity'] = {'namespace_uid': namespace['metadata']['uid'], 'source_uid': source_uid,
                                     'crds': crds, 'roots': roots}
        owner.pop('product_clean', None)
        write_private(self.owner_path, owner)
        print('MONGODB_PRODUCT_IDENTITY_BOUND namespace-source=uid-verified', flush=True)

    def diagnostics(self, destination):
        if not destination.is_dir() or not destination.name.startswith('kubeauto-mongodb-install.'):
            raise ValueError('MongoDB diagnostics must stay in the private runner directory')
        hidden = []
        for namespace in (CLUSTER, 'mongodb-lab-storage'):
            try:
                for obj in self.get('secrets', ns=namespace)['items']:
                    for value in obj.get('data', {}).values():
                        hidden.append(value)
                        decoded = base64.b64decode(value).decode('utf-8', errors='ignore')
                        if len(decoded) >= 4:
                            hidden.extend((decoded, json.dumps(decoded)[1:-1]))
            except (ValueError, OSError, subprocess.SubprocessError):
                continue
        commands = {'nodes': ['get', 'nodes', '-o', 'wide']}
        for ns in (CLUSTER, 'mongodb-lab-storage'):
            commands.update({ns + '-objects': ['-n', ns, 'get', 'pods,pvc,svc', '-o', 'wide'],
                             ns + '-events': ['-n', ns, 'get', 'events', '--sort-by=.lastTimestamp'],
                             ns + '-pods': ['-n', ns, 'describe', 'pods']})
        commands['operator'] = ['-n', CLUSTER, 'logs', 'deployment/mongodb-operator', '--all-containers', '--tail=200']
        try:
            for pod in self.get('pods', ns=CLUSTER)['items']:
                meta = pod['metadata']
                if meta.get('labels', {}).get('kubeauto.io/component') == 'mongodb-backup-fixture':
                    commands['fixture-' + meta['name']] = ['-n', CLUSTER, 'logs', meta['name'], '--all-containers', '--tail=100']
        except (ValueError, OSError, KeyError, TypeError, subprocess.SubprocessError):
            pass
        for name, command in commands.items():
            result = subprocess.run([*self.command, *command], capture_output=True, text=True, timeout=45)
            output = redact(result.stdout + result.stderr, hidden)
            path = destination / (name + '.txt')
            path.write_text(output)
            path.chmod(0o600)


def redact(text, hidden):
    for value in sorted(set(hidden), key=len, reverse=True):
        if value:
            text = text.replace(value, '[REDACTED]')
    text = re.sub(r'(mongodb(?:\+srv)?://)[^\s/@]+@', r'\1[REDACTED]@', text)
    return re.sub(r'(?i)((?:password|pwd|token|secretAccessKey)["\s:=]+)[^\s,}\"]+', r'\1[REDACTED]', text)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=('configure', 'credentials', 'client', 'artifacts', 'cache', 'verify-core', 'preflight', 'reset-incomplete-install', 'cleanup-install-client', 'bind-product', 'cleanup-qualification', 'cleanup-product', 'diagnostics'))
    parser.add_argument('--base', type=Path, default=Path('/usr/local/kubeauto'))
    parser.add_argument('--core-only', action='store_true')
    parser.add_argument('--upgrade-from-old', action='store_true')
    parser.add_argument('--allow-unready', action='store_true')
    parser.add_argument('--directory', type=Path)
    args = parser.parse_args()
    if args.allow_unready and args.mode != 'verify-core':
        parser.error('--allow-unready is limited to the explicit core recovery identity check')
    if args.mode == 'configure':
        configure(args.base, args.core_only, args.upgrade_from_old)
    elif args.mode in ('credentials', 'client'):
        if sys.stdout.isatty():
            raise ValueError('private fixtures must be piped directly to the owning API client')
        print(json.dumps(credentials() if args.mode == 'credentials' else client_object()))
    elif args.mode == 'artifacts':
        artifacts(args.base)
    elif args.mode == 'cache':
        cache_artifacts(args.base)
    elif args.mode == 'cleanup-product':
        Lab(args.base).cleanup_product()
    elif args.mode == 'bind-product':
        Lab(args.base).bind_product()
    elif args.mode == 'cleanup-qualification':
        Lab(args.base).cleanup_product(qualify_only=True)
    elif args.mode == 'reset-incomplete-install':
        Lab(args.base).reset_incomplete_install()
    elif args.mode == 'cleanup-install-client':
        Lab(args.base).cleanup_install_client()
    elif args.mode == 'preflight':
        Lab(args.base).preflight()
    elif args.mode == 'diagnostics':
        if args.directory is None:
            parser.error('--directory is required')
        Lab(args.base).diagnostics(args.directory)
    else:
        owner = Lab(args.base).verify(allow_unready=args.allow_unready)
        print('MONGODB_CORE_IDENTITY_PASS cluster_uid=' + owner['cluster_uid'], flush=True)


if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError, KeyError, subprocess.SubprocessError) as exc:
        print('MONGODB_LAB_FAIL class=environment-or-supply-chain reason=' + str(exc), file=sys.stderr)
        raise SystemExit(1)
