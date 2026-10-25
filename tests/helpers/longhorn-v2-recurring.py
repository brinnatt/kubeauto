#!/usr/bin/env python3
"""V2 recurring schedules through the product, on the qualified AIO baseline."""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import re
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
    spec = importlib.util.spec_from_file_location('v2_recurring_backup',
        BASE/'tests/helpers/longhorn-v2-backups.py')
    backup = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(backup)
    install, lab, store = backup.fixture()
    return backup, install, lab, store


def baseline(backup, install, lab, store):
    backup.baseline(install, lab, store)
    configuration = lab.yaml.safe_load((lab.DIRECTORY/'config.yml').read_text())
    declarations = lab.recurring_identity()
    detached = lab.get('settings.longhorn.io', 'allow-recurring-job-while-volume-detached', 'longhorn-system')
    if (configuration['longhorn_recurring_jobs'] or not detached or detached.get('value') != 'false' or
            any(row['labels'].get('app.kubernetes.io/managed-by') == 'kubeauto' for row in declarations.values()) or
            any((lab.DIRECTORY/name).exists() or (lab.DIRECTORY/name).is_symlink() for name in
                ('longhorn-recurring-qualification.json', 'longhorn-recurring-runtime.json',
                 'longhorn-recurring-credentials.json'))):
        raise ValueError('V2 recurring baseline retains schedules, credentials or detached-job changes')
    crons = {obj['metadata']['name']: {'uid': obj['metadata']['uid'], 'spec': obj['spec']}
             for obj in lab.get('cronjobs', namespace='longhorn-system')['items']}
    identity = {'declarations': declarations, 'crons': crons,
                'detached': {'uid': detached['metadata']['uid'], 'value': detached['value']}}
    digest = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
    print('LONGHORN_V2_RECURRING_BASELINE_VERIFIED sha256=' + digest, flush=True)


def cleanup(lab, relay_pid):
    # Recover selected credentials and stop schedules while both transports
    # remain reachable. A failed ownership check retains the store/journals.
    lab.cleanup_business(relay_pid=relay_pid)
    lab.cleanup_recurring_qualification(relay_pid)
    lab.backup_store().cleanup()


def selector_current(lab):
    evidence = json.loads((evidence_path('tests/evidence/longhorn-v2-recurring-selector-qualification-20261020.json')).read_text())
    if evidence['status'] != 'pass' or evidence['rc'] or evidence['failure_markers']:
        raise ValueError('V2 recurring work requires its completed selector qualification')
    for name in ('longhorn-v2-recurring.py', 'longhorn-v2-recurring.sh', 'longhorn-recurring.py'):
        path = 'tests/helpers/' + name
        if evidence['frozen_inputs'].get(path) != hashlib.sha256((BASE/path).read_bytes()).hexdigest():
            raise ValueError('V2 recurring selector qualification input changed: ' + path)
    state = lab.owner()
    if any(state.get(key) != value for key, value in evidence['baseline_identity'].items()):
        raise ValueError('V2 recurring selector qualification cluster identity changed')
    print('LONGHORN_V2_RECURRING_SELECTOR_QUALIFICATION_CURRENT', flush=True)


def verify(stage, log):
    """Reject partial business results even if the child exits successfully."""
    def exact(marker):
        if len(re.findall('^' + re.escape(marker) + '$', log, re.M)) != 1:
            raise ValueError('missing or duplicate recurring result: ' + marker)
    def result(prefix):
        rows = re.findall('^' + prefix + ' (.+)$', log, re.M)
        if len(rows) != 1:
            raise ValueError('missing or duplicate recurring observation')
        return json.loads(rows[0])
    if stage not in ('selector', 'snapshot', 'backup', 'rotation'):
        raise ValueError('unknown recurring result stage')
    prefix = 'LONGHORN_RECURRING_' + stage.upper()
    exact(prefix + '_BUSINESS_PASS')
    exact(prefix + '_CLEAN_PASS')
    exact('LONGHORN_BUSINESS_FIXTURES_CLEAN_PASS')
    stopped = 'BACKUP' if stage in ('backup', 'rotation') else 'SNAPSHOT'
    exact('LONGHORN_RECURRING_' + stopped + '_SCHEDULES_STOPPED_PASS')
    remounts = re.findall(r'^LONGHORN_FSYNC_REMOUNT_VERIFIED bytes=(\d+) sha256=([a-f0-9]{64}) replica_nodes=(\S+)$', log, re.M)
    if len(remounts) != 1 or int(remounts[0][0]) <= 0 or len(set(remounts[0][2].split(','))) != 3:
        raise ValueError('recurring result lacks cross-node data and three replicas')
    if stage == 'selector':
        if len(re.findall(r'^LONGHORN_RECURRING_SELECTOR_OBSERVED source_uid=\S+ schedule_uid=\S+$', log, re.M)) != 1:
            raise ValueError('recurring selector identities missing')
        return
    if stage in ('snapshot', 'backup'):
        row = result('LONGHORN_RECURRING_' + stage.upper() + '_OBSERVED')
        kind = 'snapshots' if stage == 'snapshot' else 'backups'
        if row['completed_jobs'] < 3 or row['observed_' + kind] < 3 or row['retained_' + kind] != 2 or row['detached_skipped_jobs'] < 2:
            raise ValueError('recurring retention or detached evidence incomplete')
        if not re.fullmatch('[a-f0-9]{64}', row['source_sha256']) or not re.fullmatch('[a-f0-9]{64}', row['canary_sha256']):
            raise ValueError('recurring source or unselected canary digest missing')
        if stage == 'snapshot':
            if row['source_sha256'] != remounts[0][1] or not row['cron_uid'] or not row['pod_uids']:
                raise ValueError('recurring snapshot data or native ownership missing')
            return
        receipts = row['receipts']
        if len(receipts) != 2 or row['restore_backup'] not in receipts:
            raise ValueError('scheduled backup retention receipts incomplete')
    else:
        exact('LONGHORN_RECURRING_CREDENTIALS_CLEAN_PASS')
        row = result('LONGHORN_RECURRING_CREDENTIAL_ROTATION_OBSERVED')
        if (set(row['stages']) != {'first', 'second', 'recovered', 'restore'} or
                len({row['stages'][phase]['uid'] for phase in ('first', 'second', 'recovered')}) != 3 or
                row['receipt']['backup'] != row['stages']['restore']['backup'] or
                row['native_error'] != 'InvalidAccessKeyId' or
                row['revocation'].get('native_error') != 'InvalidAccessKeyId' or
                row['revocation'].get('path') not in ('admission', 'backup') or
                row['failed_metadata_absent'] != len(row['failed_backups'])):
            raise ValueError('credential rotation lacks native failure/recovery boundaries')
        revoked = row['revocation']
        if revoked['path'] == 'backup' and not row['failed_backups']:
            raise ValueError('admitted credential failure lacks its native Backup identity')
        if revoked['path'] == 'admission' and (
                not all(revoked.get(key) for key in ('job_uid', 'pod_uid', 'snapshot_uid', 'target_uid', 'secret_uid')) or
                not re.fullmatch('[a-f0-9]{64}', revoked.get('log_sha256', ''))):
            raise ValueError('credential admission refusal lacks its native ownership chain')
        receipts = {'recovered': row['receipt']}
    for receipt in receipts.values():
        if (receipt['size'] != 2*1024**3 or not receipt['objects'] or
                not all(o['size'] > 0 and re.fullmatch('[a-f0-9]{64}', o['sha256']) for o in receipt['objects'].values())):
            raise ValueError('scheduled backup object readback incomplete')
    boundaries = re.findall(r'^LONGHORN_BACKUP_RESTORE_BOUNDARY_VERIFIED mode=(\S+) backup_sha256=([a-f0-9]{64}) source_sha256=([a-f0-9]{64}) independent_volume=(\S+)$', log, re.M)
    if (len(boundaries) != 1 or boundaries[0][0] != ('recurring' if stage == 'backup' else 'rotation') or
            boundaries[0][1] != row['source_sha256'] or
            boundaries[0][1] == boundaries[0][2] or boundaries[0][2] != remounts[0][1] or
            any(boundaries[0][3] == r['volume'] for r in receipts.values())):
        raise ValueError('scheduled backup restore lacks independent source boundary')
    for marker in ('LONGHORN_BACKUP_STORE_QUALIFIED clients=3 tls=verified object_readback=exact',
                   'LONGHORN_BACKUP_PRODUCT_FIXTURE_RESTORED', 'LONGHORN_BACKUP_TRANSPORT_CLEAN_PASS',
                   'LONGHORN_BACKUP_STORE_CLEAN_PASS'):
        exact(marker)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=('verify', 'baseline', 'cleanup', 'selector', 'snapshot', 'backup', 'rotation'))
    parser.add_argument('--relay-pid', type=int, default=0)
    parser.add_argument('--stage', choices=('selector', 'snapshot', 'backup', 'rotation'))
    parser.add_argument('--log', type=Path)
    args = parser.parse_args()
    if args.mode == 'verify':
        verify(args.stage, args.log.read_text())
        return
    backup, install, lab, store = fixture()
    lab.require_lease()
    if args.mode == 'baseline':
        baseline(backup, install, lab, store)
    elif args.mode == 'cleanup':
        cleanup(lab, args.relay_pid)
    else:
        backup.qualification_current(lab)
        if args.mode != 'selector':
            selector_current(lab)
        scenario = 'recurring_' + args.mode
        if args.mode in ('backup', 'rotation'):
            lab.backup_suite(args.relay_pid, scenario)
        else:
            lab.business(scenario, args.relay_pid)


if __name__ == '__main__':
    try:
        main()
    except Exception:
        import tempfile
        import traceback
        with tempfile.NamedTemporaryFile('w', prefix='longhorn-v2-recurring-failure-',
                suffix='.txt', dir='/var/tmp', delete=False) as diagnostic:
            traceback.print_exc(file=diagnostic)
            print('LONGHORN_V2_RECURRING_FAIL private_diagnostic=' + diagnostic.name, file=sys.stderr)
        sys.exit(1)
