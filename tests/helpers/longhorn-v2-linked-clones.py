#!/usr/bin/env python3
"""Qualify new V2 linked/nested clone data and native dependency protection.

A separate journal owns the mixed-class graph. It never sends linked entrypoint
snapshots through the generic snapshot-first cleanup path.
"""
import argparse
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import secrets
import sys

BASE = Path('/usr/local/kubeauto')
NS = 'longhorn-system'
LABEL = 'longhorn.io/linked-clone-source-snapshot'


def linked_topology(source, clone, source_replicas, clone_replicas):
    """Check immutable ancestry and each of the three same-node/same-disk pairs."""
    status = clone.get('status', {}).get('cloneStatus', {})
    if (source['spec'].get('dataEngine') != 'v2' or clone['spec'].get('dataEngine') != 'v2' or clone['spec'].get('cloneMode') != 'linked-clone' or
            status.get('sourceVolume') != source['metadata']['name'] or status.get('state') != 'completed' or
            not status.get('snapshot') or clone['metadata'].get('labels', {}).get(LABEL) != status['snapshot']):
        raise ValueError('linked clone lacks completed native ancestry and entrypoint label')
    if len(source_replicas) != 3 or len(clone_replicas) != 3:
        raise ValueError('linked clone requires three source and three child replicas')
    parents = {r['metadata']['name']: r for r in source_replicas}
    if len(parents) != 3 or len({r['spec'].get('nodeID') for r in source_replicas}) != 3:
        raise ValueError('source replicas do not span three distinct nodes')
    result = {}
    used = set()
    for child in clone_replicas:
        spec = child['spec']; parent_name = spec.get('linkedCloneSrcReplicaName')
        parent = parents.get(parent_name)
        if (not parent or parent_name in used or spec.get('volumeName') != clone['metadata']['name'] or
                parent['spec'].get('volumeName') != source['metadata']['name'] or
                any(not spec.get(k) or spec[k] != parent['spec'].get(k) for k in ('nodeID', 'diskID')) or
                child['metadata']['uid'] == parent['metadata']['uid']):
            raise ValueError('linked clone replica ancestry or node/disk placement differs')
        used.add(parent_name)
        result[child['metadata']['name']] = {'uid': child['metadata']['uid'], 'source': parent_name,
            'source_uid': parent['metadata']['uid'], 'node': spec['nodeID'], 'disk': spec['diskID']}
    return result


class Probe:
    def __init__(self, lab):
        self.lab = lab
        self.record = lab.DIRECTORY/'longhorn-v2-linked-clones.json'
        self.journal = None

    def save(self):
        self.lab.write_private(self.record, self.journal)

    def current(self, kind, name, uid, namespace=''):
        obj = self.lab.get(kind, name, namespace)
        if obj and obj['metadata']['uid'] != uid:
            raise ValueError('linked clone fixture identity replaced: '+kind+'/'+name)
        return obj

    def create(self, obj):
        return self.lab.create_business_object(self.journal, self.record, obj)

    def bind(self, name):
        lab = self.lab; j = self.journal
        uid = j['objects'].get('PersistentVolumeClaim/'+name)
        if not uid: return None
        claim = self.current('pvc', name, uid, j['namespace'])
        expected_class = j['class_name'] if name != 'source' else 'longhorn'
        if claim and claim['spec'].get('storageClassName') != expected_class:
            raise ValueError('linked claim class changed')
        matches = [p for p in lab.get('pv')['items'] if p.get('spec', {}).get('claimRef', {}).get('uid') == uid]
        if len(matches) > 1: raise ValueError('multiple PVs refer to linked claim')
        if not matches: return None
        pv = matches[0]; spec = pv['spec']; ref = spec['claimRef']
        if (ref.get('namespace') != j['namespace'] or ref.get('name') != name or
                spec.get('storageClassName') != expected_class or spec.get('persistentVolumeReclaimPolicy') != 'Retain' or
                spec.get('csi', {}).get('driver') != 'driver.longhorn.io'):
            raise ValueError('linked clone PV binding differs')
        row = j['claims'].setdefault(name, {})
        identity = {'pv': pv['metadata']['name'], 'pv_uid': pv['metadata']['uid'], 'volume': spec['csi']['volumeHandle']}
        volume = lab.get('volumes.longhorn.io', identity['volume'], NS)
        if volume:
            if volume['spec'].get('dataEngine') != 'v2': raise ValueError('linked fixture changed data engine')
            identity['volume_uid'] = volume['metadata']['uid']
        if any(k in row and row[k] != v for k, v in identity.items()):
            raise ValueError('linked clone binding was replaced')
        row.update(identity); self.save()
        return row if volume else None

    def claim(self, name, parent=None):
        lab = self.lab; j = self.journal
        obj = lab.clone_claim(j['namespace'], name, '2Gi')
        obj['spec']['storageClassName'] = j['class_name'] if parent else 'longhorn'
        if parent: obj['spec']['dataSource']['name'] = parent
        else: del obj['spec']['dataSource']
        self.create(obj)
        def bound():
            row = self.bind(name)
            claim = lab.get('pvc', name, j['namespace'])
            return row if row and claim and claim.get('status', {}).get('phase') == 'Bound' else None
        row = lab.wait('linked-'+name+'-bound', 900, bound)
        if parent:
            def completed():
                volume = self.current('volumes.longhorn.io', row['volume'], row['volume_uid'], NS)
                status = volume.get('status', {}).get('cloneStatus', {})
                if status.get('state') == 'failed': raise ValueError('linked clone failed')
                return volume if status.get('state') == 'completed' else None
            lab.wait('linked-'+name+'-completed', 900, completed)
        self.client(name, int({'source': 0, 'clone': 1, 'nested': 2}[name]))
        lab.wait('linked-'+name+'-healthy', 900, lambda:
            lab.replica_topology(row, 3) if lab.healthy_replicas(row['volume']) else None)
        return row

    def client(self, name, index, suffix=''):
        lab = self.lab; j = self.journal; pod = name+suffix
        self.create(lab.client_pod(j['namespace'], pod, name, 'longhorn-node-'+lab.WORKERS[index].rsplit('.', 1)[1]))
        lab.wait('linked-'+pod+'-ready', 600, lambda: any(c['type'] == 'Ready' and c['status'] == 'True'
            for c in (lab.get('pod', pod, j['namespace']) or {}).get('status', {}).get('conditions', [])))
        return pod

    def io(self, pod, value, *, read=False):
        actual = self.lab.kubectl('-n', self.journal['namespace'], 'exec', pod, '--', 'python3', '-c',
            self.lab.payload_program(value, read_only=read)).stdout.strip()
        digest = hashlib.sha256(value.encode()*262144).hexdigest()
        if actual != digest: raise ValueError('linked clone application bytes differ: '+pod)
        return digest

    def graph(self, child, parent):
        lab = self.lab; j = self.journal
        c = j['claims'][child]; p = j['claims'][parent]
        source = self.current('volumes.longhorn.io', p['volume'], p['volume_uid'], NS)
        clone = self.current('volumes.longhorn.io', c['volume'], c['volume_uid'], NS)
        replicas = lab.get('replicas.longhorn.io', namespace=NS)['items']
        topology = linked_topology(source, clone,
            [r for r in replicas if r['spec'].get('volumeName') == p['volume']],
            [r for r in replicas if r['spec'].get('volumeName') == c['volume']])
        snapshot = lab.get('snapshots.longhorn.io', clone['status']['cloneStatus']['snapshot'], NS)
        if not snapshot or snapshot['spec'].get('volume') != p['volume'] or not snapshot.get('status', {}).get('readyToUse'):
            raise ValueError('linked clone entrypoint is not a ready source snapshot')
        return source, clone, topology, snapshot

    def refused(self, kind, obj, message, *, replicas=None):
        """Independent native admission check; even a broken guard cannot delete data."""
        metadata = obj['metadata']; name = metadata['name']
        if replicas is None:
            result = self.lab.kubectl('-n', NS, 'delete', kind, name, '--dry-run=server', '--wait=false', check=False)
        else:
            patch = [{'op': 'test', 'path': '/metadata/uid', 'value': metadata['uid']},
                     {'op': 'test', 'path': '/metadata/resourceVersion', 'value': metadata['resourceVersion']},
                     {'op': 'replace', 'path': '/spec/numberOfReplicas', 'value': replicas}]
            result = self.lab.kubectl('-n', NS, 'patch', kind, name, '--type=json', '--dry-run=server',
                '--patch-file=/dev/stdin', stdin=json.dumps(patch), check=False)
        if not result.returncode or message not in result.stdout+result.stderr:
            raise ValueError('linked clone native admission did not return the exact refusal: '+message)
        current = self.current(kind, name, metadata['uid'], NS)
        if not current or current['metadata'].get('deletionTimestamp') or current['spec'] != obj['spec']:
            raise ValueError('dry-run linked guard changed its protected object')
        print('LONGHORN_V2_LINKED_GUARD_REFUSED '+json.dumps({'kind': kind, 'uid': metadata['uid'],
            'reason': message, 'replicas': replicas, 'dry_run': True}, sort_keys=True), flush=True)

    def delete_pod(self, name):
        j = self.journal; uid = j['objects'].get('Pod/'+name)
        if not uid: return
        obj = self.current('pod', name, uid, j['namespace'])
        if obj: self.lab.remove(obj)
        self.lab.wait('linked-pod-clean-'+name, 180, lambda: self.current('pod', name, uid, j['namespace']) is None)

    def expand(self, name, pod, payload):
        """Expand the disposable linked volume through its original CSI claim."""
        lab = self.lab; j = self.journal; row = j['claims'][name]
        identities = (('pvc', name, j['objects']['PersistentVolumeClaim/'+name], j['namespace']),
                      ('pv', row['pv'], row['pv_uid'], ''),
                      ('volumes.longhorn.io', row['volume'], row['volume_uid'], NS),
                      ('pod', pod, j['objects']['Pod/'+pod], j['namespace']))
        def objects():
            result = {kind: self.current(kind, resource, uid, namespace)
                      for kind, resource, uid, namespace in identities}
            if any(obj is None for obj in result.values()):
                raise ValueError('linked expansion lost an original object')
            if (result['pvc']['spec'].get('volumeName') != row['pv'] or
                    result['pv']['spec'].get('csi', {}).get('volumeHandle') != row['volume'] or
                    result['volumes.longhorn.io']['spec'].get('dataEngine') != 'v2'):
                raise ValueError('linked expansion changed its original binding or engine')
            return result
        before = objects()
        if (lab.capacity_bytes(before['pvc']['status']['capacity']['storage']) != 2*1024**3 or
                int(before['volumes.longhorn.io']['spec']['size']) != 2*1024**3):
            raise ValueError('linked expansion requires its original 2 GiB volume')
        # Expansion cannot be rolled back by shrinking. The existing graph
        # journal owns native deletion of this disposable child on every exit.
        j.setdefault('expansions', {})[name] = {'from_bytes': 2*1024**3, 'to_bytes': 4*1024**3}
        self.save()
        lab.resize_claim(before['pvc'], '4Gi')
        def ready():
            current = objects()
            return (all(lab.capacity_bytes(value) == 4*1024**3 for value in (
                        current['pvc']['spec']['resources']['requests']['storage'],
                        current['pvc'].get('status', {}).get('capacity', {}).get('storage', '0'),
                        current['pv']['spec']['capacity']['storage'])) and
                    int(current['volumes.longhorn.io']['spec']['size']) == 4*1024**3 and
                    not any(c.get('status') == 'True' and c.get('type') in ('Resizing', 'FileSystemResizePending')
                            for c in (current['pvc'].get('status', {}).get('conditions') or [])))
        lab.wait('linked-expanded-'+name, 600, ready)
        measured = json.loads(lab.kubectl('-n', j['namespace'], 'exec', pod, '--', 'python3', '-c',
                                         lab.expansion_probe_program()).stdout)
        if (measured.get('capacity_bytes', 0) <= 3*1024**3 or
                measured.get('offset') != 2*1024**3+1024**2 or
                not re.fullmatch('[0-9a-f]{64}', measured.get('sha256', ''))):
            raise ValueError('linked expansion lacks fsynced bytes beyond the old capacity')
        self.io(pod, payload, read=True)
        lab.wait('linked-expanded-replicas-'+name, 600, lambda:
            lab.replica_topology(row, 3) if lab.healthy_replicas(row['volume']) else None)
        j['expansions'][name].update(measured); self.save()
        print('LONGHORN_V2_LINKED_EXPANSION_VERIFIED '+json.dumps(
            {'claim': name, 'volume_uid': row['volume_uid'], **j['expansions'][name]}, sort_keys=True), flush=True)

    def expanded_readback(self, name, pod):
        row = self.journal['expansions'][name]
        program = ("import hashlib; f=open('/data/expanded','rb'); f.seek("+str(row['offset'])+
                   "); print(hashlib.sha256(f.read(1024**2)).hexdigest())")
        digest = self.lab.kubectl('-n', self.journal['namespace'], 'exec', pod, '--',
                                  'python3', '-c', program).stdout.strip()
        if digest != row['sha256']:
            raise ValueError('linked expanded data changed across node remount')
        print('LONGHORN_V2_LINKED_EXPANSION_REMOUNT_VERIFIED claim='+name+' sha256='+digest, flush=True)

    def retire(self, name):
        lab = self.lab; j = self.journal
        row = self.bind(name) or j['claims'].get(name, {})
        uid = j['objects'].get('PersistentVolumeClaim/'+name)
        if not uid: return
        obj = self.current('pvc', name, uid, j['namespace'])
        if obj: lab.remove(obj)
        lab.wait('linked-claim-clean-'+name, 180, lambda: self.current('pvc', name, uid, j['namespace']) is None)
        self.bind(name); row = j['claims'].get(name, row)
        if not row.get('pv'): return
        pv = self.current('pv', row['pv'], row['pv_uid'])
        if pv: lab.remove(pv)
        lab.wait('linked-pv-clean-'+name, 180, lambda: self.current('pv', row['pv'], row['pv_uid']) is None)
        volume = self.current('volumes.longhorn.io', row['volume'], row.get('volume_uid'), NS)
        if volume:
            if any(p.get('spec', {}).get('csi', {}).get('volumeHandle') == row['volume'] for p in lab.get('pv')['items']):
                raise ValueError('a PV still refers to the linked fixture volume')
            volume = lab.wait('linked-detached-'+name, 240, lambda:
                (v if v and v.get('status', {}).get('state') == 'detached' else None)
                if (v := self.current('volumes.longhorn.io', row['volume'], row['volume_uid'], NS)) else None)
            lab.remove(volume)
        lab.wait('linked-volume-clean-'+name, 240, lambda: self.current('volumes.longhorn.io', row['volume'], row.get('volume_uid'), NS) is None)

    def cleanup(self):
        lab = self.lab
        lab.restore_request_input()
        if self.record.is_symlink(): raise ValueError('symlink linked clone journal')
        if not self.record.exists(): return
        self.journal = json.loads(self.record.read_text()); j = self.journal
        if j['run_id'] != lab.owner()['run_id'] or not j['namespace'].startswith('longhorn-linked-'):
            raise ValueError('foreign linked clone journal')
        ns = self.current('namespace', j['namespace'], j.get('namespace_uid'))
        if ns:
            lab.check_business_graph(j)
            for key in j['objects']:
                if key.startswith('Pod/'): self.delete_pod(key.split('/', 1)[1])
            # Explicit dependency order; never remove an entrypoint snapshot or
            # strip a protection finalizer to force deletion of a parent.
            for name in ('nested', 'clone', 'source'): self.retire(name)
            lab.check_business_graph(j)
            lab.remove(ns)
            lab.wait('linked-namespace-clean', 180, lambda: lab.get('namespace', j['namespace']) is None)
        elif any(self.current('pv', row['pv'], row['pv_uid']) or
                 self.current('volumes.longhorn.io', row['volume'], row.get('volume_uid'), NS)
                 for row in j['claims'].values() if row.get('pv')):
            raise ValueError('linked namespace disappeared with retained PVs or native volumes remaining')
        # The qualifier admitted an empty-volume baseline. A failed CSI create
        # can leave a native object before a PV exposes its UID. Do not infer
        # deletion authority from its name, or lose this receipt on that edge.
        if (lab.get('volumes.longhorn.io', namespace=NS)['items'] or
                any(p.get('spec', {}).get('csi', {}).get('driver') == 'driver.longhorn.io'
                    for p in lab.get('pv')['items'])):
            raise ValueError('linked cleanup cannot release journal: native volumes or PVs remain')
        sc = lab.get('storageclass', j['class_name'])
        if sc:
            product = lab.load_helper('../../roles/longhorn/files/longhorn.py')
            product.require_owned(sc)
            desired = product.storage_class(j['class_config'])
            fields = ('parameters','provisioner','reclaimPolicy','allowVolumeExpansion','volumeBindingMode')
            if (j.get('class_uid', sc['metadata']['uid']) != sc['metadata']['uid'] or
                    any(sc.get(k) != desired.get(k) for k in fields) or
                    any(p['spec'].get('storageClassName') == j['class_name'] for p in lab.get('pvc', namespace='*')['items']) or
                    any(p['spec'].get('storageClassName') == j['class_name'] for p in lab.get('pv')['items'])):
                raise ValueError('linked storage class changed or has consumers')
            lab.remove(sc)
            lab.wait('linked-class-clean', 60, lambda: lab.get('storageclass', j['class_name']) is None)
        self.record.unlink()
        print('LONGHORN_V2_LINKED_FIXTURES_CLEAN_PASS', flush=True)

    def business(self, relay_pid):
        lab = self.lab
        if self.record.exists() or self.record.is_symlink(): raise ValueError('linked clone journal requires scoped recovery')
        state = lab.owner(); config = lab.yaml.safe_load((lab.DIRECTORY/'config.yml').read_text())
        classes = copy.deepcopy(config['longhorn_storage_classes'])
        sc = copy.deepcopy(next(s for s in classes if s['name'] == 'longhorn'))
        sc.update(name='longhorn-linked-'+secrets.token_hex(6), clone_mode='linked-clone', default=False, reclaim_policy='Retain')
        if lab.get('storageclass', sc['name']): raise ValueError('linked class already exists')
        self.journal = {'run_id': state['run_id'], 'namespace': 'longhorn-linked-'+secrets.token_hex(8),
            'objects': {}, 'claims': {}, 'defaults': [], 'class_name': sc['name'], 'class_config': sc}
        self.save()
        with lab.scoped_cleanup(self.cleanup):
            lab.run_requests(relay_pid, [], storage_classes=classes+[sc])
            self.journal['class_uid'] = lab.get('storageclass', sc['name'])['metadata']['uid']; self.save()
            ns = json.loads(lab.kubectl('create', 'namespace', self.journal['namespace'], '-o', 'json').stdout)
            self.journal['namespace_uid'] = ns['metadata']['uid']; self.save()
            self.journal['defaults'] = lab.wait('linked-namespace-defaults', 60, lambda: lab.namespace_defaults(self.journal['namespace'])); self.save()
            payload = state['run_id']; original = self.claim('source'); source_digest = self.io('source', payload)
            self.claim('clone', 'source'); self.io('clone', payload, read=True)
            clone_payload = payload+'-linked'; clone_digest = self.io('clone', clone_payload)
            self.io('source', payload, read=True)
            self.claim('nested', 'clone'); self.io('nested', clone_payload, read=True)
            nested_payload = payload+'-nested'; nested_digest = self.io('nested', nested_payload)
            self.io('clone', clone_payload, read=True); self.io('source', payload, read=True)
            clone_payload = payload+'-linked-new'; clone_digest = self.io('clone', clone_payload)
            self.io('nested', nested_payload, read=True); self.io('source', payload, read=True)
            changed_source = payload+'-source-new'; changed_digest = self.io('source', changed_source)
            self.io('clone', clone_payload, read=True); self.io('nested', nested_payload, read=True)
            self.expand('clone', 'clone', clone_payload)
            self.expand('nested', 'nested', nested_payload)
            graphs = {}
            for child, parent in (('clone','source'),('nested','clone')):
                source, clone, topology, snapshot = self.graph(child, parent)
                self.refused('volumes.longhorn.io', source, 'it is the source of linked-clone volume(s)')
                self.refused('snapshots.longhorn.io', snapshot, 'it is the entrypoint for linked-clone volume(s)')
                self.refused('volumes.longhorn.io', source, 'cannot decrease replica count of linked-clone source volume', replicas=2)
                self.refused('volumes.longhorn.io', clone, 'exceeds source volume', replicas=4)
                replicas = lab.get('replicas.longhorn.io', namespace=NS)['items']
                source_replica = next(r for r in replicas if r['metadata']['name'] == next(iter(topology.values()))['source'])
                self.refused('replicas.longhorn.io', source_replica, 'it is the source of linked-clone replica(s)')
                graphs[child] = {'volume_uid': clone['metadata']['uid'], 'source_uid': source['metadata']['uid'],
                    'snapshot_uid': snapshot['metadata']['uid'], 'snapshot': snapshot['metadata']['name'], 'replicas': topology}
            for name, value, index in (('source',changed_source,1),('clone',clone_payload,2),('nested',nested_payload,0)):
                self.delete_pod(name); pod = self.client(name, index, '-remount'); self.io(pod, value, read=True)
                if name in self.journal.get('expansions', {}): self.expanded_readback(name, pod)
                lab.wait('linked-remount-healthy-'+name, 600, lambda:
                    lab.replica_topology(self.journal['claims'][name], 3)
                    if lab.healthy_replicas(self.journal['claims'][name]['volume']) else None)
            for child, parent in (('clone','source'),('nested','clone')):
                _, clone, topology, snapshot = self.graph(child, parent)
                if (topology != graphs[child]['replicas'] or clone['metadata']['uid'] != graphs[child]['volume_uid'] or
                        snapshot['metadata']['uid'] != graphs[child]['snapshot_uid']):
                    raise ValueError('linked graph identities changed across client remount')
            self.delete_pod('nested-remount'); self.retire('nested')
            self.io('clone-remount', clone_payload, read=True); self.io('source-remount', changed_source, read=True)
            self.delete_pod('clone-remount'); self.retire('clone')
            self.io('source-remount', changed_source, read=True)
            print('LONGHORN_V2_LINKED_NESTED_RESULT '+json.dumps({'graphs': graphs,
                'initial_source_sha256': source_digest, 'source_sha256': changed_digest,
                'clone_sha256': clone_digest, 'nested_sha256': nested_digest,
                'cross_node_readback': True, 'source_volume_uid': original['volume_uid'],
                'guards': 10, 'native_admission_dry_run': True,
                'expansions': self.journal['expansions'],
                'dependent_retirement_parent_readback': True}, sort_keys=True), flush=True)
        print('LONGHORN_V2_LINKED_NESTED_BUSINESS_PASS', flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=('business','clean'))
    parser.add_argument('--relay-pid', type=int, default=0)
    args = parser.parse_args()
    spec = importlib.util.spec_from_file_location('linked_install', BASE/'tests/helpers/longhorn-v2-install.py')
    install = importlib.util.module_from_spec(spec); spec.loader.exec_module(install)
    _, lab = install.fixture(); lab.require_lease(); probe = Probe(lab)
    if args.mode == 'clean': probe.cleanup()
    else:
        if args.relay_pid <= 0: parser.error('business requires a live relay PID')
        probe.business(args.relay_pid)


if __name__ == '__main__':
    try: main()
    except Exception:
        import tempfile
        import traceback
        with tempfile.NamedTemporaryFile('w', prefix='longhorn-v2-linked-failure-', suffix='.txt', dir='/var/tmp', delete=False) as diagnostic:
            traceback.print_exc(file=diagnostic)
            print('LONGHORN_V2_LINKED_FAIL private_diagnostic='+diagnostic.name, file=sys.stderr)
        sys.exit(1)
