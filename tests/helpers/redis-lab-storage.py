#!/usr/bin/env python3
"""Dedicated Redis CSI fixture with fixed artifacts and bounded cleanup.

Pinned official Chart/source: https://github.com/longhorn/longhorn/releases/tag/v1.10.1
The vendored uninstall hook runs longhorn-manager uninstall after the deletion confirmation.
The enclosing Redis runner owns the host lease and dedicated six-node cluster.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tarfile
import tempfile

import yaml

NODES = {f'192.168.47.{i}' for i in range(131, 137)}
WORKERS = {f'192.168.47.{i}' for i in range(131, 134)}
NAMESPACE = 'redis-lab-storage'
RELEASE = 'redis-lab-longhorn'
STORAGE_CLASS = 'redis-longhorn'
DATA_PATH = '/var/lib/kubeauto-redis-longhorn'
CHART_SHA256 = 'a871d397e19cf3243949abd41fd294869f5c2c490014f29e71866a2433ec7fb9'
LABELS = {'kubeauto.io/component': 'redis-test-storage', 'kubeauto.io/managed-by': 'kubeauto'}


def node_addresses(nodes):
    addresses = {}
    for node in nodes:
        ips = [a['address'] for a in node['status']['addresses'] if a['type'] == 'InternalIP']
        if len(ips) != 1 or ips[0] not in NODES or ips[0] in addresses.values():
            raise ValueError('node outside dedicated Redis allowlist or duplicate node IP')
        if node['status']['nodeInfo']['kubeletVersion'] != 'v1.33.6':
            raise ValueError('dedicated Redis matrix requires Kubernetes 1.33.6')
        if not any(c['type'] == 'Ready' and c['status'] == 'True' for c in node['status']['conditions']):
            raise ValueError('Redis storage prerequisite has a non-Ready node')
        addresses[node['metadata']['name']] = ips[0]
    if set(addresses.values()) != NODES:
        raise ValueError('dedicated Redis cluster does not contain exactly the six leased nodes')
    return addresses


def image_plan(chart, evidence):
    if hashlib.sha256(chart.read_bytes()).hexdigest() != CHART_SHA256:
        raise ValueError('Redis CSI Chart checksum mismatch')
    if (evidence.get('workflow_conclusion') != 'success' or evidence.get('durable_rc') != 0 or
            evidence.get('failure_markers') != 0):
        raise ValueError('Redis CSI publication was not accepted')
    records = {r['key']: r for r in evidence['images']}
    with tarfile.open(chart) as archive:
        defaults = yaml.safe_load(archive.extractfile('longhorn/values.yaml'))
    images = []
    values = {'global': {'imageRegistry': 'registry.talkschool.cn:5000'}, 'image': {}}
    for group in ('longhorn', 'csi'):
        values['image'][group] = {}
        for key, pin in defaults['image'][group].items():
            name = pin['repository'].split('/')[-1]
            mirror = ('longhorn-' + name if group == 'csi' or name in
                      ('backing-image-manager', 'support-bundle-kit') else name)
            ref = f"brinnatt/{mirror}:{pin['tag']}"
            record = records.get(ref)
            if not record or set(record['platforms']) != {'linux/amd64', 'linux/arm64'}:
                raise ValueError('Redis CSI image lacks exact dual-architecture publication: ' + ref)
            values['image'][group][key] = {'repository': 'redis-lab/brinnatt/' + mirror, 'tag': pin['tag']}
            images.append(record)
    if len(images) != 13 or len(records) != 13:
        raise ValueError('Redis CSI artifact coverage is incomplete')
    values.update(persistence={'defaultClass': False},
                  defaultSettings={'defaultDataPath': DATA_PATH, 'defaultReplicaCount': 2,
                                   'storageMinimalAvailablePercentage': 10,
                                   'storageReservedPercentageForDefaultDisk': 10,
                                   'upgradeChecker': False, 'allowCollectingLonghornUsageMetrics': False})
    selector = {'kubeauto.io/redis-storage': 'true'}
    for component in ('longhornManager', 'longhornDriver', 'longhornUI'):
        values[component] = {'nodeSelector': selector}
    values['defaultSettings']['systemManagedComponentsNodeSelector'] = 'kubeauto.io/redis-storage:true'
    return values, images


def write_state(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile('w', dir=path.parent, delete=False) as stream:
        json.dump(data, stream, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
        temporary = Path(stream.name)
    temporary.chmod(0o600)
    temporary.replace(path)


class Fixture:
    def __init__(self, args):
        self.args = args
        self.kubectl = ['kubectl', '--kubeconfig=' + str(args.kubeconfig)]
        self.state_file = args.base / 'clusters/redis-gate/redis-storage-owned.json'

    def run(self, argv, *, input_text=None, timeout=90):
        result = subprocess.run(argv, text=True, capture_output=True, input=input_text, timeout=timeout)
        if result.returncode:
            raise ValueError('fixture command failed: ' + argv[0] + '; preserve runner diagnostics')
        return result.stdout.strip()

    def k(self, *argv, **kwargs):
        return self.run([*self.kubectl, *argv], **kwargs)

    def get(self, kind, name=None):
        argv = ['get', kind, *([name] if name else []), '-o', 'json']
        if name:
            argv.append('--ignore-not-found')
        text = self.k(*argv)
        return json.loads(text) if text else None

    def ssh(self, ip, script):
        if ip not in NODES:
            raise ValueError('forbidden Redis fixture SSH target')
        return self.run(['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10',
                         '-o', 'ServerAliveInterval=5', '-o', 'ServerAliveCountMax=2',
                         'root@' + ip, 'bash', '-se'], input_text=script, timeout=300)

    def owned(self, obj):
        return all(obj['metadata'].get('labels', {}).get(k) == v for k, v in LABELS.items())

    def verify_clean(self):
        for kind, name in (('namespace', NAMESPACE), ('storageclass', STORAGE_CLASS)):
            if self.get(kind, name):
                raise ValueError('Redis CSI fixture remains: ' + name)
        if self.state_file.exists():
            raise ValueError('Redis CSI ownership state remains')
        print('REDIS_STORAGE_CLEAN_VERIFY_PASS')

    def artifact_plan(self):
        payload = os.environ.get('REDIS_STORAGE_PUBLICATION_EVIDENCE')
        evidence = (json.loads(payload) if payload else
                    json.loads((self.args.base / 'tests/evidence/redis-storage-images-20261012.json').read_text()))
        return image_plan(self.args.base / 'tests/fixtures/longhorn-1.10.1.tgz', evidence)

    def artifacts(self):
        _, images = self.artifact_plan()
        for image in images:
            raw = subprocess.run(['skopeo', 'inspect', '--raw', 'docker://' + image['talkedu_ref']],
                                 capture_output=True, check=True, timeout=90).stdout
            if 'sha256:' + hashlib.sha256(raw).hexdigest() != image['digest']:
                raise ValueError('Redis CSI delivery source digest changed')
            print('REDIS_STORAGE_MANIFEST_PASS image=' + image['key'] + ' digest=' + image['digest'], flush=True)
        print('REDIS_STORAGE_ARTIFACTS_PASS count=13')

    def health(self):
        state = json.loads(self.state_file.read_text())
        current = node_addresses(self.get('nodes')['items'])
        if (current != state['nodes'] or self.get('namespace', 'kube-system')['metadata']['uid'] !=
                state['cluster_uid'] or state.get('namespace') != NAMESPACE):
            raise ValueError('Redis CSI reusable baseline identity changed')
        if not all(obj and self.owned(obj) for obj in
                   (self.get('namespace', NAMESPACE), self.get('storageclass', STORAGE_CLASS))):
            raise ValueError('Redis CSI reusable baseline ownership is missing')
        sc = self.get('storageclass', STORAGE_CLASS)
        if (sc['provisioner'] != 'driver.longhorn.io' or sc.get('allowVolumeExpansion') is not True or
                sc.get('volumeBindingMode') != 'WaitForFirstConsumer'):
            raise ValueError('Redis CSI reusable StorageClass contract changed')
        for kind, name in (('daemonset', 'longhorn-manager'), ('deployment', 'longhorn-driver-deployer')):
            obj = json.loads(self.k('-n', NAMESPACE, 'get', kind, name, '-o', 'json'))
            expected = obj['status'].get('desiredNumberScheduled') if kind == 'daemonset' else obj['spec']['replicas']
            ready = obj['status'].get('numberReady') if kind == 'daemonset' else obj['status'].get('readyReplicas')
            if not expected or ready != expected:
                raise ValueError('Redis CSI reusable controller is unhealthy')
        nodes = json.loads(self.k('-n', NAMESPACE, 'get', 'nodes.longhorn.io', '-o', 'json'))['items']
        healthy = [n for n in nodes if any(c['type'] == 'Ready' and c['status'] == 'True'
                                         for c in n['status'].get('conditions', []))]
        if len(healthy) != 3:
            raise ValueError('Redis CSI reusable baseline lacks three healthy storage workers')
        pods = json.loads(self.k('-n', NAMESPACE, 'get', 'pods', '-o', 'json'))['items']
        for pod in pods:
            if pod.get('status', {}).get('phase') != 'Succeeded' and not any(
                    c['type'] == 'Ready' and c['status'] == 'True' for c in pod.get('status', {}).get('conditions', [])):
                raise ValueError('Redis CSI baseline has a failed or unready Pod')
        print('REDIS_STORAGE_BASELINE_REUSE_PASS workers=3 identity=verified')

    def prepare(self):
        addresses = node_addresses(self.get('nodes')['items'])
        uid = self.get('namespace', 'kube-system')['metadata']['uid']
        if self.state_file.exists():
            raise ValueError('existing fixture requires explicit health/ownership verification before reuse')
        for kind, name in (('namespace', NAMESPACE), ('storageclass', STORAGE_CLASS)):
            if self.get(kind, name):
                raise ValueError('refusing pre-existing Redis CSI resource: ' + name)
        crds = self.get('crds')['items']
        if any(c['metadata']['name'].endswith('.longhorn.io') for c in crds):
            raise ValueError('refusing another Longhorn installation in this cluster')
        values, images = self.artifact_plan()
        # The fixed launcher verifies all current indexes before cluster setup.
        # Copy immutable child manifests here, never trust a mutable tag alone.
        for image in images:
            # Immutable image cache is reusable infrastructure, never business
            # fixture data. Do not restart/GC the shared registry during cleanup.
            target = '127.0.0.1:5000/redis-lab/' + image['key']
            probe = subprocess.run(['skopeo', 'inspect', '--raw', '--tls-verify=false',
                                    'docker://' + target], capture_output=True, timeout=60)
            expected = image['platforms']['linux/amd64']
            if probe.returncode:
                if b'manifest unknown' not in probe.stderr.lower() and b'name unknown' not in probe.stderr.lower():
                    raise ValueError('Redis CSI local artifact cache is unavailable')
                self.run(['skopeo', 'copy', '--override-os', 'linux', '--override-arch', 'amd64',
                          '--preserve-digests', '--dest-tls-verify=false',
                          'docker://' + image['talkedu_ref'].rsplit(':', 1)[0] + '@' + expected,
                          'docker://' + target], timeout=900)
                probe = subprocess.run(['skopeo', 'inspect', '--raw', '--tls-verify=false',
                                        'docker://' + target], capture_output=True, check=True, timeout=60)
            if 'sha256:' + hashlib.sha256(probe.stdout).hexdigest() != expected:
                raise ValueError('Redis CSI local artifact cache digest mismatch')
            print('REDIS_STORAGE_CACHE_PASS image=' + image['key'] + ' digest=' + expected, flush=True)
        state = {'cluster_uid': uid, 'nodes': addresses, 'workers': {}, 'namespace': NAMESPACE}
        write_state(self.state_file, state)
        for node, ip in addresses.items():
            if ip not in WORKERS:
                continue
            probe = self.ssh(ip, '''
if test -e /var/lib/kubeauto-redis-longhorn; then exit 1; fi
if command -v iscsiadm >/dev/null; then echo installed=yes; else echo installed=no; fi
printf 'active=%s\n' "$(systemctl is-active iscsid || true)"
printf 'enabled=%s\n' "$(systemctl is-enabled iscsid || true)"
''')
            prior = dict(line.split('=', 1) for line in probe.splitlines() if '=' in line)
            if self.get('node', node)['metadata'].get('labels', {}).get('kubeauto.io/redis-storage') is not None:
                raise ValueError('refusing an existing Redis storage node label')
            state['workers'][node] = prior
            write_state(self.state_file, state)
            self.ssh(ip, 'command -v iscsiadm >/dev/null || dnf -y install iscsi-initiator-utils\n'
                          'modprobe iscsi_tcp\nsystemctl start iscsid\n')
            self.k('label', 'node', node, 'kubeauto.io/redis-storage=true')
        namespace = {'apiVersion': 'v1', 'kind': 'Namespace', 'metadata': {'name': NAMESPACE, 'labels': LABELS}}
        self.k('create', '-f', '-', input_text=json.dumps(namespace))
        values_file = self.state_file.with_name('redis-longhorn-values.json')
        values_file.write_text(json.dumps(values))
        self.run([str(self.args.helm), 'upgrade', '--install', RELEASE,
                  str(self.args.base / 'tests/fixtures/longhorn-1.10.1.tgz'),
                  '--kubeconfig', str(self.args.kubeconfig), '--namespace', NAMESPACE,
                  '--values', str(values_file), '--wait', '--timeout', '20m'], timeout=1250)
        self.k('-n', NAMESPACE, 'rollout', 'status', 'deployment/longhorn-driver-deployer',
               '--timeout=10m', timeout=650)
        sc = {'apiVersion': 'storage.k8s.io/v1', 'kind': 'StorageClass',
              'metadata': {'name': STORAGE_CLASS, 'labels': LABELS},
              'provisioner': 'driver.longhorn.io', 'allowVolumeExpansion': True,
              'reclaimPolicy': 'Delete', 'volumeBindingMode': 'WaitForFirstConsumer',
              'parameters': {'numberOfReplicas': '2', 'staleReplicaTimeout': '30',
                             'dataLocality': 'disabled', 'fsType': 'ext4'}}
        self.k('create', '-f', '-', input_text=json.dumps(sc))
        print('REDIS_STORAGE_LAB_PASS storageclass=redis-longhorn version=1.10.1')

    def cleanup(self):
        if not self.state_file.exists():
            self.verify_clean()
            return
        state = json.loads(self.state_file.read_text())
        current = node_addresses(self.get('nodes')['items'])
        if (current != state['nodes'] or self.get('namespace', 'kube-system')['metadata']['uid'] !=
                state['cluster_uid'] or state.get('namespace') != NAMESPACE):
            raise ValueError('Redis CSI cleanup cluster ownership changed')
        namespace = self.get('namespace', NAMESPACE)
        sc = self.get('storageclass', STORAGE_CLASS)
        if any(obj and not self.owned(obj) for obj in (namespace, sc)):
            raise ValueError('refusing foreign CSI namespace or StorageClass')
        if any(v['spec'].get('storageClassName') == STORAGE_CLASS for v in self.get('pv')['items']):
            raise ValueError('Redis CSI still has dependent persistent volumes')
        if sc:
            self.k('delete', 'storageclass', STORAGE_CLASS)
        if namespace:
            settings = self.k('-n', NAMESPACE, 'get', 'settings.longhorn.io',
                              'deleting-confirmation-flag', '--ignore-not-found', '-o', 'name')
            if settings:
                self.k('-n', NAMESPACE, 'patch', 'settings.longhorn.io', 'deleting-confirmation-flag',
                       '--type=merge', '-p', '{"value":"true"}')
            releases = json.loads(self.run([str(self.args.helm), '--kubeconfig', str(self.args.kubeconfig),
                                           'list', '--all', '-n', NAMESPACE, '--filter', '^' + RELEASE + '$', '-o', 'json']))
            if releases:
                self.run([str(self.args.helm), '--kubeconfig', str(self.args.kubeconfig), 'uninstall',
                          RELEASE, '-n', NAMESPACE, '--wait', '--timeout', '15m'], timeout=950)
            self.k('delete', 'namespace', NAMESPACE, '--wait=true', '--timeout=10m', timeout=650)
        # CRDs were absent before this dedicated fixture. Refuse any survivor's
        # consumer rather than forcing finalizers or deleting another workload.
        for crd in self.get('crds')['items']:
            name = crd['metadata']['name']
            if name.endswith('.longhorn.io'):
                if json.loads(self.k('get', name, '-A', '-o', 'json'))['items']:
                    raise ValueError('Longhorn CRD still has consumers: ' + name)
                self.k('delete', 'crd', name, '--wait=true', '--timeout=5m', timeout=350)
        for node, prior in state['workers'].items():
            ip = current[node]
            if ip not in WORKERS:
                raise ValueError('invalid Redis CSI worker journal')
            self.ssh(ip, 'test -z "$(findmnt -rn -o TARGET | '
                          'awk \'$0 ~ /^\\/var\\/lib\\/kubeauto-redis-longhorn(\\/|$)/\')"\n'
                          'rm -rf -- /var/lib/kubeauto-redis-longhorn\n' +
                          ('systemctl stop iscsid\n' if prior['active'] != 'active' else '') +
                          ('dnf -y remove iscsi-initiator-utils\n' if prior['installed'] == 'no' else ''))
            self.k('label', 'node', node, 'kubeauto.io/redis-storage-')
        self.state_file.with_name('redis-longhorn-values.json').unlink(missing_ok=True)
        self.state_file.unlink()
        self.verify_clean()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=('render', 'artifacts', 'prepare', 'health', 'cleanup', 'verify'))
    parser.add_argument('--base', type=Path, default=Path('/usr/local/kubeauto'))
    parser.add_argument('--kubeconfig', type=Path)
    parser.add_argument('--helm', type=Path)
    args = parser.parse_args()
    args.kubeconfig = args.kubeconfig or args.base / 'clusters/redis-gate/kubectl.kubeconfig'
    args.helm = args.helm or args.base / 'extra-bin/helm'
    if args.mode == 'render':
        values, _ = image_plan(args.base / 'tests/fixtures/longhorn-1.10.1.tgz',
                               json.loads((args.base / 'tests/evidence/redis-storage-images-20261012.json').read_text()))
        print(json.dumps(values))
    else:
        fixture = Fixture(args)
        {'artifacts': fixture.artifacts, 'prepare': fixture.prepare,
         'health': fixture.health, 'cleanup': fixture.cleanup, 'verify': fixture.verify_clean}[args.mode]()


if __name__ == '__main__':
    main()
