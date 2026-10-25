#!/usr/bin/env python3
"""Targeted fresh-material contracts; no host, Kubernetes or registry operations."""
import ast
import copy
from contextlib import redirect_stdout
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[2]
HELPERS=ROOT/'tests/helpers'



def load(path):
    spec = importlib.util.spec_from_file_location(path.stem.replace('-', '_'), path)
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module


MATERIAL = load(HELPERS/'longhorn-full-material.py')
UPGRADE = load(HELPERS/'longhorn-v2-upgrade-baseline.py')
OLD_TESTS = load(ROOT/'tests/unit/test_longhorn_v2_upgrade_baseline.py')
OLD_TESTS.MODULE = UPGRADE


def private(path, text='{}'):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text); path.chmod(0o600)
    return path


class FullMaterialContracts(unittest.TestCase):
    def setUp(self):
        self.environment = patch.dict(os.environ)
        self.environment.start(); os.environ.pop(MATERIAL.ENV, None)
        self.addCleanup(self.environment.stop)

    def material(self, base):
        directory = base/'logs/fresh/evidence'; directory.mkdir(parents=True); directory.chmod(0o700)
        os.environ[MATERIAL.ENV] = str(directory)
        return directory

    def test_every_default_path_is_exact_and_does_not_require_an_existing_file(self):
        for historical in MATERIAL.FILES:
            self.assertEqual(Path('/nonexistent')/historical, MATERIAL.path('/nonexistent', historical))

    def test_explicit_mode_missing_current_material_never_falls_back_to_present_history(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary); directory = self.material(base)
            for historical, name in MATERIAL.FILES.items():
                private(base/historical)
                with self.assertRaises(FileNotFoundError): MATERIAL.path(base, historical)
                self.assertEqual(directory/name, MATERIAL.path(base, historical, optional=True))
                private(directory/name)
                self.assertEqual(directory/name, MATERIAL.path(base, historical))

    def test_empty_relative_outside_logs_and_symlink_directory_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            base=Path(temporary); directory=self.material(base)
            bad_paths=('', 'logs/fresh/evidence', str(base/'elsewhere'), str(base/'logs/fresh/../fresh/evidence'))
            (base/'logs/alias').symlink_to(directory, target_is_directory=True)
            for value in (*bad_paths,str(base/'logs/alias')):
                os.environ[MATERIAL.ENV]=value
                with self.subTest(value=value), self.assertRaises(ValueError): MATERIAL.path(base,next(iter(MATERIAL.FILES)),optional=True)

    def test_public_directory_public_file_symlink_and_foreign_owner_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            base=Path(temporary); directory=self.material(base); historical=next(iter(MATERIAL.FILES)); path=directory/MATERIAL.FILES[historical]
            private(path); directory.chmod(0o755)
            with self.assertRaises(ValueError): MATERIAL.path(base,historical)
            directory.chmod(0o700);path.chmod(0o644)
            with self.assertRaises(ValueError): MATERIAL.path(base,historical)
            path.unlink();path.symlink_to(private(base/'other.json'))
            with self.assertRaises(ValueError): MATERIAL.path(base,historical,optional=True)
            path.unlink();private(path)
            with patch.object(MATERIAL.os,'geteuid',return_value=os.geteuid()+1), self.assertRaises(ValueError): MATERIAL.path(base,historical)

    def test_unregistered_material_is_rejected_in_both_modes(self):
        with self.assertRaises(ValueError): MATERIAL.path('/tmp','tests/evidence/other.json')
        os.environ[MATERIAL.ENV]=''
        with self.assertRaises(ValueError): MATERIAL.path('/tmp','tests/evidence/other.json')

    def fixture(self, temporary):
        base, evidence, after, ref = OLD_TESTS.UpgradeBaselineContracts().fixture(temporary)
        before=evidence['transition']['before']; prior=json.loads((base/UPGRADE.PREVIOUS).read_text())
        private(base/'roles/longhorn/files/images.json',json.dumps({'1.12.1':{'longhorn-instance-manager':UPGRADE.TAG}}))
        evidence['frozen_inputs']={name:UPGRADE.digest(base/name) for name in UPGRADE.INPUTS}
        private(base/UPGRADE.EVIDENCE,json.dumps(evidence))
        directory=self.material(base)
        prior.update(signed_scope='v2-aio-install-qualification',frozen_inputs={name:UPGRADE.digest(base/name) for name in UPGRADE.INPUTS},
            native_block_receipt_sha256=before['native_block_receipt_sha256'])
        prior['independent_readback']={'installation_healthy':True,'empty_business':True,'install_business_journal_absent':True,
            'baseline':before,'managers':{name:{key:row[key] for key in ('uid','node','image')} for name,row in before['managers'].items()}}
        prior['business']={'bytes':'9437184','sha256':'1'*64,'replica_nodes':'longhorn-node-146,longhorn-node-147,longhorn-node-148'}
        lines=['LONGHORN_V2_AIO_INSTALL_QUALIFICATION_PASS','LONGHORN_V2_AIO_INSTALL_QUALIFICATION_SCOPED_CLEAN_PASS',
            'LONGHORN_INSTALL_BUSINESS_PASS','LONGHORN_BUSINESS_FIXTURES_CLEAN_PASS',
            'LONGHORN_FSYNC_REMOUNT_VERIFIED bytes=9437184 sha256='+'1'*64+' replica_nodes='+prior['business']['replica_nodes']]
        log=private(directory/'install.log','\n'.join(lines)+'\n')
        exit_path=private(directory/'install.exit','0\n');finalized=private(directory/'install.finalized','0\n')
        prior['receipt_refs']={'log':ref(log),'exit':ref(exit_path),'finalized':ref(finalized)}
        path=private(directory/'v2-install.json',json.dumps(prior))
        return base,directory,path,prior,before,ref

    def test_fresh_previous_is_real_initial_install_and_binds_exact_original_identity(self):
        with tempfile.TemporaryDirectory() as temporary:
            base,directory,path,prior,before,ref=self.fixture(temporary)
            with patch.object(UPGRADE,'BASE',base):
                self.assertEqual(prior,UPGRADE.previous())
                self.assertEqual(ref(path),UPGRADE.bind_previous(before))
            self.assertEqual('v2-aio-install-qualification',prior['signed_scope'])

    def test_fresh_previous_refuses_adoption_scope_durable_drift_input_drift_and_unproven_readback(self):
        for defect in ('scope','exit','finalized','input','health','empty','journal','manager','receipt','remote'):
            with self.subTest(defect=defect), tempfile.TemporaryDirectory() as temporary:
                base,directory,path,prior,before,ref=self.fixture(temporary)
                if defect=='scope':prior['signed_scope']='v2-im-revision-2-adopt-qualification'
                if defect in ('exit','finalized'):prior['durable'][defect]=1
                if defect=='input':(base/UPGRADE.INPUTS[0]).write_text('foreign')
                if defect in ('health','empty','journal'):
                    key={'health':'installation_healthy','empty':'empty_business','journal':'install_business_journal_absent'}[defect]
                    prior['independent_readback'][key]=False
                if defect=='manager':next(iter(prior['independent_readback']['managers'].values()))['image']='foreign:old'
                if defect=='receipt':prior['native_block_receipt_sha256']='2'*64
                if defect=='remote':prior['remote_input_verification']['mismatches']=1
                private(path,json.dumps(prior))
                with patch.object(UPGRADE,'BASE',base), self.assertRaises(ValueError):UPGRADE.previous()

    def test_original_execution_receipts_business_hash_and_unique_success_are_mandatory(self):
        for defect in ('exit','finalized','log-sha','duplicate','missing','failure','data','reference-escape'):
            with self.subTest(defect=defect),tempfile.TemporaryDirectory() as temporary:
                base,directory,path,prior,before,ref=self.fixture(temporary)
                key=defect if defect in ('exit','finalized') else 'log'; receipt=base/prior['receipt_refs'][key]['path']
                if defect in ('exit','finalized'):receipt.write_text('1\n')
                if defect=='log-sha':receipt.write_text(receipt.read_text()+'changed\n')
                if defect=='duplicate':receipt.write_text(receipt.read_text()+'LONGHORN_V2_AIO_INSTALL_QUALIFICATION_PASS\n')
                if defect=='missing':receipt.write_text(receipt.read_text().replace('LONGHORN_INSTALL_BUSINESS_PASS\n',''))
                if defect=='failure':receipt.write_text(receipt.read_text()+'LONGHORN_FIRST_FAILURE phase=business rc=1\n')
                if defect=='data':prior['business']['sha256']='3'*64
                if defect=='reference-escape':prior['receipt_refs']['log']={'path':'../elsewhere','sha256':'1'*64}
                elif defect!='log-sha':prior['receipt_refs'][key]=ref(receipt)
                private(path,json.dumps(prior))
                with patch.object(UPGRADE,'BASE',base), self.assertRaises(ValueError):UPGRADE.previous()

    def test_fresh_before_namespace_receipt_manager_and_hardware_drift_are_refused(self):
        for defect in ('namespace','receipt','manager','hardware'):
            with self.subTest(defect=defect),tempfile.TemporaryDirectory() as temporary:
                base,directory,path,prior,before,ref=self.fixture(temporary); actual=copy.deepcopy(before)
                if defect=='namespace':actual['baseline_identity']['namespace_uid']='foreign'
                if defect=='receipt':actual['native_block_receipt_sha256']='0'*64
                if defect=='manager':next(iter(actual['managers'].values()))['image']='foreign:old'
                if defect=='hardware':next(iter(actual['baseline_identity']['raw_disks'].values()))['identity']='foreign'
                with patch.object(UPGRADE,'BASE',base), self.assertRaises(ValueError):UPGRADE.bind_previous(actual)

    def test_full_controller_uid_recreation_preserves_exact_stable_storage_and_image_identity(self):
        with tempfile.TemporaryDirectory() as temporary:
            base,directory,path,prior,before,ref=self.fixture(temporary);current=copy.deepcopy(before)
            current['managers']={name+'-recreated':{**manager,'uid':manager['uid']+'-new','pod_uid':manager['pod_uid']+'-new'} for name,manager in current['managers'].items()}
            with patch.object(UPGRADE,'BASE',base):self.assertEqual(ref(path),UPGRADE.bind_previous(current))
            for defect in ('core','namespace','disk','receipt','image','digest','missing-owner'):
                changed=copy.deepcopy(current)
                if defect=='core':changed['baseline_identity']['cluster_uid']='foreign'
                if defect=='namespace':changed['baseline_identity']['namespace_uid']='foreign'
                if defect=='disk':next(iter(changed['native_block_receipt']['nodes'].values()))['disks']['raw']['disk_uuid']='foreign'
                if defect=='receipt':changed['native_block_receipt_sha256']='0'*64
                if defect=='image':next(iter(changed['managers'].values()))['image']='foreign:old'
                if defect=='digest':next(iter(changed['managers'].values()))['image_id']='containerd://sha256:'+'b'*64
                if defect=='missing-owner':next(iter(changed['managers'].values())).pop('uid')
                with self.subTest(defect=defect),self.assertRaises(ValueError):UPGRADE.stable_installation(changed,before)

    def test_missing_current_upgrade_is_the_only_allowed_optional_path(self):
        with tempfile.TemporaryDirectory() as temporary:
            base,directory,path,prior,before,ref=self.fixture(temporary)
            with patch.object(UPGRADE,'BASE',base), self.assertRaises(FileNotFoundError):UPGRADE.accepted(SimpleNamespace())
            with patch.object(UPGRADE,'BASE',base):
                self.assertEqual(directory/'v2-upgrade.json',UPGRADE.evidence_path(UPGRADE.EVIDENCE,optional=True))

    def test_default_previous_retains_strict_adoption_scope(self):
        with tempfile.TemporaryDirectory() as temporary:
            base,_,_,_=OLD_TESTS.UpgradeBaselineContracts().fixture(temporary)
            with patch.object(UPGRADE,'BASE',base): self.assertEqual('v2-im-revision-2-adopt-qualification',UPGRADE.previous()['signed_scope'])
            prior=json.loads((base/UPGRADE.PREVIOUS).read_text());prior['signed_scope']='v2-aio-install-qualification';(base/UPGRADE.PREVIOUS).write_text(json.dumps(prior))
            with patch.object(UPGRADE,'BASE',base),self.assertRaises(ValueError):UPGRADE.previous()

    def test_full_real_52_transition_uses_only_current_material_and_all_original_guards(self):
        with tempfile.TemporaryDirectory() as temporary:
            base,directory,path,prior,before,ref=self.fixture(temporary)
            evidence=json.loads((base/UPGRADE.EVIDENCE).read_text());transition=evidence['transition']
            transition['previous_evidence']=ref(path)
            preparation=base/transition['preparation']['path'];prep=json.loads(preparation.read_text())
            prep['previous_evidence']=ref(path);preparation.write_text(json.dumps(prep));transition['preparation']=ref(preparation)
            log=base/evidence['receipt_refs']['log']['path'];lines=log.read_text().splitlines()
            lines=[('LONGHORN_V2_UPGRADE_BASELINE_TRANSITION '+json.dumps(transition))
                if line.startswith('LONGHORN_V2_UPGRADE_BASELINE_TRANSITION ') else line for line in lines]
            log.write_text('\n'.join(lines)+'\n');evidence['receipt_refs']['log']=ref(log)
            private(directory/'v2-upgrade.json',json.dumps(evidence))
            after=transition['after']
            with patch.object(UPGRADE,'BASE',base),patch.object(UPGRADE,'capture',return_value=after),redirect_stdout(io.StringIO()):
                self.assertEqual(after,UPGRADE.accepted(SimpleNamespace()))
            # The reviewed historical-input adapter is a mismatch fallback only.
            revision=UPGRADE.load('longhorn-im-revision-3-adopt.py'); original_load=UPGRADE.load
            evidence['frozen_inputs'][UPGRADE.INPUTS[0]]='0'*64
            private(directory/'v2-upgrade.json',json.dumps(evidence))
            for allowed in (False,True):
                with self.subTest(reviewed_input=allowed), patch.object(UPGRADE,'BASE',base), \
                        patch.object(UPGRADE,'load',side_effect=lambda name: revision if name=='longhorn-im-revision-3-adopt.py' else original_load(name)), \
                        patch.object(revision,'gate_input_current',return_value=allowed) as compatibility:
                    if allowed:
                        self.assertEqual(after,UPGRADE.accepted_evidence()[1])
                    else:
                        with self.assertRaisesRegex(ValueError,'current frozen 52 evidence'):UPGRADE.accepted_evidence()
                    compatibility.assert_called_once_with(UPGRADE.INPUTS[0],'0'*64)
            # A current pending transition must block despite valid historical PASS.
            evidence['status']='pending';private(directory/'v2-upgrade.json',json.dumps(evidence))
            with patch.object(UPGRADE,'BASE',base),self.assertRaises(ValueError):UPGRADE.accepted(SimpleNamespace())

    def test_other_consumers_keep_frozen_input_and_installation_identity_refusals(self):
        cases={
            'longhorn-v2-resources.py':('qualification_current',None,['tests/helpers/longhorn-v2-resources.py','tests/helpers/longhorn-v2-resources.sh','roles/longhorn/files/longhorn.py'], 'v2-resources.json'),
            'longhorn-v2-backups.py':('qualification_current','lab',['tests/helpers/longhorn-backups.py','tests/helpers/longhorn-v2-backups.py','tests/helpers/longhorn-v2-backups.sh','tests/helpers/longhorn-v2-install.py'],'v2-backup-store.json'),
            'longhorn-v2-recurring.py':('selector_current','lab',['tests/helpers/longhorn-v2-recurring.py','tests/helpers/longhorn-v2-recurring.sh','tests/helpers/longhorn-recurring.py'],'v2-selector.json'),
            'longhorn-v2-disaster.py':('qualification_current',None,['roles/longhorn/files/longhorn.py','tests/helpers/longhorn-v2-dr-store.py','tests/helpers/longhorn-v2-dr-target-install.py','tests/helpers/longhorn-backups.py','tests/helpers/longhorn-v2-backups.py','tests/helpers/longhorn-lab.py'],'dr-store.json'),
        }
        for name,(method,arg,inputs,material) in cases.items():
            with self.subTest(consumer=name),tempfile.TemporaryDirectory() as temporary:
                base=Path(temporary);directory=self.material(base);module=load(HELPERS/name)
                for item in inputs:private(base/item,item)
                row={'status':'pass','rc':0,'failure_markers':0,'frozen_inputs':{item:hashlib.sha256((base/item).read_bytes()).hexdigest() for item in inputs},'baseline_identity':{'namespace_uid':'owned'}}
                path=private(directory/material,json.dumps(row));lab=SimpleNamespace(owner=lambda:{'namespace_uid':'owned'})
                args=(lab,) if arg else ()
                with patch.object(module,'BASE',base),redirect_stdout(io.StringIO()):getattr(module,method)(*args)
                row['frozen_inputs'][inputs[0]]='0'*64;private(path,json.dumps(row))
                with patch.object(module,'BASE',base),self.assertRaises(ValueError):getattr(module,method)(*args)
                if arg:
                    row['frozen_inputs'][inputs[0]]=hashlib.sha256((base/inputs[0]).read_bytes()).hexdigest()
                    row['baseline_identity']['namespace_uid']='foreign';private(path,json.dumps(row))
                    with patch.object(module,'BASE',base),self.assertRaises(ValueError):getattr(module,method)(*args)

    def test_actual_target_core_producer_and_install_consumer_close_fresh_material_boundary(self):
        producer=load(HELPERS/'longhorn-full-evidence.py')
        consumer=load(HELPERS/'longhorn-v2-dr-target-install.py')
        install=load(HELPERS/'longhorn-v2-install.py')
        with tempfile.TemporaryDirectory() as temporary:
            base=Path(temporary);directory=self.material(base)
            names=('tests/helpers/longhorn-v2-dr-target.py','tests/helpers/kubernetes-production-smoke.sh',
                'tests/helpers/longhorn-lab.py','roles/longhorn/files/longhorn.py')
            for name in names:private(base/name,name)
            smoke=hashlib.sha256((base/names[1]).read_bytes()).hexdigest()
            owner={'run_id':'run','cluster_uid':'target','node_uids':{'node':'uid'},'raw_disks':{'node':{'identity':'stable'}},'target_core_smoke_sha256':smoke}
            lab=SimpleNamespace(DIRECTORY=base/'clusters/target',core_identity=lambda:None,require_lease=lambda:None,
                owner=lambda:owner,get=lambda *a,**k:None)
            lab.DIRECTORY.mkdir(parents=True)
            row={'status':'pass','rc':0,'failure_markers':0,
                'frozen_inputs':{name:hashlib.sha256((base/name).read_bytes()).hexdigest() for name in names}}
            with patch.object(producer,'BASE',base),patch.object(consumer,'BASE',base),patch.object(install,'BASE',base), \
                    patch.object(producer,'load',return_value=SimpleNamespace(fixture=lambda:lab)), \
                    patch.object(consumer,'load',return_value=install),redirect_stdout(io.StringIO()):
                # A committed historical PASS cannot satisfy this run before production.
                historical={**row,'baseline_identity':{key:owner[key] for key in ('run_id','cluster_uid','node_uids','raw_disks')}}
                private(base/'tests/evidence/longhorn-v2-dr-target-core-20261020.json',json.dumps(historical))
                with self.assertRaises(FileNotFoundError):consumer.core_current(lab)
                produced=producer.material('v2-dr-target-core',copy.deepcopy(row),[])
                self.assertFalse(produced['independent_readback']['longhorn_installed'])
                path=private(directory/'target-core.json',json.dumps(produced))
                consumer.core_current(lab)
                owner['cluster_uid']='foreign'
                with self.assertRaises(ValueError):consumer.core_current(lab)
                owner['cluster_uid']='target';(base/names[1]).write_text('changed')
                with self.assertRaises(ValueError):consumer.core_current(lab)
                # Production itself rejects a retained storage/page-pool journal.
                private(lab.DIRECTORY/'longhorn-v2-resources.json')
                with self.assertRaises(ValueError):producer.material('v2-dr-target-core',copy.deepcopy(row),[])

    def test_original_artifact_gate_refuses_unpublished_selected_tag_before_network_or_live_work(self):
        artifacts=load(HELPERS/'longhorn-artifacts.py')
        with tempfile.TemporaryDirectory() as temporary:
            base=Path(temporary)
            private(base/'tests/evidence/longhorn-images-20261021.json',json.dumps({
                'status':'pass','images':[{'key':'longhorn-instance-manager:v1.12.1-kubeauto.2'}]}))
            selected=SimpleNamespace(component_images={'longhorn':['longhorn-instance-manager:v1.12.1-kubeauto.3']})
            with patch.object(artifacts,'ROOT',base),patch('common.constants.KubeConstant',return_value=selected), \
                    patch.object(artifacts,'read_manifest',side_effect=AssertionError('network must not start before inventory proof')) as network:
                with self.assertRaisesRegex(ValueError,'exact runtime/upgrade image inventory'):artifacts.main()
                network.assert_not_called()
        shell=(HELPERS/'longhorn-full.sh').read_text()
        self.assertIn('artifacts) exec "$PY" "$BASE/tests/helpers/longhorn-artifacts.py"',shell)

    def test_shell_activation_assertions_are_unchanged_after_path_selection(self):
        def inline(path):return path.read_text().split("<<'PY_ACTIVATION'\n",1)[1].split('\nPY_ACTIVATION',1)[0]
        expected='32ee6b5f50122c12c987259628acfeb5d0c69d6eb97c6e78cc2845ea82b86165';new=inline(HELPERS/'longhorn-v2-dr-failover.sh')
        new='\n'.join(line for line in new.splitlines() if not line.startswith(('import importlib.util','spec=','material=')))
        for historical in ('tests/evidence/longhorn-v2-dr-activation-qualification-20261021.json','tests/evidence/longhorn-v2-dr-standby-qualification-20261021.json'):
            new=new.replace("material.path(root,'"+historical+"')","root/'"+historical+"'")
        self.assertEqual(expected,hashlib.sha256(ast.dump(ast.parse(new)).encode()).hexdigest())

    def test_unchanged_consumer_acceptance_ast_except_explicit_path_seams(self):
        class Undo(ast.NodeTransformer):
            def visit_FunctionDef(self,node):
                if node.name in ('full_material','evidence_path','fresh_previous','stable_installation','current_baseline','revision3_adopted_input_current'):return None
                if node.name=='volume_baseline':
                    class RemoveRevision3Catalog(ast.NodeTransformer):
                        def visit_Assign(self,statement):
                            if any(isinstance(target,ast.Name) and target.id=='catalog' for target in statement.targets):return None
                            return self.generic_visit(statement)
                        def visit_If(self,statement):
                            if ast.unparse(statement.test)=="catalog['1.12.1']['longhorn-instance-manager'] == 'v1.12.1-kubeauto.3'":return None
                            return self.generic_visit(statement)
                    node=RemoveRevision3Catalog().visit(node)
                    node.body=[n for n in node.body if not (isinstance(n,ast.Import) and [a.name for a in n.names]==['os'])
                               and not (isinstance(n,ast.If) and 'LONGHORN_V2_RUNTIME_CONTINUATION' in ast.unparse(n.test))]
                return self.generic_visit(node)
            def visit_If(self,node):
                if ast.unparse(node.test)=='revision3_adopted_input_current(evidence, name, current)':return []
                # Added explicit full-mode branch; the original default body remains.
                if ast.unparse(node.test)=='full_material().active()':return [self.visit(n) for n in node.orelse]
                return self.generic_visit(node)
            def visit_IfExp(self,node):
                if ast.unparse(node.test)=='full_material().active()':return self.visit(node.orelse)
                return self.generic_visit(node)
            def visit_Call(self,node):
                if isinstance(node.func,ast.Name) and node.func.id=='current_baseline':
                    return ast.Call(func=ast.Name(id='capture',ctx=ast.Load()),args=[node.args[0]],keywords=[])
                if isinstance(node.func,ast.Name) and node.func.id=='evidence_path':
                    return ast.BinOp(left=ast.Name(id='BASE',ctx=ast.Load()),op=ast.Div(),right=node.args[0])
                return self.generic_visit(node)
        expected={'longhorn-v2-install.py': 'e2918c6bd8601ef46cb8e87c160809466f09d3fba4394bdbcb53e53ef24aa7fe', 'longhorn-v2-dr-store.py': '1864bc92eef21815f0e0e85f072401f369ca6a51bdc2c91d43be0b02158d3512', 'longhorn-v2-recurring.py': '5ed7fa32173bb28d4c94204b847c955205adcb6748848a3de4c7fcaeeb4467d3', 'longhorn-v2-dr-target-install.py': 'c12a0d5a12ec9b9c543d4ab3b3241b6a47a554a1ce27eefe8325965f5187d9f9', 'longhorn-v2-dr-failover.py': 'df926d7e3831e4a984c74d32763bd51d686770b439adb84ff0926794cb085d6f', 'longhorn-v2-disaster.py': '6ffef780eecdea80f1246f899d755ff56309fdd5b7f68ffa90390279fde90478', 'longhorn-v2-backups.py': '07c77cfe27fd6ae411059cce121b230822666a0f42a10a8e70383d15c25e76bc', 'longhorn-v2-resources.py': 'edda2efa624343af5eadb2aa38bc0e2b07a056e63de54e29de20abd6586c46c4'}
        for name,digest in expected.items():
            updated=Undo().visit(ast.parse((HELPERS/name).read_text()))
            self.assertEqual(hashlib.sha256(ast.dump(updated,include_attributes=False).encode()).hexdigest(),digest,name)

    def test_unique_offline_52_reader_keeps_canonical_guard_ast_and_current_material_path(self):
        def fn(text,name):return next(n for n in ast.parse(text).body if isinstance(n,ast.FunctionDef) and n.name==name)
        expected='ced0fb579de5564595abc38e171a4fb57bbafb4b36e4c949b1ec8fca2f471720'
        actual=fn((HELPERS/'longhorn-v2-upgrade-baseline.py').read_text(),'accepted_evidence')
        extension=ast.parse("revision = load('longhorn-im-revision-3-adopt.py'); revision.BASE = BASE").body
        for statement in extension:
            matches=[node for node in actual.body if ast.dump(node,include_attributes=False)==ast.dump(statement,include_attributes=False)]
            self.assertEqual(len(matches),1,'only the reviewed historical-input adapter may extend original guards')
            actual.body.remove(matches[0])
        fallback=ast.parse("row.get('frozen_inputs', {}).get(name) != digest(BASE/name) and not revision.gate_input_current(name, row.get('frozen_inputs', {}).get(name))",mode='eval').body
        matches=[node for node in ast.walk(actual) if ast.dump(node,include_attributes=False)==ast.dump(fallback,include_attributes=False)]
        self.assertEqual(len(matches),1,'the adapter must guard exactly the mismatched frozen input')
        class Undo(ast.NodeTransformer):
            def visit_BoolOp(self,node):
                if ast.dump(node,include_attributes=False)==ast.dump(fallback,include_attributes=False):
                    return self.visit(node.values[0])
                return self.generic_visit(node)
            def visit_Call(self,node):
                if isinstance(node.func,ast.Name) and node.func.id=='evidence_path':
                    return ast.BinOp(left=ast.Name(id='BASE',ctx=ast.Load()),op=ast.Div(),right=node.args[0])
                return self.generic_visit(node)
        self.assertEqual(expected,hashlib.sha256(ast.dump(Undo().visit(actual),include_attributes=False).encode()).hexdigest())
        new=(HELPERS/'longhorn-v2-upgrade-baseline.py').read_text()
        expected_functions={'capture': '6e4b838b161f9a6ac565dffad9cbdb0baf27610e5dd673146ab38b620cef1543', 'verify_execution': '66fb833512063da481da40ac00dfef9dd892d8409d69960003dc6f53a6ec1e57', 'digest': 'a9a7bc94e5bd990c54460007001935ef821103250b663d02f089924c5016ea30', 'reference': 'bd95f171e9010006cb94d4b1e4878e3b39690d8280dbceb11c8162fb402f3480', 'referenced': 'a8782e729804810057732ae1b7daa03c920698904e9ce73f5e149578f1df27ff'}
        for name,digest in expected_functions.items():
            actual=fn(new,name)
            if name=='capture':
                actual.body=[node for node in actual.body if not (isinstance(node,ast.Assign) and any(isinstance(target,ast.Name) and target.id=='tag' for target in node.targets))]
                class OriginalTag(ast.NodeTransformer):
                    def visit_Name(self,node):
                        return ast.copy_location(ast.Name(id='TAG',ctx=node.ctx),node) if node.id=='tag' else node
                actual=OriginalTag().visit(actual)
            if name == 'verify_execution':
                extension = ast.parse('''if evidence.get('checkpoint_execution'):
    restores += verify_checkpoint(evidence, result, lines)
''').body[0]
                matches = [node for node in actual.body
                           if ast.dump(node, include_attributes=False) == ast.dump(extension, include_attributes=False)]
                self.assertEqual(len(matches), 1, 'only the explicit checkpoint handoff may extend original guards')
                actual.body.remove(matches[0])
            self.assertEqual(hashlib.sha256(ast.dump(actual,include_attributes=False).encode()).hexdigest(),digest,name)


def load_tests(loader,tests,pattern):
    tests.addTests(loader.loadTestsFromTestCase(OLD_TESTS.UpgradeBaselineContracts))
    return tests


if __name__=='__main__':unittest.main(verbosity=2)
