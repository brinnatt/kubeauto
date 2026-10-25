"""V2 full-copy boundaries; linked/nested clones remain separate scenarios."""
import copy
from contextlib import redirect_stdout
import hashlib
import importlib.util
import io
import json
import os
import re
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location('v2_clone_contract', ROOT/'tests/helpers/longhorn-v2-clones.py')
CLONES = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CLONES)


class FullCopyContracts(unittest.TestCase):
    def fixture(self):
        journal = {'namespace': 'business', 'volume': 'source', 'volume_uid': 'source-uid', 'pv': 'source-pv',
                   'claims': {'clone': {'volume': 'clone', 'volume_uid': 'clone-uid', 'pv': 'clone-pv'}}}
        volumes = {name: {'metadata': {'uid': name+'-uid'}, 'spec': {'dataEngine': 'v2', 'size': str(2*1024**3),
                           'cloneMode': 'full-copy'}, 'status': {'cloneStatus': {'state': 'completed',
                           'sourceVolume': 'source', 'snapshot': 'clone-snapshot'}}} for name in ('source', 'clone')}
        topologies = {name: {name+str(i): {'uid': name+str(i), 'node': 'node'+str(i)} for i in range(3)}
                      for name in ('source', 'clone')}
        replicas = [{'spec': {'volumeName': 'clone', 'linkedCloneSrcReplicaName': ''}} for _ in range(3)]
        digest = hashlib.sha256(b'payload'*262144).hexdigest()
        clone_digest = hashlib.sha256(b'payload-clone'*262144).hexdigest()
        def get(kind, name='', namespace=''):
            return copy.deepcopy(volumes[name] if kind == 'volumes.longhorn.io' else {'items': replicas})
        def wait(stage, timeout, check):
            self.assertEqual((stage, timeout), ('v2-full-copy-completed-three-replicas', 900))
            result = check()
            if not result: raise TimeoutError(stage)
            return result
        def kubectl(*args):
            code = args[-1]
            return SimpleNamespace(stdout=('a'*64 if 'os.urandom' in code else
                clone_digest if code == 'payload-clone' else digest if code == 'payload' else ''))
        lab = SimpleNamespace(get=Mock(side_effect=get), capacity_bytes=int,
            replica_topology=Mock(side_effect=lambda row, count: topologies[row['volume']]),
            healthy_replicas=Mock(return_value=['node0', 'node1', 'node2']), wait=Mock(side_effect=wait),
            payload_program=Mock(side_effect=lambda payload, **kw: payload), kubectl=Mock(side_effect=kubectl))
        return lab, journal, volumes, topologies, replicas, digest

    def observation(self):
        lab, journal, _, _, _, digest = self.fixture()
        with redirect_stdout(io.StringIO()) as output:
            CLONES.full_copy(lab, journal, 'payload', digest)
        return output.getvalue(), lab

    def test_full_copy_confirms_source_only_file_target_write_and_exact_topologies(self):
        log, lab = self.observation()
        row = json.loads(log.split(' ', 1)[1])
        self.assertEqual(row['source_only_bytes'], 1024**2)
        self.assertTrue(row['source_only_absent_from_clone'])
        self.assertNotEqual(row['source_sha256'], row['clone_sha256'])
        self.assertEqual(lab.kubectl.call_count, 4)
        self.assertIn("assert not os.path.exists('/data/clone-source-only')", lab.kubectl.call_args_list[1].args[-1])

    def test_identity_engine_capacity_clone_status_and_dependency_errors_stop_before_writes(self):
        for defect in ('same-volume', 'same-pv', 'uid', 'v1', 'size', 'mode', 'wrong-source', 'failed',
                       'copy-only', 'no-snapshot', 'topology', 'shared-replica', 'linked', 'raw-disk-health'):
            with self.subTest(defect=defect):
                lab, journal, volumes, topologies, replicas, digest = self.fixture()
                if defect == 'same-volume': journal['claims']['clone']['volume'] = 'source'
                if defect == 'same-pv': journal['claims']['clone']['pv'] = 'source-pv'
                if defect == 'uid': volumes['clone']['metadata']['uid'] = 'foreign'
                if defect == 'v1': volumes['clone']['spec']['dataEngine'] = 'v1'
                if defect == 'size': volumes['clone']['spec']['size'] = str(3*1024**3)
                if defect == 'mode': volumes['clone']['spec']['cloneMode'] = 'linked-clone'
                if defect == 'wrong-source': volumes['clone']['status']['cloneStatus']['sourceVolume'] = 'foreign'
                if defect == 'failed': volumes['clone']['status']['cloneStatus']['state'] = 'failed'
                if defect == 'copy-only': volumes['clone']['status']['cloneStatus']['state'] = 'copy-completed-awaiting-healthy'
                if defect == 'no-snapshot': volumes['clone']['status']['cloneStatus']['snapshot'] = ''
                if defect == 'topology': topologies['clone'] = None
                if defect == 'shared-replica': topologies['clone']['clone0']['uid'] = 'source0'
                if defect == 'linked': replicas[0]['spec']['linkedCloneSrcReplicaName'] = 'source-replica'
                if defect == 'raw-disk-health': lab.healthy_replicas.return_value = None
                with self.assertRaises((ValueError, TimeoutError)): CLONES.full_copy(lab, journal, 'payload', digest)
                lab.kubectl.assert_not_called()

    def test_marker_failure_clone_leak_or_source_corruption_cannot_emit_success(self):
        for defect in ('source-file', 'target-sees-source', 'target-data', 'source-data'):
            with self.subTest(defect=defect):
                lab, journal, *_, digest = self.fixture()
                original = lab.kubectl.side_effect
                def kubectl(*args):
                    code = args[-1]
                    if defect == 'source-file' and 'os.urandom' in code: return SimpleNamespace(stdout='')
                    if defect == 'target-sees-source' and 'assert not os.path.exists' in code: raise ValueError('leaked source file')
                    if (defect == 'target-data' and code == 'payload-clone') or (defect == 'source-data' and code == 'payload'):
                        return SimpleNamespace(stdout='corrupt')
                    return original(*args)
                lab.kubectl.side_effect = kubectl
                with redirect_stdout(io.StringIO()) as output, self.assertRaises(ValueError):
                    CLONES.full_copy(lab, journal, 'payload', digest)
                self.assertNotIn('LONGHORN_V2_FULL_COPY_CLONE_OBSERVED', output.getvalue())

    def test_product_class_reentry_and_original_clone_precede_extended_proof_and_restore_hook(self):
        for failure in (None, 'product', 'clone', 'proof'):
            with self.subTest(failure=failure):
                calls = []
                def step(name):
                    calls.append(name)
                    if failure == name: raise ValueError(name)
                original = Mock(side_effect=lambda *args: step('clone'))
                program = Mock(return_value='original-program')
                lab = SimpleNamespace(verify_clone=original, payload_program=program,
                    run_requests=Mock(side_effect=lambda *args: step('product')))
                def business(*args, **options):
                    lab.verify_clone({}, None, 'payload', 'digest')
                    self.assertIn("open('/data/clone-source-only','rb')", lab.payload_program('payload', read_only=True))
                lab.business = Mock(side_effect=business)
                def proof(*args):
                    step('proof')
                    return {'source_only_sha256': 'a'*64}
                with patch.object(CLONES, 'full_copy', side_effect=proof), redirect_stdout(io.StringIO()):
                    if failure:
                        with self.assertRaises(ValueError): CLONES.business(lab, 12)
                    else: CLONES.business(lab, 12)
                self.assertIs(lab.verify_clone, original)
                self.assertIs(lab.payload_program, program)
                expected = ['product', 'clone', 'proof']
                self.assertEqual(calls, expected[:expected.index(failure)+1] if failure else expected)

    def test_complete_results_require_original_remount_cleanup_and_both_write_directions(self):
        log, _ = self.observation()
        row = json.loads(log.split(' ', 1)[1])
        suffix = ('LONGHORN_FSYNC_REMOUNT_VERIFIED bytes=9437184 sha256='+row['source_sha256']+
                  ' replica_nodes=node0,node1,node2\nLONGHORN_CLONE_BUSINESS_PASS\nLONGHORN_BUSINESS_FIXTURES_CLEAN_PASS\n'
                  'LONGHORN_V2_CLONE_SOURCE_FILE_REMOUNT_VERIFIED\n')
        CLONES.verify_log(log+suffix)
        for bad in (log+log+suffix, log+suffix.replace('replica_nodes=node0,node1,node2', 'replica_nodes=node0,node1'),
                    log+suffix.replace('LONGHORN_CLONE_BUSINESS_PASS', ''),
                    (log+suffix).replace('"source_only_absent_from_clone": true', '"source_only_absent_from_clone": false'),
                    (log+suffix).replace('"clone_mode": "full-copy"', '"clone_mode": "linked-clone"')):
            with self.assertRaises(ValueError): CLONES.verify_log(bad)

    def test_generated_remount_guard_executes_and_rejects_lost_confirmed_source_file(self):
        data = b'x'*1024**2
        digest = hashlib.sha256(data).hexdigest()
        for defect in ('none', 'corrupt', 'missing', 'not-observed'):
            with self.subTest(defect=defect), tempfile.TemporaryDirectory() as directory:
                path = Path(directory)/'source-only'
                if defect != 'missing': path.write_bytes(data if defect != 'corrupt' else b'y'*len(data))
                lab = SimpleNamespace(verify_clone=Mock(), run_requests=Mock(),
                    payload_program=Mock(return_value="import hashlib;print('original-source-digest')"))
                def business(*args, **options):
                    lab.verify_clone({}, None, 'payload', 'digest')
                    if defect == 'not-observed': return
                    code = lab.payload_program('payload', read_only=True).replace("'/data/clone-source-only'", repr(str(path)))
                    subprocess.run([sys.executable, '-c', code], check=True, text=True, capture_output=True)
                lab.business = business
                with patch.object(CLONES, 'full_copy', return_value={'source_only_sha256': digest}), redirect_stdout(io.StringIO()) as output:
                    if defect == 'none':
                        CLONES.business(lab, 12)
                        self.assertIn('LONGHORN_V2_CLONE_SOURCE_FILE_REMOUNT_VERIFIED', output.getvalue())
                    else:
                        with self.assertRaises((ValueError, subprocess.CalledProcessError)): CLONES.business(lab, 12)
                        self.assertNotIn('LONGHORN_V2_CLONE_SOURCE_FILE_REMOUNT_VERIFIED', output.getvalue())


    def test_shell_runs_real_clone_result_verifier_and_preserves_failure_cleanup(self):
        source = (ROOT/'tests/helpers/longhorn-v2-volumes.sh').read_text()
        log, _ = self.observation()
        row = json.loads(log.split(' ', 1)[1])
        log += ('LONGHORN_FSYNC_REMOUNT_VERIFIED bytes=9437184 sha256='+row['source_sha256']+
                ' replica_nodes=node0,node1,node2\nLONGHORN_CLONE_BUSINESS_PASS\n'
                'LONGHORN_BUSINESS_FIXTURES_CLEAN_PASS\nLONGHORN_V2_CLONE_SOURCE_FILE_REMOUNT_VERIFIED\n')
        for failure in ('none', 'transport', 'baseline', 'business', 'missing-result', 'missing-remount',
                        'cleanup', 'final-baseline', 'core'):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory:
                base = Path(directory)
                (base/'.venv/bin').mkdir(parents=True); (base/'tests/helpers').mkdir(parents=True)
                (base/'.venv/bin/python').symlink_to(sys.executable)
                (base/'kube-bin').mkdir()
                command = base/'kube-bin/kubectl'
                command.write_text('#!/bin/sh\nexit 0\n'); command.chmod(0o755)
                selected = log
                if failure == 'missing-result': selected = '\n'.join(log.splitlines()[1:])+'\n'
                if failure == 'missing-remount': selected = log.replace('LONGHORN_V2_CLONE_SOURCE_FILE_REMOUNT_VERIFIED', '')
                (base/'observation.log').write_text(selected)
                helper = '''import os,signal,sys,time
from pathlib import Path
mode=sys.argv[1];failure=os.environ['FAIL_MODE']
if mode=='relay-maintain':
 signal.signal(signal.SIGTERM,lambda *_:sys.exit(0))
 while True:time.sleep(.01)
with open('calls.txt','a') as stream:stream.write(mode+'\\n')
if mode=='volume-baseline':
 counter=Path('baselines');n=int(counter.read_text())+1 if counter.exists() else 1;counter.write_text(str(n))
 if failure=='baseline' or failure=='final-baseline' and n>1:sys.exit(17)
if (failure=='transport' and mode=='verify-transport') or (failure=='cleanup' and mode=='business-clean') or (failure=='core' and mode=='verify-core'):sys.exit(17)
'''
                for name in ('core', 'install'):
                    (base/f'tests/helpers/longhorn-v2-{name}.py').write_text(helper)
                clone = '''import os,runpy,sys
from pathlib import Path
mode=sys.argv[1];failure=os.environ['FAIL_MODE']
with open('calls.txt','a') as stream:stream.write('clone-'+mode+'\\n')
if mode=='verify':runpy.run_path(os.environ['CLONE_SOURCE'],run_name='__main__')
elif mode=='business':
 if failure=='business':sys.exit(17)
 print(Path('observation.log').read_text())
'''
                (base/'tests/helpers/longhorn-v2-clones.py').write_text(clone)
                script = source.replace('BASE=/usr/local/kubeauto', 'BASE='+str(base)).replace(
                    '/var/tmp/kubeauto-longhorn-v2-volumes.', str(base/'work.'))
                result = subprocess.run(['bash', '-c', script, 'contract', 'v2-full-copy-clone-qualification'],
                    env=dict(os.environ, LONGHORN_PARENT_LEASE_HELD='yes', FAIL_MODE=failure,
                             CLONE_SOURCE=str(ROOT/'tests/helpers/longhorn-v2-clones.py')),
                    text=True, capture_output=True, timeout=15)
                for name in re.findall(r'private_diagnostic=(/var/tmp/longhorn-v2-clone-failure-\S+)', result.stderr):
                    Path(name).unlink()
                calls = (base/'calls.txt').read_text().splitlines()
                marker = 'LONGHORN_V2_FULL_COPY_CLONE_QUALIFICATION_PASS'
                self.assertEqual(result.returncode == 0, failure == 'none', result.stdout+result.stderr)
                self.assertEqual(marker in result.stdout, failure == 'none')
                if failure in ('transport', 'baseline'):
                    self.assertNotIn('clone-business', calls)
                    self.assertNotIn('business-clean', calls)
                else:
                    self.assertIn('clone-business', calls)
                    self.assertLess(calls.index('business-clean'), calls.index('relay-clean'))
                    self.assertEqual(calls[-1], 'verify-core')
                    self.assertNotIn('business-snapshot', calls)
                    self.assertNotIn('restore-hosts', calls)
                    if failure != 'business': self.assertIn('clone-verify', calls)

    def test_fixed_dispatch_live_route_allowlists_and_matrix_owner(self):
        mode = 'v2-full-copy-clone-qualification'
        script = 'source "$1"\nlonghorn_launch() { printf "%s %s" "$1" "$2"; }\nlonghorn_dispatch --longhorn-'+mode
        result = subprocess.run(['bash', '-c', script, 'contract', str(ROOT/'tests/helpers/longhorn-runner.sh')],
                                capture_output=True, text=True, check=True)
        self.assertEqual(result.stdout, mode+' LONGHORN_V2_FULL_COPY_CLONE_QUALIFICATION_PASS')
        runner = (ROOT/'tests/helpers/longhorn-runner.sh').read_text()
        for variable in ('module', 'mode'):
            branches = re.findall(r'case "\$'+variable+r'" in ([^\n]+)', runner)
            self.assertEqual(len(branches), 1)
            self.assertIn(mode, branches[0].split(')')[0].split('|'))
        live = (ROOT/'tests/helpers/longhorn-live.sh').read_text()
        route = live.split(mode+'|', 1)[1].split(';;', 1)[0]
        self.assertIn('longhorn-v2-volumes.sh', route)
        matrix = (ROOT/'tests/longhorn-test-matrix.yaml').read_text().split('- id: LONGHORN-49\n', 1)[1].split('- id:', 1)[0]
        self.assertIn('--longhorn-'+mode, matrix)
        self.assertIn('  group: v2-clone\n', matrix)
        self.assertIn('  success_marker: LONGHORN_V2_CLONE_PASS\n', matrix)



if __name__ == '__main__':
    unittest.main()
