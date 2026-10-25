"""V2 control-plane loss must preserve original data-plane identities and I/O."""
import copy
from contextlib import redirect_stdout
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shlex
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location('v2_manager_loss', ROOT/'tests/helpers/longhorn-v2-manager-loss.py')
GATE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(GATE)
COMMON = GATE.load('longhorn-v2-control-faults.py')


class ManagerLossContracts(unittest.TestCase):
    def fixture(self, base):
        path = base/'roles/longhorn/files/images.json'
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({'1.12.1': {'longhorn-manager': 'v1.12.1', 'longhorn-instance-manager': 'v1.12.1'}}))
        def pod(name, uid, kind, owner, node):
            return {'metadata': {'name': name, 'uid': uid, 'labels': {'app': 'longhorn-manager'},
                'ownerReferences': [{'kind': kind, 'uid': owner, 'controller': True}]},
                'spec': {'nodeName': node, 'containers': [{'name': 'longhorn-manager', 'image': 'registry.test/longhorn-manager:v1.12.1'}]},
                'status': {'phase': 'Running', 'conditions': [{'type': 'Ready', 'status': 'True'}]}}
        managers = [{'metadata': {'name': 'im'+str(i), 'uid': 'im-uid'+str(i)},
            'spec': {'nodeID': 'n'+str(i), 'dataEngine': 'v2', 'type': 'aio', 'image': 'registry.test/longhorn-instance-manager:v1.12.1'},
            'status': {'currentState': 'running'}} for i in range(3)]
        pods = {'im'+str(i): pod('im'+str(i), 'im-pod'+str(i), 'InstanceManager', 'im-uid'+str(i), 'n'+str(i)) for i in range(3)}
        pods['manager'] = pod('manager', 'manager-old', 'DaemonSet', 'ds', 'n0')
        for i in range(3):
            pods['im'+str(i)]['spec']['containers'] = [{'name': 'instance-manager',
                'image': 'registry.test/longhorn-instance-manager:v1.12.1', 'args': ['--spdk-enabled']}]
            pods['im'+str(i)]['spec']['volumes'] = [{'secret': {'secretName': 'longhorn-grpc-tls'}}]
        objects = {'daemonset': {'metadata': {'uid': 'ds', 'labels': {'app.kubernetes.io/instance': 'longhorn'}}, 'spec': {'template': 'original'}}}
        objects['instancemanagers.longhorn.io'] = {'items': managers}
        for kind, count in (('engines.longhorn.io', 1), ('enginefrontends.longhorn.io', 1), ('replicas.longhorn.io', 3)):
            objects[kind] = {'items': [{'metadata': {'name': kind+str(i), 'uid': kind+'-uid'+str(i),
                'ownerReferences': [{'kind': 'Volume', 'uid': 'volume-uid'}]},
                'spec': {'volumeName': 'volume', 'nodeID': 'n'+str(i), 'diskID': 'disk'+str(i)},
                'status': {'currentState': 'running', 'instanceManagerName': 'im'+str(i)}} for i in range(count)]}
        def get(kind, name='', namespace=''):
            return copy.deepcopy(pods.get(name) if kind == 'pod' else {'items': list(pods.values())} if kind == 'pods' else objects[kind])
        topology = {str(i): {'uid': 'r'+str(i), 'node': 'n'+str(i)} for i in range(3)}
        lab = SimpleNamespace(BASE=base, DIRECTORY=base, get=Mock(side_effect=get), write_private=Mock(), remove=Mock(),
            replica_topology=Mock(return_value=topology), healthy_replicas=Mock(return_value=True),
            check_business_graph=Mock(), installation_health=Mock(), wait=Mock(side_effect=lambda label, timeout, check: check()))
        common = SimpleNamespace(owner=COMMON.owner, pod_ready=COMMON.pod_ready,
            boundary=Mock(return_value=({}, {'original_client_node': 'n0'})))
        return lab, {'volume': 'volume', 'volume_uid': 'volume-uid'}, objects, pods, common

    def test_runtime_records_actual_consumers_and_rejects_foreign_missing_or_unready_state(self):
        for defect in ('none', 'extra-im', 'im-engine', 'im-owner', 'im-ready', 'foreign-instance', 'instance-owner',
                       'instance-state', 'instance-manager', 'extra-instance', 'im-image', 'pod-image', 'spdk', 'tls'):
            with self.subTest(defect=defect), tempfile.TemporaryDirectory() as directory:
                lab, journal, objects, pods, common = self.fixture(Path(directory))
                im = objects['instancemanagers.longhorn.io']['items'][0]
                instance = objects['enginefrontends.longhorn.io']['items'][0]
                if defect == 'extra-im':
                    other = copy.deepcopy(im)
                    other['metadata']['name'] = 'foreign-im'
                    objects['instancemanagers.longhorn.io']['items'].append(other)
                if defect == 'im-engine': im['spec']['dataEngine'] = 'v1'
                if defect == 'im-owner': pods['im0']['metadata']['ownerReferences'][0]['uid'] = 'foreign'
                if defect == 'im-ready': pods['im0']['status']['conditions'][0]['status'] = 'False'
                if defect == 'foreign-instance': instance['spec']['volumeName'] = 'foreign'
                if defect == 'instance-owner': instance['metadata']['ownerReferences'][0]['uid'] = 'foreign'
                if defect == 'instance-state': instance['status']['currentState'] = 'stopped'
                if defect == 'instance-manager': instance['status']['instanceManagerName'] = 'unknown'
                if defect == 'extra-instance': objects['enginefrontends.longhorn.io']['items'].append(copy.deepcopy(instance))
                if defect == 'im-image': im['spec']['image'] = 'other'
                if defect == 'pod-image': pods['im0']['spec']['containers'][0]['image'] = 'other'
                if defect == 'spdk': pods['im0']['spec']['containers'][0]['args'] = []
                if defect == 'tls': pods['im0']['spec']['volumes'] = []
                with patch.object(GATE, 'load', return_value=common):
                    if defect == 'none':
                        result = GATE.runtime(lab, journal)
                        self.assertEqual('im-pod0', result['managers']['im0']['pod_uid'])
                        self.assertEqual(3, len(result['replicas']))
                    else:
                        with self.assertRaises(ValueError): GATE.runtime(lab, journal)

    def test_delete_requires_stable_boundary_and_persists_intent_before_native_delete(self):
        with tempfile.TemporaryDirectory() as directory:
            lab, journal, objects, pods, common = self.fixture(Path(directory))
            calls = []
            lab.write_private.side_effect = lambda *a: calls.append('journal')
            lab.remove.side_effect = lambda obj: calls.append(('delete', obj['metadata']['uid']))
            with patch.object(GATE, 'load', return_value=common), redirect_stdout(io.StringIO()):
                proof = GATE.inject(lab, journal, 'manager')
            self.assertEqual(['journal', ('delete', 'manager-old')], calls)
            self.assertEqual(proof, journal['v2_manager_loss'])
            lab.remove.reset_mock()
            with patch.object(GATE, 'runtime', side_effect=[proof['runtime'], {}]):
                with self.assertRaises(ValueError): GATE.inject(lab, journal, 'manager')
            lab.remove.assert_not_called()

    def test_manager_parent_image_readiness_and_component_replacement_never_count_as_recovery(self):
        defects = ('none', 'old', 'unready', 'parent', 'spec', 'image', 'im-uid', 'im-spec', 'im-pod', 'im-owner', 'im-set')
        for defect in defects:
            with self.subTest(defect=defect), tempfile.TemporaryDirectory() as directory:
                lab, journal, objects, pods, common = self.fixture(Path(directory))
                with patch.object(GATE, 'load', return_value=common), redirect_stdout(io.StringIO()):
                    proof = GATE.inject(lab, journal, 'manager')
                    pods['manager']['metadata']['uid'] = 'manager-new'
                    if defect == 'old': pods['manager']['metadata']['uid'] = 'manager-old'
                    if defect == 'unready': pods['manager']['status']['conditions'][0]['status'] = 'False'
                    if defect == 'parent': objects['daemonset']['metadata']['uid'] = 'other'
                    if defect == 'spec': objects['daemonset']['spec']['template'] = 'other'
                    if defect == 'image': pods['manager']['spec']['containers'][0]['image'] = 'other'
                    if defect == 'im-uid': objects['instancemanagers.longhorn.io']['items'][0]['metadata']['uid'] = 'other'
                    if defect == 'im-spec': objects['instancemanagers.longhorn.io']['items'][0]['spec']['type'] = 'other'
                    if defect == 'im-pod': pods['im0']['metadata']['uid'] = 'other'
                    if defect == 'im-owner': pods['im0']['metadata']['ownerReferences'][0]['uid'] = 'other'
                    if defect == 'im-set': objects['instancemanagers.longhorn.io']['items'].pop()
                    if defect in ('old', 'unready'): self.assertFalse(GATE.recovered(lab, journal, proof))
                    elif defect == 'none': self.assertTrue(GATE.recovered(lab, journal, proof))
                    else:
                        with self.assertRaises(ValueError): GATE.recovered(lab, journal, proof)

    def test_unhealthy_volume_waits_and_changed_data_plane_stops(self):
        with tempfile.TemporaryDirectory() as directory:
            lab, journal, objects, pods, common = self.fixture(Path(directory))
            with patch.object(GATE, 'load', return_value=common), redirect_stdout(io.StringIO()):
                proof = GATE.inject(lab, journal, 'manager')
                pods['manager']['metadata']['uid'] = 'manager-new'
                lab.healthy_replicas.return_value = False
                self.assertFalse(GATE.recovered(lab, journal, proof))
                lab.healthy_replicas.return_value = True
                objects['engines.longhorn.io']['items'][0]['metadata']['uid'] = 'other'
                with self.assertRaises(ValueError): GATE.recovered(lab, journal, proof)

    def test_cleanup_restores_component_before_business_delete_and_retains_unknown_state(self):
        with tempfile.TemporaryDirectory() as directory:
            lab, journal, objects, pods, common = self.fixture(Path(directory))
            with patch.object(GATE, 'load', return_value=common), redirect_stdout(io.StringIO()):
                proof = GATE.inject(lab, journal, 'manager')
            path = Path(directory)/GATE.RECORD
            path.write_text(json.dumps(journal))
            calls = []
            original = Mock(side_effect=lambda *a, **kw: calls.append('business'))
            lab.wait.side_effect = lambda *a: calls.append('restore')
            with patch.object(GATE, 'component_restored', return_value=None):
                with self.assertRaises(ValueError): GATE.cleanup(lab, original)
            original.assert_not_called()
            self.assertTrue(path.exists())
            pods['manager']['metadata']['uid'] = 'manager-new'
            with patch.object(GATE, 'load', return_value=common), redirect_stdout(io.StringIO()):
                GATE.cleanup(lab, original)
            self.assertEqual(['restore', 'restore', 'business'], calls)

    def test_exception_diagnostics_precede_component_recovery_and_business_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            lab, journal, objects, pods, common = self.fixture(Path(directory))
            record = Path(directory)/GATE.RECORD
            record.write_text(json.dumps(journal))
            calls = []
            original = Mock(side_effect=lambda *a, **kw: calls.append('business'))
            with patch.object(GATE, 'diagnostics', side_effect=lambda *a: calls.append('diagnostics')):
                try:
                    raise ValueError('first owning failure')
                except ValueError:
                    GATE.cleanup(lab, original)
            self.assertEqual(['diagnostics', 'business'], calls)

    def test_diagnostics_are_private_scoped_and_keep_rendered_inputs_without_reading_secrets(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            (base/'yml/longhorn').mkdir(parents=True)
            (base/'config.yml').write_text('original input')
            (base/'yml/longhorn/input.json').write_text('{}')
            def write(path, data):
                path.write_text(json.dumps(data))
                path.chmod(0o600)
            lab = SimpleNamespace(DIRECTORY=base, write_private=write,
                kubectl=Mock(return_value=SimpleNamespace(returncode=0, stdout='state', stderr='')))
            with redirect_stdout(io.StringIO()) as output:
                try:
                    raise ValueError('first failure')
                except ValueError:
                    GATE.diagnostics(lab, {'namespace': 'owned-business'})
            folder = next(base.glob('longhorn-v2-manager-failure-*'))
            self.assertEqual(0o700, folder.stat().st_mode & 0o777)
            self.assertEqual('original input', (folder/'config.yml').read_text())
            self.assertEqual('ValueError', json.loads((folder/'first-failure.json').read_text())['exception_type'])
            self.assertTrue(all(p.stat().st_mode & 0o777 == 0o600 for p in folder.iterdir()))
            for call in lab.kubectl.call_args_list:
                self.assertNotIn('secrets', call.args)
                self.assertFalse(call.kwargs['check'])
                self.assertEqual(10, call.kwargs['timeout'])

    def observation(self):
        faults, control = GATE.load('longhorn-faults.py'), GATE.load('longhorn-control-faults.py')
        token = 'owner:manager'
        def ledger(value):
            rows = [{'type': 'ack', 'sequence': i, 'sha256': hashlib.sha256(faults.ledger_block(value, i)).hexdigest()} for i in range(4)]
            return control.confirmed(rows, value, faults)
        confirmed = ledger(token)
        confirmed.update(client='writer', fresh=ledger(token+':recovered'))
        confirmed['fresh']['path'] = '/data/control-recovered-ledger'
        runtime = {'volume_uid': 'v', 'managers': {str(i): {'uid': 'im'+str(i), 'pod_uid': 'p'+str(i), 'spec': {'nodeID': 'n'+str(i)}} for i in range(3)},
                   'replicas': {str(i): {'node': 'n'+str(i), 'uid': 'r'+str(i)} for i in range(3)}}
        runtime['instances'] = {kind: {str(i): {'uid': kind+str(i), 'manager': str(i)} for i in range(count)}
            for kind, count in (('engines.longhorn.io', 1), ('enginefrontends.longhorn.io', 1), ('replicas.longhorn.io', 3))}
        result = {'variant': 'manager', 'client_rc': 0, 'recovery_mode': 'native-component-recreation-original-client',
                  'recovery_s': 1, 'retries': [], 'confirmed': confirmed['summary'], 'fresh_confirmed': confirmed['fresh']['summary']}
        rows = {'LONGHORN_V2_MANAGER_LOSS_INJECTED': {'fault': 'v2-manager-pod-delete', 'pod_uid': 'old', 'parent': {'uid': 'ds'}, 'runtime': runtime},
            'LONGHORN_V2_MANAGER_LOSS_RECOVERED': {'pod_uid': 'new', 'parent_uid': 'ds', 'runtime': copy.deepcopy(runtime)},
            'LONGHORN_CONTROL_FAULT_RESULT': result, 'LONGHORN_CONTROL_FAULT_ACKNOWLEDGEMENTS': confirmed,
            'LONGHORN_CONTROL_FAULT_REMOUNT_VERIFIED': {'original': confirmed['summary'], 'recovered': confirmed['fresh']['summary']}}
        suffix = ('LONGHORN_FSYNC_REMOUNT_VERIFIED bytes='+str(5*262144)+' sha256='+hashlib.sha256(b'owner'*262144).hexdigest()+
            ' replica_nodes=n0,n1,n2\nLONGHORN_V2_MANAGER_LOSS_BUSINESS_PASS\nLONGHORN_CONTROL_FAILURE_BUSINESS_PASS\n'
            'LONGHORN_BUSINESS_FIXTURES_CLEAN_PASS\nLONGHORN_V2_MANAGER_RESTORED_BEFORE_CLEANUP\n')
        return rows, suffix

    def test_verifier_requires_original_runtime_every_ack_fresh_writes_remount_and_cleanup(self):
        for defect in ('none', 'old-pod', 'parent', 'runtime', 'client', 'duration', 'token', 'ack', 'summary', 'remount', 'payload', 'cleanup', 'failure', 'duplicate'):
            with self.subTest(defect=defect):
                rows, suffix = self.observation()
                result = rows['LONGHORN_CONTROL_FAULT_RESULT']
                ledger = rows['LONGHORN_CONTROL_FAULT_ACKNOWLEDGEMENTS']
                recovery = rows['LONGHORN_V2_MANAGER_LOSS_RECOVERED']
                if defect == 'old-pod': recovery['pod_uid'] = 'old'
                if defect == 'parent': recovery['parent_uid'] = 'other'
                if defect == 'runtime': recovery['runtime']['managers']['0']['pod_uid'] = 'other'
                if defect == 'client': result['client_rc'] = 1
                if defect == 'duration': result['recovery_s'] = 1201
                if defect == 'token': ledger['fresh']['token'] = 'other'
                if defect == 'ack': ledger['records'][0]['sha256'] = 'f'*64
                if defect == 'summary': result['confirmed'] = {}
                if defect == 'remount': rows['LONGHORN_CONTROL_FAULT_REMOUNT_VERIFIED']['original'] = {}
                if defect == 'payload': suffix = suffix.replace('n0,n1,n2', 'n0,n0,n2')
                if defect == 'cleanup': suffix = suffix.replace('LONGHORN_V2_MANAGER_RESTORED_BEFORE_CLEANUP', '')
                if defect == 'failure': suffix += 'LONGHORN_FIRST_FAILURE phase=test rc=1\n'
                text = ''.join(k+' '+json.dumps(v)+'\n' for k, v in rows.items())+suffix
                if defect == 'duplicate': text += text.splitlines()[0]+'\n'
                if defect == 'none': self.assertEqual(result, GATE.verify_log(text))
                else:
                    with self.assertRaises(ValueError): GATE.verify_log(text)

    def test_business_adapter_restores_loader_and_cleanup_after_success_or_error(self):
        for failure in (False, True):
            with self.subTest(failure=failure):
                control = SimpleNamespace()
                original, clean = Mock(return_value=control), Mock()
                lab = SimpleNamespace(load_helper=original, cleanup_business=clean, run_requests=Mock())
                def business(*a, **kw):
                    self.assertIs(control.inject, GATE.inject)
                    self.assertIs(control.recovered, GATE.recovered)
                    self.assertIs(lab.load_helper('longhorn-control-faults.py'), control)
                    self.assertEqual('manager', kw['fault_variant'])
                    if failure: raise ValueError('business error')
                lab.business = Mock(side_effect=business)
                with redirect_stdout(io.StringIO()) as output:
                    if failure:
                        with self.assertRaises(ValueError): GATE.business(lab, 9)
                        self.assertNotIn('LONGHORN_V2_MANAGER_LOSS_BUSINESS_PASS', output.getvalue())
                    else:
                        GATE.business(lab, 9)
                self.assertIs(lab.load_helper, original)
                self.assertIs(lab.cleanup_business, clean)

    def test_fixed_shell_reaches_real_verifier_and_recovers_before_cleanup_baseline(self):
        source = (ROOT/'tests/helpers/longhorn-v2-volumes.sh').read_text()
        cases = [('qualification', f) for f in ('none', 'business', 'bad-result', 'cleanup', 'baseline', 'core')]
        cases += [('cleanup', f) for f in ('none', 'cleanup', 'baseline', 'core')]
        for mode, failure in cases:
            with self.subTest(mode=mode, failure=failure), tempfile.TemporaryDirectory() as directory:
                base = Path(directory)
                (base/'.venv/bin').mkdir(parents=True)
                (base/'tests/helpers').mkdir(parents=True)
                python = base/'.venv/bin/python'
                python.write_text('#!/bin/sh\nexec '+shlex.quote(os.sys.executable)+' "$@"\n')
                python.chmod(0o755)
                (base/'kube-bin').mkdir()
                kubectl = base/'kube-bin/kubectl'
                kubectl.write_text('#!/bin/sh\nexit 0\n'); kubectl.chmod(0o755)
                rows, suffix = self.observation()
                if failure == 'bad-result': rows['LONGHORN_CONTROL_FAULT_RESULT']['client_rc'] = 1
                (base/'observation.log').write_text(''.join(k+' '+json.dumps(v)+'\n' for k, v in rows.items())+suffix)
                common = '''import os,signal,sys,time
mode=sys.argv[1];failure=os.environ['FAIL_MODE']
if mode=='relay-maintain':
 signal.signal(signal.SIGTERM,lambda *_:sys.exit(0))
 while True:time.sleep(.01)
with open('calls.txt','a') as stream:stream.write(mode+'\\n')
if (mode=='volume-baseline' and failure=='baseline') or (mode=='verify-core' and failure=='core'):sys.exit(17)
'''
                for name in ('core', 'install'): (base/f'tests/helpers/longhorn-v2-{name}.py').write_text(common)
                helper = '''import os,runpy,sys
from pathlib import Path
mode=sys.argv[1]
with open('calls.txt','a') as stream:stream.write(mode+'\\n')
if mode=='verify':runpy.run_path(os.environ['MANAGER_SOURCE'],run_name='__main__')
elif mode=='business':
 if os.environ['FAIL_MODE']=='business':sys.exit(17)
 print(Path('observation.log').read_text())
elif mode=='cleanup':
 if os.environ['FAIL_MODE']=='cleanup':sys.exit(17)
else:sys.exit(18)
'''
                (base/'tests/helpers/longhorn-v2-manager-loss.py').write_text(helper)
                script = source.replace('BASE=/usr/local/kubeauto', 'BASE='+str(base)).replace(
                    '/var/tmp/kubeauto-longhorn-v2-volumes.', str(base/'work.'))
                result = subprocess.run(['bash', '-c', script, 'contract', 'v2-manager-loss-'+mode],
                    text=True, capture_output=True, timeout=15,
                    env=dict(os.environ, LONGHORN_PARENT_LEASE_HELD='yes', FAIL_MODE=failure,
                             MANAGER_SOURCE=str(ROOT/'tests/helpers/longhorn-v2-manager-loss.py')))
                marker = 'LONGHORN_V2_MANAGER_LOSS_'+mode.upper()+'_PASS'
                calls = (base/'calls.txt').read_text().splitlines()
                if failure == 'none':
                    self.assertEqual(0, result.returncode, result.stdout+result.stderr)
                    self.assertIn(marker, result.stdout)
                    self.assertLess(calls.index('cleanup'), len(calls)-1-calls[::-1].index('volume-baseline'))
                    if mode == 'qualification': self.assertIn('verify', calls)
                    else:
                        self.assertNotIn('business', calls)
                        self.assertNotIn('LONGHORN_V2_MANAGER_LOSS_QUALIFICATION_PASS', result.stdout)
                else:
                    self.assertNotEqual(0, result.returncode)
                    self.assertNotIn(marker, result.stdout)
                if failure != 'baseline' or mode == 'cleanup': self.assertIn('cleanup', calls)

    def test_dispatch_keeps_fault_and_recovery_in_the_owned_product_volume_path(self):
        for suffix in ('qualification', 'cleanup'):
            mode = 'v2-manager-loss-'+suffix
            self.assertIn('--longhorn-'+mode+') longhorn_launch '+mode+' LONGHORN_V2_MANAGER_LOSS_'+suffix.upper()+'_PASS',
                          (ROOT/'tests/helpers/longhorn-runner.sh').read_text())
            self.assertIn(mode, (ROOT/'tests/helpers/longhorn-live.sh').read_text())
        self.assertIn('tests.unit.test_longhorn_v2_manager_loss', (ROOT/'tests/helpers/longhorn-static.sh').read_text())


if __name__ == '__main__':
    unittest.main()
