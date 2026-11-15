"""Prove the multi-host action adapter reads only while owning its state lock."""
import fcntl
import json
import os
from pathlib import Path
import select
import subprocess
import sys
import tempfile
import unittest


CHILD = '''
import importlib.util, json, sys
from types import SimpleNamespace
spec = importlib.util.spec_from_file_location('adapter', sys.argv[1])
adapter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(adapter)
real_flock = adapter.fcntl.flock
def announced_flock(fd, mode):
    print('LOCK_ATTEMPT', flush=True)
    real_flock(fd, mode)
adapter.fcntl.flock = announced_flock
action = adapter.ActionModule.__new__(adapter.ActionModule)
action._task = SimpleNamespace(args={'module': 'shell', 'values': {'_raw_params':
    'systemctl is-active --quiet kubelet; /proc/$upgrade_pid/exe" --version'}},
    get_name=lambda: 'Observe fixture process')
print(json.dumps(action.run(task_vars={'inventory_hostname': 'master-one'})), flush=True)
'''


class KubernetesFixtureLockContract(unittest.TestCase):
    def test_action_waits_for_lock_then_reads_current_state(self):
        fixture = Path(__file__).parent/'fixtures/kubernetes_action.py'
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = {'nodes': {'master-one': {'kubelet': 'v1.33.6'}}}
            path = root/'state.json'
            path.write_text(json.dumps(state))
            with (root/'state.lock').open('a') as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                process = subprocess.Popen([sys.executable, '-B', '-c', CHILD, str(fixture)],
                    env={**os.environ, 'UPGRADE_KUBERNETES_TEST_ROOT': str(root)},
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                try:
                    self.assertTrue(select.select([process.stdout], [], [], 10)[0],
                                    'adapter never attempted the lock')
                    self.assertEqual(process.stdout.readline().strip(), 'LOCK_ATTEMPT')
                    self.assertIsNone(process.poll())
                    self.assertFalse((root/'trace.jsonl').exists(), 'action read state before lock')
                    # Replace the state while holding the lock. The child must
                    # observe this version, rather than an earlier cached read.
                    state['nodes']['master-one']['kubelet'] = 'v1.34.13'
                    path.write_text(json.dumps(state))
                    fcntl.flock(lock, fcntl.LOCK_UN)
                    stdout, stderr = process.communicate(timeout=10)
                    self.assertEqual(process.returncode, 0, stderr)
                    self.assertEqual(json.loads(stdout)['stdout'], 'Kubernetes v1.34.13')
                    self.assertEqual(json.loads(path.read_text()), state)
                finally:
                    if process.poll() is None:
                        process.kill()
                    process.communicate()


if __name__ == '__main__':
    unittest.main()
