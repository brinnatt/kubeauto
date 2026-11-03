#!/usr/bin/env python3
"""Local-only disproofs for full-stage receipts, dependencies and launch wiring."""
import importlib.util
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import shlex
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch, Mock
from types import SimpleNamespace, ModuleType

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

    def test_protection_prefix_reuse_preserves_failed_parent_and_rejects_drift(self):
        previous=self.work/'previous.json';self.m.write(previous,{'current_inputs':self.inputs})
        children={};spans=[]
        for mode in self.m.PROTECTION_PREFIX:
            marker='LONGHORN_'+mode.upper().replace('-','_')
            raw=marker+'_SCOPED_CLEAN_PASS\n'+marker+'_PASS\n'
            path=self.work/(mode+'.log');path.write_text(raw);path.chmod(0o600)
            children[mode]=self.m.reference(path)
            spans += ['LONGHORN_V2_DATA_PROTECTION_STAGE_START stage='+mode+'\n'+raw+
                'LONGHORN_V2_DATA_PROTECTION_STAGE_PASS stage='+mode+'\n']
        raw=''.join(spans)+('ValueError: DR activation dependency changed: common/constants.py\n'
            'LONGHORN_FIRST_FAILURE phase=v2-dr-failback-qualification rc=1 diagnostics=/var/tmp/kubeauto-longhorn-v2-data-protection.fixture\n')
        refs={}
        for key,value in (('log',raw),('exit','1\n'),('finalized','1\n')):
            path=self.work/('failed-parent.'+key);path.write_text(value);path.chmod(0o600)
            refs[key]=self.m.reference(path)
        review={'protection_continuation':{'source_review':self.m.reference(previous),
            'parent':refs,'children':children}}
        def invoke():
            with patch.object(self.m,'check_inputs',return_value=self.inputs), \
                    patch.object(self.m,'resume_review',return_value=review):
                return self.m.resume_protection(self.m.PROTECTION_PREFIX[0])
        self.assertTrue(invoke())
        for fault in ('product','parent-exit','parent-failure','missing-prefix','child-marker','child-bytes','foreign-child'):
            with self.subTest(fault=fault):
                changed=json.loads(json.dumps(review));target=changed['protection_continuation']
                original=(self.work/(self.m.PROTECTION_PREFIX[0]+'.log')).read_bytes()
                altered_raw=raw
                if fault=='product':self.inputs['product.py']='b'*64
                elif fault=='parent-exit':
                    path=self.work/'success.exit';path.write_text('0\n');path.chmod(0o600)
                    target['parent']['exit']=self.m.reference(path)
                elif fault=='parent-failure':altered_raw=raw.replace('phase=v2-dr-failback-qualification','phase=v2-systemrestore-qualification')
                elif fault=='missing-prefix':altered_raw=raw.replace('LONGHORN_V2_DATA_PROTECTION_STAGE_PASS stage='+self.m.PROTECTION_PREFIX[1]+'\n','')
                else:
                    path=self.work/(self.m.PROTECTION_PREFIX[0]+'.log')
                    value=original.decode().replace('_SCOPED_CLEAN_PASS','_MISSING') if fault=='child-marker' else original.decode()+'foreign\n'
                    path.write_text(value)
                    if fault!='child-bytes':target['children'][self.m.PROTECTION_PREFIX[0]]=self.m.reference(path)
                    if fault=='child-marker':altered_raw=raw.replace(original.decode(),value)
                if altered_raw!=raw:
                    path=self.work/'altered-parent.log';path.write_text(altered_raw);path.chmod(0o600)
                    target['parent']['log']=self.m.reference(path)
                with patch.object(self.m,'check_inputs',return_value=self.inputs), \
                        patch.object(self.m,'resume_review',return_value=changed):
                    with self.assertRaises(ValueError):self.m.resume_protection(self.m.PROTECTION_PREFIX[0])
                self.inputs['product.py']='a'*64
                (self.work/(self.m.PROTECTION_PREFIX[0]+'.log')).write_bytes(original)

    def source_snapshot_fixture(self):
        self.freeze.stop()
        names = ('common/constants.py', 'conf/config.yml', 'controller/cluster/cli.py',
            'service/cluster/manager.py', 'service/cluster/downloader.py',
            'tests/longhorn-test-matrix.yaml', 'tests/run_enterprise_regression.sh',
            'tests/helpers/run-durable-gate.sh', 'tests/helpers/kubernetes-production-smoke.sh',
            'tests/helpers/lab-wipe-nodes.sh', 'roles/clean/tasks/clean_node.yml',
            'roles/longhorn/files/longhorn.py', 'tests/helpers/longhorn-full-evidence.py',
            'tests/helpers/longhorn-full-stage-verify.py')
        fixtures = ('longhorn-full-cli.py', 'longhorn-v2-install-reviewed-7afb928f.py.txt',
            'longhorn-v2-dr-target-install-reviewed-af065aa3.py.txt',
            'longhorn-v2-upgrade-baseline-reviewed-59b57a74.py.txt')
        for name in (*names, *('tests/fixtures/'+name for name in fixtures)):
            path = self.base/name; path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes((ROOT/name).read_bytes())
        return tuple('tests/fixtures/'+name for name in fixtures)

    def reviewed_repair_fixture(self):
        name='tests/helpers/longhorn-full.sh'
        original=self.work/'repair-sources'/name;original.parent.mkdir(parents=True,mode=0o700)
        original.write_text('original runner');original.chmod(0o600)
        self.inputs[name]=self.m.digest(original)
        (self.work/'frozen-inputs.json').write_text(json.dumps(self.inputs))
        current=dict(self.inputs,**{name:hashlib.sha256(b'repaired runner').hexdigest()})
        stamp=hashlib.sha256(json.dumps(current,sort_keys=True).encode()).hexdigest()
        refs={}
        for key,value in (('exit','0\n'),('finalized','0\n'),
                ('log','LONGHORN_FULL_RESUME_CONTRACT_PASS inputs='+stamp+'\n')):
            path=self.work/('contracts.'+key);path.write_text(value);path.chmod(0o600)
            refs[key]=self.m.reference(path)
        review={'scope':'full-resume-handoff-and-transport-hosts','current_inputs':current,
            'original_inputs':self.m.reference(self.work/'frozen-inputs.json'),
            'changes':{name:{'old':self.inputs[name],'current':current[name],'original':self.m.reference(original)}},
            'contracts':refs,'accepted':{},'children':{}}
        return current,review

    def pending_repair_fixture(self):
        from contextlib import redirect_stdout
        import io
        from tests.unit.test_longhorn_manager_adopt import ManagerAdoptionContracts
        from tests.unit.test_longhorn_v2_integrity import IntegrityContracts, INTEGRITY
        previous_inputs, previous = self.reviewed_repair_fixture()
        _, transition, _, _ = ManagerAdoptionContracts().pending_pins(self.base)
        for name, edge in transition['inputs'].items():
            self.inputs[name] = previous_inputs[name] = edge['old_sha256']
        (self.work/'frozen-inputs.json').write_text(json.dumps(self.inputs))
        previous['original_inputs'] = self.m.reference(self.work/'frozen-inputs.json')

        def contracts(prefix, inputs):
            stamp = hashlib.sha256(json.dumps(inputs, sort_keys=True).encode()).hexdigest()
            refs = {}
            for key in ('log', 'exit', 'finalized'):
                path = self.work/(prefix+'.'+key)
                path.write_text('LONGHORN_FULL_RESUME_CONTRACT_PASS inputs='+stamp+'\n' if key == 'log' else '0\n')
                path.chmod(0o600); refs[key] = self.m.reference(path)
            return refs

        previous['current_inputs'] = previous_inputs
        previous['contracts'] = contracts('previous-contract', previous_inputs)
        self.m.write(self.work/'resume-review.json', previous)
        self.receipt('rwo')
        with patch.object(self.m, 'frozen_inputs', return_value=previous_inputs):
            self.m.accept('rwo', self.work/'stages/rwo')
        archived = self.work/'previous-review.json'; self.m.write(archived, previous)
        current = dict(previous_inputs)
        changes = dict(previous['changes'])
        for name, edge in transition['inputs'].items():
            path = self.work/'repair-sources'/name; path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text((self.base/name).read_text().replace('v1.12.1-kubeauto.2', 'v1.12.1-kubeauto.1'))
            path.chmod(0o600); self.assertEqual(self.m.digest(path), edge['old_sha256'])
            current[name] = edge['sha256']
            changes[name] = {'old': edge['old_sha256'], 'current': edge['sha256'], 'original': self.m.reference(path)}
        name = 'tests/fixtures/longhorn-manager-pending-pin-transition.json'
        current[name] = self.m.digest(self.base/name)
        changes[name] = {'old': None, 'current': current[name], 'original': None}
        lab, journal, _ = IntegrityContracts().fixture()
        with redirect_stdout(io.StringIO()) as output: INTEGRITY.verify(lab, journal)
        log = output.getvalue()
        log += ''.join('LONGHORN_SNAPSHOT_BOUNDARY_VERIFIED type='+kind+' snapshot_sha256='+'a'*64+' source_sha256='+'b'*64+'\n' for kind in ('native', 'csi'))
        log += 'LONGHORN_FSYNC_REMOUNT_VERIFIED bytes=12 sha256='+'b'*64+' replica_nodes=a,b,c\n'
        log += 'LONGHORN_V2_EXPECTED_PRODUCT_REFUSAL_VERIFIED\nLONGHORN_BUSINESS_FIXTURES_CLEAN_PASS\nLONGHORN_SNAPSHOT_BUSINESS_PASS\n'
        result = {'before': {'daemonset_uid': 'manager'}, 'after': {'daemonset_uid': 'manager',
                  'pods': {str(n): {'image': 'registry/longhorn-manager:v1.12.1-kubeauto.2'} for n in range(3)}}}
        log += 'LONGHORN_MANAGER_ADOPT_RESULT '+json.dumps(result)+'\n'
        log += 'LONGHORN_MANAGER_ADOPT_PASS\nLONGHORN_V2_INTEGRITY_QUALIFICATION_PASS\nLONGHORN_V2_INTEGRITY_QUALIFICATION_SCOPED_CLEAN_PASS\n'
        focused = {}
        for key in ('log', 'exit', 'finalized'):
            path = self.work/('focused.'+key); path.write_text(log if key == 'log' else '0\n')
            path.chmod(0o600); focused[key] = self.m.reference(path)
        review = dict(previous, scope='full-resume-v2-manager-pending', current_inputs=current,
            changes=changes, contracts=contracts('current-contract', current),
            previous_review=self.m.reference(archived), pending_focused=focused,
            accepted={'rwo': self.m.reference(self.work/'accepted/rwo.json')})
        (self.work/'resume-review.json').write_text(json.dumps(review))
        manager = module('pending_cohort_manager', HELPERS/'longhorn-manager-adopt.py')
        self.patches.append(patch.object(self.m, 'load', side_effect=lambda name: {
            'longhorn-manager-adopt.py': manager, 'longhorn-v2-integrity.py': INTEGRITY}.get(name, self.resolver)))
        self.patches[-1].start(); self.addCleanup(self.patches[-1].stop)
        return current, review

    def test_pending_repair_preserves_the_exact_previous_cohort_without_rewriting_evidence(self):
        current, review = self.pending_repair_fixture()
        before = (self.work/'accepted/rwo.json').read_bytes()
        with patch.object(self.m, 'frozen_inputs', return_value=current):
            self.m.accepted_stage('rwo')
            self.assertEqual(before, (self.work/'accepted/rwo.json').read_bytes())
            changed = dict(current, **{'conf/config.yml': 'b'*64})
            with self.assertRaises(ValueError): self.m.resume_review(self.work, changed)

    def test_pending_repair_refuses_marker_only_restore_or_failed_durable(self):
        current, review = self.pending_repair_fixture()
        for defect in ('restore', 'exit', 'runtime'):
            with self.subTest(defect=defect):
                refs = review['pending_focused']; path = self.m.referenced(refs['log' if defect != 'exit' else 'exit'])
                saved = path.read_text()
                changed = saved.replace('type=csi', 'type=native') if defect == 'restore' else (
                    '1\n' if defect == 'exit' else saved.replace('registry/longhorn-manager:v1.12.1-kubeauto.2',
                                                               'registry/longhorn-manager:v1.12.1-kubeauto.1'))
                path.write_text(changed)
                key = 'exit' if defect == 'exit' else 'log'; refs[key] = self.m.reference(path)
                (self.work/'resume-review.json').write_text(json.dumps(review))
                with self.assertRaises(ValueError): self.m.resume_review(self.work, current)
                path.write_text(saved); refs[key] = self.m.reference(path)

    def test_pending_repair_reopens_only_case_48_without_changing_coverage(self):
        import yaml
        current, review = self.pending_repair_fixture()
        name = 'tests/longhorn-test-matrix.yaml'
        before = {'cases': [{'id': 'LONGHORN-48', 'status': 'pass'},
                            {'id': 'LONGHORN-49', 'status': 'pass'}]}
        original = self.work/'matrix-before.yaml'
        original.write_text(yaml.safe_dump(before)); original.chmod(0o600)
        path = self.base/name; path.parent.mkdir(parents=True, exist_ok=True)
        self.inputs[name] = self.m.digest(original)
        (self.work/'frozen-inputs.json').write_text(json.dumps(self.inputs))
        # The old gate-only cohort still used the original complete matrix.
        previous_path = self.m.referenced(review['previous_review'])
        previous = json.loads(previous_path.read_text())
        previous['current_inputs'][name] = self.inputs[name]
        previous_path.write_text(json.dumps(previous))
        review['previous_review'] = self.m.reference(previous_path)
        for defect in ('none', 'wrong-case', 'coverage'):
            after = json.loads(json.dumps(before))
            after['cases'][0]['status'] = 'pending'
            if defect == 'wrong-case': after['cases'][1]['status'] = 'pending'
            if defect == 'coverage': after['cases'][0]['requirement'] = 'changed'
            path.write_text(yaml.safe_dump(after)); current[name] = self.m.digest(path)
            review['changes'][name] = {'old': self.inputs[name], 'current': current[name],
                                      'original': self.m.reference(original)}
            # Test the exact matrix guard; prior-cohort contracts have their
            # own independent positive/negative tests.
            with patch.object(self.m, 'resume_review'):
                if defect == 'none': self.m.pending_resume_review(self.work, review, current)
                else:
                    with self.assertRaisesRegex(ValueError, 'unrelated matrix coverage'):
                        self.m.pending_resume_review(self.work, review, current)

    def test_focused_material_binds_the_real_install_handoff_and_restores_context(self):
        mode='v2-aio-install-qualification'; prefix=self.receipt(mode)
        refs,_=self.m.execution(mode,prefix)
        row={'receipt_refs':refs,'scope':mode}
        self.m.write(self.work/'accepted'/(mode+'.json'),row)
        baseline=SimpleNamespace(previous=Mock(return_value=row))
        prior=os.environ['LONGHORN_FULL_EVIDENCE_DIR']
        with patch.object(self.m,'load',side_effect=lambda name: baseline if name ==
                          'longhorn-v2-upgrade-baseline.py' else self.resolver):
            self.m.focused_material(self.work)
            self.assertEqual(prior,os.environ['LONGHORN_FULL_EVIDENCE_DIR'])
            Path(str(prefix)+'.exit').write_text('1\n')
            with self.assertRaises(ValueError):self.m.focused_material(self.work)
            self.assertEqual(prior,os.environ['LONGHORN_FULL_EVIDENCE_DIR'])

    def test_full_copy_continuation_preserves_original_business_and_transport_list(self):
        repair='tests/evidence/longhorn-linked-legacy-dependency-impact-20261024.json'
        previous=self.work/'previous-impact.json'
        self.m.write(previous,{'contract_receipts':{'log':{'path':'logs/old-contract.log'}}})
        original={'full_copy':['original-business'],'reuse_impact':'original-impact','revision3_reuse':'original-adoption',
            'inputs':{'tests/helpers/longhorn-im-revision-3-adopt.py':'a'*64,
                      'roles/longhorn/files/images.json':'1'*64},
            'transport_refs':{repair:self.m.digest(previous),'logs/old-contract.log':'b'*64,'logs/business.log':'c'*64}}
        current=json.loads(json.dumps(original));current['inputs']['tests/helpers/longhorn-im-revision-3-adopt.py']='d'*64
        del current['transport_refs']['logs/old-contract.log'];current['transport_refs'][repair]='e'*64
        current['transport_refs']['logs/new-contract.log']='f'*64
        path=self.work/'full-copy-reuse.json';path.write_text(json.dumps(original))
        names=self.work/'full-copy-files.txt';names.write_text(''.join(n+'\n' for n in sorted(original['transport_refs'])))
        review={'full_copy_transition':{'original':self.m.reference(path),'current':current,
            'original_retirement_impact':self.m.reference(previous)}}
        with patch.object(self.m,'full_copy_material',return_value=current),patch.object(self.m,'resume_review',return_value=review):
            self.m.verify_full_copy_transport(self.work)
            current['inputs']['roles/longhorn/files/images.json']='2'*64
            with self.assertRaises(ValueError):self.m.verify_full_copy_transport(self.work)
            review['scope']='full-resume-v2-manager-pending'
            self.m.verify_full_copy_transport(self.work)
            del review['scope'];current['inputs']['roles/longhorn/files/images.json']='1'*64
            for defect in ('business','product','raw-log','transport-list'):
                with self.subTest(defect=defect):
                    bad=json.loads(json.dumps(current))
                    if defect=='business':bad['full_copy']=['different-business']
                    elif defect=='product':bad['inputs']['product.py']='a'*64
                    elif defect=='raw-log':del bad['transport_refs']['logs/business.log']
                    else:names.write_text('unreviewed\n')
                    review['full_copy_transition']['current']=bad
                    with patch.object(self.m,'full_copy_material',return_value=bad),self.assertRaises(ValueError):
                        self.m.verify_full_copy_transport(self.work)

    def test_middleware_continuation_uses_real_business_verifier_and_rejects_incomplete_cleanup(self):
        from tests.unit.test_longhorn_v1_middleware import business_log
        real=module('middleware_continuation_contract',HELPERS/'longhorn-v1-middleware.py')
        lab=SimpleNamespace(DIRECTORY=self.work/'cluster',require_lease=Mock())
        lab.DIRECTORY.mkdir(mode=0o700)
        health=SimpleNamespace(baseline=Mock())
        def load(name):
            return {'longhorn-v1-middleware.py':real,'longhorn-lab.py':lab,
                'longhorn-v1-storage-faults.py':health}[name]
        def receipt(path,text):
            path.write_text(text);path.chmod(0o600);return self.m.reference(path)
        row={'source_review':receipt(self.work/'source-review.json',json.dumps({'current_inputs':self.inputs})),
            'stages':{}}
        specs={'monitoring-product-setup':'LONGHORN_V1_MONITORING_SETUP_PASS\n',
            'database-product-setup':'LONGHORN_MIDDLEWARE_SETUP_PASS\n',
            'database-native-recovery':business_log()+'\n'}
        for name,text in specs.items():
            row['stages'][name]={key:receipt(self.work/(name+'.'+key),
                text+'LONGHORN_MIDDLEWARE_STAGE_EXIT rc=0\n' if key=='log' else '0\n')
                for key in ('log','exit','finalized')}
        cleanup=['LONGHORN_MIDDLEWARE_CONSUMERS_SCOPED_CLEAN_PASS','LONGHORN_MIDDLEWARE_SCOPED_CLEAN_PASS',
            'LONGHORN_MIDDLEWARE_CLEANUP_PASS','LONGHORN_GATE_EXIT rc=0']
        with patch.object(self.m,'load',side_effect=load):
            accepted=self.m.middleware_continuation(row,cleanup)
            self.assertEqual(accepted.count('LONGHORN_MIDDLEWARE_PASS'),1)
            health.baseline.assert_called_once_with(lab)
            for marker in cleanup:
                with self.subTest(missing=marker),self.assertRaises(ValueError):
                    self.m.middleware_continuation(row,[line for line in cleanup if line!=marker])
            residue=lab.DIRECTORY/'longhorn-v1-monitoring.json';residue.write_text('{}')
            with self.assertRaises(ValueError):self.m.middleware_continuation(row,cleanup)
            residue.unlink()
            original=row['stages']['database-native-recovery']['log']
            path=self.m.referenced(original)
            row['stages']['database-native-recovery']['log']=receipt(path,'LONGHORN_MIDDLEWARE_BUSINESS_PASS\nLONGHORN_MIDDLEWARE_STAGE_EXIT rc=0\n')
            with self.assertRaises(ValueError):self.m.middleware_continuation(row,cleanup)
            row['stages']['database-native-recovery']['log']=receipt(path,specs['database-native-recovery']+'LONGHORN_MIDDLEWARE_STAGE_EXIT rc=0\n')
            row['stages']['database-product-setup']['exit']=receipt(self.work/'bad.exit','1\n')
            with self.assertRaises(ValueError):self.m.middleware_continuation(row,cleanup)

    def test_reviewed_handoff_retains_original_stage_but_refuses_unknown_changes_and_tampering(self):
        current,review=self.reviewed_repair_fixture()
        self.receipt('rwo');self.m.accept('rwo',self.work/'stages/rwo')
        path=self.work/'accepted/rwo.json';before=path.read_bytes()
        review['accepted']['rwo']=self.m.reference(path)
        self.m.write(self.work/'resume-review.json',review)
        with patch.object(self.m,'frozen_inputs',return_value=current):
            self.assertEqual(self.m.check_inputs(self.work),current)
            self.m.accepted_stage('rwo')
            self.assertEqual(path.read_bytes(),before)
            with patch.object(self.m,'frozen_inputs',return_value=dict(current,**{'product.py':'c'*64})):
                self.assertRaises(ValueError,self.m.check_inputs,self.work)
            original=self.work/'repair-sources/tests/helpers/longhorn-full.sh'
            original.write_text('wrong archived source')
            self.assertRaises(ValueError,self.m.check_inputs,self.work)

    def test_resume_child_requires_original_cleanup_and_zero_durable_without_replaying_failure(self):
        current,review=self.reviewed_repair_fixture()
        refs={}
        for key,value in (('exit','0\n'),('finalized','0\n'),('log',
                'LONGHORN_DISK_FAILURE_SCOPED_CLEAN_PASS\nLONGHORN_DISK_FAILURE_PASS\nLONGHORN_DISK_FAILURE_EXIT rc=0\n')):
            path=self.work/('disk.'+key);path.write_text(value);path.chmod(0o600);refs[key]=self.m.reference(path)
        review['children']['disk-failure']={'receipts':refs}
        self.m.write(self.work/'resume-review.json',review)
        parallel=module('actual_parallel_resume',HELPERS/'longhorn-parallel.py')
        original_load=self.m.load
        with patch.object(self.m,'frozen_inputs',return_value=current), \
                patch.object(self.m,'load',side_effect=lambda name:parallel if name=='longhorn-parallel.py' else original_load(name)):
            self.assertTrue(self.m.resume_child('disk-failure'))
            self.assertFalse(self.m.resume_child('middleware'))
            (self.work/'disk.exit').write_text('1\n')
            self.assertRaises(ValueError,self.m.resume_child,'disk-failure')

    def test_resume_data_protection_consumes_each_complete_original_child_only(self):
        current, review = self.reviewed_repair_fixture()
        parallel = module('actual_backup_resume', HELPERS/'longhorn-parallel.py')
        for mode in ('backup', 'backup-refusal', 'recurring'):
            refs = {}
            for key in ('log', 'exit', 'finalized'):
                path = self.work/(mode+'.'+key)
                path.write_text('\n'.join(parallel.MARKERS[mode])+'\n' if key == 'log' else '0\n')
                path.chmod(0o600); refs[key] = self.m.reference(path)
            review['children'][mode] = {'receipts': refs}
        self.m.write(self.work/'resume-review.json', review)
        original_load = self.m.load
        with patch.object(self.m, 'frozen_inputs', return_value=current), patch.object(self.m, 'load',
                side_effect=lambda name: parallel if name == 'longhorn-parallel.py' else original_load(name)):
            for mode in ('backup', 'backup-refusal', 'recurring'):
                self.assertTrue(self.m.resume_child(mode))
                path = self.work/(mode+'.log'); saved = path.read_text()
                path.write_text(saved.replace(parallel.MARKERS[mode][0]+'\n', ''))
                review['children'][mode]['receipts']['log'] = self.m.reference(path)
                (self.work/'resume-review.json').write_text(json.dumps(review))
                with self.assertRaises(ValueError): self.m.resume_child(mode)
                path.write_text(saved); review['children'][mode]['receipts']['log'] = self.m.reference(path)
                (self.work/'resume-review.json').write_text(json.dumps(review))

    def test_resume_installed_baseline_checks_storage_disks_and_both_independent_clusters(self):
        lab=Mock();faults=Mock();adopt=Mock()
        readers={'longhorn-lab.py':lab,'longhorn-v1-storage-faults.py':faults,'longhorn-v2-im-adopt.py':adopt,
                 'longhorn-full-material.py':self.resolver}
        with patch.object(self.m,'check_inputs'),patch.object(self.m,'accepted_stage') as accepted, \
                patch.object(self.m,'disk_cleanup_readback',return_value=False), \
                patch.object(self.m,'load',side_effect=readers.__getitem__), \
                patch.object(self.m,'source_readback') as source,patch.object(self.m,'target_readback') as target, \
                patch.object(self.m,'retirement_resume',return_value=''):
            self.m.resume_baseline()
            self.assertEqual(accepted.call_count,4)
            lab.require_lease.assert_called_once_with();faults.baseline.assert_called_once_with(lab)
            adopt.filesystem_disks.assert_called_once_with(lab)
            source.assert_called_once_with();target.assert_called_once_with()
            faults.baseline.side_effect=ValueError('owned business remains')
            self.assertRaisesRegex(ValueError,'business remains',self.m.resume_baseline)

    def test_completed_final_cleanup_resumes_only_readonly_clean_verification(self):
        self.m.write(self.work/'accepted/final-clean.json', {'status': 'pass'})
        lab = SimpleNamespace(execute=Mock(return_value=SimpleNamespace(stdout='LAB_CLEAN_VERIFY_PASS\n')))
        with patch.object(self.m, 'work_directory', return_value=self.work), \
                patch.object(self.m, 'check_inputs'), patch.object(self.m, 'accepted_stage') as accepted, \
                patch.object(self.m, 'absent') as absent, patch.object(self.m, 'load', return_value=lab):
            with patch('builtins.print') as printed:
                self.m.resume_baseline()
            printed.assert_any_call('LAB_CLEAN_VERIFY_PASS\n', end='', flush=True)
        accepted.assert_called_once_with('final-clean')
        self.assertEqual(4, len(list(absent.call_args.args[0])))
        command = lab.execute.call_args.args[0]
        self.assertIn('--verify', command)
        self.assertEqual(10, len([x for x in command if str(x).startswith('192.168.')]))
        with patch.object(self.m, 'work_directory', return_value=self.work), \
                patch.object(self.m, 'check_inputs'), patch.object(self.m, 'accepted_stage', side_effect=ValueError('bad receipt')), \
                patch.object(self.m, 'load', return_value=lab):
            with self.assertRaisesRegex(ValueError, 'bad receipt'): self.m.resume_baseline()
        self.assertEqual(1, lab.execute.call_count)

        for invalid in ('', 'LAB_CLEAN_VERIFY_PASS\nLAB_CLEAN_VERIFY_PASS\n'):
            lab.execute.return_value.stdout = invalid
            with patch.object(self.m, 'work_directory', return_value=self.work), \
                    patch.object(self.m, 'check_inputs'), patch.object(self.m, 'accepted_stage'), \
                    patch.object(self.m, 'absent'), patch.object(self.m, 'load', return_value=lab):
                with self.assertRaisesRegex(ValueError, 'LAB_CLEAN_VERIFY_PASS'):
                    self.m.resume_baseline()

    def test_maintenance_transition_requires_exact_reviewed_cleanup_and_never_product(self):
        name = 'tests/helpers/longhorn-v2-coexist.py'
        review = {'mount_cleanup_repair': {'files': {name: {'original': {'sha256': 'old'}}}}}
        with patch.object(self.m, 'check_inputs', return_value=self.inputs), \
                patch.object(self.m, 'resume_review', return_value=review), \
                patch.object(self.m, 'mount_cleanup_repair', return_value=True) as validated:
            self.assertTrue(self.m.maintenance_input_current(name, 'old'))
            self.assertFalse(self.m.maintenance_input_current(name, 'unknown'))
            self.assertFalse(self.m.maintenance_input_current('roles/longhorn/files/longhorn.py', 'old'))
            self.assertFalse(self.m.maintenance_input_current('tests/helpers/longhorn-v2-disk-lifecycle.py', 'old'))
            validated.return_value = False
            self.assertFalse(self.m.maintenance_input_current(name, 'old'))

    def test_resume_after_source_retirement_requires_released_empty_pool_not_installation(self):
        for mode in ('v2-cleanup','v2-cleanup-target'):
            self.m.write(self.work/'accepted'/(mode+'.json'),{})
        lab=Mock();readers={'longhorn-lab.py':lab,'longhorn-v1-storage-faults.py':Mock(),
                           'longhorn-v2-im-adopt.py':Mock(),'longhorn-full-material.py':self.resolver}
        with patch.object(self.m,'check_inputs'),patch.object(self.m,'accepted_stage') as accepted, \
                patch.object(self.m,'load',side_effect=readers.__getitem__), \
                patch.object(self.m,'disk_cleanup_readback') as disk,patch.object(self.m,'source_readback') as source, \
                patch.object(self.m,'target_readback') as target:
            self.m.resume_baseline()
            disk.assert_not_called();source.assert_not_called();target.assert_not_called()
            self.assertEqual(accepted.call_count,6)
            args=lab.execute.call_args.args[0]
            self.assertEqual(args[2:],['--verify','--rocky-only','192.168.47.146','192.168.47.147','192.168.47.148','192.168.47.149'])
            self.assertEqual(lab.execute.call_args.kwargs,{'timeout':180})
            node=self.base/'clusters/longhorn-node-gate';node.mkdir(parents=True)
            lab.execute.reset_mock()
            with patch.object(self.m,'node_join_readback',side_effect=ValueError('foreign partial join')):
                with self.assertRaisesRegex(ValueError,'foreign partial join'):self.m.resume_baseline()
            with patch.object(self.m,'node_join_readback') as readback:
                self.m.resume_baseline();readback.assert_called_once_with()
            lab.execute.assert_not_called()

    def test_partial_node_join_reuses_only_original_healthy_data_without_mutation(self):
        lab=Mock();lifecycle=Mock()
        nodes=[{'status':{'conditions':[{'type':'Ready','status':'True'}]}} for _ in range(4)]
        lab.get.return_value={'items':nodes}
        lab.replica_topology.return_value={'a':{'node':'146'},'b':{'node':'147'},'c':{'node':'148'}}
        node=Mock(NAME='149');node.lifecycle_module.return_value=node;node.fixture.return_value=lab
        node.journal.return_value={'payload':'original'};node.Lifecycle.return_value=lifecycle
        with patch.object(self.m,'load',return_value=node):
            self.m.node_join_readback()
            lifecycle.partial_add_identity.assert_called_once_with()
            lifecycle.workload.data.assert_called_once_with('original',read=True)
            lab.remote.assert_not_called();lifecycle.save.assert_not_called();lifecycle.resume_business.assert_not_called()
            lifecycle.workload.data.reset_mock()
            lifecycle.partial_add_identity.side_effect=ValueError('foreign host/boot/cert')
            with self.assertRaisesRegex(ValueError,'foreign host'):self.m.node_join_readback()
            lifecycle.workload.data.assert_not_called()
            lifecycle.partial_add_identity.side_effect=None
            nodes[0]['status']['conditions'][0]['status']='False'
            with self.assertRaisesRegex(ValueError,'unhealthy'):self.m.node_join_readback()
            nodes[0]['status']['conditions'][0]['status']='True'
            for bad in ({},{'a':{'node':'149'}}):
                lab.replica_topology.return_value=bad
                with self.assertRaisesRegex(ValueError,'three-replica'):self.m.node_join_readback()

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
        self.assertIn('--checksum --chmod=D700,F600 --files-from=',runner)
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

    def test_completed_reader_cohort_keeps_original_inputs_and_requires_exact_contracts(self):
        row = self.m.accept('rwo', self.receipt())
        path = self.work/'accepted/rwo.json'
        original = path.read_bytes()
        current = dict(self.inputs, **{'tests/helpers/longhorn-v2-clone.py': 'b'*64})
        refs = {}
        stamp = hashlib.sha256(json.dumps(self.inputs, sort_keys=True).encode()).hexdigest()
        for key, value in (('log', 'LONGHORN_FULL_RESUME_CONTRACT_PASS inputs='+stamp+'\n'),
                           ('exit', '0\n'), ('finalized', '0\n')):
            target = self.work/('cohort.'+key); target.write_text(value); target.chmod(0o600)
            refs[key] = self.m.reference(target)
        cohort = {'inputs': self.inputs, 'contracts': refs}
        review = {'accepted': {'rwo': self.m.reference(path)}, 'completed_reader_cohort': cohort}
        with patch.object(self.m, 'check_inputs', return_value=current), \
                patch.object(self.m, 'resume_review', return_value=review):
            self.assertEqual(self.m.accepted_stage('rwo')[1], row)
            self.assertEqual(path.read_bytes(), original)
            review['completed_reader_cohorts'] = [review.pop('completed_reader_cohort')]
            self.assertEqual(self.m.accepted_stage('rwo')[1], row)
            self.assertEqual(path.read_bytes(), original)
            for defect in ('product', 'marker', 'failed', 'exit'):
                with self.subTest(defect=defect):
                    if defect == 'product': current['product.py'] = 'foreign'
                    else:
                        target = self.work/('cohort.exit' if defect == 'exit' else 'cohort.log')
                        value = '1\n' if defect == 'exit' else 'wrong-marker\n' if defect == 'marker' else (
                            'LONGHORN_FULL_RESUME_CONTRACT_PASS inputs='+stamp+'\nLONGHORN_FIRST_FAILURE phase=contract rc=1\n')
                        target.write_text(value)
                        refs['exit' if defect == 'exit' else 'log'] = self.m.reference(target)
                    with self.assertRaises(ValueError): self.m.accepted_stage('rwo')
                    current['product.py'] = self.inputs['product.py']

    def support_budget_fixture(self):
        name = 'tests/helpers/longhorn-v2-observability.py'
        before = (HELPERS/'longhorn-v2-observability.py').read_text().replace(
            'SUPPORT_COMPRESSED_BYTES = 1024**3', 'SUPPORT_COMPRESSED_BYTES = 512*1024**2', 1).replace(
            'Lab limits are 1 GiB compressed', 'Lab limits are 512 MiB compressed', 1)
        original = self.work/'support-before.py'; original.write_text(before); original.chmod(0o600)
        target = self.base/name; target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text((HELPERS/'longhorn-v2-observability.py').read_text())
        review = {'support_budget_repair': {'original_helper': self.m.reference(original)},
                  'completed_support_cohort': {'inputs': {name: self.m.digest(original)}}}
        return review, {name: self.m.digest(target)}, target

    def test_completed_clone_uses_archived_owners_after_runtime_journals_are_gone(self):
        original = {'origin': 'clone-before'}; after = {'origin': 'real-upgrade-after'}
        linked = {'receipts': {'original': 'immutable'}, 'stages': ['linked', 'nested', 'incomplete']}
        source = self.work/'evidence/v2-upgrade.json'; self.m.write(source, {'original': 'accepted-upgrade'})
        proof = {'status': 'pass', 'matrix_item': 'LONGHORN-49', 'signed_scope': 'v2-clone', 'rc': 0,
            'failure_markers': 0, 'durable': {'exit': 0, 'finalized': 0}, 'baseline': original,
            'linked': linked, 'linked_upgrade_side': 'before', 'accepted_upgrade_baseline': after,
            'upgrade_evidence': self.m.reference(source), 'full_copy_reuse': {'original': 'copy'}}
        upgrade = {'transition': {'before': original, 'after': after}}
        def stable(before, candidate):
            if before != candidate: raise ValueError('changed native identity')
        modules = {'longhorn-v2-clone.py': SimpleNamespace(linked_execution=lambda *a: (original, linked)),
                   'longhorn-v2-runtime-continuation.py': SimpleNamespace(stable=stable)}
        with patch.object(self.m, 'accepted_stage', return_value=(None, upgrade, None)), \
                patch.object(self.m, 'clone_directory', return_value=self.work/'raw'), \
                patch.object(self.m, 'load', side_effect=modules.__getitem__), \
                patch.object(self.m, 'full_copy_material', return_value=proof['full_copy_reuse']), \
                patch.object(self.m, 'clone_acceptance', side_effect=AssertionError('runtime journal reopened')):
            def verify(row):
                self.m.verify_completed_clone({'clone_acceptance': row},
                    ['LONGHORN_FULL_CLONE_ACCEPTANCE '+json.dumps(row)], self.work, {})
            verify(proof)
            for field, changed in (('linked', {}), ('baseline', {}), ('linked_upgrade_side', 'after'),
                                   ('upgrade_evidence', {'path': 'foreign', 'sha256': '0'*64}),
                                   ('accepted_upgrade_baseline', original), ('rc', 1)):
                with self.subTest(field=field):
                    bad = dict(proof, **{field: changed})
                    with self.assertRaises(ValueError): verify(bad)

    def test_clone_reuse_only_updates_reviewed_reader_provenance(self):
        previous = {'full_copy_reuse': {'original': 'copy'}, 'linked': {'raw': 'same'}, 'business': 'same'}
        current = dict(previous, full_copy_reuse={'current': 'reader'})
        source = self.work/'support-previous-review.json'
        self.m.write(source, {'full_copy_transition': {'current': previous['full_copy_reuse']}})
        review = {'support_budget_repair': {'previous_review': self.m.reference(source)},
                  'full_copy_transition': {'current': current['full_copy_reuse']}}
        with patch.object(self.m, 'resume_review', return_value=review), \
                patch.object(self.m, 'support_budget_repair', return_value=True), \
                patch.object(self.m, 'verify_full_copy_transport') as transport:
            self.assertTrue(self.m.clone_receipt_matches(previous, current, self.work, {}))
            transport.assert_called_once_with(self.work)
            self.assertFalse(self.m.clone_receipt_matches(previous, dict(current, business='foreign'), self.work, {}))
            self.assertFalse(self.m.clone_receipt_matches(previous, dict(current, full_copy_reuse={'foreign': 'reader'}), self.work, {}))
            with patch.object(self.m, 'support_budget_repair', return_value=False):
                self.assertFalse(self.m.clone_receipt_matches(previous, current, self.work, {}))

    def node_cleanup_fixture(self):
        proof={'classification':'product-node-cleanup','files':{}}
        current={}
        for i,name in enumerate(self.m.NODE_RETIREMENT_REPAIRS):
            before=self.work/('node-before-'+str(i));before.write_text('old');before.chmod(0o600)
            after=self.base/name;after.parent.mkdir(parents=True,exist_ok=True);after.write_text('new');after.chmod(0o600)
            current[name]=self.m.digest(after)
            proof['files'][name]={'original':self.m.reference(before),'current':{'path':name,'sha256':current[name]}}
        row={'phase':'deleting','business_passed':True,'host':'192.168.47.149',
             **{k:k for k in ('id','run_id','cluster_uid','raw_disks','binding','payload','disk_uuids')}}
        path=self.work/'node-first.json';path.write_text(json.dumps(row));path.chmod(0o600)
        proof['first_journal']=self.m.reference(path)
        raw=self.work/'node-business.log';raw.write_text('LONGHORN_V2_DISK_LIFECYCLE_BUSINESS_PASS\n');raw.chmod(0o600)
        proof['business']=self.m.reference(raw)
        return {'node_retirement_repair':proof},current,row

    def test_node_cleanup_reuse_requires_completed_business_and_exact_current_sources(self):
        for defect in ('','source','business','scope'):
            with self.subTest(defect=defect):
                review,current,row=self.node_cleanup_fixture();proof=review['node_retirement_repair']
                if defect=='source':current[next(iter(current))]='foreign'
                elif defect=='business':
                    path=self.m.referenced(proof['business']);path.write_text(path.read_text()+'LONGHORN_FIRST_FAILURE rc=1\n');proof['business']=self.m.reference(path)
                elif defect=='scope':proof['files'].pop(next(iter(proof['files'])))
                if defect:
                    with self.assertRaises(ValueError):self.m.node_retirement_repair(review,current)
                else:self.assertTrue(self.m.node_retirement_repair(review,current))

    def test_disk_resume_refuses_replaced_or_incomplete_journal_without_running_business(self):
        review,current,row=self.node_cleanup_fixture();lab=Mock();disk=SimpleNamespace(fixture=lambda:lab,journal=lambda value:row)
        original=dict(row)
        with patch.object(self.m,'check_inputs',return_value=current),patch.object(self.m,'resume_review',return_value=review), \
                patch.object(self.m,'load',side_effect=lambda name:disk if name=='longhorn-v2-disk-lifecycle.py' else self.resolver):
            self.assertEqual(str(self.m.referenced(review['node_retirement_repair']['business'])),self.m.disk_resume())
            for field in ('id','cluster_uid','raw_disks','binding','payload','disk_uuids','phase','business_passed'):
                row.clear();row.update(original);row[field]='changed'
                with self.subTest(field=field),self.assertRaises(ValueError):self.m.disk_resume()
        lab.assert_not_called()

    def test_disk_cleanup_readback_projects_only_original_removed_client_without_writes(self):
        source=('192.168.47.146','192.168.47.147','192.168.47.148');host='192.168.47.149'
        directory=self.base/'cluster';(directory/'yml/longhorn').mkdir(parents=True)
        (directory/'hosts').write_text('original inventory')
        config={'longhorn_inventory_nodes':list(source+(host,)),
                'longhorn_client_nodes':list(source+(host,)), 'longhorn_v2_nodes':list(source),
                'longhorn_block_disks':dict.fromkeys(source, []),'longhorn_action':'present'}
        path=directory/'yml/longhorn/input.json';path.write_text(json.dumps(config));original=path.read_bytes()
        state={'namespace_uid':'ns','tls_uid':'tls','encryption_uid':'key'}
        lab=Mock(DIRECTORY=directory);lab.owner.return_value=state
        objects={('namespace',self.m.NS):'ns',('secret','longhorn-grpc-tls'):'tls',
                 ('secret','longhorn-volume-key'):'key'}
        lab.get.side_effect=lambda kind,name,*args: {'metadata':{'uid':objects[(kind,name)]}} if (kind,name) in objects else None
        product=ModuleType('disk_cleanup_product')
        product.API=Mock();product.node_map=Mock();product.healthy=Mock(return_value=True)
        # Execute the real path loader, then intercept only the product API.
        product_path=self.base/'roles/longhorn/files/longhorn.py'
        product_path.parent.mkdir(parents=True,exist_ok=True)
        product_path.write_text('pass\n')
        lifecycle=Mock();disk=SimpleNamespace(fixture=lambda:lab,journal=lambda value:{'inventory':'original inventory'},
            SOURCE=source,HOST=host,NAME='longhorn-node-149',Lifecycle=Mock(return_value=lifecycle),load=Mock(return_value=product))
        adopter=Mock();review={'accepted':{}}
        with patch.object(self.m,'work_directory',return_value=self.work), \
                patch.object(self.m,'check_inputs'),patch.object(self.m,'resume_review',return_value=review), \
                patch.object(self.m,'node_retirement_repair',return_value=True),patch.object(self.m,'disk_resume') as resume, \
                patch.object(self.m.importlib.util,'module_from_spec',return_value=product), \
                patch.object(self.m,'load',side_effect=lambda name:disk if name=='longhorn-v2-disk-lifecycle.py' else adopter):
            self.assertTrue(self.m.disk_cleanup_readback())
            c=product.healthy.call_args.args[0]
            self.assertEqual(c['longhorn_client_nodes'],list(source));self.assertEqual(c['longhorn_inventory_nodes'],list(source))
            self.assertEqual(path.read_bytes(),original);lifecycle.current.assert_called_once_with()
            self.assertEqual(lifecycle.disk.call_args_list, [unittest.mock.call(a) for a in source])
            lab.remote.assert_not_called();lab.write_private.assert_not_called();resume.assert_called_once_with()
            for key in ('longhorn_client_nodes','longhorn_inventory_nodes','longhorn_v2_nodes','longhorn_block_disks'):
                bad=dict(config);bad[key]=[];path.write_text(json.dumps(bad))
                with self.subTest(key=key),self.assertRaises(ValueError):self.m.disk_cleanup_readback()
            path.write_bytes(original);product.healthy.return_value=False
            with self.assertRaisesRegex(ValueError,'unhealthy'):self.m.disk_cleanup_readback()
            product.healthy.return_value=True;objects[('namespace',self.m.NS)]='replaced'
            with self.assertRaisesRegex(ValueError,'identity'):self.m.disk_cleanup_readback()
            review['accepted']['v2-disk-lifecycle']={}
            self.assertFalse(self.m.disk_cleanup_readback())

    def mount_fixture(self):
        proof={'classification':'test-gate','product_behavior_changed':False,'files':{}}
        originals={
            'tests/helpers/longhorn-v1-recovery-target.py': "        self.host(row,'remove')\n",
            'tests/helpers/longhorn-v2-coexist.py': "    for address in row.get('host_intents', []): host(lab, row, address, 'remove')\n"}
        replacements={
            'tests/helpers/longhorn-v1-recovery-target.py': "        self.host(row,'remove')\n        # Both the released kubelet drop-in and removed fstab mount must leave\n        # systemd's loaded dependency graph before this fixture is retired.\n        lab.remote(HOST,['systemctl','daemon-reload'])\n",
            'tests/helpers/longhorn-v2-coexist.py': "    for address in row.get('host_intents', []):\n        host(lab, row, address, 'remove')\n        lab.remote(address, ['systemctl', 'daemon-reload'])\n"}
        current={}
        for i,name in enumerate(self.m.MOUNT_REPAIRS):
            before=self.work/('before-mount-'+str(i)); before.write_text(originals.get(name,'fixture')) ;before.chmod(0o600)
            after=self.base/name;after.parent.mkdir(parents=True,exist_ok=True);after.write_text(replacements.get(name,'fixed-fixture'));after.chmod(0o600)
            proof['files'][name]={'original':self.m.reference(before),'current':self.m.reference(after)}
            current[name]=self.m.digest(after)
        failure=self.work/'mount-failure.json';failure.unlink(missing_ok=True);self.m.write(failure,{'scope':'target','phase':'native-uninstall','guards_passed':True})
        proof['failure_journal']=self.m.reference(failure)
        return {'mount_cleanup_repair':proof},current

    def test_mount_cleanup_transition_preserves_fixture_business_and_rejects_missing_proof(self):
        review,current=self.mount_fixture();self.assertTrue(self.m.mount_cleanup_repair(review,current))
        name='tests/helpers/longhorn-v2-coexist.py';path=self.base/name
        path.write_text(path.read_text()+"    erase_foreign_data()\n")
        current[name]=self.m.digest(path);review['mount_cleanup_repair']['files'][name]['current']=self.m.reference(path)
        with self.assertRaisesRegex(ValueError,'business behavior'):self.m.mount_cleanup_repair(review,current)

    def test_mount_cleanup_scope_and_original_guard_cannot_be_relaxed(self):
        for defect in ('product','scope','guard'):
            with self.subTest(defect=defect):
                review,current=self.mount_fixture();proof=review['mount_cleanup_repair']
                if defect=='product':proof['product_behavior_changed']=True
                elif defect=='scope':proof['files'].pop(next(iter(proof['files'])))
                else:
                    path=self.m.referenced(proof['failure_journal']);path.write_text('{"scope":"target","phase":"native-uninstall","guards_passed":false}')
                    proof['failure_journal']=self.m.reference(path)
                with self.assertRaises(ValueError):self.m.mount_cleanup_repair(review,current)

    def test_support_budget_transition_rejects_any_other_behavior_change(self):
        review, current, target = self.support_budget_fixture()
        self.assertTrue(self.m.support_budget_repair(review, current))
        saved = target.read_text()
        for old, new in (('SUPPORT_DISK_HEADROOM = 1024**3', 'SUPPORT_DISK_HEADROOM = 0'),
                         ('SUPPORT_EXPANDED_BYTES = 4*1024**3', 'SUPPORT_EXPANDED_BYTES = 8*1024**3'),
                         ('archive.testzip() is not None', 'False'),
                         ('not 0 < status.get(\'filesize\', 0)', 'not 1 < status.get(\'filesize\', 0)')):
            with self.subTest(change=old):
                self.assertIn(old, saved)
                target.write_text(saved.replace(old, new, 1)); current[str(target.relative_to(self.base))] = self.m.digest(target)
                with self.assertRaises(ValueError): self.m.support_budget_repair(review, current)
        target.write_text(saved); current[str(target.relative_to(self.base))] = self.m.digest(target)
        review['completed_support_cohort']['inputs'][str(target.relative_to(self.base))] = 'foreign'
        with self.assertRaises(ValueError): self.m.support_budget_repair(review, current)

    def test_degraded_transition_accepts_only_exact_reviewed_helper_and_original_budget(self):
        review, current, target = self.support_budget_fixture()
        original = self.m.referenced(review['support_budget_repair']['original_helper'])
        # A distinct original assertion proves this additional transition is
        # consumed, rather than accidentally passing through the budget branch.
        original.write_text(original.read_text()+'# exact pre-transition assertion\n')
        review['support_budget_repair']['original_helper'] = self.m.reference(original)
        review['completed_support_cohort']['inputs'][str(target.relative_to(self.base))] = self.m.digest(original)
        budget = self.work/'budget-original.py'
        budget.write_text(original.read_text().replace('SUPPORT_COMPRESSED_BYTES = 512*1024**2',
            'SUPPORT_COMPRESSED_BYTES = 1024**3', 1).replace('Lab limits are 512 MiB compressed',
            'Lab limits are 1 GiB compressed', 1)); budget.chmod(0o600)
        review['support_budget_repair']['native_degraded_repair'] = {'original_helper': self.m.reference(budget)}
        self.assertTrue(self.m.support_budget_repair(review, current))
        saved = target.read_text()
        target.write_text(saved+'# unreviewed change\n'); current[str(target.relative_to(self.base))] = self.m.digest(target)
        with self.assertRaisesRegex(ValueError, 'exact reviewed assertion'): self.m.support_budget_repair(review, current)
        target.write_text(saved); current[str(target.relative_to(self.base))] = self.m.digest(target)
        budget.write_text(budget.read_text()+'# changed original\n')
        review['support_budget_repair']['native_degraded_repair']['original_helper'] = self.m.reference(budget)
        with self.assertRaisesRegex(ValueError, 'exact reviewed assertion'): self.m.support_budget_repair(review, current)

    def test_support_cohort_preserves_original_accepted_stage_without_new_business(self):
        row = self.m.accept('rwo', self.receipt()); path = self.work/'accepted/rwo.json'; saved = path.read_bytes()
        review, current, target = self.support_budget_fixture()
        # The unchanged completed scope keeps its own old complete input set.
        review['completed_support_cohort']['inputs'].update(self.inputs)
        row['frozen_inputs'] = dict(review['completed_support_cohort']['inputs'])
        path.write_text(json.dumps(row)); saved = path.read_bytes()
        stamp = hashlib.sha256(json.dumps(row['frozen_inputs'], sort_keys=True).encode()).hexdigest()
        refs = {}
        for key, value in (('log', 'LONGHORN_FULL_RESUME_CONTRACT_PASS inputs='+stamp+'\n'), ('exit', '0\n'), ('finalized', '0\n')):
            source = self.work/('support-contract.'+key); source.write_text(value); source.chmod(0o600); refs[key] = self.m.reference(source)
        review['completed_support_cohort']['contracts'] = refs
        review['accepted'] = {'rwo': self.m.reference(path)}; current.update(self.inputs)
        with patch.object(self.m, 'check_inputs', return_value=current), patch.object(self.m, 'resume_review', return_value=review):
            self.assertEqual(self.m.accepted_stage('rwo')[1], row); self.assertEqual(path.read_bytes(), saved)
            current['product.py'] = 'changed'
            with self.assertRaises(ValueError): self.m.accepted_stage('rwo')

    def test_stability_reuse_rechecks_real_ledger_and_current_clean_baseline(self):
        from tests.unit.test_longhorn_v1_stability import fixture_log, render
        stability = module('actual_stability_reuse', HELPERS/'longhorn-v1-stability.py')
        parallel = module('actual_stability_parallel', HELPERS/'longhorn-parallel.py')
        review, current, _ = self.support_budget_fixture()
        cohort = review['completed_support_cohort']['inputs']; current.update(self.inputs)
        raw = render(fixture_log())+'\n'+'\n'.join(parallel.MARKERS['v1-stability'])+'\n'
        refs = {}
        for key, value in (('log', raw), ('exit', '0\n'), ('finalized', '0\n')):
            source = self.work/('stability.'+key); source.write_text(value); source.chmod(0o600); refs[key] = self.m.reference(source)
        review['children'] = {'v1-stability': {'receipts': refs, 'frozen_inputs': dict(cohort)}}
        lab = SimpleNamespace(require_lease=Mock())
        modules = {'longhorn-parallel.py': parallel, 'longhorn-v1-stability.py': stability,
                   'longhorn-lab.py': lab, 'longhorn-full-material.py': self.resolver}
        with patch.object(self.m, 'check_inputs', return_value=current), patch.object(self.m, 'frozen_inputs', return_value=current), \
                patch.object(self.m, 'resume_review', return_value=review), patch.object(self.m, 'load', side_effect=modules.__getitem__), \
                patch.object(stability, 'baseline') as baseline:
            self.assertTrue(self.m.resume_child('v1-stability')); baseline.assert_called_once_with(lab)
            for defect in ('ledger', 'exit', 'input', 'clean-baseline'):
                with self.subTest(defect=defect):
                    source = self.work/'stability.log'; source.write_text(raw)
                    (self.work/'stability.exit').write_text('1\n' if defect == 'exit' else '0\n')
                    if defect == 'ledger': source.write_text(raw.replace('"observed_elapsed_s": 1800.0', '"observed_elapsed_s": 1799.0'))
                    refs['log'] = self.m.reference(source); refs['exit'] = self.m.reference(self.work/'stability.exit')
                    review['children']['v1-stability']['frozen_inputs'] = {} if defect == 'input' else dict(cohort)
                    baseline.side_effect = ValueError('dirty baseline') if defect == 'clean-baseline' else None
                    with self.assertRaises(ValueError): self.m.resume_child('v1-stability')

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
        # Qualification legitimately invokes the cleanup entry six times.
        stages['cleanup-qualification'][2].extend(['LONGHORN_CLEANUP_PASS'] * 6)
        for mode,name in self.m.MATERIALS.items():
            self.m.write(self.work/'evidence'/name,stages[mode][1])
        with patch.object(self.m,'accepted_stage',side_effect=lambda mode,**kwargs:stages[mode]), \
                patch.object(self.m,'maintenance51',return_value={}):
            result=self.m.complete_result()
        refs=result['cases']['LONGHORN-19']['stages']
        self.assertEqual(set(refs),{'replica-failure','v1-operations-v2-platform-install'})
        for owner in refs:self.assertEqual(refs[owner],self.m.reference(stages[owner][0]))
        self.assertEqual(set(result['cases']['LONGHORN-41']['stages']),
                         {'cleanup', 'cleanup-qualification'})
        for markers in ([], ['LONGHORN_CLEANUP_PASS', 'LONGHORN_CLEANUP_PASS']):
            stages['cleanup'][2][:] = markers
            with patch.object(self.m,'accepted_stage',side_effect=lambda mode,**kwargs:stages[mode]), \
                    patch.object(self.m,'maintenance51',return_value={}):
                with self.assertRaisesRegex(ValueError, 'LONGHORN_CLEANUP_PASS'):
                    self.m.complete_result()
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

    def test_resume_reuses_verified_stage_and_runs_only_unaccepted_stage(self):
        """The composed full runner must exercise the real resume handoff."""
        base=self.base
        helper=base/'tests/helpers';helper.mkdir(parents=True,exist_ok=True)
        shutil.copy2(HELPERS/'longhorn-full.sh',helper/'longhorn-full.sh')
        shutil.copy2(HELPERS/'longhorn-full-stage-verify.py',helper/'longhorn-full-stage-verify.py')
        venv=base/'.venv/bin';venv.mkdir(parents=True,exist_ok=True)
        (venv/'python').symlink_to(sys.executable)
        evidence=helper/'longhorn-full-evidence.py'
        evidence.write_text('''#!/usr/bin/env python3
import json, pathlib, sys
root=pathlib.Path(__file__).resolve().parents[2]
MODES={'rwo','rwx','core'}
def accepted_stage(mode):
    path=root/'logs/longhorn-full/run.fixture'/'accepted'/(mode+'.json')
    if not path.is_file(): raise ValueError('missing accepted stage')
    return path, json.loads(path.read_text()), []
cmd=sys.argv[1]
if cmd == 'resume-baseline':
    cluster=root/'clusters/longhorn-gate'
    assert cluster.is_dir(), 'accepted core requires its retained owned cluster'
    assert not (cluster/'business.json').exists(), 'owned business remains'
    (root/'baseline-checked').write_text('healthy-empty')
    raise SystemExit(0)
if cmd in {'check-inputs','verify-preflight','init','freeze','finalize'}:
    raise SystemExit(0)
if cmd == 'release-proof':
    print('/var/tmp/proof.json'); raise SystemExit(0)
if cmd in {'clone-accept','retirement-resume'}:
    raise SystemExit(0)
if cmd == 'accept':
    mode=sys.argv[2]; prefix=pathlib.Path(sys.argv[4])
    prefix.parent.mkdir(parents=True,exist_ok=True)
    for suffix, value in (('.log','LONGHORN_STAGE_PASS\\n'),('.exit','0\\n'),('.finalized','0\\n')):
        (pathlib.Path(str(prefix)+suffix)).write_text(value)
    work=prefix.parents[1]
    (work/'accepted').mkdir(parents=True,exist_ok=True)
    (work/'accepted'/f'{mode}.json').write_text(json.dumps({'mode':mode})+'\\n')
    raise SystemExit(0)
if cmd not in MODES:
    raise SystemExit(2)
''');evidence.chmod(0o700)
        gate=helper/'run-durable-gate.sh'
        gate.write_text('''#!/usr/bin/env bash
set -u
prefix=$1; shift 2
mkdir -p "$(dirname "$prefix")"
"$@" >"$prefix.log" 2>&1; rc=$?
printf '%s\\n' "$rc" >"$prefix.exit"
printf '0\\n' >"$prefix.finalized"
exit "$rc"
''');gate.chmod(0o700)
        live=helper/'longhorn-live.sh'
        live.write_text("#!/usr/bin/env bash\nprintf '%s\\n' \"$1\" >> \"$CALLS\"\n");live.chmod(0o700)
        for name,body in {
            'longhorn-lab.py': "import pathlib, shutil, sys\nroot=pathlib.Path(__file__).resolve().parents[2]\nif sys.argv[1]=='archive-core': shutil.rmtree(root/'clusters/longhorn-gate')\n",
            'longhorn-parallel.py': "print('{}')\n",
            'lab-wipe-nodes.sh': 'if [[ "$1" == --verify ]]; then [[ ! -e "$BASE/core-active" ]]; else rm -f "$BASE/core-active"; fi\n',
        }.items():
            path=helper/name;path.write_text('#!/usr/bin/env python3\n'+body if name.endswith('.py') else '#!/usr/bin/env bash\n'+body);path.chmod(0o700)
        work=base/'logs/longhorn-full/run.fixture';(work/'evidence').mkdir(parents=True,exist_ok=True)
        (work/'stages').mkdir(exist_ok=True);(work/'accepted').mkdir(exist_ok=True)
        (work/'accepted/rwo.json').write_text('{"mode":"rwo"}\n')
        (work/'accepted/core.json').write_text('{"mode":"core"}\n')
        cluster=base/'clusters/longhorn-gate';cluster.mkdir(parents=True)
        (base/'core-active').touch()
        env=dict(os.environ,BASE=str(base),CALLS=str(base/'calls'),LONGHORN_PARENT_LEASE_HELD='yes',
             LONGHORN_FULL_RESUME='yes',LONGHORN_FULL_EVIDENCE_DIR=str(work/'evidence'))
        locks=[]
        for fd,name in ((7,'kubeauto-ceph-compute.lock'),(8,'kubeauto-redis-core-pool.lock'),(9,'kubeauto-ceph-priority.lock')):
            lock=Path('/var/lock')/name;lock.parent.mkdir(parents=True,exist_ok=True);lock.touch();locks.append(lock)
        command=['bash','-c',
            'exec 7>"/var/lock/kubeauto-ceph-compute.lock"; '
            'exec 8>"/var/lock/kubeauto-redis-core-pool.lock"; '
            'exec 9>"/var/lock/kubeauto-ceph-priority.lock"; '
            'bash "$1" full','contract',str(helper/'longhorn-full.sh')]
        result=subprocess.run(command,env=env,text=True,capture_output=True,timeout=30)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertIn('LONGHORN_FULL_STAGE_REUSED stage=rwo',result.stdout)
        self.assertNotIn('rwo\n',(base/'calls').read_text())
        self.assertIn('rwx\n',(base/'calls').read_text())
        self.assertEqual((base/'baseline-checked').read_text(),'healthy-empty')
        calls=(base/'calls').read_bytes()
        cluster.mkdir(parents=True)
        (cluster/'business.json').write_text('foreign residual business')
        refused=subprocess.run(command,env=env,text=True,capture_output=True,timeout=30)
        self.assertNotEqual(refused.returncode,0,refused.stdout+refused.stderr)
        self.assertIn('owned business remains',refused.stderr)
        self.assertEqual((base/'calls').read_bytes(),calls)

    def test_resume_directory_is_restricted_to_runner_owned_full_work(self):
        runner=(HELPERS/'longhorn-runner.sh').read_text()
        self.assertIn('LONGHORN_FULL_RESUME_DIRECTORY',runner)
        self.assertIn('^$ROOT/logs/longhorn-full/run\\.[A-Za-z0-9-]+$',runner)
        self.assertIn('verify-preflight',runner)


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
