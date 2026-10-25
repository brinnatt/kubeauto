#!/usr/bin/env python3
"""V2 external-backup gates on the original owned three-node AIO baseline."""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import sys


BASE = Path('/usr/local/kubeauto')


def full_material():
    from importlib.util import module_from_spec, spec_from_file_location
    spec = spec_from_file_location('longhorn_full_material', Path(__file__).with_name('longhorn-full-material.py'))
    module = module_from_spec(spec); spec.loader.exec_module(module)
    return module


def evidence_path(historical, *, optional=False):
    return full_material().path(BASE, historical, optional=optional)


def fixture():
    spec = importlib.util.spec_from_file_location('v2_backup_install',
        BASE/'tests/helpers/longhorn-v2-install.py')
    install = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(install)
    _, lab = install.fixture()
    backups = lab.load_helper('longhorn-backups.py')
    # Every nested verification and recovery uses this same pool configuration.
    lab.backup_store = lambda: backups.Store(lab, endpoints=backups.V2_ENDPOINTS)
    return install, lab, backups


def baseline(install, lab, backups):
    install.volume_baseline(lab)
    store = lab.backup_store()
    target = lab.get('backuptargets.longhorn.io', 'default', 'longhorn-system')
    records = ('longhorn-backup-store.json', 'longhorn-backup-store',
               'longhorn-backup-refusal.json', 'longhorn-recurring-credentials.json',
               'longhorn-recurring-runtime.json')
    if (any((lab.DIRECTORY/name).exists() or (lab.DIRECTORY/name).is_symlink() for name in records) or
            not target or target['spec'].get('backupTargetURL') or target['spec'].get('credentialSecret') or
            any(lab.get(kind, namespace='longhorn-system')['items'] for kind in backups.BACKUP_RESOURCES) or
            any(obj['metadata'].get('labels', {}).get('kubeauto.io/longhorn-backup-run')
                for kind in ('pods', 'secrets') for obj in lab.get(kind, namespace='longhorn-system')['items']) or
            store.transport_listener() is not None):
        raise ValueError('V2 backup baseline retains consumers, credentials or fixture state')
    identity = {'uid': target['metadata']['uid'], 'configuration': backups.target_configuration(target['spec'])}
    digest = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
    print('LONGHORN_V2_BACKUP_BASELINE_VERIFIED target_sha256=' + digest + ' backup_objects=0 fixture_objects=0', flush=True)


def cleanup(lab):
    # Keep the store reachable until business recovery removes its consumers.
    lab.cleanup_business()
    lab.backup_store().cleanup()


def qualification_current(lab):
    evidence = json.loads((evidence_path('tests/evidence/longhorn-v2-backup-store-qualification-20261020.json')).read_text())
    if evidence['status'] != 'pass' or evidence['rc'] or evidence['failure_markers']:
        raise ValueError('V2 backup requires its completed external-store qualification')
    for name in ('tests/helpers/longhorn-backups.py', 'tests/helpers/longhorn-v2-backups.py',
                 'tests/helpers/longhorn-v2-backups.sh', 'tests/helpers/longhorn-v2-install.py'):
        if evidence['frozen_inputs'].get(name) != hashlib.sha256((BASE/name).read_bytes()).hexdigest():
            raise ValueError('V2 backup store qualification input changed: ' + name)
    state = lab.owner()
    if any(state.get(key) != value for key, value in evidence['baseline_identity'].items()):
        raise ValueError('V2 backup qualification cluster identity changed')
    print('LONGHORN_V2_BACKUP_STORE_QUALIFICATION_CURRENT', flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=('baseline', 'qualify', 'business', 'refusal', 'cleanup', 'qualification-current'))
    parser.add_argument('--relay-pid', type=int, default=0)
    args = parser.parse_args()
    install, lab, backups = fixture()
    lab.require_lease()
    if args.mode == 'baseline':
        baseline(install, lab, backups)
    elif args.mode == 'qualify':
        lab.qualify_backup_store()
    elif args.mode in ('business', 'refusal'):
        qualification_current(lab)
        if args.mode == 'refusal':
            lab.backup_suite(args.relay_pid, 'backup_refusal')
        else:
            lab.backup_suite(args.relay_pid)
    elif args.mode == 'qualification-current':
        qualification_current(lab)
    else:
        cleanup(lab)


if __name__ == '__main__':
    try:
        main()
    except Exception:
        # Backend exceptions may contain secrets. Preserve privately, never echo.
        import tempfile
        import traceback
        with tempfile.NamedTemporaryFile('w', prefix='longhorn-v2-backup-failure-',
                suffix='.txt', dir='/var/tmp', delete=False) as diagnostic:
            traceback.print_exc(file=diagnostic)
            print('LONGHORN_V2_BACKUP_FAIL private_diagnostic=' + diagnostic.name, file=sys.stderr)
        sys.exit(1)
