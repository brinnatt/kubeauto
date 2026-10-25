#!/usr/bin/env python3
"""V2 snapshot-aware TRIM measured from exclusive SPDK lvstore allocation."""
import importlib.util
import json
from pathlib import Path
import secrets
import time

spec = importlib.util.spec_from_file_location('longhorn_trim_native', Path(__file__).with_name('longhorn-trim.py'))
native = importlib.util.module_from_spec(spec)
spec.loader.exec_module(native)
SIZE = native.SIZE


class Probe(native.Probe):
    def engine(self):
        volumes = self.lab.get('volumes.longhorn.io', namespace='longhorn-system')['items']
        if len(volumes) != 1:
            raise ValueError('V2 trim requires exclusive native allocation measurement')
        volume = volumes[0]
        if (volume['metadata']['name'] != self.journal['volume'] or
                volume['metadata']['uid'] != self.journal['volume_uid'] or
                volume['spec'].get('dataEngine') != 'v2' or volume['spec'].get('encrypted') or
                volume['spec'].get('frontend') != 'blockdev' or volume['spec'].get('accessMode') != 'rwo' or
                self.lab.replica_topology(self.journal, 3) != self.topology):
            raise ValueError('V2 trim requires original healthy unencrypted RWO block-device replicas')
        if not self.lab.healthy_replicas(self.journal['volume']):
            raise ValueError('V2 trim replicas are not currently healthy on their qualified disks')
        engines = [obj for obj in self.lab.get('engines.longhorn.io', namespace='longhorn-system')['items']
                   if obj['spec'].get('volumeName') == self.journal['volume'] and
                   obj['spec'].get('nodeID') == volume['status'].get('currentNodeID') and
                   obj.get('status', {}).get('currentState') == 'running']
        if len(engines) != 1 or engines[0]['spec'].get('dataEngine') != 'v2':
            raise ValueError('V2 trim requires one original running V2 engine')
        engine = engines[0]
        # The native snapshot list includes the writable head. Manager likewise
        # excludes this reserved entry when reconciling actual Snapshot CRs.
        unowned_snapshots = sorted(name for name, info in engine['status'].get('snapshots', {}).items()
                                   if info.get('usercreated') and name not in ('volume-head', self.snapshot))
        if (engine['status'].get('isExpanding') or
                any(row.get('isRebuilding') or row.get('error')
                    for row in engine['status'].get('rebuildStatus', {}).values()) or
                unowned_snapshots):
            print('LONGHORN_V2_TRIM_REJECT_STATE ' + json.dumps({
                'engine_uid': engine['metadata']['uid'],
                'is_expanding': bool(engine['status'].get('isExpanding')),
                'replicas': {name: {'rebuilding': bool(row.get('isRebuilding')),
                                    'error_present': bool(row.get('error'))}
                             for name, row in engine['status'].get('rebuildStatus', {}).items()},
                'unowned_snapshots': unowned_snapshots,
            }, sort_keys=True), flush=True)
            raise ValueError('V2 trim refuses expansion, rebuilding or an unowned snapshot')
        return engine

    def sample(self, stage):
        self.engine()
        receipt = json.loads((self.lab.DIRECTORY/'yml/longhorn/block-disks.json').read_text())
        replicas = self.lab.get('replicas.longhorn.io', namespace='longhorn-system')['items']
        if {r['metadata']['name'] for r in replicas} != set(self.topology):
            raise ValueError('V2 trim rejects another replica allocation consumer')
        result = {}
        for replica in replicas:
            name = replica['metadata']['name']; original = self.topology[name]
            node = replica['spec'].get('nodeID')
            rows = [row for row in receipt['nodes'].values() if row['name'] == node]
            if (len(rows) != 1 or replica['metadata']['uid'] != original['uid'] or node != original['node'] or
                    replica['spec'].get('dataEngine') != 'v2'):
                raise ValueError('V2 trim original replica identity changed')
            row = rows[0]
            disk = self.lab.get('nodes.longhorn.io', node, 'longhorn-system')
            if disk['metadata']['uid'] != row['longhorn_node_uid']:
                raise ValueError('V2 trim original native node changed')
            candidates = [(key, value) for key, value in row['disks'].items()
                          if value['disk_uuid'] == replica['spec'].get('diskID')]
            if len(candidates) != 1:
                raise ValueError('V2 trim replica moved off its original disk')
            key, expected = candidates[0]
            config = disk['spec']['disks'][key]; status = disk['status']['diskStatus'][key]
            conditions = {condition['type']: condition['status'] for condition in status['conditions']}
            if (config.get('diskType') != 'block' or config.get('diskDriver') != 'aio' or
                    status.get('diskUUID') != expected['disk_uuid'] or
                    any(conditions.get(condition) != 'True' for condition in ('Ready', 'Schedulable'))):
                raise ValueError('V2 trim native disk identity or health changed')
            total = status.get('storageMaximum'); available = status.get('storageAvailable')
            if type(total) is not int or type(available) is not int or not 0 <= available <= total or total == 0:
                raise ValueError('V2 trim disk allocation sample is invalid')
            result[name] = total - available
        print('LONGHORN_V2_TRIM_ALLOCATION stage=' + stage + ' bytes=' + json.dumps(result, sort_keys=True), flush=True)
        return result

    def settled(self, stage):
        # Native disk monitor period is 30 seconds. Observe a stable value for
        # two full periods instead of treating one stale API sample as final.
        previous = None
        changed = time.monotonic()
        def stable():
            nonlocal previous, changed
            current = self.sample(stage)
            if current != previous:
                previous, changed = current, time.monotonic()
            return current if time.monotonic() - changed >= 65 else None
        return self.lab.wait('v2-trim-settled-' + stage, 300, stable)

    def prepare(self, relay_pid):
        self.action('trimFilesystem')
        baseline = self.settled('baseline')
        identity = self.file('create')
        self.snapshot = 'trim-' + secrets.token_hex(12)
        request = {'kind': 'snapshot', 'name': self.snapshot, 'volume': self.journal['volume']}
        self.journal.setdefault('native_snapshots', {})[self.snapshot] = {'request': request}
        self.lab.write_private(self.lab.DIRECTORY/'longhorn-install-business.json', self.journal)
        self.lab.run_requests(relay_pid, [request])
        self.lab.bind_snapshots(self.journal)
        def allocated():
            current = self.sample('snapshot-retained')
            return current if all(current[name] - value >= SIZE//2 for name, value in baseline.items()) else None
        self.lab.wait('v2-trim-filler-allocated', 180, allocated)
        removed = self.file('remove', identity)
        if removed['free_bytes'] - identity['free_bytes'] < SIZE//2:
            raise ValueError('V2 trim deletion did not free guest filesystem blocks')
        self.action('trimFilesystem')
        retained = self.settled('deleted-file-valid-snapshot')
        if any(retained[name] - value < SIZE//2 for name, value in baseline.items()):
            raise ValueError('V2 trim lost allocation retained by a valid snapshot')
        snapshot = self.lab.get('snapshots.longhorn.io', self.snapshot, 'longhorn-system')
        expected = self.journal['native_snapshots'][self.snapshot]['uid']
        if (not snapshot or snapshot['metadata']['uid'] != expected or
                snapshot['metadata'].get('deletionTimestamp') or not snapshot['status'].get('readyToUse')):
            raise ValueError('V2 trim valid snapshot identity changed')
        self.lab.remove(snapshot)
        def gone():
            current = self.lab.get('snapshots.longhorn.io', self.snapshot, 'longhorn-system')
            if current and current['metadata']['uid'] != expected:
                raise ValueError('V2 trim snapshot replaced during deletion')
            return current is None and self.snapshot not in self.engine()['status'].get('snapshots', {})
        self.lab.wait('v2-trim-original-snapshot-deleted', 600, gone)
        return {'baseline': baseline, 'retained': retained, 'snapshot': {'name': self.snapshot, 'uid': expected},
                'guest_freed_bytes': removed['free_bytes'] - identity['free_bytes'],
                'filler_sha256': identity['sha256'], 'replicas': self.topology}

    def finish(self, proof, payload, digest):
        snapshot = proof['snapshot']; self.snapshot = snapshot['name']
        original = self.journal.get('native_snapshots', {}).get(self.snapshot, {})
        if (not snapshot['uid'] or original.get('uid') != snapshot['uid'] or
                original.get('request') != {'kind': 'snapshot', 'name': self.snapshot, 'volume': self.journal['volume']} or
                self.lab.get('snapshots.longhorn.io', self.snapshot, 'longhorn-system')):
            raise ValueError('V2 trim original snapshot deletion proof changed')
        self.action('trimFilesystem')
        def reclaimed():
            current = self.sample('after-delete-remount-trim')
            return current if all(proof['retained'][name] - value >= SIZE//2 for name, value in current.items()) else None
        after = self.lab.wait('v2-trim-actual-lvstore-reclaim', 600, reclaimed)
        actual = self.lab.kubectl('-n', self.journal['namespace'], 'exec', 'remount-reader', '--', 'python3', '-c',
            self.lab.payload_program(payload, read_only=True)).stdout.strip()
        if actual != digest:
            raise ValueError('V2 trim changed original confirmed bytes')
        proof.update(after=after, filler_bytes=SIZE, confirmed_sha256=digest,
                     measurement='exclusive SPDK lvstore used bytes via native disk status; not hypervisor physical allocation')
        print('LONGHORN_V2_TRIM_RESULT ' + json.dumps(proof, sort_keys=True), flush=True)


def prepare(lab, journal, relay_pid):
    topology = lab.wait('v2-trim-original-replicas', 600, lambda: lab.replica_topology(journal, 3))
    return Probe(lab, journal, topology).prepare(relay_pid)


def finish(lab, journal, proof, payload, digest):
    Probe(lab, journal, proof['replicas']).finish(proof, payload, digest)
