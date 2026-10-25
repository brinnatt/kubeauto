"""Native scheduled backup retention and independent CSI restore qualifier.

This prerequisite does not claim credential rotation or full LONGHORN-16.
All schedule changes use the product; native reads prove actual artifacts.
"""
import hashlib
import json


NAMESPACE = 'longhorn-system'


def retained(observation):
    row = observation['row']; backups = observation['backups']
    if len(row['completed']) < 3 or len(row['backups']) < 3 or len(backups) != 2:
        return None
    for obj in backups:
        status = obj.get('status', {})
        if (obj['metadata'].get('deletionTimestamp') or status.get('state') != 'Completed' or
                status.get('progress') != 100 or status.get('error') or not status.get('backupCreatedAt')):
            return None
    if not row['pods']:
        raise ValueError('scheduled backup completed without an observed Pod ownership chain')
    return observation


def verify(lab, journal, create, relay_pid, payload, digest, configuration):
    schedule = lab.load_helper('longhorn-recurring.py').Schedule(lab)
    namespace = journal['namespace']; record = lab.DIRECTORY / 'longhorn-install-business.json'
    store = lab.backup_store(); receipts = {}
    with lab.scoped_cleanup(lambda: schedule.cleanup(relay_pid)):
        schedule.begin(journal, relay_pid, task='backup-force-create', backup_configuration=configuration)
        create({'apiVersion': 'v1', 'kind': 'PersistentVolumeClaim',
                'metadata': {'name': 'unselected', 'namespace': namespace},
                'spec': {'accessModes': ['ReadWriteOnce'], 'storageClassName': journal['storage_class'],
                         'resources': {'requests': {'storage': '2Gi'}}}})
        lab.wait('scheduled-backup-canary-bound', 600, lambda:
                 lab.get('pvc', 'unselected', namespace).get('status', {}).get('phase') == 'Bound')
        create(lab.client_pod(namespace, 'unselected', 'unselected',
                             'longhorn-node-' + lab.WORKERS[1].rsplit('.', 1)[1]))
        lab.wait('scheduled-backup-canary-ready', 600, lambda: any(
            x['type'] == 'Ready' and x['status'] == 'True'
            for x in lab.get('pod', 'unselected', namespace).get('status', {}).get('conditions', [])))
        canary_payload = payload + '-unselected'
        canary_digest = lab.kubectl('-n', namespace, 'exec', 'unselected', '--', 'python3', '-c',
                                    lab.payload_program(canary_payload)).stdout.strip()
        if canary_digest != hashlib.sha256(canary_payload.encode() * 262144).hexdigest():
            raise ValueError('scheduled backup canary write was not confirmed')
        schedule.select_source()
        lab.wait('scheduled-backup-retention', 1800, lambda: retained(schedule.sample()))
        schedule.cadence(relay_pid, '0 0 1 1 *')
        def idle():
            observation = schedule.sample()
            return observation if retained(observation) and all(
                obj.get('status', {}).get('succeeded') == 1 for obj in observation['jobs']) else None
        observation = lab.wait('scheduled-backup-prior-jobs-complete', 900, idle)
        before = {obj['metadata']['name']: obj['metadata']['uid'] for obj in observation['backups']}
        # Native CR retention alone does not prove the backend removed expired
        # metadata. Read every retained config/block and the exact metadata set.
        for name in before:
            receipts[name] = store.backup_receipt(journal['volume'], name, 2 * 1024**3)
        prefix = lab.load_helper('longhorn-backups.py').volume_prefix(journal['volume']) + 'backups/'
        bucket = store.receipt()['bucket']
        raw = store.mc('ls', '--recursive', '--json', 'lh/' + bucket + '/' + prefix)
        listed = set()
        for line in raw.splitlines():
            item = json.loads(line)
            if item.get('status') != 'success' or item.get('type') != 'file':
                raise ValueError('scheduled backup metadata listing is not a native file result')
            if item['key'] in listed:
                raise ValueError('scheduled backup metadata listing repeats an object')
            listed.add(item['key'])
        # mc ls keys are relative to the requested prefix, as in the account
        # qualifier. An unexpected directory or key must not pass retention.
        expected = {'backup_' + name + '.cfg' for name in before}
        if listed != expected or not (set(observation['row']['backups']) - set(before)):
            raise ValueError('scheduled backup backend retention differs from two observed backups')
        writer = lab.get('pod', 'writer', namespace)
        if writer['metadata']['uid'] != journal['objects']['Pod/writer']:
            raise ValueError('scheduled backup writer was replaced')
        lab.remove(writer)
        lab.wait('scheduled-backup-writer-gone', 180, lambda: not lab.get('pod', 'writer', namespace))
        lab.wait('scheduled-backup-source-detached', 600, lambda:
                 lab.get('volumes.longhorn.io', journal['volume'], NAMESPACE)['status']['state'] == 'detached')
        detached = schedule.sample()
        completed = set(detached['row']['completed'])
        snapshots = {obj['metadata']['name']: obj['metadata']['uid'] for obj in detached['snapshots']}
        schedule.cadence(relay_pid, '* * * * *')
        def skipped():
            observation = schedule.sample()
            current = {obj['metadata']['name']: obj['metadata']['uid'] for obj in observation['backups']}
            if (current != before or
                    {obj['metadata']['name']: obj['metadata']['uid'] for obj in observation['snapshots']} != snapshots or
                    lab.get('volumes.longhorn.io', journal['volume'], NAMESPACE)['status']['state'] != 'detached'):
                raise ValueError('detached scheduled backup changed artifacts or attached its source')
            return observation if len(set(observation['row']['completed']) - completed) >= 2 else None
        observation = lab.wait('scheduled-backup-two-detached-cycles', 300, skipped)
        actual = lab.kubectl('-n', namespace, 'exec', 'unselected', '--', 'python3', '-c',
                             lab.payload_program(canary_payload, read_only=True)).stdout.strip()
        if actual != canary_digest:
            raise ValueError('scheduled backup modified unselected canary bytes')
        latest = max(observation['backups'], key=lambda obj: (obj['status']['backupCreatedAt'], obj['metadata']['name']))
        latest_name = latest['metadata']['name']
        print('LONGHORN_RECURRING_BACKUP_OBSERVED ' + json.dumps({
            'completed_jobs': len(observation['row']['completed']),
            'observed_backups': len(observation['row']['backups']), 'retained_backups': len(before),
            'backend_origins': observation['row'].get('backup_origins', {}),
            'detached_skipped_jobs': len(set(observation['row']['completed']) - completed),
            'source_sha256': digest, 'canary_sha256': canary_digest,
            'restore_backup': latest_name, 'receipts': receipts}, sort_keys=True), flush=True)
    # Schedule cleanup hands exact observed UIDs/specifications to business
    # cleanup before any CSI restore or source-volume deletion.
    journal.update(json.loads(record.read_text()))
    for name, receipt in receipts.items():
        journal['backups'][name]['object_receipt'] = receipt
    lab.write_private(record, journal)
    lab.bind_backups(journal)
    source_client = 'recurring-source-reader'
    create(lab.client_pod(namespace, source_client, 'data',
                         'longhorn-node-' + lab.WORKERS[0].rsplit('.', 1)[1]))
    lab.wait('scheduled-backup-source-reader', 600, lambda: any(
        x['type'] == 'Ready' and x['status'] == 'True'
        for x in lab.get('pod', source_client, namespace).get('status', {}).get('conditions', [])))
    actual = lab.kubectl('-n', namespace, 'exec', source_client, '--', 'python3', '-c',
                         lab.payload_program(payload, read_only=True)).stdout.strip()
    if actual != digest:
        raise ValueError('scheduled backup source bytes changed during detached cycles')
    source_payload = payload + '-after-recurring'
    source_digest = lab.kubectl('-n', namespace, 'exec', source_client, '--', 'python3', '-c',
                                lab.payload_program(source_payload)).stdout.strip()
    if source_digest != hashlib.sha256(source_payload.encode() * 262144).hexdigest():
        raise ValueError('scheduled backup post-point source write was not confirmed')
    lab.verify_backup_restore(journal, create, 'recurring', latest_name, payload, digest,
                              source_payload, source_digest, source_client=source_client)
    return source_payload, source_digest, source_client
