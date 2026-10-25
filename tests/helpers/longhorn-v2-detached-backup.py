#!/usr/bin/env python3
"""Detached scheduled backup and full interval on the owned V2 baseline.

Schedules use config+kubecli. The documented native detached-job Setting is
temporarily changed with UID/resourceVersion guards and restored before exit.
An incremental request may create a full backend backup; mode evidence binds
the native request to the owning Pod's executionCount, not backend inference.
"""
import argparse
import errno
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import socket
import sys
from types import SimpleNamespace


BASE = Path('/usr/local/kubeauto')
NAMESPACE = 'longhorn-system'
SETTING = 'allow-recurring-job-while-volume-detached'
RECORD = 'longhorn-v2-detached-setting.json'


def fixture():
    spec = importlib.util.spec_from_file_location('v2_detached_recurring',
        BASE/'tests/helpers/longhorn-v2-recurring.py')
    recurring = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(recurring)
    backup, install, lab, store = recurring.fixture()
    original = lab.backup_store
    lab.backup_store = lambda: protected_store(lab, original)
    return recurring, backup, install, lab, store


def protected_store(lab, factory):
    store = factory()
    original = store.cleanup
    def cleanup(*args, **kwargs):
        path = lab.DIRECTORY/RECORD
        if path.exists() or path.is_symlink():
            raise ValueError('restore the original detached-job Setting before removing its backup store')
        return original(*args, **kwargs)
    store.cleanup = cleanup
    return store


class Setting:
    def __init__(self, lab):
        self.lab = lab
        self.path = lab.DIRECTORY/RECORD

    def current(self):
        obj = self.lab.get('settings.longhorn.io', SETTING, NAMESPACE)
        if not obj or obj.get('value') not in ('true', 'false'):
            raise ValueError('detached-job native Setting is unavailable')
        return obj

    def change(self, current, value):
        patch = [{'op': 'test', 'path': '/metadata/uid', 'value': current['metadata']['uid']},
                 {'op': 'test', 'path': '/metadata/resourceVersion', 'value': current['metadata']['resourceVersion']},
                 {'op': 'test', 'path': '/value', 'value': current['value']},
                 {'op': 'replace', 'path': '/value', 'value': value}]
        self.lab.kubectl('-n', NAMESPACE, 'patch', 'settings.longhorn.io', SETTING,
                         '--type=json', '--patch-file=/dev/stdin', stdin=json.dumps(patch))
        observed = self.current()
        if observed['metadata']['uid'] != current['metadata']['uid'] or observed['value'] != value:
            raise ValueError('detached-job Setting update was not observed')

    def enable(self):
        if self.path.exists() or self.path.is_symlink():
            raise ValueError('detached-job Setting journal requires recovery')
        current = self.current()
        if current['value'] != 'false':
            raise ValueError('detached-job qualification requires original disabled Setting')
        state = self.lab.owner()
        self.lab.write_private(self.path, {'run_id': state['run_id'], 'cluster_uid': state['cluster_uid'],
                                          'uid': current['metadata']['uid'], 'original': current['value']})
        self.change(current, 'true')

    def restore(self):
        if self.path.is_symlink():
            raise ValueError('detached-job Setting journal is a symlink')
        if not self.path.exists():
            return
        row = json.loads(self.path.read_text()); state = self.lab.owner()
        current = self.current()
        if (row['run_id'] != state['run_id'] or row['cluster_uid'] != state['cluster_uid'] or
                row['uid'] != current['metadata']['uid'] or row['original'] != 'false'):
            raise ValueError('detached-job Setting recovery identity changed')
        if current['value'] != row['original']:
            self.change(current, row['original'])
        self.path.unlink()
        print('LONGHORN_V2_DETACHED_SETTING_RESTORED', flush=True)


def pod_origin(log, *, schedule, volume):
    """Read only the exact pinned manager's Starting volume job record."""
    rows = []
    for line in log.splitlines():
        if 'msg="Starting volume job"' not in line:
            continue
        fields = {}
        for key in ('job', 'volumeName', 'snapshotName', 'executionCount'):
            matches = re.findall(r'(?:^|\s)' + key + r'=([^\s]+)', line)
            if len(matches) != 1:
                raise ValueError('native recurring log omits an unambiguous field')
            fields[key] = matches[0]
        if fields['job'] != schedule or fields['volumeName'] != volume:
            raise ValueError('native recurring log belongs to a different schedule or volume')
        if not re.fullmatch(r'[1-9][0-9]*', fields['executionCount']):
            raise ValueError('native recurring executionCount is invalid')
        rows.append({'execution_count': int(fields['executionCount']), 'snapshot': fields['snapshotName']})
    if len(rows) != 1:
        raise ValueError('expected one native volume-job start per successful Pod')
    return rows[0]


def bind_modes(lab, observation, origins):
    row = observation['row']
    owned_jobs = {job['metadata']['uid']: job for job in observation['jobs']}
    for pod in observation['pods']:
        if pod.get('status', {}).get('phase') != 'Succeeded':
            continue
        name = pod['metadata']['name']; uid = pod['metadata']['uid']
        if uid in origins:
            continue
        refs = [ref for ref in pod['metadata'].get('ownerReferences', []) if ref.get('controller')]
        if len(refs) != 1 or refs[0]['kind'] != 'Job' or refs[0]['uid'] not in owned_jobs:
            raise ValueError('detached backup Pod lacks its original Job owner')
        job = owned_jobs[refs[0]['uid']]
        if job.get('status', {}).get('succeeded') != 1:
            continue
        log = lab.kubectl('-n', NAMESPACE, 'logs', name, '--request-timeout=10s').stdout
        origin = pod_origin(log, schedule=row['name'], volume=row['volume'])
        origin.update(pod_uid=uid, job_uid=job['metadata']['uid'])
        if any(previous['execution_count'] == origin['execution_count'] for previous in origins.values()):
            raise ValueError('two successful Pods claim the same recurring executionCount')
        origins[uid] = origin
    modes = {}
    for backup in observation['backups']:
        status = backup.get('status', {})
        if status.get('state') != 'Completed' or status.get('progress') != 100 or status.get('error'):
            continue
        matches = [origin for origin in origins.values() if origin['snapshot'] == backup['spec']['snapshotName']]
        if len(matches) != 1:
            continue
        origin = matches[0]
        expected = 'full' if origin['execution_count'] % 2 == 0 else 'incremental'
        if backup['spec'].get('backupMode') != expected:
            raise ValueError('periodic full request differs from its native executionCount')
        modes[backup['metadata']['name']] = dict(origin, uid=backup['metadata']['uid'], mode=expected)
    return modes


def stop(lab, relay_pid):
    # Drain this exact schedule before restoring the global policy. Any failed
    # ownership check retains the Setting journal and external recovery store.
    lab.load_helper('longhorn-recurring.py').Schedule(lab).cleanup(relay_pid)
    Setting(lab).restore()


def recovery_boundary(lab):
    """Admit only the archived post-namespace-deletion host-reboot boundary."""
    evidence = json.loads((BASE/'tests/evidence/longhorn-v2-detached-backup-network-first-failure-20261020.json').read_text())
    binding = evidence['recovery_binding']
    if (evidence['classification'] != 'environment/host-reboot' or
            Path('/proc/sys/kernel/random/boot_id').read_text().strip() != binding['boot_id'] or
            Path('/proc', str(binding['old_pid'])).exists()):
        raise ValueError('detached recovery is not the archived lost-task reboot boundary')
    state = lab.owner()
    if any(state.get(key) != value for key, value in binding['owner'].items()):
        raise ValueError('detached recovery owner or shared relay identity changed')
    lab.core_identity()
    record = lab.DIRECTORY/'longhorn-install-business.json'
    if record.is_symlink():
        raise ValueError('symlink detached business journal')
    journal = json.loads(record.read_text())
    if (hashlib.sha256(json.dumps(journal, sort_keys=True).encode()).hexdigest() != binding['business_sha256'] or
            not journal['namespace'].startswith('longhorn-business-') or
            lab.get('namespace', journal['namespace']) is not None):
        raise ValueError('detached business journal or absent namespace boundary changed')
    config = lab.yaml.safe_load((lab.DIRECTORY/'config.yml').read_text())
    if config['longhorn_requests'] or config['longhorn_recurring_jobs'] or Setting(lab).current()['value'] != 'false':
        raise ValueError('detached recovery retains requests, schedules or changed Setting')
    for name in (RECORD, 'longhorn-request-input.json', 'longhorn-recurring-runtime.json',
                 'longhorn-recurring-credentials.json', 'longhorn-recurring-qualification.json'):
        path = lab.DIRECTORY/name
        if path.exists() or path.is_symlink():
            raise ValueError('detached recovery retains another recovery journal')
    # Require an empty storage boundary, including controllers and consumers;
    # absence of the namespace alone does not prove Retain data was retired.
    for kind in ('volumes.longhorn.io', 'replicas.longhorn.io', 'engines.longhorn.io',
                 'snapshots.longhorn.io', 'backups.longhorn.io', 'backupvolumes.longhorn.io',
                 'backupbackingimages.longhorn.io', 'systembackups.longhorn.io',
                 'systemrestores.longhorn.io', 'recurringjobs.longhorn.io', 'cronjobs'):
        if lab.get(kind, namespace=NAMESPACE)['items']:
            raise ValueError('detached recovery retains storage objects or consumers: ' + kind)
    if (any(obj.get('spec', {}).get('csi', {}).get('driver') == 'driver.longhorn.io'
            for obj in lab.get('pv')['items']) or
            lab.get('volumesnapshotcontents.snapshot.storage.k8s.io')['items'] or
            any(obj['metadata']['name'].startswith('longhorn-business-') for obj in lab.get('namespaces')['items'])):
        raise ValueError('detached recovery retains PV, snapshot content or business namespace')
    store = lab.backup_store()
    has_store = store.record.exists() or store.record.is_symlink() or store.directory.exists() or store.directory.is_symlink()
    if has_store:
        row = store.receipt()
        if row['name'] != binding['store_name'] or row['directory_identity'] != binding['directory_identity']:
            raise ValueError('detached recovery store identity changed')
        transport = store.transport_receipt(row)
        if transport and transport.get('socket_identity') != binding['socket_identity']:
            raise ValueError('detached recovery socket receipt changed')
        obj = store.inspect(row)
        if obj and (obj['Id'] != binding['container_id'] or obj['State']['Running']):
            raise ValueError('detached recovery container was replaced or restarted')
    else:
        target = lab.get('backuptargets.longhorn.io', 'default', NAMESPACE)
        containers = lab.execute(['docker', 'container', 'ls', '-a', '--filter',
            'name=^/' + binding['store_name'] + '$', '--format', '{{.ID}}']).stdout.strip()
        if (store.transport_listener() is not None or containers or not target or
                target['spec'].get('backupTargetURL') or target['spec'].get('credentialSecret')):
            raise ValueError('detached recovery retains an unowned store consumer')
    allowed = binding['store_name']
    for kind in ('pods', 'secrets'):
        for obj in lab.get(kind, namespace=NAMESPACE)['items']:
            if obj['metadata'].get('labels', {}).get('kubeauto.io/longhorn-backup-run'):
                if not has_store or obj['metadata']['name'] != allowed + ('-transport' if kind == 'pods' else ''):
                    raise ValueError('foreign backup fixture consumer')
    print('LONGHORN_V2_DETACHED_RECOVERY_BOUNDARY_VERIFIED', flush=True)
    return record, binding, store


def retire_stale_socket(store):
    """Remove a receipt-bound socket only after ECONNREFUSED proves no master."""
    row = store.receipt()
    binding = store.transport_receipt(row)
    path = store.directory/'transport.sock'
    if not binding or not path.exists():
        return
    if path.is_symlink() or not path.is_socket():
        raise ValueError('backup transport is not its original Unix socket')
    identity = path.stat()
    with socket.socket(socket.AF_UNIX) as probe:
        try:
            probe.connect(str(path))
        except OSError as error:
            if error.errno != errno.ECONNREFUSED:
                raise
        else:
            raise ValueError('backup SSH master is still active')
    current = path.lstat()
    if (current.st_dev, current.st_ino) != (identity.st_dev, identity.st_ino):
        raise ValueError('backup transport socket changed during recovery')
    path.unlink()
    print('LONGHORN_V2_DETACHED_STALE_SOCKET_RETIRED', flush=True)


def recover(lab):
    record, binding, store = recovery_boundary(lab)
    if store.record.exists():
        # The existing Store checks target/namespace/Secret UID and data before
        # mutation; no replacement object or foreign reference is adopted.
        store.restore_product()
        retire_stale_socket(store)
        store.cleanup()
    # Keep the exact journal until all external cleanup has completed. A failed
    # cleanup can retry this same narrow recovery without losing its receipt.
    if hashlib.sha256(json.dumps(json.loads(record.read_text()), sort_keys=True).encode()).hexdigest() != binding['business_sha256']:
        raise ValueError('detached business journal changed during recovery')
    record.unlink()
    print('LONGHORN_V2_DETACHED_INTERRUPTED_CLEANUP_RECOVERED', flush=True)


def verify(lab, journal, create, relay_pid, payload, digest, configuration):
    parent = lab.load_helper('longhorn-recurring.py').Schedule
    class Schedule(parent):
        @staticmethod
        def declaration(row, cron):
            result = parent.declaration(row, cron)
            result['parameters'] = {'full-backup-interval': '2'}
            return result
    schedule = Schedule(lab)
    namespace = journal['namespace']; origins = {}; attached_without_client = False
    with lab.scoped_cleanup(lambda: stop(lab, relay_pid)):
        schedule.begin(journal, relay_pid, task='backup-force-create',
                       backup_configuration=configuration, cron='0 0 1 1 *')
        schedule.select_source()
        writer = lab.get('pod', 'writer', namespace)
        if not writer or writer['metadata']['uid'] != journal['objects']['Pod/writer']:
            raise ValueError('detached backup writer identity changed')
        lab.remove(writer)
        lab.wait('detached-backup-writer-gone', 180, lambda: not lab.get('pod', 'writer', namespace))
        lab.wait('detached-backup-source-idle', 600, lambda:
                 lab.get('volumes.longhorn.io', journal['volume'], NAMESPACE)['status']['state'] == 'detached')
        Setting(lab).enable()
        schedule.cadence(relay_pid, '* * * * *')
        def completed():
            nonlocal attached_without_client
            observation = schedule.sample()
            source = lab.get('volumes.longhorn.io', journal['volume'], NAMESPACE)
            if source['metadata']['uid'] != journal['volume_uid']:
                raise ValueError('detached backup source identity changed')
            if lab.get('pods', namespace=namespace)['items']:
                raise ValueError('detached backup gained a workload consumer')
            if source.get('status', {}).get('state') == 'attached':
                attached_without_client = True
            modes = bind_modes(lab, observation, origins)
            return (observation, modes) if ({m['mode'] for m in modes.values()} == {'full', 'incremental'} and
                                            attached_without_client) else None
        lab.wait('detached-backup-periodic-modes', 1200, completed)
        schedule.cadence(relay_pid, '0 0 1 1 *')
        def idle():
            observation = schedule.sample()
            modes = bind_modes(lab, observation, origins)
            volume = lab.get('volumes.longhorn.io', journal['volume'], NAMESPACE)
            if (all(obj.get('status', {}).get('succeeded') == 1 for obj in observation['jobs']) and
                    volume['status']['state'] == 'detached' and not volume['spec'].get('nodeID') and
                    {m['mode'] for m in modes.values()} == {'full', 'incremental'}):
                return observation, modes
            return None
        observation, modes = lab.wait('detached-backup-complete-and-idle', 900, idle)
        receipts = {name: lab.backup_store().backup_receipt(journal['volume'], name, 2*1024**3) for name in modes}
        latest = max(observation['backups'], key=lambda obj: (obj['status']['backupCreatedAt'], obj['metadata']['name']))
        latest_name = latest['metadata']['name']
        if latest_name not in receipts:
            raise ValueError('latest detached backup lacks its native mode and external receipt')
        result = {'volume_uid': journal['volume_uid'], 'schedule_uid': observation['row']['uid'],
                  'interval': 2, 'attached_without_workload': attached_without_client,
                  'returned_detached': True, 'modes': modes, 'receipts': receipts,
                  'restore_backup': latest_name, 'snapshot_sha256': digest}
    record = lab.DIRECTORY/'longhorn-install-business.json'
    journal.update(json.loads(record.read_text()))
    for name, receipt in receipts.items():
        journal['backups'][name]['object_receipt'] = receipt
    lab.write_private(record, journal)
    reader = 'detached-source-reader'
    create(lab.client_pod(namespace, reader, 'data', 'longhorn-node-' + lab.WORKERS[0].rsplit('.', 1)[1]))
    lab.wait('detached-backup-source-reader', 600, lambda: any(
        row['type'] == 'Ready' and row['status'] == 'True'
        for row in lab.get('pod', reader, namespace).get('status', {}).get('conditions', [])))
    actual = lab.kubectl('-n', namespace, 'exec', reader, '--', 'python3', '-c',
                         lab.payload_program(payload, read_only=True)).stdout.strip()
    if actual != digest:
        raise ValueError('source bytes changed during detached scheduled backups')
    source_payload = payload + '-after-detached'
    source_digest = lab.kubectl('-n', namespace, 'exec', reader, '--', 'python3', '-c',
                                lab.payload_program(source_payload)).stdout.strip()
    if source_digest != hashlib.sha256(source_payload.encode()*262144).hexdigest() or source_digest == digest:
        raise ValueError('post-backup source write was not confirmed')
    lab.verify_backup_restore(journal, create, 'detached', latest_name, payload, digest,
                              source_payload, source_digest, source_client=reader)
    result['source_sha256'] = source_digest
    print('LONGHORN_V2_DETACHED_BACKUP_RESULT ' + json.dumps(result, sort_keys=True), flush=True)
    return source_payload, source_digest, reader


def business(lab, relay_pid):
    original = lab.load_helper
    def load(name):
        return SimpleNamespace(verify=verify) if name == 'longhorn-recurring-backup.py' else original(name)
    lab.load_helper = load
    try:
        lab.backup_suite(relay_pid, 'recurring_backup')
    finally:
        lab.load_helper = original


def verify_log(log):
    rows = re.findall(r'^LONGHORN_V2_DETACHED_BACKUP_RESULT (.+)$', log, re.M)
    if len(rows) != 1:
        raise ValueError('detached backup requires exactly one result')
    row = json.loads(rows[0])
    if (not row['volume_uid'] or not row['schedule_uid'] or row['interval'] != 2 or
            row['attached_without_workload'] is not True or row['returned_detached'] is not True or
            {item['mode'] for item in row['modes'].values()} != {'full', 'incremental'} or
            row['modes'].keys() != row['receipts'].keys() or row['restore_backup'] not in row['modes'] or
            len({item['uid'] for item in row['modes'].values()}) != len(row['modes'])):
        raise ValueError('detached backup lacks periodic request or external receipt boundaries')
    counts = set(); pods = set(); jobs = set(); volumes = set()
    for name, item in row['modes'].items():
        count = item['execution_count']
        if (type(count) is not int or count < 1 or count in counts or
                not item['pod_uid'] or item['pod_uid'] in pods or
                not item['job_uid'] or item['job_uid'] in jobs or not item['snapshot'] or
                item['mode'] != ('full' if count % 2 == 0 else 'incremental')):
            raise ValueError('detached backup lacks unique native execution identities')
        counts.add(count); pods.add(item['pod_uid']); jobs.add(item['job_uid'])
        receipt = row['receipts'][name]
        if (receipt['backup'] != name or receipt['size'] != 2*1024**3 or
                not receipt['volume'] or not receipt['objects']):
            raise ValueError('detached backup receipt lacks complete external objects')
        volumes.add(receipt['volume'])
        for obj in receipt['objects'].values():
            if obj['size'] <= 0 or not re.fullmatch('[a-f0-9]{64}', obj['sha256']):
                raise ValueError('detached backup external object is empty or lacks hash')
    if len(volumes) != 1:
        raise ValueError('detached backup receipts belong to different source volumes')
    boundaries = re.findall(r'^LONGHORN_BACKUP_RESTORE_BOUNDARY_VERIFIED mode=detached '
                            r'backup_sha256=([a-f0-9]{64}) source_sha256=([a-f0-9]{64}) '
                            r'independent_volume=(\S+)$', log, re.M)
    remounts = re.findall(r'^LONGHORN_FSYNC_REMOUNT_VERIFIED bytes=(\d+) sha256=([a-f0-9]{64}) replica_nodes=(\S+)$', log, re.M)
    if (len(boundaries) != 1 or len(remounts) != 1 or int(remounts[0][0]) <= 0 or
            len(set(remounts[0][2].split(','))) != 3 or
            boundaries[0][0] != row['snapshot_sha256'] or
            boundaries[0][1] != row['source_sha256'] or row['snapshot_sha256'] == row['source_sha256'] or
            remounts[0][1] != row['source_sha256'] or
            any(boundaries[0][2] == receipt['volume'] for receipt in row['receipts'].values())):
        raise ValueError('detached backup lacks independent restore and cross-node source data')
    for marker in ('LONGHORN_V2_DETACHED_SETTING_RESTORED', 'LONGHORN_RECURRING_BACKUP_SCHEDULES_STOPPED_PASS',
                   'LONGHORN_RECURRING_BACKUP_BUSINESS_PASS', 'LONGHORN_RECURRING_BACKUP_CLEAN_PASS',
                   'LONGHORN_BUSINESS_FIXTURES_CLEAN_PASS', 'LONGHORN_BACKUP_PRODUCT_FIXTURE_RESTORED',
                   'LONGHORN_BACKUP_TRANSPORT_CLEAN_PASS', 'LONGHORN_BACKUP_STORE_CLEAN_PASS',
                   'LONGHORN_BACKUP_STORE_QUALIFIED clients=3 tls=verified object_readback=exact'):
        if log.splitlines().count(marker) != 1:
            raise ValueError('detached backup lacks exact business/cleanup marker: ' + marker)
    return row


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=('baseline', 'business', 'cleanup', 'verify', 'recovery-baseline', 'recover'))
    parser.add_argument('--relay-pid', type=int, default=0)
    parser.add_argument('--log', type=Path)
    args = parser.parse_args()
    if args.mode == 'verify':
        verify_log(args.log.read_text())
        return
    recurring, backup, install, lab, store = fixture()
    lab.require_lease()
    if args.mode == 'recovery-baseline':
        recovery_boundary(lab)
    elif args.mode == 'recover':
        recover(lab)
    elif args.mode == 'baseline':
        if (lab.DIRECTORY/RECORD).exists() or (lab.DIRECTORY/RECORD).is_symlink():
            raise ValueError('detached Setting journal must be recovered before baseline reuse')
        recurring.baseline(backup, install, lab, store)
    elif args.mode == 'cleanup':
        stop(lab, args.relay_pid)
        recurring.cleanup(lab, args.relay_pid)
    else:
        backup.qualification_current(lab)
        recurring.selector_current(lab)
        business(lab, args.relay_pid)


if __name__ == '__main__':
    try:
        main()
    except Exception:
        import tempfile
        import traceback
        with tempfile.NamedTemporaryFile('w', prefix='longhorn-v2-detached-failure-',
                suffix='.txt', dir='/var/tmp', delete=False) as diagnostic:
            traceback.print_exc(file=diagnostic)
            print('LONGHORN_V2_DETACHED_BACKUP_FAIL private_diagnostic=' + diagnostic.name, file=sys.stderr)
        sys.exit(1)
