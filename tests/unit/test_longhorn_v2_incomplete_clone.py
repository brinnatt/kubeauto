import ast
import copy
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import types
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]

spec = importlib.util.spec_from_file_location('incomplete', ROOT/'tests/helpers/longhorn-v2-incomplete-clone.py')
candidate = importlib.util.module_from_spec(spec); spec.loader.exec_module(candidate)


def volume(state='initiated'):
    return {'apiVersion':'longhorn.io/v1beta2','kind':'Volume',
        'metadata':{'name':'real-clone','uid':'clone-uid'},
        'spec':{'dataEngine':'v2','cloneMode':'linked-clone','size':'2147483648','dataSource':'vol://source'},
        'status':{'cloneStatus':{'state':state}}}


class Lab:
    def __init__(self, clone, *, accepted=False):
        self.clone=copy.deepcopy(clone); self.created=None; self.mutate=None; self.calls=[]
        self.result=types.SimpleNamespace(returncode=0 if accepted else 1,
            stdout=json.dumps(candidate.nested_request(clone,'dryrun')) if accepted else '',
            stderr='' if accepted else 'cannot use volume real-clone as linked-clone source: its own cloning is not yet completed (state: initiated)')
    def get(self, kind, name, namespace):
        return self.clone if name=='real-clone' else self.created
    def kubectl(self,*args,**kwargs):
        self.calls.append((args,kwargs))
        if self.mutate:self.mutate(self)
        # Fixed Manager types/data_source.go accepts vol:// and snap:// only.
        # Reject bad fixture input before simulating the clone-state guard.
        source=json.loads(kwargs['stdin'])['spec']['dataSource']
        if not source.startswith('vol://') or not source.removeprefix('vol://'):
            return types.SimpleNamespace(returncode=1,stdout='',stderr='invalid volume data source '+source)
        return self.result


class AdmissionTests(unittest.TestCase):
    def check(self, lab, clone=None, completed=False):
        clone=clone or volume()
        return candidate.dry_run(lab,clone,candidate.nested_request(clone,'dryrun'),completed=completed)
    def test_exact_real_state_refusal_and_server_dryrun(self):
        lab=Lab(volume()); self.assertTrue(self.check(lab)['uid_and_spec_unchanged'])
        args=lab.calls[0][0]; self.assertIn('--dry-run=server',args)
        self.assertEqual(json.loads(lab.calls[0][1]['stdin'])['spec']['dataSource'],'vol://real-clone')
    def test_request_uses_pinned_manager_volume_protocol(self):
        request=candidate.nested_request(volume(),'dryrun')
        self.assertEqual(request['spec']['dataSource'],'vol://real-clone')
        self.assertEqual(volume()['spec']['dataSource'],'vol://source')
    def test_completed_positive_control(self):
        clone=volume('completed'); self.assertTrue(self.check(Lab(clone,accepted=True),clone,True)['completed_positive_control'])
    def test_unrelated_refusal_is_not_pass(self):
        lab=Lab(volume()); lab.result.stderr='instance manager API too old'
        with self.assertRaisesRegex(ValueError,'wrong admission'):self.check(lab)
    def test_missing_guard_is_not_pass(self):
        with self.assertRaisesRegex(ValueError,'wrong admission'):self.check(Lab(volume(),accepted=True))
    def test_wrong_source_uid_or_spec_stops(self):
        for field in ('uid','spec'):
            lab=Lab(volume())
            def mutate(state,field=field):
                if field=='uid':state.clone['metadata']['uid']='replacement'
                else:state.clone['spec']['size']='4294967296'
            lab.mutate=mutate
            with self.subTest(field=field),self.assertRaisesRegex(ValueError,'changed or replaced'):self.check(lab)
    def test_no_created_nested_resource_even_after_failure(self):
        lab=Lab(volume()); lab.mutate=lambda state:setattr(state,'created',{'metadata':{'uid':'bad'}})
        with self.assertRaisesRegex(ValueError,'changed or replaced'):self.check(lab)
    def test_completed_or_failed_source_cannot_sign_negative(self):
        for state in ('completed','failed'):
            clone=volume(state)
            with self.subTest(state=state),self.assertRaisesRegex(ValueError,'wrong real'):self.check(Lab(clone),clone)
    def test_source_cannot_escape_controlled_hold(self):
        lab=Lab(volume());lab.mutate=lambda state:state.clone['status']['cloneStatus'].update(state='completed')
        with self.assertRaisesRegex(ValueError,'escaped'):self.check(lab)
    def test_positive_requires_correct_datasource(self):
        clone=volume('completed');lab=Lab(clone,accepted=True);lab.result.stdout='{}'
        with self.assertRaisesRegex(ValueError,'positive control'):self.check(lab,clone,True)
    def test_request_rejects_v1_and_fullcopy_and_clears_attachment(self):
        for defect in ('dataEngine','cloneMode'):
            clone=volume();clone['spec'][defect]='v1' if defect=='dataEngine' else 'full-copy'
            with self.subTest(defect=defect),self.assertRaises(ValueError):candidate.nested_request(clone,'new')
        clone=volume();clone['spec'].update(nodeID='old',migrationNodeID='old')
        request=candidate.nested_request(clone,'new')
        self.assertEqual(request['spec']['nodeID'],'');self.assertEqual(request['spec']['migrationNodeID'],'')
        self.assertEqual(clone['spec']['nodeID'],'old')



class SchedulingPrecheckTests(unittest.TestCase):
    def blocked(self, clone=None, replicas=None):
        # Execute the real nested predicate, keeping the owning UID lookup and
        # replica filter. The pinned controller can refuse before CreateReplica.
        tree=ast.parse((ROOT/'tests/helpers/longhorn-v2-incomplete-clone.py').read_text())
        method=next(n for n in ast.walk(tree) if isinstance(n,ast.FunctionDef) and n.name=='blocked')
        fixture=volume();fixture['status']['conditions']=[{'type':'Scheduled','status':'False',
            'reason':'ReplicaSchedulingFailure','message':'precheck new replica failed: disks are unavailable'}]
        if clone is not None:fixture=clone
        lab=types.SimpleNamespace(get=lambda *args,**kwargs:{'items':replicas or []})
        probe=types.SimpleNamespace(current=mock.Mock(return_value=fixture))
        namespace={'self':probe,'lab':lab,'row':{'volume':'real-clone','volume_uid':'clone-uid'},'NS':candidate.NS}
        exec(compile(ast.fix_missing_locations(ast.Module(body=[method],type_ignores=[])),'owning-blocked','exec'),namespace)
        result=namespace['blocked']()
        probe.current.assert_called_once_with('volumes.longhorn.io','real-clone','clone-uid',candidate.NS)
        return result
    def test_zero_replica_crs_are_the_pinned_precheck_boundary(self):
        self.assertIsNotNone(self.blocked())
    def test_unallocated_replica_crs_are_not_the_precreate_boundary(self):
        r={'spec':{'volumeName':'real-clone','nodeID':'','diskID':''}}
        self.assertIsNone(self.blocked(replicas=[r]*3))
    def test_assigned_replica_or_terminal_clone_cannot_sign_hold(self):
        r={'spec':{'volumeName':'real-clone','nodeID':'unexpected','diskID':'disk'}}
        with self.assertRaisesRegex(ValueError,'escaped'):self.blocked(replicas=[r])
        for state in ('failed','completed'):
            with self.subTest(state=state),self.assertRaisesRegex(ValueError,'escaped'):self.blocked(volume(state))
    def test_unobserved_or_unrelated_scheduling_failure_is_not_ready(self):
        clone=volume();clone['status']['conditions']=[]
        self.assertIsNone(self.blocked(clone))
        clone['status']['conditions']=[{'type':'Scheduled','status':'False','reason':'ForeignFailure','message':'different'}]
        self.assertIsNone(self.blocked(clone))
    def test_other_volume_replicas_do_not_belong_to_this_hold(self):
        r={'spec':{'volumeName':'source','nodeID':'original','diskID':'disk'}}
        self.assertIsNotNone(self.blocked(replicas=[r]))

class RecoveryTests(unittest.TestCase):
    def recovery_probe(self):
        class Base:
            def __init__(self,lab):self.lab=lab
            def save(self):self.saved=True
        cls=candidate.probe_type(types.SimpleNamespace(Probe=Base),None)
        probe=cls(types.SimpleNamespace(DIRECTORY=Path('/unused')),11)
        original={'longhorn-node-'+str(146+n):{'uid':str(n),'disk':'data','disk_uuid':'uuid'+str(n),
            'spec':{'allowScheduling':True,'evictionRequested':False,'path':'stable'}} for n in range(3)}
        probe.journal={'disk_hold_intent':True,'original_disks':{'192.168.47.'+str(146+n):[{'name':'data','path':'stable','identity':'uuid'+str(n)}] for n in range(3)},'native_disks':original}
        probe.config=mock.Mock();return probe,original
    def test_cleanup_restores_disk_before_dependency_cleanup(self):
        calls=[]
        class Base:
            def __init__(self,lab):self.lab=lab
            def cleanup(self):calls.append('dependency-clean')
        cls=candidate.probe_type(types.SimpleNamespace(Probe=Base),None)
        with tempfile.TemporaryDirectory() as tmp:
            lab=types.SimpleNamespace(DIRECTORY=Path(tmp),restore_request_input=lambda:calls.append('input-recovery'),owner=lambda:{'run_id':'owned'})
            probe=cls(lab,11);probe.record.write_text(json.dumps({'run_id':'owned'}))
            probe.restore_disks=lambda:calls.append('disk-recovery');probe.cleanup()
        self.assertEqual(calls,['input-recovery','disk-recovery','dependency-clean'])
    def test_partial_apply_recovers_only_original_or_held_identity(self):
        before={str(n):{'uid':str(n),'disk':'data','disk_uuid':'uuid'+str(n),
            'spec':{'allowScheduling':True,'evictionRequested':False,'path':'stable'}} for n in range(3)}
        held=candidate.expected_disks(before,True)
        self.assertEqual(before['0']['spec']['allowScheduling'],True)
        self.assertFalse(held['0']['spec']['allowScheduling'])
        self.assertEqual(held['0']['disk_uuid'],before['0']['disk_uuid'])
    def test_actual_recovery_accepts_partial_apply_and_restores_exact_original(self):
        probe,before=self.recovery_probe();partial=candidate.expected_disks(before,True);partial['longhorn-node-146']=before['longhorn-node-146']
        with mock.patch.object(candidate,'disk_state',side_effect=[partial,before]):probe.restore_disks()
        expected=copy.deepcopy(probe.journal['original_disks'])
        for rows in expected.values():rows[0].update(allow_scheduling=True,eviction_requested=False)
        probe.config.assert_called_once_with(longhorn_block_disks=expected)
        self.assertTrue(all('allow_scheduling' not in rows[0] for rows in probe.journal['original_disks'].values()))
        self.assertFalse(probe.journal['disk_hold_intent']);self.assertTrue(probe.saved)
    def test_replaced_uuid_uid_or_foreign_flag_stops_before_reconcile(self):
        for defect in ('uid','disk_uuid','spec'):
            probe,before=self.recovery_probe();bad=copy.deepcopy(before)
            if defect=='spec':bad['longhorn-node-146']['spec']['evictionRequested']=True
            else:bad['longhorn-node-146'][defect]='foreign'
            with self.subTest(defect=defect),mock.patch.object(candidate,'disk_state',return_value=bad):
                with self.assertRaisesRegex(ValueError,'foreign setting'):probe.restore_disks()
            probe.config.assert_not_called();self.assertTrue(probe.journal['disk_hold_intent'])
    def test_failed_restore_preserves_recovery_intent(self):
        probe,before=self.recovery_probe()
        with mock.patch.object(candidate,'disk_state',side_effect=[candidate.expected_disks(before,True),candidate.expected_disks(before,True)]):
            with self.assertRaisesRegex(ValueError,'not exactly restored'):probe.restore_disks()
        self.assertTrue(probe.journal['disk_hold_intent'])


class ShellExitTests(unittest.TestCase):
    def execute(self,business_rc=0,cleanup_rc=0,missing_clean=False):
        with tempfile.TemporaryDirectory() as tmp:
            base=Path(tmp);(base/'.venv/bin').mkdir(parents=True);(base/'tests/helpers').mkdir(parents=True)
            (base/'clusters/longhorn-v2-gate').mkdir(parents=True);(base/'bin').mkdir()
            python=base/'.venv/bin/python'
            python.write_text('''#!/usr/bin/python3
import os,signal,sys,time
from pathlib import Path
base=Path(os.environ['FIXTURE_BASE']);mode=sys.argv[2]
journal=base/'clusters/longhorn-v2-gate/longhorn-v2-incomplete-clone.json'
if mode=='relay-maintain':
 signal.signal(signal.SIGTERM,lambda *_:sys.exit(0));(base/'ready').touch()
 while True:time.sleep(.02)
elif mode=='business':
 deadline=time.monotonic()+3
 while not (base/'ready').exists():
  if time.monotonic()>deadline:sys.exit(6)
  time.sleep(.01)
 journal.write_text('{}')
 rc=int(os.environ['BUSINESS_RC'])
 if rc:sys.exit(rc)
 journal.unlink()
 print('LONGHORN_V2_INCOMPLETE_CLONE_RESULT {}')
 if os.environ['MISSING_CLEAN']=='no':print('LONGHORN_V2_INCOMPLETE_CLONE_FIXTURES_CLEAN_PASS')
 print('LONGHORN_V2_INCOMPLETE_CLONE_BUSINESS_PASS')
elif mode=='clean':
 rc=int(os.environ['CLEANUP_RC'])
 if rc:sys.exit(rc)
 journal.unlink();print('LONGHORN_V2_INCOMPLETE_CLONE_FIXTURES_CLEAN_PASS')
elif mode=='volume-baseline':print('LONGHORN_V2_VOLUME_BASELINE_VERIFIED business_objects=0')
''');python.chmod(0o700)
            kubectl=base/'bin/kubectl';kubectl.write_text('#!/bin/sh\nexit 0\n');kubectl.chmod(0o700)
            original=(ROOT/'tests/helpers/longhorn-v2-incomplete-clone.sh').read_text()
            script=base/'gate.sh';script.write_text(original.replace('BASE=/usr/local/kubeauto','BASE='+str(base)).replace('/var/tmp/kubeauto-longhorn-v2-incomplete.',str(base/'work.')))
            result=subprocess.run(['bash',str(script),'v2-incomplete-clone-qualification'],env=dict(os.environ,
                FIXTURE_BASE=str(base),BUSINESS_RC=str(business_rc),CLEANUP_RC=str(cleanup_rc),
                MISSING_CLEAN='yes' if missing_clean else 'no',LONGHORN_PARENT_LEASE_HELD='yes',PATH=str(base/'bin')+':'+os.environ['PATH']),
                text=True,capture_output=True,timeout=8)
            work=next(base.glob('work.*'));status=(work/'status').read_text()
            first=(work/'first-failure.status').read_text() if (work/'first-failure.status').exists() else None
            journal=(base/'clusters/longhorn-v2-gate/longhorn-v2-incomplete-clone.json').exists()
            return result,status,first,journal
    def test_success_requires_real_business_and_cleanup_markers(self):
        result,status,first,journal=self.execute()
        self.assertEqual(result.returncode,0,result.stderr);self.assertIn('rc=0 ',status)
        self.assertIn('LONGHORN_V2_INCOMPLETE_CLONE_QUALIFICATION_SCOPED_CLEAN_PASS',result.stdout)
        self.assertIsNone(first);self.assertFalse(journal)
    def test_business_failure_survives_successful_cleanup(self):
        result,status,first,journal=self.execute(business_rc=7)
        self.assertEqual(result.returncode,7);self.assertIn('rc=7 ',status);self.assertIn('rc=7 ',first)
        self.assertNotIn('QUALIFICATION_PASS',result.stdout);self.assertFalse(journal)
    def test_cleanup_failure_keeps_first_failure_and_journal(self):
        result,status,first,journal=self.execute(business_rc=7,cleanup_rc=9)
        self.assertEqual(result.returncode,7);self.assertIn('cleanup_rc=9',status);self.assertIn('rc=7 ',first)
        self.assertNotIn('QUALIFICATION_PASS',result.stdout);self.assertTrue(journal)
    def test_absent_business_cleanup_marker_cannot_manufacture_pass(self):
        result,status,first,journal=self.execute(missing_clean=True)
        self.assertNotEqual(result.returncode,0);self.assertNotIn('QUALIFICATION_PASS',result.stdout)
        self.assertIn('rc=1 ',status);self.assertIsNotNone(first)


if __name__=='__main__':unittest.main()


class PublicRouteTests(unittest.TestCase):
    def test_exact_public_routes_lease_allowlists_and_static_registration(self):
        import re
        runner = (ROOT/'tests/helpers/longhorn-runner.sh').read_text()
        allowed = re.search(r'case "\$module" in ([^)]+)\)', runner).group(1).split('|')
        leased = re.search(r'case "\$mode" in ([^)]+)\) ;; \*\) exit 2', runner).group(1).split('|')
        for mode in ('v2-incomplete-clone-qualification', 'v2-incomplete-clone-cleanup'):
            self.assertIn(mode, allowed); self.assertIn(mode, leased)
            result = subprocess.run(['bash', '-c', 'source "$1"; longhorn_launch() { printf "%s\\n" "$@"; }; longhorn_dispatch "$2"',
                'contract', str(ROOT/'tests/helpers/longhorn-runner.sh'), '--longhorn-'+mode], capture_output=True, text=True, check=True)
            self.assertEqual([mode, 'LONGHORN_'+mode.upper().replace('-', '_')+'_PASS'], result.stdout.splitlines())
        self.assertIn('exec bash "$BASE/tests/helpers/longhorn-v2-incomplete-clone.sh" "$MODE"', (ROOT/'tests/helpers/longhorn-live.sh').read_text())
        self.assertIn('tests.unit.test_longhorn_v2_incomplete_clone', (ROOT/'tests/helpers/longhorn-static.sh').read_text())
