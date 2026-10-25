"""Private, identity-bound credential references for scheduled backup probes.

The product selects each Secret through config+kubecli. Recovery reconnects
the original valid fixture reference before native backup/volume cleanup.
"""
import base64
import copy
import hashlib
import json
import os
import re
import secrets


NAMESPACE = 'longhorn-system'


def data_digest(data):
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


def credential_failure(observation, previous):
    """A new native failed point, never merely a Job exit or target timeout."""
    result = []
    for backup in observation['backups']:
        if backup['metadata']['uid'] in previous:
            continue
        status = backup.get('status', {})
        if status.get('state') == 'Completed' or status.get('progress') == 100 or status.get('backupCreatedAt'):
            raise ValueError('revoked credential unexpectedly produced a completed backup')
        error = status.get('error')
        if status.get('state') in ('Error', 'Unknown') or error:
            if (status.get('state') != 'Error' or not isinstance(error, str) or
                    not re.search(r'(?<![A-Za-z0-9_])InvalidAccessKeyId(?![A-Za-z0-9_])', error)):
                raise ValueError('scheduled failure is not the native revoked-credential error')
            result.append(backup)
    return result or None


def admission_refusal(lab, observation, previous_backups, previous_jobs, previous_snapshots,
                      target_uid, entry):
    """Bind a pre-Backup webhook refusal to new scheduled work and the revoked key.

    A successful Job alone proves neither backup success nor credential refusal.
    Schedule.sample validates the live source and full native ownership chain;
    retain that chain here when associating its Pod log with the new snapshot.
    """
    row = observation['row']
    if any(obj['metadata']['uid'] not in previous_backups for obj in observation['backups']):
        return None  # Admitted points must be judged by their native Backup status.

    def controlled(obj, kind, uid):
        refs = obj['metadata'].get('ownerReferences', [])
        if kind == 'RecurringJob':
            return (len(refs) == 1 and refs[0].get('kind') == kind and refs[0].get('uid') == uid and
                    refs[0].get('apiVersion') == 'longhorn.io/v1beta2')
        refs = [ref for ref in refs if ref.get('controller')]
        return len(refs) == 1 and refs[0].get('kind') == kind and refs[0].get('uid') == uid

    cron = observation.get('cron')
    if not cron or cron['metadata']['uid'] != row['cron_uid'] or not controlled(cron, 'RecurringJob', row['uid']):
        return None
    jobs = {obj['metadata']['uid']: obj for obj in observation['jobs']
            if obj['metadata']['uid'] not in previous_jobs and obj.get('status', {}).get('succeeded') == 1 and
            row['jobs'].get(obj['metadata']['name']) == obj['metadata']['uid'] and
            controlled(obj, 'CronJob', row['cron_uid'])}
    snapshots = [obj for obj in observation['snapshots']
                 if obj['metadata']['uid'] not in previous_snapshots and
                 row['snapshots'].get(obj['metadata']['name']) == obj['metadata']['uid'] and
                 obj['spec'].get('volume') == row['volume'] and
                 obj['spec'].get('labels', {}).get('RecurringJob') == row['name'] and
                 obj.get('status', {}).get('readyToUse')]
    if not jobs or not snapshots:
        return None

    def target_error():
        target = lab.get('backuptargets.longhorn.io', 'default', NAMESPACE)
        secret = lab.get('secret', entry['name'], NAMESPACE)
        if (not target or target['metadata']['uid'] != target_uid or
                target['spec'].get('credentialSecret') != entry['name'] or
                not secret or secret['metadata']['uid'] != entry['uid'] or
                data_digest(secret.get('data', {})) != entry['digest']):
            raise ValueError('revoked credential target or Secret identity changed')
        status = target.get('status', {})
        return status.get('available') is False and any(
            condition.get('type') == 'Unavailable' and condition.get('status') == 'True' and
            re.search(r'(?<![A-Za-z0-9_])InvalidAccessKeyId(?![A-Za-z0-9_])', condition.get('message', ''))
            for condition in status.get('conditions', []))

    if not target_error():
        return None
    for pod in observation['pods']:
        if (pod.get('status', {}).get('phase') != 'Succeeded' or
                row['pods'].get(pod['metadata']['name']) != pod['metadata']['uid']):
            continue
        parents = [uid for uid in jobs if controlled(pod, 'Job', uid)]
        if len(parents) != 1:
            continue
        def same_pod():
            current = lab.get('pod', pod['metadata']['name'], NAMESPACE)
            if (not current or current['metadata']['uid'] != pod['metadata']['uid'] or
                    not controlled(current, 'Job', parents[0])):
                raise ValueError('scheduled refusal Pod identity or owner changed during log read')
        same_pod()
        raw = lab.kubectl('-n', NAMESPACE, 'logs', pod['metadata']['name'], '--tail=200', timeout=30).stdout
        same_pod()
        if not target_error():
            raise ValueError('revoked target error changed during scheduled log read')
        current_backups = lab.get('backups.longhorn.io', namespace=NAMESPACE)['items']
        if any(obj.get('spec', {}).get('labels', {}).get('RecurringJob') == row['name'] and
               obj['metadata']['uid'] not in previous_backups for obj in current_backups):
            raise ValueError('scheduled Backup appeared during admission refusal observation')
        for snapshot in snapshots:
            fields = {'job': row['name'], 'volumeName': row['volume'], 'snapshotName': snapshot['metadata']['name']}
            for line in raw.replace('\\"', '"').splitlines():
                phrases = ('level=error', 'failed to complete backupAndCleanup', 'failed to backup snapshot',
                           'admission webhook "validator.longhorn.io" denied the request',
                           'backup target default is not available')
                if all(phrase in line for phrase in phrases) and all(
                        re.search(r'(?:^|\s)' + key + r'=(?:"' + re.escape(value) + r'"|' +
                                  re.escape(value) + r')(?:\s|$)', line) for key, value in fields.items()):
                    return {'path': 'admission', 'job_uid': parents[0], 'pod_uid': pod['metadata']['uid'],
                            'snapshot_uid': snapshot['metadata']['uid'], 'target_uid': target_uid,
                            'secret_uid': entry['uid'], 'native_error': 'InvalidAccessKeyId',
                            'log_sha256': hashlib.sha256(raw.encode()).hexdigest()}
    return None


class Credentials:
    def __init__(self, lab, store):
        self.lab, self.store = lab, store
        self.record = lab.DIRECTORY / 'longhorn-recurring-credentials.json'
        self.accounts = lab.load_helper('longhorn-accounts.py').Accounts(lab, store)

    def initialize(self):
        if self.record.exists() or self.record.is_symlink():
            raise ValueError('previous recurring credentials need recovery')
        row = self.store.receipt(); binding = row['product_binding']
        state = self.lab.owner()
        if row['run_id'] != state['run_id'] or row['cluster_uid'] != state['cluster_uid']:
            raise ValueError('recurring credentials belong to another cluster or run')
        self.lab.write_private(self.record, {'run_id': row['run_id'], 'cluster_uid': row['cluster_uid'],
            'container_id': row['container_id'], 'target_uid': binding['target_uid'],
            'namespace_uid': binding['namespace_uid'], 'root_name': binding['secret_name'],
            'root_uid': binding['secret_uid'], 'root_digest': binding['secret_digest'], 'secrets': {}})
        self.read()
        self.accounts.initialize()

    def read(self):
        if self.record.is_symlink():
            raise ValueError('recurring credential journal is a symlink')
        info = self.record.stat()
        if info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError('recurring credential journal is not private')
        row = json.loads(self.record.read_text()); store = self.store.receipt()
        binding = store['product_binding']; state = self.lab.owner()
        expected = {'run_id': state['run_id'], 'cluster_uid': state['cluster_uid'],
            'container_id': store['container_id'], 'target_uid': binding['target_uid'],
            'namespace_uid': binding['namespace_uid'], 'root_name': binding['secret_name'],
            'root_uid': binding['secret_uid'], 'root_digest': binding['secret_digest']}
        if any(row.get(key) != value for key, value in expected.items()):
            raise ValueError('recurring credential fixture identity changed')
        self.accounts.context()  # Proves the original container is still running.
        namespace = self.lab.get('namespace', NAMESPACE)
        target = self.lab.get('backuptargets.longhorn.io', 'default', NAMESPACE)
        root = self.lab.get('secret', row['root_name'], NAMESPACE)
        if (not namespace or namespace['metadata']['uid'] != row['namespace_uid'] or
                not target or target['metadata']['uid'] != row['target_uid'] or
                not root or root['metadata']['uid'] != row['root_uid'] or
                data_digest(root.get('data', {})) != row['root_digest']):
            raise ValueError('recurring credential namespace, target or recovery Secret changed')
        for identifier, entry in row['secrets'].items():
            if (not re.fullmatch(r'lh[a-f0-9]{18}', identifier) or
                    not re.fullmatch(r'longhorn-rotation-[a-f0-9]{20}', entry['name'])):
                raise ValueError('recurring credential intent identity differs')
            current = self.lab.get('secret', entry['name'], NAMESPACE)
            if current and (current['metadata']['uid'] != entry.get('uid') or
                    current['metadata'].get('labels', {}).get('kubeauto.io/longhorn-backup-run') != row['run_id'] or
                    data_digest(current.get('data', {})) != entry['digest']):
                raise ValueError('recurring credential Secret identity or data changed')
            if not current and entry.get('uid') and not entry.get('retiring'):
                raise ValueError('recurring credential Secret disappeared')
        identity = self.lab.load_helper('longhorn-backups.py').target_configuration
        allowed = [binding['original_spec'], binding['fixture_spec']]
        allowed.extend(dict(binding['fixture_spec'], credentialSecret=entry['name'])
                       for entry in row['secrets'].values() if entry.get('uid'))
        if identity(target['spec']) not in [identity(spec) for spec in allowed]:
            raise ValueError('recurring target configuration changed outside its fixture')
        return row

    def create(self):
        row = self.read(); identifier = self.accounts.create()
        name = 'longhorn-rotation-' + secrets.token_hex(10)
        if self.lab.get('secret', name, NAMESPACE):
            raise ValueError('recurring credential Secret name already exists')
        root = self.lab.get('secret', row['root_name'], NAMESPACE)
        account = self.accounts.read()['accounts'][identifier]
        data = dict(root['data'], AWS_ACCESS_KEY_ID=base64.b64encode(identifier.encode()).decode(),
                    AWS_SECRET_ACCESS_KEY=base64.b64encode(account['secret'].encode()).decode())
        row['secrets'][identifier] = {'name': name, 'digest': data_digest(data)}
        self.lab.write_private(self.record, row)  # Creation intent precedes API mutation.
        obj = {'apiVersion': 'v1', 'kind': 'Secret', 'metadata': {'name': name, 'namespace': NAMESPACE,
            'labels': {'kubeauto.io/longhorn-backup-run': row['run_id']}}, 'type': 'Opaque', 'data': data}
        response = json.loads(self.lab.kubectl('create', '-f', '-', '-o', 'json', stdin=json.dumps(obj)).stdout)
        if (response.get('kind') != 'Secret' or response['metadata'].get('name') != name or
                response['metadata'].get('namespace') != NAMESPACE or not response['metadata'].get('uid') or
                data_digest(response.get('data', {})) != row['secrets'][identifier]['digest']):
            raise ValueError('recurring credential Secret creation response differs')
        row['secrets'][identifier]['uid'] = response['metadata']['uid']
        self.lab.write_private(self.record, row)
        self.read()
        return identifier

    def configuration(self, identifier=None):
        row = self.read()
        name = row['root_name']
        if identifier is not None:
            account = self.accounts.read()['accounts'].get(identifier)
            entry = row['secrets'].get(identifier)
            if not account or account['phase'] != 'ready' or not entry or not entry.get('uid'):
                raise ValueError('recurring credential is not a ready owned identity')
            self.accounts.verify(identifier)
            name = entry['name']
        return {'longhorn_backup_target': self.store.receipt()['product_binding']['fixture_spec']['backupTargetURL'],
                'longhorn_backup_secret': name}

    def select(self, relay_pid, schedule, identifier=None):
        configuration = self.configuration(identifier)
        row = schedule.read()
        row['backup_configuration'] = configuration
        schedule.save(row)  # The same reference is available to interrupted cleanup.
        self.lab.run_requests(relay_pid, [], recurring_jobs=[schedule.declaration(row, row['spec']['cron'])],
                              backup_configuration=configuration)
        self.read()
        target = self.lab.get('backuptargets.longhorn.io', 'default', NAMESPACE)
        if target['spec']['credentialSecret'] != configuration['longhorn_backup_secret']:
            raise ValueError('product credential selection was not observed')

    def restore_root(self, relay_pid):
        if not self.record.exists() and not self.record.is_symlink():
            return
        self.read(); self.lab.restore_request_input()
        schedule = self.lab.load_helper('longhorn-recurring.py').Schedule(self.lab)
        configuration = self.configuration()
        declarations = []
        if schedule.record.exists() or schedule.record.is_symlink():
            row = schedule.read()
            if not row.get('uid') or row.get('pending_spec'):
                raise ValueError('unconfirmed recurring schedule needs its original recovery')
            row['backup_configuration'] = configuration; schedule.save(row)
            declarations = [schedule.declaration(row, row['spec']['cron'])]
        target = self.lab.get('backuptargets.longhorn.io', 'default', NAMESPACE)
        if (target['spec'].get('credentialSecret') != configuration['longhorn_backup_secret'] or
                not target.get('status', {}).get('available')):
            self.lab.run_requests(relay_pid, [], recurring_jobs=declarations, backup_configuration=configuration)
        self.read()
        target = self.lab.get('backuptargets.longhorn.io', 'default', NAMESPACE)
        if (target['spec']['credentialSecret'] != configuration['longhorn_backup_secret'] or
                not target.get('status', {}).get('available')):
            raise ValueError('original valid recurring credential was not restored')
        print('LONGHORN_RECURRING_CREDENTIAL_REFERENCE_RESTORED', flush=True)

    def cleanup(self):
        if not self.record.exists() and not self.record.is_symlink():
            return
        row = self.read()
        target = self.lab.get('backuptargets.longhorn.io', 'default', NAMESPACE)
        schedule = self.lab.DIRECTORY / 'longhorn-recurring-runtime.json'
        if (target['spec'].get('credentialSecret') != row['root_name'] or not target.get('status', {}).get('available') or
                schedule.exists() or schedule.is_symlink()):
            raise ValueError('recurring credential cleanup requires restored reference and stopped schedule')
        for identifier, entry in row['secrets'].items():
            current = self.lab.get('secret', entry['name'], NAMESPACE)
            if not entry.get('uid'):
                raise ValueError('unconfirmed credential Secret creation needs exact-identity recovery')
            entry['retiring'] = True; self.lab.write_private(self.record, row)
            if current:
                self.lab.remove(current)
            self.lab.wait('recurring-credential-secret-clean', 90, lambda entry=entry:
                          self.lab.get('secret', entry['name'], NAMESPACE) is None)
            self.read()
        self.record.unlink()
        self.accounts.cleanup()
        print('LONGHORN_RECURRING_CREDENTIALS_CLEAN_PASS', flush=True)

def completed_point(observation, previous):
    points = []
    for obj in observation['backups']:
        if obj['metadata']['uid'] in previous:
            continue
        status = obj.get('status', {})
        if (not obj['metadata'].get('deletionTimestamp') and status.get('state') == 'Completed' and
                status.get('progress') == 100 and not status.get('error') and status.get('backupCreatedAt')):
            points.append(obj)
    return max(points, key=lambda obj: (obj['status']['backupCreatedAt'], obj['metadata']['name'])) if points else None


def verify(lab, journal, create, relay_pid, payload, digest, configuration):
    """Use new scheduled points to prove key selection, revocation and repair."""
    schedule = lab.load_helper('longhorn-recurring.py').Schedule(lab)
    store = lab.backup_store()
    credentials = Credentials(lab, store)
    record = lab.DIRECTORY / 'longhorn-install-business.json'
    namespace = journal['namespace']
    observations = {}
    frozen_failures = {}

    def recover():
        credentials.restore_root(relay_pid)
        schedule.cleanup(relay_pid)
        credentials.cleanup()

    def inspect_point(obj, identifier, phase):
        row = schedule.read(); name = obj['metadata']['name']
        expected = store.backup_origin(row['volume'], name, obj['spec']['snapshotName'], row['name'])
        prefix = lab.load_helper('longhorn-backups.py').volume_prefix(row['volume'])
        key = 'lh/' + store.receipt()['bucket'] + '/' + prefix + 'backups/backup_' + name + '.cfg'
        response = credentials.accounts.command('cat', key, account=identifier)
        if hashlib.sha256(response.stdout.encode()).hexdigest() != expected:
            raise ValueError('signed recurring credential read differs from native backup config')
        observations[phase] = {'backup': name, 'uid': obj['metadata']['uid'], 'config_sha256': expected}
        print('LONGHORN_RECURRING_CREDENTIAL_STAGE phase=' + phase + ' backup_uid=' + obj['metadata']['uid'], flush=True)
        return key, expected

    def recorded_error(obj):
        uid = obj['metadata']['uid']
        identity = {key: value for key, value in obj['spec'].items() if key != 'syncRequestedAt'}
        if (uid not in frozen_failures or identity != frozen_failures[uid]['spec'] or
                not credential_failure({'backups': [obj]}, set())):
            raise ValueError('a new or changed recurring credential error appeared after repair')
        return True

    def observe(failure=None):
        observation = schedule.sample(backup_failure=failure)
        for obj in observation['backups']:
            if obj['metadata']['uid'] in frozen_failures:
                recorded_error(obj)
        return observation

    def wait_point(label, previous, failure=None):
        def ready():
            observation = observe(failure)
            return completed_point(observation, previous)
        return lab.wait(label, 600, ready)

    def settle_unselected(failure, previous=None):
        before = set(schedule.read()['completed'])
        def idle():
            observation = observe(failure)
            if previous is not None:
                credential_failure(observation, previous)
            return observation if (set(observation['row']['completed']) - before and all(
                obj.get('status', {}).get('succeeded') == 1 for obj in observation['jobs'])) else None
        # v1.12.1 recurringjob waits up to 90 * 5s for BackupStatus even
        # when Backup already reports Error. CronJob Forbid blocks the next
        # one-minute cycle until that Job exits; retain controller/API margin.
        timeout = 600 if failure is not None else 300
        return lab.wait('credential-source-unselected-cycle', timeout, idle)

    with lab.scoped_cleanup(recover):
        schedule.begin(journal, relay_pid, task='backup-force-create', backup_configuration=configuration)
        credentials.initialize()
        first = credentials.create(); credentials.select(relay_pid, schedule, first)
        previous = set(observe()['row']['backups'].values())
        schedule.select_source()
        inspect_point(wait_point('credential-a-scheduled-backup', previous), first, 'first')
        second = credentials.create(); credentials.select(relay_pid, schedule, second)
        previous = set(observe()['row']['backups'].values())
        key, expected = inspect_point(wait_point('credential-b-scheduled-backup', previous), second, 'second')
        positive = credentials.accounts.command('cat', key, account=first)
        if hashlib.sha256(positive.stdout.encode()).hexdigest() != expected:
            raise ValueError('old credential positive control differs before revocation')
        credentials.accounts.revoke(first)
        refusal = credentials.accounts.command('cat', key, account=first, allow_revoked=True, check=False)
        credentials.accounts.require_refusal(refusal,
            {'InvalidAccessKeyId'}, 'scheduled-old-key-refusal', operation='cat', target=key)
        if hashlib.sha256(credentials.accounts.command('cat', key, account=second).stdout.encode()).hexdigest() != expected:
            raise ValueError('replacement credential positive control differs after revocation')
        schedule.deselect_source()
        settle_unselected(None)
        before_revocation = observe()
        previous = set(before_revocation['row']['backups'].values())
        previous_jobs = set(before_revocation['row']['jobs'].values())
        previous_snapshots = set(before_revocation['row']['snapshots'].values())
        credentials.accounts.revoke(second)
        schedule.select_source()
        refusal_evidence = {}
        def revoked_error(obj):
            if not credential_failure({'backups': [obj]}, previous):
                raise ValueError('native revoked-key error belongs to an earlier backup')
            return True
        def failed():
            observation = observe(revoked_error)
            if credential_failure(observation, previous):
                refusal_evidence.update(path='backup', native_error='InvalidAccessKeyId')
                return observation
            state = credentials.read()
            evidence = admission_refusal(lab, observation, previous, previous_jobs, previous_snapshots,
                                         state['target_uid'], state['secrets'][second])
            if evidence:
                refusal_evidence.update(evidence)
                return observation
            return None
        lab.wait('selected-revoked-key-native-backup-error', 600, failed)
        # Native volume selection is a documented customer operation. Remove
        # this owned group while preserving the product schedule and journal;
        # wait a complete unselected cycle before changing the invalid target.
        schedule.deselect_source()
        stopped = settle_unselected(revoked_error, previous)
        for obj in credential_failure(stopped, previous) or []:
            frozen_failures[obj['metadata']['uid']] = {
                'name': obj['metadata']['name'],
                'spec': {key: copy.deepcopy(value) for key, value in obj['spec'].items() if key != 'syncRequestedAt'}}
        print('LONGHORN_RECURRING_CREDENTIAL_STAGE phase=native-revocation-verified failed_points=' +
              str(len(frozen_failures)), flush=True)
        third = credentials.create(); credentials.select(relay_pid, schedule, third)
        previous = set(observe(recorded_error)['row']['backups'].values())
        schedule.select_source()
        inspect_point(wait_point('credential-c-recovered-scheduled-backup', previous, recorded_error), third, 'recovered')
        schedule.deselect_source()
        final = settle_unselected(recorded_error)
        latest = completed_point(final, previous)
        if not latest:
            raise ValueError('recovered credential has no retained completed backup')
        latest_name = latest['metadata']['name']
        key, expected = inspect_point(latest, third, 'restore')
        receipt = store.backup_receipt(journal['volume'], latest_name, 2 * 1024**3)
        for failed_point in frozen_failures.values():
            missing = key.rsplit('/', 1)[0] + '/backup_' + failed_point['name'] + '.cfg'
            credentials.accounts.require_refusal(credentials.accounts.command('stat', missing, check=False),
                {'NoSuchKey'}, 'failed-scheduled-point-has-no-metadata', operation='stat', target=missing)
        if hashlib.sha256(credentials.accounts.command('cat', key, account=third).stdout.encode()).hexdigest() != expected:
            raise ValueError('failed metadata absence lacked a valid existing-object control')
        schedule.cleanup(relay_pid)
        credentials.restore_root(relay_pid)
        credentials.cleanup()
        print('LONGHORN_RECURRING_CREDENTIAL_ROTATION_OBSERVED ' + json.dumps({
            'stages': observations, 'failed_backups': {uid: point['name'] for uid, point in frozen_failures.items()},
            'revocation': refusal_evidence,
            'native_error': 'InvalidAccessKeyId', 'failed_metadata_absent': len(frozen_failures),
            'source_sha256': digest, 'receipt': receipt}, sort_keys=True), flush=True)
    journal.update(json.loads(record.read_text()))
    journal['backups'][latest_name]['object_receipt'] = receipt
    lab.write_private(record, journal); lab.bind_backups(journal)
    source_payload = payload + '-after-credential-rotation'
    source_digest = lab.kubectl('-n', namespace, 'exec', 'writer', '--', 'python3', '-c',
                               lab.payload_program(source_payload)).stdout.strip()
    if source_digest != hashlib.sha256(source_payload.encode() * 262144).hexdigest():
        raise ValueError('post-rotation source write was not confirmed')
    lab.verify_backup_restore(journal, create, 'rotation', latest_name, payload, digest,
                              source_payload, source_digest, source_client='writer')
    return source_payload, source_digest, 'writer'
