"""Local real-process pause/continue and exact SSH-255 regression contracts."""
import importlib.util
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[2]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module


class RebootRelayContracts(unittest.TestCase):
    def fixture(self, directory, child=False):
        root = Path(directory)
        script = root/'longhorn-v2-install.py'
        script.write_text('''import pathlib,sys,time,subprocess
root=pathlib.Path(__file__).parent
assert sys.argv[1]=='relay-maintain'
if (root/'child').exists():
 child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(.3)'])
 (root/'ready').write_text('ready');child.wait()
else:(root/'ready').write_text('ready')
while True:
 if (root/'ssh-255').exists():
  (root/'foreign-marker').write_text('foreign V2 relay socket/rule');sys.exit(1)
 time.sleep(.01)
''')
        if child: (root/'child').write_text('owned-child')
        helper = root/'longhorn-v2-reboot-relay.py'
        helper.write_bytes((ROOT/'tests/helpers/longhorn-v2-reboot-relay.py').read_bytes())
        relay = load('temporary_reboot_relay', helper)
        runtime = load('real_runtime_intent', ROOT/'tests/helpers/longhorn-v2-runtime-faults.py')
        original = load('original_process_identity', ROOT/'tests/helpers/longhorn-lab.py')
        state = {'run_id': 'run', 'cluster_uid': 'cluster', 'relays': [],
                 'raw_disks': {'192.168.47.148': {'machine_id': 'machine'}}}
        proof = {'case': 'worker-reboot', 'run_id': 'run', 'cluster_uid': 'cluster',
                 'address': '192.168.47.148', 'node': 'longhorn-node-148', 'manager': 'im',
                 'original_host': {'machine_id': 'machine', 'boot_id': 'old'},
                 'runtime': {'volume_uid': 'volume', 'managers': {'im': {'spec': {'nodeID': 'longhorn-node-148'}}}}}
        journal = {'volume_uid': 'volume', 'v2_runtime_fault': proof}
        def write(path, value):
            path.write_text(json.dumps(value));path.chmod(0o600)
        lab = SimpleNamespace(DIRECTORY=root, WORKERS=('192.168.47.148',), owner=lambda:state,
            require_lease=Mock(), load_helper=Mock(return_value=runtime),
            relay_rows=lambda row:row['relays'], process_identity=original.process_identity,
            write_private=Mock(side_effect=write), relay_ensure=Mock())
        process = subprocess.Popen([sys.executable, str(script), 'relay-maintain'])
        def cleanup():
            if process.poll() is None:
                os.kill(process.pid, signal.SIGCONT); process.terminate()
                try: process.wait(timeout=5)
                except subprocess.TimeoutExpired: process.kill(); process.wait(timeout=5)
        self.addCleanup(cleanup)
        until=time.monotonic()+5
        while not (root/'ready').exists():
            if time.monotonic()>until: self.fail('local observer fixture did not start')
            time.sleep(.01)
        return relay, lab, journal, proof, process

    def test_exact_original_ssh_255_is_not_a_foreign_rule_observation(self):
        profile=load('unchanged_v2_transport',ROOT/'tests/helpers/longhorn-v2-lab.py')
        lab=SimpleNamespace(owner=lambda:{'relays':[]},relay_rows=lambda state:[],
            relay_rule=lambda action:['iptables',action],remote=Mock(return_value=SimpleNamespace(returncode=255)))
        with tempfile.TemporaryDirectory() as directory, patch.object(profile,'DIRECTORY',Path(directory)), \
                patch.object(profile,'relay_probe',return_value=False):
            with self.assertRaisesRegex(ValueError,'foreign V2 relay socket/rule'):
                profile.relay_ensure(lab)
        self.assertEqual(lab.remote.call_args.kwargs,{'check':False})

    def test_same_process_survives_only_the_owned_ssh_outage_then_original_probe(self):
        with tempfile.TemporaryDirectory() as directory:
            relay,lab,journal,proof,process=self.fixture(directory)
            before=lab.process_identity(process.pid)
            with patch('sys.stdout',new_callable=io.StringIO) as output:
                relay.pause(lab,journal,proof,process.pid)
                self.assertEqual(relay.process(lab,proof[relay.KEY]),('T',[]))
                (Path(directory)/'ssh-255').write_text('expected reboot outage')
                time.sleep(.05)
                self.assertIsNone(process.poll())
                self.assertFalse((Path(directory)/'foreign-marker').exists())
                (Path(directory)/'ssh-255').unlink()
                relay.resume(lab,journal,proof)
                relay.resume_current(lab)
            self.assertEqual(before,lab.process_identity(process.pid))
            lab.relay_ensure.assert_called_once_with()
            self.assertEqual(output.getvalue().count('REBOOT_RELAY_PAUSED '),1)
            self.assertEqual(output.getvalue().count('REBOOT_RELAY_RESUMED '),1)
            self.assertEqual(proof[relay.KEY]['phase'],'resumed')
            self.assertEqual(list(Path(directory).glob('*.json')),[Path(directory)/relay.RECORD])

    def test_inflight_ssh_child_must_finish_before_pause_is_accepted(self):
        with tempfile.TemporaryDirectory() as directory:
            relay,lab,journal,proof,process=self.fixture(directory,child=True)
            relay.pause(lab,journal,proof,process.pid)
            self.assertEqual(relay.process(lab,proof[relay.KEY]),('T',[]))
            self.assertEqual(proof[relay.KEY]['phase'],'paused')
            relay.resume(lab,journal,proof)

    def test_wrong_lease_volume_process_tick_uid_and_existing_stop_cannot_be_signalled(self):
        for defect in ('lease','volume','tick','uid','stopped','relays'):
            with self.subTest(defect=defect),tempfile.TemporaryDirectory() as directory:
                relay,lab,journal,proof,process=self.fixture(directory)
                if defect=='lease':lab.require_lease.side_effect=ValueError('lease absent')
                elif defect=='volume':proof['runtime']['volume_uid']='foreign'
                elif defect=='tick':lab.process_identity=Mock(side_effect=[([sys.executable.encode(),str(Path(directory)/'longhorn-v2-install.py').encode(),b'relay-maintain'],'wrong'),None])
                elif defect=='stopped':os.kill(process.pid,signal.SIGSTOP)
                elif defect=='relays':lab.owner()['relays']=[{'node':'foreign'}]
                with patch.object(relay.os,'geteuid',return_value=-1 if defect=='uid' else os.geteuid()), \
                        patch.object(relay.os,'kill') as kill:
                    with self.assertRaises(ValueError):relay.pause(lab,journal,proof,process.pid)
                    kill.assert_not_called()

    def test_resume_restores_before_http_failure_or_expired_budget_and_preserves_journal(self):
        for defect in ('http','expired','intent'):
            with self.subTest(defect=defect),tempfile.TemporaryDirectory() as directory:
                relay,lab,journal,proof,process=self.fixture(directory)
                relay.pause(lab,journal,proof,process.pid)
                if defect=='http':lab.relay_ensure.side_effect=ValueError('foreign V2 relay socket/rule')
                elif defect=='expired':proof[relay.KEY]['started']-=1201
                else:proof[relay.KEY]['phase']='pause-intent'
                if defect=='intent':relay.resume(lab,journal,proof)
                else:
                    with self.assertRaises(ValueError):relay.resume(lab,journal,proof)
                self.assertNotIn(relay.process(lab,proof[relay.KEY])[0],('T','t'))
                self.assertTrue((Path(directory)/relay.RECORD).exists())

    def test_replaced_original_process_is_not_resumed(self):
        with tempfile.TemporaryDirectory() as directory:
            relay,lab,journal,proof,process=self.fixture(directory)
            relay.pause(lab,journal,proof,process.pid)
            proof[relay.KEY]['start_tick']='foreign'
            with patch.object(relay.os,'kill') as kill:
                with self.assertRaises(ValueError):relay.resume(lab,journal,proof)
                kill.assert_not_called()

    def test_cleanup_restores_observer_before_graph_or_business_delete(self):
        with tempfile.TemporaryDirectory() as directory:
            relay,lab,journal,proof,process=self.fixture(directory)
            relay.pause(lab,journal,proof,process.pid)
            runtime=lab.load_helper.return_value
            lab.check_business_graph=Mock()
            original=Mock()
            recovery=Mock();recovery.resume.side_effect=ValueError('original HTTP probe failed')
            with patch.object(runtime,'load',return_value=recovery):
                with self.assertRaisesRegex(ValueError,'original HTTP probe failed'):
                    runtime.cleanup(lab,original)
            recovery.resume.assert_called_once()
            lab.check_business_graph.assert_not_called();original.assert_not_called()
            self.assertTrue((Path(directory)/relay.RECORD).exists())

    def test_business_failure_finally_restores_observer_and_original_hooks(self):
        runtime=load('runtime_finally_contract',ROOT/'tests/helpers/longhorn-v2-runtime-faults.py')
        control=Mock();original=Mock(return_value=control);cleanup=Mock()
        lab=SimpleNamespace(load_helper=original,cleanup_business=cleanup,run_requests=Mock(),
                            business=Mock(side_effect=RuntimeError('owned business failure')))
        relay=Mock()
        with patch.object(runtime,'load',return_value=relay):
            with self.assertRaisesRegex(RuntimeError,'owned business failure'):
                runtime.business(lab,77,'worker-reboot')
        relay.resume_current.assert_called_once_with(lab)
        self.assertIs(lab.load_helper,original);self.assertIs(lab.cleanup_business,cleanup)
