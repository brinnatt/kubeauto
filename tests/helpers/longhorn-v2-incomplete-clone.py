#!/usr/bin/env python3
"""LONGHORN-49: real CSI clone held by reversible product disk scheduling.

The fixed owning shell supervises relay, lease, baseline,
durable exit, cleanup and this helper's exact business/clean markers.
"""
import argparse
import copy
import importlib.util
import json
from pathlib import Path
import secrets

BASE = Path('/usr/local/kubeauto')
NS = 'longhorn-system'


def load(name):
    spec = importlib.util.spec_from_file_location(name.replace('-', '_'), BASE/'tests/helpers'/name)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def disk_state(lab, declared):
    """Bind every declared source disk to native UID, spec and stable UUID."""
    result = {}
    for host, disks in declared.items():
        if len(disks) != 1:
            raise ValueError('incomplete clone only owns one admitted disk per source host')
        name = 'longhorn-node-'+host.rsplit('.', 1)[1]
        node = lab.get('nodes.longhorn.io', name, NS)
        if not node or set(node['spec'].get('disks', {})) != {disks[0]['name']}:
            raise ValueError('incomplete clone disk set or native node changed')
        disk = disks[0]['name']; status = node['status'].get('diskStatus', {}).get(disk, {})
        if not status.get('diskUUID'):
            raise ValueError('incomplete clone requires original native disk UUID')
        result[name] = {'uid': node['metadata']['uid'], 'disk': disk,
                        'disk_uuid': status['diskUUID'], 'spec': copy.deepcopy(node['spec']['disks'][disk])}
    if len(result) != 3:
        raise ValueError('incomplete clone requires exactly three independent source nodes')
    return result


def expected_disks(before, held):
    result = copy.deepcopy(before)
    for row in result.values():
        row['spec'].update(allowScheduling=not held, evictionRequested=False)
    return result


def nested_request(clone, name):
    if clone['spec'].get('dataEngine') != 'v2' or clone['spec'].get('cloneMode') != 'linked-clone':
        raise ValueError('incomplete source must be a real V2 linked clone')
    spec = copy.deepcopy(clone['spec'])
    spec['dataSource'] = 'vol://'+clone['metadata']['name']
    # Controller-owned attachment intention is not part of a new CSI request.
    spec['nodeID'] = ''; spec['migrationNodeID'] = ''
    return {'apiVersion': clone['apiVersion'], 'kind': 'Volume',
            'metadata': {'name': name, 'namespace': NS}, 'spec': spec}


def dry_run(lab, clone, request, *, completed):
    """Exact admission result plus unchanged real source UID/spec; no mutation."""
    name = clone['metadata']['name']; uid = clone['metadata']['uid']
    state = clone.get('status', {}).get('cloneStatus', {}).get('state', '')
    if (state == 'completed') != completed or state == 'failed':
        raise ValueError('incomplete clone assertion has the wrong real controller state')
    if lab.get('volumes.longhorn.io', request['metadata']['name'], NS):
        raise ValueError('nested dry-run name already exists')
    result = lab.kubectl('create', '-f', '-', '--dry-run=server', '-o', 'json',
                         stdin=json.dumps(request), check=False)
    current = lab.get('volumes.longhorn.io', name, NS)
    if (not current or current['metadata']['uid'] != uid or current['spec'] != clone['spec'] or
            current['metadata'].get('deletionTimestamp') or
            lab.get('volumes.longhorn.io', request['metadata']['name'], NS)):
        raise ValueError('nested dry-run changed or replaced its real fixture')
    after = current.get('status', {}).get('cloneStatus', {}).get('state', '')
    if (after == 'completed') != completed or after == 'failed':
        raise ValueError('source escaped its controlled cloning boundary')
    if completed:
        accepted = json.loads(result.stdout) if result.returncode == 0 else None
        if not accepted or accepted.get('spec', {}).get('dataSource') != request['spec']['dataSource']:
            raise ValueError('completed-clone positive control was not accepted')
    else:
        reason = 'cannot use volume '+name+' as linked-clone source: its own cloning is not yet completed (state: '
        if result.returncode == 0 or reason not in result.stdout+result.stderr:
            raise ValueError('incomplete nesting returned the wrong admission result')
    return {'clone_uid': uid, 'before_state': state, 'after_state': after,
            'nested_name': request['metadata']['name'], 'server_dry_run': True,
            'completed_positive_control': completed, 'uid_and_spec_unchanged': True}


def probe_type(linked, lifecycle):
    class Probe(linked.Probe):
        def __init__(self, lab, relay_pid):
            super().__init__(lab)
            self.record = lab.DIRECTORY/'longhorn-v2-incomplete-clone.json'
            self.relay_pid = relay_pid

        def config(self, **changes):
            lifecycle.Probe.config(self, **changes)

        def restore_disks(self):
            j = self.journal
            if not j.get('disk_hold_intent'):
                return
            # Intent is durable before product reconciliation; recovery may see
            # an original or held disk on each host after an interrupted apply.
            actual = disk_state(self.lab, j['original_disks'])
            held = expected_disks(j['native_disks'], True)
            if set(actual) != set(j['native_disks']) or any(
                    actual[name] not in (row, held[name]) for name, row in j['native_disks'].items()):
                raise ValueError('disk recovery found a replaced identity or foreign setting')
            restored = copy.deepcopy(j['original_disks'])
            for host, disks in restored.items():
                original = j['native_disks']['longhorn-node-'+host.rsplit('.', 1)[1]]['spec']
                # Omitted product flags preserve native state, so explicitly
                # undo our hold while retaining the original config bytes.
                disks[0].update(allow_scheduling=original['allowScheduling'],
                                eviction_requested=original['evictionRequested'])
            self.config(longhorn_block_disks=restored)
            if disk_state(self.lab, j['original_disks']) != j['native_disks']:
                raise ValueError('incomplete clone disk configuration was not exactly restored')
            j['disk_hold_intent'] = False
            self.save()

        def cleanup(self):
            if self.record.is_symlink():
                raise ValueError('symlink incomplete clone journal')
            if not self.record.exists():
                return
            self.lab.restore_request_input()
            self.journal = json.loads(self.record.read_text())
            if self.journal['run_id'] != self.lab.owner()['run_id']:
                raise ValueError('foreign incomplete clone journal')
            self.restore_disks()
            super().cleanup()
            print('LONGHORN_V2_INCOMPLETE_CLONE_FIXTURES_CLEAN_PASS', flush=True)

        def business(self):
            lab = self.lab
            if self.record.exists() or self.record.is_symlink():
                raise ValueError('incomplete clone requires owned scoped recovery')
            state = lab.owner(); configuration = lab.yaml.safe_load((lab.DIRECTORY/'config.yml').read_text())
            classes = copy.deepcopy(configuration['longhorn_storage_classes'])
            sc = copy.deepcopy(next(row for row in classes if row['name'] == 'longhorn'))
            sc.update(name='longhorn-incomplete-'+secrets.token_hex(6), clone_mode='linked-clone',
                      default=False, reclaim_policy='Retain')
            if lab.get('storageclass', sc['name']):
                raise ValueError('incomplete clone class already exists')
            disks = copy.deepcopy(configuration['longhorn_block_disks'])
            native = disk_state(lab, disks)
            if native != expected_disks(native, False):
                raise ValueError('incomplete clone requires scheduling enabled without eviction')
            self.journal = {'run_id': state['run_id'], 'namespace': 'longhorn-linked-'+secrets.token_hex(8),
                'objects': {}, 'claims': {}, 'defaults': [], 'class_name': sc['name'], 'class_config': sc,
                'original_disks': disks, 'native_disks': native, 'disk_hold_intent': False}
            self.save()
            with lab.scoped_cleanup(self.cleanup):
                lab.run_requests(self.relay_pid, [], storage_classes=classes+[sc])
                self.journal['class_uid'] = lab.get('storageclass', sc['name'])['metadata']['uid']; self.save()
                ns = json.loads(lab.kubectl('create', 'namespace', self.journal['namespace'], '-o', 'json').stdout)
                self.journal['namespace_uid'] = ns['metadata']['uid']; self.save()
                self.journal['defaults'] = lab.wait('incomplete-namespace-defaults', 60,
                    lambda: lab.namespace_defaults(self.journal['namespace'])); self.save()
                self.claim('source'); self.io('source', state['run_id'])
                source_row = self.journal['claims']['source']
                source_native = self.current('volumes.longhorn.io', source_row['volume'], source_row['volume_uid'], NS)
                original = lab.replica_topology(source_row, 3)
                def source_unchanged():
                    current = self.current('volumes.longhorn.io', source_row['volume'], source_row['volume_uid'], NS)
                    if (not current or current['spec'] != source_native['spec'] or
                            lab.replica_topology(source_row, 3) != original or not lab.healthy_replicas(source_row['volume'])):
                        raise ValueError('scheduling fixture changed its source volume or healthy replica identities')
                self.journal['disk_hold_intent'] = True; self.save()
                held = copy.deepcopy(disks)
                for rows in held.values():
                    rows[0].update(allow_scheduling=False, eviction_requested=False)
                self.config(longhorn_block_disks=held)
                if disk_state(lab, disks) != expected_disks(native, True):
                    raise ValueError('product scheduling hold did not converge')
                obj = lab.clone_claim(self.journal['namespace'], 'clone', '2Gi')
                obj['spec'].update(storageClassName=sc['name'], dataSource={'kind':'PersistentVolumeClaim','name':'source'})
                self.create(obj)
                def bound():
                    row = self.bind('clone'); claim = lab.get('pvc', 'clone', self.journal['namespace'])
                    return row if row and claim and claim.get('status', {}).get('phase') == 'Bound' else None
                row = lab.wait('incomplete-clone-pv-bound', 900, bound)
                def blocked():
                    clone = self.current('volumes.longhorn.io', row['volume'], row['volume_uid'], NS)
                    state = clone.get('status', {}).get('cloneStatus', {}).get('state', '')
                    if state in ('failed', 'completed'):
                        raise ValueError('clone escaped its real scheduling hold')
                    replicas = [r for r in lab.get('replicas.longhorn.io', namespace=NS)['items']
                                if r['spec'].get('volumeName') == row['volume']]
                    if any(r['spec'].get('nodeID') or r['spec'].get('diskID') for r in replicas):
                        raise ValueError('clone escaped the product scheduling hold')
                    refused = any(c.get('type') == 'Scheduled' and c.get('status') == 'False' and
                                  c.get('reason') == 'ReplicaSchedulingFailure' and
                                  c.get('message') == 'precheck new replica failed: disks are unavailable'
                                  for c in clone.get('status', {}).get('conditions', []))
                    # v1.12.1 precheck returns before createReplica, so no CRs
                    # exist while all declared disks refuse new scheduling.
                    return clone if not replicas and refused else None
                clone = lab.wait('incomplete-clone-native-scheduling-blocked', 180, blocked)
                source_unchanged()
                request = nested_request(clone, 'nested-dryrun-'+secrets.token_hex(8))
                negative = dry_run(lab, clone, request, completed=False)
                source_unchanged()
                self.io('source', state['run_id'], read=True)
                self.restore_disks()
                def ready():
                    current = self.current('volumes.longhorn.io', row['volume'], row['volume_uid'], NS)
                    if current.get('status', {}).get('cloneStatus', {}).get('state') == 'failed':
                        raise ValueError('restored incomplete clone failed')
                    return current if current.get('status', {}).get('cloneStatus', {}).get('state') == 'completed' else None
                clone = lab.wait('incomplete-clone-restored-completed', 900, ready)
                self.client('clone', 1)
                lab.wait('incomplete-clone-restored-healthy', 900,
                    lambda: lab.replica_topology(row, 3) if lab.healthy_replicas(row['volume']) else None)
                self.io('clone', state['run_id'], read=True); self.io('source', state['run_id'], read=True)
                clone = self.current('volumes.longhorn.io', row['volume'], row['volume_uid'], NS)
                positive = dry_run(lab, clone, request, completed=True)
                source_unchanged()
                print('LONGHORN_V2_INCOMPLETE_CLONE_RESULT '+json.dumps({'negative':negative,
                    'positive':positive, 'disk_identity_and_settings_restored':True,
                    'source_replica_identities':original, 'real_csi_pv_uid':row['pv_uid']},sort_keys=True),flush=True)
            print('LONGHORN_V2_INCOMPLETE_CLONE_BUSINESS_PASS', flush=True)
    return Probe


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=('business', 'clean'))
    parser.add_argument('--relay-pid', type=int, required=True)
    args = parser.parse_args()
    if args.relay_pid <= 0:
        parser.error('live supervised relay required')
    install = load('longhorn-v2-install.py'); _, lab = install.fixture(); lab.require_lease()
    probe = probe_type(load('longhorn-v2-linked-clones.py'), load('longhorn-v2-linked-lifecycle.py'))(lab,args.relay_pid)
    if args.mode == 'clean': probe.cleanup()
    else: probe.business()


if __name__ == '__main__':
    main()
