"""Native interrupt identity, safe configuration recovery and result contracts."""
import copy
from contextlib import contextmanager
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shlex
from types import SimpleNamespace
import tempfile
import subprocess
import unittest
from unittest.mock import Mock, patch

import yaml

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location('interrupt_contract', ROOT/'tests/helpers/longhorn-v2-interrupt.py')
GATE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(GATE)


class InterruptContracts(unittest.TestCase):
    def fixture(self, directory):
        objects = {'namespace': {'metadata': {'uid': 'installation-ns'}},
                   'settings.longhorn.io': {'metadata': {'uid': 'setting'}, 'value': '{"v2":"false"}'}}
        managers = []
        for index in range(3):
            name = 'im'+str(index)
            node = 'longhorn-node-'+str(146+index)
            managers.append({'metadata': {'name': name, 'uid': name+'-uid'},
                'spec': {'dataEngine': 'v2', 'type': 'aio', 'nodeID': node, 'image': 'pinned-image'},
                'status': {'currentState': 'running', 'dataEngineStatus': {'v2': {'interruptModeEnabled': 'false'}}}})
            objects[('pod', name)] = {'metadata': {'uid': name+'-pod', 'ownerReferences': [
                {'kind': 'InstanceManager', 'uid': name+'-uid', 'controller': True}]},
                'spec': {'nodeName': node, 'containers': [{'name': 'instance-manager', 'image': 'pinned-image',
                    'args': ['--spdk-enabled']}], 'volumes': [{'secret': {'secretName': 'longhorn-grpc-tls'}}]},
                'status': {'phase': 'Running', 'conditions': [{'type': 'Ready', 'status': 'True'}]}}
        objects['instancemanagers.longhorn.io'] = {'items': managers}
        owner = {'run_id': 'run', 'cluster_uid': 'cluster', 'namespace_uid': 'installation-ns'}
        def get(kind, name='', namespace=''):
            return copy.deepcopy(objects.get((kind, name), objects.get(kind)))
        lab = SimpleNamespace(DIRECTORY=Path(directory), get=Mock(side_effect=get), owner=Mock(return_value=owner),
            WORKERS=['192.168.47.'+str(i) for i in range(146, 149)], check_business_graph=Mock(),
            remove=Mock(), cleanup_business=Mock(), installation_health=Mock())
        return lab, objects

    def journal(self, lab, objects):
        original = GATE.components(lab, False)
        config = yaml.safe_dump({'longhorn_data_engines': ['v2'], 'longhorn_v2_interrupt_mode': False,
                                 'longhorn_requests': []}, sort_keys=False)
        (lab.DIRECTORY/'config.yml').write_text(config)
        business = {'namespace': 'business', 'namespace_uid': 'business-ns', 'volume': 'volume',
                    'volume_uid': 'volume-uid', 'objects': {'Pod/writer': 'client-uid'}}
        row = {'run_id': 'run', 'cluster_uid': 'cluster', 'installation_namespace_uid': 'installation-ns',
               'namespace': 'business', 'namespace_uid': 'business-ns', 'volume_uid': 'volume-uid',
               'original_config': config, 'original': original}
        for name, value in ((GATE.RECORD, row), (GATE.BUSINESS, business)):
            (lab.DIRECTORY/name).write_text(json.dumps(value))
        objects['volumes.longhorn.io'] = {'items': [{'metadata': {'name': 'volume', 'uid': 'volume-uid'},
            'spec': {'dataEngine': 'v2', 'nodeID': ''}, 'status': {'state': 'detached'}}]}
        for kind in ('engines.longhorn.io', 'enginefrontends.longhorn.io', 'replicas.longhorn.io'):
            objects[kind] = {'items': []}
        return row, business

    def test_components_observe_native_status_and_pod_flags_with_original_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            lab, objects = self.fixture(directory)
            original = GATE.components(lab, False)
            self.assertEqual(3, len(original['managers']))
            self.assertIsNone(GATE.components(lab, True, original))
            objects['settings.longhorn.io']['value'] = '{"v2":"true"}'
            for index, im in enumerate(objects['instancemanagers.longhorn.io']['items']):
                im['status']['dataEngineStatus']['v2']['interruptModeEnabled'] = 'true'
                pod = objects[('pod', 'im'+str(index))]
                pod['spec']['containers'][0]['args'].append('--spdk-interrupt-mode')
                pod['metadata']['uid'] += '-new'
            enabled = GATE.components(lab, True, original)
            self.assertEqual(3, len(enabled['managers']))
            self.assertNotEqual(original['managers']['im0']['pod_uid'], enabled['managers']['im0']['pod_uid'])

    def test_replaced_unready_or_unqualified_native_components_never_converge(self):
        defects = ('namespace', 'setting', 'manager', 'spec', 'count', 'nodes', 'engine', 'driver',
                   'pod-owner', 'pod-node', 'pod-image', 'pod-tls', 'pod-spdk', 'pod-name',
                   'not-ready', 'native-state', 'runtime-status', 'flag')
        for defect in defects:
            with self.subTest(defect=defect), tempfile.TemporaryDirectory() as directory:
                lab, objects = self.fixture(directory)
                original = GATE.components(lab, False)
                im = objects['instancemanagers.longhorn.io']['items'][0]
                pod = objects[('pod', 'im0')]
                if defect == 'namespace': objects['namespace']['metadata']['uid'] = 'foreign'
                if defect == 'setting': objects['settings.longhorn.io']['metadata']['uid'] = 'foreign'
                if defect == 'manager': im['metadata']['uid'] = 'foreign'
                if defect == 'spec': im['spec']['image'] = 'unreviewed'
                if defect == 'count': objects['instancemanagers.longhorn.io']['items'].pop()
                if defect == 'nodes': im['spec']['nodeID'] = 'foreign'
                if defect == 'engine': im['spec']['dataEngine'] = 'v1'
                if defect == 'driver': im['spec']['type'] = 'e'
                if defect == 'pod-owner': pod['metadata']['ownerReferences'][0]['uid'] = 'foreign'
                if defect == 'pod-node': pod['spec']['nodeName'] = 'foreign'
                if defect == 'pod-image': pod['spec']['containers'][0]['image'] = 'foreign'
                if defect == 'pod-tls': pod['spec']['volumes'] = []
                if defect == 'pod-spdk': pod['spec']['containers'][0]['args'] = []
                if defect == 'pod-name': pod['spec']['containers'][0]['name'] = 'foreign'
                if defect == 'not-ready': pod['status']['phase'] = 'Pending'
                if defect == 'native-state': im['status']['currentState'] = 'error'
                if defect == 'runtime-status': im['status']['dataEngineStatus']['v2']['interruptModeEnabled'] = 'true'
                if defect == 'flag': pod['spec']['containers'][0]['args'].append('--spdk-interrupt-mode')
                if defect in ('not-ready', 'native-state', 'runtime-status', 'flag'):
                    self.assertIsNone(GATE.components(lab, False, original))
                else:
                    with self.assertRaises(ValueError): GATE.components(lab, False, original)

    def test_installation_and_business_namespace_uids_are_distinct_recovery_authorities(self):
        for defect in ('none', 'run_id', 'cluster_uid', 'installation_namespace_uid', 'namespace_uid', 'volume_uid', 'config', 'symlink'):
            with self.subTest(defect=defect), tempfile.TemporaryDirectory() as directory:
                lab, objects = self.fixture(directory)
                row, business = self.journal(lab, objects)
                if defect == 'none':
                    self.assertEqual((row, business), GATE.load_journal(lab))
                    continue
                if defect == 'config': row['original_config'] = row['original_config'].replace('false', 'true')
                elif defect == 'symlink':
                    path = lab.DIRECTORY/GATE.RECORD
                    path.rename(lab.DIRECTORY/'foreign.json')
                    path.symlink_to(lab.DIRECTORY/'foreign.json')
                else: row[defect] = 'foreign'
                if defect != 'symlink': (lab.DIRECTORY/GATE.RECORD).write_text(json.dumps(row))
                with self.assertRaises(ValueError): GATE.load_journal(lab)

    def test_detached_requires_original_volume_and_no_running_or_foreign_native_instance(self):
        for defect in ('none', 'attached', 'desired-node', 'uid', 'foreign-volume', 'running', 'foreign-instance'):
            with self.subTest(defect=defect), tempfile.TemporaryDirectory() as directory:
                lab, objects = self.fixture(directory)
                _, business = self.journal(lab, objects)
                volume = objects['volumes.longhorn.io']['items'][0]
                if defect == 'attached': volume['status']['state'] = 'attached'
                if defect == 'desired-node': volume['spec']['nodeID'] = 'node'
                if defect == 'uid': volume['metadata']['uid'] = 'other'
                if defect == 'foreign-volume': objects['volumes.longhorn.io']['items'].append(copy.deepcopy(volume))
                if defect in ('running', 'foreign-instance'):
                    objects['replicas.longhorn.io']['items'] = [{'spec': {'volumeName': 'other' if defect == 'foreign-instance' else 'volume'},
                                                               'status': {'currentState': 'running'}}]
                if defect in ('uid', 'foreign-volume', 'foreign-instance'):
                    with self.assertRaises(ValueError): GATE.detached(lab, business)
                else: self.assertEqual(defect == 'none', GATE.detached(lab, business))

    def test_product_uses_config_and_supervised_kubecli_and_restores_input_on_error(self):
        for failure in (False, True):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory:
                lab, objects = self.fixture(directory)
                row, _ = self.journal(lab, objects)
                path = lab.DIRECTORY/'config.yml'
                receipt = lab.DIRECTORY/'longhorn-request-input.json'
                def write_private(target, data): target.write_text(json.dumps(data))
                def restore():
                    path.write_text(json.loads(receipt.read_text())['original'])
                    receipt.unlink()
                @contextmanager
                def cleanup(callback):
                    try: yield
                    finally: callback()
                def supervise(pid, step, **kwargs):
                    self.assertEqual((123, 'longhorn-setup'), (pid, step))
                    self.assertEqual(True, yaml.safe_load(path.read_text())['longhorn_v2_interrupt_mode'])
                    self.assertEqual(0o600, path.stat().st_mode & 0o777)
                    self.assertEqual(row['original_config'], json.loads(receipt.read_text())['original'])
                    if failure: raise RuntimeError('stopped product')
                lab.write_private = write_private
                lab.restore_request_input = restore
                lab.scoped_cleanup = cleanup
                lab.supervise_core = Mock(side_effect=supervise)
                if failure:
                    with self.assertRaises(RuntimeError): GATE.product(lab, 123, True)
                else: GATE.product(lab, 123, True)
                self.assertEqual(row['original_config'], path.read_text())
                self.assertFalse(receipt.exists())

    def test_product_refuses_replaced_identity_or_attached_unsafe_change_before_any_write(self):
        for defect in ('setting', 'manager', 'attached', 'config', 'receipt'):
            with self.subTest(defect=defect), tempfile.TemporaryDirectory() as directory:
                lab, objects = self.fixture(directory)
                row, _ = self.journal(lab, objects)
                lab.write_private = Mock()
                lab.supervise_core = Mock()
                if defect == 'setting': objects['settings.longhorn.io']['metadata']['uid'] = 'other'
                if defect == 'manager': objects['instancemanagers.longhorn.io']['items'][0]['metadata']['uid'] = 'other'
                if defect == 'attached': objects['volumes.longhorn.io']['items'][0]['status']['state'] = 'attached'
                if defect == 'config': (lab.DIRECTORY/'config.yml').write_text(row['original_config']+'# changed\n')
                if defect == 'receipt': (lab.DIRECTORY/'longhorn-request-input.json').write_text('{}')
                with self.assertRaises(ValueError): GATE.product(lab, 123, True)
                lab.write_private.assert_not_called()
                lab.supervise_core.assert_not_called()

    def test_unknown_or_replaced_recovery_retains_journal_and_never_cleans_business(self):
        for defect in ('input', 'receipt', 'components', 'restore'):
            with self.subTest(defect=defect), tempfile.TemporaryDirectory() as directory:
                lab, objects = self.fixture(directory)
                self.journal(lab, objects)
                lab.restore_request_input = Mock()
                lab.wait = Mock(side_effect=lambda _, __, check: check())
                if defect == 'input': (lab.DIRECTORY/'config.yml').write_text('foreign')
                if defect == 'receipt': (lab.DIRECTORY/'longhorn-request-input.json').write_text('{"original":"foreign"}')
                observed = patch.object(GATE, 'components', side_effect=ValueError('replaced')) if defect == 'components' else patch.object(
                    GATE, 'components', return_value={'setting_value': '{"v2":"false"}'})
                with patch.object(GATE, 'stop_clients') as stop, patch.object(GATE, 'product') as product, observed:
                    if defect == 'restore': product.side_effect = RuntimeError('product failed')
                    with self.assertRaises((ValueError, RuntimeError)): GATE.cleanup(lab, 123)
                    if defect in ('input', 'receipt'): stop.assert_not_called(); product.assert_not_called()
                self.assertTrue((lab.DIRECTORY/GATE.RECORD).exists())
                lab.cleanup_business.assert_not_called()

    def test_recovery_restores_native_profile_before_business_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            lab, objects = self.fixture(directory)
            row, _ = self.journal(lab, objects)
            calls = []
            lab.wait = Mock(side_effect=lambda _, __, check: check())
            lab.installation_health.side_effect = lambda: calls.append('health')
            def clean(**kwargs):
                self.assertFalse((lab.DIRECTORY/GATE.RECORD).exists())
                self.assertEqual({'relay_pid': 123}, kwargs)
                calls.append('business-clean')
            lab.cleanup_business.side_effect = clean
            with patch.object(GATE, 'stop_clients', side_effect=lambda *_: calls.append('detach')), patch.object(
                    GATE, 'product', side_effect=lambda _, pid, enabled: calls.append(('product', pid, enabled))), patch.object(
                    GATE, 'components', return_value=row['original']):
                GATE.cleanup(lab, 123)
            self.assertEqual(['detach', ('product', 123, False), 'health', 'business-clean'], calls)

    def test_missing_relay_allows_no_profile_journal_but_never_stops_a_journaled_client(self):
        with tempfile.TemporaryDirectory() as directory:
            lab, objects = self.fixture(directory)
            GATE.cleanup(lab, 0)
            lab.cleanup_business.assert_called_once_with(relay_pid=0)
            lab.cleanup_business.reset_mock()
            self.journal(lab, objects)
            with patch.object(GATE, 'stop_clients') as stop, patch.object(GATE, 'product') as product:
                with self.assertRaisesRegex(ValueError, 'live relay'):
                    GATE.cleanup(lab, 0)
                stop.assert_not_called()
                product.assert_not_called()
            self.assertTrue((lab.DIRECTORY/GATE.RECORD).exists())
            lab.cleanup_business.assert_not_called()

    def test_cleanup_cli_can_reach_no_change_cleanup_when_relay_never_started(self):
        lab = SimpleNamespace(require_lease=Mock())
        fixture = SimpleNamespace(fixture=lambda: (None, lab))
        with patch.object(GATE, 'load', return_value=fixture), patch.object(GATE, 'cleanup') as cleanup, patch(
                'sys.argv', ['interrupt', 'cleanup', '--relay-pid', '0']):
            GATE.main()
        cleanup.assert_called_once_with(lab, 0)

    def test_client_replacement_anywhere_stops_before_deleting_the_first_client(self):
        with tempfile.TemporaryDirectory() as directory:
            lab, objects = self.fixture(directory)
            _, business = self.journal(lab, objects)
            business['objects'] = {'Pod/a': 'a', 'Pod/b': 'b'}
            objects[('pod', 'a')] = {'metadata': {'uid': 'a'}}
            objects[('pod', 'b')] = {'metadata': {'uid': 'foreign'}}
            lab.load_helper = Mock()
            with self.assertRaises(ValueError): GATE.stop_clients(lab, business)
            lab.remove.assert_not_called()
            lab.load_helper.assert_not_called()

    def result(self):
        with tempfile.TemporaryDirectory() as directory:
            lab, _ = self.fixture(directory)
            original = GATE.components(lab, False)
        active, restored = copy.deepcopy(original), copy.deepcopy(original)
        active['setting_value'] = '{"v2":"true"}'
        for name in original['managers']:
            active['managers'][name]['pod_uid'] += '-active'
            active['managers'][name]['args'].append('--spdk-interrupt-mode')
            restored['managers'][name]['pod_uid'] += '-restored'
        faults = GATE.load('longhorn-faults.py')
        token = 'run:interrupt'
        records = [{'type': 'ack', 'sequence': i, 'sha256': hashlib.sha256(faults.ledger_block(token, i)).hexdigest()}
                   for i in range(4)]
        ledger = GATE.load('longhorn-control-faults.py').confirmed(records, token, faults)
        row = {'volume_uid': 'volume', 'payload_bytes': 3*262144, 'payload_sha256': hashlib.sha256(b'run'*262144).hexdigest(),
               'replicas': {str(i): {'node': str(i), 'uid': str(i)} for i in range(3)},
               'interrupt_readback': True, 'polling_readback': True, 'ledger_cross_node_readback': True,
               'original': original, 'interrupt': active, 'restored': restored, 'ledger': ledger}
        def log():
            return 'LONGHORN_V2_INTERRUPT_RESULT '+json.dumps(row)+'\n'+''.join(m+'\n' for m in (
                'LONGHORN_V2_INTERRUPT_ATTACHED_REFUSED', 'LONGHORN_V2_INTERRUPT_ORIGINAL_PROFILE_RESTORED',
                'LONGHORN_V2_INTERRUPT_BUSINESS_PASS', 'LONGHORN_BUSINESS_FIXTURES_CLEAN_PASS'))
        return row, log

    def test_result_requires_real_pod_roundtrip_original_identities_acknowledgements_and_cleanup(self):
        row, log = self.result()
        self.assertEqual(row, GATE.verify_log(log()))
        for defect in ('hash', 'length', 'replica', 'same-pod', 'setting', 'manager', 'flags', 'old-flags',
                       'ledger', 'readback', 'cleanup', 'refusal', 'duplicate', 'failure'):
            with self.subTest(defect=defect):
                row, log = self.result()
                if defect == 'hash': row['payload_sha256'] = '0'*64
                if defect == 'length': row['payload_bytes'] = 0
                if defect == 'replica': row['replicas']['1']['node'] = '0'
                if defect == 'same-pod': row['interrupt']['managers']['im0']['pod_uid'] = row['original']['managers']['im0']['pod_uid']
                if defect == 'setting': row['interrupt']['setting_uid'] = 'other'
                if defect == 'manager': row['interrupt']['managers']['im0']['uid'] = 'other'
                if defect == 'flags': row['interrupt']['managers']['im0']['args'].remove('--spdk-interrupt-mode')
                if defect == 'old-flags': row['restored']['managers']['im0']['args'].append('--foreign')
                if defect == 'ledger': row['ledger']['records'][0]['sha256'] = '0'*64
                if defect == 'readback': row['ledger_cross_node_readback'] = False
                text = log()
                if defect == 'cleanup': text = text.replace('LONGHORN_BUSINESS_FIXTURES_CLEAN_PASS', '')
                if defect == 'refusal': text = text.replace('LONGHORN_V2_INTERRUPT_ATTACHED_REFUSED', '')
                if defect == 'duplicate': text += text.splitlines()[0]+'\n'
                if defect == 'failure': text += 'LONGHORN_FIRST_FAILURE phase=restore rc=1\n'
                with self.assertRaises(ValueError): GATE.verify_log(text)

    def test_real_shell_uses_verifier_and_recovery_entry_reaches_journal_before_empty_baseline(self):
        source = (ROOT/'tests/helpers/longhorn-v2-volumes.sh').read_text()
        for recovery in (False, True):
            for failure in ('none', 'relay', 'business', 'bad-result', 'cleanup', 'core'):
                if recovery and failure in ('business', 'bad-result'):
                    continue
                with self.subTest(recovery=recovery, failure=failure), tempfile.TemporaryDirectory() as directory:
                    base = Path(directory)
                    (base/'.venv/bin').mkdir(parents=True)
                    interpreter = base/'.venv/bin/python'
                    # A symlink under this fake venv loses the real pyvenv.cfg
                    # and its pinned PyYAML. Invoke the actual venv interpreter.
                    interpreter.write_text('#!/bin/sh\nexec '+shlex.quote(os.sys.executable)+' "$@"\n')
                    interpreter.chmod(0o755)
                    (base/'tests/helpers').mkdir(parents=True)
                    (base/'kube-bin').mkdir()
                    kubectl = base/'kube-bin/kubectl'
                    kubectl.write_text('#!/bin/sh\nexit 0\n')
                    kubectl.chmod(0o755)
                    row, log = self.result()
                    if failure == 'bad-result': row['ledger_cross_node_readback'] = False
                    (base/'observation.log').write_text(log())
                    stub = '''import os,signal,sys,time
mode=sys.argv[1]
if mode=='relay-maintain':
 signal.signal(signal.SIGTERM,lambda *_:sys.exit(0))
 while True:time.sleep(.01)
with open('calls.txt','a') as stream:stream.write(mode+'\\n')
if mode=='business-clean':sys.exit(99)
if (mode=='verify-core' and os.environ['FAIL_MODE']=='core') or (mode=='relay-ensure' and os.environ['FAIL_MODE']=='relay'):sys.exit(17)
'''
                    for name in ('core', 'install'):
                        (base/f'tests/helpers/longhorn-v2-{name}.py').write_text(stub)
                    (base/'tests/helpers/longhorn-v2-interrupt.py').write_text('''import os,runpy,sys
from pathlib import Path
mode=sys.argv[1]
with open('calls.txt','a') as stream:stream.write('interrupt-'+mode+'\\n')
if mode=='verify':runpy.run_path(os.environ['INTERRUPT_SOURCE'],run_name='__main__')
elif mode=='business':
 if os.environ['FAIL_MODE']=='business':sys.exit(17)
 print(Path('observation.log').read_text())
elif mode=='cleanup':
 if os.environ['FAIL_MODE']=='cleanup':sys.exit(17)
else:sys.exit(18)
''')
                    script = source.replace('BASE=/usr/local/kubeauto', 'BASE='+str(base)).replace(
                        '/var/tmp/kubeauto-longhorn-v2-volumes.', str(base/'work.'))
                    mode = 'v2-interrupt-cleanup' if recovery else 'v2-interrupt-qualification'
                    result = subprocess.run(['bash', '-c', script, 'contract', mode], text=True,
                        capture_output=True, timeout=15, env=dict(os.environ, LONGHORN_PARENT_LEASE_HELD='yes',
                        FAIL_MODE=failure, INTERRUPT_SOURCE=str(ROOT/'tests/helpers/longhorn-v2-interrupt.py')))
                    calls = (base/'calls.txt').read_text().splitlines()
                    self.assertIn('interrupt-cleanup', calls)
                    if recovery:
                        self.assertNotIn('interrupt-business', calls)
                        self.assertNotIn('interrupt-verify', calls)
                        self.assertNotIn('LONGHORN_V2_INTERRUPT_QUALIFICATION_PASS', result.stdout)
                        if 'volume-baseline' in calls:
                            self.assertLess(calls.index('interrupt-cleanup'), calls.index('volume-baseline'))
                    if failure == 'none':
                        self.assertEqual(0, result.returncode, result.stdout+result.stderr)
                        marker = 'LONGHORN_'+mode.upper().replace('-', '_')
                        self.assertIn(marker+'_SCOPED_CLEAN_PASS', result.stdout)
                        self.assertIn(marker+'_PASS', result.stdout)
                        if not recovery: self.assertIn('interrupt-verify', calls)
                    else:
                        self.assertNotEqual(0, result.returncode)
                        self.assertNotIn('LONGHORN_'+mode.upper().replace('-', '_')+'_PASS', result.stdout)

    def test_dispatch_and_static_contracts_keep_qualification_in_fixed_leased_product_gate(self):
        runner = (ROOT/'tests/helpers/longhorn-runner.sh').read_text()
        live = (ROOT/'tests/helpers/longhorn-live.sh').read_text()
        static = (ROOT/'tests/helpers/longhorn-static.sh').read_text()
        for mode in ('v2-interrupt-cleanup', 'v2-interrupt-qualification'):
            self.assertIn('--longhorn-'+mode+') longhorn_launch '+mode+' LONGHORN_'+mode.upper().replace('-', '_')+'_PASS', runner)
            self.assertIn(mode+'|', live)
        self.assertIn('tests.unit.test_longhorn_v2_interrupt', static)
        self.assertIn('tests/helpers/longhorn-v2-interrupt.py', static)


if __name__ == '__main__':
    unittest.main()
