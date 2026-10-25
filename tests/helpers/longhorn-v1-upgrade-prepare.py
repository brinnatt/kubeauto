#!/usr/bin/env python3
"""Owned empty-store transition for LONGHORN-35/36; no in-place downgrade.

The original six-node core survives. Its empty 1.12.1 storage installation is
retired through kubecli before a clean 1.12.0 installation is created. A failed
transition retains its exact stage; it must never be mistaken for a new baseline.
"""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import sys

import yaml

SPEC = importlib.util.spec_from_file_location('v1_upgrade_prepare_impl', Path(__file__).with_name('longhorn-v1-upgrade-lifecycle.py'))
impl = importlib.util.module_from_spec(SPEC); SPEC.loader.exec_module(impl)
RECORD = 'longhorn-v1-upgrade-preparation.json'
ALLOWLIST_SHA256 = 'fd50cf8ccb3b1f1e0b1efa0a1b7fc576cf84f897af6d5f59460cb87f412ee7c9'
JOURNALS = ('longhorn-install-business.json', 'longhorn-backup-store.json',
    'longhorn-dr-backup-consumer.json', 'longhorn-v1-upgrade-lifecycle.json',
    'longhorn-upgrade-lifecycle.json', 'longhorn-request-input.json')


def digest(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file(): raise ValueError('missing regular transition input: '+str(path))
    return hashlib.sha256(path.read_bytes()).hexdigest()


def empty(lab, backups, *, initial_receipt=False):
    """Refuse before generic uninstall cleanup could remove any existing work."""
    journals = [name for name in JOURNALS if not (initial_receipt and name == 'longhorn-v1-upgrade-lifecycle.json')]
    if any((lab.DIRECTORY/name).exists() or (lab.DIRECTORY/name).is_symlink() for name in journals):
        raise ValueError('V1 upgrade transition retains another business journal')
    if (any(lab.get(kind, namespace=impl.NS)['items'] for kind in ('volumes.longhorn.io', *backups.BACKUP_RESOURCES)) or
            any(p.get('spec', {}).get('csi', {}).get('driver') == 'driver.longhorn.io' for p in lab.get('pv')['items']) or
            any(n['metadata']['name'].startswith('longhorn-business-') for n in lab.get('namespaces')['items'])):
        raise ValueError('V1 upgrade transition requires an empty storage installation')
    target = lab.get('backuptargets.longhorn.io', 'default', impl.NS)
    if not target or target['spec'].get('backupTargetURL') or target['spec'].get('credentialSecret'):
        raise ValueError('V1 upgrade transition retains an external backup consumer')


def core(lab):
    lab.core_identity(); state = lab.owner()
    return {key: state[key] for key in ('run_id', 'cluster_uid', 'node_uids')}


def healthy(lab, engine, version):
    lab.core_identity(); lab.installation_health()
    release = impl.upgrade.release(lab)
    config = yaml.safe_load((lab.DIRECTORY/'config.yml').read_text())
    if (release['app_version'].removeprefix('v') != version or release.get('status') != 'deployed' or
            config.get('longhorn_data_engines', ['v1']) != [engine] or
            str(config.get('longhorn_version')) != version or config.get('longhorn_action', 'present') != 'present' or
            config.get('longhorn_requests')):
        raise ValueError('V1 transition requires its actual pinned, idle engine/version')


def target_identity(target):
    return {'retained': impl.upgrade.retained_identity(target, {}),
            'config_sha256': digest(target.DIRECTORY/'config.yml'),
            'raw_receipt_sha256': digest(target.DIRECTORY/'yml/longhorn/block-disks.json')}


class Preparation:
    def __init__(self, source, target, backups):
        self.source, self.target, self.backups = source, target, backups
        self.path = source.DIRECTORY/RECORD

    def save(self, row): self.source.write_private(self.path, row)

    def read(self):
        digest(self.path); row = json.loads(self.path.read_text())
        if core(self.source) != row['source_core'] or core(self.target) != row['target_core']:
            raise ValueError('V1 upgrade transition core ownership changed')
        if target_identity(self.target) != row['target_identity']:
            raise ValueError('V1 upgrade transition changed the original independent target')
        return row

    def preflight(self):
        source, target = self.source, self.target
        if self.path.exists() or self.path.is_symlink():
            self.initial_failure()
            print('LONGHORN_V1_UPGRADE_TRANSITION_PREFLIGHT_PASS', flush=True)
            return
        if tuple(source.NODES) != impl.SOURCE_NODES or tuple(target.NODES) != ('192.168.47.149',):
            raise ValueError('V1 transition requires the exact source and independent target inventories')
        if source.owner()['cluster_uid'] == target.owner()['cluster_uid']:
            raise ValueError('V1 transition target must be independent')
        for lab, engine in ((source, 'v1'), (target, 'v2')):
            healthy(lab, engine, impl.upgrade.NEW); empty(lab, self.backups)
        allowlist = source.BASE/'tests/fixtures/longhorn-v1-qualified-disks.txt'
        if digest(allowlist) != ALLOWLIST_SHA256: raise ValueError('qualified source disk allowlist changed')
        state = source.owner()
        if (set(state.get('disks', {})) != set(source.WORKERS) or not state.get('host_state') or
                not state.get('namespace_uid') or any(row['phase'] != 'mounted' for row in state['disks'].values())):
            raise ValueError('V1 transition needs its complete original disk/host/namespace receipts')
        # Inventory and verify both pinned versions before removing storage.
        source.cache_artifacts()
        row = {'source_core': core(source), 'target_core': core(target), 'target_identity': target_identity(target),
               'allowlist_sha256': ALLOWLIST_SHA256, 'stage': 'preflight-complete'}
        self.save(row)
        print('LONGHORN_V1_UPGRADE_TRANSITION_PREFLIGHT_PASS', flush=True)

    def prepare(self, pid):
        row = self.read()
        if row['stage'] == 'old-ready' and pid > 0:
            business, content, archived = self.initial_failure()
            if business is None:
                # A qualified pre-upgrade scene keeps its original data and
                # protection requests; the business driver resumes that scope.
                print('LONGHORN_V1_UPGRADE_TRANSITION_OLD_READY', flush=True)
                return
            # Preserve the failed attempt byte-for-byte before retiring only its
            # empty receipt. Never reinstall or downgrade this healthy baseline.
            with os.fdopen(os.open(archived, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'wb') as stream:
                stream.write(content); stream.flush(); os.fsync(stream.fileno())
            if business.read_bytes() != content: raise ValueError('initial upgrade receipt changed before retirement')
            business.unlink()
            print('LONGHORN_V1_UPGRADE_TRANSITION_OLD_READY', flush=True)
            return
        if row['stage'] != 'preflight-complete' or pid <= 0: raise ValueError('invalid V1 transition stage or supervisor')
        source = self.source
        healthy(source, 'v1', impl.upgrade.NEW); empty(source, self.backups)
        row['stage'] = 'retiring-empty-new'; self.save(row)
        source.recover_installation(pid)
        if core(source) != row['source_core']: raise ValueError('empty-store uninstall changed the original core')
        row['stage'] = 'capturing-hosts'; self.save(row); source.capture_hosts()
        row['stage'] = 'preparing-qualified-disks'; self.save(row)
        allowlist = source.BASE/'tests/fixtures/longhorn-v1-qualified-disks.txt'
        if digest(allowlist) != row['allowlist_sha256']: raise ValueError('qualified source disk input changed')
        source.prepare_disks(allowlist)
        row['stage'] = 'preparing-credentials'; self.save(row); source.prepare_credentials()
        row['stage'] = 'installing-old'; self.save(row); impl.configure_old(source, pid)
        healthy(source, 'v1', impl.upgrade.OLD)
        if target_identity(self.target) != row['target_identity']: raise ValueError('old setup changed the independent target')
        row.update(stage='old-ready', source_namespace_uid=source.owner()['namespace_uid']); self.save(row)
        print('LONGHORN_V1_UPGRADE_TRANSITION_OLD_READY', flush=True)

    def initial_failure(self):
        """Prove runtime discovery failed before any business side effect."""
        row = self.read(); source, target = self.source, self.target
        if (row['stage'] != 'old-ready' or tuple(source.NODES) != impl.SOURCE_NODES or
                tuple(target.NODES) != ('192.168.47.149',) or
                source.owner()['cluster_uid'] == target.owner()['cluster_uid'] or
                source.owner()['namespace_uid'] != row['source_namespace_uid']):
            raise ValueError('not the original old-ready upgrade boundary')
        business = source.DIRECTORY/'longhorn-v1-upgrade-lifecycle.json'
        digest(business); content = business.read_bytes(); receipt = json.loads(content)
        if set(receipt) != {'id', 'source_identity', 'target_identity', 'payloads', 'points', 'stage'}:
            if set(receipt) not in (impl.V1Upgrade.PROTECTION_FIELDS, impl.V1Upgrade.MANAGER_FIELDS):
                raise ValueError('upgrade already started business or its initial identities changed')
            shared = impl.upgrade.load('longhorn-v2-dr-store.py')
            scenario = impl.V1Upgrade(source, target, self.backups, shared, 0, 0)
            scenario.resume_preflight(allow_expired_clients=True)
            return None, None, None
        if (set(receipt) != {'id', 'source_identity', 'target_identity', 'payloads', 'points', 'stage'} or
                not isinstance(receipt['id'], str) or not re.fullmatch('[0-9a-f]{32}', receipt['id']) or
                receipt['stage'] != 'source-1.12.0' or receipt['payloads'] != {} or receipt['points'] != {} or
                receipt['source_identity'] != impl.upgrade.retained_identity(source, {}) or
                receipt['target_identity'] != impl.upgrade.retained_identity(target, {})):
            raise ValueError('upgrade already started business or its initial identities changed')
        archived = business.with_name('longhorn-v1-upgrade-before-business-'+receipt['id']+'.json')
        if archived.exists() or archived.is_symlink(): raise ValueError('initial upgrade archive already exists')
        for lab, engine, version in ((source, 'v1', impl.upgrade.OLD), (target, 'v2', impl.upgrade.NEW)):
            healthy(lab, engine, version)
            empty(lab, self.backups, initial_receipt=(lab is source))
        impl.upgrade.runtime_identity(source, impl.upgrade.OLD)
        return business, content, archived

    def finalize(self):
        row = self.read()
        if row['stage'] not in ('old-ready', 'complete'): raise ValueError('incomplete V1 transition retained; no automatic rollback')
        for lab, engine in ((self.source, 'v1'), (self.target, 'v2')):
            healthy(lab, engine, impl.upgrade.NEW); empty(lab, self.backups)
        results = []
        for path in self.source.DIRECTORY.glob('longhorn-upgrade-result-*.json'):
            digest(path); result = json.loads(path.read_text())
            if (result.get('stage') == 'complete' and result.get('source_identity', {}).get('namespace_uid') == row['source_namespace_uid'] and
                    result.get('source_identity', {}).get('cluster_uid') == row['source_core']['cluster_uid'] and
                    result.get('target_identity', {}).get('cluster_uid') == row['target_core']['cluster_uid']): results.append(result)
        if not results: raise ValueError('V1 upgrade lacks its completed isolated-recovery result')
        row['stage'] = 'complete'; self.save(row)
        archived = self.path.with_name('longhorn-v1-upgrade-preparation-complete-'+results[-1]['id']+'.json')
        if archived.exists() or archived.is_symlink(): raise ValueError('V1 transition archive already exists')
        self.path.rename(archived)
        print('LONGHORN_V1_UPGRADE_TRANSITION_SCOPED_CLEAN_PASS original_core=true target_raw_identity=unchanged', flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=('preflight', 'prepare', 'finalize'))
    parser.add_argument('--source-relay-pid', type=int, default=0)
    args = parser.parse_args()
    source, target, backups, _ = impl.fixture(); source.require_lease(); target.require_lease()
    scenario = Preparation(source, target, backups)
    if args.mode == 'prepare': scenario.prepare(args.source_relay_pid)
    elif args.mode == 'preflight': scenario.preflight()
    else: scenario.finalize()


if __name__ == '__main__':
    try: main()
    except Exception:
        import traceback
        traceback.print_exc(); sys.exit(1)
