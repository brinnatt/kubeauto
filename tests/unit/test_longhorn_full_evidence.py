#!/usr/bin/env python3
"""Local-only disproofs for full-stage receipts, dependencies and launch wiring."""
import importlib.util
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import tempfile
import unittest
from unittest.mock import patch, Mock
from types import SimpleNamespace

ROOT=Path(__file__).resolve().parents[2]
HELPERS=ROOT/'tests/helpers'
REPO=ROOT

def module(name,path):
    spec=importlib.util.spec_from_file_location(name,path)
    result=importlib.util.module_from_spec(spec);spec.loader.exec_module(result);return result

class FullEvidenceContracts(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.base=Path(self.temp.name);self.work=self.base/'logs/longhorn-full/run.fixture'
        for path in (self.work,self.work/'evidence',self.work/'preflight',self.work/'stages',self.work/'accepted'):
            path.mkdir(parents=True,exist_ok=True,mode=0o700);path.chmod(0o700)
        self.m=module('full_evidence',HELPERS/'longhorn-full-evidence.py');self.m.BASE=self.base
        self.resolver=module('full_material',HELPERS/'longhorn-full-material.py')
        self.patches=[patch.object(self.m,'load',side_effect=lambda name:self.resolver),
            patch.dict(os.environ,{'LONGHORN_FULL_EVIDENCE_DIR':str(self.work/'evidence')})]
        for p in self.patches:p.start();self.addCleanup(p.stop)
        self.reuse={'transport_refs':{'logs/accepted/gate.log':'b'*64}}
        self.reuse_patch=patch.object(self.m,'full_copy_material',return_value=self.reuse)
        self.reuse_patch.start();self.addCleanup(self.reuse_patch.stop)
        self.inputs={'product.py':'a'*64}
        self.freeze=patch.object(self.m,'frozen_inputs',return_value=self.inputs);self.freeze.start();self.addCleanup(self.freeze.stop)
        self.m.write(self.work/'frozen-inputs.json',self.inputs)
        self.m.write(self.work/'preflight-inputs.json',self.inputs)
        self.m.write(self.work/'full-copy-reuse.json',self.reuse)
        names=self.work/'full-copy-files.txt';names.write_text('logs/accepted/gate.log\n');names.chmod(0o600)

    def source_snapshot_fixture(self):
        self.freeze.stop()
        names = ('common/constants.py', 'conf/config.yml', 'controller/cluster/cli.py',
            'service/cluster/manager.py', 'service/cluster/downloader.py',
            'tests/longhorn-test-matrix.yaml', 'tests/run_enterprise_regression.sh',
            'tests/helpers/run-durable-gate.sh', 'tests/helpers/kubernetes-production-smoke.sh',
            'tests/helpers/lab-wipe-nodes.sh',
            'roles/longhorn/files/longhorn.py', 'tests/helpers/longhorn-full-evidence.py')
        fixtures = ('longhorn-full-cli.py', 'longhorn-v2-install-reviewed-7afb928f.py.txt',
            'longhorn-v2-dr-target-install-reviewed-af065aa3.py.txt',
            'longhorn-v2-upgrade-baseline-reviewed-59b57a74.py.txt')
        for name in (*names, *('tests/fixtures/'+name for name in fixtures)):
            path = self.base/name; path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes((ROOT/name).read_bytes())
        return tuple('tests/fixtures/'+name for name in fixtures)

    def test_frozen_inputs_ignore_added_and_changed_python_cache_files(self):
        self.source_snapshot_fixture()
        baseline = self.m.frozen_inputs()
        for name in ('roles/longhorn/files/__pycache__/longhorn.cpython-312.pyc',
                'roles/longhorn/files/__pycache__/cache-metadata.json',
                'roles/longhorn/files/orphan.pyc', 'roles/longhorn/files/orphan.pyo',
                'tests/helpers/longhorn-cache.pyc'):
            with self.subTest(cache=name):
                path = self.base/name; path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b'initial cache')
                self.assertEqual(baseline, self.m.frozen_inputs())
                path.write_bytes(b'recompiled cache')
                self.assertEqual(baseline, self.m.frozen_inputs())

    def test_frozen_inputs_bind_full_cli_and_each_reviewed_source_fixture(self):
        fixtures = self.source_snapshot_fixture()
        baseline = self.m.frozen_inputs()
        for name in fixtures:
            with self.subTest(fixture=name):
                self.assertIn(name, baseline)
                path = self.base/name; original = path.read_bytes()
                self.assertEqual(self.m.digest(path), baseline[name])
                path.write_bytes(original+b'\n# changed fixture\n')
                changed = self.m.frozen_inputs()
                self.assertNotEqual(baseline[name], changed[name])
                self.assertEqual(baseline, {**changed, name: baseline[name]})
                path.write_bytes(original)
                self.assertEqual(baseline, self.m.frozen_inputs())

    def test_frozen_inputs_bind_actual_shared_node_cleanup(self):
        self.source_snapshot_fixture()
        baseline=self.m.frozen_inputs();name='tests/helpers/lab-wipe-nodes.sh'
        self.assertIn(name,baseline)
        (self.base/name).write_text('changed cleanup\n')
        self.assertNotEqual(baseline[name],self.m.frozen_inputs()[name])

    def receipt(self,mode='rwo',lines=None,rc=0,finalized=0):
        prefix=self.work/('preflight' if mode in self.m.LOCAL else 'stages')/mode
        rows=list(self.m.markers(mode)) if lines is None else lines
        data={'log':'\n'.join(rows+['LONGHORN_FULL_CHILD_EXIT rc=0'])+'\n','exit':str(rc)+'\n','finalized':str(finalized)+'\n'}
        for suffix,value in data.items():
            p=Path(str(prefix)+'.'+suffix);p.write_text(value);p.chmod(0o600)
        return prefix

    def test_full_clone_calls_only_unique_consumer_with_verified_original_archive(self):
        directory=self.work/'raw/v1-ownership-v2-linked-acceptance/kubeauto-longhorn-parallel.fixture'
        directory.mkdir(parents=True,mode=0o700)
        log=directory/'v2.log';log.write_text('actual-original-aggregate');log.chmod(0o600)
        bound={'original':'/var/tmp/kubeauto-longhorn-parallel.fixture/v2.log','copy':self.m.reference(log)}
        validator=Mock(return_value={'status':'pass','signed_scope':'v2-clone','full_copy_reuse':{'verified':True}})
        row={'nested_receipts':[bound]}
        with patch.object(self.m,'accepted_stage',return_value=(None,row,None)) as owning, \
                patch.object(self.m,'load',return_value=SimpleNamespace(validate_clone49=validator)):
            result=self.m.clone_acceptance()
            self.assertEqual(result['signed_scope'],'v2-clone')
            owning.assert_called_once_with('v1-ownership-v2-linked-acceptance')
            validator.assert_called_once_with(self.base,directory)
            log.write_text('changed')
            with self.assertRaises(ValueError):self.m.clone_acceptance()
            self.assertEqual(validator.call_count,1)
            row['nested_receipts']=[]
            with self.assertRaises(ValueError):self.m.clone_acceptance()
        shell=(HELPERS/'longhorn-full.sh').read_text()
        self.assertNotIn('v2-full-copy-clone-qualification',shell)
        self.assertNotIn('v2-full-copy-lifecycle-qualification',shell)
        self.assertEqual(self.m.CASE_STAGES[49],('v2-clone','v1-ownership-v2-linked-acceptance','v2-upgrade-lifecycle'))

    def test_transport_accepts_only_the_unique_consumers_exact_refs_and_unchanged_bytes(self):
        self.m.verify_full_copy_transport(self.work)
        path=self.work/'full-copy-files.txt';path.write_text('logs/unreviewed/gate.log\n')
        with self.assertRaises(ValueError):self.m.verify_full_copy_transport(self.work)
        path.write_text('logs/accepted/gate.log\n')
        self.reuse['transport_refs']['logs/accepted/gate.log']='c'*64
        with self.assertRaises(ValueError):self.m.verify_full_copy_transport(self.work)
        self.reuse_patch.stop()
        validator=Mock(return_value={'transport_refs':{'../outside':'a'*64}})
        with patch.object(self.m,'load',return_value=SimpleNamespace(preflight_full_copy=validator)):
            with self.assertRaises(ValueError):self.m.full_copy_material()
        validator.assert_called_once_with(self.base)
        runner=(HELPERS/'longhorn-runner.sh').read_text()
        self.assertIn('--ignore-existing --chmod=D700,F600 --files-from=',runner)
        self.assertIn('full-copy-files.txt',runner)
        self.assertIn("verify-preflight --directory '$remote'",runner)

    def test_actual_original_durable_cli_zero_and_nonzero_are_read_from_files(self):
        for rc in (0,7):
            with self.subTest(rc=rc):
                prefix=self.work/'stages/rwo'
                command=['bash',str(REPO/'tests/helpers/run-durable-gate.sh'),str(prefix),'LONGHORN_FULL_CHILD_EXIT',
                    'bash','-c','printf "%s\\n" LONGHORN_RWO_PASS LONGHORN_RWO_SCOPED_CLEAN_PASS; exit '+str(rc)]
                result=subprocess.run(command,text=True,capture_output=True)
                log=Path(str(prefix)+'.log');log.write_text(result.stdout);log.chmod(0o600)
                for suffix in ('exit','finalized'):Path(str(prefix)+'.'+suffix).chmod(0o600)
                self.assertEqual(result.returncode,rc)
                if rc:self.assertRaises(ValueError,self.m.execution,'rwo',prefix)
                else:self.m.execution('rwo',prefix)

    def test_failed_or_unfinalized_or_duplicate_or_missing_or_failure_raw_never_accept(self):
        good=list(self.m.markers('rwo'))
        variants=[(good,2,0),(good,0,2),(good+good[:1],0,0),(good[:1],0,0),
            (good+['LONGHORN_FIRST_FAILURE phase=owned rc=1'],0,0)]
        for lines,rc,finalized in variants:
            with self.subTest(lines=lines,rc=rc,finalized=finalized):
                prefix=self.receipt(lines=lines,rc=rc,finalized=finalized)
                self.assertRaises(ValueError,self.m.accept,'rwo',prefix)
                self.assertFalse((self.work/'accepted/rwo.json').exists())

    def test_ansible_retry_event_requires_complete_success_and_durable_zero(self):
        retry='FAILED - RETRYING: [192.168.47.149 -> localhost]: wait for calico-node running in a polling manner (15 retries left).'
        good=[retry,*self.m.markers('rwo')]
        self.m.execution('rwo',self.receipt(lines=good))
        for lines,rc,finalized in ((good,1,0),(good,0,1),([retry],0,0),
                ([retry,*self.m.markers('rwo'),'FAILED terminal task'],0,0),
                ([retry,*self.m.markers('rwo'),'LONGHORN_FIRST_FAILURE phase=owned rc=1'],0,0),
                ([retry.removesuffix('.'),*self.m.markers('rwo')],0,0)):
            with self.subTest(lines=lines,rc=rc,finalized=finalized):
                with self.assertRaises(ValueError):
                    self.m.execution('rwo',self.receipt(lines=lines,rc=rc,finalized=finalized))

    def test_stage_receipt_path_is_fixed_to_owning_run_and_local_boundary(self):
        prefix=self.receipt()
        for wrong in (self.work/'stages/rwx',self.work/'preflight/rwo',self.work/'other/rwo'):
            self.assertRaises(ValueError,self.m.execution,'rwo',wrong)
        self.m.execution('rwo',prefix)

    def test_file_metadata_hash_and_parent_symlink_are_refused(self):
        prefix=self.receipt();p=Path(str(prefix)+'.log');ref=self.m.reference(p)
        p.write_text(p.read_text()+'changed\n');self.assertRaises(ValueError,self.m.referenced,ref)
        p.chmod(0o644);self.assertRaises(ValueError,self.m.reference,p)
        p.unlink();p.symlink_to(self.work/'frozen-inputs.json');self.assertRaises(ValueError,self.m.reference,p)
        self.assertRaises(ValueError,self.m.referenced,{'path':'../escape','sha256':'0'*64})

    def test_stale_frozen_input_blocks_before_scope_acceptance(self):
        prefix=self.receipt()
        with patch.object(self.m,'frozen_inputs',return_value={'product.py':'b'*64}):
            self.assertRaises(ValueError,self.m.accept,'rwo',prefix)
        self.assertFalse((self.work/'accepted/rwo.json').exists())

    def test_immutable_material_never_overwrites_historical_or_current(self):
        p=self.work/'evidence/observed.json';self.m.write(p,{'native':'original'})
        self.assertRaises(ValueError,self.m.write,p,{'native':'replacement'})
        self.assertEqual(json.loads(p.read_text()),{'native':'original'})

    def test_accepted_stage_rebinds_original_raw_and_durable_bytes(self):
        prefix=self.receipt();row=self.m.accept('rwo',prefix)
        self.assertEqual(self.m.accepted_stage('rwo')[1],row)
        path=Path(str(prefix)+'.log');path.write_text(path.read_text()+'drift\n')
        self.assertRaises(ValueError,self.m.accepted_stage,'rwo')

    def test_every_matrix_row_has_exact_complete_owner_without_repeating_upgrade(self):
        import yaml
        cases=yaml.safe_load((REPO/'tests/longhorn-test-matrix.yaml').read_text())['cases']
        self.assertEqual([x['group'] for x in cases],self.m.CASE_GROUPS)
        self.assertEqual(set(self.m.CASE_STAGES),set(range(1,55))-{43})
        self.assertEqual(self.m.CASE_STAGES[35],self.m.CASE_STAGES[36])
        for number,owners in self.m.CASE_STAGES.items():
            self.assertTrue(owners)
            self.assertLessEqual(set(owners),set(self.m.MODES),number)
        self.assertIn('v2-upgrade-lifecycle',self.m.CASE_STAGES[49])
        self.assertIn('v2-interrupt-qualification',self.m.CASE_STAGES[51])
        self.assertIn('v2-disk-lifecycle',self.m.CASE_STAGES[51])
        self.assertIn('v2-failure',self.m.CASE_STAGES[51])

    def test_case19_result_binds_both_original_process_and_physical_disk_owners(self):
        import yaml
        self.assertEqual(self.m.CASE_STAGES[19],
                         ('replica-failure','v1-operations-v2-platform-install'))
        cases=yaml.safe_load((REPO/'tests/longhorn-test-matrix.yaml').read_text())['cases']
        matrix=self.base/'tests/longhorn-test-matrix.yaml';matrix.parent.mkdir(mode=0o700)
        matrix.write_text(yaml.safe_dump({'cases':cases}))
        stages={}
        for mode in self.m.MODES:
            path=self.work/'accepted'/(mode+'.json');row={'owning_scope':mode}
            self.m.write(path,row)
            lines=[case['success_marker'] for n,case in enumerate(cases,1)
                   if n not in self.m.AGGREGATE_CASES and self.m.CASE_STAGES[n][0]==mode]
            stages[mode]=(path,row,lines)
        for mode,name in self.m.MATERIALS.items():
            self.m.write(self.work/'evidence'/name,stages[mode][1])
        with patch.object(self.m,'accepted_stage',side_effect=lambda mode,**kwargs:stages[mode]), \
                patch.object(self.m,'maintenance51',return_value={}):
            result=self.m.complete_result()
        refs=result['cases']['LONGHORN-19']['stages']
        self.assertEqual(set(refs),{'replica-failure','v1-operations-v2-platform-install'})
        for owner in refs:self.assertEqual(refs[owner],self.m.reference(stages[owner][0]))
        self.assertFalse((self.work/'full-result.json').exists())

    def test_case19_physical_owner_refuses_missing_or_duplicate_original_disk_stage(self):
        rows=['LONGHORN_PARALLEL_CHILD_RESULTS v1_rc=0 v2_rc=0']
        rows+=['LONGHORN_PARALLEL_V1_OPERATIONS_STAGE_PASS stage='+stage+' durable_rc=0 finalized=0'
               for stage in ('maintenance','capacity','integrity','disk-lifecycle','node-failure',
                             'network-failure','last-replica','middleware')]
        disk='LONGHORN_PARALLEL_V1_OPERATIONS_STAGE_PASS stage=disk-failure durable_rc=0 finalized=0'
        for invalid in (rows,rows+[disk,disk]):
            with self.subTest(copies=invalid.count(disk)),self.assertRaisesRegex(ValueError,'stage=disk-failure'):
                self.m.owning_scope('v1-operations-v2-platform-install',invalid)

    def test_shell_exact_plan_has_every_stage_once_and_only_proven_parallel_combinations(self):
        s=(HELPERS/'longhorn-full.sh').read_text();s=s[s.index('  full)'):]
        loops=re.findall(r'for mode in\s+(.+?); do',s,re.S)
        modes=[mode for loop in loops for mode in shlex.split(loop.replace('\\\n',''))]
        self.assertEqual(set(modes),set(self.m.MODES));self.assertEqual(len(modes),len(set(modes)))
        self.assertEqual([m for m in modes if 'v1-' in m and '-v2-' in m],
            ['v1-ownership-v2-linked-acceptance','v1-operations-v2-platform-install','v1-backup-v2-data-protection','v1-stability-v2-observability'])
        self.assertEqual(modes.count('v1-upgrade-lifecycle'),1)
        for before,after in [('v2-resources-qualification','v2-aio-install-qualification'),
                ('v2-aio-install-qualification','v2-dr-target-core'),
                ('v2-dr-target-install-qualification','system-restore'),
                ('v1-backup-v2-data-protection','v1-upgrade-lifecycle'),
                ('v2-backup-store-qualification','v1-backup-v2-data-protection'),
                ('v2-dr-activation-qualification','v1-backup-v2-data-protection'),
                ('v1-backup-v2-data-protection','system-restore'),('v2-upgrade-lifecycle','v2-clone'),
                ('v2-failure','v2-upgrade-lifecycle'),
                ('v1-operations-v2-platform-install','v2-middleware'),
                ('v2-upgrade-lifecycle','v1-stability-v2-observability'),
                ('v1-stability-v2-observability','v2-middleware'),
                ('v2-middleware','v2-cleanup-target'),
                ('v2-disk-lifecycle','v2-cleanup'),('cleanup-qualification','v2-cleanup-target'),
                ('v2-cleanup-target','v2-disk-lifecycle'),('v2-cleanup','node-lifecycle'),
                ('node-lifecycle','uninstall'),('uninstall','final-clean')]:
            self.assertLess(modes.index(before),modes.index(after),(before,after))
        self.assertEqual(modes[-1],'final-clean')
        self.assertIn('release-proof target 192.168.47.149',s)
        self.assertIn('release-proof source 192.168.47.148',s)
        self.assertNotIn('setsid',s) # Existing parallel parent remains sole supervisor.

    def test_parallel_or_legacy_or_failure_partial_scope_cannot_be_generic_pass(self):
        for mode in ('v1-operations-v2-platform-install','v1-ownership-v2-linked-acceptance',
                'v1-stability-v2-observability','v1-upgrade-lifecycle','v2-upgrade-lifecycle','v1-backup-v2-data-protection','v2-failure'):
            with self.subTest(mode=mode):
                self.assertRaises(ValueError,self.m.owning_scope,mode,list(self.m.markers(mode)))

    def test_local_preflight_requires_same_bytes_after_transfer_and_no_parent_lease(self):
        for mode in self.m.LOCAL:self.receipt(mode)
        self.m.verify_preflight(self.work)
        p=self.work/'preflight/artifacts.finalized';p.write_text('143\n')
        self.assertRaises(ValueError,self.m.verify_preflight,self.work)

    def test_syntax_and_full_wiring_reuse_original_transport_and_collect_real_outer_fences(self):
        for p in [HELPERS/'longhorn-full.sh',HELPERS/'longhorn-runner.sh',HELPERS/'longhorn-live.sh',HELPERS/'longhorn-parallel.sh']:
            subprocess.run(['bash','-n',str(p)],check=True)
        s=(HELPERS/'longhorn-runner.sh').read_text()
        self.assertIn('--longhorn-only) longhorn_full_launch',s)
        self.assertIn('case "$module" in full|',s);self.assertIn('case "$mode" in full|',s)
        self.assertIn('export LONGHORN_FULL_EVIDENCE_DIR="$full_work/evidence"',s)
        self.assertIn('verify-preflight',s);self.assertIn('sync-kubeauto.sh',s)
        self.assertIn('cat /tmp/kubeauto-longhorn-gate.exit',s)
        self.assertIn('cat /tmp/kubeauto-longhorn-gate.finalized',s)
        self.assertIn('import-result',s)
        self.assertIn('exec bash "$BASE/tests/helpers/longhorn-full.sh" full',(HELPERS/'longhorn-live.sh').read_text())


    def test_outer_finalized_is_required_before_any_full_acceptance_signature(self):
        outer=self.base/'logs/longhorn-cases/full-fixture';outer.mkdir(parents=True,mode=0o700)
        result={'frozen_inputs':self.inputs,'cases':{'LONGHORN-43':{'status':'pass'}}}
        self.m.write(self.work/'full-result.json',result)
        data={'gate.log':'\n'.join(('LAB_CLEAN_VERIFY_PASS','LONGHORN_DELIVERY_BRANCH_PASS',
            'LONGHORN_FULL_CASES_VERIFIED count=54 fresh=true','LONGHORN_GATE_EXIT rc=0'))+'\n',
            'durable-exit':'0\n','durable-finalized':'143\n',
            'durable-status.txt':'state=exited rc=0 failure_markers=0\n'}
        for name,value in data.items():
            path=outer/name;path.write_text(value);path.chmod(0o600)
        with patch.object(self.m,'complete_result',return_value=result):
            self.assertRaises(ValueError,self.m.import_result,self.work,outer)
            self.assertFalse((self.work/'accepted-full.json').exists())
            (outer/'durable-finalized').write_text('0\n')
            accepted=self.m.import_result(self.work,outer)
        self.assertEqual(accepted['durable'],{'exit':0,'finalized':0})
        self.assertIs(accepted['outer_durable_required'],False)
        self.assertTrue((self.work/'accepted-full.json').exists())

    def test_original_nested_parallel_receipts_are_copied_by_exact_bytes_and_nonzero_refused(self):
        import shutil,uuid
        original=Path('/var/tmp/kubeauto-longhorn-parallel.'+uuid.uuid4().hex)
        original.mkdir(mode=0o700);self.addCleanup(shutil.rmtree,original)
        for branch in ('v1','v2'):
            for suffix in ('.log','.exit','.finalized'):
                path=original/(branch+suffix);path.write_text('original native log\n' if suffix=='.log' else '0\n');path.chmod(0o600)
        rows=self.m.archive_nested('v1-stability-v2-observability',[
            'LONGHORN_PARALLEL_DURABLE_DIRECTORY path='+str(original)])
        self.assertEqual(len(rows),6)
        for row in rows:self.assertEqual(Path(row['original']).read_bytes(),self.m.referenced(row['copy']).read_bytes())
        (original/'v1.finalized').write_text('143\n')
        self.assertRaises(ValueError,self.m.archive_nested,'other-mode',[
            'LONGHORN_PARALLEL_DURABLE_DIRECTORY path='+str(original)])

    def test_clean_full_is_blocked_when_any_owning_focused_case_is_pending(self):
        import yaml
        path=self.base/'tests/longhorn-test-matrix.yaml';path.parent.mkdir(parents=True)
        evidence=self.base/'tests/evidence/focused.json';evidence.parent.mkdir()
        evidence.write_text(json.dumps({'status':'pass','rc':0,'failure_markers':0}))
        cases=[{'id':'LONGHORN-%02d'%n,'status':'pass','implementation_status':'implemented',
            'evidence':'tests/evidence/focused.json'} for n in range(1,55)]
        cases[42]['status']='pending';path.write_text(yaml.safe_dump({'cases':cases}))
        self.m.ready() # 43 is the final clean gate itself.
        for number in (51,53):
            cases[number-1]['status']='pending';path.write_text(yaml.safe_dump({'cases':cases}))
            with self.assertRaisesRegex(ValueError,'LONGHORN-%02d'%number):self.m.ready()
            cases[number-1]['status']='pass'
        path.write_text(yaml.safe_dump({'cases':cases[:-1]}))
        self.assertRaises(ValueError,self.m.ready)


    def test_completed_disk_journal_is_original_bytes_and_uses_owning_verifier(self):
        root=self.base/'clusters/longhorn-v2-gate';root.mkdir(parents=True,mode=0o700)
        source=root/'longhorn-v2-disk-lifecycle-completed-12345678-1234-1234-1234-123456789abc.json'
        raw=b'{"original": true, "format": "retained"}\n'
        source.write_bytes(raw);source.chmod(0o600)
        marker='LONGHORN_V2_DISK_LIFECYCLE_COMPLETED path='+str(source)+' sha256='+self.m.digest(source)
        verifier=Mock()
        with patch.object(self.m,'load',side_effect=lambda name:SimpleNamespace(verify_log=verifier) if name=='longhorn-v2-disk-lifecycle.py' else self.resolver):
            rows=self.m.archive_nested('v2-disk-lifecycle',[marker])
        verifier.assert_called_once_with(marker,json.loads(raw))
        self.assertEqual(len(rows),1);self.assertEqual(rows[0]['original'],str(source))
        self.assertEqual(self.m.referenced(rows[0]['copy']).read_bytes(),raw)
        self.assertEqual(rows[0]['copy']['sha256'],self.m.digest(source))

    def test_missing_duplicate_drifted_or_foreign_disk_journal_is_refused(self):
        root=self.base/'clusters/longhorn-v2-gate';root.mkdir(parents=True,mode=0o700)
        source=root/'longhorn-v2-disk-lifecycle-completed-12345678-1234-1234-1234-123456789abc.json'
        source.write_text('{}');source.chmod(0o600)
        marker='LONGHORN_V2_DISK_LIFECYCLE_COMPLETED path='+str(source)+' sha256='+self.m.digest(source)
        foreign=self.base/'foreign.json';foreign.write_text('{}');foreign.chmod(0o600)
        for lines in ([],[marker,marker],[marker.replace(self.m.digest(source),'a'*64)],
                      [marker.replace(str(source),str(foreign))]):
            with self.subTest(lines=lines),self.assertRaises(ValueError):
                self.m.archive_nested('v2-disk-lifecycle',lines)

    def test_focused_53_dispatch_and_baseline_do_not_depend_on_full_ready(self):
        script='source "$1"\nlonghorn_launch() { printf "%s\\n" "$@"; }\nlonghorn_full_launch() { exit 99; }\nlonghorn_dispatch "$2"'
        for option,mode in (('--longhorn-v2-observability','v2-observability'),
                            ('--longhorn-v1-stability-v2-observability','v1-stability-v2-observability')):
            result=subprocess.run(['bash','-c',script,'contract',str(HELPERS/'longhorn-runner.sh'),option],
                                  text=True,capture_output=True,timeout=5)
            self.assertEqual(result.returncode,0,result.stdout+result.stderr)
            self.assertEqual(result.stdout.splitlines()[0],mode)
        import ast
        tree=ast.parse((HELPERS/'longhorn-v2-install.py').read_text())
        baseline=next(node for node in tree.body if isinstance(node,ast.FunctionDef) and node.name=='volume_baseline')
        source=ast.unparse(baseline)
        self.assertIn('upgrade.accepted(lab)',source)
        self.assertNotIn('longhorn-full-evidence',source);self.assertNotIn('.ready(',source)
        shell=(HELPERS/'longhorn-v2-observability.sh').read_text()
        self.assertNotIn('longhorn-full',shell)
        self.assertLess(shell.index('"$INSTALL" volume-baseline'),shell.index('stage cpu-isolation'))

class NodeCleanupCgroupContracts(unittest.TestCase):
    def test_stopped_runtime_with_zero_shims_still_refuses_orphan_pod_processes(self):
        source=(HELPERS/'lab-wipe-nodes.sh').read_text()
        verify=source.split("verify_cmd='\n",1)[1].split("\n'\n",1)[0]
        start=verify.index('pod_count=')
        end=verify.index('test "$pod_count" -eq 0 || bad=1',start)+len('test "$pod_count" -eq 0 || bad=1')
        check=verify[start:end]
        for groups,rc in (('0::/kubepods.slice/kubepods-burstable.slice/cri-containerd-owned.scope\n',1),
                          ('2:cpu:/kubepods/burstable/pod-owned/container\n',1),
                          ('0::/podruntime.slice/kubepods.slice/pod-owned.scope\n',1),
                          ('0::/system.slice/docker.service\n0::/user.slice/session.scope\n',0)):
            with self.subTest(groups=groups):
                script='bad=0\nps() { printf "%s" "$CGROUP_ROWS"; }\n'+check+'\nexit "$bad"\n'
                result=subprocess.run(['bash','-c',script],env={**os.environ,'CGROUP_ROWS':groups},capture_output=True,text=True,timeout=5)
                self.assertEqual(result.returncode,rc,result.stderr)

    def test_wipe_stops_owned_pod_slices_before_removing_runtime_state(self):
        source=(HELPERS/'lab-wipe-nodes.sh').read_text()
        wipe=source.split("wipe_cmd='\n",1)[1].split("\n'\n",1)[0]
        stop='systemctl stop kubepods.slice podruntime.slice 2>/dev/null'
        self.assertLess(wipe.index('systemctl stop kubelet'),wipe.index(stop))
        self.assertLess(wipe.index(stop),wipe.index('rm -rf /var/lib/kubelet'))
        self.assertIn('pod_count=',wipe)
        self.assertLess(wipe.index('test "$pod_count" -eq 0'),wipe.index('echo WIPE_OK'))

if __name__=='__main__':unittest.main(verbosity=2)
