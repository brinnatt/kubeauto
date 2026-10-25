#!/usr/bin/env python3
"""Owned empty-store V2 transition; preserve raw devices, never downgrade data."""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import sys

import yaml

SPEC = importlib.util.spec_from_file_location('v2_upgrade_prepare_impl', Path(__file__).with_name('longhorn-upgrade-lifecycle.py'))
impl = importlib.util.module_from_spec(SPEC); SPEC.loader.exec_module(impl)
BASELINE_SPEC = importlib.util.spec_from_file_location('v2_upgrade_baseline_impl', Path(__file__).with_name('longhorn-v2-upgrade-baseline.py'))
baseline = importlib.util.module_from_spec(BASELINE_SPEC); BASELINE_SPEC.loader.exec_module(baseline)
RECORD = 'longhorn-v2-upgrade-preparation.json'
SOURCE_NODES = ('192.168.47.146', '192.168.47.147', '192.168.47.148')
JOURNALS = ('longhorn-install-business.json', 'longhorn-backup-store.json',
    'longhorn-dr-backup-consumer.json', 'longhorn-upgrade-lifecycle.json', 'longhorn-request-input.json')


def digest(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file(): raise ValueError('missing regular V2 transition input: '+str(path))
    return hashlib.sha256(path.read_bytes()).hexdigest()


def core(lab):
    lab.core_identity(); state = lab.owner()
    return {key: state[key] for key in ('run_id', 'cluster_uid', 'node_uids', 'raw_disks')}


def raw_identity(lab):
    path = lab.DIRECTORY/'yml/longhorn/block-disks.json'; digest(path)
    receipt = json.loads(path.read_text()); state = lab.owner()
    if receipt.get('cluster_uid') != state['cluster_uid'] or receipt.get('namespace_uid') != state.get('namespace_uid'):
        raise ValueError('V2 transition raw receipt belongs to another installation')
    if set(receipt.get('nodes', {})) != set(lab.NODES): raise ValueError('V2 transition raw disk set changed')
    uuids = []
    for address, node in receipt['nodes'].items():
        disks = node.get('disks', {})
        expected = state['raw_disks'][address]
        if (len(disks) != 1 or any(not disk.get('disk_uuid') or disk.get('disk_driver') != 'aio' or
                disk.get('path') != expected['path'] or disk.get('hardware_identity') != expected['identity']
                for disk in disks.values())):
            raise ValueError('V2 transition raw receipt differs from its stable hardware allowlist')
        uuids.extend(disk['disk_uuid'] for disk in disks.values())
    if len(set(uuids)) != len(uuids): raise ValueError('V2 transition raw UUID is duplicated')
    return {address: {name: {key: disk[key] for key in ('disk_uuid', 'path', 'disk_driver', 'hardware_identity')}
        for name, disk in node['disks'].items()} for address, node in receipt['nodes'].items()}


def empty(lab, backups):
    if any((lab.DIRECTORY/name).exists() or (lab.DIRECTORY/name).is_symlink() for name in JOURNALS):
        raise ValueError('V2 transition retains another business journal')
    if (any(lab.get(kind, namespace=impl.NS)['items'] for kind in ('volumes.longhorn.io', *backups.BACKUP_RESOURCES)) or
            any(p.get('spec', {}).get('csi', {}).get('driver') == 'driver.longhorn.io' for p in lab.get('pv')['items']) or
            any(n['metadata']['name'].startswith('longhorn-business-') for n in lab.get('namespaces')['items'])):
        raise ValueError('V2 transition requires an empty storage installation')
    target = lab.get('backuptargets.longhorn.io', 'default', impl.NS)
    if not target or target['spec'].get('backupTargetURL') or target['spec'].get('credentialSecret'):
        raise ValueError('V2 transition retains an external backup consumer')


def healthy(lab, version):
    lab.core_identity(); lab.installation_health()
    release = impl.release(lab); config = yaml.safe_load((lab.DIRECTORY/'config.yml').read_text())
    if (release['app_version'].removeprefix('v') != version or release.get('status') != 'deployed' or
            config.get('longhorn_data_engines') != ['v2'] or str(config.get('longhorn_version')) != version or
            config.get('longhorn_action', 'present') != 'present' or config.get('longhorn_requests')):
        raise ValueError('V2 transition requires its actual pinned idle engine/version')


def target_identity(target):
    return {'retained': impl.retained_identity(target, {}), 'config_sha256': digest(target.DIRECTORY/'config.yml'),
            'raw_receipt_sha256': digest(target.DIRECTORY/'yml/longhorn/block-disks.json')}


class Preparation:
    def __init__(self, source, target, backups, install):
        self.source, self.target, self.backups, self.install = source, target, backups, install
        self.path = source.DIRECTORY/RECORD
        self.baseline = baseline

    def save(self, row): self.source.write_private(self.path, row)

    def read(self):
        digest(self.path); row = json.loads(self.path.read_text())
        if core(self.source) != row['source_core'] or core(self.target) != row['target_core']:
            raise ValueError('V2 transition core or raw hardware ownership changed')
        if target_identity(self.target) != row['target_identity']:
            raise ValueError('V2 transition changed the independent target')
        return row

    def preflight(self):
        source, target = self.source, self.target
        if self.path.exists() or self.path.is_symlink(): raise ValueError('recover the existing V2 transition stage first')
        if tuple(source.NODES) != SOURCE_NODES or tuple(target.NODES) != ('192.168.47.149',):
            raise ValueError('V2 transition requires its exact independently leased inventories')
        if source.owner()['cluster_uid'] == target.owner()['cluster_uid']:
            raise ValueError('V2 transition target must be independent')
        self.install.volume_baseline(source)
        for lab in (source, target): healthy(lab, impl.NEW); empty(lab, self.backups)
        source.cache_artifacts()
        config_path = source.DIRECTORY/'config.yml'; digest(config_path)
        before = self.baseline.capture(source)
        previous = self.baseline.bind_previous(before)
        row = {'source_core': core(source), 'target_core': core(target), 'target_identity': target_identity(target),
               'source_raw': raw_identity(source), 'original_config': config_path.read_text(), 'stage': 'preflight-complete',
               'baseline_before': before, 'previous_evidence': previous}
        self.save(row)
        print('LONGHORN_V2_UPGRADE_TRANSITION_PREFLIGHT_PASS', flush=True)

    def prepare(self, pid):
        row = self.read(); source = self.source
        if pid <= 0: raise ValueError('invalid V2 transition supervisor')
        if row['stage'] == 'preflight-complete':
            healthy(source, impl.NEW); empty(source, self.backups)
            if (source.DIRECTORY/'config.yml').read_text() != row['original_config'] or raw_identity(source) != row['source_raw']:
                raise ValueError('V2 transition source changed since its preflight')
            row['stage'] = 'retiring-empty-new'; self.save(row)
        else:
            self.resume_preflight()
            if row['stage'] == 'old-ready':
                if (source.DIRECTORY/'longhorn-upgrade-lifecycle.json').exists():
                    raise ValueError('completed rollout requires the post-upgrade continuation path')
                print('LONGHORN_V2_UPGRADE_TRANSITION_OLD_READY', flush=True)
                return
        original_restore = source.restore_hosts
        def restore_retired_hosts():
            self.recover_retired_endpoint(row)
            original_restore()
        source.restore_hosts = restore_retired_hosts
        try:
            source.recover_installation(pid)
        finally:
            source.restore_hosts = original_restore
        if core(source) != row['source_core']: raise ValueError('empty-store uninstall changed core or raw hardware')
        row['stage'] = 'capturing-hosts'; self.save(row); self.install.capture_hosts(source)
        row['stage'] = 'quiescing-multipath'; self.save(row); self.install.quiesce_multipath(source)
        row['stage'] = 'preparing-credentials'; self.save(row); source.prepare_credentials()
        row['stage'] = 'installing-old'; self.save(row); impl.configure_old(source, pid)
        healthy(source, impl.OLD)
        if raw_identity(source) != row['source_raw']:
            raise ValueError('V2 old installation changed original raw lvstore identity')
        if target_identity(self.target) != row['target_identity']: raise ValueError('old setup changed the independent target')
        row.update(stage='old-ready', source_namespace_uid=source.owner()['namespace_uid']); self.save(row)
        print('LONGHORN_V2_UPGRADE_TRANSITION_OLD_READY', flush=True)

    def resume_preflight(self):
        """Only resume the original empty uninstall after native disk release."""
        row = self.read(); source = self.source
        if row['stage'] == 'old-ready':
            if (source.DIRECTORY/'longhorn-upgrade-lifecycle.json').exists():
                scenario = impl.Upgrade(source, self.target, self.backups,
                    impl.load('longhorn-v2-dr-store.py'), 0, 0)
                scenario.resume_preflight()
                print('LONGHORN_V2_UPGRADE_RETIREMENT_RESUME_PREFLIGHT_PASS', flush=True)
                return row
            healthy(source, impl.OLD); healthy(self.target, impl.NEW)
            empty(source, self.backups); empty(self.target, self.backups)
            expected = yaml.safe_load(row['original_config'])
            expected.update(longhorn_version=impl.OLD, longhorn_upgrade_id='', longhorn_upgrade_system_backup='')
            if (yaml.safe_load((source.DIRECTORY/'config.yml').read_text()) != expected or
                    source.owner()['namespace_uid'] != row['source_namespace_uid'] or
                    raw_identity(source) != row['source_raw']):
                raise ValueError('old-ready reuse changed original configuration, namespace or raw identity')
            print('LONGHORN_V2_UPGRADE_RETIREMENT_RESUME_PREFLIGHT_PASS', flush=True)
            return row
        if row['stage'] != 'retiring-empty-new':
            raise ValueError('V2 transition stage has no qualified automatic recovery')
        healthy(self.target, impl.NEW); empty(self.target, self.backups)
        if (source.DIRECTORY/'config.yml').read_text() != row['original_config']:
            raise ValueError('V2 transition retirement configuration changed')
        state = source.owner(); identity = row['baseline_before']['baseline_identity']
        if state.get('namespace_uid') != identity['namespace_uid']:
            raise ValueError('V2 transition retirement namespace changed')
        receipt = source.DIRECTORY/'yml/longhorn'/('block-disks-uninstalled-'+identity['namespace_uid']+'.json')
        digest(receipt)
        if (json.loads(receipt.read_text()) != row['baseline_before']['native_block_receipt'] or
                (source.DIRECTORY/'yml/longhorn/block-disks.json').exists() or
                (source.DIRECTORY/'yml/longhorn/block-disks.json').is_symlink() or
                any((source.DIRECTORY/name).exists() or (source.DIRECTORY/name).is_symlink() for name in JOURNALS)):
            raise ValueError('V2 transition retirement lacks its original released empty-store receipts')
        source.execute([sys.executable, source.BASE/'roles/longhorn/files/longhorn.py', 'guard',
                        '--kubeconfig', source.DIRECTORY/'kubectl.kubeconfig'])
        # recover_installation rechecks original namespace, credentials, shared
        # APIs, native leases and the complete graph before any fixture deletion.
        print('LONGHORN_V2_UPGRADE_RETIREMENT_RESUME_PREFLIGHT_PASS', flush=True)
        return row

    def recover_retired_endpoint(self, row):
        """Recover only the original completed fault scope during empty retirement."""
        ref = row['previous_evidence'].get('runtime_continuation')
        if not ref: return  # Fresh full-chain runs own and check their cleanup.
        source = self.source
        runtime = json.loads(baseline.referenced(ref).read_text())
        if runtime.get('baseline') != row['baseline_before']:
            raise ValueError('retired endpoint recovery changed original runtime boundary')
        failure = json.loads(baseline.referenced(runtime['failure_scopes']['all-replicas']).read_text())
        if (failure.get('signed_scope') != 'v2-failure/all-replicas' or failure.get('status') != 'pass' or
                failure.get('durable') != {'exit': 0, 'finalized': 0}):
            raise ValueError('retired endpoint lacks completed original owning fault scope')
        refs = failure['raw_evidence']
        logs = [name for name in refs if name.endswith('/all-replicas.log')]
        if len(logs) != 1: raise ValueError('retired endpoint lacks its unique original fault log')
        text = baseline.referenced({'path': logs[0], 'sha256': refs[logs[0]]}).read_text()
        observation = failure['observation']
        names = {name.rsplit('-r-', 1)[0] for name in observation['refusal']['replicas']}
        if len(names) != 1: raise ValueError('retired endpoint original replicas disagree')
        volume = names.pop(); uid = observation['source_volume_uid']
        lines = text.splitlines()
        marker = 'LONGHORN_V2_ALL_REPLICAS_RECOVERY '
        if ([json.loads(line[len(marker):]) for line in lines if line.startswith(marker)] != [observation] or
                lines.count('LONGHORN_DELETE_POLICY_RECLAIM_VERIFIED volume='+volume) != 1 or
                observation['source_fenced'].get('volume_uid') != uid or
                not observation['source_fenced'].get('writer_absent') or
                observation['source_fenced'].get('volume_state') != 'detached'):
            raise ValueError('retired endpoint lacks original source fencing and CSI reclamation')
        if (source.get('pods', namespace=impl.NS)['items'] or
                source.get('crd', 'volumes.longhorn.io') or
                any(p.get('spec', {}).get('csi', {}).get('driver') == 'driver.longhorn.io'
                    for p in source.get('pv')['items'])):
            raise ValueError('retired endpoint recovery requires the empty native-uninstalled store')
        source.cleanup_retired_v2_endpoint(volume, uid, 2*1024**3)

    def finalize(self):
        row = self.read()
        if row['stage'] != 'old-ready': raise ValueError('incomplete V2 transition retained; no automatic rollback')
        for lab in (self.source, self.target): healthy(lab, impl.NEW); empty(lab, self.backups)
        if raw_identity(self.source) != row['source_raw']: raise ValueError('V2 upgrade changed original raw lvstore identity')
        results = []
        for path in self.source.DIRECTORY.glob('longhorn-upgrade-result-*.json'):
            digest(path); result = json.loads(path.read_text())
            if (result.get('stage') == 'complete' and result.get('source_identity', {}).get('namespace_uid') == row['source_namespace_uid'] and
                    result.get('source_identity', {}).get('cluster_uid') == row['source_core']['cluster_uid'] and
                    result.get('target_identity', {}).get('cluster_uid') == row['target_core']['cluster_uid'] and
                    result.get('legacy', {}).get('refusal_verified') and result['legacy'].get('retired')): results.append(result)
        if len(results) != 1: raise ValueError('V2 transition lacks one completed upgrade and real legacy refusal result')
        # Only reversible fixture inputs return to their exact original values.
        # The completed product remains 1.12.1; failed rollouts never enter here.
        original = yaml.safe_load(row['original_config'])
        current = yaml.safe_load((self.source.DIRECTORY/'config.yml').read_text())
        for key in ('longhorn_upgrade_id', 'longhorn_upgrade_system_backup'):
            current[key] = original[key]
        if current != original: raise ValueError('V2 transition configuration changed outside its owned upgrade inputs')
        archive = self.path.with_name('longhorn-v2-upgrade-preparation-complete-'+results[0]['id']+'.json')
        if archive.exists() or archive.is_symlink(): raise ValueError('V2 transition archive already exists')
        impl.atomic_config(self.source, original)
        row['baseline_after'] = self.baseline.capture(self.source)
        row['stage'] = 'complete'; self.source.write_private(archive, row)
        transition = {'previous_evidence': row['previous_evidence'], 'before': row['baseline_before'], 'after': row['baseline_after'],
            'preparation': self.baseline.reference(archive),
            'upgrade_result': self.baseline.reference(self.source.DIRECTORY/('longhorn-upgrade-result-'+results[0]['id']+'.json'))}
        self.baseline.validate_transition(transition)
        self.path.unlink()
        print('LONGHORN_V2_UPGRADE_BASELINE_TRANSITION '+json.dumps(transition, sort_keys=True), flush=True)
        print('LONGHORN_V2_UPGRADE_TRANSITION_SCOPED_CLEAN_PASS original_core=true source_raw_identity=unchanged target_identity=unchanged', flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=('preflight', 'resume-preflight', 'check', 'prepare', 'finalize'))
    parser.add_argument('--source-relay-pid', type=int, default=0)
    args = parser.parse_args()
    shared = impl.load('longhorn-v2-dr-store.py')
    _, install, source, backups, _, _, target = shared.fixture()
    source.require_lease(); target.require_lease()
    scenario = Preparation(source, target, backups, install)
    if args.mode == 'prepare': scenario.prepare(args.source_relay_pid)
    elif args.mode == 'preflight': scenario.preflight()
    elif args.mode == 'resume-preflight': scenario.resume_preflight()
    elif args.mode == 'check': scenario.read()
    else: scenario.finalize()


if __name__ == '__main__':
    try: main()
    except Exception:
        import traceback
        traceback.print_exc(); sys.exit(1)
