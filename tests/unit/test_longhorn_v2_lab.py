"""Isolated V2 fixture/transport contracts before any remote mutation."""
import importlib.util
import io
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

import yaml

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location('v2_lab_contract', ROOT/'tests/helpers/longhorn-v2-lab.py')
V2 = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(V2)


class V2FixtureContracts(unittest.TestCase):
    def load(self):
        with patch.object(V2, 'BASE', ROOT):
            return V2.fixture()

    def test_profile_keeps_v1_constants_and_transport_fingerprint_unchanged(self):
        spec = importlib.util.spec_from_file_location('original_v1_lab', ROOT/'tests/helpers/longhorn-lab.py')
        v1 = importlib.util.module_from_spec(spec); spec.loader.exec_module(v1)
        fingerprint = v1.transport_fingerprint()
        v2 = self.load()
        self.assertEqual(v1.transport_fingerprint(), fingerprint)
        self.assertEqual(v1.CLUSTER, 'longhorn-gate')
        self.assertEqual(len(v1.NODES), 6)
        self.assertEqual(v1.PORT, 23520)
        self.assertEqual(v2.NODES, V2.NODES)
        self.assertEqual(v2.WORKERS, V2.NODES)
        self.assertEqual(v2.PORT, 23520)
        self.assertEqual(v2.DIRECTORY, Path('/usr/local/kubeauto/clusters/longhorn-v2-gate'))
        self.assertNotEqual(v2.transport_fingerprint(), fingerprint)

    def test_actual_listener_probe_accepts_profile_port_without_host_mutation(self):
        lab = self.load()
        with patch('sys.stdin', io.StringIO(json.dumps({'port': lab.PORT, 'action': 'probe'}))), \
                patch('sys.stdout', new_callable=io.StringIO) as output, \
                patch('subprocess.check_output', return_value='') as command:
            exec(compile(lab.listener_script(), 'v2-listener-probe', 'exec'), {})
        self.assertIsNone(json.loads(output.getvalue()))
        self.assertEqual(command.call_args.args[0], ['ss', '-H', '-lntp', 'sport = :23520'])

    def test_minimal_ubuntu_probe_uses_python_and_explicitly_bypasses_proxies(self):
        lab = SimpleNamespace(remote=MagicMock(return_value=SimpleNamespace(returncode=0)))
        self.assertTrue(V2.relay_probe(lab, V2.NODES[0]))
        args = lab.remote.call_args.args[1]
        self.assertEqual(args[:2], ['python3', '-c'])
        opener = MagicMock()
        opener.open.return_value.__enter__.return_value.status = 200
        with patch('urllib.request.build_opener', return_value=opener), \
                patch('urllib.request.ProxyHandler') as proxy:
            exec(compile(args[2], 'v2-registry-http-probe', 'exec'), {})
        proxy.assert_called_once_with({})
        self.assertEqual(opener.open.call_args.kwargs, {'timeout': 5})

    def test_three_node_relay_journals_before_connection_and_rechecks_http(self):
        lab = self.load()
        with tempfile.TemporaryDirectory() as directory:
            state = {'relays': []}; calls = []
            def connect(row):
                self.assertEqual(calls[-1], ('write', row['node']))
                calls.append(('connect', row['node']))
            def remote(node, argv=None, **kwargs):
                return SimpleNamespace(returncode=1 if argv and '-C' in argv else 0)
            with patch.object(V2, 'DIRECTORY', Path(directory)), \
                    patch.object(V2, 'relay_probe', return_value=True) as probe, \
                    patch.object(lab, 'owner', return_value=state), \
                    patch.object(lab, 'remote', side_effect=remote), \
                    patch.object(lab, 'relay_connect', side_effect=connect), \
                    patch.object(lab, 'bind_listener'), \
                    patch.object(lab, 'write_private', side_effect=lambda *_: calls.append(('write', state['relays'][-1]['node']))), \
                    patch('sys.stdout', new_callable=io.StringIO) as output:
                V2.relay_ensure(lab, force=True)
            self.assertEqual([row['node'] for row in state['relays']], list(V2.NODES))
            self.assertEqual(probe.call_count, 3)
            self.assertIn('TRANSPORT_READY nodes=3', output.getvalue())
            self.assertNotIn('nodes=6', output.getvalue())

    def test_foreign_relay_rule_stops_before_journal_or_connection(self):
        lab = self.load()
        with patch.object(lab, 'owner', return_value={'relays': []}), \
                patch.object(lab, 'remote', return_value=SimpleNamespace(returncode=0)), \
                patch.object(lab, 'write_private') as write, patch.object(lab, 'relay_connect') as connect:
            with self.assertRaisesRegex(ValueError, 'foreign V2 relay'):
                V2.relay_ensure(lab, force=True)
        write.assert_not_called()
        connect.assert_not_called()

    def test_inventory_has_exact_three_hosts_in_each_role_and_independent_networks(self):
        text = V2.inventory()
        for host in V2.NODES:
            self.assertEqual(text.count(host), 3)
        for group in ('etcd', 'kube_master', 'kube_node'):
            self.assertIn('[' + group + ']', text)
        for value in ('10.87.0.0/16', '172.31.0.0/16', '/clusters/longhorn-v2-gate'):
            self.assertIn(value, text)
        for value in ('192.168.122.', '192.168.47.129', '192.168.47.145', '/clusters/longhorn-gate'):
            self.assertNotIn(value, text)

    def test_configure_and_scoped_cleanup_reuse_exact_owner_checks(self):
        lab = self.load()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'longhorn-v2-gate'; path.mkdir()
            config = yaml.safe_load((ROOT/'conf/config.yml').read_text())
            config['longhorn_version'] = '1.12.1'  # kubecli new resolves the version constant.
            (path/'config.yml').write_text(yaml.safe_dump(config))
            with patch.object(V2, 'DIRECTORY', path), patch.object(lab, 'DIRECTORY', path), \
                    patch.object(lab, 'STATE', path/'longhorn-lab-owner.json'):
                V2.configure(lab)
                state = lab.owner()
                self.assertEqual(state['cluster'], 'longhorn-v2-gate')
                self.assertEqual(state['nodes'], list(V2.NODES))
                self.assertTrue(state['clean_start_verified'])
                config = yaml.safe_load((path/'config.yml').read_text())
                self.assertEqual(config['longhorn_install'], 'no')
                self.assertEqual(config['longhorn_block_disks'], {})
                self.assertEqual(config['longhorn_storage_nodes'], [])
                # Even this uninitialized fixture cannot delete a foreign file.
                (path/'foreign').write_text('keep')
                with self.assertRaisesRegex(ValueError, 'foreign file'):
                    lab.qualification_clean()
                self.assertTrue(lab.STATE.exists())
                (path/'foreign').unlink()
                lab.qualification_clean()
            self.assertFalse(path.exists())

    def test_symlink_inventory_is_rejected_before_config_change(self):
        lab = self.load()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory); config = path/'config.yml'
            config.write_text('longhorn_version: 1.12.1\n')
            target = path/'foreign'; target.write_text('untouched')
            (path/'hosts').symlink_to(target)
            with patch.object(V2, 'DIRECTORY', path), patch.object(lab, 'STATE', path/'owner.json'):
                with self.assertRaisesRegex(ValueError, 'symlink'):
                    V2.configure(lab)
            self.assertEqual(config.read_text(), 'longhorn_version: 1.12.1\n')
            self.assertEqual(target.read_text(), 'untouched')

    def test_dispatch_uses_separate_mode_and_both_runner_allowlists(self):
        source = ROOT/'tests/helpers/longhorn-runner.sh'
        script = 'source "$1"\nlonghorn_launch() { printf "%s %s" "$1" "$2"; }\nlonghorn_dispatch --longhorn-v2-registry-qualification\n'
        result = subprocess.run(['bash', '-c', script, 'contract', str(source)],
                                text=True, capture_output=True, check=True)
        self.assertEqual(result.stdout, 'v2-registry-qualification LONGHORN_V2_REGISTRY_QUALIFICATION_PASS')
        text = source.read_text()
        for variable in ('module', 'mode'):
            branches = re.findall(r'case "\$' + variable + r'" in ([^\n]+)', text)
            self.assertEqual(len(branches), 1)
            allowed = branches[0].split(')')[0].split('|')
            self.assertIn('v2-registry-qualification', allowed)
            self.assertIn('v2-core', allowed)
        result = subprocess.run(['bash', '-c', script.replace(
            '--longhorn-v2-registry-qualification', '--longhorn-v2-core'),
            'contract', str(source)], text=True, capture_output=True, check=True)
        self.assertEqual(result.stdout, 'v2-core LONGHORN_V2_CORE_PASS')

    def test_remote_branch_failure_preserves_first_rc_and_cleans_only_v2(self):
        script = (ROOT/'tests/helpers/longhorn-v2-live.sh').read_text()
        for failure in ('none', 'configure', 'relay-qualification', 'relay-ensure', 'relay-clean'):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory:
                base = Path(directory); (base/'.venv/bin').mkdir(parents=True)
                (base/'.venv/bin/python').symlink_to(sys.executable)
                (base/'tests/helpers').mkdir(parents=True)
                (base/'kubecli.py').write_text("from pathlib import Path\nPath('clusters/longhorn-v2-gate').mkdir(parents=True)\n")
                (base/'tests/helpers/longhorn-v2-lab.py').write_text('''import os,sys
mode=sys.argv[1]
with open('calls.txt','a') as stream: stream.write(mode+'\\n')
if mode==os.environ['FAIL_MODE']: sys.exit(17)
''')
                env = dict(__import__('os').environ, LONGHORN_PARENT_LEASE_HELD='yes', FAIL_MODE=failure)
                result = subprocess.run(['bash', '-c', 'curl() { return 0; }\nsleep() { return 0; }\n' +
                                         script.replace('BASE=/usr/local/kubeauto', 'BASE=' + str(base)),
                                         'v2-contract', 'v2-registry-qualification'],
                                        env=env, text=True, capture_output=True, timeout=15)
                calls = (base/'calls.txt').read_text().splitlines()
                self.assertEqual(calls[0], 'host-inputs')
                self.assertEqual(calls[-1], 'host-inputs')
                self.assertIn('relay-clean', calls)
                if failure == 'relay-clean':
                    self.assertNotIn('qualification-clean', calls)
                else:
                    self.assertIn('qualification-clean', calls)
                if failure == 'none':
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertIn('LONGHORN_V2_REGISTRY_QUALIFICATION_SCOPED_CLEAN_PASS', result.stdout)
                    self.assertIn('LONGHORN_V2_REGISTRY_LIFETIME_PASS', result.stdout)
                else:
                    self.assertEqual(result.returncode, 17, result.stderr)
                    self.assertNotIn('LONGHORN_V2_REGISTRY_QUALIFICATION_PASS', result.stdout)
                for forbidden in ('wipefs', 'modprobe', 'lab-wipe', 'longhorn-lab.py', 'setup '):
                    self.assertNotIn(forbidden, script)


if __name__ == '__main__':
    unittest.main()
