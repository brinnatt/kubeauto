#!/usr/bin/env python3
"""Restore-chain refusal on one exact block of the runner's isolated backup.

The normal config+kubecli backup and CSI recovery paths remain unchanged.
A native restore CR independently observes the bad-chain error before repair.
"""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import secrets
import sys

BASE = Path('/usr/local/kubeauto')
NS = 'longhorn-system'
LABEL = 'kubeauto.io/longhorn-broken-restore'


class Probe:
    def __init__(self, lab, store):
        self.lab, self.store = lab, store
        self.record = lab.DIRECTORY/'longhorn-v2-broken-restore.json'
        self.accounts = lab.load_helper('longhorn-accounts.py')
        self.client = self.accounts.Accounts(lab, store)

    def save(self, row):
        self.lab.write_private(self.record, row)

    def target(self, key):
        return 'lh/'+self.store.receipt()['bucket']+'/'+key

    def present(self, key):
        target = self.target(key)
        result = self.client.command('stat', target, check=False)
        if result.returncode == 0:
            values = self.accounts.documents(result.stdout)
            if len(values) != 1 or values[0].get('status') != 'success':
                raise ValueError('unrecognized backup object stat success')
            return True
        if self.accounts.native_refusal(result, {'NoSuchKey'}, operation='stat', target=target):
            return False
        self.client.fail(result, 'broken-chain-object-stat')

    def read(self):
        if self.record.is_symlink():
            raise ValueError('symlink broken-restore journal')
        row = json.loads(self.record.read_text())
        store = self.store.receipt(); state = self.lab.owner()
        prefix = self.lab.load_helper('longhorn-backups.py').volume_prefix(row['source_volume'])
        block = row['block']; checksum = block.rsplit('/', 1)[-1].removesuffix('.blk')
        expected = prefix+'blocks/'+checksum[:2]+'/'+checksum[2:4]+'/'+checksum+'.blk'
        if (row['run_id'] != state['run_id'] or row['cluster_uid'] != state['cluster_uid'] or
                row['store_name'] != store['name'] or row['container_id'] != self.store.inspect(store)['Id'] or
                row['bucket'] != store['bucket'] or not re.fullmatch('[a-f0-9]{64}', checksum) or
                block != expected or row['escrow'] != prefix+'fault-escrow/'+row['nonce']+'.blk' or
                not re.fullmatch('[a-f0-9]{24}', row['nonce']) or
                row['volume_name'] != 'broken-restore-'+row['nonce'] or
                row['object'].get('size', 0) <= 0 or not re.fullmatch('[a-f0-9]{64}', row['object'].get('sha256', ''))):
            raise ValueError('broken-restore journal or store identity changed')
        return row

    def activate(self, journal, backup):
        if self.record.exists() or self.record.is_symlink():
            raise ValueError('previous broken-restore fault requires recovery')
        receipt = journal['backups'][backup]['object_receipt']
        verified = self.store.backup_receipt(journal['volume'], backup, 2*1024**3)
        if receipt != verified:
            raise ValueError('backup objects changed before fault admission')
        blocks = sorted(k for k in receipt['objects'] if k.endswith('.blk'))
        if not blocks:
            raise ValueError('backup has no verified referenced block')
        store = self.store.receipt(); nonce = secrets.token_hex(12)
        row = {'run_id': self.lab.owner()['run_id'], 'cluster_uid': self.lab.owner()['cluster_uid'],
            'store_name': store['name'], 'container_id': self.store.inspect(store)['Id'], 'bucket': store['bucket'],
            'source_volume': journal['volume'], 'backup': backup, 'block': blocks[0],
            'object': receipt['objects'][blocks[0]], 'nonce': nonce, 'volume_name': 'broken-restore-'+nonce,
            'escrow': self.lab.load_helper('longhorn-backups.py').volume_prefix(journal['volume'])+'fault-escrow/'+nonce+'.blk'}
        if self.present(row['escrow']) or self.lab.get('volumes.longhorn.io', row['volume_name'], NS):
            raise ValueError('broken-restore escrow or native target already exists')
        # Persist intent before either copy or deletion. A lost response is
        # resolved from original/escrow byte hashes, never an assumed outcome.
        self.save(row)
        self.client.command('cp', self.target(row['block']), self.target(row['escrow']))
        if self.store.object_digest(row['escrow']) != row['object']:
            raise ValueError('backup escrow differs from verified original')
        if self.store.object_digest(row['block']) != row['object']:
            raise ValueError('original backup object changed before isolation')
        self.client.command('rm', self.target(row['block']))
        if self.present(row['block']):
            raise ValueError('faulted backup block remains readable')
        print('LONGHORN_V2_RESTORE_BLOCK_ISOLATED '+json.dumps({
            'backup': backup, 'block': row['block'], 'object': row['object']}, sort_keys=True), flush=True)
        return row

    def volume(self, row):
        volume = self.lab.get('volumes.longhorn.io', row['volume_name'], NS)
        if volume and (not row.get('volume_uid') or volume['metadata']['uid'] != row['volume_uid'] or
                volume['metadata'].get('labels', {}).get(LABEL) != row['nonce'] or
                any(volume['spec'].get(k) != v for k, v in row['volume_spec'].items())):
            raise ValueError('broken-restore target changed or create outcome is unknown')
        return volume

    def failure_event(self, row, engine):
        # The volume controller records the replica's native restore error
        # before failing it. Stopping the Engine then clears restoreStatus;
        # the UID-bound Warning event preserves that exact error across polls.
        replicas = {r['metadata']['name']: r for r in
            self.lab.get('replicas.longhorn.io', namespace=NS)['items']
            if r['spec'].get('volumeName') == row['volume_name'] and
            r['spec'].get('engineName') == engine['metadata']['name'] and
            r['spec'].get('dataEngine') == 'v2' and r['metadata'].get('uid') and
            any(ref.get('kind') == 'Volume' and ref.get('uid') == row['volume_uid']
                for ref in r['metadata'].get('ownerReferences', []))}
        for event in self.lab.get('events', namespace=NS)['items']:
            ref = event.get('involvedObject', {})
            if (not event['metadata'].get('uid') or event.get('type') != 'Warning' or
                    event.get('reason') != 'FailedRestore' or
                    event.get('source', {}).get('component') != 'longhorn-volume-controller' or
                    any(ref.get(k) != v for k, v in {
                        'apiVersion': 'longhorn.io/v1beta2', 'kind': 'Volume', 'namespace': NS,
                        'name': row['volume_name'], 'uid': row['volume_uid']}.items())):
                continue
            error = event.get('message', '')
            if not self.missing_block(row, error):
                continue
            for name, replica in replicas.items():
                if error.startswith('replica '+name+' failed the restore: '):
                    return {'error': error, 'error_origin': 'native-event',
                        'event_uid': event['metadata']['uid'], 'replica_uid': replica['metadata']['uid']}
        return None

    @staticmethod
    def missing_block(row, error):
        return row['block'] in error and any(code in error for code in (
            'NoSuchKey', 'specified key does not exist', 'Object does not exist'))

    def observe(self, journal, backup):
        row = self.read()
        source = self.lab.get('backups.longhorn.io', backup, NS)
        if not source or source['metadata']['uid'] != journal['backups'][backup]['uid']:
            raise ValueError('broken-restore backup identity changed')
        row['volume_spec'] = {'size': str(2*1024**3), 'fromBackup': source['status']['url'],
            'numberOfReplicas': 3, 'frontend': 'blockdev', 'dataEngine': 'v2'}
        self.save(row)
        obj = {'apiVersion': 'longhorn.io/v1beta2', 'kind': 'Volume', 'metadata': {
            'name': row['volume_name'], 'namespace': NS, 'labels': {LABEL: row['nonce']}}, 'spec': row['volume_spec']}
        volume = json.loads(self.lab.kubectl('create', '-f', '-', '-o', 'json', stdin=json.dumps(obj)).stdout)
        row['volume_uid'] = volume['metadata']['uid']; self.save(row)

        def refused():
            volume = self.volume(row)
            if not volume:
                raise ValueError('broken-restore target disappeared')
            current = self.lab.get('backups.longhorn.io', backup, NS)
            if not current or current['metadata']['uid'] != source['metadata']['uid']:
                raise ValueError('broken-restore backup replaced during observation')
            if self.present(row['block']):
                raise ValueError('fault was repaired before native refusal')
            # A newly created Volume serializes zero-value status booleans.
            # The controller sets both flags only in checkAndInitVolumeRestore;
            # restoreRequired=false has completion meaning after that boundary.
            status = volume.get('status', {})
            if status.get('restoreInitiated') is not True:
                return None
            if status.get('restoreRequired') is not True:
                raise ValueError('broken chain was accepted as a completed restore')
            engines = [e for e in self.lab.get('engines.longhorn.io', namespace=NS)['items']
                if e['spec'].get('volumeName') == row['volume_name']]
            for engine in engines:
                if engine['spec'].get('requestedBackupRestore') != backup:
                    continue
                if engine.get('status', {}).get('lastRestoredBackup') == backup:
                    raise ValueError('broken chain was recorded as restored')
                failure = next(({'error': s.get('error', ''), 'error_origin': 'engine-status'}
                    for s in (engine.get('status', {}).get('restoreStatus') or {}).values()
                    if self.missing_block(row, s.get('error', ''))), None)
                if failure is None and (engine.get('status', {}).get('currentState') in ('stopped', 'error') or
                        status.get('robustness') == 'faulted'):
                    failure = self.failure_event(row, engine)
                if failure:
                    error = failure.pop('error')
                    return {'volume_uid': row['volume_uid'], 'engine_uid': engine['metadata']['uid'],
                        'backup_uid': source['metadata']['uid'], 'backup': backup,
                        'block': row['block'], 'error_sha256': hashlib.sha256(error.encode()).hexdigest(),
                        'restore_required': True, 'block_absent': True,
                        'last_restored_backup_matches': False, **failure}
            return None
        # The pinned backupstore retries a missing read for 516 seconds. A
        # timeout is a gate failure, never accepted as native refusal evidence.
        result = self.lab.wait('broken-chain-native-refusal', 1000, refused)
        print('LONGHORN_V2_BROKEN_CHAIN_NATIVE_REFUSED '+json.dumps(result, sort_keys=True), flush=True)
        return result

    def cleanup(self):
        if self.record.is_symlink():
            raise ValueError('symlink broken-restore journal')
        if not self.record.exists():
            return
        row = self.read()
        # Repair the source even if native cleanup is blocked. Never overwrite
        # a different object, even inside a runner-owned bucket.
        original = self.present(row['block']); escrow = self.present(row['escrow'])
        if original and self.store.object_digest(row['block']) != row['object']:
            raise ValueError('original backup object changed outside this fault')
        if escrow and self.store.object_digest(row['escrow']) != row['object']:
            raise ValueError('backup escrow bytes changed')
        if not original:
            if not escrow:
                raise ValueError('original and escrow are absent; retain owning store')
            self.client.command('cp', self.target(row['escrow']), self.target(row['block']))
            if self.store.object_digest(row['block']) != row['object']:
                raise ValueError('restored backup object differs')
        volume = self.volume(row)
        if volume:
            if any(p.get('spec', {}).get('csi', {}).get('volumeHandle') == row['volume_name']
                    for p in self.lab.get('pv')['items']):
                raise ValueError('a PV consumes the failed native probe')
            self.lab.remove(volume)
            self.lab.wait('broken-chain-target-clean', 300, lambda: self.volume(row) is None)
        if escrow:
            self.client.command('rm', self.target(row['escrow']))
            if self.present(row['escrow']):
                raise ValueError('backup escrow remains after recovery')
        self.record.unlink()
        print('LONGHORN_V2_BROKEN_RESTORE_FAULT_CLEAN_PASS', flush=True)


def business(lab, relay_pid):
    store = lab.backup_store(); probe = Probe(lab, store)
    original_restore, original_cleanup = lab.verify_backup_restore, lab.cleanup_business
    observed = []

    def restore(journal, create, name, backup, *args, **options):
        if name == 'incremental':
            probe.activate(journal, backup)
            observed.append(probe.observe(journal, backup))
            probe.cleanup()
            if store.backup_receipt(journal['volume'], backup, 2*1024**3) != journal['backups'][backup]['object_receipt']:
                raise ValueError('repaired backup chain differs from original receipt')
        return original_restore(journal, create, name, backup, *args, **options)

    def cleanup(*args, **options):
        probe.cleanup()
        return original_cleanup(*args, **options)

    lab.verify_backup_restore, lab.cleanup_business = restore, cleanup
    try:
        with lab.scoped_cleanup(lambda: (cleanup(), store.cleanup())):
            store.prepare(); store.qualify()
            lab.business('backup', relay_pid, backup_configuration=store.product_configuration())
        if len(observed) != 1:
            raise ValueError('broken-chain negative path did not execute exactly once')
        print('LONGHORN_V2_BROKEN_RESTORE_BUSINESS_PASS', flush=True)
    finally:
        lab.verify_backup_restore, lab.cleanup_business = original_restore, original_cleanup


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=('business', 'clean'))
    parser.add_argument('--relay-pid', type=int, default=0)
    args = parser.parse_args()
    spec = importlib.util.spec_from_file_location('broken_backup', BASE/'tests/helpers/longhorn-v2-backups.py')
    backups = importlib.util.module_from_spec(spec); spec.loader.exec_module(backups)
    _install, lab, _store_module = backups.fixture(); lab.require_lease()
    if args.mode == 'clean':
        Probe(lab, lab.backup_store()).cleanup()
    else:
        if args.relay_pid <= 0:
            parser.error('business requires the runner-owned relay')
        backups.qualification_current(lab)
        business(lab, args.relay_pid)


if __name__ == '__main__':
    try:
        main()
    except Exception:
        import tempfile
        import traceback
        with tempfile.NamedTemporaryFile('w', prefix='longhorn-v2-broken-restore-', suffix='.txt',
                dir='/var/tmp', delete=False) as diagnostic:
            traceback.print_exc(file=diagnostic)
            print('LONGHORN_V2_BROKEN_RESTORE_FAIL private_diagnostic='+diagnostic.name, file=sys.stderr)
        sys.exit(1)
