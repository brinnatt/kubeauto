"""Product-created recurring schedules and their native UID ownership chain.

Each focused qualifier remains separate from complete LONGHORN-16 acceptance.
"""
import json
import hashlib
import secrets


NAMESPACE = 'longhorn-system'


def controlled(obj, kind, uid):
    # Longhorn's GetOwnerReferencesForRecurringJob omits controller=true;
    # Kubernetes CronJob->Job and Job->Pod references set it explicitly.
    if kind == 'RecurringJob':
        references = obj['metadata'].get('ownerReferences', [])
        return (len(references) == 1 and references[0].get('kind') == kind and
                references[0].get('uid') == uid and references[0].get('apiVersion') == 'longhorn.io/v1beta2')
    references = [ref for ref in obj['metadata'].get('ownerReferences', []) if ref.get('controller')]
    return len(references) == 1 and references[0].get('kind') == kind and references[0].get('uid') == uid


def remember(ledger, obj):
    """A disappearance is allowed; same-name replacement never grants ownership."""
    name, uid = obj['metadata']['name'], obj['metadata']['uid']
    if name in ledger and ledger[name] != uid:
        raise ValueError('recurring child identity was replaced')
    ledger[name] = uid


def children(objects, ledger, owns):
    selected = []
    for obj in objects:
        name = obj['metadata']['name']
        if name in ledger and (obj['metadata']['uid'] != ledger[name] or not owns(obj)):
            raise ValueError('recorded recurring child identity or owner changed')
        if owns(obj):
            remember(ledger, obj)
            selected.append(obj)
    return selected


class Schedule:
    def __init__(self, lab):
        self.lab = lab
        self.record = lab.DIRECTORY / 'longhorn-recurring-runtime.json'

    def read(self):
        if self.record.is_symlink():
            raise ValueError('recurring runtime journal is a symlink')
        row = json.loads(self.record.read_text()); state = self.lab.owner()
        if row['run_id'] != state['run_id'] or row['cluster_uid'] != state['cluster_uid']:
            raise ValueError('recurring runtime belongs to another cluster or run')
        source = self.lab.get('volumes.longhorn.io', row['volume'], NAMESPACE)
        if not source or source['metadata']['uid'] != row['volume_uid']:
            raise ValueError('recurring source volume was replaced')
        return row

    def save(self, row):
        self.lab.write_private(self.record, row)

    def request(self, row, relay_pid, jobs):
        options = {'recurring_jobs': jobs}
        if row.get('backup_configuration'):
            options['backup_configuration'] = row['backup_configuration']
        self.lab.run_requests(relay_pid, [], **options)

    @staticmethod
    def declaration(row, cron):
        return {'name': row['name'], 'task': row.get('task', 'snapshot'), 'cron': cron,
                'retain': 2, 'concurrency': 1, 'groups': [row['group']]}

    def begin(self, journal, relay_pid, *, task='snapshot', backup_configuration=None, cron='* * * * *'):
        if task not in ('snapshot', 'backup-force-create') or bool(backup_configuration) != (task == 'backup-force-create'):
            raise ValueError('recurring runtime requires a supported task and matching backup configuration')
        if self.record.exists() or self.record.is_symlink():
            raise ValueError('previous recurring runtime needs recovery')
        configuration = self.lab.yaml.safe_load((self.lab.DIRECTORY / 'config.yml').read_text())
        baseline = self.lab.recurring_identity()
        if configuration['longhorn_recurring_jobs'] or any(
                obj['labels'].get('app.kubernetes.io/managed-by') == 'kubeauto' for obj in baseline.values()):
            raise ValueError('recurring runtime requires an empty managed schedule baseline')
        state = self.lab.owner(); name = task.split('-')[0] + '-' + secrets.token_hex(10)
        setting = self.lab.get('settings.longhorn.io', 'allow-recurring-job-while-volume-detached', NAMESPACE)
        if not setting or setting.get('value') != 'false':
            raise ValueError('snapshot qualifier requires the native detached-job setting disabled')
        row = {'run_id': state['run_id'], 'cluster_uid': state['cluster_uid'],
               'volume': journal['volume'], 'volume_uid': journal['volume_uid'],
               'name': name, 'group': 'group-' + secrets.token_hex(10), 'baseline': baseline,
               'jobs': {}, 'pods': {}, 'completed': [], 'snapshots': {}, 'phase': 'intent'}
        row['task'] = task
        if backup_configuration:
            row.update(backup_configuration=backup_configuration, backups={}, backup_specs={})
        if any(row['group'] in value['spec'].get('groups', []) for value in baseline.values()):
            raise ValueError('recurring qualification group already exists')
        self.save(row)
        self.request(row, relay_pid, [self.declaration(row, cron)])
        current = self.lab.get('recurringjobs.longhorn.io', name, NAMESPACE)
        if not current or current['metadata']['name'] in baseline:
            raise ValueError('recurring product declaration was not observed')
        row.update(uid=current['metadata']['uid'], spec=current['spec'], phase='running')
        self.save(row)
        self.lab.wait('recurring-native-cronjob', 120, self.sample)

    def cadence(self, relay_pid, cron):
        row = self.read()
        declaration = self.declaration(row, cron)
        desired = dict(row['spec'], cron=cron)
        row['pending_spec'] = desired; self.save(row)
        self.request(row, relay_pid, [declaration])
        current = self.lab.get('recurringjobs.longhorn.io', row['name'], NAMESPACE)
        if not current or current['metadata']['uid'] != row['uid'] or current['spec'] != desired:
            raise ValueError('recurring cadence update changed identity or spec')
        row['previous_cron'] = row['spec']['cron']
        row['spec'] = desired; row.pop('pending_spec'); self.save(row)
        def converged():
            observation = self.sample()
            return observation if observation and observation['cron']['spec']['schedule'] == cron else None
        self.lab.wait('recurring-cadence-converged', 120, converged)
        row = self.read(); row.pop('previous_cron'); self.save(row)

    def sample(self, *, stopping=False, backup_failure=None):
        row = self.read()
        current = self.lab.get('recurringjobs.longhorn.io', row['name'], NAMESPACE)
        allowed_specs = [row.get('spec')]
        if stopping and row.get('pending_spec'):
            allowed_specs.append(row['pending_spec'])
        if current and (current['metadata']['uid'] != row.get('uid') or current['spec'] not in allowed_specs):
            raise ValueError('recurring declaration identity or spec changed')
        if not current and not stopping:
            raise ValueError('recurring declaration disappeared while active')
        others = {key: value for key, value in self.lab.recurring_identity().items() if key != row['name']}
        if others != row['baseline']:
            raise ValueError('foreign recurring declaration changed')
        cron = self.lab.get('cronjob', row['name'], NAMESPACE)
        if cron:
            spec = cron['spec']; template = spec['jobTemplate']['spec']
            crons = [value['cron'] for value in allowed_specs if value]
            if row.get('previous_cron'):
                crons.append(row['previous_cron'])
            if (not controlled(cron, 'RecurringJob', row.get('uid')) or
                    cron['metadata']['uid'] != row.get('cron_uid', cron['metadata']['uid']) or
                    spec['schedule'] not in crons or spec['concurrencyPolicy'] != 'Forbid' or
                    template['backoffLimit'] != 3 or template['template']['spec']['restartPolicy'] != 'OnFailure'):
                raise ValueError('recurring native CronJob contract differs')
            row['cron_uid'] = cron['metadata']['uid']
        elif not stopping:
            return None
        jobs = children(self.lab.get('jobs', namespace=NAMESPACE)['items'], row['jobs'],
                        lambda obj: bool(row.get('cron_uid')) and controlled(obj, 'CronJob', row['cron_uid']))
        for obj in jobs:
            status = obj.get('status', {})
            if any(c.get('type') == 'Failed' and c.get('status') == 'True' for c in status.get('conditions', [])):
                if not stopping:
                    raise ValueError('native recurring snapshot Job failed')
            if status.get('succeeded', 0) == 1 and obj['metadata']['uid'] not in row['completed']:
                row['completed'].append(obj['metadata']['uid'])
        pods = children(self.lab.get('pods', namespace=NAMESPACE)['items'], row['pods'],
                        lambda obj: any(controlled(obj, 'Job', uid) for uid in row['jobs'].values()))
        snapshots = children(self.lab.get('snapshots.longhorn.io', namespace=NAMESPACE)['items'], row['snapshots'],
                             lambda obj: obj.get('spec', {}).get('labels', {}).get('RecurringJob') == row['name'])
        for obj in snapshots:
            if obj['spec'].get('volume') != row['volume']:
                raise ValueError('recurring snapshot selected an unexpected volume')
        backups = []
        if row.get('task') == 'backup-force-create':
            candidates = children(self.lab.get('backups.longhorn.io', namespace=NAMESPACE)['items'], row['backups'],
                                  lambda obj: obj.get('spec', {}).get('labels', {}).get('RecurringJob') == row['name'])
            for obj in candidates:
                name = obj['metadata']['name']; spec = obj['spec']; labels = obj['metadata'].get('labels', {})
                if labels.get('backup-volume') != row['volume'] or labels.get('backup-target') != 'default':
                    raise ValueError('recurring backup source or target differs')
                # The poller updates syncRequestedAt; it does not grant ownership
                # of changed snapshot, mode, block size or recurring labels.
                identity = {key: value for key, value in spec.items() if key != 'syncRequestedAt'}
                if row['backup_specs'].get(name, identity) != identity:
                    raise ValueError('recurring backup specification changed')
                available = self.observe_backup_origin(row, obj, snapshots)
                row['backup_specs'][name] = identity
                if not available:
                    continue
                backups.append(obj)
                if not stopping and (obj.get('status', {}).get('state') in ('Error', 'Unknown') or
                                     obj.get('status', {}).get('error')):
                    if backup_failure is None or backup_failure(obj) is not True:
                        raise ValueError('native recurring backup failed')
        self.save(row)
        return {'row': row, 'cron': cron, 'jobs': jobs, 'pods': pods, 'snapshots': snapshots, 'backups': backups}

    def observe_backup_origin(self, row, backup, snapshots):
        name = backup['metadata']['name']; snapshot = backup['spec'].get('snapshotName')
        if snapshot in row['snapshots']:
            return True
        origins = row.setdefault('backup_origins', {})
        if name in origins:
            receipt = origins[name]
            if receipt['uid'] != backup['metadata']['uid'] or receipt['snapshot'] != snapshot:
                raise ValueError('recurring backup provenance identity changed')
            return True
        if not snapshot:
            raise ValueError('recurring backup snapshot identity is missing')
        # Snapshot creation may fall between the list and this backup read.
        current = self.lab.get('snapshots.longhorn.io', snapshot, NAMESPACE)
        if current:
            if (current['metadata']['name'] != snapshot or current['spec'].get('volume') != row['volume'] or
                    current['spec'].get('labels', {}).get('RecurringJob') != row['name']):
                raise ValueError('recurring backup snapshot provenance differs')
            remember(row['snapshots'], current)
            snapshots.append(current)
            return True
        # Manager 1.12.1 can collect a completed backup's local snapshot before
        # our next poll. Preserve backend proof separately; never invent a UID.
        status = backup.get('status', {})
        if (status.get('state') not in ('Completed', 'Deleting') or status.get('progress') != 100 or status.get('error') or
                not status.get('backupCreatedAt') or status.get('snapshotName') != snapshot):
            raise ValueError('unobserved recurring snapshot requires a completed native backup')
        if status['state'] == 'Deleting':
            # Retention deletes the remote point asynchronously and can collect
            # its snapshot before our poll. Observe native GC by exact identity;
            # this point grants neither restore evidence nor a fabricated UID.
            if not backup['metadata'].get('deletionTimestamp'):
                raise ValueError('recurring backup deletion has no native deletion intent')
            identity = {key: value for key, value in backup['spec'].items() if key != 'syncRequestedAt'}
            def gone():
                current = self.lab.get('backups.longhorn.io', name, NAMESPACE)
                if not current:
                    return True
                if (current['metadata']['uid'] != backup['metadata']['uid'] or
                        not current['metadata'].get('deletionTimestamp') or
                        current.get('status', {}).get('state') != 'Deleting' or
                        current.get('status', {}).get('error') or
                        current['metadata'].get('labels', {}) != backup['metadata'].get('labels', {}) or
                        {key: value for key, value in current['spec'].items() if key != 'syncRequestedAt'} != identity):
                    raise ValueError('recurring backup changed during native deletion')
                return False
            self.lab.wait('recurring-unobserved-backup-gc', 600, gone)
            return False
        digest = self.lab.backup_store().backup_origin(row['volume'], name, snapshot, row['name'])
        origins[name] = {'uid': backup['metadata']['uid'], 'snapshot': snapshot, 'sha256': digest}
        return True

    def select_source(self):
        row = self.read(); source = self.lab.get('volumes.longhorn.io', row['volume'], NAMESPACE)
        key = 'recurring-job-group.longhorn.io/' + row['group']
        if key in source['metadata'].get('labels', {}):
            raise ValueError('recurring selector already exists')
        patch = [{'op': 'test', 'path': '/metadata/uid', 'value': row['volume_uid']},
                 {'op': 'test', 'path': '/metadata/resourceVersion', 'value': source['metadata']['resourceVersion']},
                 {'op': 'add', 'path': '/metadata/labels', 'value': dict(source['metadata'].get('labels', {}), **{key: 'enabled'})}]
        self.lab.kubectl('-n', NAMESPACE, 'patch', 'volumes.longhorn.io', row['volume'],
                         '--type=json', '--patch-file=/dev/stdin', stdin=json.dumps(patch))
        if self.lab.get('volumes.longhorn.io', row['volume'], NAMESPACE)['metadata'].get('labels', {}).get(key) != 'enabled':
            raise ValueError('recurring volume selector was not observed')

    def recover_selector(self):
        """Retire only this fixture's orphan label after its schedule is gone.

        Explicit failed-run recovery only; normal qualification must prove the
        product/native deletion cleaned the selector without this operation.
        """
        observation = self.sample(stopping=True); row = observation['row']
        if (self.lab.get('recurringjobs.longhorn.io', row['name'], NAMESPACE) or
                observation['cron'] or observation['jobs'] or observation['pods']):
            raise ValueError('recurring selector recovery requires the schedule and all children gone')
        if self._remove_source_selector(row):
            print('LONGHORN_RECURRING_ORPHAN_SELECTOR_RECOVERED', flush=True)

    def deselect_source(self):
        """Remove this volume's owned group through the native selection API."""
        row = self.read()
        current = self.lab.get('recurringjobs.longhorn.io', row['name'], NAMESPACE)
        if not current or current['metadata']['uid'] != row['uid'] or current['spec'] != row['spec']:
            raise ValueError('recurring source deselection requires its original active schedule')
        if not self._remove_source_selector(row):
            raise ValueError('recurring source group is not selected')

    def _remove_source_selector(self, row):
        source = self.lab.get('volumes.longhorn.io', row['volume'], NAMESPACE)
        if source['metadata']['uid'] != row['volume_uid']:
            raise ValueError('recurring selector source was replaced')
        key = 'recurring-job-group.longhorn.io/' + row['group']
        value = source['metadata'].get('labels', {}).get(key)
        if value is None:
            return False
        if value != 'enabled':
            raise ValueError('recurring selector recovery value changed')
        path = '/metadata/labels/' + key.replace('~', '~0').replace('/', '~1')
        patch = [{'op': 'test', 'path': '/metadata/uid', 'value': row['volume_uid']},
                 {'op': 'test', 'path': '/metadata/resourceVersion', 'value': source['metadata']['resourceVersion']},
                 {'op': 'test', 'path': path, 'value': 'enabled'}, {'op': 'remove', 'path': path}]
        self.lab.kubectl('-n', NAMESPACE, 'patch', 'volumes.longhorn.io', row['volume'],
                         '--type=json', '--patch-file=/dev/stdin', stdin=json.dumps(patch))
        current = self.lab.get('volumes.longhorn.io', row['volume'], NAMESPACE)
        if current['metadata']['uid'] != row['volume_uid'] or key in current['metadata'].get('labels', {}):
            raise ValueError('recurring source selector removal was not observed')
        return True

    def cleanup(self, relay_pid, *, recover_selector=False):
        if not self.record.exists() and not self.record.is_symlink():
            return
        row = self.read()
        if not row.get('uid'):
            raise ValueError('unconfirmed recurring creation needs exact-identity recovery')
        self.lab.restore_request_input()
        observation = self.sample(stopping=True)
        if (self.lab.get('recurringjobs.longhorn.io', row['name'], NAMESPACE) or
                observation['cron'] or observation['jobs'] or observation['pods']):
            self.request(row, relay_pid, [])
        def stopped():
            observation = self.sample(stopping=True)
            return (not observation['cron'] and not observation['jobs'] and not observation['pods'] and
                    not self.lab.get('recurringjobs.longhorn.io', row['name'], NAMESPACE))
        self.lab.wait('recurring-owned-jobs-stopped', 600, stopped)
        if recover_selector:
            self.recover_selector()
        observation = self.sample(stopping=True)
        # The newest snapshot can remain the parent of volume-head after a
        # native delete. Bind its observed UID to the business cleanup, which
        # deletes the source before waiting for all native snapshots to vanish.
        # Never wait for this CR while the retained source still exists.
        business_record = self.lab.DIRECTORY / 'longhorn-install-business.json'
        if business_record.is_symlink():
            raise ValueError('recurring business journal is a symlink')
        business = json.loads(business_record.read_text())
        if business.get('volume') != row['volume'] or business.get('volume_uid') != row['volume_uid']:
            raise ValueError('recurring cleanup business source differs')
        for obj in observation['snapshots']:
            entry = business.setdefault('native_snapshots', {}).setdefault(obj['metadata']['name'], {})
            if entry.get('uid', obj['metadata']['uid']) != obj['metadata']['uid']:
                raise ValueError('recurring snapshot handoff identity differs')
            entry['uid'] = obj['metadata']['uid']
        for name, uid in observation['row'].get('backups', {}).items():
            entry = {'uid': uid, 'recurring_job': row['name'],
                     'spec': observation['row']['backup_specs'][name]}
            previous = business.setdefault('backups', {}).get(name, entry)
            if previous != entry:
                raise ValueError('recurring backup handoff identity differs')
            business['backups'][name] = entry
        self.lab.write_private(business_record, business)
        self.lab.wait('recurring-selector-clean', 120, lambda:
            'recurring-job-group.longhorn.io/' + row['group'] not in
            self.lab.get('volumes.longhorn.io', row['volume'], NAMESPACE)['metadata'].get('labels', {}))
        if self.lab.recurring_identity() != row['baseline']:
            raise ValueError('recurring baseline differs after cleanup')
        self.record.unlink()
        kind = 'BACKUP' if row.get('task') == 'backup-force-create' else 'SNAPSHOT'
        print('LONGHORN_RECURRING_' + kind + '_SCHEDULES_STOPPED_PASS', flush=True)

    def verify_selector(self, journal, relay_pid):
        """Narrow product deletion proof before timed retention scenarios."""
        with self.lab.scoped_cleanup(lambda: self.cleanup(relay_pid)):
            self.begin(journal, relay_pid, cron='0 0 1 1 *')
            self.select_source()
            observation = self.sample()
            if any(observation[key] for key in ('jobs', 'pods', 'snapshots', 'backups')):
                raise ValueError('annual selector qualifier unexpectedly scheduled work')
            print('LONGHORN_RECURRING_SELECTOR_OBSERVED source_uid=' + journal['volume_uid'] +
                  ' schedule_uid=' + observation['row']['uid'], flush=True)

    def verify_snapshot(self, journal, create, relay_pid, payload, digest):
        """Exercise schedules, retention, unselected canary and detached skips."""
        with self.lab.scoped_cleanup(lambda: self.cleanup(relay_pid)):
            self.begin(journal, relay_pid)
            namespace = journal['namespace']
            create({'apiVersion': 'v1', 'kind': 'PersistentVolumeClaim',
                    'metadata': {'name': 'unselected', 'namespace': namespace},
                    'spec': {'accessModes': ['ReadWriteOnce'], 'storageClassName': journal['storage_class'],
                             'resources': {'requests': {'storage': '2Gi'}}}})
            self.lab.wait('recurring-canary-bound', 600, lambda:
                self.lab.get('pvc', 'unselected', namespace).get('status', {}).get('phase') == 'Bound')
            create(self.lab.client_pod(namespace, 'unselected', 'unselected',
                   'longhorn-node-' + self.lab.WORKERS[1].rsplit('.', 1)[1]))
            self.lab.wait('recurring-canary-ready', 600, lambda: any(
                x['type'] == 'Ready' and x['status'] == 'True'
                for x in self.lab.get('pod', 'unselected', namespace).get('status', {}).get('conditions', [])))
            canary_digest = self.lab.kubectl('-n', namespace, 'exec', 'unselected', '--', 'python3', '-c',
                self.lab.payload_program(payload + '-unselected')).stdout.strip()
            if canary_digest != hashlib.sha256((payload + '-unselected').encode() * 262144).hexdigest():
                raise ValueError('unselected canary did not confirm its fsynced payload')
            self.select_source()
            def retained():
                # Normal snapshot tasks skip unchanged heads. Keep the primary
                # acknowledged payload untouched and fsync a separate file.
                self.lab.kubectl('-n', namespace, 'exec', 'writer', '--', 'python3', '-c',
                    "import os,time;f=open('/data/recurring-tick','w');f.write(str(time.time_ns()));f.flush();os.fsync(f.fileno());f.close()")
                observation = self.sample(); row = observation['row']; snapshots = observation['snapshots']
                if len(row['completed']) < 3 or len(row['snapshots']) < 3 or len(snapshots) != 2:
                    return None
                if not all(obj.get('status', {}).get('readyToUse') and not obj['metadata'].get('deletionTimestamp') for obj in snapshots):
                    return None
                if not row['pods']:
                    raise ValueError('recurring Job completed without an observed Pod owner chain')
                return observation
            retained_observation = self.lab.wait('recurring-snapshot-retention', 900, retained)
            # Pause through the product's cron declaration and wait for every
            # already-started Job before measuring detached skips. A detached
            # volume cannot invalidate work selected while it was attached.
            self.cadence(relay_pid, '0 0 1 1 *')
            def idle():
                observation = self.sample()
                return observation if all(obj.get('status', {}).get('succeeded') == 1 for obj in observation['jobs']) else None
            self.lab.wait('recurring-prior-jobs-complete', 600, idle)
            writer = self.lab.get('pod', 'writer', namespace)
            if writer['metadata']['uid'] != journal['objects']['Pod/writer']:
                raise ValueError('recurring writer was replaced')
            self.lab.remove(writer)
            self.lab.wait('recurring-writer-gone', 180, lambda: not self.lab.get('pod', 'writer', namespace))
            self.lab.wait('recurring-source-detached', 600, lambda:
                self.lab.get('volumes.longhorn.io', journal['volume'], NAMESPACE).get('status', {}).get('state') == 'detached')
            detached_start = self.sample()
            before = {obj['metadata']['name']: obj['metadata']['uid'] for obj in detached_start['snapshots']}
            completed = set(detached_start['row']['completed'])
            self.cadence(relay_pid, '* * * * *')
            def skipped():
                observation = self.sample()
                if {obj['metadata']['name']: obj['metadata']['uid'] for obj in observation['snapshots']} != before:
                    raise ValueError('detached recurring job changed native snapshot identities')
                if self.lab.get('volumes.longhorn.io', journal['volume'], NAMESPACE)['status']['state'] != 'detached':
                    raise ValueError('detached recurring job unexpectedly attached its volume')
                return observation if len(set(observation['row']['completed']) - completed) >= 2 else None
            skipped_observation = self.lab.wait('recurring-detached-two-cycles', 300, skipped)
            actual = self.lab.kubectl('-n', namespace, 'exec', 'unselected', '--', 'python3', '-c',
                self.lab.payload_program(payload + '-unselected', read_only=True)).stdout.strip()
            if actual != canary_digest:
                raise ValueError('unselected canary acknowledged bytes changed')
            row = skipped_observation['row']
            print('LONGHORN_RECURRING_SNAPSHOT_OBSERVED ' + json.dumps({
                'completed_jobs': len(row['completed']), 'observed_snapshots': len(row['snapshots']),
                'retained_snapshots': len(before), 'detached_skipped_jobs': len(set(row['completed']) - completed),
                'cron_uid': row['cron_uid'], 'pod_uids': sorted(row['pods'].values()),
                'source_sha256': digest, 'canary_sha256': canary_digest}, sort_keys=True), flush=True)
        # The outer business path expects its original writer before performing
        # the independent cross-worker remount/data check.
        journal.update(json.loads((self.lab.DIRECTORY / 'longhorn-install-business.json').read_text()))
        create(self.lab.client_pod(namespace, 'recurring-reader', 'data',
                                  'longhorn-node-' + self.lab.WORKERS[0].rsplit('.', 1)[1]))
        self.lab.wait('recurring-reader-ready', 600, lambda: any(
            x['type'] == 'Ready' and x['status'] == 'True'
            for x in self.lab.get('pod', 'recurring-reader', namespace).get('status', {}).get('conditions', [])))
        actual = self.lab.kubectl('-n', namespace, 'exec', 'recurring-reader', '--', 'python3', '-c',
            self.lab.payload_program(payload, read_only=True)).stdout.strip()
        if actual != digest:
            raise ValueError('recurring snapshot source bytes differ after detached cycles')
        return 'recurring-reader'
