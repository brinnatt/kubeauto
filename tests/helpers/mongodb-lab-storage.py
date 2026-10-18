#!/usr/bin/env python3
"""Dedicated MongoDB CSI fixture with fixed artifacts and bounded cleanup.

Pinned official Chart/source: https://github.com/longhorn/longhorn/releases/tag/v1.10.1
The vendored uninstall hook runs longhorn-manager uninstall after the deletion confirmation.
The enclosing MongoDB runner owns the host lease and dedicated six-node cluster.
Only the exact immutable image publication records are reused from the existing
Longhorn catalog evidence; no Redis runtime, ownership or acceptance is reused.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import secrets
import signal
import subprocess
import sys
import tarfile
import tempfile
import time

import yaml

NODES = {'192.168.122.135', '192.168.122.40', '192.168.122.72',
         '192.168.122.212', '192.168.122.165', '192.168.122.238'}
WORKERS = {'192.168.122.212', '192.168.122.165', '192.168.122.238'}
REPLICA_SPARES = {'mongodb-node-135': '192.168.122.135', 'mongodb-node-40': '192.168.122.40'}
NAMESPACE = 'mongodb-lab-storage'
RELEASE = 'mongodb-lab-longhorn'
STORAGE_CLASS = 'mongodb-longhorn'
DATA_PATH = '/var/lib/kubeauto-mongodb-longhorn'
CHART_SHA256 = 'a871d397e19cf3243949abd41fd294869f5c2c490014f29e71866a2433ec7fb9'
LABELS = {'kubeauto.io/component': 'mongodb-test-storage', 'kubeauto.io/managed-by': 'kubeauto'}


class StorageNotReady(ValueError):
    """Owned controllers are still converging; identity errors never retry."""


def replica_spare(node):
    """Only two already leased masters may temporarily provide extra domains."""
    meta, spec, status = node['metadata'], node['spec'], node['status']
    if (meta['name'] not in REPLICA_SPARES or not meta.get('uid') or
            [a['address'] for a in status['addresses'] if a['type'] == 'InternalIP'] !=
            [REPLICA_SPARES[meta['name']]] or
            meta.get('labels', {}).get('kubeauto.io/mongodb-storage') is not None or
            meta.get('labels', {}).get('kubeauto.io/mongodb-replica-domain') is not None or
            not any(c['type'] == 'Ready' and c['status'] == 'True' for c in status.get('conditions', [])) or
            any(t.get('effect') != 'NoSchedule' or t.get('key') not in
                ('node-role.kubernetes.io/master', 'node-role.kubernetes.io/control-plane',
                 *(['node.kubernetes.io/unschedulable'] if spec.get('unschedulable') else []))
                for t in spec.get('taints', []))):
        raise ValueError('MongoDB replica spare is not a clean healthy leased master')
    return {'name': meta['name'], 'uid': meta['uid'], 'ip': REPLICA_SPARES[meta['name']],
            'taints': spec.get('taints', []), 'unschedulable': spec.get('unschedulable', False)}


def node_addresses(nodes):
    addresses = {}
    for node in nodes:
        ips = [a['address'] for a in node['status']['addresses'] if a['type'] == 'InternalIP']
        if len(ips) != 1 or ips[0] not in NODES or ips[0] in addresses.values():
            raise ValueError('node outside dedicated MongoDB allowlist or duplicate node IP')
        if node['status']['nodeInfo']['kubeletVersion'] != 'v1.33.6':
            raise ValueError('dedicated MongoDB matrix requires Kubernetes 1.33.6')
        if not any(c['type'] == 'Ready' and c['status'] == 'True' for c in node['status']['conditions']):
            raise ValueError('MongoDB storage prerequisite has a non-Ready node')
        addresses[node['metadata']['name']] = ips[0]
    if set(addresses.values()) != NODES:
        raise ValueError('dedicated MongoDB cluster does not contain exactly the six leased nodes')
    return addresses


def image_plan(chart, evidence):
    if hashlib.sha256(chart.read_bytes()).hexdigest() != CHART_SHA256:
        raise ValueError('MongoDB CSI Chart checksum mismatch')
    if (evidence.get('workflow_conclusion') != 'success' or evidence.get('durable_rc') != 0 or
            evidence.get('failure_markers') != 0):
        raise ValueError('MongoDB CSI publication was not accepted')
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
                raise ValueError('MongoDB CSI image lacks exact dual-architecture publication: ' + ref)
            values['image'][group][key] = {'repository': 'mongodb-lab/brinnatt/' + mirror, 'tag': pin['tag']}
            images.append(record)
    if len(images) != 13 or len(records) != 13:
        raise ValueError('MongoDB CSI artifact coverage is incomplete')
    values.update(persistence={'defaultClass': False},
                  defaultSettings={'defaultDataPath': DATA_PATH, 'defaultReplicaCount': 2,
                                   'storageMinimalAvailablePercentage': 10,
                                   'storageReservedPercentageForDefaultDisk': 10,
                                   'upgradeChecker': False, 'allowCollectingLonghornUsageMetrics': False})
    selector = {'kubeauto.io/mongodb-storage': 'true'}
    for component in ('longhornManager', 'longhornDriver', 'longhornUI'):
        values[component] = {'nodeSelector': selector}
    values['defaultSettings']['systemManagedComponentsNodeSelector'] = 'kubeauto.io/mongodb-storage:true'
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


def reclaim_plan(source, claims, pvs, volumes, engines, source_uid):
    """Only reclaim free blocks and internal snapshots of the three lab source volumes."""
    if (not source_uid or source['metadata']['uid'] != source_uid or
            source['metadata']['name'] != 'mongodb-gate-data' or
            source['metadata'].get('namespace') != 'mongodb-gate' or
            source['metadata'].get('labels', {}).get('app.kubernetes.io/managed-by') != 'kubeauto'):
        raise ValueError('MongoDB storage reclaim source identity changed')
    expected = {'mongod-data-mongodb-gate-data-rs0-' + str(i) for i in range(3)}
    claims = [c for c in claims if c['metadata'].get('labels', {}).get('app.kubernetes.io/instance') == 'mongodb-gate-data']
    if {c['metadata']['name'] for c in claims} != expected or len(claims) != 3:
        raise ValueError('MongoDB storage reclaim requires exactly three source claims')
    plan = []
    for claim in sorted(claims, key=lambda c: c['metadata']['name']):
        name, uid = claim['metadata']['name'], claim['metadata']['uid']
        volume = claim['spec'].get('volumeName')
        pv = next((p for p in pvs if p['metadata']['name'] == volume), {})
        v = next((v for v in volumes if v['metadata']['name'] == volume), {})
        es = [e for e in engines if e['spec'].get('volumeName') == volume]
        ref = pv.get('spec', {}).get('claimRef', {})
        if (claim['metadata'].get('namespace') != 'mongodb-gate' or
                claim['spec'].get('storageClassName') != STORAGE_CLASS or
                claim.get('status', {}).get('phase') != 'Bound' or
                (ref.get('namespace'), ref.get('name'), ref.get('uid')) != ('mongodb-gate', name, uid) or
                pv.get('spec', {}).get('storageClassName') != STORAGE_CLASS or
                pv.get('spec', {}).get('csi', {}).get('driver') != 'driver.longhorn.io' or
                pv.get('spec', {}).get('csi', {}).get('volumeHandle') != volume or
                not v or v.get('metadata', {}).get('namespace') != NAMESPACE or
                v['spec'].get('nodeID') not in {'mongodb-node-' + ip.rsplit('.', 1)[1] for ip in WORKERS} or
                v.get('status', {}).get('state') != 'attached' or v['status'].get('robustness') != 'healthy' or
                v['spec'].get('encrypted') or v['spec'].get('dataEngine', 'v1') != 'v1' or
                len(es) != 1 or es[0].get('status', {}).get('currentState') != 'running' or
                len(es[0]['status'].get('replicaModeMap', {})) != 2 or
                set(es[0]['status']['replicaModeMap'].values()) != {'RW'}):
            raise ValueError('MongoDB storage reclaim refuses rebound, unhealthy or rebuilding volumes')
        snapshots = es[0]['status'].get('snapshots')
        if not isinstance(snapshots, dict) or 'volume-head' not in snapshots:
            raise ValueError('MongoDB storage reclaim requires the actual native snapshot graph')
        for key, snap in snapshots.items():
            if (snap.get('name') != key or snap.get('usercreated') is not False or
                    snap.get('labels', {}) not in ({}, {'replica-expansion': v['spec']['size']})):
                raise ValueError('MongoDB storage reclaim refuses user or unrecognized snapshots')
        plan.append({'claim': name, 'claim_uid': uid, 'volume': volume,
            'pv_uid': pv['metadata']['uid'], 'volume_uid': v['metadata']['uid'],
            'engine_uid': es[0]['metadata']['uid'],
            'snapshots': sorted(n for n in snapshots if n != 'volume-head'),
            'pending_snapshots': sorted(n for n, s in snapshots.items() if n != 'volume-head' and not s.get('removed')),
            'purging': any(s.get('isPurging') for s in es[0]['status'].get('purgeStatus', {}).values()),
            'purge_errors': [s['error'] for s in es[0]['status'].get('purgeStatus', {}).values() if s.get('error')],
            'actual_bytes': int(v['status']['actualSize'])})
    return plan


def probe_objects(run_id, node):
    if not run_id.isalnum() or len(run_id) != 16 or node not in {'mongodb-node-' + ip.rsplit('.', 1)[1] for ip in WORKERS}:
        raise ValueError('MongoDB CSI probe identity is outside its leased worker boundary')
    metadata = {'name': 'mongodb-csi-' + run_id, 'namespace': NAMESPACE,
                'labels': LABELS | {'kubeauto.io/test-run': run_id}}
    pvc = {'apiVersion': 'v1', 'kind': 'PersistentVolumeClaim', 'metadata': metadata,
           'spec': {'storageClassName': STORAGE_CLASS, 'accessModes': ['ReadWriteOnce'],
                    'resources': {'requests': {'storage': '1Gi'}}}}
    pod = {'apiVersion': 'v1', 'kind': 'Pod', 'metadata': metadata,
           'spec': {'restartPolicy': 'Never', 'nodeSelector': {'kubernetes.io/hostname': node},
                    'containers': [{'name': 'probe',
                                    'image': 'registry.talkschool.cn:5000/brinnatt/percona-server-mongodb:8.0.32-14',
                                    'command': ['sh', '-c', 'trap "exit 0" TERM INT; sleep 3600 & wait'],
                                    'securityContext': {'runAsUser': 0},
                                    'resources': {'requests': {'cpu': '10m', 'memory': '64Mi'},
                                                  'limits': {'cpu': '500m', 'memory': '256Mi'}},
                                    'volumeMounts': [{'name': 'data', 'mountPath': '/probe'}]}],
                    'volumes': [{'name': 'data', 'persistentVolumeClaim': {'claimName': metadata['name']}}]}}
    return pvc, pod


class Fixture:
    def __init__(self, args):
        self.args = args
        self.kubectl = ['kubectl', '--kubeconfig=' + str(args.kubeconfig)]
        self.state_file = args.base / 'clusters/mongodb-gate/mongodb-storage-owned.json'

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
            raise ValueError('forbidden MongoDB fixture SSH target')
        return self.run(['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10',
                         '-o', 'ServerAliveInterval=5', '-o', 'ServerAliveCountMax=2',
                         'root@' + ip, 'bash', '-se'], input_text=script, timeout=300)

    def owned(self, obj):
        return all(obj['metadata'].get('labels', {}).get(k) == v for k, v in LABELS.items())

    def verify_clean(self):
        for kind, name in (('namespace', NAMESPACE), ('storageclass', STORAGE_CLASS)):
            if self.get(kind, name):
                raise ValueError('MongoDB CSI fixture remains: ' + name)
        if self.state_file.exists():
            raise ValueError('MongoDB CSI ownership state remains')
        print('MONGODB_STORAGE_CLEAN_VERIFY_PASS')

    def artifact_plan(self):
        payload = os.environ.get('MONGODB_STORAGE_PUBLICATION_EVIDENCE')
        evidence = (json.loads(payload) if payload else
                    json.loads((self.args.base / 'tests/evidence/redis-storage-images-20261012.json').read_text()))
        return image_plan(self.args.base / 'tests/fixtures/longhorn-1.10.1.tgz', evidence)

    def artifacts(self):
        _, images = self.artifact_plan()
        for image in images:
            raw = subprocess.run(['skopeo', 'inspect', '--raw', 'docker://' + image['talkedu_ref']],
                                 capture_output=True, check=True, timeout=90).stdout
            if 'sha256:' + hashlib.sha256(raw).hexdigest() != image['digest']:
                raise ValueError('MongoDB CSI delivery source digest changed')
            print('MONGODB_STORAGE_MANIFEST_PASS image=' + image['key'] + ' digest=' + image['digest'], flush=True)
        print('MONGODB_STORAGE_ARTIFACTS_PASS count=13')

    def health(self):
        state = json.loads(self.state_file.read_text())
        current = node_addresses(self.get('nodes')['items'])
        if (current != state['nodes'] or self.get('namespace', 'kube-system')['metadata']['uid'] !=
                state['cluster_uid'] or state.get('namespace') != NAMESPACE):
            raise ValueError('MongoDB CSI reusable baseline identity changed')
        if not all(obj and self.owned(obj) for obj in
                   (self.get('namespace', NAMESPACE), self.get('storageclass', STORAGE_CLASS))):
            raise ValueError('MongoDB CSI reusable baseline ownership is missing')
        sc = self.get('storageclass', STORAGE_CLASS)
        if (sc['provisioner'] != 'driver.longhorn.io' or sc.get('allowVolumeExpansion') is not True or
                sc.get('volumeBindingMode') != 'WaitForFirstConsumer'):
            raise ValueError('MongoDB CSI reusable StorageClass contract changed')
        # driver-deployer creates these resources asynchronously. Helm --wait
        # cannot wait for controllers absent from its rendered release manifest.
        controllers = [('daemonset', 'longhorn-manager'), ('daemonset', 'longhorn-csi-plugin')]
        controllers += [('deployment', name) for name in ('longhorn-driver-deployer', 'csi-attacher',
                        'csi-provisioner', 'csi-resizer', 'csi-snapshotter')]
        for kind, name in controllers:
            text = self.k('-n', NAMESPACE, 'get', kind, name, '--ignore-not-found', '-o', 'json')
            if not text:
                raise StorageNotReady('MongoDB CSI controller is not created: ' + name)
            obj = json.loads(text)
            expected = obj['status'].get('desiredNumberScheduled') if kind == 'daemonset' else obj['spec']['replicas']
            ready = obj['status'].get('numberReady') if kind == 'daemonset' else obj['status'].get('readyReplicas')
            if (not expected or ready != expected or
                    obj['status'].get('observedGeneration', 0) < obj['metadata']['generation']):
                raise StorageNotReady('MongoDB CSI controller has not converged: ' + name)
        if not self.k('get', 'csidriver', 'driver.longhorn.io', '--ignore-not-found', '-o', 'name'):
            raise StorageNotReady('MongoDB CSI driver registration is missing')
        nodes = json.loads(self.k('-n', NAMESPACE, 'get', 'nodes.longhorn.io', '-o', 'json'))['items']
        healthy = [n for n in nodes if any(c['type'] == 'Ready' and c['status'] == 'True'
                                         for c in n['status'].get('conditions', []))]
        if len(healthy) != 3:
            raise StorageNotReady('MongoDB CSI reusable baseline lacks three healthy storage workers')
        pods = json.loads(self.k('-n', NAMESPACE, 'get', 'pods', '-o', 'json'))['items']
        for pod in pods:
            if pod.get('status', {}).get('phase') in ('Failed', 'Unknown'):
                raise ValueError('MongoDB CSI baseline has a terminal Pod: ' + pod['metadata']['name'])
            if pod.get('status', {}).get('phase') != 'Succeeded' and not any(
                    c['type'] == 'Ready' and c['status'] == 'True' for c in pod.get('status', {}).get('conditions', [])):
                raise StorageNotReady('MongoDB CSI Pod is not Ready: ' + pod['metadata']['name'])
        print('MONGODB_STORAGE_BASELINE_REUSE_PASS workers=3 identity=verified')

    def wait_health(self, timeout=900):
        deadline = time.monotonic() + timeout
        while True:
            try:
                self.health()
                return
            except StorageNotReady as exc:
                if time.monotonic() >= deadline:
                    raise ValueError('MongoDB CSI convergence deadline exceeded: ' + str(exc)) from None
                print('MONGODB_STORAGE_HEARTBEAT stage=controller-convergence waiting=' + str(exc), flush=True)
                time.sleep(min(5, max(0, deadline - time.monotonic())))

    def reclaim(self):
        """Longhorn 1.10.1 native snapshotDelete/trimFilesystem/snapshotPurge.

        Internal rebuild/expansion snapshots retain deleted pressure/log files.
        This fixture has no customer CSI snapshots; refuse any user snapshot.
        Native operations are reentrant and do not change volume configuration.
        Official manager source: 29b7fda2692c8df5b1aa18c477f77863c7acc1e7.
        """
        self.health()
        owner_path = self.args.base / 'clusters/mongodb-gate/mongodb-lab-owner.json'
        owner = json.loads(owner_path.read_text())
        state = json.loads(self.state_file.read_text())
        if owner['cluster_uid'] != state['cluster_uid']:
            raise ValueError('MongoDB storage reclaim cluster identity changed')
        def plan():
            def objects(kind, namespace=None):
                return json.loads(self.k(*(['-n', namespace] if namespace else []), 'get', kind, '-o', 'json'))['items']
            source = json.loads(self.k('-n', 'mongodb-gate', 'get', 'psmdb', 'mongodb-gate-data', '-o', 'json'))
            return reclaim_plan(source, objects('pvc', 'mongodb-gate'), objects('pv'),
                objects('volumes.longhorn.io', NAMESPACE), objects('engines.longhorn.io', NAMESPACE),
                owner.get('product_identity', {}).get('source_uid'))
        before = plan()
        identity_fields = ('claim', 'claim_uid', 'volume', 'pv_uid', 'volume_uid', 'engine_uid')
        identity = lambda rows: [{k: r[k] for k in identity_fields} for r in rows]
        def action(row, name, body):
            current = plan()  # All volumes must pass before each mutation.
            if identity(current) != identity(before):
                raise ValueError('MongoDB storage reclaim volume identity changed')
            path = '/api/v1/namespaces/' + NAMESPACE + '/services/longhorn-backend:9500/proxy/v1/volumes/' + row['volume']
            result = json.loads(self.run([*self.kubectl, 'create', '--raw', path + '?action=' + name, '-f', '-'],
                input_text=json.dumps(body), timeout=180))
            if result.get('id') != row['volume']:
                raise ValueError('MongoDB storage reclaim native API response differs from target')
        for row in before:
            for name in row['snapshots']:
                action(row, 'snapshotDelete', {'name': name})
            action(row, 'trimFilesystem', {})
            action(row, 'snapshotPurge', {})
            print('MONGODB_STORAGE_RECLAIM_VOLUME_PASS volume=' + row['volume'] + ' native-trim-and-purge=true', flush=True)
        deadline = time.monotonic() + 600
        while True:
            after = plan()
            if identity(after) != identity(before):
                raise ValueError('MongoDB storage reclaim changed volume identity')
            if any(r['purge_errors'] for r in after):
                raise ValueError('MongoDB storage reclaim native purge reported errors')
            # A removed snapshot adjacent to the active head can remain in
            # the graph. Its trimmed blocks no longer consume host storage.
            if all(not r['pending_snapshots'] and not r['purging'] for r in after):break
            if time.monotonic() >= deadline:
                raise ValueError('MongoDB storage reclaim native purge deadline exceeded')
            print('MONGODB_STORAGE_RECLAIM_HEARTBEAT stage=native-purge', flush=True);time.sleep(5)
        for ip in sorted(WORKERS):
            raw = self.ssh(ip, "python3 -c 'import os;v=os.statvfs(\"/var/lib/kubeauto-mongodb-longhorn\");print(v.f_frsize*v.f_blocks,v.f_frsize*v.f_bavail)'\n")
            total, free = map(int, raw.split())
            if free < max(16 * 1024**3, total // 4):
                raise ValueError('MongoDB storage reclaim host headroom remains below the lab budget')
            print('MONGODB_STORAGE_RECLAIM_HEADROOM_PASS node=' + ip + ' free-bytes=' + str(free), flush=True)
        print('MONGODB_STORAGE_RECLAIM_SCOPED_CLEAN_PASS volume-identities=preserved internal-snapshots=removed-or-purged settings=unchanged', flush=True)
        print('MONGODB_STORAGE_RECLAIM_PASS', flush=True)

    def probe(self):
        self.wait_health()
        journal = self.state_file.with_name('mongodb-storage-probe.json')
        if journal.exists():
            raise ValueError('MongoDB CSI probe journal requires owning fixture recovery')
        run_id = secrets.token_hex(8)
        pvc, pod = probe_objects(run_id, 'mongodb-node-212')
        name = pvc['metadata']['name']
        for kind in ('pod', 'pvc'):
            if self.k('-n', NAMESPACE, 'get', kind, name, '--ignore-not-found', '-o', 'name'):
                raise ValueError('MongoDB CSI probe refuses an existing resource')
        state = {'run_id': run_id, 'name': name, 'pv': None, 'attempted': [], 'uids': {}}
        write_state(journal, state)

        def get(kind):
            text = self.k('-n', NAMESPACE, 'get', kind, name, '--ignore-not-found', '-o', 'json')
            return json.loads(text) if text else None

        def create(obj, kind):
            if kind not in state['attempted']:
                state['attempted'].append(kind)
            state['uids'].pop(kind, None)
            write_state(journal, state)
            result = json.loads(self.k('create', '-f', '-', '-o', 'json', input_text=json.dumps(obj)))
            state['uids'][kind] = result['metadata']['uid']
            write_state(journal, state)

        def delete(kind):
            obj = get(kind)
            if not obj:
                return
            meta = obj['metadata']
            if (not self.owned(obj) or meta.get('labels', {}).get('kubeauto.io/test-run') != run_id or
                    (kind in state['uids'] and meta['uid'] != state['uids'][kind])):
                raise ValueError('MongoDB CSI cleanup refuses changed fixture ownership')
            if kind == 'pvc':
                state['pv'] = obj['spec'].get('volumeName') or state['pv']
                write_state(journal, state)
            resource = 'persistentvolumeclaims' if kind == 'pvc' else 'pods'
            options = {'apiVersion': 'v1', 'kind': 'DeleteOptions', 'preconditions': {'uid': meta['uid']}}
            self.k('delete', '--raw', '/api/v1/namespaces/' + NAMESPACE + '/' + resource + '/' + name,
                   '-f', '-', input_text=json.dumps(options))
            self.k('-n', NAMESPACE, 'wait', '--for=delete', kind + '/' + name, '--timeout=180s', timeout=200)

        first_error = None
        try:
            create(pvc, 'pvc')
            create(pod, 'pod')
            self.k('-n', NAMESPACE, 'wait', '--for=condition=Ready', 'pod/' + name, '--timeout=300s', timeout=320)
            self.k('-n', NAMESPACE, 'exec', name, '--', 'sh', '-ec',
                   'getent hosts kubernetes.default.svc.cluster.local >/dev/null; '
                   'printf mongodb-csi-business-verified > /probe/verified; sync; '
                   'test "$(cat /probe/verified)" = mongodb-csi-business-verified')
            claim = get('pvc')
            if claim['status'].get('phase') != 'Bound' or not claim['spec'].get('volumeName'):
                raise ValueError('MongoDB CSI probe lacks a bound PV')
            state['pv'] = claim['spec']['volumeName']
            write_state(journal, state)
            pv = self.get('pv', state['pv'])
            if (pv['spec'].get('csi', {}).get('driver') != 'driver.longhorn.io' or
                    pv['spec']['claimRef']['uid'] != claim['metadata']['uid'] or
                    pv['spec']['persistentVolumeReclaimPolicy'] != 'Delete'):
                raise ValueError('MongoDB CSI probe PV identity or reclaim policy differs')
            self.k('-n', NAMESPACE, 'patch', 'pvc', name, '--type=merge',
                   '-p', '{"spec":{"resources":{"requests":{"storage":"2Gi"}}}}')
            deadline = time.monotonic() + 300
            while get('pvc')['status'].get('capacity', {}).get('storage') != '2Gi':
                if time.monotonic() >= deadline:
                    raise ValueError('MongoDB CSI expansion deadline exceeded')
                print('MONGODB_STORAGE_HEARTBEAT stage=expand waiting=true', flush=True)
                time.sleep(5)
            delete('pod')
            pod['spec']['nodeSelector']['kubernetes.io/hostname'] = 'mongodb-node-165'
            create(pod, 'pod')
            self.k('-n', NAMESPACE, 'wait', '--for=condition=Ready', 'pod/' + name, '--timeout=300s', timeout=320)
            self.k('-n', NAMESPACE, 'exec', name, '--', 'sh', '-ec',
                   'test "$(cat /probe/verified)" = mongodb-csi-business-verified; '
                   'printf retained-after-remount >> /probe/verified; sync; '
                   'test "$(cat /probe/verified)" = mongodb-csi-business-verifiedretained-after-remount')
            print('MONGODB_STORAGE_ASSERTIONS_PASS rwo=true expand=1Gi-to-2Gi remount=other-worker data=retained', flush=True)
        except BaseException as exc:
            first_error = exc
            raise
        finally:
            try:
                for kind in reversed(state['attempted']):
                    delete(kind)
                if state['pv']:
                    self.k('wait', '--for=delete', 'pv/' + state['pv'], '--timeout=180s', timeout=200)
                if self.k('-n', NAMESPACE, 'get', 'pod,pvc', '-l', 'kubeauto.io/test-run=' + run_id, '-o', 'name'):
                    raise ValueError('MongoDB CSI probe resources remain')
                journal.unlink()
                print('MONGODB_STORAGE_SCOPED_CLEAN_PASS pod-pvc-pv=absent', flush=True)
            except BaseException:
                print('MONGODB_STORAGE_SCOPED_CLEAN_FAIL journal=preserved', flush=True)
                if first_error is None:
                    raise
        self.health()
        print('MONGODB_STORAGE_PASS', flush=True)

    def prepare(self):
        addresses = node_addresses(self.get('nodes')['items'])
        uid = self.get('namespace', 'kube-system')['metadata']['uid']
        if self.state_file.exists():
            raise ValueError('existing fixture requires explicit health/ownership verification before reuse')
        for kind, name in (('namespace', NAMESPACE), ('storageclass', STORAGE_CLASS)):
            if self.get(kind, name):
                raise ValueError('refusing pre-existing MongoDB CSI resource: ' + name)
        crds = self.get('crds')['items']
        if any(c['metadata']['name'].endswith('.longhorn.io') for c in crds):
            raise ValueError('refusing another Longhorn installation in this cluster')
        values, images = self.artifact_plan()
        # The fixed launcher verifies all current indexes before cluster setup.
        # Copy immutable child manifests here, never trust a mutable tag alone.
        for image in images:
            # Immutable image cache is reusable infrastructure, never business
            # fixture data. Do not restart/GC the shared registry during cleanup.
            target = '127.0.0.1:5000/mongodb-lab/' + image['key']
            probe = subprocess.run(['skopeo', 'inspect', '--raw', '--tls-verify=false',
                                    'docker://' + target], capture_output=True, timeout=60)
            expected = image['platforms']['linux/amd64']
            if probe.returncode:
                if b'manifest unknown' not in probe.stderr.lower() and b'name unknown' not in probe.stderr.lower():
                    raise ValueError('MongoDB CSI local artifact cache is unavailable')
                self.run(['skopeo', 'copy', '--override-os', 'linux', '--override-arch', 'amd64',
                          '--preserve-digests', '--dest-tls-verify=false',
                          'docker://' + image['talkedu_ref'].rsplit(':', 1)[0] + '@' + expected,
                          'docker://' + target], timeout=900)
                probe = subprocess.run(['skopeo', 'inspect', '--raw', '--tls-verify=false',
                                        'docker://' + target], capture_output=True, check=True, timeout=60)
            if 'sha256:' + hashlib.sha256(probe.stdout).hexdigest() != expected:
                raise ValueError('MongoDB CSI local artifact cache digest mismatch')
            print('MONGODB_STORAGE_CACHE_PASS image=' + image['key'] + ' digest=' + expected, flush=True)
        state = {'cluster_uid': uid, 'nodes': addresses, 'workers': {}, 'namespace': NAMESPACE}
        write_state(self.state_file, state)
        for node, ip in addresses.items():
            if ip not in WORKERS:
                continue
            print('MONGODB_STORAGE_HEARTBEAT stage=host-prerequisites node=' + ip, flush=True)
            probe = self.ssh(ip, '''
if test -e /var/lib/kubeauto-mongodb-longhorn; then exit 1; fi
if command -v iscsiadm >/dev/null; then echo installed=yes; else echo installed=no; fi
printf 'active=%s\n' "$(systemctl is-active iscsid || true)"
printf 'enabled=%s\n' "$(systemctl is-enabled iscsid || true)"
''')
            prior = dict(line.split('=', 1) for line in probe.splitlines() if '=' in line)
            if self.get('node', node)['metadata'].get('labels', {}).get('kubeauto.io/mongodb-storage') is not None:
                raise ValueError('refusing an existing MongoDB storage node label')
            state['workers'][node] = prior
            write_state(self.state_file, state)
            self.ssh(ip, 'command -v iscsiadm >/dev/null || dnf -y install iscsi-initiator-utils\n'
                          'modprobe iscsi_tcp\nsystemctl start iscsid\n')
            self.k('label', 'node', node, 'kubeauto.io/mongodb-storage=true')
        namespace = {'apiVersion': 'v1', 'kind': 'Namespace', 'metadata': {'name': NAMESPACE, 'labels': LABELS}}
        self.k('create', '-f', '-', input_text=json.dumps(namespace))
        values_file = self.state_file.with_name('mongodb-longhorn-values.json')
        values_file.write_text(json.dumps(values))
        print('MONGODB_STORAGE_HEARTBEAT stage=chart-convergence timeout=20m', flush=True)
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
        self.wait_health()
        print('MONGODB_STORAGE_LAB_PASS storageclass=mongodb-longhorn version=1.10.1')

    def cleanup(self):
        if not self.state_file.exists():
            self.verify_clean()
            return
        state = json.loads(self.state_file.read_text())
        current = node_addresses(self.get('nodes')['items'])
        if (current != state['nodes'] or self.get('namespace', 'kube-system')['metadata']['uid'] !=
                state['cluster_uid'] or state.get('namespace') != NAMESPACE):
            raise ValueError('MongoDB CSI cleanup cluster ownership changed')
        namespace = self.get('namespace', NAMESPACE)
        sc = self.get('storageclass', STORAGE_CLASS)
        if any(obj and not self.owned(obj) for obj in (namespace, sc)):
            raise ValueError('refusing foreign CSI namespace or StorageClass')
        if any(v['spec'].get('storageClassName') == STORAGE_CLASS for v in self.get('pv')['items']):
            raise ValueError('MongoDB CSI still has dependent persistent volumes')
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
                raise ValueError('invalid MongoDB CSI worker journal')
            self.ssh(ip, 'test -z "$(findmnt -rn -o TARGET | '
                          'awk \'$0 ~ /^\\/var\\/lib\\/kubeauto-mongodb-longhorn(\\/|$)/\')"\n'
                          'rm -rf -- /var/lib/kubeauto-mongodb-longhorn\n' +
                          ('systemctl stop iscsid\n' if prior['active'] != 'active' else '') +
                          ('dnf -y remove iscsi-initiator-utils\n' if prior['installed'] == 'no' else ''))
            self.k('label', 'node', node, 'kubeauto.io/mongodb-storage-')
        self.state_file.with_name('mongodb-longhorn-values.json').unlink(missing_ok=True)
        self.state_file.unlink()
        self.verify_clean()


class ReplicaDomains:
    """Temporary fifth-domain capacity; return replicas through Longhorn first.

    The enclosing fixed runner owns the six-node lease. No external host is
    added, and the customer's hostname anti-affinity is never weakened.
    """
    def __init__(self, storage, run_id):
        if len(run_id) != 16 or any(c not in '0123456789abcdef' for c in run_id):
            raise ValueError('MongoDB replica domains require a hexadecimal run identity')
        self.storage, self.run_id = storage, run_id
        self.path = storage.state_file.with_name('mongodb-fault-replica-domains.json')
        self.state = None

    def save(self):
        write_state(self.path, self.state)

    def verify(self):
        state = json.loads(self.path.read_text())
        if (self.path.is_symlink() or state.get('run_id') != self.run_id or
                state.get('cluster_uid') != self.storage.get('namespace', 'kube-system')['metadata']['uid'] or
                set(state.get('nodes', {})) != set(REPLICA_SPARES)):
            raise ValueError('MongoDB replica-domain journal identity changed')
        for name, row in state['nodes'].items():
            node = self.storage.get('node', name)
            if (row.get('name') != name or row.get('ip') != REPLICA_SPARES[name] or
                    node['metadata']['uid'] != row.get('uid') or
                    [a['address'] for a in node['status']['addresses'] if a['type'] == 'InternalIP'] != [row['ip']] or
                    row.get('host', {}).get('installed') not in ('yes', 'no') or
                    row.get('host', {}).get('active') not in ('active', 'inactive') or
                    (row.get('host', {}).get('enabled') not in ('enabled', 'disabled', 'static', 'not-found') and
                     row.get('host') != {'installed': 'no', 'active': 'inactive', 'enabled': ''}) or
                    not isinstance(row.get('taints'), list) or not isinstance(row.get('unschedulable'), bool)):
                raise ValueError('MongoDB replica-domain node or host identity changed')
            labels = node['metadata'].get('labels', {})
            marker = labels.get('kubeauto.io/mongodb-replica-domain')
            label = labels.get('kubeauto.io/mongodb-storage')
            unschedulable = node['spec'].get('unschedulable', False)
            taints = node['spec'].get('taints', [])
            # The Kubernetes node controller adds/removes this exact taint
            # asynchronously around drain/uncordon; no other taint may drift.
            generated = {'key': 'node.kubernetes.io/unschedulable', 'effect': 'NoSchedule'}
            normalized = [t for t in taints if not (unschedulable and
                {k: v for k, v in t.items() if k != 'timeAdded'} == generated)]
            prior = (taints, unschedulable)
            active = (normalized, unschedulable)
            original = marker is None and label is None and prior == (row['taints'], row['unschedulable'])
            promoted = marker == self.run_id and label == 'true' and active in (([], False), ([], True))
            if not (original or promoted):
                raise ValueError('MongoDB replica-domain scheduling or labels changed externally')
        self.state = state

    def patch(self, name, spec, labels):
        self.verify()
        node = self.storage.get('node', name)
        self.storage.k('patch', 'node', name, '--type=merge', '-p', json.dumps({
            'metadata': {'uid': node['metadata']['uid'], 'resourceVersion': node['metadata']['resourceVersion'],
                         'labels': labels}, 'spec': spec}))

    def wait(self, stage, predicate, timeout=600):
        deadline = time.monotonic() + timeout
        while True:
            self.verify()
            if predicate():return
            if time.monotonic() >= deadline:
                raise ValueError('MongoDB replica-domain deadline exceeded stage=' + stage)
            print('MONGODB_REPLICA_DOMAIN_HEARTBEAT stage=' + stage, flush=True)
            time.sleep(5)

    def ready(self):
        nodes = json.loads(self.storage.k('-n', NAMESPACE, 'get', 'nodes.longhorn.io', '-o', 'json'))['items']
        healthy = {n['metadata']['name'] for n in nodes if any(
            c['type'] == 'Ready' and c['status'] == 'True' for c in n['status'].get('conditions', []))}
        if healthy != set(REPLICA_SPARES) | {'mongodb-node-' + ip.rsplit('.', 1)[1] for ip in WORKERS}:
            return False
        for name in REPLICA_SPARES:
            pods = json.loads(self.storage.k('-n', NAMESPACE, 'get', 'pods',
                '--field-selector=spec.nodeName=' + name, '-o', 'json'))['items']
            for prefix in ('longhorn-manager-', 'longhorn-csi-plugin-'):
                matching = [p for p in pods if p['metadata']['name'].startswith(prefix)]
                if len(matching) != 1 or not any(c['type'] == 'Ready' and c['status'] == 'True'
                        for c in matching[0].get('status', {}).get('conditions', [])):return False
        return True

    def prepare(self):
        if self.path.exists() or self.path.is_symlink():
            raise ValueError('MongoDB replica-domain preparation requires recovery of its journal')
        self.storage.health()
        rows = {name: replica_spare(self.storage.get('node', name)) for name in REPLICA_SPARES}
        # Validate BOTH hosts before installing or untainting either one.
        for row in rows.values():
            if self.storage.k('-n', NAMESPACE, 'get', 'nodes.longhorn.io', row['name'], '--ignore-not-found', '-o', 'name'):
                raise ValueError('MongoDB replica spare already has Longhorn state')
            probe = self.storage.ssh(row['ip'], '''
test ! -e /var/lib/kubeauto-mongodb-longhorn
test ! -L /var/lib/kubeauto-mongodb-longhorn
if command -v iscsiadm >/dev/null; then echo installed=yes; else echo installed=no; fi
printf 'active=%s\n' "$(systemctl is-active iscsid || true)"
printf 'enabled=%s\n' "$(systemctl is-enabled iscsid || true)"
''')
            row['host'] = dict(line.split('=', 1) for line in probe.splitlines() if '=' in line)
        self.state = {'run_id': self.run_id,
            'cluster_uid': self.storage.get('namespace', 'kube-system')['metadata']['uid'], 'nodes': rows}
        self.save();self.verify()
        for name in rows:
            self.storage.ssh(rows[name]['ip'], 'command -v iscsiadm >/dev/null || dnf -y install iscsi-initiator-utils\n'
                'modprobe iscsi_tcp\nsystemctl start iscsid\n')
            self.patch(name, {'unschedulable': False, 'taints': []},
                {'kubeauto.io/mongodb-storage': 'true', 'kubeauto.io/mongodb-replica-domain': self.run_id})
        self.wait('five-real-csi-domains', self.ready)
        print('MONGODB_REPLICA_DOMAINS_READY hostnames=5 csi=true lease=existing-six-nodes', flush=True)

    def empty(self, name):
        for kind in ('replicas.longhorn.io', 'engines.longhorn.io'):
            rows = json.loads(self.storage.k('-n', NAMESPACE, 'get', kind, '-o', 'json'))['items']
            if any(r['spec'].get('nodeID') == name for r in rows):return False
        return True

    def removable(self, name):
        pods = json.loads(self.storage.k('-n', NAMESPACE, 'get', 'pods',
            '--field-selector=spec.nodeName=' + name, '-o', 'json'))['items']
        raw = self.storage.k('-n', NAMESPACE, 'get', 'nodes.longhorn.io', name, '--ignore-not-found', '-o', 'json')
        if pods or not self.empty(name):return False
        if not raw:return True
        node = json.loads(raw)
        conditions = [c for c in node.get('status', {}).get('conditions', []) if c['type'] == 'Ready']
        return (node['spec'].get('allowScheduling') is False and len(conditions) == 1 and
            conditions[0]['status'] != 'True' and conditions[0].get('reason') in
            ('ManagerPodMissing', 'KubernetesNodeGone'))

    def restore(self):
        if not self.path.exists():return
        self.verify()
        for name in REPLICA_SPARES:
            raw = self.storage.k('-n', NAMESPACE, 'get', 'nodes.longhorn.io', name, '--ignore-not-found', '-o', 'json')
            if raw:
                node = json.loads(raw)
                self.storage.k('-n', NAMESPACE, 'patch', 'nodes.longhorn.io', name, '--type=merge', '-p',
                    json.dumps({'metadata': {'uid': node['metadata']['uid'], 'resourceVersion': node['metadata']['resourceVersion']},
                        'spec': {'allowScheduling': False, 'evictionRequested': True}}))
                self.storage.k('drain', name, '--ignore-daemonsets', '--delete-emptydir-data',
                    '--timeout=600s', timeout=650)
                self.wait('evict-replicas-' + name, lambda: self.empty(name))
            row = self.state['nodes'][name]
            self.patch(name, {'taints': row['taints'], 'unschedulable': row['unschedulable']},
                {'kubeauto.io/mongodb-storage': None, 'kubeauto.io/mongodb-replica-domain': None})
            self.wait('remove-manager-' + name, lambda: self.removable(name))
            self.storage.k('-n', NAMESPACE, 'delete', 'nodes.longhorn.io', name,
                '--ignore-not-found', '--wait=true', '--timeout=180s', timeout=200)
            host = row['host']
            self.storage.ssh(row['ip'], r'''
test ! -L /var/lib/kubeauto-mongodb-longhorn
test -z "$(findmnt -rn -o SOURCE,TARGET | awk '$0 ~ /\/dev\/longhorn\// || $0 ~ /\/var\/lib\/kubeauto-mongodb-longhorn(\/|$)/')"
rm -rf -- /var/lib/kubeauto-mongodb-longhorn
''' + ('if systemctl is-active --quiet iscsid; then systemctl stop iscsid; fi\n'
       if host['active'] != 'active' else '') +
                ('if command -v iscsiadm >/dev/null; then dnf -y remove iscsi-initiator-utils; fi\n'
                 if host['installed'] == 'no' else ''))
            probe = self.storage.ssh(row['ip'], '''
test ! -e /var/lib/kubeauto-mongodb-longhorn
test ! -L /var/lib/kubeauto-mongodb-longhorn
if command -v iscsiadm >/dev/null; then echo installed=yes; else echo installed=no; fi
printf 'active=%s\n' "$(systemctl is-active iscsid || true)"
printf 'enabled=%s\n' "$(systemctl is-enabled iscsid || true)"
''')
            if dict(line.split('=', 1) for line in probe.splitlines() if '=' in line) != host:
                raise ValueError('MongoDB replica-domain host restoration differs from its original state')
            self.state['nodes'][name]['restored'] = True;self.save()
        self.verify();self.storage.wait_health()
        self.path.unlink()
        print('MONGODB_REPLICA_DOMAINS_CLEAN_PASS scheduling-taints-host=restored extra-csi-replicas=absent', flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=('render', 'artifacts', 'prepare', 'health', 'probe', 'reclaim', 'cleanup', 'verify'))
    parser.add_argument('--base', type=Path, default=Path('/usr/local/kubeauto'))
    parser.add_argument('--kubeconfig', type=Path)
    parser.add_argument('--helm', type=Path)
    args = parser.parse_args()
    args.kubeconfig = args.kubeconfig or args.base / 'clusters/mongodb-gate/kubectl.kubeconfig'
    args.helm = args.helm or args.base / 'extra-bin/helm'
    if args.mode == 'render':
        values, _ = image_plan(args.base / 'tests/fixtures/longhorn-1.10.1.tgz',
                               json.loads((args.base / 'tests/evidence/redis-storage-images-20261012.json').read_text()))
        print(json.dumps(values))
    else:
        fixture = Fixture(args)
        {'artifacts': fixture.artifacts, 'prepare': fixture.prepare, 'probe': fixture.probe,
         'health': fixture.health, 'reclaim': fixture.reclaim, 'cleanup': fixture.cleanup, 'verify': fixture.verify_clean}[args.mode]()


if __name__ == '__main__':
    signal.signal(signal.SIGTERM, lambda signum, frame: sys.exit(128 + signum))
    main()
