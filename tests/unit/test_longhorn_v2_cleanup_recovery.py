"""Exact interruption windows and public recovery transport; no lab access."""
import copy
import hashlib
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
from unittest.mock import patch

spec=importlib.util.spec_from_file_location('cleanup_contract_fixture',Path(__file__).with_name('test_longhorn_v2_cleanup.py'))
fixture_module=importlib.util.module_from_spec(spec);spec.loader.exec_module(fixture_module)
gate,fixture_row=fixture_module.gate,fixture_module.fixture_row


def write(path,value):
    path.write_text(value if isinstance(value,str) else json.dumps(value));path.chmod(0o600)


class RecoveryPlan(unittest.TestCase):
    def setUp(self):
        import uuid
        self.root=Path('/var/tmp')/('kubeauto-longhorn-v2-retirement.'+uuid.uuid4().hex)
        self.root.mkdir(mode=0o700);self.addCleanup(shutil.rmtree,self.root);self.journal=self.root/'retirement.json'
        self.row=fixture_row();self.row.update(guards_passed=True,canaries={n:{'driver':'sd'} for n in gate.HOSTS['source']})
        self.save()
        for name,value in (('retire.exit','143'),('retire.finalized','143'),('retire.log','original failed attempt\n')):write(self.root/name,value)
    def save(self):write(self.journal,self.row)
    def plan(self):return gate.recovery_plan(self.journal,'source')
    def test_all_declared_resumable_phases_preserve_original_durable_bytes(self):
        original={p.name:p.read_bytes() for p in self.root.glob('retire.*')}
        for phase in ('prepared','native-uninstall','storage-clean','core-retiring','core-clean','released'):
            self.row['phase']=phase;self.save();self.assertEqual(self.plan(),phase)
        self.assertEqual(original,{p.name:p.read_bytes() for p in self.root.glob('retire.*')})
    def test_early_or_foreign_phase_guard_scope_and_hosts_refused(self):
        baseline=copy.deepcopy(self.row)
        for change in ({'phase':'capture'},{'guards_passed':False},{'checkpoint_clean':['failure']},
                       {'scope':'target'},{'canaries':{}},{'raw_disks':{}}):
            self.row=dict(baseline,**change);self.save()
            with self.assertRaises(ValueError):self.plan()
    def test_noncanonical_symlink_or_writable_artifact_refused(self):
        with self.assertRaisesRegex(ValueError,'explicit original'):gate.recovery_plan(Path('/tmp/retirement.json'),'source')
        self.journal.chmod(0o666)
        with self.assertRaisesRegex(ValueError,'private and owned'):self.plan()
        self.save();self.journal.rename(self.root/'real.json');self.journal.symlink_to('real.json')
        with self.assertRaisesRegex(ValueError,'private and owned'):self.plan()
    def test_original_fences_must_agree_and_success_requires_released(self):
        write(self.root/'retire.exit','0')
        with self.assertRaisesRegex(ValueError,'durable state'):self.plan()
        write(self.root/'retire.finalized','0');self.row['phase']='storage-clean';self.save()
        with self.assertRaisesRegex(ValueError,'durable state'):self.plan()
        self.row['phase']='released';self.save();self.assertEqual(self.plan(),'released')
    def test_target_scope_binds_original_source_boundary_hash(self):
        self.row=fixture_row('target');self.row.update(guards_passed=True,canaries={n:{'driver':'sd'} for n in gate.HOSTS['target']})
        write(self.root/'source-boundary','original source boundary\n')
        self.row['source_boundary_sha256']=hashlib.sha256((self.root/'source-boundary').read_bytes()).hexdigest();self.save()
        self.assertEqual(gate.recovery_plan(self.journal,'target'),'released')
        write(self.root/'source-boundary','foreign source boundary\n')
        with self.assertRaisesRegex(ValueError,'original source boundary'):gate.recovery_plan(self.journal,'target')



class CoreRecovery(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        self.active=self.root/'active';self.archive=self.root/'archive';self.active.mkdir(mode=0o700)
        self.row=fixture_row('target');self.row.update(phase='storage-clean',canaries={n:{'driver':'sd','canary':{'inode':1}} for n in gate.HOSTS['target']})
        self.observed=self.row.pop('released')
        self.journal=self.root/'journal.json';self.save();self.events=[]
        self.state=dict(self.row,disks=[],relays=[],namespace_uid=None)
        write(self.active/'longhorn-lab-owner.json',self.state);write(self.active/'hosts','exact hosts')
        self.lab=SimpleNamespace(DIRECTORY=self.active,STATE=self.active/'longhorn-lab-owner.json',NODES=gate.HOSTS['target'],
            owner=lambda:json.loads(self.lab.STATE.read_text()),write_private=write,
            execute=lambda *a,**k:self.record('verify',SimpleNamespace(stdout='LAB_CLEAN_VERIFY_PASS\n')),
            remote=lambda *a,**k:SimpleNamespace(stdout=json.dumps(self.observed[gate.HOSTS['target'][0]]['inventory'])))
        self.pool=SimpleNamespace(PROBE='read-only host probe',assess=lambda inv:self.observed[gate.HOSTS['target'][0]]['assessment'])
        self.core=SimpleNamespace(cleanup=self.cleanup)
        self.path_patch=patch.object(gate,'Path',side_effect=self.path);self.path_patch.start();self.addCleanup(self.path_patch.stop)
        self.load_patch=patch.object(gate,'load',side_effect=lambda n:self.core if n=='longhorn-v2-core.py' else self.pool);self.load_patch.start();self.addCleanup(self.load_patch.stop)
        self.remote_patch=patch.object(gate,'remote',side_effect=lambda lab,row,node,action,clean:self.record(action,{'driver':'sd'}));self.remote_patch.start();self.addCleanup(self.remote_patch.stop)
    def path(self,value):
        return self.root if value=='/var/tmp/kubeauto-longhorn-history' else Path(value)
    def record(self,event,value):self.events.append(event);return value
    def save(self):write(self.journal,self.row)
    def cleanup(self,lab):
        self.events.append('wipe');archive=self.root/('core-'+self.row['run_id'])
        lab.DIRECTORY.rename(archive);archive.chmod(0o700)
    def prepare_archive(self,phase='core-clean'):
        self.row['core_intent']={'owner_sha256':hashlib.sha256(self.lab.STATE.read_bytes()).hexdigest(),
            'inventory_sha256':hashlib.sha256((self.active/'hosts').read_bytes()).hexdigest()}
        self.row.update(phase=phase,archive=str(self.root/('core-'+self.row['run_id'])))
        self.cleanup(self.lab);self.events=[];self.save()
    def run_core(self):gate.core(self.lab,'target',self.journal,True)
    def test_product_0755_core_directory_and_archive_remain_valid_without_chmod(self):
        # kubecli new creates the deployment directory as 0755. The owning
        # cleanup moves it and sets the archive to 0700; journals stay private.
        self.active.chmod(0o755)
        self.run_core()
        archive=Path(gate.read(self.journal)['archive'])
        self.assertEqual(archive.stat().st_mode & 0o777,0o700)
        self.events=[];self.run_core()
        self.assertNotIn('wipe',self.events)
        self.assertIn('verify',self.events)
        with self.assertRaisesRegex(ValueError,'private and owned'):
            self.root.chmod(0o755)
            gate.private_path(self.root,True)

    def test_writable_or_foreign_core_directory_is_refused_before_wipe(self):
        for mode in (0o775,0o777):
            self.active.chmod(mode)
            with self.assertRaisesRegex(ValueError,'owned'):
                self.run_core()
            self.assertNotIn('wipe',self.events)
        self.active.chmod(0o755)
        with patch.object(gate.os,'geteuid',return_value=self.active.stat().st_uid+1):
            with self.assertRaisesRegex(ValueError,'owned'):
                self.run_core()
        self.assertNotIn('wipe',self.events)

    def test_symlink_core_directory_is_refused_before_wipe(self):
        real=self.root/'real-core';self.active.rename(real)
        self.active.symlink_to(real,target_is_directory=True)
        with self.assertRaisesRegex(ValueError,'missing or replaced'):
            self.run_core()
        self.assertNotIn('wipe',self.events)

    def test_storage_clean_pins_identity_then_uses_fixed_core_cleanup(self):
        self.run_core();row=gate.read(self.journal)
        self.assertEqual(row['phase'],'released');self.assertIn('wipe',self.events);self.assertIn('verify',self.events)
        self.assertLess(self.events.index('verify'),self.events.index('remove'))
    def test_core_archived_before_phase_write_resumes_without_second_wipe(self):
        self.prepare_archive('core-retiring');self.run_core()
        self.assertNotIn('wipe',self.events);self.assertIn('verify',self.events)
        self.assertEqual(gate.read(self.journal)['phase'],'released')
    def test_interrupted_unlink_and_released_retry_recheck_each_host(self):
        self.prepare_archive();node=self.lab.NODES[0]
        self.row['release_intents']={node:self.row['canaries'][node]};self.save();self.run_core()
        self.assertIn('finish-remove',self.events);self.events=[];self.run_core()
        self.assertIn('finish-remove',self.events);self.assertIn('verify',self.events)
    def test_foreign_archive_owner_and_remove_intent_cannot_release(self):
        self.prepare_archive();node=self.lab.NODES[0]
        self.row['release_intents']={node:{'foreign':True}};self.save()
        with self.assertRaisesRegex(ValueError,'removal intent'):self.run_core()
        self.assertNotIn('remove',self.events);self.assertNotIn('finish-remove',self.events)
        archive=Path(self.row['archive']);write(archive/'hosts','replacement')
        with self.assertRaisesRegex(ValueError,'archive identity'):self.run_core()
        self.assertEqual(self.lab.DIRECTORY,self.active)
    def test_partial_active_core_reuses_exact_cleanup_and_conflicting_archive_refused(self):
        self.row['core_intent']={'owner_sha256':hashlib.sha256(self.lab.STATE.read_bytes()).hexdigest(),
            'inventory_sha256':hashlib.sha256((self.active/'hosts').read_bytes()).hexdigest()}
        self.row.update(phase='core-retiring',archive=str(self.root/('core-'+self.row['run_id'])));self.save()
        self.run_core();self.assertIn('wipe',self.events)


class NativeWindow(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        self.directory=self.root/'yml/longhorn';self.directory.mkdir(parents=True)
        self.row=fixture_row('target');self.row['native_receipt']={'namespace_uid':'original','cluster_uid':self.row['cluster_uid']}
        self.row['uninstall_shared']={'shared':'same'}
        self.state=dict(self.row,namespace_uid='original',disks=[],host_state=None)
        write(self.directory/'block-disks-uninstalled-original.json',self.row['native_receipt'])
        write(self.directory/'installation-uninstalled-original.json',{'cluster_uid':self.row['cluster_uid'],'namespace_uid':'original'})
        write(self.root/'config.yml',{'longhorn_install':'yes','longhorn_storage_classes':[{'name':'longhorn'},{'name':'fixture'}]})
        self.events=[];self.lab=SimpleNamespace(DIRECTORY=self.root,STATE=self.root/'owner.json',get=lambda *a:None,
            shared_snapshot_identity=lambda:{'shared':'same'},execute=lambda *a:self.events.append('guard'),
            owner=lambda:copy.deepcopy(self.state),write_private=write)
    def test_exact_absent_namespace_window_finishes_owner_and_config(self):
        gate.complete_uninstall(self.lab,self.row,self.state)
        self.assertEqual(self.events,['guard']);state=json.loads(self.lab.STATE.read_text())
        self.assertNotIn('namespace_uid',state);self.assertTrue(state['native_uninstalled'])
        self.assertEqual(json.loads((self.root/'config.yml').read_text())['longhorn_install'],'no')
    def test_shared_change_replacement_receipt_or_live_namespace_blocks_reconciliation(self):
        self.row['uninstall_shared']={'shared':'foreign'}
        with self.assertRaisesRegex(ValueError,'unknown native'):gate.complete_uninstall(self.lab,self.row,self.state)
        self.row['uninstall_shared']={'shared':'same'};write(self.directory/'installation-uninstalled-original.json',{})
        with self.assertRaisesRegex(ValueError,'archive differs'):gate.complete_uninstall(self.lab,self.row,self.state)
        self.assertFalse(self.lab.STATE.exists());self.assertEqual(self.events,[])


class ShellRecovery(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        self.helpers=self.root/'tests/helpers';self.helpers.mkdir(parents=True)
        source=Path(__file__).resolve().parents[1]/'helpers'
        script=(source/'longhorn-v2-cleanup.sh').read_text().replace('BASE=/usr/local/kubeauto','BASE='+str(self.root)).replace('PY="$BASE/.venv/bin/python"','PY='+sys.executable)
        write(self.helpers/'longhorn-v2-cleanup.sh',script)
        shutil.copy2(source/'run-durable-gate.sh',self.helpers/'run-durable-gate.sh')
        self.original=self.root/'original';self.original.mkdir();self.journal=self.original/'retirement.json';write(self.journal,'{}')
        for name,value in (('retire.exit','143\n'),('retire.finalized','143\n'),('retire.log','original failure\n')):write(self.original/name,value)
        self.frozen={p.name:p.read_bytes() for p in self.original.iterdir()}
        write(self.root/'control-config','interrupted test context')
        stub='''import pathlib,sys,json,os
base=pathlib.Path(__file__).resolve().parents[2]
with (base/'actions').open('a') as f:f.write(json.dumps(sys.argv[1:])+'\\n')
a=sys.argv[1]
if a=='recovery-plan': print('core-clean')
elif a=='recover-core':
 if os.environ.get('RECOVERY_FAIL'):raise SystemExit(73)
 for n in ('192.168.47.146','192.168.47.147','192.168.47.148'):print('LONGHORN_V2_SOURCE_HOST_RELEASE_PASS host='+n)
 print('LONGHORN_V2_CLEANUP_SCOPED_CLEAN_PASS scope=source')
elif a=='source-boundary':print(os.environ.get('BOUNDARY','original source'))
elif a=='finalize':
 prefix=pathlib.Path(sys.argv[sys.argv.index('--status-prefix')+1])
 assert prefix.parent!=pathlib.Path(sys.argv[sys.argv.index('--journal')+1]).parent
 assert pathlib.Path(str(prefix)+'.exit').read_text().strip()=='0'
 assert pathlib.Path(str(prefix)+'.finalized').read_text().strip()=='0'
elif a not in ('relay-clean',):raise SystemExit('unexpected mutation '+a)
'''
        for name in ('longhorn-v2-cleanup.py','longhorn-v2-core.py','longhorn-v2-dr-target.py'):write(self.helpers/name,stub)
    def run_shell(self,scope='source',**extra):
        env=dict(os.environ,LONGHORN_PARENT_LEASE_HELD='yes',LONGHORN_V2_RETIREMENT_JOURNAL=str(self.journal),**extra)
        mode='v2-cleanup-recovery' if scope=='source' else 'v2-cleanup-target-recovery'
        return subprocess.run(['bash',str(self.helpers/'longhorn-v2-cleanup.sh'),mode],env=env,capture_output=True,text=True,timeout=10)
    def test_public_recovery_has_new_durable_never_prepare_and_restores_current_context(self):
        result=self.run_shell();self.assertEqual(result.returncode,0,result.stderr)
        actions=[json.loads(l) for l in (self.root/'actions').read_text().splitlines()]
        self.assertNotIn('prepare',[a[0] for a in actions]);self.assertIn('recover-core',[a[0] for a in actions])
        self.assertEqual((self.root/'control-config').read_text(),'interrupted test context')
        self.assertEqual(self.frozen,{p.name:p.read_bytes() for p in self.original.iterdir() if p.is_file()})
        attempt=next(self.original.glob('recovery.*'));self.assertEqual((attempt/'retire.exit').read_text().strip(),'0')
        self.assertIn('LONGHORN_V2_CLEANUP_PASS',result.stdout)
    def test_failed_recovery_retains_both_failed_attempts_and_never_finalizes(self):
        result=self.run_shell(RECOVERY_FAIL='yes');self.assertNotEqual(result.returncode,0)
        actions=[json.loads(l)[0] for l in (self.root/'actions').read_text().splitlines()]
        self.assertNotIn('finalize',actions)
        self.assertEqual((self.original/'retire.exit').read_text(),'143\n')
        attempt=next(self.original.glob('recovery.*'))
        self.assertEqual((attempt/'retire.exit').read_text().strip(),'73')
        self.assertEqual((attempt/'retire.finalized').read_text().strip(),'73')
        self.assertEqual((self.root/'control-config').read_text(),'interrupted test context')

    def test_target_original_source_boundary_mismatch_blocks_recovery_before_mutation(self):
        write(self.original/'source-boundary','original source\n')
        result=self.run_shell('target',BOUNDARY='changed source');self.assertNotEqual(result.returncode,0)
        actions=[json.loads(l)[0] for l in (self.root/'actions').read_text().splitlines()]
        self.assertNotIn('recover-core',actions);self.assertNotIn('finalize',actions)
        self.assertEqual((self.original/'retire.exit').read_text(),'143\n')


class RecoveryLauncher(unittest.TestCase):
    def test_recovery_journal_and_recovered_host_proofs_use_separate_stdin_records(self):
        runner=Path(__file__).resolve().parents[1]/'helpers/longhorn-runner.sh'
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            for name in ('.venv/bin','tests/helpers'):(root/name).mkdir(parents=True)
            for name in ('.venv/bin/python','tests/helpers/sync-kubeauto.sh'):
                path=root/name;write(path,'#!/bin/sh\nexit 0\n');path.chmod(0o700)
            script='''set -Eeuo pipefail
source "$1"
ROOT="$2"
longhorn_local_gate() { :; }
ssh_longhorn() {
 if [[ "$1" == *LONGHORN_LEASE_READY* ]]; then
  printf 'LONGHORN_LEASE_READY\\n'
  IFS= read -r mode
  IFS= read -r argument
  printf '%s\\n%s\\n' "$mode" "$argument" > "$ROOT/received"
  if [[ "$mode" == node-lifecycle ]]; then
   IFS= read -r target_proof
   printf '%s\\n' "$target_proof" >> "$ROOT/received"
  fi
  printf 'LONGHORN_DURABLE_LAUNCHED\\n'
 fi
}
monitor_remote_job() { :; }
remote_job_summary() { :; }
longhorn_launch "$CONTRACT_MODE" CONTRACT_PASS
'''
            for mode,variable,argument in (
                ('v2-cleanup-recovery','LONGHORN_V2_RETIREMENT_JOURNAL','retirement.json'),
                ('v2-cleanup-target-recovery','LONGHORN_V2_RETIREMENT_JOURNAL','retirement.json'),
                ('node-lifecycle','LONGHORN_V2_RELEASE_PROOF','recovery.Ab1234/release-proofs/longhorn-v2-source-released-192.168.47.148.json'),
                ('v2-disk-lifecycle','LONGHORN_V2_TARGET_RELEASE_PROOF','recovery.Ab1234/release-proofs/longhorn-v2-target-released-192.168.47.149.json')):
                valid='/var/tmp/kubeauto-longhorn-v2-retirement.Ab1234/'+argument
                for value in (valid,valid+'\nforeign',valid+'/../retirement.json'):
                    received=root/'received';received.unlink(missing_ok=True)
                    # Runtime proof variables from the owning live launch must
                    # not supply a missing dependency in this isolated fixture.
                    env={key:item for key,item in os.environ.items() if not key.startswith('LONGHORN_')}
                    env.update(CONTRACT_MODE=mode,**{variable:value})
                    target='/var/tmp/kubeauto-longhorn-v2-retirement.Ab1234/recovery.Ab1234/release-proofs/longhorn-v2-target-released-192.168.47.149.json'
                    if mode=='node-lifecycle':env['LONGHORN_V2_TARGET_RELEASE_PROOF']=target
                    result=subprocess.run(['bash','-c',script,'contract',str(runner),str(root)],
                        env=env,capture_output=True,text=True,timeout=5)
                    self.assertEqual(result.returncode==0,value==valid,(mode,result.stderr))
                    if value==valid:
                        expected=[mode,value]+([target] if mode=='node-lifecycle' else [])
                        self.assertEqual(received.read_text().splitlines(),expected)
                    else:self.assertFalse(received.exists())
                    if mode=='node-lifecycle' and value==valid:
                        for peer in ('', target+'\nforeign'):
                            received.unlink(missing_ok=True)
                            refused=subprocess.run(['bash','-c',script,'contract',str(runner),str(root)],
                                env=dict(env,LONGHORN_V2_TARGET_RELEASE_PROOF=peer),capture_output=True,text=True,timeout=5)
                            self.assertNotEqual(refused.returncode,0)
                            self.assertFalse(received.exists())


if __name__=='__main__':unittest.main()
