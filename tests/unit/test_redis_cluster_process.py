"""Ancestor-namespace signals require exact CRI and /proc ownership."""
import importlib.util
import json
from pathlib import Path
import signal
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('redis_process_contract', ROOT / 'tests/helpers/redis-cluster-process.py')
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)


class RedisProcessTests(unittest.TestCase):
    def request(self, action='stop'):
        return {'action': action, 'pod': 'owned-0', 'uid': 'uid', 'namespace': 'redis-gate',
                'container': 'redis', 'container_id': 'a' * 64, 'pid': 8123, 'pid_start': '1234'}

    def test_proc_identity_requires_start_comm_and_cgroup(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            proc = root / '8123'
            proc.mkdir()
            (proc / 'stat').write_text('8123 (redis-server) ' + ' '.join(['T'] + ['0'] * 18 + ['1234']))
            (proc / 'comm').write_text('redis-server\n')
            (proc / 'cgroup').write_text('0::/system.slice/cri-containerd-' + 'a' * 64 + '.scope')
            self.assertEqual(gate.process_identity(8123, 'a' * 64, root),
                             {'pid': 8123, 'pid_start': '1234', 'state': 'T'})
            with self.assertRaises(ValueError): gate.process_identity(8123, 'b' * 64, root)
            (proc / 'comm').write_text('sh\n')
            with self.assertRaises(ValueError): gate.process_identity(8123, 'a' * 64, root)

    def test_cri_never_accepts_foreign_pod_uid_or_non_running_container(self):
        request = self.request()
        labels = {'io.kubernetes.pod.uid': 'uid', 'io.kubernetes.pod.name': 'owned-0',
                  'io.kubernetes.pod.namespace': 'redis-gate', 'io.kubernetes.container.name': 'redis'}
        data = {'status': {'id': 'a' * 64, 'state': 'CONTAINER_RUNNING', 'labels': labels},
                'info': {'pid': 8123, 'runtimeSpec': {'process': {'env': ['password=SECRET']}}}}
        with patch.object(gate.subprocess, 'run') as run, patch.object(gate, 'process_identity') as proc:
            run.return_value = Mock(returncode=0, stdout=json.dumps(data))
            gate.inspect_identity(request)
            proc.assert_called_once_with(8123, 'a' * 64)
            proc.reset_mock()
            data['status']['labels']['io.kubernetes.pod.uid'] = 'foreign'
            run.return_value.stdout = json.dumps(data)
            with self.assertRaisesRegex(ValueError, 'ownership'): gate.inspect_identity(request)
            proc.assert_not_called()

    def test_stop_requires_actual_T_and_changed_process_is_never_signalled(self):
        identity = {'pid': 8123, 'pid_start': '1234', 'state': 'S'}
        with patch.object(gate, 'inspect_identity', return_value=identity), \
             patch.object(gate, 'process_identity', return_value={**identity, 'state': 'T'}), \
             patch.object(gate.os, 'kill') as kill:
            self.assertEqual(gate.act(self.request())['outcome'], 'paused')
            kill.assert_called_once_with(8123, signal.SIGSTOP)
        with patch.object(gate, 'inspect_identity', return_value={**identity, 'pid_start': '9999'}), \
             patch.object(gate.os, 'kill') as kill:
            with self.assertRaisesRegex(ValueError, 'changed'): gate.act(self.request())
            kill.assert_not_called()

    def test_successful_signal_return_without_T_is_rejected(self):
        identity = {'pid': 8123, 'pid_start': '1234', 'state': 'S'}
        with patch.object(gate, 'inspect_identity', return_value=identity), \
             patch.object(gate, 'process_identity', return_value=identity), \
             patch.object(gate.os, 'kill'), patch.object(gate.time, 'monotonic', side_effect=[0, 4]):
            with self.assertRaisesRegex(ValueError, 'actual process state'): gate.act(self.request())

    def test_cleanup_never_continues_reused_pid_or_missing_process(self):
        with patch.object(gate, 'process_identity', side_effect=FileNotFoundError), \
             patch.object(gate.os, 'kill') as kill:
            self.assertEqual(gate.act(self.request('continue'))['outcome'], 'original-process-gone')
            kill.assert_not_called()
        with patch.object(gate, 'process_identity', return_value={'pid_start': '9999'}), \
             patch.object(gate.os, 'kill') as kill:
            self.assertEqual(gate.act(self.request('continue'))['outcome'], 'original-process-replaced')
            kill.assert_not_called()


if __name__ == '__main__':
    unittest.main()
