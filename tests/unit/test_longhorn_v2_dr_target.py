import base64
import copy
from contextlib import nullcontext
import importlib.util
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[2]
CANDIDATE = ROOT/'tests/helpers/longhorn-v2-dr-target.py'
spec = importlib.util.spec_from_file_location('dr_target_contract', CANDIDATE)
gate = importlib.util.module_from_spec(spec); spec.loader.exec_module(gate)


def public_key(value=1):
    name = b'ssh-ed25519'
    wire = struct.pack('>I', len(name)) + name + struct.pack('>I', 32) + bytes([value])*32
    return 'ssh-ed25519 ' + base64.b64encode(wire).decode() + ' fixture'


class TargetContracts(unittest.TestCase):
    def test_disk_admission_binds_hardware_identity_and_stable_path_across_kernel_rename(self):
        assessment = {'blockers': [], 'candidates': [{'name': 'sdb', 'identity': 'original',
            'size_bytes': 100*1024**3, 'by_id': ['/dev/disk/by-id/scsi-original', '/dev/disk/by-id/wwn-original']}]}
        expected = gate.disk_identity(assessment)
        assessment['candidates'][0]['name'] = 'sdc'
        self.assertEqual(expected, gate.disk_identity(assessment))
        assessment['candidates'][0]['identity'] = 'replacement'
        self.assertNotEqual(expected, gate.disk_identity(assessment))
        assessment['blockers'] = ['disk-in-use']
        with self.assertRaises(ValueError):
            gate.disk_identity(assessment)

    def test_fixed_dispatch_leases_before_key_bootstrap_and_preserves_existing_routes(self):
        runner = (ROOT/'tests/helpers/longhorn-runner.sh').read_text()
        live = (ROOT/'tests/helpers/longhorn-live.sh').read_text()
        static = (ROOT/'tests/helpers/longhorn-static.sh').read_text()
        for name in ('v2-dr-target-registry-qualification', 'v2-dr-target-core'):
            self.assertIn('--longhorn-'+name+') longhorn_launch '+name, runner)
        self.assertIn('exec bash "$BASE/tests/helpers/longhorn-v2-dr-target.sh" "$MODE"', live)
        self.assertLess(runner.index('LONGHORN_LEASE_READY ]]; then'), runner.index('LONGHORN_TARGET_PARENT_LEASE_HELD=yes'))
        self.assertLess(runner.index('LONGHORN_TARGET_PARENT_LEASE_HELD=yes'), runner.index('KUBEAUTO_SYNC_SKIP_CONTROL_SETUP=1'))
        self.assertIn('test_longhorn_v2_dr_target -v', static)
        for command in ('--longhorn-v2-detached-backup-qualification)', '--longhorn-v2-backup-qualification)',
                        '--longhorn-backup)', '--longhorn-snapshot)'):
            self.assertIn(command, runner)

    def run_script(self, script, row):
        return subprocess.run([sys.executable, '-c', script], input=json.dumps(row), text=True,
                              capture_output=True, timeout=15)

    def bootstrap_fixture(self, directory):
        base = Path(directory); machine = base/'machine-id'; machine.write_text('approved')
        # Script runs as this test user in a temporary directory; production
        # retains its fixed root paths and UID=0 ownership requirement.
        script = gate.bootstrap_script().replace('/root/.ssh', str(base/'ssh')).replace(
            '/etc/machine-id', str(machine)).replace('info.st_uid==0', 'info.st_uid==os.getuid()')
        return base/'ssh', script

    def test_root_bootstrap_preserves_existing_keys_is_idempotent_and_binds_machine(self):
        with tempfile.TemporaryDirectory() as directory:
            parent, script = self.bootstrap_fixture(directory); parent.mkdir(mode=0o700)
            path = parent/'authorized_keys'; path.write_text(public_key(2)+'\n'); path.chmod(0o600)
            row = {'machine_id':'approved', 'keys':[public_key()]}
            self.assertEqual(0, self.run_script(script, row).returncode)
            expected = path.read_bytes(); self.assertIn(public_key(2).encode(), expected)
            self.assertEqual(0, self.run_script(script, row).returncode)
            self.assertEqual(expected, path.read_bytes())
            row['machine_id']='replacement'
            self.assertNotEqual(0, self.run_script(script, row).returncode)
            self.assertEqual(expected, path.read_bytes())

    def test_root_bootstrap_refuses_symlink_parent_file_or_existing_temporary(self):
        for kind in ('parent','file','temporary'):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                parent, script = self.bootstrap_fixture(directory)
                if kind == 'parent': parent.symlink_to(Path(directory)/'absent')
                else:
                    parent.mkdir(mode=0o700)
                    if kind == 'file': (parent/'authorized_keys').symlink_to('foreign')
                    else: (parent/'kubeauto-dr-key-bootstrap.tmp').write_text('foreign')
                result = self.run_script(script, {'machine_id':'approved','keys':[public_key()]})
                self.assertNotEqual(0, result.returncode)
                if kind == 'temporary': self.assertEqual('foreign', (parent/'kubeauto-dr-key-bootstrap.tmp').read_text())

    def test_known_host_key_is_authenticated_idempotent_and_conflict_refuses(self):
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory); path = parent/'known_hosts'
            path.write_text('other '+public_key(3)+'\n'); path.chmod(0o600)
            script = gate.known_host_script().replace('/root/.ssh/known_hosts',str(path)).replace(
                'info.st_uid==0', 'info.st_uid==os.getuid()')
            row = {'host':'192.168.47.149','key':' '.join(public_key().split()[:2])}
            self.assertEqual(0, self.run_script(script,row).returncode)
            original = path.read_bytes()
            self.assertEqual(0, self.run_script(script,row).returncode)
            self.assertEqual(original,path.read_bytes())
            row['key']=' '.join(public_key(2).split()[:2])
            self.assertNotEqual(0,self.run_script(script,row).returncode)
            self.assertEqual(original,path.read_bytes())

    def test_target_inventory_contains_only_approved_single_node_and_disjoint_networks(self):
        text = gate.inventory()
        self.assertEqual(3,text.count('192.168.47.149'))
        for forbidden in ('192.168.47.129','192.168.47.146','192.168.47.147','192.168.47.148',
                          '192.168.122.1','10.87.0.0/16','172.31.0.0/16'):
            self.assertNotIn(forbidden,text)
        self.assertIn('longhorn-v2-dr-target',text)
        self.assertIn('10.88.0.0/16',text); self.assertIn('172.29.0.0/16',text)

    def identity_fixture(self, directory):
        base = Path(directory); source = base/'clusters/longhorn-v2-gate'; source.mkdir(parents=True)
        (source/'longhorn-lab-owner.json').write_text(json.dumps({'cluster_uid':'source-cluster',
            'node_uids':{'source-node':'source-node-uid'}}))
        state = {'run_id':'target-run'}
        node = {'metadata':{'name':'longhorn-node-149','uid':'target-node-uid'},
            'status':{'addresses':[{'type':'InternalIP','address':'192.168.47.149'}],
                      'conditions':[{'type':'Ready','status':'True'}]}}
        namespace = {'metadata':{'uid':'target-cluster'}}
        lab = SimpleNamespace(owner=lambda:state,STATE=base/'owner.json',write_private=Mock())
        lab.get = lambda kind,*a: {'items':[node]} if kind=='nodes' else namespace
        return base,lab,state,node,namespace

    def test_target_cluster_node_and_readiness_cannot_alias_source_or_be_replaced(self):
        with tempfile.TemporaryDirectory() as directory:
            base,lab,state,node,namespace = self.identity_fixture(directory)
            with patch.object(gate,'BASE',base):
                gate.core_identity(lab)
                self.assertEqual('target-cluster',state['cluster_uid'])
                for change in ('cluster','node-uid','ip','name','ready'):
                    original_node = copy.deepcopy(node); original_ns = copy.deepcopy(namespace)
                    if change=='cluster': namespace['metadata']['uid']='source-cluster'
                    elif change=='node-uid': node['metadata']['uid']='source-node-uid'
                    elif change=='ip': node['status']['addresses'][0]['address']='192.168.47.146'
                    elif change=='name': node['metadata']['name']='foreign'
                    else: node['status']['conditions'][0]['status']='False'
                    with self.subTest(change=change), self.assertRaises(ValueError): gate.core_identity(lab)
                    node.clear(); node.update(original_node); namespace.clear();namespace.update(original_ns)

    def test_supervised_product_steps_use_exact_target_cli_and_reject_unowned_relay(self):
        expected = [str(CANDIDATE.resolve()).encode(),b'relay-maintain']
        lab = SimpleNamespace(process_identity=Mock(return_value=(expected,'ticks')),
                              scoped_cleanup=lambda cleanup:nullcontext(),stop_product=Mock())
        process = Mock(returncode=0);process.poll.return_value=0
        with patch.object(gate.subprocess,'Popen',return_value=process) as start:
            gate.supervise(lab,42,'setup')
        self.assertEqual([sys.executable,str(gate.BASE/'kubecli.py'),'setup','longhorn-v2-dr-target','90'],
                         start.call_args.args[0])
        lab.process_identity.return_value=None
        with patch.object(gate.subprocess,'Popen') as start,self.assertRaises(ValueError):
            gate.supervise(lab,42,'setup')
        start.assert_not_called()

    def test_transport_qualification_requires_durable_pass_and_current_fingerprint(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory); folder=base/'tests/evidence';folder.mkdir(parents=True)
            p=folder/'longhorn-v2-dr-target-registry-qualification-20261020.json'
            row={'status':'pass','rc':0,'failure_markers':0,'transport_fingerprint':'current'}
            lab=SimpleNamespace(transport_fingerprint=lambda:'current')
            with patch.object(gate,'BASE',base):
                p.write_text(json.dumps(row));gate.qualification_current(lab)
                for key,value in (('status','pending'),('rc',1),('failure_markers',1),('transport_fingerprint','old')):
                    wrong=dict(row);wrong[key]=value;p.write_text(json.dumps(wrong))
                    with self.subTest(key=key),self.assertRaises(ValueError):gate.qualification_current(lab)

    def test_bootstrap_requires_parent_lease_before_any_key_or_ssh_command(self):
        with patch.dict(gate.os.environ,{},clear=True),patch.object(gate,'execute') as execute,self.assertRaises(ValueError):
            gate.bootstrap()
        execute.assert_not_called()


class TargetShellContracts(unittest.TestCase):
    def run_shell(self, directory, mode='v2-dr-target-core', failure='', *, delayed_relay=False):
        base = Path(directory)
        for name in ('.venv/bin','tests/helpers','clusters','kube-bin'):
            (base/name).mkdir(parents=True,exist_ok=True)
        (base/'control-config').write_text('original-control-context')
        driver = base/'.venv/bin/python'
        driver.write_text("""#!/usr/bin/env python3
import os,signal,sys,time
from pathlib import Path
base=Path(__file__).resolve().parents[2]
helper,mode=sys.argv[1:3]
with (base/'calls').open('a') as output:output.write(mode+'\\n')
if mode==os.environ.get('DR_FIXTURE_FAILURE'):sys.exit(7)
if helper.endswith('kubecli.py'):
 (base/'clusters/longhorn-v2-dr-target').mkdir()
elif mode=='source-boundary':
 calls=(base/'calls').read_text().splitlines().count(mode)
 print('LONGHORN_V2_DR_SOURCE_BOUNDARY sha256='+('different' if os.environ.get('DR_FIXTURE_FAILURE')=='source-drift' and calls>1 else 'original'))
elif mode=='relay-maintain':
 if os.environ.get('DR_FIXTURE_DELAY_RELAY')=='yes':
  (base/'relay-started').touch()
  while not (base/'relay-release').exists():time.sleep(.001)
 signal.signal(signal.SIGTERM,lambda *_:sys.exit(0))
 (base/'relay-ready').touch()
 while True:time.sleep(.01)
""")
        driver.chmod(0o755)
        (base/'tests/helpers/lab-wipe-nodes.sh').write_text('exit 0\n')
        kubectl = base/'kube-bin/kubectl';kubectl.write_text('#!/bin/sh\nexit 0\n');kubectl.chmod(0o755)
        candidate = CANDIDATE.with_suffix('.sh')
        raw = candidate.read_text().replace('BASE=/usr/local/kubeauto','BASE='+str(base)).replace(
            'control_config=/root/.kube/config','control_config='+str(base/'control-config')).replace(
            'SECONDS - started < 120','SECONDS - started < 1').replace('sleep 2','sleep 0.01')
        # The shortened lifetime can expire before Python installs its handler.
        # Synchronize this fixture rather than changing the production supervisor.
        ready_gate = """
: > "$BASE/relay-release"
fixture_deadline=$((SECONDS + 5))
until [[ -f "$BASE/relay-ready" ]]; do
  (( SECONDS < fixture_deadline ))
  kill -0 "$relay_pid"
  sleep 0.01
done
"""
        raw = raw.replace('relay_pid=$!', 'relay_pid=$!\n' + ready_gate)
        if delayed_relay:
            # Force the former race without depending on timing or CPU load:
            # the stub waits before installing SIGTERM; lifetime expires at once.
            started_gate = """
fixture_deadline=$((SECONDS + 5))
until [[ -f "$BASE/relay-started" ]]; do
  (( SECONDS < fixture_deadline ))
  kill -0 "$relay_pid"
  sleep 0.01
done
"""
            raw = raw.replace('relay_pid=$!', 'relay_pid=$!\n' + started_gate).replace(
                'SECONDS - started < 1', 'SECONDS - started < 0').replace(
                'wait "$relay_pid" || {',
                'wait "$relay_pid" || { fixture_wait_rc=$?; '
                'echo "DR_FIXTURE_RELAY_WAIT_RC=$fixture_wait_rc" >&2;')
        script=base/'gate.sh';script.write_text(raw)
        environment=dict(os.environ,LONGHORN_PARENT_LEASE_HELD='yes',DR_FIXTURE_FAILURE=failure,
                         DR_FIXTURE_DELAY_RELAY='yes' if delayed_relay else 'no')
        result=subprocess.run(['bash',str(script),mode],text=True,capture_output=True,env=environment,timeout=15)
        calls=(base/'calls').read_text().splitlines()
        self.assertEqual('original-control-context',(base/'control-config').read_text())
        return result,calls

    def test_core_success_preserves_context_and_proves_source_before_terminal_marker(self):
        with tempfile.TemporaryDirectory() as directory:
            result,calls=self.run_shell(directory)
            self.assertEqual(0,result.returncode,result.stdout+result.stderr)
            self.assertIn('LONGHORN_V2_DR_TARGET_CORE_SCOPED_CLEAN_PASS',result.stdout)
            self.assertIn('LONGHORN_V2_DR_TARGET_CORE_PASS',result.stdout)
            self.assertEqual(2,calls.count('source-boundary'))
            self.assertLess(calls.index('qualification-current'),calls.index('new'))
            self.assertLess(calls.index('smoke'),calls.index('relay-clean'))
            self.assertNotIn('core-cleanup',calls)

    def test_failed_prerequisite_never_runs_product_and_failed_product_recovers_only_target(self):
        for failure in ('qualification-current','supervise','smoke','source-drift'):
            with self.subTest(failure=failure),tempfile.TemporaryDirectory() as directory:
                result,calls=self.run_shell(directory,failure=failure)
                self.assertNotEqual(0,result.returncode)
                self.assertNotIn('LONGHORN_V2_DR_TARGET_CORE_PASS',result.stdout)
                if failure=='qualification-current':self.assertNotIn('new',calls)
                elif failure!='source-drift':
                    self.assertIn('core-cleanup',calls)
                    self.assertLess(calls.index('relay-clean'),calls.index('core-cleanup'))
                if failure=='supervise':self.assertNotIn('smoke',calls)

    def test_transport_cleanup_waits_for_delayed_fixture_handler_at_immediate_lifetime_exit(self):
        with tempfile.TemporaryDirectory() as directory:
            result,calls=self.run_shell(directory,'v2-dr-target-registry-qualification',
                                        delayed_relay=True)
            self.assertEqual(0,result.returncode,result.stdout+result.stderr)
            self.assertTrue((Path(directory)/'relay-ready').is_file())
            self.assertIn('LONGHORN_V2_DR_TARGET_TRANSPORT_LIFETIME_PASS',result.stdout)
            self.assertIn('LONGHORN_V2_DR_TARGET_REGISTRY_QUALIFICATION_SCOPED_CLEAN_PASS',result.stdout)
            self.assertIn('qualification-clean',calls)
            self.assertNotIn('DR_FIXTURE_RELAY_WAIT_RC=',result.stderr)

    def test_transport_gate_checks_lifetime_and_scoped_cleanup_without_core_install(self):
        with tempfile.TemporaryDirectory() as directory:
            result,calls=self.run_shell(directory,'v2-dr-target-registry-qualification')
            self.assertEqual(0,result.returncode,result.stdout+result.stderr)
            self.assertIn('relay-qualification',calls)
            self.assertIn('qualification-clean',calls)
            self.assertIn('LONGHORN_V2_DR_TARGET_TRANSPORT_LIFETIME_PASS',result.stdout)
            self.assertNotIn('supervise',calls);self.assertNotIn('smoke',calls)


if __name__=='__main__':unittest.main()
