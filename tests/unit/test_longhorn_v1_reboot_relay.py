"""Owned V1 reboot SSH outage, absent NAT rule and exact process contracts."""
from contextlib import contextmanager, redirect_stdout
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


M = load('v1_reboot', ROOT/'tests/helpers/longhorn-v1-reboot-relay.py')
V2 = load('v2_process', ROOT/'tests/helpers/longhorn-v2-reboot-relay.py')
LAB = load('original_lab', ROOT/'tests/helpers/longhorn-lab.py')


class RebootContracts(unittest.TestCase):
    def fixture(self, directory, child=False):
        root = Path(directory); script = root/'longhorn-lab.py'
        script.write_text('''import pathlib,subprocess,sys,time
root=pathlib.Path(__file__).parent
if (root/'child').exists():
 child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(.3)'])
 (root/'ready').touch();child.wait()
else:(root/'ready').touch()
while True:
 if (root/'ssh-255').exists():(root/'failed').touch();sys.exit(255)
 time.sleep(.01)
''')
        if child:(root/'child').touch()
        process = subprocess.Popen([sys.executable,str(script),'relay-maintain'])
        def cleanup():
            if process.poll() is None:
                os.kill(process.pid,signal.SIGCONT);process.terminate()
                try:process.wait(timeout=5)
                except subprocess.TimeoutExpired:process.kill();process.wait(timeout=5)
        self.addCleanup(cleanup)
        until=time.monotonic()+5
        while not (root/'ready').exists():
            if time.monotonic()>until:self.fail('observer fixture failed to start')
            time.sleep(.01)
        relay=load('temporary_v1_reboot',root/'longhorn-v1-reboot-relay.py') if (root/'longhorn-v1-reboot-relay.py').exists() else None
        if relay is None:
            (root/'longhorn-v1-reboot-relay.py').write_bytes(Path(M.__file__).read_bytes())
            relay=load('temporary_v1_reboot',root/'longhorn-v1-reboot-relay.py')
        original={'machine_id':'machine','boot_id':'old'}; current=dict(original,boot_id='new')
        row={'node':'worker','socket':str(root/'longhorn-registry-worker.sock'),'listener':{'boot_id':'old'}}
        state={'run_id':'run','cluster_uid':'cluster','relays':[row]}
        common=SimpleNamespace(host=Mock(return_value=original))
        runtime=SimpleNamespace(host_call=Mock())
        lab=SimpleNamespace(DIRECTORY=root,WORKERS=['worker'],PORT=23520,SSH=['ssh'],
            require_lease=Mock(),owner=lambda:state,relay_rows=lambda s:s['relays'],
            process_identity=LAB.process_identity,scoped_cleanup=LAB.scoped_cleanup,
            wait=lambda name,timeout,callback:callback(),relay_rule=lambda action:['rule',action],
            listener_script=lambda:'listener',execute=Mock(),relay_connect=Mock(),
            bind_listener=Mock(),relay_ensure=Mock(),remote=Mock())
        lab.write_private=lambda path,journal:path.write_text(json.dumps(journal))
        def remote(node,argv,**kwargs):
            if argv[0]=='rule':return SimpleNamespace(returncode=1 if argv[1]=='-C' else 0)
            return SimpleNamespace(stdout='null')
        lab.remote.side_effect=remote
        def helpers(name):
            return {'longhorn-v2-reboot-relay.py':V2,'longhorn-v1-stability.py':common,
                'longhorn-v2-runtime-faults.py':runtime}[name]
        relay.load=helpers
        def reboot(*args,**kwargs):
            self.assertEqual(('T',[]),V2.process(lab,json.loads((root/relay.RECORD).read_text())['reboot'][V2.KEY]))
            (root/'ssh-255').touch();time.sleep(.05)
            self.assertIsNone(process.poll());self.assertFalse((root/'failed').exists())
            (root/'ssh-255').unlink();common.host.return_value=current
        runtime.host_call.side_effect=reboot
        return relay,lab,process,original,current,row,runtime,common

    def test_same_original_pid_survives_expected_outage_and_restores_missing_rule(self):
        for child in (False,True):
            with self.subTest(child=child),tempfile.TemporaryDirectory() as directory:
                relay,lab,proc,old,new,row,runtime,common=self.fixture(directory,child)
                identity=lab.process_identity(proc.pid)
                with redirect_stdout(io.StringIO()):
                    self.assertEqual(new,relay.reboot(lab,proc.pid,'worker',old))
                self.assertEqual(identity,lab.process_identity(proc.pid))
                self.assertNotIn(V2.process(lab,{'pid':proc.pid,'argv':[v.decode() for v in identity[0]],
                    'start_tick':identity[1],'control_boot_id':Path('/proc/sys/kernel/random/boot_id').read_text().strip()})[0],('T','t'))
                lab.relay_connect.assert_called_once_with(row)
                self.assertIn(['rule','-A'],[call.args[1] for call in lab.remote.call_args_list])
                lab.relay_ensure.assert_called_once()
                runtime.host_call.assert_called_once_with(lab,'worker',{'action':'reboot','expected':old},check=False)
                self.assertFalse((Path(directory)/relay.RECORD).exists())

    def test_original_direct_registry_path_needs_no_relay_row_or_relay_mutation(self):
        with tempfile.TemporaryDirectory() as directory:
            relay,lab,proc,old,new,row,runtime,common=self.fixture(directory)
            lab.owner()['relays']=[]
            with redirect_stdout(io.StringIO()):self.assertEqual(new,relay.reboot(lab,proc.pid,'worker',old))
            lab.remote.assert_not_called();lab.relay_connect.assert_not_called()
            lab.relay_ensure.assert_called_once()
            self.assertEqual([],lab.owner()['relays'])

    def test_repair_reuses_only_exact_current_listener_and_never_duplicates_existing_rule(self):
        with tempfile.TemporaryDirectory() as directory:
            relay,lab,proc,old,new,row,runtime,common=self.fixture(directory)
            common.host.return_value=new;row['listener']={'boot_id':'new'}
            lab.remote.side_effect=lambda node,argv,**kwargs:SimpleNamespace(
                returncode=0,stdout=json.dumps(row['listener']))
            proof={'run_id':'run','cluster_uid':'cluster','address':'worker','original_host':old,'socket':row['socket']}
            self.assertEqual(new,relay.repair(lab,proof))
            lab.relay_connect.assert_not_called()
            self.assertNotIn(['rule','-A'],[call.args[1] for call in lab.remote.call_args_list])
            lab.relay_ensure.assert_called_once()

    def test_non_reboot_ssh_255_is_not_swallowed(self):
        with patch.object(LAB,'owner',return_value={'relays':[{'node':'worker','socket':str(LAB.DIRECTORY/'longhorn-registry-worker.sock')}]}),\
                patch.object(LAB,'NODES',('worker',)),patch.object(LAB,'execute',return_value=SimpleNamespace(returncode=1)),\
                patch.object(LAB,'remote',side_effect=ValueError('ssh rc=255')):
            with self.assertRaisesRegex(ValueError,'ssh rc=255'):LAB.relay_ensure()

    def test_foreign_listener_and_rule_inspection_failure_fail_before_connect_or_add(self):
        for defect in ('listener','rule255'):
            with self.subTest(defect=defect),tempfile.TemporaryDirectory() as directory:
                relay,lab,proc,old,new,row,runtime,common=self.fixture(directory)
                def remote(node,argv,**kwargs):
                    if argv[0]=='rule':return SimpleNamespace(returncode=255 if defect=='rule255' else 1)
                    return SimpleNamespace(stdout=json.dumps({'boot_id':'foreign'}))
                lab.remote.side_effect=remote
                with self.assertRaises(ValueError),redirect_stdout(io.StringIO()):relay.reboot(lab,proc.pid,'worker',old)
                lab.relay_connect.assert_not_called()
                self.assertNotIn(['rule','-A'],[call.args[1] for call in lab.remote.call_args_list])
                proof=json.loads((Path(directory)/relay.RECORD).read_text())['reboot']
                self.assertNotIn(V2.process(lab,proof[V2.KEY])[0],('T','t'))

    def test_reboot_error_and_interruption_always_release_original_process(self):
        for error in (RuntimeError('reboot request failed'),KeyboardInterrupt()):
            with self.subTest(error=type(error)),tempfile.TemporaryDirectory() as directory:
                relay,lab,proc,old,new,row,runtime,common=self.fixture(directory)
                runtime.host_call.side_effect=error
                with self.assertRaises(type(error)),redirect_stdout(io.StringIO()):relay.reboot(lab,proc.pid,'worker',old)
                proof=json.loads((Path(directory)/relay.RECORD).read_text())['reboot']
                self.assertEqual('resumed',proof[V2.KEY]['phase'])
                self.assertNotIn(V2.process(lab,proof[V2.KEY])[0],('T','t'))

    def test_wrong_owner_original_boot_or_pid_never_signals_or_reboots(self):
        for defect in ('owner','boot','pid','stopped'):
            with self.subTest(defect=defect),tempfile.TemporaryDirectory() as directory:
                relay,lab,proc,old,new,row,runtime,common=self.fixture(directory)
                if defect=='owner':lab.require_lease.side_effect=ValueError('lease absent')
                if defect=='boot':row['listener']['boot_id']='foreign'
                if defect=='pid':lab.process_identity=Mock(return_value=([b'python',b'/foreign',b'relay-maintain'],'123'))
                if defect=='stopped':os.kill(proc.pid,signal.SIGSTOP);time.sleep(.02)
                with patch.object(relay.os,'kill') as kill:
                    with self.assertRaises(ValueError):relay.reboot(lab,proc.pid,'worker',old)
                    kill.assert_not_called()
                runtime.host_call.assert_not_called()

    def test_recovery_repairs_before_releasing_retained_exact_process_without_reboot_replay(self):
        with tempfile.TemporaryDirectory() as directory:
            relay,lab,proc,old,new,row,runtime,common=self.fixture(directory)
            proof={'run_id':'run','cluster_uid':'cluster','address':'worker','original_host':old,'socket':row['socket'],'reboot_intent':True}
            journal={'reboot':proof}
            with redirect_stdout(io.StringIO()):relay.pause(lab,journal,proof,proc.pid,V2)
            common.host.return_value=new
            with redirect_stdout(io.StringIO()):relay.recover_current(lab)
            runtime.host_call.assert_not_called();lab.relay_ensure.assert_called_once()
            self.assertFalse((Path(directory)/relay.RECORD).exists())

    def test_pause_failure_recovery_never_waits_for_or_requests_a_boot(self):
        with tempfile.TemporaryDirectory() as directory:
            relay,lab,proc,old,new,row,runtime,common=self.fixture(directory)
            proof={'run_id':'run','cluster_uid':'cluster','address':'worker','original_host':old,'socket':row['socket']}
            journal={'reboot':proof}
            with redirect_stdout(io.StringIO()):relay.pause(lab,journal,proof,proc.pid,V2)
            lab.wait=Mock(side_effect=AssertionError('unrequested new boot wait'))
            with redirect_stdout(io.StringIO()):relay.recover_current(lab)
            runtime.host_call.assert_not_called();lab.wait.assert_not_called()
            self.assertFalse((Path(directory)/relay.RECORD).exists())

    def test_cli_recovery_retires_only_original_observer_before_new_shell_starts(self):
        with tempfile.TemporaryDirectory() as directory:
            relay,lab,proc,old,new,row,runtime,common=self.fixture(directory)
            proof={'run_id':'run','cluster_uid':'cluster','address':'worker','original_host':old,'socket':row['socket']}
            journal={'reboot':proof}
            with redirect_stdout(io.StringIO()):relay.pause(lab,journal,proof,proc.pid,V2)
            def wait(name,timeout,callback):
                until=time.monotonic()+5
                while True:
                    result=callback()
                    if result:return result
                    if time.monotonic()>until:self.fail('retired keeper did not exit')
                    time.sleep(.01)
            lab.wait=wait
            with redirect_stdout(io.StringIO()):relay.recover_current(lab,retire=True)
            proc.wait(timeout=5)
            self.assertIsNone(lab.process_identity(proc.pid))
            runtime.host_call.assert_not_called()

    def test_each_fixed_shell_recovers_original_relay_before_starting_a_new_observer(self):
        for name in ('longhorn-node-failure.sh','longhorn-v1-storage-faults.sh','longhorn-v1-stability.sh'):
            source=(ROOT/'tests/helpers'/name).read_text()
            with self.subTest(name=name):
                self.assertLess(source.index('reboot-relay.py" recover-current'),source.index('"$LAB" verify-core',source.index('"$LAB" verify-transport')))
                self.assertLess(source.index('reboot-relay.py" recover-current'),source.index('"$LAB" relay-ensure'))
                self.assertLess(source.index('reboot-relay.py" recover-current'),source.index('"$LAB" relay-maintain'))

    def test_three_existing_owned_reboot_paths_use_helper_and_cleanup_recovers_first(self):
        for name,needle in (('longhorn-v1-stability.py','reboot(lab, self.pid, ip, original)'),
                            ('longhorn-node-failure.py','reboot(lab,self.pid,address,self.row[\'fence\'][\'host\'])'),
                            ('longhorn-v1-storage-faults.py',"reboot(lab, journal[INTENT]['relay_pid'], proof['address'], original)")):
            source=(ROOT/'tests/helpers'/name).read_text()
            with self.subTest(name=name):
                self.assertIn(needle,source);self.assertIn('recover_current(',source)
                self.assertNotIn("{'action': 'reboot', 'expected': original}, check=False",source)


if __name__=='__main__':unittest.main()
