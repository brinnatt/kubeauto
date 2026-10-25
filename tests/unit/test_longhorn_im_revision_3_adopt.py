"""Revision3 admission contracts; publication rows below are temporary fixtures."""
import copy
from contextlib import redirect_stdout
import hashlib
import importlib.util
import io
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location('revision3', ROOT/'tests/helpers/longhorn-im-revision-3-adopt.py')
REVISION = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(REVISION)
PUBLICATION = 'tests/evidence/longhorn-v2-raid-status-publication-20261022.json'


class Revision3Contracts(unittest.TestCase):
    def test_historical_full_reader_allows_only_exact_retry_classification(self):
        name='tests/helpers/longhorn-full-evidence.py'
        source=(ROOT/name).read_text()
        expected='907ce9c5697e37df7a487667ee76f341ebb81a3fde253b99cf1a4e6b342f9e43'
        with tempfile.TemporaryDirectory() as directory, patch.object(REVISION,'BASE',Path(directory)):
            path=Path(directory)/name;path.parent.mkdir(parents=True);path.write_text(source)
            self.assertTrue(REVISION.gate_input_current(name,expected))
            self.assertFalse(REVISION.gate_input_current(name,'0'*64))
            for changed in (source+'\n# unrelated change\n',
                            source.replace("refs,lines=execution(mode,prefix)","refs,lines=({},[])",1),
                            source.replace("!=1:","!=0:",1),
                            source.replace('ERROR[:!]','IGNORED_ERROR',1)):
                self.assertNotEqual(changed,source)
                path.write_text(changed)
                self.assertFalse(REVISION.gate_input_current(name,expected))

    def test_nullable_disk_status_reuse_requires_exact_complete_original_product(self):
        source = (ROOT/REVISION.SOURCE).read_text()
        original = source
        for variable, key in (('status', 'name'), ('state', 'disk_name')):
            original = original.replace(variable+" = (current.get('status', {}).get('diskStatus') or {}).get("+key+") or {}",
                variable+" = current.get('status', {}).get('diskStatus', {}).get("+key+", {})")
        self.assertNotEqual(source, original)
        historical = hashlib.sha256(original.encode()).hexdigest()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); path = root/REVISION.SOURCE
            path.parent.mkdir(parents=True); path.write_text(source)
            with patch.object(REVISION, 'BASE', root):
                self.assertTrue(REVISION.gate_input_current(REVISION.SOURCE, historical))
                self.assertFalse(REVISION.gate_input_current(REVISION.SOURCE, '0'*64))
                path.write_text(source+'\n# unrelated change\n')
                self.assertFalse(REVISION.gate_input_current(REVISION.SOURCE, historical))

    def fixture(self, base, engine='v2', source=None):
        def write(name, value):
            path = base/name; path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(value) if isinstance(value, dict) else value)
            return hashlib.sha256(path.read_bytes()).hexdigest()
        names = set(REVISION.CORE_INPUTS) | {'playbooks/09.longhorn.yml'}
        for name in names:
            write(name, source if name == REVISION.SOURCE and source is not None else
                  (ROOT/name).read_text() if name in (REVISION.ADOPTER, REVISION.DRIVER) else 'frozen\n')
        write(REVISION.CATALOG, {'1.12.1': {'longhorn-instance-manager': REVISION.NEW_TAG}})
        before = {'baseline_identity': {'cluster_uid': 'cluster', 'namespace_uid': 'namespace'},
            'native_block_receipt_sha256': 'disk-receipt', 'config_sha256': 'config'}
        old = {'im': {'uid': 'old-im', 'node': 'longhorn-node-149' if engine == 'v2-dr-target' else 'longhorn-node-146',
            'image': 'registry/longhorn-instance-manager:'+REVISION.OLD_TAG}}
        prior = {'status': 'pass', 'rc': 0, 'failure_markers': 0, 'version': '1.12.1',
            'durable': {'exit': 0, 'finalized': 0}, 'signed_scope': engine+'-im-revision-2-adopt-qualification',
            'instance_manager_version': REVISION.OLD_TAG, 'baseline_identity': before['baseline_identity'],
            'remote_input_verification': {'mismatches': 0},
            'independent_readback': {'installation_healthy': True, 'empty_business': True, 'adoption_journal_absent': True,
                'baseline': before, 'managers': old},
            'frozen_inputs': {name: REVISION.digest(base/name) for name in names if name != REVISION.DRIVER}}
        previous = 'tests/evidence/longhorn-'+engine+'-im-revision-2-adopt-qualification-20261021.json'
        publication = {'status': 'pass', 'durable_rc': 0, 'failure_markers': 0, 'selected_image_count': 1,
            'image': 'brinnatt/longhorn-instance-manager:'+REVISION.NEW_TAG,
            'publication_record': {'key': 'brinnatt/longhorn-instance-manager:'+REVISION.NEW_TAG,
                'schema': 1, 'digest': 'sha256:'+'a'*64, 'platforms': {'linux/amd64': 'sha256:'+'b'*64, 'linux/arm64': 'sha256:'+'c'*64}}}
        plan = {'schema': 1, 'status': 'ready', 'old_tag': REVISION.OLD_TAG, 'new_tag': REVISION.NEW_TAG,
            'publication_evidence': {'path': PUBLICATION, 'sha256': write(PUBLICATION, publication)},
            'profiles': {engine: {'previous_evidence': {'path': previous, 'sha256': write(previous, prior)},
                'inputs': {name: {'old_sha256': prior['frozen_inputs'].get(name), 'sha256': REVISION.digest(base/name),
                    'source': 'isolated admission fixture', 'impact': 'exact old/new boundary'} for name in names}}}}
        write(REVISION.PLAN, plan)
        return plan, prior, before, old, write

    def test_retirement_repair_binds_exact_current_gates_original_sources_and_durable_contract(self):
        for defect in ('none', 'product', 'missing', 'current', 'origin', 'exit', 'marker', 'inputs'):
            with self.subTest(defect=defect), tempfile.TemporaryDirectory() as directory:
                base=Path(directory)
                def write(name, value):
                    path=base/name;path.parent.mkdir(parents=True,exist_ok=True)
                    path.write_text(value if isinstance(value,str) else json.dumps(value))
                    return {'path': name, 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
                changes={}
                for name in REVISION.RETIREMENT_GATES:
                    old=write('logs/original/'+Path(name).name,'original '+name)
                    current=write(name,'reviewed '+name)
                    changes[name]={'sha256':current['sha256'],'origins':{old['sha256']:old}}
                inputs={name:edge['sha256'] for name,edge in changes.items()}
                marker='LONGHORN_V2_RETIREMENT_REPAIR_CONTRACT_PASS\n'
                log=marker+'LONGHORN_V2_RETIREMENT_REPAIR_CONTRACT_INPUTS '+json.dumps(inputs)+'\n'
                repair={'scope':'v2-upgrade-retirement-gc-repair','product_behavior_changed':False,'changes':changes,
                    'contract_receipts':{key:write('logs/contract.'+key,log if key=='log' else '0\n') for key in ('log','exit','finalized')}}
                name='tests/helpers/longhorn-cleanup.py';old=next(iter(changes[name]['origins']))
                self.assertIn(name, REVISION.RETIREMENT_GATES)
                if defect=='product':repair['product_behavior_changed']=True
                if defect=='missing':del changes[REVISION.DRIVER]
                if defect=='current':write(name,'unreviewed')
                if defect=='origin':write(next(iter(changes[name]['origins'].values()))['path'],'tampered')
                if defect=='exit':repair['contract_receipts']['exit']=write('logs/contract.exit','1\n')
                if defect=='marker':repair['contract_receipts']['log']=write('logs/contract.log','missing marker\n')
                if defect=='inputs':repair['contract_receipts']['log']=write('logs/contract.log',marker+'LONGHORN_V2_RETIREMENT_REPAIR_CONTRACT_INPUTS {}\n')
                write(REVISION.RETIREMENT_REPAIR,repair)
                with patch.object(REVISION,'BASE',base):
                    if defect=='none':
                        # Same-subnet node handoffs changed these three outer
                        # shells; immutable historical adoption remains reusable
                        # only through each exact original/current input edge.
                        for candidate in (name, 'tests/helpers/longhorn-full.sh',
                                          'tests/helpers/longhorn-live.sh',
                                          'tests/helpers/longhorn-static.sh'):
                            origin=next(iter(changes[candidate]['origins']))
                            self.assertTrue(REVISION.gate_input_current(candidate,origin))
                            self.assertFalse(REVISION.gate_input_current(candidate,'unrecognized'))
                            frozen=(base/candidate).read_text()
                            write(candidate,frozen+'unreviewed change')
                            with self.assertRaises(ValueError):
                                REVISION.gate_input_current(candidate,origin)
                            write(candidate,frozen)
                        write(REVISION.SOURCE,'product')
                        self.assertFalse(REVISION.gate_input_current(REVISION.SOURCE,'old'))
                    else:
                        with self.assertRaises(ValueError):REVISION.gate_input_current(name,old)

    def test_historical_installation_accepts_only_exact_retirement_rpc_wire_correction(self):
        with tempfile.TemporaryDirectory() as directory:
            base=Path(directory);name='roles/longhorn/files/block-retire.py'
            path=base/name;path.parent.mkdir(parents=True)
            current=(ROOT/name).read_text()
            wire="{'jsonrpc':'2.0','id':index,'method':method}"
            old="{'jsonrpc':'2.0','id':index,'method':method,'params':{}}"
            original=current.replace(wire,old,1);sha=hashlib.sha256(original.encode()).hexdigest()
            with patch.object(REVISION,'BASE',base):
                path.write_text(current);self.assertTrue(REVISION.gate_input_current(name,sha))
                self.assertFalse(REVISION.gate_input_current(name,'f'*64))
                for changed in (current+'\n# unrelated change\n',current.replace("'bdev_lvol_get_lvstores'","'different_method'"),current+wire):
                    path.write_text(changed);self.assertFalse(REVISION.gate_input_current(name,sha))
                path.write_text(original);self.assertTrue(REVISION.gate_input_current(name,sha))

    def test_exact_new_edge_keeps_original_revision2_receipt_read_only(self):
        for engine in REVISION.ENGINES:
            with self.subTest(engine=engine), tempfile.TemporaryDirectory() as directory:
                base = Path(directory); plan, prior, _, _, _ = self.fixture(base, engine)
                old_path = base/plan['profiles'][engine]['previous_evidence']['path']; original = old_path.read_bytes()
                with patch.object(REVISION, 'BASE', base):
                    self.assertEqual((plan, plan['profiles'][engine], prior), REVISION.read_plan(engine)[:3])
                self.assertEqual(original, old_path.read_bytes())

    def test_missing_future_publication_and_unreviewed_edges_fail_closed(self):
        for defect in ('pending', 'missing-publication', 'old-publication', 'publication-failed', 'arm64',
                       'previous-scope', 'previous-tag', 'previous-durable', 'previous-sha',
                       'missing-old-driver', 'missing-new-wrapper', 'old-sha', 'changed-input', 'new-tag',
                       'digest', 'platform-digest', 'old-pin'):
            with self.subTest(defect=defect), tempfile.TemporaryDirectory() as directory:
                base = Path(directory); plan, prior, _, _, write = self.fixture(base)
                selected = plan['profiles']['v2']
                if defect == 'pending': plan['status'] = 'pending-publication-and-input-review'
                if defect == 'missing-publication': (base/PUBLICATION).unlink()
                if defect == 'old-publication': plan['publication_evidence']['path'] = 'tests/evidence/old.json'
                if defect in ('publication-failed', 'arm64', 'digest', 'platform-digest', 'old-pin'):
                    publication = json.loads((base/PUBLICATION).read_text())
                    if defect == 'publication-failed': publication['durable_rc'] = 1
                    if defect == 'arm64': del publication['publication_record']['platforms']['linux/arm64']
                    if defect == 'digest': publication['publication_record']['digest'] = 'sha256:bad'
                    if defect == 'platform-digest': publication['publication_record']['platforms']['linux/amd64'] = 'foreign'
                    if defect == 'old-pin': publication['publication_record']['key'] = 'brinnatt/longhorn-instance-manager:'+REVISION.OLD_TAG
                    plan['publication_evidence']['sha256'] = write(PUBLICATION, publication)
                if defect.startswith('previous-'):
                    if defect == 'previous-scope': prior['signed_scope'] = 'v2-im-adopt-qualification'
                    if defect == 'previous-tag': prior['instance_manager_version'] = 'v1.12.1-kubeauto.1'
                    if defect == 'previous-durable': prior['durable']['finalized'] = 1
                    selected['previous_evidence']['sha256'] = write(selected['previous_evidence']['path'], prior)
                    if defect == 'previous-sha': selected['previous_evidence']['sha256'] = 'foreign'
                if defect == 'missing-old-driver': del selected['inputs']['tests/helpers/longhorn-im-revision-adopt.py']
                if defect == 'missing-new-wrapper': del selected['inputs']['tests/helpers/longhorn-im-revision-3-adopt.sh']
                if defect == 'old-sha': selected['inputs'][REVISION.SOURCE]['old_sha256'] = 'foreign'
                if defect == 'changed-input': (base/REVISION.SOURCE).write_text('drift')
                if defect == 'new-tag': plan['new_tag'] = 'latest'
                write(REVISION.PLAN, plan)
                with patch.object(REVISION, 'BASE', base), self.assertRaises((ValueError, FileNotFoundError)):
                    REVISION.read_plan('v2')

    def test_historical_adoption_keeps_three_routing_inputs_separate_from_new_admission(self):
        for changed in REVISION.REUSE_GATE_INPUTS:
            with self.subTest(changed=changed), tempfile.TemporaryDirectory() as directory:
                base = Path(directory); plan, prior, _, _, _ = self.fixture(base)
                original_plan = (base/REVISION.PLAN).read_bytes()
                (base/changed).write_text('current routing fixture\n')
                with patch.object(REVISION, 'BASE', base):
                    with self.assertRaises(ValueError): REVISION.read_plan('v2')
                    self.assertEqual(prior, REVISION.read_plan('v2', historical=True)[2])
                    (base/REVISION.SOURCE).write_text('unqualified product drift\n')
                    with self.assertRaises(ValueError): REVISION.read_plan('v2', historical=True)
                self.assertEqual(original_plan, (base/REVISION.PLAN).read_bytes())
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory); self.fixture(base)
            (base/'tests/helpers/longhorn-live.sh').write_text('unknown owning gate change\n')
            with patch.object(REVISION, 'BASE', base), self.assertRaises(ValueError):
                REVISION.read_plan('v2', historical=True)

    def test_pending_candidate_never_grants_admission(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            plan, _, _, _, write = self.fixture(base)
            plan['status'] = 'pending-publication-and-input-review'
            write(REVISION.PLAN, plan)
            with patch.object(REVISION, 'BASE', base), self.assertRaisesRegex(ValueError, 'reviewed exact'):
                REVISION.read_plan('v2')

    def test_historical_real_adoption_log_keeps_frozen_plan_and_product_guards_after_routing_change(self):
        for defect in ('routing', 'product', 'frozen-result', 'missing-business'):
            with self.subTest(defect=defect), tempfile.TemporaryDirectory() as directory:
                base=Path(directory);plan,prior,before,old,write=self.fixture(base)
                selected=plan['profiles']['v2'];nodes=['longhorn-node-'+str(n) for n in (146,147,148)]
                before={'required_manager_nodes':nodes}
                old={str(i):{'uid':'old-'+node,'node':node,'image':'registry/im:'+REVISION.OLD_TAG} for i,node in enumerate(nodes)}
                new={str(i):{'uid':'new-'+node,'node':node,'image':'registry/im:'+REVISION.NEW_TAG} for i,node in enumerate(nodes)}
                plan_sha=REVISION.digest(base/REVISION.PLAN)
                result={'phase':'verified','engine':'v2','before':before,'old_managers':old,'new_managers':new,
                    'plan_sha256':plan_sha,'frozen_inputs':{name:row['sha256'] for name,row in selected['inputs'].items()},
                    'transition':{'plan':{'path':REVISION.PLAN,'sha256':plan_sha},
                        'previous_evidence':selected['previous_evidence'],'publication_evidence':plan['publication_evidence'],
                        'old_tag':REVISION.OLD_TAG,'new_tag':REVISION.NEW_TAG}}
                with patch.object(REVISION,'BASE',base):
                    gate=REVISION.profile();prefix=gate.prefix('v2')
                    def log():
                        lines=[prefix+'_PRECONDITIONS_VERIFIED '+json.dumps(before),prefix+'_RUNTIME_VERIFIED '+json.dumps(new),
                               prefix+'_RESULT '+json.dumps(result),prefix+'_BUSINESS_PASS','LONGHORN_INSTALL_BUSINESS_PASS',
                               'LONGHORN_BUSINESS_FIXTURES_CLEAN_PASS',prefix+'_FIXTURES_CLEAN_PASS',
                               'LONGHORN_FSYNC_REMOUNT_VERIFIED bytes=9437184 sha256='+'a'*64+' replica_nodes='+','.join(nodes)]
                        if defect=='missing-business':lines.remove('LONGHORN_INSTALL_BUSINESS_PASS')
                        return '\n'.join(lines)+'\n'
                    if defect!='missing-business':gate.verify_log(log())
                    (base/REVISION.DRIVER).write_text('reviewed routing change\n')
                    with self.assertRaises(ValueError):gate.verify_log(log())
                    with self.assertRaises(ValueError):REVISION.read_plan('v2')
                    if defect=='product':(base/REVISION.SOURCE).write_text('unreviewed product change\n')
                    if defect=='frozen-result':result['frozen_inputs'][REVISION.DRIVER]='changed-receipt'
                    if defect=='routing':gate.verify_log(log(),historical=True)
                    else:
                        with self.assertRaises(ValueError):gate.verify_log(log(),historical=True)

    def test_same_product_setup_and_journal_path_uses_only_revision3_markers(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory); plan, prior, before, old, _ = self.fixture(base)
            cluster = base/'cluster'; cluster.mkdir(); (cluster/'config.yml').write_text(
                'longhorn_version: 1.12.1\nlonghorn_image_prefix: registry\nlonghorn_data_engines: [v2]\n')
            calls = []; journals = []
            def write(path, row):
                calls.append('journal'); journals.append(copy.deepcopy(row)); path.write_text(json.dumps(row))
            def setup(pid, requests):
                calls.append('setup09'); self.assertEqual((7, []), (pid, requests))
                self.assertEqual('setup-intent', journals[-1]['phase'])
                self.assertEqual(plan['profiles']['v2']['previous_evidence'], journals[-1]['transition']['previous_evidence'])
            lab = SimpleNamespace(DIRECTORY=cluster, owner=lambda: {'run_id': 'run'}, write_private=write,
                run_requests=setup, wait=lambda name, timeout, fn: fn(), business=Mock())
            install = SimpleNamespace(health=Mock(), business=Mock())
            new = {'new': dict(old['im'], uid='new-im', image='registry/longhorn-instance-manager:'+REVISION.NEW_TAG)}
            with patch.object(REVISION, 'BASE', base):
                gate = REVISION.profile()
                self.assertEqual('longhorn-v2-im-revision-3-adopt.json', gate.RECORD)
                self.assertEqual('LONGHORN_V2_IM_REVISION_3_ADOPT', gate.prefix('v2'))
                with patch.object(gate, 'baseline', return_value=before), patch.object(gate, 'old_health'), \
                        patch.object(gate, 'managers', side_effect=[old, new, new]), redirect_stdout(io.StringIO()):
                    gate.adopt(install, lab, 7, 'v2')
            self.assertEqual(['journal', 'setup09'], calls[:2])
            self.assertEqual(REVISION.OLD_TAG, journals[0]['transition']['old_tag'])
            self.assertEqual(REVISION.NEW_TAG, journals[0]['transition']['new_tag'])
            lab.business.assert_called_once_with('install', relay_pid=7)
            self.assertEqual(prior, json.loads((base/plan['profiles']['v2']['previous_evidence']['path']).read_text()))


    def test_fixed_shell_routes_three_profiles_and_preserves_failed_durable_state(self):
        source = (ROOT/'tests/helpers/longhorn-im-revision-3-adopt.sh').read_text()
        helper = '''import os,signal,sys,time
from pathlib import Path
mode=sys.argv[1]
if mode=='relay-maintain':
 signal.signal(signal.SIGTERM,lambda *_:sys.exit(0))
 while True:time.sleep(.01)
with open('calls.txt','a') as f:f.write(Path(__file__).name+':'+mode+'\\n')
if mode=='business' and os.environ['FAIL_MODE']=='business':sys.exit(17)
if mode=='readback':
 engine=sys.argv[sys.argv.index('--engine')+1]
 print('LONGHORN_'+engine.upper().replace('-','_')+'_IM_REVISION_3_ADOPT_INDEPENDENT_READBACK {}')
 Path(sys.argv[sys.argv.index('--output')+1]).write_text('{}')
'''
        for engine, failure in [('v1','none'),('v2','none'),('v2-dr-target','none'),('v2','business')]:
            with self.subTest(engine=engine, failure=failure), tempfile.TemporaryDirectory() as directory:
                base=Path(directory); (base/'.venv/bin').mkdir(parents=True)
                (base/'.venv/bin/python').symlink_to(sys.executable)
                (base/'tests/helpers').mkdir(parents=True); (base/'kube-bin').mkdir()
                kubectl=base/'kube-bin/kubectl'; kubectl.write_text('#!/bin/sh\nexit 0\n'); kubectl.chmod(0o755)
                for name in ('longhorn-lab.py','longhorn-v2-core.py','longhorn-v2-install.py',
                             'longhorn-v2-dr-target.py','longhorn-im-revision-3-adopt.py'):
                    (base/'tests/helpers'/name).write_text(helper)
                script=source.replace('BASE=/usr/local/kubeauto','BASE='+str(base)).replace(
                    '/var/tmp/kubeauto-longhorn-v2-im-adopt.',str(base/'work.'))
                result=subprocess.run(['bash','-c',script,'contract',engine+'-im-revision-3-adopt-qualification'],
                    env=dict(os.environ,LONGHORN_PARENT_LEASE_HELD='yes',FAIL_MODE=failure),
                    capture_output=True,text=True,timeout=10)
                calls=(base/'calls.txt').read_text().splitlines()
                self.assertIn('longhorn-im-revision-3-adopt.py:cache',calls)
                self.assertIn('longhorn-im-revision-3-adopt.py:business',calls)
                self.assertFalse(any('longhorn-im-revision-adopt.py:' in c for c in calls))
                exits=list(base.glob('work.*/exit'));self.assertEqual(1,len(exits))
                self.assertEqual(result.returncode,int(exits[0].read_text()))
                marker='LONGHORN_'+engine.upper().replace('-','_')+'_IM_REVISION_3_ADOPT_QUALIFICATION_PASS'
                if failure=='none':
                    self.assertEqual(0,result.returncode,result.stderr);self.assertIn(marker,result.stdout)
                    self.assertIn('longhorn-im-revision-3-adopt.py:readback',calls)
                else:
                    self.assertEqual(17,result.returncode,result.stderr);self.assertNotIn(marker,result.stdout)
                    self.assertIn('longhorn-im-revision-3-adopt.py:clean',calls)


    def completed_fixture(self, base, engine='v2', source=None):
        plan, previous, before, old, write = self.fixture(base, engine, source=source)
        selected = plan['profiles'][engine]
        name = REVISION.SOURCE
        first_inputs = dict(previous['frozen_inputs']); first_inputs[name] = 'first-source-sha'
        first = copy.deepcopy(previous)
        first.update(signed_scope=engine+'-im-adopt-qualification', instance_manager_version='v1.12.1-kubeauto.1',
                     frozen_inputs=first_inputs,
                     input_compatibility={name: {'original_sha256': 'qualification-source-sha', 'sha256': first_inputs[name]}})
        first_path = 'tests/evidence/longhorn-'+engine+'-im-adopt-qualification-20261021.json'
        pub2 = {'status':'pass','durable_rc':0,'failure_markers':0,'selected_image_count':1,
                'image':'brinnatt/longhorn-instance-manager:'+REVISION.OLD_TAG}
        pub2_path = 'tests/evidence/longhorn-v2-linked-tls-rebuild-publication-20261021.json'
        old_plan = {'schema':1,'old_tag':'v1.12.1-kubeauto.1','new_tag':REVISION.OLD_TAG,
                    'publication_evidence':{'path':pub2_path,'sha256':write(pub2_path,pub2)},
                    'profiles':{engine:{'previous_evidence':{'path':first_path,'sha256':write(first_path,first)},
                        'inputs':{key:{'old_sha256':first_inputs.get(key),'sha256':value}
                                  for key,value in previous['frozen_inputs'].items()}}}}
        old_plan_path = 'tests/fixtures/longhorn-im-revision-2-plan.json'
        old_plan_sha = write(old_plan_path,old_plan)
        previous['plan_sha256'] = old_plan_sha
        previous['transition'] = {'plan':{'path':old_plan_path,'sha256':old_plan_sha},
            'previous_evidence':old_plan['profiles'][engine]['previous_evidence'],
            'publication_evidence':old_plan['publication_evidence'],
            'old_tag':old_plan['old_tag'],'new_tag':old_plan['new_tag']}
        selected['previous_evidence']['sha256'] = write(selected['previous_evidence']['path'],previous)
        plan_sha = write(REVISION.PLAN,plan)
        inputs = {key:row['sha256'] for key,row in selected['inputs'].items()}
        managers = {'im-new':dict(old['im'],uid='new-im',image='registry/longhorn-instance-manager:'+REVISION.NEW_TAG,
            pod_uid='pod-new',platform='linux/amd64',image_id='registry/longhorn-instance-manager@sha256:'+'b'*64)}
        accepted = {'status':'pass','rc':0,'failure_markers':0,'durable':{'exit':0,'finalized':0},
            'signed_scope':engine+'-im-revision-3-adopt-qualification','version':'1.12.1',
            'instance_manager_version':REVISION.NEW_TAG,'remote_input_verification':{'mismatches':0},
            'baseline_identity':before['baseline_identity'],'plan_sha256':plan_sha,'frozen_inputs':inputs,
            'transition':{'plan':{'path':REVISION.PLAN,'sha256':plan_sha},
                'previous_evidence':selected['previous_evidence'],'publication_evidence':plan['publication_evidence'],
                'old_tag':REVISION.OLD_TAG,'new_tag':REVISION.NEW_TAG},
            'independent_readback':{'installation_healthy':True,'empty_business':True,'adoption_journal_absent':True,
                'baseline':before,'managers':managers,'plan_sha256':plan_sha,'frozen_inputs':inputs}}
        accepted_path = 'tests/evidence/longhorn-'+engine+'-im-revision-3-adopt-qualification-20261022.json'
        write(accepted_path,accepted)
        evidence = {'baseline_identity':before['baseline_identity'],'frozen_inputs':{name:'qualification-source-sha'}}
        return evidence, name, inputs[name], accepted, accepted_path, plan, previous, write

    def test_volume_input_consumes_only_completed_exact_publication_and_history_chain(self):
        defects = ('none','missing-publication','missing-adoption','pending','wrong-digest','wrong-platform',
                   'wrong-old-pin','wrong-old-evidence','wrong-plan','wrong-new-frozen','wrong-readback-frozen',
                   'reused-manager','wrong-native-receipt','wrong-cluster','missing-pod-uid',
                   'first-proof-drift','old-plan-drift','foreign-new-image','symlink-adoption')
        for engine in ('v2','v2-dr-target'):
            for defect in defects:
                with self.subTest(engine=engine,defect=defect),tempfile.TemporaryDirectory() as directory:
                    base=Path(directory)
                    evidence,name,current,accepted,path,plan,previous,write=self.completed_fixture(base,engine)
                    managers=accepted['independent_readback']['managers']; manager=next(iter(managers.values()))
                    if defect=='pending':accepted['status']='pending'
                    if defect=='wrong-digest':manager['image_id']='registry/im@sha256:'+'d'*64
                    if defect=='wrong-platform':manager['platform']='linux/s390x'
                    if defect=='wrong-old-pin':accepted['transition']['old_tag']='v1.12.1-kubeauto.1'
                    if defect=='wrong-old-evidence':accepted['transition']['previous_evidence']['sha256']='foreign'
                    if defect=='wrong-plan':accepted['plan_sha256']='foreign'
                    if defect=='wrong-new-frozen':accepted['frozen_inputs']=dict(accepted['frozen_inputs'],**{name:'foreign'})
                    if defect=='wrong-readback-frozen':accepted['independent_readback']['frozen_inputs']={}
                    if defect=='reused-manager':manager['uid']='old-im'
                    if defect=='wrong-native-receipt':accepted['independent_readback']['baseline']=dict(
                        accepted['independent_readback']['baseline'],native_block_receipt_sha256='foreign')
                    if defect=='wrong-cluster':evidence['baseline_identity']={'cluster_uid':'foreign'}
                    if defect=='missing-pod-uid':del manager['pod_uid']
                    if defect=='foreign-new-image':manager['image']='registry/im:'+REVISION.OLD_TAG
                    write(path,accepted)
                    if defect=='missing-publication':(base/PUBLICATION).unlink()
                    if defect=='missing-adoption':(base/path).unlink()
                    if defect=='first-proof-drift':(base/previous['transition']['previous_evidence']['path']).write_text('{}')
                    if defect=='old-plan-drift':(base/previous['transition']['plan']['path']).write_text('{}')
                    if defect=='symlink-adoption':
                        target=base/path;target.rename(target.with_suffix('.other'));target.symlink_to(target.with_suffix('.other'))
                    with patch.object(REVISION,'BASE',base):
                        self.assertEqual(defect=='none',REVISION.accepted_input_current(evidence,name,current))

    def test_completed_adoption_nullable_source_reuse_remains_whole_file_and_caller_bound(self):
        source = (ROOT/REVISION.SOURCE).read_text()
        original = source
        for variable, key in (('status', 'name'), ('state', 'disk_name')):
            original = original.replace(variable+" = (current.get('status', {}).get('diskStatus') or {}).get("+key+") or {}",
                variable+" = current.get('status', {}).get('diskStatus', {}).get("+key+", {})")
        historical = hashlib.sha256(original.encode()).hexdigest()
        real_plan = json.loads((ROOT/REVISION.PLAN).read_text())
        self.assertEqual(historical, real_plan['profiles']['v2']['inputs'][REVISION.SOURCE]['sha256'])
        for engine in ('v2', 'v2-dr-target'):
            with self.subTest(engine=engine), tempfile.TemporaryDirectory() as directory:
                base = Path(directory)
                evidence, name, old, *_ = self.completed_fixture(base, engine, source=original)
                path = base/name; path.write_text(source)
                with patch.object(REVISION, 'BASE', base):
                    self.assertTrue(REVISION.accepted_input_current(evidence, name, REVISION.digest(path)))
                    self.assertFalse(REVISION.accepted_input_current(evidence, name, 'arbitrary-caller-sha'))
                    self.assertFalse(REVISION.accepted_input_current(evidence, name, old))
                    with self.assertRaisesRegex(ValueError, 'input changed'):
                        REVISION.read_plan(engine)
                    path.write_text(source+'\n# unrelated source drift\n')
                    self.assertFalse(REVISION.accepted_input_current(evidence, name, REVISION.digest(path)))

    def test_owning_install_requires_actual_revision3_adoption_even_when_source_hash_is_unchanged(self):
        spec=importlib.util.spec_from_file_location('install3',ROOT/'tests/helpers/longhorn-v2-install.py')
        install=importlib.util.module_from_spec(spec);spec.loader.exec_module(install)
        with tempfile.TemporaryDirectory() as directory:
            base=Path(directory);evidence,name,current,accepted,path,plan,previous,write=self.completed_fixture(base)
            with patch.object(install,'BASE',base),patch.object(install,'load',return_value=REVISION),patch.object(REVISION,'BASE',base):
                self.assertTrue(install.revision3_adopted_input_current(evidence,name,current))
                (base/path).unlink()
                self.assertFalse(install.revision3_adopted_input_current(evidence,name,current))
                write('tests/evidence/longhorn-v2-aio-install-qualification-20261020.json',dict(
                    evidence,status='pass',rc=0,failure_markers=0,frozen_inputs={name:current}))
                lab=SimpleNamespace(owner=Mock())
                with self.assertRaisesRegex(ValueError,'requires completed exact'):
                    install.volume_baseline(lab)
                lab.owner.assert_not_called()

    def test_runtime_manager_digest_is_read_from_running_pod_not_inferred_from_tag(self):
        for defect in ('none','digest','architecture','unready','missing-status','foreign-pod-node'):
            with self.subTest(defect=defect),tempfile.TemporaryDirectory() as directory:
                base=Path(directory);self.fixture(base)
                cluster=base/'cluster';input_path=cluster/'yml/longhorn/input.json';input_path.parent.mkdir(parents=True)
                input_path.write_text(json.dumps({'longhorn_v2_nodes':['192.168.47.146']}))
                image='registry/longhorn-instance-manager:'+REVISION.NEW_TAG
                pod={'metadata':{'name':'im','uid':'pod-im'},'spec':{'nodeName':'longhorn-node-146',
                    'containers':[{'name':'instance-manager','image':image}]},'status':{'containerStatuses':[
                        {'name':'instance-manager','ready':True,'imageID':'registry/im@sha256:'+'b'*64}]}}
                if defect=='digest':pod['status']['containerStatuses'][0]['imageID']='registry/im@sha256:'+'d'*64
                if defect=='unready':pod['status']['containerStatuses'][0]['ready']=False
                if defect=='missing-status':pod['status']['containerStatuses']=[]
                if defect=='foreign-pod-node':pod['spec']['nodeName']='foreign'
                def get(kind,name=None,namespace=None):
                    if kind=='instancemanagers.longhorn.io':return {'items':[{'metadata':{'name':'im','uid':'im-uid'},
                        'spec':{'nodeID':'longhorn-node-146','dataEngine':'v2','type':'aio','image':image},
                        'status':{'currentState':'running'}}]}
                    if kind=='settings.longhorn.io':return {'value':image}
                    if kind=='pods':return {'items':[pod]}
                    if kind=='nodes':return {'metadata':{'labels':{'kubernetes.io/arch':'s390x' if defect=='architecture' else 'amd64'}}}
                    raise AssertionError(kind)
                lab=SimpleNamespace(DIRECTORY=cluster,NODES=['192.168.47.146'],get=get)
                with patch.object(REVISION,'BASE',base):
                    gate=REVISION.profile()
                    if defect=='none':self.assertEqual(pod['status']['containerStatuses'][0]['imageID'],gate.managers(lab,image)['im']['image_id'])
                    else:
                        with self.assertRaisesRegex(ValueError,'runtime digest'):gate.managers(lab,image)
                        self.assertIsNone(gate.managers(lab,image,allow_transition=True))

    def test_fixed_static_registers_revision3_compile_and_exact_new_suite(self):
        source=(ROOT/'tests/helpers/longhorn-static.sh').read_text()
        self.assertEqual(1,source.count('"$PY" -m py_compile tests/helpers/longhorn-im-revision-3-adopt.py\n'))
        self.assertEqual(1,source.count('"$PY" -m unittest tests.unit.test_longhorn_im_revision_3_adopt -v\n'))

    def test_routes_and_preflight_reject_pending_plan_before_ssh_or_source_sync(self):
        runner=ROOT/'tests/helpers/longhorn-runner.sh'
        for engine in REVISION.ENGINES:
            marker='LONGHORN_'+engine.upper().replace('-','_')+'_IM_REVISION_3_ADOPT_QUALIFICATION_PASS'
            script='source "$1"; longhorn_launch() { printf "%s %s" "$1" "$2"; }; longhorn_dispatch "$2"'
            result=subprocess.run(['bash','-c',script,'contract',str(runner),'--longhorn-'+engine+'-im-revision-3-adopt-qualification'],capture_output=True,text=True)
            self.assertEqual(0,result.returncode,result.stderr);self.assertIn(marker,result.stdout)
        with tempfile.TemporaryDirectory() as directory:
            base=Path(directory);(base/'.venv/bin').mkdir(parents=True);(base/'.venv/bin/python').symlink_to(sys.executable)
            for name in (REVISION.DRIVER,REVISION.PLAN):
                target=base/name;target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes((ROOT/name).read_bytes())
            # Refusal is about an explicitly pending request, independent of
            # whether the repository's real publication is now ready.
            plan=json.loads((base/REVISION.PLAN).read_text())
            plan['status']='pending-publication-and-input-review'
            (base/REVISION.PLAN).write_text(json.dumps(plan))
            script='source "$1"; ROOT="$2"; longhorn_local_gate() { return 0; }; ssh_longhorn() { echo ILLEGAL_SSH; return 77; }; longhorn_launch v2-im-revision-3-adopt-qualification MARKER'
            result=subprocess.run(['bash','-c',script,'contract',str(runner),str(base)],capture_output=True,text=True)
            self.assertNotEqual(0,result.returncode);self.assertNotIn('ILLEGAL_SSH',result.stdout);self.assertIn('reviewed exact',result.stderr)
        source=runner.read_text()
        self.assertLess(source.index(' preflight --engine '),source.index('coproc '))

    def test_cleanup_refuses_partial_revision3_without_broadening_foreign_journal_scope(self):
        spec=importlib.util.spec_from_file_location('cleanup3',ROOT/'tests/helpers/longhorn-cleanup.py')
        gate=importlib.util.module_from_spec(spec);sys.modules[spec.name]=gate;spec.loader.exec_module(gate)
        for scope in gate.CLUSTERS:
            for revision in ('2','3','4'):
                with self.subTest(scope=scope,revision=revision),tempfile.TemporaryDirectory() as directory:
                    base=Path(directory);cluster=base/'clusters'/gate.CLUSTERS[scope];cluster.mkdir(parents=True)
                    owner={'cluster':gate.CLUSTERS[scope],'nodes':list(gate.NODES[scope]),'clean_start_verified':True,
                        'run_id':'12345678-1234-4234-8234-123456789012','cluster_uid':scope}
                    owner_path=cluster/'longhorn-lab-owner.json';owner_path.write_text(json.dumps(owner));owner_path.chmod(0o600)
                    engine='v2-dr-target' if scope=='target' else scope
                    journal=cluster/('longhorn-'+engine+'-im-revision-'+revision+'-adopt.json');journal.write_text('{}');journal.chmod(0o600)
                    with self.assertRaisesRegex(ValueError,'image adoption retained' if revision in ('2','3') else 'unknown'):
                        gate.plan(base)
                    self.assertEqual('{}',journal.read_text())
                    if revision=='3':
                        journal.write_text(json.dumps({'run_id':'foreign'}))
                        with self.assertRaisesRegex(ValueError,'foreign journal'):gate.plan(base)
                    if revision=='3':
                        journal.unlink();journal.symlink_to(owner_path)
                        with self.assertRaisesRegex(ValueError,'regular'):gate.plan(base)


    def reuse_fixture(self, base):
        evidence,name,current,accepted,path,plan,previous,write=self.completed_fixture(base)
        write('tests/evidence/longhorn-v2-aio-install-qualification-20261020.json',evidence)
        # The finite-impact reader delegates actual scope-log semantics to the
        # existing owning verifier. This isolated stub proves its exact call
        # boundary; the existing fault suites cover those log assertions.
        runtime='''import json
from pathlib import Path
BASE=Path('/usr/local/kubeauto')
def verify_scope(ref,scope,*,tag):
 row=json.loads((BASE/ref['path']).read_text())
 if row['signed_scope']!='v2-failure/'+scope or row['instance_manager_version']!=tag:raise ValueError('wrong single-disk receipt')
 return row
'''
        helper='tests/helpers/longhorn-v2-runtime-continuation.py'
        plan['profiles']['v2']['inputs'][helper]['sha256']=write(helper,runtime)
        sha=write(REVISION.PLAN,plan)
        inputs={key:row['sha256'] for key,row in plan['profiles']['v2']['inputs'].items()}
        accepted.update(plan_sha256=sha,frozen_inputs=inputs)
        accepted['transition']['plan']['sha256']=sha
        accepted['independent_readback'].update(plan_sha256=sha,frozen_inputs=inputs)
        write(path,accepted)
        ref=lambda name:{'path':name,'sha256':REVISION.digest(base/name)}
        contract_log='logs/source/green.log';write(contract_log,'isolated red/green boundary fixture\n')
        source={'status':'source-contract-pass','source_defect_reproduced':True,
            'source_commit':'cfd63a673e3047ca176185619565d5a81d33ec1c',
            'candidate_patch':{'sha256':'b74b2232435a30a7cc995382ec53192e7eae206f6c0dc78838727dd71e398380'},
            'red':{'rc':1},'green':{'rc':0,'new_subcases':13},'actual_json_null_fixture':True,
            'raw_evidence':{contract_log:REVISION.digest(base/contract_log)}}
        source_path='tests/evidence/longhorn-v2-raid-status-source-contract-20261022.json';write(source_path,source)
        old_full='tests/evidence/longhorn-v2-full-copy-reuse-impact-20261021.json'
        write(old_full,{'matrix_item':'LONGHORN-49','historical_scopes':[{},{}],
            'current_adoption_dependency':plan['profiles']['v2']['previous_evidence']})
        legacy={}
        for scope in REVISION.LEGACY_SCOPES:
            old_path='tests/evidence/longhorn-v2-failure-'+scope+'-20261021.json'
            write(old_path,{'status':'pass','rc':0,'failure_markers':0,'durable':{'exit':0,'finalized':0},
                'signed_scope':'v2-failure/'+scope,'version':'1.12.1','instance_manager_version':REVISION.OLD_TAG,
                'remote_input_verification':{'mismatches':0},'frozen_inputs':previous['frozen_inputs']})
            legacy[scope]=ref(old_path)
        single_path='tests/evidence/longhorn-v2-failure-single-disk-20261022.json'
        write(single_path,{'signed_scope':'v2-failure/single-disk','instance_manager_version':REVISION.NEW_TAG,
            'frozen_inputs':inputs,'raw_evidence':{}})
        impact={'schema':1,'status':'pass','scope':'v2-raid-status-finite-reuse','product_behavior_changed':True,
            'mechanism':'prior-RW-absent-from-configured-RAID-becomes-ERR',
            'publication_evidence':plan['publication_evidence'],'adoption_evidence':ref(path),
            'previous_evidence':plan['profiles']['v2']['previous_evidence'],'source_contract':ref(source_path),
            'single_disk':ref(single_path),'full_copy_impact':ref(old_full),'legacy_failure_scopes':legacy,
            'scope_impacts':{key:'bounded source and original-condition fixture' for key in (*REVISION.LEGACY_SCOPES,'full-copy')}}
        write(REVISION.REUSE_IMPACT,impact)
        return impact,ref,write

    def test_finite_reuse_requires_actual_publication_adoption_single_disk_and_explicit_changed_behavior(self):
        for defect in ('none','missing-impact','pending','publication','adoption','single-disk','false-behavior',
                       'missing-scope','source-patch','legacy-tag','single-input','wrong-old-fullcopy'):
            with self.subTest(defect=defect),tempfile.TemporaryDirectory() as directory:
                base=Path(directory);impact,ref,write=self.reuse_fixture(base)
                if defect=='pending':impact['status']='pending'
                if defect=='false-behavior':impact['product_behavior_changed']=False
                if defect=='missing-scope':del impact['legacy_failure_scopes']['manager']
                if defect=='source-patch':
                    name=impact['source_contract']['path'];row=json.loads((base/name).read_text())
                    row['candidate_patch']['sha256']='foreign';write(name,row);impact['source_contract']=ref(name)
                if defect=='legacy-tag':
                    name=impact['legacy_failure_scopes']['manager']['path'];row=json.loads((base/name).read_text())
                    row['instance_manager_version']=REVISION.NEW_TAG;write(name,row);impact['legacy_failure_scopes']['manager']=ref(name)
                if defect=='single-input':
                    name=impact['single_disk']['path'];row=json.loads((base/name).read_text());row['frozen_inputs']={}
                    write(name,row);impact['single_disk']=ref(name)
                if defect=='wrong-old-fullcopy':impact['full_copy_impact']['path']='tests/evidence/foreign.json';write('tests/evidence/foreign.json',{})
                write(REVISION.REUSE_IMPACT,impact)
                if defect=='missing-impact':(base/REVISION.REUSE_IMPACT).unlink()
                if defect in ('publication','adoption','single-disk'):
                    key={'publication':'publication_evidence','adoption':'adoption_evidence','single-disk':'single_disk'}[defect]
                    (base/impact[key]['path']).unlink()
                with patch.object(REVISION,'BASE',base):
                    if defect=='none':
                        row,refs=REVISION.read_reuse_impact();self.assertEqual(impact,row);self.assertIn(REVISION.REUSE_IMPACT,refs)
                        for scope in REVISION.LEGACY_SCOPES:
                            legacy=json.loads((base/impact['legacy_failure_scopes'][scope]['path']).read_text())
                            self.assertEqual(REVISION.OLD_TAG,legacy['instance_manager_version'])
                    else:
                        with self.assertRaises((ValueError,FileNotFoundError)):REVISION.read_reuse_impact()

    def test_reuse_transport_carries_exact_repair_origins_and_contracts_without_scanning(self):
        for defect in ('none', 'missing-origin', 'tampered-origin', 'missing-contract'):
            with self.subTest(defect=defect), tempfile.TemporaryDirectory() as directory:
                base=Path(directory);impact,ref,write=self.reuse_fixture(base)
                changes={}
                for name in REVISION.RETIREMENT_GATES:
                    if not (base/name).exists():write(name,'isolated unrelated gate fixture\n')
                    original='logs/repair-original/'+Path(name).name
                    old=write(original,(base/name).read_text())
                    if name=='tests/helpers/longhorn-cleanup.py':
                        write(name,(base/name).read_text()+'# completed abort archive classifier\n')
                    changes[name]={'sha256':REVISION.digest(base/name),'origins':{old:ref(original)}}
                inputs={name:edge['sha256'] for name,edge in changes.items()}
                write('logs/repair/contract.log','LONGHORN_V2_RETIREMENT_REPAIR_CONTRACT_PASS\n'+
                    'LONGHORN_V2_RETIREMENT_REPAIR_CONTRACT_INPUTS '+json.dumps(inputs)+'\n')
                for suffix in ('exit','finalized'):write('logs/repair/contract.'+suffix,'0\n')
                repair={'scope':'v2-upgrade-retirement-gc-repair','product_behavior_changed':False,'changes':changes,
                    'contract_receipts':{key:ref('logs/repair/contract.'+key) for key in ('log','exit','finalized')}}
                write(REVISION.RETIREMENT_REPAIR,repair)
                original=next(iter(changes['tests/helpers/longhorn-cleanup.py']['origins'].values()))
                if defect=='missing-origin':(base/original['path']).unlink()
                if defect=='tampered-origin':write(original['path'],'foreign bytes')
                if defect=='missing-contract':(base/'logs/repair/contract.log').unlink()
                with patch.object(REVISION,'BASE',base):
                    if defect=='none':
                        with patch.object(Path,'glob',side_effect=AssertionError('no broad scan')), \
                             patch.object(Path,'rglob',side_effect=AssertionError('no recursive scan')):
                            _,refs=REVISION.read_reuse_impact()
                        expected={REVISION.RETIREMENT_REPAIR:REVISION.digest(base/REVISION.RETIREMENT_REPAIR)}
                        for edge in changes.values():
                            expected.update({r['path']:r['sha256'] for r in edge['origins'].values()})
                        expected.update({r['path']:r['sha256'] for r in repair['contract_receipts'].values()})
                        self.assertEqual(expected,{name:refs[name] for name in expected})
                    else:
                        with self.assertRaises((ValueError,FileNotFoundError)):REVISION.read_reuse_impact()

    def test_normal_launcher_transfers_repair_raw_refs_to_a_clean_control_host(self):
        for defect in ('none','missing','tampered','outside','parent-symlink','replaced-current','existing-foreign'):
            with self.subTest(defect=defect),tempfile.TemporaryDirectory() as directory:
                base=Path(directory)/'source';remote=Path(directory)/'control'
                (base/'.venv/bin').mkdir(parents=True);remote.mkdir()
                (base/'.venv/bin/python').symlink_to(sys.executable)
                def write(name,blob):
                    path=base/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_text(blob)
                    return {'path':name,'sha256':hashlib.sha256(path.read_bytes()).hexdigest()}
                changes={}
                for name in REVISION.RETIREMENT_GATES:
                    blob=(ROOT/name).read_text()
                    current=write(name,blob)
                    original=write('logs/longhorn-cases/repair/original/'+name,blob+'\n# original fixture\n')
                    changes[name]={'sha256':current['sha256'],'origins':{original['sha256']:original}}
                receipts={key:write('logs/longhorn-cases/repair/contract.'+key,
                    'isolated contract fixture\n' if key=='log' else '0\n') for key in ('log','exit','finalized')}
                repair={'scope':'v2-upgrade-retirement-gc-repair','product_behavior_changed':False,
                    'changes':changes,'contract_receipts':receipts}
                original=next(iter(changes[REVISION.DRIVER]['origins'].values()))
                if defect=='missing':(base/original['path']).unlink()
                if defect=='tampered':(base/original['path']).write_text('foreign')
                if defect=='parent-symlink':
                    parent=(base/original['path']).parent;outside=Path(directory)/'outside'
                    parent.rename(outside);parent.symlink_to(outside,target_is_directory=True)
                if defect=='outside':
                    receipts['log']=write('unrelated/contract.log','not approved raw evidence')
                if defect=='replaced-current':(base/REVISION.DRIVER).write_text('# replaced helper\n')
                write(REVISION.RETIREMENT_REPAIR,json.dumps(repair))
                # The fixed transport guard detects the retained original impact.
                write('tests/evidence/longhorn-manager-pin-gate-impact-20261024.json','{}')
                write('logs/longhorn-cases/foreign.log','must not transfer')
                if defect=='existing-foreign':
                    target=remote/original['path'];target.parent.mkdir(parents=True);target.write_text('retain foreign bytes')
                binary=base/'bin';binary.mkdir()
                rsync=binary/'rsync'
                rsync.write_text('#!'+sys.executable+'\n'+'''import os,subprocess,sys
from pathlib import Path
args=sys.argv[1:]
assert '--ignore-existing' in args
assert args[-1]=='root@192.168.47.130:/usr/local/kubeauto/'
args[-1]=os.environ['CONTRACT_CONTROL']+'/'
Path(os.environ['CONTRACT_SOURCE'],'transfer-called').touch()
raise SystemExit(subprocess.call(['/usr/bin/rsync',*args]))
''');rsync.chmod(0o755)
                script='set -Eeuo pipefail; source "$1"; ROOT="$2"; longhorn_sync_retirement_evidence'
                result=subprocess.run(['bash','-c',script,'contract',str(ROOT/'tests/helpers/longhorn-runner.sh'),str(base)],
                    env=dict(os.environ,PATH=str(binary)+':'+os.environ['PATH'],CONTRACT_SOURCE=str(base),
                             CONTRACT_CONTROL=str(remote)),capture_output=True,text=True,timeout=10)
                valid=defect in ('none','existing-foreign')
                self.assertEqual(valid,result.returncode==0,result.stderr)
                self.assertEqual(valid,(base/'transfer-called').exists())
                self.assertFalse((remote/'logs/longhorn-cases/foreign.log').exists())
                if valid:
                    refs=list(receipts.values())+[ref for edge in changes.values() for ref in edge['origins'].values()]
                    with patch.object(REVISION,'BASE',remote):
                        for ref in refs:
                            if defect=='existing-foreign' and ref==original:
                                with self.assertRaises(ValueError):REVISION.referenced_path(ref)
                            else:REVISION.referenced_path(ref)
                    if defect=='existing-foreign':self.assertEqual('retain foreign bytes',(remote/original['path']).read_text())
                self.assertFalse(list((base/'logs/longhorn-cases').glob('retirement-transfer.*')))
        source=(ROOT/'tests/helpers/longhorn-runner.sh').read_text()
        start=source.index('if ! KUBEAUTO_SYNC_SKIP_CONTROL_SETUP=1')
        self.assertLess(start,source.index('if ! longhorn_sync_retirement_evidence;',start))
        self.assertLess(source.index('if ! longhorn_sync_retirement_evidence;',start),
                        source.index('printf \'%s\\n\' "$module"',start))

    def test_final_full_seams_are_frozen_before_real_plan_and_runtime_request_readers(self):
        # All publication/adoption/scope records below are owned temporary
        # fixtures. The actual plan, finite-impact, request and SHA readers run.
        spec=importlib.util.spec_from_file_location('order_runtime',ROOT/'tests/helpers/longhorn-v2-runtime-continuation.py')
        runtime=importlib.util.module_from_spec(spec);spec.loader.exec_module(runtime)
        spec=importlib.util.spec_from_file_location('order_old_compatibility',ROOT/'tests/helpers/longhorn-install-input-compatibility.py')
        compatibility=importlib.util.module_from_spec(spec);spec.loader.exec_module(compatibility)
        with tempfile.TemporaryDirectory() as directory:
            base=Path(directory);impact,ref,write=self.reuse_fixture(base)
            plan=json.loads((base/REVISION.PLAN).read_text());selected=plan['profiles']['v2']
            adoption=impact['adoption_evidence']['path'];accepted=json.loads((base/adoption).read_text())
            previous=json.loads((base/impact['previous_evidence']['path']).read_text())
            # Freeze the current final byte strings, including the actual full
            # reader seams and current compatibility reader, before planning.
            for name,row in selected['inputs'].items():
                if name==REVISION.CATALOG:continue
                row['sha256']=write(name,(ROOT/name).read_text())
            sha=write(REVISION.PLAN,plan);pins={name:row['sha256'] for name,row in selected['inputs'].items()}
            accepted.update(plan_sha256=sha,frozen_inputs=pins)
            accepted['transition']['plan']['sha256']=sha
            accepted['independent_readback'].update(plan_sha256=sha,frozen_inputs=pins)
            write('logs/adoption3/gate.log','LONGHORN_V2_IM_REVISION_3_ADOPT_QUALIFICATION_SCOPED_CLEAN_PASS\nLONGHORN_V2_IM_REVISION_3_ADOPT_QUALIFICATION_PASS\n')
            for key in ('exit','finalized'):write('logs/adoption3/gate.'+key,'0\n')
            accepted['receipt_refs']={'log':ref('logs/adoption3/gate.log'),**{key:ref('logs/adoption3/gate.'+key) for key in ('exit','finalized')}}
            accepted['raw_evidence']={value['path']:value['sha256'] for value in accepted['receipt_refs'].values()}
            write(adoption,accepted);impact['adoption_evidence']=ref(adoption)
            # Isolate only existing owning log semantics. Real scope readers
            # still require named rows, exact tags, frozen inputs, raw hashes,
            # original zero fences and the unique stage exit marker.
            owning="def verify_log(log,*args):\n    if 'OWNING_LOG_SEMANTICS_FIXTURE' not in log:raise ValueError('wrong owning log fixture')\n"
            for helper in ('longhorn-v2-control-faults.py','longhorn-v2-manager-loss.py','longhorn-v2-runtime-faults.py','longhorn-v2-disk-faults.py','longhorn-v2-all-replicas.py'):
                write('tests/helpers/'+helper,owning)
            scopes={}
            for scope in runtime.SCOPES:
                legacy=scope in REVISION.LEGACY_SCOPES
                path=('tests/evidence/longhorn-v2-failure-'+scope+'-20261021.json' if legacy else
                      'tests/evidence/longhorn-v2-failure-'+scope+'-20261022.json')
                prefix='logs/faults/'+scope
                write(prefix+'.log','OWNING_LOG_SEMANTICS_FIXTURE\nLONGHORN_V2_FAILURE_STAGE_EXIT rc=0\n')
                for key in ('exit','finalized'):write(prefix+'.'+key,'0\n')
                row={'status':'pass','rc':0,'failure_markers':0,'durable':{'exit':0,'finalized':0},
                    'signed_scope':'v2-failure/'+scope,'version':'1.12.1',
                    'instance_manager_version':REVISION.OLD_TAG if legacy else REVISION.NEW_TAG,
                    'remote_input_verification':{'mismatches':0},'frozen_inputs':previous['frozen_inputs'] if legacy else pins,
                    'raw_evidence':{prefix+'.'+key:ref(prefix+'.'+key)['sha256'] for key in ('log','exit','finalized')}}
                write(path,row);scopes[scope]=ref(path)
            impact['legacy_failure_scopes']={scope:scopes[scope] for scope in REVISION.LEGACY_SCOPES}
            impact['single_disk']=scopes['single-disk'];write(REVISION.REUSE_IMPACT,impact)
            contract='logs/contracts/current'
            write(contract+'.log','LONGHORN_V2_RUNTIME_CONTINUATION_CONTRACT_PASS\nLONGHORN_V2_RUNTIME_CONTINUATION_CONTRACT_INPUTS '+json.dumps({runtime.HELPER:pins[runtime.HELPER]},sort_keys=True)+'\n')
            for key in ('exit','finalized'):write(contract+'.'+key,'0\n')
            current={'scope':'focused-runtime-continuation-revision3','product_behavior_changed':True,'changes':{},
                'helper_sha256':pins[runtime.HELPER],'reuse_impact':ref(REVISION.REUSE_IMPACT),
                'contract_receipts':{key:ref(contract+'.'+key) for key in ('log','exit','finalized')}}
            write('tests/evidence/current-runtime-compatibility.json',current)
            (base/'logs/longhorn-cases').mkdir(parents=True,exist_ok=True)
            original_load=runtime.load
            def loader(name):
                return REVISION if name=='longhorn-im-revision-3-adopt.py' else original_load(name)
            profile=SimpleNamespace(verify_log=Mock())
            with patch.object(REVISION,'BASE',base),patch.object(runtime,'BASE',base), \
                    patch.object(runtime,'load',side_effect=loader),patch.object(REVISION,'profile',return_value=profile),redirect_stdout(io.StringIO()):
                actual=REVISION.read_plan('v2')
                self.assertEqual(plan,actual[0]);self.assertEqual(impact,REVISION.read_reuse_impact()[0])
                request=runtime.request('tests/evidence/current-runtime-compatibility.json',[row['path'] for row in scopes.values()])
                _,row=runtime.read_request(request)
                self.assertEqual(pins,row['frozen_inputs']);self.assertEqual(ref(adoption),row['previous_evidence'])
                # The old completed .2 map grants no exception to final full
                # reader bytes or arbitrary source changes.
                name='tests/helpers/longhorn-v2-install.py'
                self.assertFalse(compatibility.input_current(name,selected['inputs'][name]['old_sha256'],pins[name]))
                self.assertFalse(compatibility.input_current(name,pins[name],'f'*64))
                # Changing any actual full seam after plan freeze rejects both
                # current plan and already-created request, before live work.
                for name in ('tests/helpers/longhorn-full-material.py','tests/helpers/longhorn-v2-install.py','tests/helpers/longhorn-install-input-compatibility.py'):
                    path=base/name;source=path.read_text();path.write_text(source+'\n# late source change\n')
                    with self.assertRaises(ValueError):REVISION.read_plan('v2')
                    with self.assertRaises(ValueError):runtime.read_request(request)
                    path.write_text(source)
                # Current bytes alone cannot substitute missing publication.
                (base/PUBLICATION).unlink()
                with self.assertRaises((ValueError,FileNotFoundError)):REVISION.read_plan('v2')
                with self.assertRaises((ValueError,FileNotFoundError)):runtime.read_request(request)

    def test_revision3_continuation_has_no_post_adoption_byte_exceptions_and_keeps_six_original_tags(self):
        spec=importlib.util.spec_from_file_location('runtime3',ROOT/'tests/helpers/longhorn-v2-runtime-continuation.py')
        runtime=importlib.util.module_from_spec(spec);spec.loader.exec_module(runtime)
        for defect in ('none','missing-impact','false-behavior','post-adoption-seam','current-input','historical-ref','single-ref','adoption-finalized','adoption-marker'):
            with self.subTest(defect=defect),tempfile.TemporaryDirectory() as directory:
                base=Path(directory);base.joinpath('tests/evidence').mkdir(parents=True)
                def write(name,row):
                    p=base/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_text(row if isinstance(row,str) else json.dumps(row))
                    return {'path':name,'sha256':hashlib.sha256(p.read_bytes()).hexdigest()}
                helper_ref=write(runtime.HELPER,'reader fixture\n');input_ref=write('product/input','frozen\n')
                pins={runtime.HELPER:helper_ref['sha256'],'product/input':input_ref['sha256']}
                previous={'frozen_inputs':{'product/input':'old'}}
                old_ref=write('tests/evidence/previous.json',previous)
                log_ref=write('logs/adoption/gate.log','LONGHORN_V2_IM_REVISION_3_ADOPT_QUALIFICATION_SCOPED_CLEAN_PASS\nLONGHORN_V2_IM_REVISION_3_ADOPT_QUALIFICATION_PASS\n')
                exit_ref=write('logs/adoption/gate.exit','0\n');final_ref=write('logs/adoption/gate.finalized','0\n')
                prior={'status':'pass','rc':0,'failure_markers':0,'durable':{'exit':0,'finalized':0},
                    'signed_scope':'v2-im-revision-3-adopt-qualification','version':'1.12.1',
                    'instance_manager_version':REVISION.NEW_TAG,'remote_input_verification':{'mismatches':0},
                    'baseline_identity':{'cluster_uid':'cluster'},'frozen_inputs':pins,
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
                log=write('logs/contracts/check.log','LONGHORN_V2_RUNTIME_CONTINUATION_CONTRACT_PASS\n'+
                    'LONGHORN_V2_RUNTIME_CONTINUATION_CONTRACT_INPUTS '+json.dumps({runtime.HELPER:pins[runtime.HELPER]})+'\n')
                compatibility={'scope':'focused-runtime-continuation-revision3','product_behavior_changed':True,'changes':{},
                    'helper_sha256':pins[runtime.HELPER],'reuse_impact':impact_ref,
                    'contract_receipts':{'log':log,'exit':write('logs/contracts/check.exit','0\n'),
                                        'finalized':write('logs/contracts/check.finalized','0\n')}}
                if defect=='false-behavior':compatibility['product_behavior_changed']=False
                if defect=='post-adoption-seam':compatibility['changes']={'product/input':{'old_sha256':'old','sha256':'new'}}
                compat_ref=write('tests/evidence/compatibility.json',compatibility)
                row={'previous_evidence':prior_ref,'compatibility':compat_ref,'frozen_inputs':pins,'failure_scopes':copy.deepcopy(scopes)}
                if defect=='current-input':write('product/input','drift\n')
                if defect=='historical-ref':row['failure_scopes']['manager']=write('tests/evidence/other-manager.json',{})
                if defect=='single-ref':row['failure_scopes']['single-disk']=write('tests/evidence/other-disk.json',{})
                revision=SimpleNamespace(read_reuse_impact=Mock(return_value=(impact,{})),profile=lambda:SimpleNamespace(verify_log=Mock()), gate_input_current=lambda name, sha: runtime.digest(base/name) == sha)
                if defect=='missing-impact':revision.read_reuse_impact.side_effect=FileNotFoundError('pending actual impact')
                calls=[]
                def verified(ref,scope,*,tag):
                    calls.append((scope,tag));return {'frozen_inputs':previous['frozen_inputs'] if scope in REVISION.LEGACY_SCOPES else pins}
                with patch.object(runtime,'BASE',base),patch.object(runtime,'load',return_value=revision),patch.object(runtime,'verify_scope',side_effect=verified):
                    if defect=='none':
                        self.assertEqual(prior,runtime.read_inputs(row));self.assertEqual(9,len(calls))
                        self.assertEqual(6,sum(tag==REVISION.OLD_TAG for _,tag in calls))
                        self.assertEqual(3,sum(tag==REVISION.NEW_TAG for _,tag in calls))
                    else:
                        with self.assertRaises((ValueError,FileNotFoundError)):runtime.read_inputs(row)

    def test_fresh_full_revision3_consumes_this_clusters_real_initial_install_without_historical_adoption(self):
        spec=importlib.util.spec_from_file_location('fresh3_contract',ROOT/'tests/unit/test_longhorn_full_material.py')
        full=importlib.util.module_from_spec(spec);spec.loader.exec_module(full)
        spec=importlib.util.spec_from_file_location('fresh3_install',ROOT/'tests/helpers/longhorn-v2-install.py')
        install=importlib.util.module_from_spec(spec);spec.loader.exec_module(install)
        with patch.dict(os.environ),tempfile.TemporaryDirectory() as directory:
            os.environ.pop('LONGHORN_V2_RUNTIME_CONTINUATION',None)
            case=full.FullMaterialContracts();base,evidence_dir,path,prior,before,ref=case.fixture(directory)
            (base/'roles/longhorn/files/images.json').write_text(json.dumps({'1.12.1':{'longhorn-instance-manager':REVISION.NEW_TAG}}))
            prior['instance_manager_version']=REVISION.NEW_TAG
            for rows in (before['managers'],prior['independent_readback']['managers']):
                for row in rows.values():row['image']='registry/longhorn-instance-manager:'+REVISION.NEW_TAG
            prior['frozen_inputs']={name:full.UPGRADE.digest(base/name) for name in full.UPGRADE.INPUTS}
            path.write_text(json.dumps(prior))
            owned=base/'owned';receipt=owned/'yml/longhorn/block-disks.json';receipt.parent.mkdir(parents=True)
            receipt.write_text(json.dumps(before['native_block_receipt']))
            def get(kind,*args,**kwargs):
                if kind=='nodes.longhorn.io':
                    name=args[0];row=next(row for row in before['native_block_receipt']['nodes'].values() if row['name']==name)
                    return {'metadata':{'uid':row['longhorn_node_uid']},'status':{'diskStatus':{key:{'diskUUID':disk['disk_uuid']} for key,disk in row['disks'].items()}}}
                if kind in ('volumes.longhorn.io','pv','namespaces'):return {'items':[]}
                raise AssertionError(kind)
            lab=SimpleNamespace(DIRECTORY=owned,owner=lambda:before['baseline_identity'],get=get)
            with patch.object(full.UPGRADE,'BASE',base),patch.object(install,'BASE',base),\
                    patch.object(install,'load',return_value=full.UPGRADE),patch.object(install,'health'),\
                    patch.object(install,'revision3_adopted_input_current',side_effect=AssertionError('fresh full must not infer historical adoption')),\
                    redirect_stdout(io.StringIO()):
                self.assertEqual(prior,full.UPGRADE.previous())
                install.volume_baseline(lab)



    def test_revision3_ready50_consumes_exact52_request_without_claiming_complete50_or_changing_matrix(self):
        spec=importlib.util.spec_from_file_location('full3',ROOT/'tests/helpers/longhorn-full-evidence.py')
        full=importlib.util.module_from_spec(spec);spec.loader.exec_module(full)
        with tempfile.TemporaryDirectory() as directory:
            base=Path(directory);runtime_ref={'path':'runtime.json','sha256':'runtime-sha'}
            previous={'path':'tests/evidence/longhorn-v2-im-revision-3-adopt-qualification-20261022.json','sha256':'adoption-sha'}
            request={'scope':'focused-runtime-continuation-request','previous_evidence':previous}
            path=base/'request.json';path.write_text(json.dumps(request))
            runtime={**request,'scope':'focused-runtime-continuation','request':{'path':'request.json'}}
            result=base/'result.json';result.write_text('{}')
            upgrade={'status':'pass','rc':0,'failure_markers':0,'durable':{'exit':0,'finalized':0},
                'signed_scope':'v2-upgrade-lifecycle','version':'1.12.1','remote_input_verification':{'mismatches':0},
                'transition':{'previous_evidence':{**previous,'runtime_continuation':runtime_ref},
                    'upgrade_result':{'path':'result.json'},'before':{'original':'identity'}}}
            runtime['baseline']={'original':'identity'}
            runtime['request']={'path':'request.json'}
            accepted_path=base/'upgrade.json';accepted_path.write_text(json.dumps(upgrade))
            reader=SimpleNamespace(ADOPTION3=previous['path'],material=Mock(return_value=runtime),
                resolve=lambda ref:base/ref['path'],stable=Mock())
            validator=SimpleNamespace(verify_execution=Mock())
            cases=[{'id':'LONGHORN-52','status':'pass','implementation_status':'implemented','evidence':'upgrade.json'}]
            with patch.object(full,'BASE',base),patch.object(full,'load',side_effect=lambda n:reader if n=='longhorn-v2-runtime-continuation.py' else validator):
                with redirect_stdout(io.StringIO()) as output:full.focused_failure50(cases)
                self.assertIn('full_module_pass=false',output.getvalue());self.assertNotIn('LONGHORN_V2_FAILURE_PASS',output.getvalue())
                reader.material.assert_called_once_with(runtime_ref)
                cases[0]['status']='pending'
                with self.assertRaisesRegex(ValueError,'completed LONGHORN-52'):full.focused_failure50(cases)
                cases[0]['status']='pass';reader.material.side_effect=FileNotFoundError('missing actual single-disk/impact')
                with self.assertRaises(FileNotFoundError):full.focused_failure50(cases)

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



class LinkedLegacyInputContracts(unittest.TestCase):
    def test_only_two_unused_method_bodies_can_differ_for_real52_legacy_consumer(self):
        name = 'tests/helpers/longhorn-v2-linked-lifecycle.py'
        fixture = 'tests/fixtures/longhorn-v2-linked-lifecycle-reviewed-65a880c6.py.txt'
        original = (ROOT/fixture).read_bytes()
        expected = hashlib.sha256(original).hexdigest()
        current = (ROOT/name).read_text()
        with tempfile.TemporaryDirectory() as temporary, patch.object(REVISION, 'BASE', Path(temporary)):
            base = Path(temporary)
            for path, data in ((fixture, original), (name, current.encode())):
                target = base/path; target.parent.mkdir(parents=True, exist_ok=True); target.write_bytes(data)
            self.assertTrue(REVISION.gate_input_current(name, expected))
            # The actual old/new sources retain the entire legacy refusal path.
            for source in (current.replace("NS = 'longhorn-system'", "NS = 'foreign'", 1),
                           current.replace("origin.get('created_version') != '1.12.0'", "False", 1),
                           current.replace('def restore(self, name):', 'def restore(self, name=print(1)):', 1),
                           current.replace('def cleanup(self):', '@print(1)\n    def cleanup(self):', 1)):
                with self.subTest(mutation=source != current):
                    (base/name).write_text(source)
                    self.assertFalse(REVISION.gate_input_current(name, expected))
            (base/name).write_text(current)
            (base/fixture).write_bytes(original+b'\n# tampered source\n')
            self.assertFalse(REVISION.gate_input_current(name, expected))


if __name__ == '__main__':
    unittest.main()
