"""On-demand checksum ownership, asynchronous completion and honest evidence."""
import copy
from contextlib import redirect_stdout
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import yaml

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location('v2_integrity_contract', ROOT/'tests/helpers/longhorn-v2-integrity.py')
INTEGRITY = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(INTEGRITY)


class IntegrityContracts(unittest.TestCase):
    def test_dispatch_allowlists_and_matrix_bind_only_v2_data_protection(self):
        mode = 'v2-integrity-qualification'
        path = ROOT/'tests/helpers/longhorn-runner.sh'
        script = 'source "$1"\nlonghorn_launch() { printf "%s %s" "$1" "$2"; }\nlonghorn_dispatch --longhorn-'+mode+'\n'
        result = subprocess.run(['bash', '-c', script, 'contract', str(path)], text=True, capture_output=True, check=True)
        self.assertEqual(result.stdout, mode+' LONGHORN_V2_INTEGRITY_QUALIFICATION_PASS')
        for variable in ('module', 'mode'):
            branches = re.findall(r'case "\$'+variable+r'" in ([^\n]+)', path.read_text())
            self.assertEqual(len(branches), 1)
            self.assertIn(mode, branches[0].split(')')[0].split('|'))
        cases = yaml.safe_load((ROOT/'tests/longhorn-test-matrix.yaml').read_text())['cases']
        self.assertEqual([case['id'] for case in cases if 'integrity_qualification' in case], ['LONGHORN-48'])

    def fixture(self):
        journal = {'volume': 'source', 'volume_uid': 'source-uid',
                   'snapshots': {name: {'native': name} for name in ('native', 'csi')},
                   'native_snapshots': {name: {'uid': name+'-uid'} for name in ('native', 'csi')}}
        volume = {'metadata': {'uid': 'source-uid', 'resourceVersion': '17'},
                  'spec': {'dataEngine': 'v2', 'numberOfReplicas': 3, 'snapshotDataIntegrity': 'ignored'},
                  'status': {'state': 'attached', 'robustness': 'healthy'}}
        rows = {name: {'metadata': {'name': name, 'uid': name+'-uid', 'creationTimestamp': '2026-01-01T00:00:00Z'},
                       'spec': {'volume': 'source'}, 'status': {'userCreated': True, 'readyToUse': True}}
                for name in ('native', 'csi')}
        engine = {'spec': {'volumeName': 'source', 'dataEngine': 'v2'}, 'status': {}}
        config = {name: {'metadata': {'uid': name}, 'value': 'original'} for name in INTEGRITY.SETTINGS}
        state = SimpleNamespace(volume=volume, rows=rows, engine=engine, settings=config,
                                after_patch=lambda: None, observe=lambda: None)

        def get(kind, name='', namespace=''):
            if kind == 'settings.longhorn.io': result = state.settings[name]
            elif kind == 'snapshots.longhorn.io': result = {'items': list(state.rows.values())}
            elif kind == 'volumes.longhorn.io': result = state.volume
            elif kind == 'engines.longhorn.io': result = {'items': [state.engine]}
            else: raise AssertionError(kind)
            return copy.deepcopy(result)

        def kubectl(*args, stdin):
            changes = json.loads(stdin)
            self.assertEqual(changes[:3], [
                {'op': 'test', 'path': '/metadata/uid', 'value': 'source-uid'},
                {'op': 'test', 'path': '/metadata/resourceVersion', 'value': '17'},
                {'op': 'test', 'path': '/spec', 'value': state.volume['spec']}])
            state.volume['spec'] = changes[3]['value']
            state.after_patch()

        def wait(stage, timeout, check):
            self.assertEqual((stage, timeout), ('v2-on-demand-snapshot-checksum', 900))
            self.assertIsNone(check())
            requested = state.volume['spec']['snapshotHashingRequestedAt']
            state.volume['status']['lastOnDemandSnapshotHashingCompleteAt'] = requested
            for row in state.rows.values():
                row['status'].setdefault('checksum', 'opaque-native-checksum')
                row['status'].setdefault('checksumCalculatedAt', '2026-01-02T00:00:00Z')
            state.observe()
            result = check()
            if not result: raise TimeoutError(stage)
            return result
        lab = SimpleNamespace(DIRECTORY=Path('/private'), get=Mock(side_effect=get),
            replica_topology=Mock(return_value={'r1': {}, 'r2': {}, 'r3': {}}),
            healthy_replicas=Mock(return_value=['a', 'b', 'c']),
            write_private=Mock(), kubectl=Mock(side_effect=kubectl), wait=Mock(side_effect=wait))
        return lab, journal, state

    def test_native_uid_comes_from_existing_binding_and_prehashed_timestamp_need_not_advance(self):
        lab, journal, state = self.fixture()
        state.rows['native']['status'].update(checksum='previous', checksumCalculatedAt='2026-01-02T00:00:00Z')
        with redirect_stdout(io.StringIO()) as output:
            INTEGRITY.verify(lab, journal)
        row = json.loads(output.getvalue().split(' ', 1)[1])
        self.assertFalse(row['whole_data_rehash_proven'])
        self.assertTrue(row['global_settings_unchanged'])
        self.assertEqual(row['snapshots']['native']['calculated_at'], '2026-01-02T00:00:00Z')
        self.assertTrue(row['snapshots']['native']['previously_hashed'])
        self.assertFalse(row['snapshots']['csi']['previously_hashed'])
        self.assertEqual(lab.write_private.call_args.args[1]['integrity']['snapshots'],
                         {'native': 'native-uid', 'csi': 'csi-uid'})
        self.assertEqual(lab.kubectl.call_count, 2)
        self.assertEqual(state.volume['spec']['snapshotDataIntegrity'], 'ignored')
        self.assertTrue(row['original_policy_restored'])

    def test_unsafe_inputs_stop_before_any_journal_or_api_mutation(self):
        defects = ('uid', 'unbound', 'missing', 'foreign', 'not-ready', 'error', 'deleting',
                   'volume-uid', 'v1', 'replicas', 'detached', 'degraded', 'disabled', 'migration',
                   'outstanding', 'clock', 'future-snapshot', 'topology', 'expanding', 'rebuild', 'purge', 'restore')
        for defect in defects:
            with self.subTest(defect=defect):
                lab, journal, state = self.fixture()
                if defect == 'uid': state.rows['native']['metadata']['uid'] = 'replaced'
                if defect == 'unbound': journal['native_snapshots']['native'].clear()
                if defect == 'missing': state.rows.pop('native')
                if defect == 'foreign':
                    state.rows['foreign'] = copy.deepcopy(state.rows['native'])
                    state.rows['foreign']['metadata']['name'] = 'foreign'
                if defect == 'not-ready': state.rows['native']['status']['readyToUse'] = False
                if defect == 'error': state.rows['native']['status']['error'] = 'failed'
                if defect == 'deleting': state.rows['native']['metadata']['deletionTimestamp'] = 'now'
                if defect == 'volume-uid': state.volume['metadata']['uid'] = 'replaced'
                if defect == 'v1': state.volume['spec']['dataEngine'] = 'v1'
                if defect == 'replicas': state.volume['spec']['numberOfReplicas'] = 1
                if defect == 'detached': state.volume['status']['state'] = 'detached'
                if defect == 'degraded': state.volume['status']['robustness'] = 'degraded'
                if defect == 'disabled': state.volume['spec']['snapshotDataIntegrity'] = 'disabled'
                if defect == 'migration': state.volume['spec']['migrationNodeID'] = 'target'
                if defect == 'outstanding': state.volume['spec']['snapshotHashingRequestedAt'] = '2026-01-03T00:00:00Z'
                if defect == 'clock':
                    state.volume['spec']['snapshotHashingRequestedAt'] = '2099-01-03T00:00:00Z'
                    state.volume['status']['lastOnDemandSnapshotHashingCompleteAt'] = '2099-01-03T00:00:00Z'
                if defect == 'future-snapshot': state.rows['native']['metadata']['creationTimestamp'] = '2099-01-01T00:00:00Z'
                if defect == 'topology': lab.replica_topology.return_value = None
                if defect == 'expanding': state.engine['status']['isExpanding'] = True
                for field, flag, name in (('rebuildStatus', 'isRebuilding', 'rebuild'),
                                          ('purgeStatus', 'isPurging', 'purge'), ('restoreStatus', 'isRestoring', 'restore')):
                    if defect == name: state.engine['status'][field] = {'r': {flag: True}}
                with self.assertRaises(ValueError): INTEGRITY.verify(lab, journal)
                lab.kubectl.assert_not_called()
                lab.write_private.assert_not_called()

    def test_request_identity_snapshot_replacement_and_global_policy_drift_cannot_pass(self):
        for defect in ('request', 'source', 'snapshot', 'checksum', 'timestamp', 'global', 'replicas'):
            with self.subTest(defect=defect):
                lab, journal, state = self.fixture()
                def observe():
                    if defect == 'request': state.volume['spec']['snapshotHashingRequestedAt'] = 'replaced'
                    if defect == 'source': state.volume['metadata']['uid'] = 'replaced'
                    if defect == 'snapshot': state.rows['native']['metadata']['uid'] = 'replaced'
                    if defect == 'checksum': state.rows['native']['status']['checksum'] = ''
                    if defect == 'timestamp': state.rows['native']['status']['checksumCalculatedAt'] = 'invalid'
                    if defect == 'global': state.settings[INTEGRITY.SETTINGS[0]]['value'] = 'drifted'
                    if defect == 'replicas': lab.replica_topology.return_value = None
                state.observe = observe
                with redirect_stdout(io.StringIO()) as output, self.assertRaises((ValueError, TimeoutError)):
                    INTEGRITY.verify(lab, journal)
                self.assertNotIn('LONGHORN_V2_INTEGRITY_RESULT', output.getvalue())

    def test_rfc3339_comparison_handles_offsets_and_rejects_unzoned_time(self):
        self.assertEqual(INTEGRITY.timestamp('2026-01-01T08:00:00+08:00'), INTEGRITY.timestamp('2026-01-01T00:00:00Z'))
        for value in ('2026-01-01T00:00:00', '', 'tomorrow', None):
            with self.assertRaises(ValueError): INTEGRITY.timestamp(value)

    def test_product_snapshots_and_restores_precede_checksum_and_hook_is_always_restored(self):
        for failure in (None, 'snapshot', 'checksum'):
            with self.subTest(failure=failure):
                calls = []
                def original(*args):
                    calls.append('snapshot')
                    if failure == 'snapshot': raise ValueError('snapshot failed')
                    return ('post-payload', 'post-digest')
                def checksum(*args):
                    calls.append('checksum')
                    if failure == 'checksum': raise ValueError('checksum failed')
                lab = SimpleNamespace(verify_snapshot=original)
                def business(mode, pid):
                    self.assertEqual((mode, pid), ('snapshot', 12))
                    self.assertEqual(lab.verify_snapshot({}, None, pid, 'old', 'hash'), ('post-payload', 'post-digest'))
                lab.business = business
                with patch.object(INTEGRITY, 'verify', side_effect=checksum):
                    if failure:
                        with self.assertRaises(ValueError): INTEGRITY.business(lab, 12)
                    else: INTEGRITY.business(lab, 12)
                self.assertIs(lab.verify_snapshot, original)
                self.assertEqual(calls, ['snapshot'] if failure == 'snapshot' else ['snapshot', 'checksum'])

    def test_complete_log_requires_restores_original_data_and_cleanup_without_rehash_claim(self):
        lab, journal, _ = self.fixture()
        with redirect_stdout(io.StringIO()) as output: INTEGRITY.verify(lab, journal)
        log = output.getvalue()
        log += ''.join('LONGHORN_SNAPSHOT_BOUNDARY_VERIFIED type='+kind+' snapshot_sha256='+'a'*64+' source_sha256='+'b'*64+'\n' for kind in ('native', 'csi'))
        log += 'LONGHORN_FSYNC_REMOUNT_VERIFIED bytes=12 sha256='+'b'*64+' replica_nodes=a,b,c\n'
        log += 'LONGHORN_V2_EXPECTED_PRODUCT_REFUSAL_VERIFIED\nLONGHORN_BUSINESS_FIXTURES_CLEAN_PASS\nLONGHORN_SNAPSHOT_BUSINESS_PASS\n'
        INTEGRITY.verify_log(log)
        for bad in (log+output.getvalue(), log.replace('"whole_data_rehash_proven": false', '"whole_data_rehash_proven": true'),
                    log.replace('replica_nodes=a,b,c', 'replica_nodes=a,a,c'),
                    log.replace('type=csi', 'type=native'), log.replace('LONGHORN_BUSINESS_FIXTURES_CLEAN_PASS', '')):
            with self.assertRaises(ValueError): INTEGRITY.verify_log(bad)

    def test_shell_uses_own_business_and_real_log_verifier_then_scoped_cleanup(self):
        source = (ROOT/'tests/helpers/longhorn-v2-volumes.sh').read_text()
        for failure in ('none', 'business', 'result', 'cleanup', 'core'):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory:
                base = Path(directory)
                (base/'.venv/bin').mkdir(parents=True); (base/'tests/helpers').mkdir(parents=True)
                (base/'.venv/bin/python').symlink_to(sys.executable)
                (base/'kube-bin').mkdir()
                command = base/'kube-bin/kubectl'
                command.write_text('#!/bin/sh\nexit 0\n'); command.chmod(0o755)
                helper = '''import os,signal,sys,time
mode=sys.argv[1]; failure=os.environ['FAIL_MODE']
if mode=='relay-maintain':
 signal.signal(signal.SIGTERM,lambda *_:sys.exit(0))
 while True:time.sleep(.01)
with open('calls.txt','a') as stream:stream.write(mode+'\\n')
if (failure=='cleanup' and mode=='business-clean') or (failure=='core' and mode=='verify-core'):sys.exit(17)
'''
                for name in ('core', 'install'):
                    (base/'tests/helpers'/('longhorn-v2-'+name+'.py')).write_text(helper)
                integrity = '''import json,os,runpy,sys
mode=sys.argv[1];failure=os.environ['FAIL_MODE']
with open('calls.txt','a') as stream:stream.write('integrity-'+mode+'\\n')
if mode=='verify':runpy.run_path(os.environ['INTEGRITY_SOURCE'],run_name='__main__')
elif mode=='business':
 if failure=='business':sys.exit(17)
 row={'volume_uid':'owned','policy':'fast-check','requested_at':'2026-01-01T00:00:00Z',
      'completed_at':'2026-01-01T00:00:00Z','global_settings_unchanged':True,'whole_data_rehash_proven':False,
      'original_policy':'ignored','original_policy_restored':True,
      'snapshots':{n:{'uid':n,'checksum':'checksum','calculated_at':'2026-01-01T00:00:00Z','previously_hashed':False} for n in ('a','b')}}
 if failure!='result':print('LONGHORN_V2_INTEGRITY_RESULT '+json.dumps(row))
 for kind in ('native','csi'):print('LONGHORN_SNAPSHOT_BOUNDARY_VERIFIED type='+kind+' snapshot_sha256='+'a'*64+' source_sha256='+'b'*64)
 print('LONGHORN_FSYNC_REMOUNT_VERIFIED bytes=12 sha256='+'b'*64+' replica_nodes=a,b,c')
 print('LONGHORN_V2_EXPECTED_PRODUCT_REFUSAL_VERIFIED\\nLONGHORN_BUSINESS_FIXTURES_CLEAN_PASS\\nLONGHORN_SNAPSHOT_BUSINESS_PASS')
'''
                (base/'tests/helpers/longhorn-v2-integrity.py').write_text(integrity)
                script = source.replace('BASE=/usr/local/kubeauto', 'BASE='+str(base)).replace(
                    '/var/tmp/kubeauto-longhorn-v2-volumes.', str(base/'work.'))
                result = subprocess.run(['bash', '-c', script, 'contract', 'v2-integrity-qualification'],
                    env=dict(os.environ, LONGHORN_PARENT_LEASE_HELD='yes', FAIL_MODE=failure,
                             INTEGRITY_SOURCE=str(ROOT/'tests/helpers/longhorn-v2-integrity.py')),
                    text=True, capture_output=True, timeout=15)
                for name in re.findall(r'private_diagnostic=(/var/tmp/longhorn-v2-integrity-failure-\S+)', result.stderr):
                    Path(name).unlink()
                calls = (base/'calls.txt').read_text().splitlines()
                self.assertIn('integrity-business', calls)
                self.assertIn('business-clean', calls)
                self.assertIn('relay-clean', calls)
                self.assertNotIn('business-snapshot', calls)
                self.assertNotIn('restore-hosts', calls)
                marker = 'LONGHORN_V2_INTEGRITY_QUALIFICATION_PASS'
                if failure == 'none':
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertIn(marker, result.stdout)
                    self.assertIn('integrity-verify', calls)
                else:
                    self.assertNotEqual(result.returncode, 0)
                    self.assertNotIn(marker, result.stdout)


if __name__ == '__main__':
    unittest.main()
