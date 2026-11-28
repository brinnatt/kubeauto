"""Execution boundaries of the real API fixture, without starting a control plane."""
import importlib.util
import json
import contextlib
import io
from pathlib import Path
import signal
import socket
import subprocess
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location('tools_api_fixture', ROOT/'tests/helpers/tools-api-fixture.py')
FIXTURE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(FIXTURE)


class ApiFixtureContracts(unittest.TestCase):
    def test_calico_policy_plan_validation_precedes_apply_and_requires_field_readback(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            calls=[]
            applied=[]
            def apply(context,obj):
                applied.append(obj)
            def command(argv,name,**kwargs):
                self.assertTrue(any(d['kind']=='Tier' and d['metadata']['name']=='default' for d in applied))
                calls.append(('product',list(argv)))
                if 'apply' in argv:
                    backup=Path(argv[argv.index('--backup-dir')+1]);backup.mkdir(exist_ok=True)
                    (backup/'gnp.yaml').write_text('fixture backup')
                return subprocess.CompletedProcess(argv,0,'apiVersion: crd.projectcalico.org/v1\n','')
            def kubectl(context,*args,**kwargs):
                calls.append(('diagnostic',list(args)))
                return subprocess.CompletedProcess(args,1 if 'networkpolicy' in args else 0,'{"items":[]}','')
            def get(context,kind,name,namespace=None):
                if kind.startswith('globalnetwork'):
                    return {'spec':{'order':12.5,'applyOnForward':True,'performanceHints':['AssumeNeededOnEveryNode'],
                                    'ingress':[{'destination':{'ports':[19100]},'source':{'nets':['fd00::/8']}}]}}
                if kind.startswith('hostendpoint'):
                    return {'spec':{'interfaceName':'eth0','profiles':['projectcalico-default-allow'],'expectedIPs':['198.51.100.2']}}
                return {'spec':{'podSelector':{'matchLabels':{'app':'demo','tier':'backend'}}}}
            with contextlib.redirect_stdout(io.StringIO()) as output:
                FIXTURE.calico_policy_scenario(root,root,command,apply,get,kubectl)
            self.assertIn('TOOLS_CALICO_POLICY_API_PASS',output.getvalue())
            product=[args for tag,args in calls if tag=='product']
            self.assertEqual(sum('plan' in args for args in product),6)
            self.assertEqual(sum('apply' in args for args in product),12)
            self.assertEqual(sum('delete' in args for args in product),6)
            self.assertEqual(sum('--dry-run=server' in args for tag,args in calls if tag=='diagnostic'),3)
            for i,(tag,args) in enumerate(calls):
                if tag=='product' and 'apply' in args:
                    self.assertIn('--post-verify',args)
                    self.assertIn('--confirm',args)
                    self.assertTrue(any(t=='product' and 'plan' in a for t,a in calls[:i]))
            with self.assertRaisesRegex(ValueError,'GNP_FIELDS_CHANGED'):
                FIXTURE.calico_policy_scenario(root,root,command,mock.Mock(),lambda *a:{'spec':{}},kubectl)

    def test_all_namespace_fixture_does_not_supply_conflicting_namespace(self):
        common=['tool','backup','--context','a']
        argv=FIXTURE.backup_arguments(common, 'owned', 'all', ['--all-namespaces','--resources','configmaps'])
        self.assertNotIn('--namespace',argv)
        self.assertIn('--all-namespaces',argv)
        selected=FIXTURE.backup_arguments(common,'owned','selected',['--resources','configmaps'])
        self.assertEqual(selected[selected.index('--namespace')+1],'owned')
        self.assertEqual(common,['tool','backup','--context','a'])

    def test_port_check_allows_time_wait_but_refuses_a_real_listener(self):
        with socket.socket() as listener:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind(('127.0.0.1', 0)); listener.listen()
            port = listener.getsockname()[1]
            with self.assertRaises(OSError):
                FIXTURE.verify_ports([port])
            with socket.create_connection(('127.0.0.1', port)) as client:
                accepted, _ = listener.accept()
                accepted.close()  # Server side enters TIME_WAIT after client closes.
                self.assertEqual(client.recv(1), b'')
        FIXTURE.verify_ports([port])

    def test_api_instances_bind_loopback_and_separate_real_storage(self):
        a = FIXTURE.api_arguments(Path('/tmp/owned'), 'a', 16443)
        b = FIXTURE.api_arguments(Path('/tmp/owned'), 'b', 16444)
        for argv in (a, b):
            self.assertIn('--bind-address=127.0.0.1', argv)
            self.assertIn('--authorization-mode=RBAC', argv)
            self.assertIn('--client-ca-file=/tmp/owned/ca.crt', argv)
        self.assertIn('--etcd-prefix=/tools-api-a', a)
        self.assertIn('--etcd-prefix=/tools-api-b', b)
        self.assertIn('--secure-port=16443', a)
        self.assertIn('--secure-port=16444', b)

    def test_expected_refusal_requires_both_nonzero_and_specific_cause(self):
        FIXTURE.assert_refusal(subprocess.CompletedProcess([], 1, '', '403 Forbidden'), '403')
        for rc, error in ((0, '403 Forbidden'), (1, 'connection refused')):
            with self.assertRaises(ValueError):
                FIXTURE.assert_refusal(subprocess.CompletedProcess([], rc, '', error), '403')

    def test_cleanup_reaps_only_owned_processes_and_removes_only_fixture(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            work = root/'fixture'; work.mkdir()
            (work/'private.key').write_text('fixture-only')
            keep = root/'foreign'; keep.write_text('keep')
            process = mock.Mock(pid=123456)
            process.poll.return_value = None
            process.wait.side_effect = [subprocess.TimeoutExpired('owned', 20), 0]
            stream = mock.Mock()
            with mock.patch.object(FIXTURE.os, 'killpg') as kill, mock.patch.object(FIXTURE.socket, 'socket') as sock:
                FIXTURE.cleanup_owned(work, [process], [stream])
            self.assertEqual(kill.call_args_list, [mock.call(123456, signal.SIGTERM), mock.call(123456, signal.SIGKILL)])
            self.assertFalse(work.exists())
            self.assertEqual(keep.read_text(), 'keep')
            stream.close.assert_called_once()
            self.assertEqual(sock.return_value.__enter__.return_value.bind.call_count, 4)

    def test_foreign_directory_refused_before_process_or_certificate_creation(self):
        with mock.patch.object(FIXTURE.Path, 'cwd', return_value=Path('/tmp/foreign')), \
             mock.patch.object(FIXTURE.subprocess, 'Popen') as popen:
            with self.assertRaisesRegex(ValueError, 'TOOLS_API_SCOPE_REFUSED'):
                FIXTURE.main()
            popen.assert_not_called()


if __name__ == '__main__':
    unittest.main()
