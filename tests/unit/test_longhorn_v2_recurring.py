"""V2 recurring integration dispatch, complete results and scoped recovery."""
import copy
from contextlib import redirect_stdout
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import yaml

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location('v2_recurring_contract', ROOT/'tests/helpers/longhorn-v2-recurring.py')
V2 = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(V2)
STAGES = ('selector', 'snapshot', 'backup', 'rotation')


def observation(stage):
    source, latest = 'a'*64, 'b'*64
    receipt = {'volume': 'source-volume', 'backup': 'retained', 'size': 2*1024**3,
               'objects': {'config': {'size': 1, 'sha256': 'c'*64}}}
    lines = ['LONGHORN_RECURRING_' + stage.upper() + suffix for suffix in ('_BUSINESS_PASS', '_CLEAN_PASS')]
    lines += ['LONGHORN_BUSINESS_FIXTURES_CLEAN_PASS',
              'LONGHORN_RECURRING_' + ('BACKUP' if stage in ('backup', 'rotation') else 'SNAPSHOT') + '_SCHEDULES_STOPPED_PASS',
              'LONGHORN_FSYNC_REMOUNT_VERIFIED bytes=9437184 sha256=' + (source if stage in ('selector', 'snapshot') else latest) + ' replica_nodes=a,b,c']
    row = {'completed_jobs': 5, 'observed_snapshots': 3, 'observed_backups': 3,
           'retained_snapshots': 2, 'retained_backups': 2, 'detached_skipped_jobs': 2,
           'cron_uid': 'cron', 'pod_uids': ['pod'], 'source_sha256': source, 'canary_sha256': 'd'*64,
           'restore_backup': 'retained', 'receipts': {'retained': receipt, 'older': dict(receipt, backup='older')}}
    if stage == 'selector':
        lines += ['LONGHORN_RECURRING_SELECTOR_OBSERVED source_uid=volume schedule_uid=schedule']
    elif stage in ('snapshot', 'backup'):
        lines += ['LONGHORN_RECURRING_' + stage.upper() + '_OBSERVED ' + json.dumps(row)]
    else:
        row = {'stages': {name: {'uid': name, 'backup': 'retained' if name == 'restore' else name}
                         for name in ('first', 'second', 'recovered', 'restore')},
               'revocation': dict(path='admission', native_error='InvalidAccessKeyId', log_sha256='e'*64,
                                  **{key: key for key in ('job_uid', 'pod_uid', 'snapshot_uid', 'target_uid', 'secret_uid')}),
               'failed_backups': {}, 'failed_metadata_absent': 0, 'native_error': 'InvalidAccessKeyId',
               'source_sha256': source, 'receipt': receipt}
        lines += ['LONGHORN_RECURRING_CREDENTIALS_CLEAN_PASS',
                  'LONGHORN_RECURRING_CREDENTIAL_ROTATION_OBSERVED ' + json.dumps(row)]
    if stage in ('backup', 'rotation'):
        lines += ['LONGHORN_BACKUP_STORE_QUALIFIED clients=3 tls=verified object_readback=exact',
                  'LONGHORN_BACKUP_PRODUCT_FIXTURE_RESTORED', 'LONGHORN_BACKUP_TRANSPORT_CLEAN_PASS',
                  'LONGHORN_BACKUP_STORE_CLEAN_PASS',
                  'LONGHORN_BACKUP_RESTORE_BOUNDARY_VERIFIED mode=' + ('recurring' if stage == 'backup' else 'rotation') +
                  ' backup_sha256=' + source + ' source_sha256=' + latest + ' independent_volume=restored-volume']
    return '\n'.join(lines) + '\n'


class V2RecurringContracts(unittest.TestCase):
    def test_complete_results_accept_and_partial_ownership_retention_recovery_reject(self):
        for stage in STAGES:
            original = observation(stage)
            V2.verify(stage, original)
            if stage == 'rotation':
                admitted = original.replace('"path": "admission"', '"path": "backup"').replace(
                    '"failed_backups": {}, "failed_metadata_absent": 0',
                    '"failed_backups": {"failed-uid": "failed-name"}, "failed_metadata_absent": 1')
                V2.verify(stage, admitted)
            variants = [original.replace('_BUSINESS_PASS', '_INCOMPLETE'),
                        original.replace('_SCHEDULES_STOPPED_PASS', '_NOT_STOPPED'),
                        original.replace('replica_nodes=a,b,c', 'replica_nodes=a,a,c'),
                        original.replace('LONGHORN_BUSINESS_FIXTURES_CLEAN_PASS', ''),
                        original + original.splitlines()[0] + '\n']
            if stage == 'selector':
                variants += [original.replace('source_uid=volume', 'source_uid=')]
            if stage in ('snapshot', 'backup'):
                variants += [original.replace('"retained_' + ('snapshots' if stage == 'snapshot' else 'backups') + '": 2',
                                              '"retained_' + ('snapshots' if stage == 'snapshot' else 'backups') + '": 1'),
                             original.replace('"detached_skipped_jobs": 2', '"detached_skipped_jobs": 0')]
            if stage in ('backup', 'rotation'):
                variants += [original.replace('"size": 1,', '"size": 0,'),
                             original.replace('independent_volume=restored-volume', 'independent_volume=source-volume'),
                             original.replace('source_sha256=' + 'b'*64, 'source_sha256=' + 'a'*64),
                             original.replace('clients=3', 'clients=6'),
                             original.replace('mode=recurring' if stage == 'backup' else 'mode=rotation', 'mode=full')]
            if stage == 'rotation':
                variants += [original.replace('InvalidAccessKeyId', 'unrelated'),
                             original.replace('"path": "admission"', '"path": "job-success"'),
                             original.replace('"path": "admission"', '"path": "backup"'),
                             original.replace('"pod_uid": "pod_uid"', '"pod_uid": ""'),
                             original.replace('"failed_metadata_absent": 0', '"failed_metadata_absent": 1')]
            for text in variants:
                with self.subTest(stage=stage, text=text[-160:]), self.assertRaises(ValueError):
                    V2.verify(stage, text)

    def test_product_dispatch_requires_current_backup_qualification(self):
        for stage in STAGES:
            for rejected in (False, True):
                backup = SimpleNamespace(qualification_current=Mock(side_effect=ValueError('stale') if rejected else None))
                lab = SimpleNamespace(require_lease=Mock(), backup_suite=Mock(), business=Mock())
                with patch.object(V2, 'fixture', return_value=(backup, None, lab, None)), \
                        patch.object(V2, 'selector_current') as selected, \
                        patch.object(sys, 'argv', ['gate', stage, '--relay-pid', '123']):
                    if rejected:
                        with self.assertRaisesRegex(ValueError, 'stale'): V2.main()
                        lab.business.assert_not_called(); lab.backup_suite.assert_not_called()
                    else:
                        V2.main()
                        target = lab.backup_suite if stage in ('backup', 'rotation') else lab.business
                        if stage in ('backup', 'rotation'):
                            target.assert_called_once_with(123, 'recurring_' + stage)
                        else:
                            target.assert_called_once_with('recurring_' + stage, 123)
                        if stage == 'selector': selected.assert_not_called()
                        else: selected.assert_called_once_with(lab)
                backup.qualification_current.assert_called_once_with(lab)

    def test_selector_qualification_rejects_changed_inputs_identity_and_incomplete_results(self):
        for fault in (None, 'status', 'rc', 'failure_markers', 'source', 'cluster'):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as directory:
                base = Path(directory); (base/'tests/helpers').mkdir(parents=True); (base/'tests/evidence').mkdir()
                inputs = {}
                for name in ('longhorn-v2-recurring.py', 'longhorn-v2-recurring.sh', 'longhorn-recurring.py'):
                    path = 'tests/helpers/' + name
                    (base/path).write_text('source')
                    inputs[path] = hashlib.sha256(b'source').hexdigest()
                evidence = dict(status='pass', rc=0, failure_markers=0, frozen_inputs=inputs, baseline_identity={'cluster_uid': 'cluster'})
                if fault == 'status': evidence['status'] = 'pending'
                if fault in ('rc', 'failure_markers'): evidence[fault] = 1
                if fault == 'source': (base/'tests/helpers/longhorn-recurring.py').write_text('changed')
                (base/'tests/evidence/longhorn-v2-recurring-selector-qualification-20261020.json').write_text(json.dumps(evidence))
                lab = SimpleNamespace(owner=lambda: {'cluster_uid': 'changed' if fault == 'cluster' else 'cluster'})
                with patch.object(V2, 'BASE', base), redirect_stdout(io.StringIO()):
                    if fault:
                        with self.assertRaises(ValueError): V2.selector_current(lab)
                    else: V2.selector_current(lab)

    def test_stale_selector_blocks_all_timed_work_before_business(self):
        for stage in ('snapshot', 'backup', 'rotation'):
            backup = SimpleNamespace(qualification_current=Mock())
            lab = SimpleNamespace(require_lease=Mock(), backup_suite=Mock(), business=Mock())
            with patch.object(V2, 'fixture', return_value=(backup, None, lab, None)), \
                    patch.object(V2, 'selector_current', side_effect=ValueError('stale selector')), \
                    patch.object(sys, 'argv', ['gate', stage, '--relay-pid', '123']):
                with self.assertRaisesRegex(ValueError, 'stale selector'): V2.main()
            lab.business.assert_not_called(); lab.backup_suite.assert_not_called()

    def test_cleanup_retains_transports_until_schedule_credentials_and_consumers_recovered(self):
        for failure in (None, 'business', 'declarations'):
            calls = []
            def step(name):
                def action(*_args, **_kwargs):
                    calls.append(name)
                    if name == failure: raise ValueError(name)
                return action
            lab = SimpleNamespace(cleanup_business=Mock(side_effect=step('business')),
                                  cleanup_recurring_qualification=Mock(side_effect=step('declarations')),
                                  backup_store=lambda: SimpleNamespace(cleanup=step('store')))
            if failure:
                with self.assertRaises(ValueError): V2.cleanup(lab, 123)
                self.assertNotIn('store', calls)
            else:
                V2.cleanup(lab, 123)
                self.assertEqual(calls, ['business', 'declarations', 'store'])
            lab.cleanup_business.assert_called_once_with(relay_pid=123)

    def test_baseline_preserves_foreign_schedule_identity_and_rejects_owned_state(self):
        for fault in (None, 'configured', 'managed', 'detached', 'journal', 'symlink'):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as directory:
                path = Path(directory)
                (path/'config.yml').write_text(yaml.safe_dump({'longhorn_recurring_jobs': [{}] if fault == 'configured' else []}))
                if fault in ('journal', 'symlink'):
                    record = path/'longhorn-recurring-qualification.json'
                    record.symlink_to(path/'absent') if fault == 'symlink' else record.write_text('{}')
                declarations = {'foreign': {'uid': 'foreign-uid', 'labels': {}, 'spec': {'cron': '0 0 1 1 *'}}}
                if fault == 'managed': declarations['foreign']['labels']['app.kubernetes.io/managed-by'] = 'kubeauto'
                detached = {'metadata': {'uid': 'setting'}, 'value': 'true' if fault == 'detached' else 'false'}
                lab = SimpleNamespace(DIRECTORY=path, yaml=yaml, recurring_identity=lambda: copy.deepcopy(declarations),
                                      get=Mock(side_effect=lambda kind, *_args, **_kwargs: detached if kind == 'settings.longhorn.io' else {'items': []}))
                original = copy.deepcopy(declarations)
                with redirect_stdout(io.StringIO()) as output:
                    if fault:
                        with self.assertRaises(ValueError): V2.baseline(SimpleNamespace(baseline=Mock()), None, lab, None)
                    else:
                        V2.baseline(SimpleNamespace(baseline=Mock()), None, lab, None)
                        self.assertIn('LONGHORN_V2_RECURRING_BASELINE_VERIFIED sha256=', output.getvalue())
                self.assertEqual(declarations, original)

    def test_shell_preconditions_exit_state_and_cleanup_order(self):
        source = (ROOT/'tests/helpers/longhorn-v2-recurring.sh').read_text()
        for stage in STAGES:
            for fault in ('none', 'qualification-current', 'business', 'missing-stop', 'cleanup', 'baseline-changed',
                          'missing-backup-baseline', 'missing-recurring-baseline', 'missing-backup-final', 'missing-recurring-final'):
                with self.subTest(stage=stage, fault=fault), tempfile.TemporaryDirectory() as directory:
                    base = Path(directory); (base/'.venv/bin').mkdir(parents=True); (base/'tests/helpers').mkdir(parents=True)
                    (base/'.venv/bin/python').symlink_to(sys.executable); (base/'kube-bin').mkdir()
                    (base/'kube-bin/kubectl').write_text('#!/bin/sh\nexit 0\n'); (base/'kube-bin/kubectl').chmod(0o755)
                    log = observation(stage)
                    if fault == 'missing-stop': log = log.replace('_SCHEDULES_STOPPED_PASS', '_NOT_STOPPED')
                    (base/'observation.log').write_text(log)
                    helper = '''import importlib.util,os,signal,sys,time
from pathlib import Path
mode=sys.argv[1];fault=os.environ['FAULT']
if mode=='relay-maintain':
 signal.signal(signal.SIGTERM,lambda *_:sys.exit(0))
 while True:time.sleep(.01)
with open('calls.txt','a') as stream:stream.write(mode+'\\n')
if mode==fault or mode in ('selector','snapshot','backup','rotation') and fault=='business':sys.exit(17)
if mode=='baseline':
 count=Path('baseline-count');n=int(count.read_text())+1 if count.exists() else 1;count.write_text(str(n))
 if fault!='missing-backup-baseline' and not(n>1 and fault=='missing-backup-final'):print('LONGHORN_V2_BACKUP_BASELINE_VERIFIED target_sha256='+'f'*64+' backup_objects=0 fixture_objects=0')
 if fault!='missing-recurring-baseline' and not(n>1 and fault=='missing-recurring-final'):print('LONGHORN_V2_RECURRING_BASELINE_VERIFIED sha256='+('e' if n>1 and fault=='baseline-changed' else 'd')*64)
if mode in ('selector','snapshot','backup','rotation'):print(Path('observation.log').read_text())
if mode=='verify':
 spec=importlib.util.spec_from_file_location('real_verifier',os.environ['VERIFIER']);m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
 m.verify(sys.argv[sys.argv.index('--stage')+1],Path(sys.argv[sys.argv.index('--log')+1]).read_text())
'''
                    for name in ('core', 'install', 'backups', 'recurring'):
                        (base/f'tests/helpers/longhorn-v2-{name}.py').write_text(helper)
                    result = subprocess.run(['bash', '-c', source.replace('BASE=/usr/local/kubeauto', 'BASE='+str(base)),
                        'contract', 'v2-recurring-' + stage + '-qualification'], env=dict(os.environ,
                        FAULT=fault, VERIFIER=str(ROOT/'tests/helpers/longhorn-v2-recurring.py'), LONGHORN_PARENT_LEASE_HELD='yes'),
                        text=True, capture_output=True, timeout=15)
                    calls = (base/'calls.txt').read_text().splitlines()
                    self.assertEqual(result.returncode == 0, fault == 'none', result.stdout+result.stderr)
                    self.assertEqual('_SCOPED_CLEAN_PASS' in result.stdout, fault == 'none')
                    if fault == 'qualification-current': self.assertEqual(calls, ['verify-transport', 'qualification-current'])
                    elif fault in ('missing-backup-baseline', 'missing-recurring-baseline'):
                        self.assertEqual(calls, ['verify-transport', 'qualification-current', 'baseline'])
                    else:
                        self.assertLess(calls.index('cleanup'), calls.index('relay-clean'))
                        self.assertEqual(calls[-1], 'verify-core')

    def test_fixed_dispatch_and_matrix_owner(self):
        for stage in STAGES:
            mode = 'v2-recurring-' + stage + '-qualification'
            script = 'source "$1"\nlonghorn_launch() { printf "%s %s" "$1" "$2"; }\nlonghorn_dispatch --longhorn-' + mode
            result = subprocess.run(['bash', '-c', script, 'contract', str(ROOT/'tests/helpers/longhorn-runner.sh')],
                                    text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, mode + ' LONGHORN_' + mode.upper().replace('-', '_') + '_PASS')
            self.assertIn(mode, (ROOT/'tests/helpers/longhorn-live.sh').read_text())
        cases = yaml.safe_load((ROOT/'tests/longhorn-test-matrix.yaml').read_text())['cases']
        self.assertEqual([r['id'] for r in cases if 'recurring_qualification' in r], ['LONGHORN-48'])


if __name__ == '__main__':
    unittest.main()
