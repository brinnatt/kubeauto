"""Exact runtime UID continuation; all fixtures and locks remain in temp."""
import ast
import copy
from contextlib import redirect_stdout
import hashlib
import importlib.util
import io
import json
import os
import subprocess
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[2]
HERE = ROOT/'tests/helpers'

def module(path, name):
    spec=importlib.util.spec_from_file_location(name,path); value=importlib.util.module_from_spec(spec); spec.loader.exec_module(value); return value

M = module(HERE/'longhorn-v2-runtime-continuation.py','continuation_contract')
LEGACY = module(ROOT/'tests/unit/test_longhorn_v2_upgrade_baseline.py','old_baseline_contract')
NEW = module(HERE/'longhorn-v2-upgrade-baseline.py','new_baseline_contract')

class ContinuationContracts(unittest.TestCase):
    def fixture(self, directory):
        base, evidence, _, _ = LEGACY.UpgradeBaselineContracts().fixture(directory)
        prior_path=base/M.ADOPTION; prior=json.loads(prior_path.read_text())
        baseline=copy.deepcopy(evidence['transition']['before'])
        def write(name, value):
            path=base/name; path.parent.mkdir(parents=True,exist_ok=True); path.write_text(value if isinstance(value,str) else json.dumps(value)); return path
        def ref(path): return {'path':str(path.relative_to(base)),'sha256':hashlib.sha256(path.read_bytes()).hexdigest()}
        write('roles/longhorn/files/images.json',{'1.12.1':{'longhorn-instance-manager':NEW.TAG}})
        prior['frozen_inputs']={n:hashlib.sha256((base/n).read_bytes()).hexdigest() for n in NEW.INPUTS}
        prior['frozen_inputs']['tests/helpers/longhorn-v2-install.py']=hashlib.sha256(write('tests/helpers/longhorn-v2-install.py','original install').read_bytes()).hexdigest()
        baseline['managers']={k:{**v,'uid':'rebuilt-'+v['uid'],'pod_uid':'rebuilt-'+v['pod_uid']} for k,v in baseline['managers'].items()}
        projection={'baseline_identity':baseline['baseline_identity'],'native_block_receipt_sha256':baseline['native_block_receipt_sha256'],
            'config_sha256':baseline['config_sha256'],'snapshot_identity':{},'required_manager_nodes':sorted(r['node'] for r in baseline['managers'].values()),
            'disk_uuids':{r['name']+'/'+n:d['disk_uuid'] for r in baseline['native_block_receipt']['nodes'].values() for n,d in r['disks'].items()}}
        prior['independent_readback']['baseline']=projection
        adoption_log=write('logs/adoption/v1.log','\n'.join(('LONGHORN_V2_IM_REVISION_2_ADOPT_BUSINESS_PASS',
            'LONGHORN_V2_IM_REVISION_2_ADOPT_FIXTURES_CLEAN_PASS','LONGHORN_V2_IM_REVISION_2_ADOPT_QUALIFICATION_SCOPED_CLEAN_PASS',
            'LONGHORN_V2_IM_REVISION_2_ADOPT_QUALIFICATION_PASS'))+'\n')
        prior['raw_evidence']={}
        for path in (adoption_log,write('logs/adoption/v1.exit','0\n'),write('logs/adoption/v1.finalized','0\n')):
            prior['raw_evidence'][str(path.relative_to(base))]=ref(path)['sha256']
        publication={'status':'pass','durable_rc':0,'failure_markers':0,'selected_image_count':1,
            'publication_record':{'key':'brinnatt/longhorn-instance-manager:v1.12.1-kubeauto.2','digest':'sha256:'+'c'*64,
                'platforms':{'linux/amd64':'sha256:'+'a'*64,'linux/arm64':'sha256:'+'b'*64}}}
        prior['transition']={'publication_evidence':ref(write('tests/evidence/publication.json',publication))}
        write(M.ADOPTION,prior)
        refs={}
        for scope in M.SCOPES:
            prefix='logs/failures/'+scope
            path=write(prefix+'.log','OWNING_BUSINESS_PASS\nLONGHORN_V2_FAILURE_STAGE_EXIT rc=0\n')
            raw={str(p.relative_to(base)):ref(p)['sha256'] for p in (path,write(prefix+'.exit','0\n'),write(prefix+'.finalized','0\n'))}
            row={'status':'pass','rc':0,'failure_markers':0,'durable':{'exit':0,'finalized':0},'signed_scope':'v2-failure/'+scope,
                'version':'1.12.1','instance_manager_version':'v1.12.1-kubeauto.2','remote_input_verification':{'mismatches':0},
                'raw_evidence':raw,'frozen_inputs':prior['frozen_inputs']}
            refs[scope]=ref(write('tests/evidence/failure-'+scope+'.json',row))
        changes={}
        for name in M.SEAMS:
            changes[name]={'old_sha256':prior['frozen_inputs'][name],'sha256':ref(write(name,'reviewed seam '+name))['sha256']}
        helper=write(M.HELPER,(HERE/'longhorn-v2-runtime-continuation.py').read_text())
        receipts={key:ref(write('logs/contracts/contract.'+key,'LONGHORN_V2_RUNTIME_CONTINUATION_CONTRACT_PASS\n' if key=='log' else '0\n')) for key in ('log','exit','finalized')}
        inputs={**{name:change['sha256'] for name,change in changes.items()},M.HELPER:ref(helper)['sha256']}
        contract_log=base/receipts['log']['path'];contract_log.write_text(contract_log.read_text()+'LONGHORN_V2_RUNTIME_CONTINUATION_CONTRACT_INPUTS '+json.dumps(inputs,sort_keys=True)+'\n')
        receipts['log']=ref(contract_log)
        compat={'scope':'focused-runtime-continuation-gate-seams','product_behavior_changed':False,'changes':changes,
            'helper_sha256':ref(helper)['sha256'],'contract_receipts':receipts}
        compat_path=write('tests/evidence/compatibility.json',compat)
        row={'schema':1,'scope':'focused-runtime-continuation','fresh_installation':False,'previous_evidence':ref(prior_path),
            'compatibility':ref(compat_path),'failure_scopes':refs,'baseline':baseline,'adoption_projection':projection,
            'frozen_inputs':{name:ref(base/name)['sha256'] for name in set(prior['frozen_inputs'])|{M.HELPER}}}
        runtime=base/'logs/runtime'; runtime.mkdir(mode=0o700)
        material=write('logs/runtime/continuation.json',row); material.chmod(0o600)
        return base, row, material, ref, write

    def contexts(self, base, material):
        return patch.object(M,'BASE',base), patch.dict(os.environ,{M.ENV:str(material)})

    def read(self, base, material):
        a,b=self.contexts(base,material)
        with a,b,patch.object(M,'load',return_value=SimpleNamespace(verify_log=Mock())):
            return M.material()

    def refresh(self, path, row): path.write_text(json.dumps(row))

    def test_complete_nine_scope_continuation_accepts_real_ephemeral_uid_change_only(self):
        with tempfile.TemporaryDirectory() as d:
            base,row,path,_,_=self.fixture(d); self.assertEqual(row,self.read(base,path))
            current=copy.deepcopy(row['baseline'])
            current['managers']={k+'-new':{**r,'uid':r['uid']+'-new','pod_uid':r['pod_uid']+'-new'} for k,r in current['managers'].items()}
            self.assertEqual(current,M.stable(current,row['baseline']))

    def test_stable_identity_refuses_core_namespace_raw_disk_receipt_config_or_retained_changes(self):
        with tempfile.TemporaryDirectory() as d:
            _,row,_,_,_=self.fixture(d)
            for key in ('baseline_identity','native_block_receipt_sha256','native_block_receipt','config_sha256','retained'):
                current=copy.deepcopy(row['baseline']); current[key]='foreign'
                with self.subTest(key=key),self.assertRaises(ValueError): M.stable(current,row['baseline'])

    def test_stable_identity_refuses_runtime_digest_image_wrong_node_missing_or_duplicate_owner(self):
        with tempfile.TemporaryDirectory() as d:
            _,row,_,_,_=self.fixture(d)
            for defect in ('digest','image','node','uid','pod_uid','duplicate'):
                current=copy.deepcopy(row['baseline']); values=list(current['managers'].values()); first=values[0]
                if defect=='digest':first['image_id']='containerd://sha256:'+'f'*64
                if defect=='image':first['image']='foreign/longhorn-instance-manager:v1.12.1-kubeauto.2'
                if defect=='node':first['node']='foreign'
                if defect in ('uid','pod_uid'):first[defect]=''
                if defect=='duplicate':first['uid']=values[1]['uid']
                with self.subTest(defect=defect),self.assertRaises(ValueError):M.stable(current,row['baseline'])

    def test_missing_duplicate_or_failed_scope_never_consumes_a_failed_parent(self):
        for defect in ('missing','duplicate','status','scope','durable','failure','input'):
            with self.subTest(defect=defect),tempfile.TemporaryDirectory() as d:
                base,row,path,ref,_=self.fixture(d)
                if defect=='missing': row['failure_scopes'].pop('manager')
                elif defect=='duplicate':row['failure_scopes']['manager']=row['failure_scopes']['replica-im']
                else:
                    p=base/row['failure_scopes']['manager']['path']; r=json.loads(p.read_text())
                    if defect=='status':r['status']='failed'
                    if defect=='scope':r['signed_scope']='v2-failure/failed-parent'
                    if defect=='durable':r['durable']['finalized']=1
                    if defect=='failure':r['failure_markers']=1
                    if defect=='input':r['frozen_inputs'][NEW.INPUTS[0]]='0'*64
                    self.refresh(p,r);row['failure_scopes']['manager']=ref(p)
                self.refresh(path,row)
                with self.assertRaises(ValueError):self.read(base,path)

    def test_scope_reads_original_durable_raw_and_calls_exact_owning_verifier(self):
        with tempfile.TemporaryDirectory() as d:
            base,row,path,_,_=self.fixture(d); verifier=Mock()
            with patch.object(M,'BASE',base),patch.object(M,'load',return_value=SimpleNamespace(verify_log=verifier)) as loader:
                M.verify_scope(row['failure_scopes']['remote-spdk'],'remote-spdk')
                loader.assert_called_once_with('longhorn-v2-runtime-faults.py');verifier.assert_called_once()
                self.assertEqual('remote-spdk',verifier.call_args.args[1])

    def test_scope_rejects_raw_drift_nonzero_missing_finalization_and_failure_marker(self):
        for defect in ('bytes','nonzero','missing','marker','duplicate-exit','wrong-sha'):
            with self.subTest(defect=defect),tempfile.TemporaryDirectory() as d:
                base,row,path,ref,_=self.fixture(d);p=base/row['failure_scopes']['manager']['path'];scope=json.loads(p.read_text())
                raw=base/'logs/failures/manager.log';fence=raw.with_suffix('.finalized')
                if defect=='bytes':raw.write_text('changed')
                if defect=='nonzero':fence.write_text('9\n');scope['raw_evidence'][str(fence.relative_to(base))]=ref(fence)['sha256']
                if defect=='missing':fence.unlink()
                if defect=='marker':raw.write_text(raw.read_text()+'LONGHORN_FIRST_FAILURE rc=1\n');scope['raw_evidence'][str(raw.relative_to(base))]=ref(raw)['sha256']
                if defect=='duplicate-exit':raw.write_text(raw.read_text()+'LONGHORN_V2_FAILURE_STAGE_EXIT rc=0\n');scope['raw_evidence'][str(raw.relative_to(base))]=ref(raw)['sha256']
                if defect=='wrong-sha':scope['raw_evidence'][str(raw.relative_to(base))]='0'*64
                self.refresh(p,scope);row['failure_scopes']['manager']=ref(p);self.refresh(path,row)
                with self.assertRaises((ValueError,FileNotFoundError)):self.read(base,path)

    def test_only_exact_reviewed_gate_sha_transitions_are_admitted(self):
        for defect in ('unreviewed','wrong-old','wrong-new','product','false-change','contract','helper'):
            with self.subTest(defect=defect),tempfile.TemporaryDirectory() as d:
                base,row,path,ref,_=self.fixture(d); p=base/row['compatibility']['path'];compat=json.loads(p.read_text());name=next(iter(compat['changes']))
                if defect=='unreviewed':compat['changes']={}
                if defect=='wrong-old':compat['changes'][name]['old_sha256']='0'*64
                if defect=='wrong-new':compat['changes'][name]['sha256']='0'*64
                if defect=='product':compat['changes'][NEW.INPUTS[0]]={'old_sha256':'a','sha256':'b'}
                if defect=='false-change':compat['product_behavior_changed']=True
                if defect=='contract':
                    q=base/compat['contract_receipts']['finalized']['path'];q.write_text('1\n');compat['contract_receipts']['finalized']=ref(q)
                if defect=='helper':compat['helper_sha256']='0'*64
                self.refresh(p,compat);row['compatibility']=ref(p);self.refresh(path,row)
                with self.assertRaises(ValueError):self.read(base,path)

    def test_historical_adoption_raw_provenance_and_projection_are_not_rewritten(self):
        for defect in ('receipt','config','disk','identity','projection','raw','durable','markers'):
            with self.subTest(defect=defect),tempfile.TemporaryDirectory() as d:
                base,row,path,ref,_=self.fixture(d)
                if defect=='receipt':row['baseline']['native_block_receipt_sha256']='0'*64
                if defect=='config':row['baseline']['config_sha256']='0'*64
                if defect=='disk':next(iter(next(iter(row['baseline']['native_block_receipt']['nodes'].values()))['disks'].values()))['disk_uuid']='foreign'
                if defect=='identity':row['baseline']['baseline_identity']['namespace_uid']='foreign'
                if defect=='projection':row['adoption_projection']['snapshot_identity']={'foreign':'x'}
                if defect in ('raw','durable','markers'):
                    q=base/M.ADOPTION;prior=json.loads(q.read_text());r=base/'logs/adoption/v1.log'
                    if defect=='raw':r.write_text('changed')
                    if defect=='durable':r=r.with_suffix('.finalized');r.write_text('1\n');prior['raw_evidence'][str(r.relative_to(base))]=ref(r)['sha256']
                    if defect=='markers':r.write_text(r.read_text()+'LONGHORN_V2_IM_REVISION_2_ADOPT_QUALIFICATION_PASS\n');prior['raw_evidence'][str(r.relative_to(base))]=ref(r)['sha256']
                    self.refresh(q,prior);row['previous_evidence']=ref(q)
                self.refresh(path,row)
                with self.assertRaises(ValueError):self.read(base,path)

    def test_private_material_rejects_unowned_permissions_symlinks_empty_setting_or_escape(self):
        for defect in ('file-mode','directory-mode','symlink','empty','escape'):
            with self.subTest(defect=defect),tempfile.TemporaryDirectory() as d:
                base,_,path,_,_=self.fixture(d);setting=str(path)
                if defect=='file-mode':path.chmod(0o644)
                if defect=='directory-mode':path.parent.chmod(0o755)
                if defect=='symlink':target=path.with_suffix('.real');path.rename(target);path.symlink_to(target)
                if defect=='empty':setting=''
                if defect=='escape':setting='/etc/passwd'
                with patch.object(M,'BASE',base),patch.dict(os.environ,{M.ENV:setting}),self.assertRaises((ValueError,IsADirectoryError)):
                    M.material()

    def test_runtime_digest_must_match_exact_dual_published_manifest(self):
        with tempfile.TemporaryDirectory() as d:
            base,row,path,_,_=self.fixture(d);next(iter(row['baseline']['managers'].values()))['image_id']='containerd://sha256:'+'f'*64
            self.refresh(path,row)
            with self.assertRaises(ValueError):self.read(base,path)

    def test_request_and_transfer_are_bounded_readonly_and_include_all_original_scope_receipts(self):
        with tempfile.TemporaryDirectory() as d:
            base,row,path,ref,_=self.fixture(d);(base/'logs/longhorn-cases').mkdir()
            scopes=[r['path'] for r in row['failure_scopes'].values()]
            with patch.object(M,'BASE',base),patch.object(M,'load',return_value=SimpleNamespace(verify_log=Mock())),redirect_stdout(io.StringIO()):
                request=M.request(row['compatibility']['path'],scopes)
                selected=M.transfer(request,request.with_name('transfer.list'))
            self.assertEqual(0o600,request.stat().st_mode&0o777)
            self.assertEqual(0o700,request.parent.stat().st_mode&0o777)
            lines=request.with_name('transfer.list').read_text().splitlines()
            self.assertEqual(sorted(selected),lines)
            for scope in M.SCOPES:
                self.assertIn('logs/failures/'+scope+'.log',selected)
                self.assertIn('logs/failures/'+scope+'.exit',selected)
                self.assertIn('logs/failures/'+scope+'.finalized',selected)
            self.assertNotIn('baseline',json.loads(request.read_text()))
            self.assertFalse(request.with_name('runtime.json').exists())

    def test_create_takes_one_actual_capture_under_existing_lease_and_never_overwrites(self):
        with tempfile.TemporaryDirectory() as d:
            base,row,path,_,_=self.fixture(d);(base/'logs/longhorn-cases').mkdir()
            lab=SimpleNamespace(DIRECTORY=base/'clusters/longhorn-v2-gate',require_lease=Mock())
            capture=Mock(return_value=row['baseline']);projection=Mock(return_value=row['adoption_projection'])
            def load(name):
                if name=='longhorn-v2-install.py':return SimpleNamespace(fixture=lambda:(None,lab))
                if name=='longhorn-v2-upgrade-baseline.py':return SimpleNamespace(capture=capture)
                if name=='longhorn-v2-im-adopt.py':return SimpleNamespace(baseline=projection)
                return SimpleNamespace(verify_log=Mock())
            with patch.object(M,'BASE',base),patch.object(M,'load',side_effect=load),redirect_stdout(io.StringIO()):
                request=M.request(row['compatibility']['path'],[r['path'] for r in row['failure_scopes'].values()])
                runtime=M.create(request)
                frozen=runtime.read_bytes()
                with self.assertRaises(ValueError):M.create(request)
            self.assertEqual(frozen,runtime.read_bytes());lab.require_lease.assert_called_once()
            # One capture constructs the bound snapshot; one independent preflight rechecks it.
            self.assertEqual(2,capture.call_count)
            self.assertEqual(0o600,runtime.stat().st_mode&0o777)
            material=json.loads(frozen);self.assertEqual('focused-runtime-continuation',material['scope'])
            self.assertNotIn('status',material);self.assertFalse(material['fresh_installation'])

    def test_existing_runtime_reuse_requires_exact_retiring_preparation_and_never_recaptures(self):
        for defect in ('none','old-ready','stage','runtime','baseline','request'):
            with self.subTest(defect=defect),tempfile.TemporaryDirectory() as d:
                base,row,_,ref,write=self.fixture(d);(base/'logs/longhorn-cases').mkdir()
                lab=SimpleNamespace(DIRECTORY=base/'clusters/longhorn-v2-gate',require_lease=Mock())
                capture=Mock(return_value=row['baseline']);projection=Mock(return_value=row['adoption_projection'])
                def load(name):
                    if name=='longhorn-v2-install.py':return SimpleNamespace(fixture=lambda:(None,lab))
                    if name=='longhorn-v2-upgrade-baseline.py':return SimpleNamespace(capture=capture)
                    if name=='longhorn-v2-im-adopt.py':return SimpleNamespace(baseline=projection)
                    return SimpleNamespace(verify_log=Mock())
                with patch.object(M,'BASE',base),patch.object(M,'load',side_effect=load),redirect_stdout(io.StringIO()):
                    request=M.request(row['compatibility']['path'],[r['path'] for r in row['failure_scopes'].values()])
                    runtime=M.create(request);original=runtime.read_bytes();capture.reset_mock();projection.reset_mock()
                    pending={'stage':'retiring-empty-new','previous_evidence':{'runtime_continuation':ref(runtime)},'baseline_before':row['baseline']}
                    if defect=='old-ready':pending['stage']='old-ready'
                    if defect=='stage':pending['stage']='installing-old'
                    if defect=='runtime':pending['previous_evidence']['runtime_continuation']['sha256']='foreign'
                    if defect=='baseline':pending['baseline_before']={}
                    write('clusters/longhorn-v2-gate/longhorn-v2-upgrade-preparation.json',pending)
                    if defect=='request':
                        runtime_row=json.loads(original);runtime_row['request']['sha256']='foreign';runtime.write_text(json.dumps(runtime_row))
                    if defect in ('none','old-ready'):self.assertEqual(runtime,M.create(request))
                    else:
                        with self.assertRaises(ValueError):M.create(request)
                    capture.assert_not_called();projection.assert_not_called()
                    if defect!='request':self.assertEqual(original,runtime.read_bytes())


REVISION=SimpleNamespace(NEW_TAG='v1.12.1-kubeauto.3',OLD_TAG='v1.12.1-kubeauto.2',
    REUSE_IMPACT='tests/evidence/longhorn-v2-raid-status-reuse-impact-20261022.json',
    LEGACY_SCOPES=('replica-im','manager','attached-spdk','remote-spdk','replica-network','worker-reboot'))

class Revision3ReaderBoundaryContracts(unittest.TestCase):
    def test_revision3_continuation_binds_finite_routing_edges_and_keeps_six_original_tags(self):
        spec=importlib.util.spec_from_file_location('runtime3',ROOT/'tests/helpers/longhorn-v2-runtime-continuation.py')
        runtime=importlib.util.module_from_spec(spec);spec.loader.exec_module(runtime)
        for defect in ('none','missing-impact','false-behavior','post-adoption-seam','current-input','historical-ref','single-ref','adoption-finalized','adoption-marker', 'routing', 'routing-product', 'routing-missing', 'routing-old', 'routing-new', 'routing-contract'):
            with self.subTest(defect=defect),tempfile.TemporaryDirectory() as directory:
                base=Path(directory);base.joinpath('tests/evidence').mkdir(parents=True)
                def write(name,row):
                    p=base/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_text(row if isinstance(row,str) else json.dumps(row))
                    return {'path':name,'sha256':hashlib.sha256(p.read_bytes()).hexdigest()}
                helper_ref=write(runtime.HELPER,'reader fixture\n');input_ref=write('product/input','frozen\n')
                pins={runtime.HELPER:helper_ref['sha256'],'product/input':input_ref['sha256']}
                if defect.startswith('routing'):
                    for name in runtime.ROUTING_SEAMS:
                        pins[name]=write(name,'original routing fixture '+name)['sha256']
                previous={'frozen_inputs':{'product/input':'old'}}
                old_ref=write('tests/evidence/previous.json',previous)
                log_ref=write('logs/adoption/gate.log','LONGHORN_V2_IM_REVISION_3_ADOPT_QUALIFICATION_SCOPED_CLEAN_PASS\nLONGHORN_V2_IM_REVISION_3_ADOPT_QUALIFICATION_PASS\n')
                exit_ref=write('logs/adoption/gate.exit','0\n');final_ref=write('logs/adoption/gate.finalized','0\n')
                prior={'status':'pass','rc':0,'failure_markers':0,'durable':{'exit':0,'finalized':0},
                    'signed_scope':'v2-im-revision-3-adopt-qualification','version':'1.12.1',
                    'instance_manager_version':REVISION.NEW_TAG,'remote_input_verification':{'mismatches':0},
                    'baseline_identity':{'cluster_uid':'cluster'},'frozen_inputs':copy.deepcopy(pins),
                    'raw_evidence':{ref['path']:ref['sha256'] for ref in (log_ref,exit_ref,final_ref)},
                    'receipt_refs':{'log':log_ref,'exit':exit_ref,'finalized':final_ref},
                    'independent_readback':{'installation_healthy':True,'empty_business':True,'adoption_journal_absent':True,
                        'baseline':{'baseline_identity':{'cluster_uid':'cluster'}}}}
                if defect=='adoption-finalized':
                    final_ref=write('logs/adoption/gate.finalized','1\n');prior['receipt_refs']['finalized']=final_ref;prior['raw_evidence'][final_ref['path']]=final_ref['sha256']
                if defect=='adoption-marker':
                    log_ref=write('logs/adoption/gate.log','missing qualification clean marker\n');prior['receipt_refs']['log']=log_ref;prior['raw_evidence'][log_ref['path']]=log_ref['sha256']
                prior_ref=write(runtime.ADOPTION3,prior)
                scopes={scope:write('tests/evidence/'+scope+'.json',{}) for scope in runtime.SCOPES}
                impact={'adoption_evidence':prior_ref,'previous_evidence':old_ref,'single_disk':scopes['single-disk'],
                    'legacy_failure_scopes':{key:scopes[key] for key in REVISION.LEGACY_SCOPES}}
                impact_ref=write(REVISION.REUSE_IMPACT,impact)
                changes={}
                if defect.startswith('routing'):
                    for name in runtime.ROUTING_SEAMS:
                        current=write(name,'qualified current routing fixture '+name)['sha256']
                        changes[name]={'old_sha256':prior['frozen_inputs'][name], 'sha256':current}
                        pins[name]=current
                    if defect=='routing-missing': del changes[runtime.ROUTING_SEAMS[0]]
                    if defect=='routing-old': changes[runtime.ROUTING_SEAMS[0]]['old_sha256']='foreign'
                    if defect=='routing-new': changes[runtime.ROUTING_SEAMS[0]]['sha256']='foreign'
                contract_inputs={runtime.HELPER:pins[runtime.HELPER], **{name:change['sha256'] for name,change in changes.items()}}
                if defect=='routing-contract': contract_inputs={runtime.HELPER:pins[runtime.HELPER]}
                log=write('logs/contracts/check.log','LONGHORN_V2_RUNTIME_CONTINUATION_CONTRACT_PASS\n'+
                    'LONGHORN_V2_RUNTIME_CONTINUATION_CONTRACT_INPUTS '+json.dumps(contract_inputs)+'\n')
                compatibility={'scope':'focused-runtime-continuation-revision3','product_behavior_changed':True,'changes':changes,
                    'helper_sha256':pins[runtime.HELPER],'reuse_impact':impact_ref,
                    'contract_receipts':{'log':log,'exit':write('logs/contracts/check.exit','0\n'),
                                        'finalized':write('logs/contracts/check.finalized','0\n')}}
                if defect=='false-behavior':compatibility['product_behavior_changed']=False
                if defect=='post-adoption-seam':compatibility['changes']={'product/input':{'old_sha256':'old','sha256':'new'}}
                compat_ref=write('tests/evidence/compatibility.json',compatibility)
                row={'previous_evidence':prior_ref,'compatibility':compat_ref,'frozen_inputs':pins,'failure_scopes':copy.deepcopy(scopes)}
                if defect in ('current-input','routing-product'):write('product/input','drift\n')
                if defect=='historical-ref':row['failure_scopes']['manager']=write('tests/evidence/other-manager.json',{})
                if defect=='single-ref':row['failure_scopes']['single-disk']=write('tests/evidence/other-disk.json',{})
                revision=SimpleNamespace(read_reuse_impact=Mock(return_value=(impact,{})),profile=lambda:SimpleNamespace(verify_log=Mock()),
                    gate_input_current=lambda name, sha: runtime.digest(base/name) == sha)
                if defect=='missing-impact':revision.read_reuse_impact.side_effect=FileNotFoundError('pending actual impact')
                calls=[]
                def verified(ref,scope,*,tag):
                    calls.append((scope,tag));return {'frozen_inputs':previous['frozen_inputs'] if scope in REVISION.LEGACY_SCOPES else prior['frozen_inputs']}
                with patch.object(runtime,'BASE',base),patch.object(runtime,'load',return_value=revision),patch.object(runtime,'verify_scope',side_effect=verified):
                    if defect in ('none','routing'):
                        self.assertEqual(prior,runtime.read_inputs(row));self.assertEqual(9,len(calls))
                        self.assertEqual(6,sum(tag==REVISION.OLD_TAG for _,tag in calls))
                        self.assertEqual(3,sum(tag==REVISION.NEW_TAG for _,tag in calls))
                    else:
                        with self.assertRaises((ValueError,FileNotFoundError)):runtime.read_inputs(row)

    def test_revision3_fullcopy_adds_finite_impact_without_replaying_two_historical_scopes(self):
        spec=importlib.util.spec_from_file_location('clone3_contract',ROOT/'tests/unit/test_longhorn_v2_clone.py')
        old=importlib.util.module_from_spec(spec);spec.loader.exec_module(old)
        for defect in ('none','missing-impact','wrong-old-fullcopy'):
            with self.subTest(defect=defect):
                case=old.FullCopyReuseContracts();case.setUp()
                try:
                    case.write('roles/longhorn/files/images.json',json.dumps({'1.12.1':{'longhorn-instance-manager':REVISION.NEW_TAG}}))
                    old_ref=old.gate.reference(case.base,case.base/old.gate.IMPACT)
                    impact={'full_copy_impact':dict(old_ref)}
                    if defect=='wrong-old-fullcopy':impact['full_copy_impact']['sha256']='foreign'
                    path=case.write(REVISION.REUSE_IMPACT,'finite reader boundary fixture\n')
                    refs={REVISION.REUSE_IMPACT:old.gate.digest(path)}
                    revision=SimpleNamespace(REUSE_IMPACT=REVISION.REUSE_IMPACT,read_reuse_impact=Mock(return_value=(impact,refs)))
                    if defect=='missing-impact':revision.read_reuse_impact.side_effect=FileNotFoundError('pending actual impact')
                    with patch.object(old.gate,'load',return_value=revision),patch.object(subprocess,'run',side_effect=AssertionError('no live or replay')):
                        if defect=='none':
                            result=old.gate.preflight_full_copy(case.base)
                            self.assertEqual(2,len(result['full_copy']));self.assertEqual(old_ref,result['reuse_impact'])
                            self.assertIn(REVISION.REUSE_IMPACT,result['transport_refs']);revision.read_reuse_impact.assert_called_once_with()
                        else:
                            with self.assertRaises((ValueError,FileNotFoundError)):old.gate.preflight_full_copy(case.base)
                finally:case.doCleanups()

if __name__=='__main__':unittest.main(verbosity=2)
