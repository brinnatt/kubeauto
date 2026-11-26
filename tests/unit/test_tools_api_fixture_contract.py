"""Execution boundaries of the real API fixture, without starting a control plane."""
import importlib.util
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
