"""Product-entry, identity, supervisor and cleanup contracts for the V2 core."""
from contextlib import contextmanager
import copy
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
from unittest.mock import MagicMock, patch

import yaml

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location('v2_core_contract', ROOT/'tests/helpers/longhorn-v2-core.py')
CORE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CORE)
NODES = ('192.168.47.146', '192.168.47.147', '192.168.47.148')


def nodes():
    return [{'metadata': {'name': 'longhorn-node-' + ip.rsplit('.', 1)[1], 'uid': ip},
             'status': {'addresses': [{'type': 'InternalIP', 'address': ip}],
                        'conditions': [{'type': 'Ready', 'status': 'True'}]}} for ip in NODES]


class CoreContracts(unittest.TestCase):
    def test_core_identity_is_pinned_only_after_all_three_addresses_and_readiness_match(self):
        for failure in ('none', 'address', 'not-ready', 'uid', 'missing'):
            state = {}; actual = nodes()
            if failure == 'address': actual[0]['status']['addresses'][0]['address'] = 'foreign'
            if failure == 'not-ready': actual[2]['status']['conditions'][0]['status'] = 'False'
            if failure == 'uid': state.update(cluster_uid='replaced', node_uids={})
            if failure == 'missing': actual.pop()
            lab = SimpleNamespace(NODES=NODES, STATE=Path('/owned/state'), owner=lambda: state,
                                  get=lambda kind, *args: {'metadata': {'uid': 'system'}} if kind == 'namespace' else {'items': actual},
                                  write_private=MagicMock())
            with self.subTest(failure=failure):
                if failure == 'none':
                    with patch('sys.stdout', new_callable=io.StringIO) as output:
                        CORE.core_identity(lab)
                    self.assertEqual(state['cluster_uid'], 'system')
                    self.assertEqual(len(state['node_uids']), 3)
                    self.assertIn('nodes=3', output.getvalue())
                    lab.write_private.assert_called_once()
                else:
                    with self.assertRaises(ValueError): CORE.core_identity(lab)
                    lab.write_private.assert_not_called()

    def test_transport_evidence_must_match_current_v2_fingerprint(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory); target = base/'tests/evidence'; target.mkdir(parents=True)
            path = target/'longhorn-v2-registry-qualification-20261020.json'
            evidence = {'status': 'pass', 'rc': 0, 'failure_markers': 0, 'transport_fingerprint': 'current'}
            lab = SimpleNamespace(transport_fingerprint=lambda: 'current')
            with patch.object(CORE, 'BASE', base):
                path.write_text(json.dumps(evidence)); CORE.verify_transport(lab)
                for change in ({'rc': 1}, {'failure_markers': 1}, {'status': 'pending'}, {'transport_fingerprint': 'old'}):
                    path.write_text(json.dumps(dict(evidence, **change)))
                    with self.assertRaisesRegex(ValueError, 'focused qualification'): CORE.verify_transport(lab)

    def test_configure_qualifies_all_hosts_before_writing_and_pins_wwn_alias(self):
        for failure in ('none', 'cpu', 'memory', 'dirty'):
            host = {'online_cpus': '0-7', 'memory_kib': {'MemTotal': 8 * 1024**2},
                    'machine_id': 'machine', 'hostname': 'cloned-vm'}
            row = {'inventory': host, 'assessment': {'blockers': [], 'candidates': [
                {'by_id': ['/dev/disk/by-id/scsi-abc', '/dev/disk/by-id/wwn-abc'],
                 'identity': 'abc', 'size_bytes': 100 * 1024**3}]}}
            if failure == 'cpu': host['online_cpus'] = '0-1'
            if failure == 'memory': host['memory_kib']['MemTotal'] = 4 * 1024**2
            if failure == 'dirty': row['assessment']['blockers'] = ['dirty-disk']
            profile = SimpleNamespace(NODES=NODES, configure=MagicMock())
            state = {}; lab = SimpleNamespace(owner=lambda: state, STATE='/owned/state', write_private=MagicMock())
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory, patch.object(
                    CORE, 'load', return_value=SimpleNamespace(collect=lambda _: copy.deepcopy(row))):
                lab.DIRECTORY = Path(directory)
                config = lab.DIRECTORY/'config.yml'
                config.write_text('ENABLE_SETTING_HOSTNAME: false\nlonghorn_install: no\n')
                if failure == 'none':
                    CORE.configure(profile, lab)
                    self.assertEqual(set(state['raw_disks']), set(NODES))
                    self.assertTrue(all(d['path'] == '/dev/disk/by-id/wwn-abc' for d in state['raw_disks'].values()))
                    self.assertTrue(all(d['original_hostname'] == 'cloned-vm' for d in state['raw_disks'].values()))
                    self.assertIs(yaml.safe_load(config.read_text())['ENABLE_SETTING_HOSTNAME'], True)
                    profile.configure.assert_called_once_with(lab)
                else:
                    with self.assertRaises(ValueError): CORE.configure(profile, lab)
                    profile.configure.assert_not_called()
                    lab.write_private.assert_not_called()

    def test_supervisor_failure_stops_product_and_cannot_start_without_exact_relay(self):
        events = []
        @contextmanager
        def cleanup(callback):
            try: yield
            finally: callback()
        identity = ([str(Path(CORE.__file__).resolve()).encode(), b'relay-maintain'], 'start')
        lab = SimpleNamespace(CLUSTER='longhorn-v2-gate', owner=lambda: {},
                              process_identity=MagicMock(return_value=None), scoped_cleanup=cleanup,
                              stop_product=lambda *_: events.append('stopped'))
        with patch.object(CORE.subprocess, 'Popen') as popen:
            with self.assertRaisesRegex(ValueError, 'supervisor'): CORE.supervise(lab, 123, 'setup')
            popen.assert_not_called()
        process = SimpleNamespace(poll=lambda: None)
        lab.process_identity.side_effect = [identity, None]
        with patch.object(CORE.subprocess, 'Popen', return_value=process) as popen:
            with self.assertRaisesRegex(ValueError, 'supervisor exited'): CORE.supervise(lab, 123, 'setup')
        self.assertEqual(events, ['stopped'])
        self.assertEqual(popen.call_args.args[0][-3:], ['setup', 'longhorn-v2-gate', '90'])

    def test_cleanup_requires_original_three_machine_ids_and_no_storage_or_transport(self):
        for failure in ('none', 'machine', 'hostname', 'storage', 'relay', 'missing'):
            state = {'disks': {}, 'raw_disks': {ip: {'machine_id': 'original', 'original_hostname': 'cloned-vm'} for ip in NODES}}
            if failure == 'storage': state['namespace_uid'] = 'storage'
            if failure == 'relay': state['relays'] = [{}]
            if failure == 'missing': state['raw_disks'].pop(NODES[2])
            def remote(node, args):
                if args[0] == 'hostname':
                    return SimpleNamespace(stdout='foreign-hostname' if failure == 'hostname' else 'longhorn-node-' + node.rsplit('.', 1)[1])
                return SimpleNamespace(stdout='replacement' if failure == 'machine' else 'original')
            lab = SimpleNamespace(NODES=NODES, DIRECTORY=Path('/owned/v2'), owner=lambda: state,
                remote=MagicMock(side_effect=remote),
                execute=MagicMock(), archive_core=MagicMock())
            with self.subTest(failure=failure):
                if failure == 'none':
                    CORE.cleanup(lab)
                    self.assertEqual(lab.execute.call_count, 2)
                    self.assertEqual(lab.execute.call_args_list[0].args[0][-4:], ['--rocky-only', *NODES])
                    self.assertEqual(lab.execute.call_args_list[1].args[0][-5:], ['--verify', '--rocky-only', *NODES])
                    restores = [call.args[1] for call in lab.remote.call_args_list if call.args[1][0] == 'python3']
                    self.assertEqual(len(restores), 3)
                    self.assertTrue(all(args[-1] == 'cloned-vm' for args in restores))
                    lab.archive_core.assert_called_once()
                else:
                    with self.assertRaises(ValueError): CORE.cleanup(lab)
                    lab.execute.assert_not_called(); lab.archive_core.assert_not_called()

    def test_shell_preserves_first_failure_restores_control_config_and_keeps_successful_core(self):
        source = (ROOT/'tests/helpers/longhorn-v2-core.sh').read_text()
        for failure in ('none', 'configure', 'supervise-setup', 'smoke', 'relay-clean'):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory:
                base = Path(directory); (base/'.venv/bin').mkdir(parents=True)
                (base/'.venv/bin/python').symlink_to(sys.executable)
                (base/'tests/helpers').mkdir(parents=True)
                (base/'kube-bin').mkdir()
                (base/'kube-bin/kubectl').write_text('#!/bin/sh\nexit 0\n'); (base/'kube-bin/kubectl').chmod(0o755)
                (base/'tests/helpers/lab-wipe-nodes.sh').write_text('exit 0\n')
                (base/'tests/helpers/longhorn-v2-lab.py').write_text('')
                (base/'kubecli.py').write_text("from pathlib import Path\nPath('clusters/longhorn-v2-gate').mkdir(parents=True)\n")
                (base/'tests/helpers/longhorn-v2-core.py').write_text('''import os,signal,sys,time
mode=sys.argv[1]
if mode=='relay-maintain':
 signal.signal(signal.SIGTERM,lambda *_:sys.exit(0))
 while True: time.sleep(.01)
with open('calls.txt','a') as stream: stream.write(' '.join(sys.argv[1:])+'\\n')
if mode=='supervise': mode+='-'+sys.argv[-1]
if mode==os.environ['FAIL_MODE']: sys.exit(17)
''')
                config = base/'original-kubeconfig'; config.write_text('original-control-context')
                text = source.replace('BASE=/usr/local/kubeauto', 'BASE=' + str(base)).replace(
                    'control_config=/root/.kube/config', 'control_config=' + str(config)).replace(
                    '/var/tmp/kubeauto-longhorn-v2-core.', str(base/'work.'))
                result = subprocess.run(['bash', '-c', text, 'v2-core-contract', 'v2-core'],
                    env=dict(os.environ, LONGHORN_PARENT_LEASE_HELD='yes', FAIL_MODE=failure),
                    text=True, capture_output=True, timeout=15)
                calls = (base/'calls.txt').read_text().splitlines()
                self.assertEqual(config.read_text(), 'original-control-context')
                self.assertIn('relay-clean', calls)
                if failure == 'none':
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertIn('LONGHORN_V2_CORE_PASS', result.stdout)
                    self.assertNotIn('cleanup', calls)
                else:
                    self.assertEqual(result.returncode, 17, result.stderr)
                    self.assertNotIn('LONGHORN_V2_CORE_PASS', result.stdout)
                    if failure != 'relay-clean': self.assertIn('cleanup', calls)


if __name__ == '__main__':
    unittest.main()
