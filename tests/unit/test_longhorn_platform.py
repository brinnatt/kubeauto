"""LONGHORN-05's host-fault ownership, restoration and real-product contracts."""
import copy
from contextlib import contextmanager
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch
import uuid

import yaml

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location('platform_contract', ROOT/'tests/helpers/longhorn-platform.py')
PLATFORM = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PLATFORM)


class BaselineConfigContracts(unittest.TestCase):
    def test_empty_baseline_uses_the_product_v1_default_but_refuses_other_profiles(self):
        for missing, engines, valid in ((True, None, True), (False, ['v1'], True),
                (False, ['v2'], False), (False, ['v1','v2'], False),
                (False, None, False), (False, 'v1', False), (False, [], False)):
            with self.subTest(missing=missing, engines=engines), tempfile.TemporaryDirectory() as directory:
                config = {'longhorn_requests': []}
                if not missing: config['longhorn_data_engines'] = engines
                root = Path(directory); (root/'config.yml').write_text(yaml.safe_dump(config))
                lab = SimpleNamespace(DIRECTORY=root, get=lambda *a,**k: {'items': []})
                if valid: PLATFORM.empty(lab)
                else:
                    with self.assertRaisesRegex(ValueError, 'idle V1'): PLATFORM.empty(lab)


class ServiceContracts(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for name, text in {'etc/machine-id': 'machine', 'proc/sys/kernel/random/boot_id': 'boot',
                           'usr/lib/systemd/system/iscsid.service': '[Service]\nExecStart=/usr/sbin/iscsid\n',
                           'usr/lib/systemd/system/iscsid.socket': '[Socket]\nListenStream=3260\n'}.items():
            path = self.root/name; path.parent.mkdir(parents=True, exist_ok=True); path.write_text(text)
        (self.root/'run/systemd/system').mkdir(parents=True)
        self.mask = self.root/'run/systemd/system/iscsid.service'
        self.calls = []
        self.active = {'iscsid.service': 'active', 'iscsid.socket': 'active'}
        self.enabled = {'iscsid.service': 'enabled', 'iscsid.socket': 'static'}
        self.session_rc = 21
        self.fail_verb = None
        self.run_id = str(uuid.uuid4())
        self.request = {'run_id': self.run_id}
        self.mock = patch('subprocess.run', side_effect=self.command).start()
        self.addCleanup(patch.stopall)
        self.request['before'] = PLATFORM.service_action(self.request, 'inspect', self.root)

    def command(self, argv, **kwargs):
        self.calls.append(argv)
        if argv[0] == 'iscsiadm':
            return subprocess.CompletedProcess(argv, self.session_rc, '' if self.session_rc == 21 else 'foreign-session', '')
        self.assertEqual(argv[0], 'systemctl')
        verb = argv[1]
        if verb == self.fail_verb:
            return subprocess.CompletedProcess(argv, 1, '', 'injected command error')
        if verb == 'show':
            unit = argv[2]
            if '--value' in argv:
                return subprocess.CompletedProcess(argv, 0, 'masked\n' if self.mask.is_symlink() else 'loaded\n', '')
            values = {'LoadState': 'loaded', 'ActiveState': self.active[unit],
                      'UnitFileState': self.enabled[unit], 'FragmentPath': '/usr/lib/systemd/system/'+unit}
            return subprocess.CompletedProcess(argv, 0, '\n'.join(k+'='+v for k, v in values.items())+'\n', '')
        if verb in ('stop', 'start'):
            self.active[argv[2]] = 'inactive' if verb == 'stop' else 'active'
        else:
            self.assertEqual(verb, 'daemon-reload')
        return subprocess.CompletedProcess(argv, 0, '', '')

    def invoke(self, action):
        return PLATFORM.service_action(self.request, action, self.root)

    def test_complete_runtime_mask_restores_original_states_without_permanent_enablement(self):
        original = copy.deepcopy(self.active)
        result = self.invoke('inject')
        self.assertEqual(result['load_state'], 'masked')
        self.assertTrue(self.mask.is_symlink())
        self.assertEqual(self.mask.lstat().st_ino, result['mask_identity']['inode'])
        self.assertEqual(self.active, {'iscsid.service': 'inactive', 'iscsid.socket': 'inactive'})
        self.assertEqual(self.invoke('restore'), {'restored': True})
        self.assertEqual(self.invoke('restore'), {'restored': True})
        self.assertEqual(self.active, original)
        self.assertFalse(self.mask.is_symlink())
        self.assertFalse(list((self.root/'run').glob('kubeauto-longhorn-platform-*')))
        self.assertFalse(any(a[1] in ('enable', 'disable', 'mask', 'unmask') for a in self.calls if len(a) > 1))
        stops = [a[2] for a in self.calls if a[:2] == ['systemctl', 'stop']]
        self.assertEqual(stops[:2], ['iscsid.socket', 'iscsid.service'])

    def test_original_inactive_socket_is_restored_inactive(self):
        self.active['iscsid.socket'] = 'inactive'
        self.request['before'] = self.invoke('inspect')
        self.invoke('inject'); self.invoke('restore')
        self.assertEqual(self.active['iscsid.socket'], 'inactive')

    def test_foreign_mask_is_never_overwritten(self):
        os.symlink('/dev/null', self.mask)
        with self.assertRaisesRegex(ValueError, 'foreign runtime mask'):
            self.invoke('inject')
        self.assertEqual(os.readlink(self.mask), '/dev/null')
        self.assertFalse(any(a[1] == 'stop' for a in self.calls))

    def test_foreign_session_blocks_before_journal_and_stopping_units(self):
        self.session_rc = 0
        with self.assertRaisesRegex(ValueError, 'no iSCSI sessions'):
            self.invoke('inject')
        self.assertFalse(any(a[1] == 'stop' for a in self.calls))
        self.assertFalse(list((self.root/'run').glob('kubeauto-longhorn-platform-*')))

    def test_baseline_change_blocks_injection(self):
        self.active['iscsid.socket'] = 'inactive'
        with self.assertRaisesRegex(ValueError, 'baseline changed'):
            self.invoke('inject')
        self.assertFalse(any(a[1] == 'stop' for a in self.calls))

    def test_replaced_mask_inode_is_preserved_with_journal(self):
        self.invoke('inject')
        # Keep the old inode alive while replacing the path: inode reuse cannot
        # turn a foreign file into an owned fixture in this deterministic test.
        self.mask.unlink(); self.mask.write_text('foreign')
        with self.assertRaisesRegex(ValueError, 'foreign mask inode'):
            self.invoke('restore')
        self.assertEqual(self.mask.read_text(), 'foreign')
        self.assertTrue((self.root/('run/kubeauto-longhorn-platform-'+self.run_id+'.json')).exists())

    def test_changed_boot_or_fragment_blocks_restore_before_mask_removal(self):
        self.invoke('inject')
        path = self.root/'proc/sys/kernel/random/boot_id'; path.write_text('foreign')
        with self.assertRaisesRegex(ValueError, 'boot identity'):
            self.invoke('restore')
        self.assertTrue(self.mask.is_symlink())
        path.write_text('boot')
        (self.root/'usr/lib/systemd/system/iscsid.service').write_text('foreign unit')
        with self.assertRaisesRegex(ValueError, 'unit fragment changed'):
            self.invoke('restore')
        self.assertTrue(self.mask.is_symlink())

    def test_lost_link_response_is_recoverable_by_prejournaled_inode(self):
        real_link = os.link
        def lost_response(*args, **kwargs):
            real_link(*args, **kwargs)
            raise OSError('simulated lost response')
        with patch('os.link', side_effect=lost_response):
            with self.assertRaisesRegex(OSError, 'lost response'):
                self.invoke('inject')
        self.assertTrue(self.mask.is_symlink())
        self.invoke('restore')
        self.assertFalse(self.mask.is_symlink())
        self.assertEqual(self.active['iscsid.service'], 'active')

    def test_failed_stop_keeps_recoverable_intent_and_does_not_publish_mask(self):
        self.fail_verb = 'stop'
        with self.assertRaisesRegex(ValueError, 'command failed'):
            self.invoke('inject')
        self.assertFalse(self.mask.is_symlink())
        self.fail_verb = None
        self.invoke('restore')
        self.assertEqual(self.active['iscsid.service'], 'active')

    def test_foreign_journal_and_unsafe_enabled_state_are_rejected(self):
        self.enabled['iscsid.service'] = 'masked'
        with self.assertRaisesRegex(ValueError, 'masked, transitional'):
            self.invoke('inject')
        self.enabled['iscsid.service'] = 'enabled'
        self.invoke('inject')
        path = self.root/('run/kubeauto-longhorn-platform-'+self.run_id+'.json')
        row = json.loads(path.read_text()); row['run_id'] = str(uuid.uuid4()); path.write_text(json.dumps(row))
        with self.assertRaisesRegex(ValueError, 'journal or boot identity'):
            self.invoke('restore')
        self.assertTrue(self.mask.is_symlink())


class ProductContracts(unittest.TestCase):
    def test_fixed_dispatch_and_remote_route_reach_platform_owner(self):
        script = ('source "$1"\n'
                  'longhorn_launch() { printf "%s %s\\n" "$1" "$2"; }\n'
                  'longhorn_dispatch --longhorn-platform\n')
        result = subprocess.run(['bash', '-c', script, 'contract',
                                 str(ROOT/'tests/helpers/longhorn-runner.sh')],
                                text=True, capture_output=True, check=True)
        self.assertEqual(result.stdout, 'platform LONGHORN_PLATFORM_PASS\n')
        live = (ROOT/'tests/helpers/longhorn-live.sh').read_text()
        self.assertIn('platform|platform-propagation-qualification)\n    exec bash "$BASE/tests/helpers/longhorn-platform.sh" "$MODE"', live)
        runner = (ROOT/'tests/helpers/longhorn-runner.sh').read_text()
        self.assertEqual(runner.count('platform|core|disk-qualification'), 2)

    def topology(self):
        workers = ('192.168.122.212', '192.168.122.165', '192.168.122.238')
        nodes = ('192.168.122.135', '192.168.122.40', '192.168.122.72')+workers
        pods, native = [], []
        for ip in nodes:
            node = 'longhorn-node-'+ip.rsplit('.', 1)[1]
            if ip in workers: native.append({'metadata': {'name': node}, 'status': {'conditions': [{'type': 'MountPropagation', 'status': 'True'}]}})
            for app, mounts in [('longhorn-manager', {'longhorn': '/var/lib/longhorn/'}),
                                ('longhorn-csi-plugin', {'kubernetes-csi-dir': '/var/lib/kubelet/plugins/kubernetes.io/csi',
                                                         'pods-mount-dir': '/var/lib/kubelet/pods'})]:
                if app == 'longhorn-manager' and ip not in workers: continue
                pods.append({'metadata': {'name': app+node, 'uid': app+node, 'labels': {'app': app}},
                    'spec': {'nodeName': node, 'containers': [{'name': app, 'securityContext': {'privileged': True},
                        'volumeMounts': [{'name': k, 'mountPropagation': 'Bidirectional'} for k in mounts]}],
                        'volumes': [{'name': k, 'hostPath': {'path': v}} for k, v in mounts.items()]},
                    'status': {'conditions': [{'type': 'Ready', 'status': 'True'}]}})
        return SimpleNamespace(WORKERS=workers, NODES=nodes, get=lambda kind, **kwargs: {'items': native if kind == 'nodes.longhorn.io' else pods}), pods, native

    def test_propagation_verifies_each_actual_client_pod_and_host_path(self):
        lab, pods, native = self.topology()
        self.assertEqual(len(PLATFORM.propagation(lab)), 9)
        for mutate in (
                lambda: pods[1]['spec']['containers'][0]['volumeMounts'][0].update(mountPropagation='HostToContainer'),
                lambda: pods[1]['spec']['volumes'][0]['hostPath'].update(path='/wrong'),
                lambda: pods[0]['spec']['containers'][0]['securityContext'].update(privileged=False),
                lambda: native[0]['status']['conditions'][0].update(status='False'),
                lambda: pods.append(copy.deepcopy(pods[0])),
                lambda: pods.pop(0),
                lambda: pods[0]['spec'].update(nodeName='foreign-node')):
            lab, pods, native = self.topology()
            mutate()
            with self.assertRaises(ValueError):
                PLATFORM.propagation(lab)

    def test_clean_restores_service_before_business_and_input_without_claiming_success(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            row = {'run_id': 'owned', 'service': {'node': 'worker'}, 'service_intent': True, 'business_intent': True, 'reconciled': True}
            (base/PLATFORM.RECORD).write_text(json.dumps(row))
            calls = []
            lab = SimpleNamespace(DIRECTORY=base, WORKERS=('worker',), owner=lambda: {'run_id': 'owned'},
                restore_request_input=lambda: calls.append('input'), cleanup_business=lambda **kw: calls.append('business'))
            with patch.object(PLATFORM, 'remote_service', side_effect=lambda *a: calls.append('service')):
                PLATFORM.clean(lab, 10)
            self.assertEqual(calls, ['service', 'input', 'business'])
            self.assertTrue((base/PLATFORM.RECORD).exists())
            row['run_id'] = 'foreign'; (base/PLATFORM.RECORD).write_text(json.dumps(row))
            with self.assertRaisesRegex(ValueError, 'foreign platform journal'):
                PLATFORM.clean(lab, 10)

    def test_failure_cleanup_rerenders_original_input_and_archives_only_verified_recovery(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory); original = 'longhorn_data_path: /data/owned\n'
            (base/'config.yml').write_text('longhorn_data_path: /\n')
            row = {'run_id': 'owned', 'service': {'node': 'worker'}, 'service_intent': True,
                   'business_intent': False, 'reconciled': False, 'passed': False,
                   'original_config': original, 'hosts': {'original': True}, 'snapshot_identity': {'shared': True}}
            (base/PLATFORM.RECORD).write_text(json.dumps(row))
            calls = []
            def setup(*args):
                self.assertEqual((base/'config.yml').read_text(), original); calls.append('product-setup')
            lab = SimpleNamespace(DIRECTORY=base, WORKERS=('worker',), owner=lambda: {'run_id': 'owned'},
                restore_request_input=lambda: (calls.append('input'), (base/'config.yml').write_text(original)),
                supervise_core=setup, installation_health=lambda: calls.append('health'),
                write_private=lambda path, data: path.write_text(json.dumps(data)),
                shared_snapshot_identity=lambda: {'shared': True})
            with patch.object(PLATFORM, 'empty'), \
                 patch.object(PLATFORM, 'host_observation', return_value={'original': True}), \
                 patch.object(PLATFORM, 'remote_service', side_effect=lambda *a: calls.append('service')):
                PLATFORM.clean(lab, 10, failed=True)
            self.assertEqual(calls[:3], ['service', 'input', 'product-setup'])
            self.assertFalse((base/PLATFORM.RECORD).exists())
            recovered = list(base.glob('longhorn-platform-recovered-*.json'))
            self.assertEqual(len(recovered), 1)
            self.assertFalse(json.loads(recovered[0].read_text())['passed'])

    def test_remote_service_program_compiles_and_uses_only_owned_worker(self):
        def remote(node, argv, *, stdin, timeout):
            self.assertEqual(node, 'worker')
            self.assertEqual(argv[:2], ['python3', '-c'])
            compile(argv[2], '<platform-service-remote>', 'exec')
            self.assertEqual(json.loads(stdin)['action'], 'inspect')
            self.assertEqual(timeout, 240)
            return SimpleNamespace(stdout='{"safe": true}')
        self.assertEqual(PLATFORM.remote_service(SimpleNamespace(remote=remote),
                         {'run_id': str(uuid.uuid4()), 'node': 'worker'}, 'inspect'), {'safe': True})

    def test_complete_module_uses_config_kubecli_refusals_then_restored_positive_business(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory); original = 'longhorn_data_path: /data/owned\nlonghorn_data_engines: [v1]\nlonghorn_requests: []\n'
            (base/'config.yml').write_text(original)
            calls = []
            def write(path, row): path.write_text(json.dumps(row))
            def restore():
                record = base/'longhorn-request-input.json'
                if record.exists():
                    (base/'config.yml').write_text(json.loads(record.read_text())['original']); record.unlink()
            def setup(pid, step, **kwargs):
                calls.append(('setup', kwargs.get('expected_refusal')))
                if kwargs:
                    self.assertEqual(yaml.safe_load((base/'config.yml').read_text())['longhorn_data_path'], '/')
                else: self.assertEqual((base/'config.yml').read_text(), original)
            def business(*a, **kw):
                self.assertEqual((base/'config.yml').read_text(), original); calls.append(('real-business', a))
            @contextmanager
            def scoped_cleanup(cleanup):
                try: yield
                finally: cleanup()
            lab = SimpleNamespace(DIRECTORY=base, WORKERS=('worker',), owner=lambda: {'run_id': 'owned'},
                scoped_cleanup=scoped_cleanup, installation_health=MagicMock(), idempotence_snapshot=lambda ns: {'original': True},
                shared_snapshot_identity=lambda: {'shared': True}, write_private=write,
                supervise_core=setup, restore_request_input=restore, business=business, cleanup_business=MagicMock())
            def service(lab, request, action):
                calls.append(('service', action)); return {'before': True}
            with patch.object(PLATFORM, 'empty'), patch.object(PLATFORM, 'host_observation', return_value={'disk': 'original'}), \
                 patch.object(PLATFORM, 'propagation'), patch.object(PLATFORM, 'remote_service', side_effect=service), \
                 patch.object(PLATFORM, 'product_service_refusal', side_effect=lambda *a: calls.append(('service-refusal',))):
                PLATFORM.business(lab, 10)
                self.assertTrue(json.loads((base/PLATFORM.RECORD).read_text())['passed'])
                PLATFORM.finish(lab)
            self.assertFalse((base/PLATFORM.RECORD).exists())
            self.assertIn(('setup', PLATFORM.ROOT_REFUSAL), calls)
            self.assertIn(('setup', None), calls)
            self.assertIn(('real-business', ('install',)), calls)
            self.assertLess(calls.index(('service-refusal',)), calls.index(('setup', None)))

    def test_product_host_refusal_requires_nonzero_exact_task_node_and_masked_reason(self):
        valid = 'TASK [longhorn : Enable the iSCSI daemon]\nfatal: [worker]: FAILED! Unit iscsid.service is masked\n'
        identity = ([b'/owned/longhorn-lab.py', b'relay-maintain'], 'pid-start')
        lab = SimpleNamespace(__file__='/owned/longhorn-lab.py', BASE=Path('/owned'),
            DIRECTORY=Path('/owned'), CLUSTER='longhorn-gate', process_identity=lambda pid: identity,
            write_private=MagicMock(), stop_product=MagicMock())
        @contextmanager
        def cleanup(callback):
            try: yield
            finally: callback()
        lab.scoped_cleanup = cleanup
        for rc, output, accepted in ((1, valid, True), (0, valid, False),
                (1, valid.replace('worker', 'foreign'), False),
                (1, valid.replace('Enable the iSCSI daemon', 'Install packages'), False),
                (1, valid.replace('masked', 'network timeout'), False)):
            def launch(command, **kwargs):
                self.assertEqual(command[-3:], ['setup', 'longhorn-gate', '09'])
                self.assertTrue(kwargs['start_new_session'])
                kwargs['stdout'].write(output); kwargs['stdout'].flush()
                return SimpleNamespace(returncode=rc, poll=lambda: rc)
            with self.subTest(rc=rc, output=output), patch.object(PLATFORM.subprocess, 'Popen', side_effect=launch):
                if accepted: PLATFORM.product_service_refusal(lab, 10, 'worker')
                else:
                    with self.assertRaisesRegex(ValueError, 'did not refuse'):
                        PLATFORM.product_service_refusal(lab, 10, 'worker')

    def test_shell_failure_preserves_first_exit_and_emits_no_pass(self):
        for fail in ('business', 'finish', 'none'):
            with self.subTest(fail=fail), tempfile.TemporaryDirectory() as directory:
                base = Path(directory); (base/'.venv/bin').mkdir(parents=True)
                os.symlink(sys.executable, base/'.venv/bin/python')
                (base/'tests/helpers').mkdir(parents=True); (base/'kube-bin').mkdir()
                log = base/'calls'
                common = "import os,signal,sys,time\nfrom pathlib import Path\na=sys.argv[1]\nwith open(os.environ['CONTRACT_LOG'],'a') as f:f.write(Path(__file__).name+' '+a+'\\n')\n"
                (base/'tests/helpers/longhorn-lab.py').write_text(common+"if a=='relay-maintain':\n signal.signal(signal.SIGTERM,lambda *x:sys.exit(0))\n while True:time.sleep(.1)\n")
                (base/'tests/helpers/longhorn-platform.py').write_text(common+"time.sleep(.15)\nif a==os.environ['CONTRACT_FAIL']:sys.exit(17)\n")
                kubectl = base/'kube-bin/kubectl'; kubectl.write_text('#!/bin/sh\nexit 0\n'); kubectl.chmod(0o755)
                script = base/'run.sh'; script.write_text((ROOT/'tests/helpers/longhorn-platform.sh').read_text().replace('BASE=/usr/local/kubeauto', 'BASE='+str(base)))
                env = dict(os.environ, LONGHORN_PARENT_LEASE_HELD='yes', CONTRACT_LOG=str(log), CONTRACT_FAIL=fail)
                result = subprocess.run(['bash', str(script)], text=True, capture_output=True, env=env, timeout=15)
                durable = next(line.split('path=', 1)[1] for line in result.stdout.splitlines() if line.startswith('LONGHORN_PLATFORM_DURABLE_DIRECTORY'))
                try:
                    self.assertEqual(result.returncode, 0 if fail == 'none' else 17)
                    self.assertEqual((Path(durable)/'exit').read_text().strip(), str(result.returncode))
                    if fail == 'none':
                        self.assertIn('LONGHORN_PLATFORM_SCOPED_CLEAN_PASS', result.stdout)
                        self.assertIn('LONGHORN_PLATFORM_PASS', result.stdout)
                    else:
                        self.assertNotIn('LONGHORN_PLATFORM_PASS', result.stdout)
                        self.assertIn('LONGHORN_FIRST_FAILURE', result.stderr)
                        lines = log.read_text().splitlines()
                        self.assertLess(lines.index('longhorn-platform.py clean'), len(lines)-1)
                        self.assertEqual(lines[-1], 'longhorn-lab.py verify-core')
                finally:
                    shutil.rmtree(durable)


if __name__ == '__main__':
    unittest.main()
