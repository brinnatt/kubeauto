import ast
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

HELPERS = Path(__file__).resolve().parents[1]/'helpers'
def module(name):
    spec = importlib.util.spec_from_file_location(name.replace('-', '_'), HELPERS/name)
    result = importlib.util.module_from_spec(spec); spec.loader.exec_module(result)
    return result

gate = module('longhorn-v2-clone.py')

class CloneEvidenceContracts(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        for name in gate.INPUTS:
            self.write(self.base/name, 'fixture\n')
        self.baseline = {'baseline_identity': {'cluster_uid': 'core', 'namespace_uid': 'after'}, 'native': 'same-raw'}

    def write(self, path, value):
        path.parent.mkdir(parents=True, exist_ok=True); path.write_text(value)
        return path

    def fixture(self):
        directory = self.base/'logs/longhorn-cases/v2-linked-acceptance-current'
        snapshot = {'baseline': copy.deepcopy(self.baseline),
            'inputs': {name: gate.digest(self.base/name) for name in gate.INPUTS}}
        lines = ['LONGHORN_V2_CLONE_BASELINE_BEFORE '+json.dumps(snapshot, sort_keys=True)]
        for stage in gate.STAGES:
            prefix = 'LONGHORN_V2_'+stage.upper().replace('-', '_')+'_QUALIFICATION'
            lines.extend((prefix+'_SCOPED_CLEAN_PASS', prefix+'_PASS', prefix+'_EXIT rc=0',
                'LONGHORN_V2_CLONE_STAGE_RECEIPT stage='+stage+' '+json.dumps({'exit': 0, 'finalized': 0}, sort_keys=True),
                'LONGHORN_V2_LINKED_ACCEPTANCE_STAGE_PASS stage='+stage+' durable_rc=0 finalized=0'))
        lines.extend(('LONGHORN_V2_LINKED_NESTED_BUSINESS_PASS', 'LONGHORN_V2_LINKED_LIFECYCLE_PASS',
            'LONGHORN_V2_LINKED_LIFECYCLE_SCOPED_CLEAN_PASS', 'LONGHORN_V2_INCOMPLETE_CLONE_BUSINESS_PASS'))
        for prefix in ('LONGHORN_V2_LINKED_NESTED_RESULT ', 'LONGHORN_V2_LINKED_DEPENDENCY_REBUILD_VERIFIED ',
            'LONGHORN_V2_LINKED_INDEPENDENT_RESTORE_VERIFIED ', 'LONGHORN_V2_INCOMPLETE_CLONE_RESULT '):
            lines.append(prefix+json.dumps({'real_scope': 'original-owning-result'}))
        lines.extend(('LONGHORN_V2_CLONE_BASELINE_AFTER '+json.dumps(snapshot, sort_keys=True),
            'LONGHORN_V2_LINKED_ACCEPTANCE_SCOPED_CLEAN_PASS', 'LONGHORN_V2_LINKED_ACCEPTANCE_PASS', 'LONGHORN_GATE_EXIT rc=0'))
        self.write(directory/'gate.log', '\n'.join(lines)+'\n')
        for name in ('durable-exit', 'durable-finalized'): self.write(directory/name, '0\n')
        self.write(directory/'durable-status.txt', 'state=exited rc=0 failure_markers=0\n')
        return directory

    def full_fixture(self):
        canonical=self.fixture()
        directory=self.base/'logs/longhorn-full/run.20261021-120000/raw/v1-ownership-v2-linked-acceptance/kubeauto-longhorn-parallel.Ab1234'
        self.write(directory/'v2.log',(canonical/'gate.log').read_text().replace('LONGHORN_GATE_EXIT rc=0','LONGHORN_PARALLEL_V2_EXIT rc=0'))
        self.write(directory/'v2.exit','0\n');self.write(directory/'v2.finalized','0\n')
        return directory

    def test_full_original_nested_layout_uses_same49_consumer_without_synthetic_status(self):
        directory=self.full_fixture();baseline,receipt=gate.linked_execution(self.base,directory)
        self.assertEqual(baseline,self.baseline)
        self.assertEqual(set(receipt['receipts']),{'v2.log','v2.exit','v2.finalized'})
        self.assertFalse((directory/'durable-status.txt').exists())
        self.assertEqual(self.compose(directory)['matrix_item'],'LONGHORN-49')

    def test_full_layout_requires_owning_stage_and_original_parallel_exit(self):
        directory=self.full_fixture();wrong=directory.parent.with_name('foreign-stage')/directory.name
        wrong.parent.mkdir();directory.rename(wrong)
        with self.assertRaisesRegex(ValueError,'owning fixed-runner'):gate.linked_execution(self.base,wrong)
        wrong.rename(directory)
        path=directory/'v2.log';path.write_text(path.read_text().replace('LONGHORN_PARALLEL_V2_EXIT rc=0\n',''))
        with self.assertRaisesRegex(ValueError,'unique marker'):gate.linked_execution(self.base,directory)

    def test_full_original_durable_and_complete_business_assertions_remain_strict(self):
        directory=self.full_fixture();self.write(directory/'v2.finalized','143\n')
        with self.assertRaisesRegex(ValueError,'durable'):gate.linked_execution(self.base,directory)
        self.write(directory/'v2.finalized','0\n');path=directory/'v2.log'
        path.write_text(path.read_text().replace('LONGHORN_V2_INCOMPLETE_CLONE_BUSINESS_PASS\n',''))
        with self.assertRaisesRegex(ValueError,'unique marker'):gate.linked_execution(self.base,directory)

    def test_canonical_layout_cannot_skip_its_original_durable_status(self):
        directory=self.fixture();(directory/'durable-status.txt').unlink()
        with self.assertRaises(ValueError):gate.linked_execution(self.base,directory)

    def test_explicit_complete_original_linked_execution(self):
        directory = self.fixture(); baseline, receipt = gate.linked_execution(self.base, directory)
        self.assertEqual(baseline, self.baseline)
        self.assertEqual([x['stage'] for x in receipt['stages']], list(gate.STAGES))

    def test_missing_or_duplicate_native_business_result_refused(self):
        for duplicate in (False, True):
            with self.subTest(duplicate=duplicate):
                directory = self.fixture(); path = directory/'gate.log'
                target = next(x for x in path.read_text().splitlines(True) if x.startswith('LONGHORN_V2_LINKED_INDEPENDENT_RESTORE_VERIFIED '))
                path.write_text(path.read_text().replace(target, target*2 if duplicate else ''))
                with self.assertRaisesRegex(ValueError, 'unique observation'):
                    gate.linked_execution(self.base, directory)

    def test_partial_linked_missing_incomplete_cannot_accept49(self):
        directory = self.fixture(); path = directory/'gate.log'
        path.write_text(path.read_text().replace('LONGHORN_V2_INCOMPLETE_CLONE_BUSINESS_PASS\n', ''))
        with self.assertRaisesRegex(ValueError, 'unique marker'):
            gate.linked_execution(self.base, directory)

    def test_first_failure_not_hidden_by_success_markers(self):
        directory = self.fixture()
        with (directory/'gate.log').open('a') as stream: stream.write('LONGHORN_FIRST_FAILURE phase=restore rc=1\n')
        with self.assertRaisesRegex(ValueError, 'failure marker'):
            gate.linked_execution(self.base, directory)

    def test_nonzero_outer_and_nested_durable_refused(self):
        for which in ('outer', 'nested'):
            with self.subTest(which=which):
                directory = self.fixture()
                if which == 'outer': self.write(directory/'durable-finalized', '1\n')
                else:
                    path=directory/'gate.log'; path.write_text(path.read_text().replace('"finalized": 0', '"finalized": 143', 1))
                with self.assertRaisesRegex(ValueError, 'durable'):
                    gate.linked_execution(self.base, directory)

    def test_baseline_capture_and_frozen_input_change_refused(self):
        for which in ('capture', 'input'):
            with self.subTest(which=which):
                directory = self.fixture()
                if which == 'capture':
                    path=directory/'gate.log';lines=path.read_text().splitlines()
                    lines=[line.replace('"after"', '"foreign"') if line.startswith('LONGHORN_V2_CLONE_BASELINE_AFTER ') else line for line in lines]
                    path.write_text('\n'.join(lines)+'\n')
                else: self.write(self.base/gate.INPUTS[0], 'changed\n')
                with self.assertRaises(ValueError): gate.linked_execution(self.base, directory)

    def test_capture_cannot_be_moved_after_completed_stage(self):
        directory = self.fixture(); path=directory/'gate.log';lines=path.read_text().splitlines()
        first=lines.pop(0);lines.insert(6,first);path.write_text('\n'.join(lines)+'\n')
        with self.assertRaisesRegex(ValueError, 'out of order'): gate.linked_execution(self.base, directory)

    def test_full_continuation_requires_reviewed_inputs_and_original_owner_receipts(self):
        for defect in (None, 'review', 'owner', 'receipt', 'current', 'foreign-run'):
            with self.subTest(defect=defect):
                self.write(self.base/'roles/longhorn/files/images.json', 'fixture\n')
                directory = self.full_fixture()
                captured = gate.observation((directory/'v2.log').read_text().splitlines(),
                                            'LONGHORN_V2_CLONE_BASELINE_BEFORE ')
                owner = {'frozen_inputs': captured['inputs'].copy(), 'nested_receipts': [
                    {'copy': gate.reference(self.base, directory/name)}
                    for name in ('v2.log', 'v2.exit', 'v2.finalized')]}
                self.write(self.base/'roles/longhorn/files/images.json', 'reviewed manager pin\n')
                current = {name: gate.digest(self.base/name) for name in gate.INPUTS}
                reader = SimpleNamespace(work_directory=Mock(return_value=directory.parent.parent.parent),
                    check_inputs=Mock(return_value=current),
                    accepted_stage=Mock(return_value=(None, owner, None)))
                if defect == 'review': reader.check_inputs.side_effect = ValueError('unreviewed')
                if defect == 'owner': owner['frozen_inputs']['roles/longhorn/files/images.json'] = 'foreign'
                if defect == 'receipt': owner['nested_receipts'][0]['copy']['sha256'] = 'foreign'
                if defect == 'current': current['roles/longhorn/files/images.json'] = 'foreign'
                if defect == 'foreign-run': reader.work_directory.return_value = self.base/'logs/other'
                with patch.object(gate, 'load', return_value=reader):
                    if defect:
                        with self.assertRaises(ValueError): gate.linked_execution(self.base, directory)
                    else:
                        baseline, _ = gate.linked_execution(self.base, directory)
                        self.assertEqual(baseline, self.baseline)
                        reader.check_inputs.assert_called_once()
                        reader.accepted_stage.assert_called_once_with('v1-ownership-v2-linked-acceptance')

    def test_continuation_cannot_accept_changed_business_or_canonical_inputs(self):
        for full in (False, True):
            directory = self.full_fixture() if full else self.fixture()
            name = 'roles/longhorn/files/longhorn.py' if full else 'roles/longhorn/files/images.json'
            self.write(self.base/name, 'unreviewed product behavior\n')
            with patch.object(gate, 'load', side_effect=AssertionError('reject before review')):
                with self.assertRaisesRegex(ValueError, 'owning frozen inputs'):
                    gate.linked_execution(self.base, directory)

    def test_foreign_directory_and_symlink_refused(self):
        directory=self.fixture();alias=self.base/'logs/longhorn-cases/alias';alias.symlink_to(directory,target_is_directory=True)
        with self.assertRaises(ValueError):gate.linked_execution(self.base,alias)
        outside=self.base/'foreign';outside.mkdir()
        with self.assertRaises(ValueError):gate.linked_execution(self.base,outside)

    def compose(self, directory, *, side='after', reject_upgrade=False, material=False):
        before={'baseline_identity': {'cluster_uid':'core','namespace_uid':'before'}, 'native':'same-raw'}
        after=copy.deepcopy(self.baseline)
        if side=='before':
            before=copy.deepcopy(self.baseline);after['baseline_identity']['namespace_uid']='new-after'
        evidence={'transition': {'before':before, 'after':after}}
        def stable(current,baseline):
            if current!=baseline:raise ValueError('foreign core/native')
        self.write(self.base/'tests/evidence/upgrade.json','{}')
        actual_upgrade=self.write(self.base/'logs/longhorn-full/run.current/evidence/v2-upgrade.json','{\"current\":true}') if material else self.base/'tests/evidence/upgrade.json'
        def accepted():
            if reject_upgrade:raise ValueError('real52 pending')
            return evidence,after,{'legacy':'real four refusals'}
        def load(base,name):
            if name=='longhorn-v2-upgrade-baseline.py':
                interface={'accepted_evidence':accepted,'EVIDENCE':'tests/evidence/upgrade.json'}
                if material:interface['evidence_path']=lambda name:actual_upgrade
                return SimpleNamespace(**interface)
            if name=='longhorn-v2-runtime-continuation.py':return SimpleNamespace(stable=stable)
            raise AssertionError(name)
        with patch.object(gate,'preflight_full_copy',return_value={'real':'historical'}), patch.object(gate,'load',side_effect=load):
            return gate.validate_clone49(self.base,directory)

    def test52_after_namespace_transition_and_linked_before52_supported(self):
        for side in ('after','before'):
            with self.subTest(side=side):
                row=self.compose(self.fixture(),side=side)
                self.assertEqual(row['linked_upgrade_side'],side)
                self.assertEqual(row['matrix_item'],'LONGHORN-49')

    def test_full_upgrade_receipt_binds_actual_material_evidence_path(self):
        row=self.compose(self.full_fixture(),material=True)
        path=self.base/'logs/longhorn-full/run.current/evidence/v2-upgrade.json'
        self.assertEqual(row['upgrade_evidence'],{'path':str(path.relative_to(self.base)),'sha256':gate.digest(path)})

    def test_pending52_rejects_complete_linked(self):
        with self.assertRaisesRegex(ValueError,'real52 pending'):
            self.compose(self.fixture(),reject_upgrade=True)

    def test_unrelated_namespace_not_accepted_as_upgrade_transition(self):
        directory=self.fixture();self.baseline['baseline_identity']['cluster_uid']='foreign'
        with self.assertRaisesRegex(ValueError,'unrelated'):
            self.compose(directory)

class FullCopyReuseContracts(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.base=Path(self.temp.name)
        for name in gate.INPUTS:self.write(name,'fixture\n')
        self.write('roles/longhorn/files/images.json',json.dumps({'1.12.1':{'longhorn-instance-manager':'v1.12.1-kubeauto.2'}}))
        self.mechanism=self.write('tests/helpers/actual.py','def business():\n    return "pinned"\n')
        node=gate.ast.parse(self.mechanism.read_text()).body[0]
        ast_sha=hashlib.sha256(gate.ast.dump(node,include_attributes=False).encode()).hexdigest()
        self.impact={'matrix_item':'LONGHORN-49','historical_scopes':[]}
        for scope in gate.SCOPES:
            prefix='LONGHORN_V2_'+scope.upper().replace('-','_')+'_QUALIFICATION'
            log=self.write('logs/'+scope+'/gate.log','\n'.join((prefix+'_SCOPED_CLEAN_PASS',prefix+'_PASS','LONGHORN_GATE_EXIT rc=0'))+'\n')
            state=self.write('logs/'+scope+'/durable-status.txt','state=exited rc=0 failure_markers=0\n')
            row={'status':'pass','rc':0,'failure_markers':0,'remote_input_verification':{'mismatches':0},'durable_status':state.read_text(),
                'raw_logs':{str(p.relative_to(self.base)):gate.digest(p) for p in (log,state)}}
            path=self.write('tests/evidence/longhorn-v2-'+scope+'-qualification-date.json',json.dumps(row))
            self.impact['historical_scopes'].append({'evidence':str(path.relative_to(self.base)),'sha256':gate.digest(path),
                'status':'historical-scope-verified','unchanged_owning_functions_ast_sha256':{'tests/helpers/actual.py:business':ast_sha}})
        for key in ('current_adoption_dependency','artifact_publication_evidence'):
            path=self.write('tests/evidence/'+key+'.json','{}')
            self.impact[key]=gate.reference(self.base,path)
        self.write(gate.IMPACT,json.dumps(self.impact))

    def write(self,name,text):
        path=self.base/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_text(text);return path

    def test_two_historical_complete_scopes_consumed_without_live_commands(self):
        with patch.object(subprocess,'run',side_effect=AssertionError('no live command')):
            self.assertEqual(len(gate.preflight_full_copy(self.base)['full_copy']),2)

    def test_transport_closure_contains_only_already_verified_explicit_refs(self):
        expected={gate.IMPACT:gate.digest(self.base/gate.IMPACT)}
        for bound in self.impact['historical_scopes']:
            path=self.base/bound['evidence'];expected[bound['evidence']]=gate.digest(path)
            expected.update(json.loads(path.read_text())['raw_logs'])
        for key in ('current_adoption_dependency','artifact_publication_evidence'):
            ref=self.impact[key];expected[ref['path']]=ref['sha256']
        self.write('logs/foreign/unrelated.log','must not be scanned or transported')
        with patch.object(Path,'glob',side_effect=AssertionError('no logs scan')),patch.object(Path,'rglob',side_effect=AssertionError('no recursive scan')):
            self.assertEqual(gate.preflight_full_copy(self.base)['transport_refs'],expected)
        self.assertEqual(len(expected),9)

    def test_historical_raw_and_owning_function_changes_refused(self):
        raw=self.base/'logs/full-copy-clone/gate.log';original=raw.read_text();raw.write_text(original+'changed\n')
        with self.assertRaisesRegex(ValueError,'historical raw digest'):gate.preflight_full_copy(self.base)
        raw.write_text(original);self.mechanism.write_text('def business():\n    return "different"\n')
        with self.assertRaisesRegex(ValueError,'owning mechanism changed'):gate.preflight_full_copy(self.base)

    def test_one_fullcopy_scope_cannot_complete49(self):
        self.impact['historical_scopes'].pop();self.write(gate.IMPACT,json.dumps(self.impact))
        with self.assertRaisesRegex(ValueError,'incomplete scope'):gate.preflight_full_copy(self.base)

    def test_full49_real_readers_compose_original_archive_and_current52_without_live_capture(self):
        root=HELPERS.parents[1]
        spec=importlib.util.spec_from_file_location('full_material_joint_contract',root/'tests/unit/test_longhorn_full_material.py')
        full=importlib.util.module_from_spec(spec);spec.loader.exec_module(full)
        upgrade=full.UPGRADE;runtime=module('longhorn-v2-runtime-continuation.py')
        with patch.dict(os.environ):
            case=full.FullMaterialContracts()
            base,directory,prior_path,prior,before,ref=case.fixture(self.base)
            evidence=json.loads((base/upgrade.EVIDENCE).read_text());transition=evidence['transition']
            transition['previous_evidence']=ref(prior_path)
            preparation=base/transition['preparation']['path'];prep=json.loads(preparation.read_text())
            prep['previous_evidence']=ref(prior_path);preparation.write_text(json.dumps(prep));transition['preparation']=ref(preparation)
            log=base/evidence['receipt_refs']['log']['path']
            log.write_text('\n'.join(('LONGHORN_V2_UPGRADE_BASELINE_TRANSITION '+json.dumps(transition))
                if line.startswith('LONGHORN_V2_UPGRADE_BASELINE_TRANSITION ') else line for line in log.read_text().splitlines())+'\n')
            evidence['receipt_refs']['log']=ref(log)
            current=directory/'v2-upgrade.json';current.write_text(json.dumps(evidence));current.chmod(0o600)
            linked=CloneEvidenceContracts();linked.base=base;linked.baseline=before
            archive=linked.full_fixture()
            def actual_reader(base,name):
                if name=='longhorn-v2-upgrade-baseline.py':return upgrade
                if name=='longhorn-v2-runtime-continuation.py':return runtime
                raise AssertionError('unexpected reader '+name)
            with patch.object(upgrade,'BASE',base),patch.object(gate,'load',side_effect=actual_reader), \
                    patch.object(upgrade,'capture',side_effect=AssertionError('49 must not launch live capture')):
                result=gate.validate_clone49(base,archive)
                self.assertEqual('before',result['linked_upgrade_side'])
                self.assertEqual(ref(current),result['upgrade_evidence'])
                self.assertEqual(2,len(result['full_copy_reuse']['full_copy']))
                # Fresh hashes cannot hide a real failed 52 durable fence.
                fence=base/evidence['receipt_refs']['finalized']['path'];fence.write_text('1\n')
                evidence['receipt_refs']['finalized']=ref(fence);current.write_text(json.dumps(evidence))
                with self.assertRaises(ValueError):gate.validate_clone49(base,archive)
                fence.write_text('0\n');evidence['receipt_refs']['finalized']=ref(fence);current.write_text(json.dumps(evidence))
                (archive/'v2.finalized').write_text('143\n')
                with self.assertRaisesRegex(ValueError,'durable'):gate.validate_clone49(base,archive)

class UpgradeReaderContracts(unittest.TestCase):
    def test_offline_reader_preserves_strict_receipts_and_live_reader_preserves_capture(self):
        upgrade=module('longhorn-v2-upgrade-baseline.py')
        with tempfile.TemporaryDirectory() as tmp:
            base=Path(tmp);path=base/upgrade.EVIDENCE;path.parent.mkdir(parents=True)
            baseline={'namespace':'new'}
            row={'status':'pass','rc':0,'failure_markers':0,'durable':{'exit':0,'finalized':0},
                'signed_scope':'v2-upgrade-lifecycle','version':'1.12.1',
                'remote_input_verification':{'mismatches':0},'transition':{'previous_evidence':{'sha256':'bound'}},
                'independent_readback':{'installation_healthy':True,'empty_business':True,'transition_journal_absent':True,'baseline':baseline}}
            path.write_text(json.dumps(row))
            with patch.object(upgrade,'BASE',base),patch.object(upgrade,'INPUTS',()),\
                 patch.object(upgrade,'validate_transition',return_value=(baseline,{'legacy':'real'})),\
                 patch.object(upgrade,'verify_execution') as receipts,patch.object(upgrade,'capture',return_value={'namespace':'foreign'}) as capture:
                self.assertEqual(upgrade.accepted_evidence()[1],baseline);capture.assert_not_called();receipts.assert_called_once()
                with self.assertRaisesRegex(ValueError,'current native/runtime'):upgrade.accepted(object())
                receipts.side_effect=ValueError('missing original four refusals')
                with self.assertRaisesRegex(ValueError,'four refusals'):upgrade.accepted_evidence()

class CanonicalDispatchContracts(unittest.TestCase):
    def test_consumer_route_never_calls_live_launcher(self):
        runner=HELPERS/'longhorn-runner.sh'
        source=runner.read_text();start=source.index('longhorn_v2_clone() {');stop=source.index('\n}\n',start)+3
        function=source[start:stop]
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);(root/'.venv/bin').mkdir(parents=True);(root/'tests/helpers').mkdir(parents=True)
            fake=root/'.venv/bin/python';fake.write_text('#!/bin/sh\nprintf "%s\\n" "$@"\n');fake.chmod(0o700)
            script='set -e\nROOT='+str(root)+'\n'+function+'\nlonghorn_launch() { exit 99; }\n'
            failed=subprocess.run(['bash','-c',script+'longhorn_v2_clone'],text=True,capture_output=True)
            self.assertNotEqual(failed.returncode,0);self.assertIn('no-automatic-replay',failed.stderr)
            passed=subprocess.run(['bash','-c',script+'LONGHORN_V2_CLONE_DIRECTORY="'+str(root)+'/explicit"\nlonghorn_v2_clone'],text=True,capture_output=True)
            self.assertEqual(passed.returncode,0);self.assertIn('finish\n',passed.stdout);self.assertIn('/explicit',passed.stdout)

class FocusedCompositionContracts(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.base=Path(self.tmp.name);self.directory=self.base/'logs/longhorn-cases/composed'
        self.directory.mkdir(parents=True)
        self.pins={}
        for name in gate.INPUTS:
            p=self.base/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes((HELPERS.parents[1]/name).read_bytes())
            self.pins[name]=gate.digest(p)
        # A checked-in immutable source fixture avoids depending on lab logs.
        original=HELPERS.parents[1]/'tests/fixtures/longhorn-v2-clone-reviewed-focussed-origin.py.txt'
        original=self.write('logs/original-consumer.py',original.read_text())
        self.owner={key: {'identity':'same-source'} for key in ('run_id','cluster_uid','namespace_uid','node_uids','raw_disks')}
        before={'baseline':{'baseline_identity':self.owner},'inputs':{**self.pins,'tests/helpers/longhorn-v2-clone.py':gate.digest(original)}}
        parent=self.write('logs/failed-parent.log','LONGHORN_V2_CLONE_BASELINE_BEFORE '+json.dumps(before)+'\nLONGHORN_FIRST_FAILURE phase=lifecycle rc=1\nLONGHORN_GATE_EXIT rc=1\n')
        self.parent_exit=self.write('logs/failed-parent.exit','1\n')
        self.row={'schema':1,'status':'original-stage-composition','matrix_item':'LONGHORN-49','original_parent_log':self.ref(parent),'original_consumer':self.ref(original),'stages':{}}
        for stage in gate.STAGES:
            prefix='LONGHORN_V2_'+stage.upper().replace('-','_')+'_QUALIFICATION'
            lines=[prefix+'_SCOPED_CLEAN_PASS',prefix+'_PASS',prefix+'_EXIT rc=0' if stage=='linked-clone' else 'LONGHORN_GATE_EXIT rc=0']
            lines += {'linked-clone':['LONGHORN_V2_LINKED_NESTED_BUSINESS_PASS','LONGHORN_V2_LINKED_FIXTURES_CLEAN_PASS','LONGHORN_V2_LINKED_NESTED_RESULT {"result":true}'],
                'linked-lifecycle':['LONGHORN_V2_LINKED_LIFECYCLE_PASS','LONGHORN_V2_LINKED_LIFECYCLE_SCOPED_CLEAN_PASS','LONGHORN_V2_LINKED_DEPENDENCY_REBUILD_VERIFIED {"result":true}','LONGHORN_V2_LINKED_INDEPENDENT_RESTORE_VERIFIED {"result":true}'],
                'incomplete-clone':['LONGHORN_V2_INCOMPLETE_CLONE_BUSINESS_PASS','LONGHORN_V2_INCOMPLETE_CLONE_FIXTURES_CLEAN_PASS','LONGHORN_V2_INCOMPLETE_CLONE_RESULT {"result":true}']}[stage]
            pins=dict(self.pins)
            if stage=='linked-clone':pins['tests/helpers/longhorn-v2-clone.py']=gate.digest(original)
            values={'log':'\n'.join(lines)+'\n','exit':'0\n','finalized':'0\n','inputs':json.dumps(pins),'owner':json.dumps(self.owner)}
            self.row['stages'][stage]={k:self.ref(self.write('logs/'+stage+'/'+k,v)) for k,v in values.items()}
        self.save()
    def write(self,name,value):
        p=self.base/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_text(value);return p
    def ref(self,path):return gate.reference(self.base,path)
    def save(self):self.write('logs/longhorn-cases/composed/composition.json',json.dumps(self.row))
    def mutate(self,stage,kind,value):
        p=self.base/self.row['stages'][stage][kind]['path'];p.write_text(value);self.row['stages'][stage][kind]=self.ref(p);self.save()
    def test_complete_original_children_preserve_failed_parent(self):
        baseline,receipt=gate.linked_execution(self.base,self.directory)
        self.assertEqual(baseline['baseline_identity'],self.owner)
        self.assertEqual([r['stage'] for r in receipt['stages']],list(gate.STAGES))
        self.assertEqual(self.parent_exit.read_text(),'1\n')
    def test_nonzero_child_and_lost_restore_assertion_refuse(self):
        self.mutate('linked-lifecycle','finalized','143\n')
        with self.assertRaises(ValueError):gate.linked_execution(self.base,self.directory)
        self.mutate('linked-lifecycle','finalized','0\n')
        p=self.base/self.row['stages']['linked-lifecycle']['log']['path']
        self.mutate('linked-lifecycle','log','\n'.join(x for x in p.read_text().splitlines() if not x.startswith('LONGHORN_V2_LINKED_INDEPENDENT_RESTORE_VERIFIED '))+'\n')
        with self.assertRaises(ValueError):gate.linked_execution(self.base,self.directory)
    def test_changed_live_driver_input_or_source_refuses(self):
        pins=dict(self.pins);pins['tests/helpers/longhorn-v2-linked-lifecycle.py']='f'*64
        self.mutate('linked-clone','inputs',json.dumps(pins))
        gate.linked_execution(self.base,self.directory) # not executed by creation stage
        self.mutate('linked-lifecycle','inputs',json.dumps(pins))
        with self.assertRaises(ValueError):gate.linked_execution(self.base,self.directory)
        self.mutate('linked-lifecycle','inputs',json.dumps(self.pins))
        self.mutate('incomplete-clone','owner',json.dumps({**self.owner,'namespace_uid':'foreign'}))
        with self.assertRaises(ValueError):gate.linked_execution(self.base,self.directory)
    def test_later_incomplete_driver_is_not_an_earlier_stage_dependency(self):
        pins=dict(self.pins);pins['tests/helpers/longhorn-v2-incomplete-clone.py']='f'*64
        for stage in ('linked-clone','linked-lifecycle'):
            self.mutate(stage,'inputs',json.dumps(pins))
        gate.linked_execution(self.base,self.directory)
        self.mutate('incomplete-clone','inputs',json.dumps(pins))
        with self.assertRaisesRegex(ValueError,'focused input changed'):
            gate.linked_execution(self.base,self.directory)
    def test_evidence_consumer_only_change_does_not_replay_live_lifecycle(self):
        pins=dict(self.pins);pins['tests/helpers/longhorn-v2-clone.py']='e'*64
        self.mutate('linked-lifecycle','inputs',json.dumps(pins))
        gate.linked_execution(self.base,self.directory)
        p=self.base/'tests/helpers/longhorn-v2-clone.py'
        p.write_text(p.read_text().replace("'stage-receipt'","'foreign-receipt'"))
        with self.assertRaisesRegex(ValueError,'snapshot/receipt'):
            gate.linked_execution(self.base,self.directory)
    def test_creation_adoption_reader_requires_existing_exact_repair_contract(self):
        name='tests/helpers/longhorn-im-revision-3-adopt.py'
        pins=dict(self.pins);pins[name]='e'*64
        self.mutate('linked-clone','inputs',json.dumps(pins))
        checked=Mock(return_value=True)
        with patch.object(gate,'load',return_value=SimpleNamespace(gate_input_current=checked)):
            gate.linked_execution(self.base,self.directory)
        checked.assert_called_once_with(name,'e'*64)
        with patch.object(gate,'load',return_value=SimpleNamespace(gate_input_current=lambda *_:False)):
            with self.assertRaisesRegex(ValueError,'focused input changed'):
                gate.linked_execution(self.base,self.directory)
        pins['roles/longhorn/files/longhorn.py']='d'*64
        self.mutate('linked-clone','inputs',json.dumps(pins))
        with patch.object(gate,'load',return_value=SimpleNamespace(gate_input_current=lambda *_:True)):
            with self.assertRaisesRegex(ValueError,'focused input changed'):
                gate.linked_execution(self.base,self.directory)
    def test_uncalled_full_copy_reader_keeps_definition_contract(self):
        p=self.base/'tests/helpers/longhorn-v2-clone.py';source=p.read_text()
        # Its body is not on the original snapshot/stage-receipt call path.
        p.write_text(source.replace('canonical = ast.parse(ast.unparse(node)).body[0]',
                                    "raise ValueError('uncalled reader')",1))
        gate.linked_execution(self.base,self.directory)
        for changed in (source.replace('def clone_owning_ast_sha256(node, key):',
                                       'def clone_owning_ast_sha256(node, key=None):',1),
                        source.replace('def clone_owning_ast_sha256(node, key):',
                                       '@foreign\ndef clone_owning_ast_sha256(node, key):',1),
                        source.replace('def clone_owning_ast_sha256(node, key):',
                                       'def clone_owning_ast_sha256(node, foreign):',1)):
            p.write_text(changed)
            with self.assertRaisesRegex(ValueError,'snapshot/receipt'):
                gate.linked_execution(self.base,self.directory)
    def test_changed_creation_consumer_or_raw_bytes_refuses(self):
        p=self.base/'tests/helpers/longhorn-v2-clone.py';p.write_text(p.read_text().replace("'stage-receipt'","'foreign-receipt'"))
        with self.assertRaises(ValueError):gate.linked_execution(self.base,self.directory)
        p.write_bytes((HELPERS/'longhorn-v2-clone.py').read_bytes())
        log=self.base/self.row['stages']['linked-clone']['log']['path'];log.write_text(log.read_text()+'tampered\n')
        with self.assertRaises(ValueError):gate.linked_execution(self.base,self.directory)




class NullableBlockIdentityReuseContracts(unittest.TestCase):
    def test_real_block_identity_keeps_full_original_ast_except_exact_nullable_read(self):
        root = HELPERS.parents[1]
        source = (root/'roles/longhorn/files/longhorn.py').read_text()
        current = "status = (current.get('status', {}).get('diskStatus') or {}).get(name) or {}"
        previous = "status = current.get('status', {}).get('diskStatus', {}).get(name, {})"
        self.assertEqual(source.count(current), 1)
        original = source.replace(current, previous, 1)
        def function(text):
            return next(n for n in ast.parse(text).body if getattr(n, 'name', '') == 'check_block_identity')
        key = 'roles/longhorn/files/longhorn.py:check_block_identity'
        expected = hashlib.sha256(ast.dump(function(original), include_attributes=False).encode()).hexdigest()
        self.assertEqual(expected, 'b09f7c4dd9282d388bbba4a9f1d4b36d7b73413c796dabf4c941e06805ee80f2')
        self.assertEqual(gate.clone_owning_ast_sha256(function(source), key), expected)
        self.assertEqual(gate.clone_owning_ast_sha256(function(original), key), expected)
        for changed in (source.replace(current, current.replace('diskStatus', 'foreignStatus'), 1),
                        source.replace(current, current + "\n            raise ValueError('unrelated change')", 1),
                        source.replace(current, current.replace(' or {}', ' or None'), 1)):
            self.assertNotEqual(gate.clone_owning_ast_sha256(function(changed), key), expected)
        self.assertNotEqual(gate.clone_owning_ast_sha256(function(source), 'foreign:check_block_identity'), expected)


if __name__=='__main__':unittest.main()
