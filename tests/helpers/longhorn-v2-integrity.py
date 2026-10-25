#!/usr/bin/env python3
"""Qualify the documented native on-demand checksum after product snapshots.

The disposable source belongs to the existing business journal. This changes
its per-volume policy only; it neither changes global policies nor corrupts
SPDK data. Completion is not evidence that prehashed snapshots were re-read.
"""
import argparse
from datetime import datetime, timezone
import importlib.util
import json
from pathlib import Path
import re
import sys


BASE = Path('/usr/local/kubeauto')
NAMESPACE = 'longhorn-system'
SETTINGS = ('snapshot-data-integrity', 'snapshot-data-integrity-immediate-check-after-snapshot-creation',
            'snapshot-data-integrity-cronjob')


def timestamp(value):
    if not isinstance(value, str) or not re.fullmatch(
            r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})', value):
        raise ValueError('checksum timestamp must be RFC3339 with an explicit timezone')
    return datetime.fromisoformat(value.replace('Z', '+00:00'))


def settings(lab):
    result = {}
    for name in SETTINGS:
        obj = lab.get('settings.longhorn.io', name, NAMESPACE)
        if not obj:
            raise ValueError('missing native snapshot integrity setting')
        result[name] = {'uid': obj['metadata']['uid'], 'value': obj['value']}
    return result


def snapshots(lab, journal):
    rows = {obj['metadata']['name']: obj for obj in lab.get('snapshots.longhorn.io', namespace=NAMESPACE)['items']
            if obj.get('spec', {}).get('volume') == journal['volume'] and obj.get('status', {}).get('userCreated')}
    expected = {row['native'] for row in journal['snapshots'].values()}
    if len(expected) < 2 or expected != rows.keys():
        raise ValueError('checksum qualification requires its product/native and CSI snapshots')
    for name, row in journal['snapshots'].items():
        uid = journal.get('native_snapshots', {}).get(row['native'], {}).get('uid')
        if not uid or rows[row['native']]['metadata']['uid'] != uid:
            raise ValueError('checksum source snapshot identity changed: ' + name)
    if any(obj['metadata'].get('deletionTimestamp') or obj.get('status', {}).get('error') or
           not obj.get('status', {}).get('readyToUse') for obj in rows.values()):
        raise ValueError('checksum qualification requires ready, error-free user snapshots')
    return rows


def verify(lab, journal):
    original_settings = settings(lab)
    before = snapshots(lab, journal)
    volume = lab.get('volumes.longhorn.io', journal['volume'], NAMESPACE)
    if (not volume or volume['metadata']['uid'] != journal['volume_uid'] or
            volume['spec'].get('dataEngine') != 'v2' or
            volume['spec'].get('numberOfReplicas') != 3 or
            volume.get('status', {}).get('state') != 'attached' or
            volume.get('status', {}).get('robustness') != 'healthy' or
            volume['spec'].get('migrationNodeID') or
            volume['spec'].get('snapshotDataIntegrity', 'ignored') not in ('ignored', 'enabled', 'fast-check')):
        raise ValueError('checksum qualification requires its original healthy three-replica V2 source')
    if not lab.replica_topology(journal, 3) or not lab.healthy_replicas(journal['volume']):
        raise ValueError('checksum qualification requires three healthy RW replicas')
    engines = [obj for obj in lab.get('engines.longhorn.io', namespace=NAMESPACE)['items']
               if obj['spec'].get('volumeName') == journal['volume']]
    if (len(engines) != 1 or engines[0]['spec'].get('dataEngine') != 'v2' or
            engines[0].get('status', {}).get('isExpanding') or
            engines[0].get('status', {}).get('rebuildStatus') or
            any(row.get(flag) or row.get('error') for field, flag in
                (('rebuildStatus', 'isRebuilding'), ('purgeStatus', 'isPurging'), ('restoreStatus', 'isRestoring'))
                for row in engines[0].get('status', {}).get(field, {}).values())):
        raise ValueError('checksum qualification cannot overlap engine maintenance')
    previous_request = volume['spec'].get('snapshotHashingRequestedAt', '')
    previous_completed = volume.get('status', {}).get('lastOnDemandSnapshotHashingCompleteAt', '')
    if previous_request != previous_completed:
        raise ValueError('checksum qualification cannot overwrite an outstanding request')
    requested = datetime.now(timezone.utc).isoformat(timespec='microseconds').replace('+00:00', 'Z')
    if previous_request and timestamp(requested) <= timestamp(previous_request):
        raise ValueError('checksum request clock did not advance')
    request_time = timestamp(requested)
    if any(timestamp(obj['metadata']['creationTimestamp']) > request_time
           for obj in before.values()):
        raise ValueError('snapshot API time exceeds the checksum requester clock')
    journal['integrity'] = {'settings': original_settings, 'requested_at': requested,
                            'original_policy': volume['spec'].get('snapshotDataIntegrity', 'ignored'),
                            'snapshots': {name: obj['metadata']['uid'] for name, obj in before.items()}}
    lab.write_private(lab.DIRECTORY/'longhorn-install-business.json', journal)
    desired = dict(volume['spec'], snapshotDataIntegrity='fast-check', snapshotHashingRequestedAt=requested)
    patch = [{'op': 'test', 'path': '/metadata/uid', 'value': journal['volume_uid']},
             {'op': 'test', 'path': '/metadata/resourceVersion', 'value': volume['metadata']['resourceVersion']},
             {'op': 'test', 'path': '/spec', 'value': volume['spec']},
             {'op': 'replace', 'path': '/spec', 'value': desired}]
    lab.kubectl('-n', NAMESPACE, 'patch', 'volumes.longhorn.io', journal['volume'],
                '--type=json', '--patch-file=/dev/stdin', stdin=json.dumps(patch))

    def completed():
        current = lab.get('volumes.longhorn.io', journal['volume'], NAMESPACE)
        if (not current or current['metadata']['uid'] != journal['volume_uid'] or
                current['spec'].get('snapshotHashingRequestedAt') != requested or
                current['spec'].get('snapshotDataIntegrity') != 'fast-check'):
            raise ValueError('checksum source or request changed during observation')
        rows = snapshots(lab, journal)
        if {name: obj['metadata']['uid'] for name, obj in rows.items()} != journal['integrity']['snapshots']:
            raise ValueError('checksum snapshot set changed during observation')
        if current.get('status', {}).get('lastOnDemandSnapshotHashingCompleteAt') != requested:
            return None
        if not all(obj.get('status', {}).get('checksum') and obj['status'].get('checksumCalculatedAt') for obj in rows.values()):
            raise ValueError('checksum completion lacks snapshot checksums or calculation timestamps')
        if not lab.replica_topology(journal, 3) or not lab.healthy_replicas(journal['volume']):
            return None
        for obj in rows.values():
            timestamp(obj['status']['checksumCalculatedAt'])
        return rows
    rows = lab.wait('v2-on-demand-snapshot-checksum', 900, completed)
    if settings(lab) != original_settings:
        raise ValueError('global snapshot integrity settings changed')
    current = lab.get('volumes.longhorn.io', journal['volume'], NAMESPACE)
    if (not current or current['metadata']['uid'] != journal['volume_uid'] or
            current['spec'].get('snapshotHashingRequestedAt') != requested or
            current['spec'].get('snapshotDataIntegrity') != 'fast-check' or
            current.get('status', {}).get('lastOnDemandSnapshotHashingCompleteAt') != requested):
        raise ValueError('checksum source changed before restoring its per-volume policy')
    original_policy = journal['integrity']['original_policy']
    patch = [{'op': 'test', 'path': '/metadata/uid', 'value': journal['volume_uid']},
             {'op': 'test', 'path': '/metadata/resourceVersion', 'value': current['metadata']['resourceVersion']},
             {'op': 'test', 'path': '/spec', 'value': current['spec']},
             {'op': 'replace', 'path': '/spec', 'value': dict(current['spec'], snapshotDataIntegrity=original_policy)}]
    lab.kubectl('-n', NAMESPACE, 'patch', 'volumes.longhorn.io', journal['volume'],
                '--type=json', '--patch-file=/dev/stdin', stdin=json.dumps(patch))
    restored = lab.get('volumes.longhorn.io', journal['volume'], NAMESPACE)
    if (not restored or restored['metadata']['uid'] != journal['volume_uid'] or
            restored['spec'].get('snapshotDataIntegrity') != original_policy or
            restored['spec'].get('snapshotHashingRequestedAt') != requested or settings(lab) != original_settings):
        raise ValueError('checksum original per-volume or global policy was not preserved')
    result = {'volume_uid': journal['volume_uid'], 'policy': 'fast-check', 'requested_at': requested,
              'completed_at': requested, 'global_settings_unchanged': True, 'whole_data_rehash_proven': False,
              'original_policy': original_policy, 'original_policy_restored': True,
              'snapshots': {name: {'uid': obj['metadata']['uid'], 'checksum': obj['status']['checksum'],
                                  'calculated_at': obj['status']['checksumCalculatedAt'],
                                  'previously_hashed': bool(before[name].get('status', {}).get('checksum'))}
                            for name, obj in rows.items()}}
    print('LONGHORN_V2_INTEGRITY_RESULT ' + json.dumps(result, sort_keys=True), flush=True)


def verify_log(log):
    rows = re.findall(r'^LONGHORN_V2_INTEGRITY_RESULT (.+)$', log, re.M)
    if len(rows) != 1:
        raise ValueError('checksum qualification requires exactly one result')
    result = json.loads(rows[0])
    if (not result['volume_uid'] or result['policy'] != 'fast-check' or
            result['requested_at'] != result['completed_at'] or
            result['global_settings_unchanged'] is not True or result['whole_data_rehash_proven'] is not False or
            result['original_policy_restored'] is not True or
            result['original_policy'] not in ('ignored', 'enabled', 'fast-check') or
            len(result['snapshots']) < 2):
        raise ValueError('checksum result does not prove its qualified scope')
    timestamp(result['requested_at'])
    for row in result['snapshots'].values():
        if not row['uid'] or not row['checksum'] or type(row['previously_hashed']) is not bool:
            raise ValueError('checksum result lacks snapshot identity or checksum')
        timestamp(row['calculated_at'])
    boundaries = re.findall(r'^LONGHORN_SNAPSHOT_BOUNDARY_VERIFIED type=(\S+) snapshot_sha256=([a-f0-9]{64}) source_sha256=([a-f0-9]{64})$', log, re.M)
    remounts = re.findall(r'^LONGHORN_FSYNC_REMOUNT_VERIFIED bytes=(\d+) sha256=([a-f0-9]{64}) replica_nodes=(\S+)$', log, re.M)
    if (len(boundaries) != 2 or {row[0] for row in boundaries} != {'native', 'csi'} or
            len({row[1] for row in boundaries}) != 1 or len({row[2] for row in boundaries}) != 1 or
            boundaries[0][1] == boundaries[0][2] or len(remounts) != 1 or int(remounts[0][0]) <= 0 or
            remounts[0][1] != boundaries[0][2] or len(set(remounts[0][2].split(','))) != 3):
        raise ValueError('checksum result lacks independent restores and original cross-node data')
    for marker in ('LONGHORN_V2_EXPECTED_PRODUCT_REFUSAL_VERIFIED', 'LONGHORN_BUSINESS_FIXTURES_CLEAN_PASS',
                   'LONGHORN_SNAPSHOT_BUSINESS_PASS'):
        if log.splitlines().count(marker) != 1:
            raise ValueError('checksum qualification lacks business/cleanup marker: ' + marker)
    return result


def business(lab, relay_pid):
    original = lab.verify_snapshot
    def with_checksum(journal, create, pid, payload, digest):
        result = original(journal, create, pid, payload, digest)
        verify(lab, journal)
        return result
    lab.verify_snapshot = with_checksum
    try:
        lab.business('snapshot', relay_pid)
    finally:
        lab.verify_snapshot = original


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=('business', 'verify'))
    parser.add_argument('--relay-pid', type=int, default=0)
    parser.add_argument('--log', type=Path)
    args = parser.parse_args()
    if args.mode == 'verify':
        if not args.log:
            parser.error('verify requires --log')
        verify_log(args.log.read_text())
        return
    if args.relay_pid <= 0:
        parser.error('business requires a positive --relay-pid')
    spec = importlib.util.spec_from_file_location('v2_integrity_install', BASE/'tests/helpers/longhorn-v2-install.py')
    install = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(install)
    _, lab = install.fixture()
    lab.require_lease()
    business(lab, args.relay_pid)


if __name__ == '__main__':
    try:
        main()
    except Exception:
        import tempfile
        import traceback
        with tempfile.NamedTemporaryFile('w', prefix='longhorn-v2-integrity-failure-',
                suffix='.txt', dir='/var/tmp', delete=False) as diagnostic:
            traceback.print_exc(file=diagnostic)
            print('LONGHORN_V2_INTEGRITY_FAIL private_diagnostic=' + diagnostic.name, file=sys.stderr)
        sys.exit(1)
