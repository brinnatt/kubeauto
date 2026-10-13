#!/usr/bin/env python3
"""Real drain and sequential rolling restart without weakening Redis PDBs."""
import argparse
from contextlib import ExitStack
import importlib.util
import json
from pathlib import Path
import re
import signal
import tempfile
import time


def load(base, filename, name):
    spec = importlib.util.spec_from_file_location(name, base / 'tests/helpers' / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def spare_contract(node):
    metadata, spec = node['metadata'], node['spec']
    if (metadata['name'] != 'redis-node-134' or
            [a['address'] for a in node['status']['addresses'] if a['type'] == 'InternalIP'] != ['192.168.47.134'] or
            metadata.get('labels', {}).get('kubeauto.io/redis-storage') is not None or
            not any(c['type'] == 'Ready' and c['status'] == 'True' for c in node['status']['conditions']) or
            any(t['effect'] != 'NoSchedule' or
                (t['key'] not in ('node-role.kubernetes.io/master', 'node-role.kubernetes.io/control-plane') and
                 not (t['key'] == 'node.kubernetes.io/unschedulable' and spec.get('unschedulable') is True))
                for t in spec.get('taints', []))):
        raise ValueError('maintenance spare must be the healthy unlabelled leased master 134')
    return {'uid': metadata['uid'], 'unschedulable': spec.get('unschedulable', False),
            'taints': spec.get('taints', [])}


def pdb_contract(pdbs):
    specs = {p['metadata']['name']: p['spec'] for p in pdbs}
    if len(specs) != len(pdbs):
        raise ValueError('maintenance disruption budget identities are duplicated')
    for app in ('redis-gate-data', 'redis-gate-data-sentinel-sentinel'):
        matching = [s for s in specs.values() if s.get('selector', {}).get('matchLabels', {}).get('app') == app]
        if len(matching) != 1 or matching[0].get('minAvailable') != 2:
            raise ValueError('Sentinel maintenance requires the upstream two-of-three PDB budget')
    return specs


class Maintenance:
    def __init__(self, source, cluster, focused, stability, storage, *, repair=False):
        self.source, self.cluster, self.focused = source, cluster, focused
        self.journal = source.cluster / 'redis-maintenance-fault.json'
        self.changed = False
        if repair:
            self.state = self.repair_identity()
            source.run_id = self.state['run_id']
            source.key = 'kubeauto:redis:focused:' + source.run_id
            source.value = ('verified-' + source.run_id).encode()
            self.changed = True
        elif self.journal.exists():
            raise ValueError('maintenance refuses an unrecovered node journal')
        self.retained = stability.Stability(source, cluster, focused)
        self.storage = storage.Fixture(argparse.Namespace(base=source.base, kubeconfig=source.kubeconfig,
                                                         helm=source.base / 'extra-bin/helm'))
        self.pdbs = pdb_contract(source.get('pdb')['items'])

    def repair_identity(self):
        if self.journal.is_symlink() or not self.journal.is_file():
            raise ValueError('maintenance repair requires its regular owned journal')
        state = json.loads(self.journal.read_text())
        prior = state.get('spare', {})
        if (not re.fullmatch(r'[0-9a-f]{32}', state.get('run_id', '')) or
                state.get('cluster_uid') != self.cluster.journal['cluster_uid'] or
                prior.get('host', {}).get('installed') not in ('yes', 'no') or
                prior.get('host', {}).get('active') not in ('active', 'inactive') or
                not isinstance(prior.get('unschedulable'), bool) or
                not isinstance(prior.get('taints'), list)):
            raise ValueError('maintenance repair journal ownership or host state is invalid')
        for name, uid in [('redis-node-131', state.get('worker_uid')),
                          ('redis-node-134', prior.get('uid'))]:
            node = json.loads(self.source.k('get', 'node', name, '-o', 'json'))
            addresses = [a['address'] for a in node['status']['addresses'] if a['type'] == 'InternalIP']
            if (not uid or node['metadata']['uid'] != uid or
                    addresses != ['192.168.47.' + name.rsplit('-', 1)[1]]):
                raise ValueError('maintenance repair refuses a changed node identity')
        return state

    def wait_spare_removable(self):
        deadline = time.monotonic() + 300
        while time.monotonic() < deadline:
            pods = json.loads(self.source.k('-n', 'redis-lab-storage', 'get', 'pods',
                              '--field-selector=spec.nodeName=redis-node-134', '-o', 'json'))['items']
            raw = self.source.k('-n', 'redis-lab-storage', 'get', 'nodes.longhorn.io',
                                'redis-node-134', '--ignore-not-found', '-o', 'json')
            if not raw and not pods:
                return
            if raw:
                node = json.loads(raw)
                conditions = [c for c in node['status'].get('conditions', []) if c['type'] == 'Ready']
                empty = True
                for kind in ('replicas.longhorn.io', 'engines.longhorn.io'):
                    objects = json.loads(self.source.k('-n', 'redis-lab-storage', 'get', kind, '-o', 'json'))['items']
                    empty = empty and not any(o['spec'].get('nodeID') == 'redis-node-134' for o in objects)
                if (not pods and len(conditions) == 1 and conditions[0]['status'] != 'True' and
                        conditions[0].get('reason') in ('ManagerPodMissing', 'KubernetesNodeGone') and
                        node['spec'].get('allowScheduling') is False and empty):
                    return
            print('REDIS_MAINTENANCE_HEARTBEAT stage=spare-manager-removal', flush=True)
            time.sleep(2)
        raise ValueError('maintenance spare does not meet upstream Longhorn node deletion conditions')

    def verify(self):
        self.retained.verify_retained()
        if pdb_contract(self.source.get('pdb')['items']) != self.pdbs:
            raise ValueError('maintenance changed a customer disruption budget')

    def rolling(self):
        before = self.source.members()
        for role in ('replication', 'sentinel'):
            groups = before[role]
            sts = groups[0]['metadata']['labels']['app']
            current = self.source.get('statefulset', sts)
            if current['spec'].get('updateStrategy', {}).get('type', 'RollingUpdate') != 'RollingUpdate':
                raise ValueError('maintenance requires the declared rolling update strategy')
            old = {p['metadata']['name']: p['metadata']['uid'] for p in groups}
            self.source.k('-n', self.source.ns, 'rollout', 'restart', 'statefulset/' + sts)
            deadline = time.monotonic() + 600
            while time.monotonic() < deadline:
                pods = self.source.members()[role]
                if len(pods) != 3:
                    raise ValueError('rolling maintenance changed declared member count')
                healthy = [p for p in pods if not p['metadata'].get('deletionTimestamp') and any(
                    c['type'] == 'Ready' and c['status'] == 'True' for c in p['status'].get('conditions', []))]
                if len(healthy) < 2:
                    raise ValueError('rolling maintenance violated two-of-three availability')
                if all(p['metadata']['uid'] != old[p['metadata']['name']] for p in healthy) and len(healthy) == 3:
                    self.source.forwards.clear()
                    self.verify()
                    break
                print('REDIS_MAINTENANCE_HEARTBEAT stage=rolling role=' + role, flush=True)
                time.sleep(2)
            else:
                raise ValueError('rolling maintenance did not replace all declared Pods')
        print('REDIS_ROLLING_MAINTENANCE_PASS pods=6 max-unavailable=1 data-and-pvc=retained', flush=True)

    def patch_node(self, name, spec, labels=None):
        obj = json.loads(self.source.k('get', 'node', name, '-o', 'json'))
        body = {'metadata': {'uid': obj['metadata']['uid'], 'resourceVersion': obj['metadata']['resourceVersion']},
                'spec': spec}
        if labels is not None:
            body['metadata']['labels'] = labels
        self.source.k('patch', 'node', name, '--type=merge', '-p', json.dumps(body))

    def wait_storage(self, count):
        deadline = time.monotonic() + 300
        while time.monotonic() < deadline:
            nodes = json.loads(self.source.k('-n', 'redis-lab-storage', 'get', 'nodes.longhorn.io', '-o', 'json'))['items']
            ready = [n for n in nodes if any(c['type'] == 'Ready' and c['status'] == 'True'
                                            for c in n['status'].get('conditions', []))]
            pods = json.loads(self.source.k('-n', 'redis-lab-storage', 'get', 'pods',
                                            '--field-selector=spec.nodeName=redis-node-134', '-o', 'json'))['items']
            plugin = [p for p in pods if p['metadata']['name'].startswith('longhorn-csi-plugin-')]
            if len(ready) == count and plugin and all(any(c['type'] == 'Ready' and c['status'] == 'True'
                    for c in p['status'].get('conditions', [])) for p in plugin):
                return
            print('REDIS_MAINTENANCE_HEARTBEAT stage=spare-csi ready=' + str(len(ready)), flush=True)
            time.sleep(3)
        raise ValueError('maintenance spare CSI did not become ready')

    def prepare_spare(self):
        node = json.loads(self.source.k('get', 'node', 'redis-node-134', '-o', 'json'))
        prior = spare_contract(node)
        probe = self.storage.ssh('192.168.47.134', '''
test ! -e /var/lib/kubeauto-redis-longhorn
if command -v iscsiadm >/dev/null; then echo installed=yes; else echo installed=no; fi
printf 'active=%s\n' "$(systemctl is-active iscsid || true)"
''')
        prior['host'] = dict(line.split('=', 1) for line in probe.splitlines() if '=' in line)
        worker = json.loads(self.source.k('get', 'node', 'redis-node-131', '-o', 'json'))
        if worker['spec'].get('unschedulable'):
            raise ValueError('maintenance worker already cordoned')
        self.state = {'run_id': self.source.run_id, 'cluster_uid': self.cluster.journal['cluster_uid'],
                      'spare': prior, 'worker_uid': worker['metadata']['uid']}
        with self.journal.open('x') as out:
            self.journal.chmod(0o600)
            json.dump(self.state, out)
        self.changed = True
        self.storage.ssh('192.168.47.134', 'command -v iscsiadm >/dev/null || dnf -y install iscsi-initiator-utils\n'
                         'modprobe iscsi_tcp\nsystemctl start iscsid\n')
        self.patch_node('redis-node-134', {'unschedulable': False, 'taints': []},
                        {'kubeauto.io/redis-storage': 'true'})
        self.wait_storage(4)
        print('REDIS_MAINTENANCE_SPARE_PASS node=redis-node-134 lease=existing-core csi=ready', flush=True)

    def drain(self):
        self.prepare_spare()
        # The upstream drain command uses Eviction API; force/delete is forbidden.
        self.source.k('drain', 'redis-node-131', '--ignore-daemonsets', '--delete-emptydir-data',
                      '--timeout=10m', timeout=650)
        pods = json.loads(self.source.k('get', 'pods', '-A', '--field-selector=spec.nodeName=redis-node-131', '-o', 'json'))['items']
        if any(p['metadata'].get('namespace') == self.source.ns for p in pods):
            raise ValueError('node drain left a Redis member on the maintenance node')
        self.source.forwards.clear()
        self.verify()
        print('REDIS_NODE_DRAIN_PASS node=redis-node-131 eviction-api=used pdb=unchanged data-and-pvc=retained', flush=True)

    def restore(self):
        if not self.changed:
            return
        state = json.loads(self.journal.read_text())
        if state != self.state:
            raise ValueError('maintenance cleanup refuses a changed journal')
        for name, uid in [('redis-node-131', state['worker_uid']), ('redis-node-134', state['spare']['uid'])]:
            if json.loads(self.source.k('get', 'node', name, '-o', 'json'))['metadata']['uid'] != uid:
                raise ValueError('maintenance node identity changed during cleanup')
        self.source.k('uncordon', 'redis-node-131')
        # Evict Longhorn replicas through its owning controller before removing
        # the temporary storage label or host data directory.
        raw = self.source.k('-n', 'redis-lab-storage', 'get', 'nodes.longhorn.io', 'redis-node-134',
                            '--ignore-not-found', '-o', 'json')
        if raw:
            self.source.k('-n', 'redis-lab-storage', 'patch', 'nodes.longhorn.io', 'redis-node-134',
                          '--type=merge', '-p', '{"spec":{"allowScheduling":false,"evictionRequested":true}}')
            self.source.k('drain', 'redis-node-134', '--ignore-daemonsets', '--delete-emptydir-data',
                          '--timeout=10m', timeout=650)
            deadline = time.monotonic() + 300
            while time.monotonic() < deadline:
                replicas = json.loads(self.source.k('-n', 'redis-lab-storage', 'get', 'replicas.longhorn.io', '-o', 'json'))['items']
                if not any(r['spec'].get('nodeID') == 'redis-node-134' for r in replicas):
                    break
                print('REDIS_MAINTENANCE_HEARTBEAT stage=spare-replica-eviction', flush=True)
                time.sleep(3)
            else:
                raise ValueError('maintenance spare still owns Longhorn replicas')
        self.source.forwards.clear()
        self.verify()
        prior = state['spare']
        self.patch_node('redis-node-134', {'unschedulable': prior['unschedulable'], 'taints': prior['taints']},
                        {'kubeauto.io/redis-storage': None})
        # Longhorn v1.10.1 admission rejects a Ready node, even after replicas
        # have been evacuated. Wait for the manager/CSI and its status update.
        self.wait_spare_removable()
        self.source.k('-n', 'redis-lab-storage', 'delete', 'nodes.longhorn.io', 'redis-node-134',
                      '--ignore-not-found', '--wait=true', '--timeout=180s', timeout=200)
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            pods = json.loads(self.source.k('-n', 'redis-lab-storage', 'get', 'pods',
                                            '--field-selector=spec.nodeName=redis-node-134', '-o', 'json'))['items']
            if not pods:
                break
            time.sleep(2)
        else:
            raise ValueError('maintenance spare CSI processes remain')
        self.storage.ssh('192.168.47.134', 'test -z "$(findmnt -rn -o TARGET | '
                         'awk \'$0 ~ /^\\/var\\/lib\\/kubeauto-redis-longhorn(\\/|$)/\')"\n'
                         'rm -rf -- /var/lib/kubeauto-redis-longhorn\n' +
                         ('systemctl stop iscsid\n' if prior['host']['active'] != 'active' else '') +
                         ('dnf -y remove iscsi-initiator-utils\n' if prior['host']['installed'] == 'no' else ''))
        self.storage.health()
        self.journal.unlink()
        print('REDIS_MAINTENANCE_NODE_CLEAN_PASS scheduling-and-taints=restored spare-csi=removed', flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('module', choices=('maintenance-rolling', 'maintenance-drain', 'maintenance-repair'))
    parser.add_argument('--base', type=Path, default=Path('/usr/local/kubeauto'))
    args = parser.parse_args()
    def terminate(signum, frame):
        raise SystemExit(128 + signum)
    signal.signal(signal.SIGTERM, terminate)
    focused = load(args.base, 'redis-focused-gate.py', 'redis_maintenance_focused')
    clients = load(args.base, 'redis-cluster-client-gate.py', 'redis_maintenance_cluster')
    stability = load(args.base, 'redis-stability-gate.py', 'redis_maintenance_retained')
    storage = load(args.base, 'redis-lab-storage.py', 'redis_maintenance_storage')
    with tempfile.TemporaryDirectory(prefix='redis-maintenance-') as tmp, ExitStack() as stack:
        source = focused.Gate(args, Path(tmp), stack)
        repair = args.module == 'maintenance-repair'
        if repair:
            source.identity()
            if (source.get('networkpolicy')['items'] or
                    any(p.name != 'redis-maintenance-fault.json' for p in source.cluster.glob('*fault*'))):
                raise ValueError('maintenance repair refuses unrelated fault residue')
            source.health()
        else:
            source.baseline()
        cluster = clients.Cluster(source, focused, Path(tmp))
        cluster.health()
        gate = Maintenance(source, cluster, focused, stability, storage, repair=repair)
        failure = None
        try:
            if repair:
                gate.verify()
            elif args.module == 'maintenance-rolling':
                gate.retained.sample()
                gate.rolling()
            else:
                gate.retained.sample()
                gate.drain()
        except BaseException as exc:
            failure = exc
            raise
        finally:
            try:
                gate.restore()
                cluster.cleanup()
                source.cleanup_keys()
            except BaseException:
                if failure is None:
                    raise
                print('REDIS_MAINTENANCE_CLEANUP_FAIL original-failure=retained journal=retained', flush=True)
    print('REDIS_FOCUSED_SCOPED_CLEAN_PASS module=' + args.module, flush=True)
    print('REDIS_FOCUSED_MODULE_PASS module=' + args.module, flush=True)


if __name__ == '__main__':
    main()
