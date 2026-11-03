"""Finite maintenance composition and actual disk/drain observation contracts."""
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT=Path(__file__).resolve().parents[2]
def module(name):
    spec=importlib.util.spec_from_file_location(name.replace('-','_'),ROOT/'tests/helpers'/name)
    value=importlib.util.module_from_spec(spec);spec.loader.exec_module(value);return value
M=module('longhorn-v2-maintenance.py');D=module('longhorn-v2-disk-lifecycle.py')

class CompositionContracts(unittest.TestCase):
    def fixture(self, directory):
        base=Path(directory)
        def write(name,value):
            path=base/name;path.parent.mkdir(parents=True,exist_ok=True)
            path.write_text(value if isinstance(value,str) else json.dumps(value));return path
        def ref(path):return {'path':str(path.relative_to(base)),'sha256':M.digest(path)}
        product=write(M.PRODUCT,'product'); scopes={}
        for scope in M.SCOPES:
            own=write('tests/helpers/'+M.SPECS[scope][1],scope)
            inputs={M.PRODUCT:M.digest(product),'tests/helpers/'+own.name:M.digest(own)}
            execution={key:ref(write('logs/'+scope+'/'+key,'ORIGINAL_BUSINESS_OBSERVATION\n' if key=='log' else '0\n')) for key in ('log','exit','finalized')}
            row={'status':'pass','rc':0,'failure_markers':0,'version':'1.12.1','signed_scope':M.SPECS[scope][0],
                 'frozen_inputs':inputs,'raw_logs':{x['path']:x['sha256'] for x in execution.values()},
                 'baseline_identity':{'run_id':'run','cluster_uid':'core','node_uids':{n:n+'-uid' for n in M.NODES}}}
            scopes[scope]={'evidence':ref(write('tests/evidence/'+scope+'.json',row)),**execution,'artifacts':{}}
        return base,scopes,write,ref

    def test_all_seven_explicit_raw_executions_dispatch_once_without_live_launch(self):
        with tempfile.TemporaryDirectory() as d:
            base,scopes,_,_=self.fixture(d)
            with patch.object(M,'verify_business',side_effect=lambda b,s,l,a:{'scope':s}) as verify:
                result=M.validate_maintenance51(base,scopes)
            self.assertEqual(result['signed_scope'],'v2-maintenance');self.assertEqual(verify.call_count,7)
            self.assertEqual(set(result['scopes']),set(M.SCOPES));self.assertNotIn('v2-failure',result['signed_scope'])

    def test_missing_or_unknown_scope_never_signs_maintenance(self):
        with tempfile.TemporaryDirectory() as d:
            base,scopes,_,_=self.fixture(d)
            for key in M.SCOPES:
                actual=copy.deepcopy(scopes);actual.pop(key)
                with self.assertRaisesRegex(ValueError,'seven'):M.validate_maintenance51(base,actual)
            with self.assertRaisesRegex(ValueError,'seven'):M.validate_maintenance51(base,{**scopes,'foreign':{}})

    def test_changed_raw_failed_fence_unbound_log_and_symlink_are_refused(self):
        for defect in ('raw','exit','finalized','unbound','symlink'):
            with self.subTest(defect=defect),tempfile.TemporaryDirectory() as d:
                base,scopes,write,ref=self.fixture(d);scope=scopes['platform']
                if defect=='raw':(base/scope['log']['path']).write_text('changed')
                elif defect in ('exit','finalized'):
                    path=base/scope[defect]['path'];path.write_text('1\n');scope[defect]=ref(path)
                    evidence=base/scope['evidence']['path'];row=json.loads(evidence.read_text());row['raw_logs'][str(path.relative_to(base))]=M.digest(path);evidence.write_text(json.dumps(row));scope['evidence']=ref(evidence)
                elif defect=='unbound':scope['log']=ref(write('logs/foreign','business'))
                else:
                    path=base/scope['log']['path'];saved=path.with_name('saved');path.rename(saved);path.symlink_to(saved)
                with patch.object(M,'verify_business',return_value={}),self.assertRaises(ValueError):M.validate_maintenance51(base,scopes)

    def test_pending_wrong_scope_implementation_drift_and_foreign_core_are_refused(self):
        for defect in ('pending','scope','version','implementation','core'):
            with self.subTest(defect=defect),tempfile.TemporaryDirectory() as d:
                base,scopes,write,ref=self.fixture(d);item=scopes['middleware'];path=base/item['evidence']['path'];row=json.loads(path.read_text())
                if defect=='pending':row['status']='pending'
                elif defect=='scope':row['signed_scope']='v1-middleware'
                elif defect=='version':row['version']='1.12.0'
                elif defect=='implementation':write('tests/helpers/'+M.SPECS['middleware'][1],'changed code')
                else:row['baseline_identity']['cluster_uid']='other'
                path.write_text(json.dumps(row));item['evidence']=ref(path)
                with patch.object(M,'verify_business',return_value={}),self.assertRaises(ValueError):M.validate_maintenance51(base,scopes)

    def test_original_verifier_failure_is_not_hidden_by_accepted_envelope(self):
        with tempfile.TemporaryDirectory() as d:
            base,scopes,_,_=self.fixture(d)
            with patch.object(M,'verify_business',side_effect=ValueError('actual ACK changed')),self.assertRaisesRegex(ValueError,'ACK'):M.validate_maintenance51(base,scopes)

    def test_reviewed_reader_transition_keeps_original_inputs_and_default_strictness(self):
        with tempfile.TemporaryDirectory() as d:
            base, scopes, write, _ = self.fixture(d)
            name = 'tests/helpers/'+M.SPECS['install'][1]
            original = M.digest(base/name); write(name, 'cleanup daemon reload only')
            before = (base/scopes['install']['evidence']['path']).read_bytes()
            with patch.object(M, 'verify_business', return_value={}):
                with self.assertRaisesRegex(ValueError, 'implementation changed'):
                    M.validate_maintenance51(base, scopes)
                result = M.validate_maintenance51(base, scopes,
                    input_current=lambda path, old: path == name and old == original)
            self.assertEqual('pass', result['status'])
            self.assertEqual(before, (base/scopes['install']['evidence']['path']).read_bytes())
            write(M.PRODUCT, 'foreign product')
            with patch.object(M, 'verify_business', return_value={}), self.assertRaisesRegex(ValueError, 'implementation changed'):
                M.validate_maintenance51(base, scopes, input_current=lambda *args: True)

    def test_public_route_requires_explicit_input_and_never_launches_live(self):
        helper=ROOT/'tests/helpers/longhorn-runner.sh';source=helper.read_text();a=source.index('longhorn_v2_maintenance() {');b=source.index('\n}\n',a)+3
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);(root/'.venv/bin').mkdir(parents=True);python=root/'.venv/bin/python';python.write_text('#!/bin/sh\nprintf "%s\\n" "$@"\n');python.chmod(0o700)
            script='ROOT='+str(root)+'\n'+source[a:b]+'\nlonghorn_launch() { exit 99; }\n'
            failed=subprocess.run(['bash','-c',script+'longhorn_v2_maintenance'],capture_output=True,text=True)
            self.assertNotEqual(failed.returncode,0);self.assertIn('no-automatic-replay',failed.stderr)
            okay=subprocess.run(['bash','-c',script+'LONGHORN_V2_MAINTENANCE_EVIDENCE_JSON=tests/evidence/explicit.json\nlonghorn_v2_maintenance'],capture_output=True,text=True)
            self.assertEqual(okay.returncode,0);self.assertIn('--evidence\ntests/evidence/explicit.json',okay.stdout)

class DiskObservationContracts(unittest.TestCase):
    def fixture(self):
        nodes=['longhorn-node-'+str(i) for i in (146,147,148)]
        before={str(i):{'node':n,'uid':'replica-'+str(i)} for i,n in enumerate(nodes)}
        moved={**{k:v for k,v in before.items() if v['node']!=nodes[-1]},'new':{'node':D.NAME,'uid':'replica-new'}}
        uuids={a:'uuid-'+a for a in D.SOURCE+(D.HOST,)}
        row={'phase':'clean','business_passed':True,'host':D.HOST,'run_id':'run','cluster_uid':'core',
             'raw_disks':{a:{'identity':a,'path':'/dev/disk/by-id/'+a} for a in uuids},'disk_uuids':uuids,
             'retired':[D.SOURCE[-1],D.HOST],'canaries':{},'multipath':{'phase':'restored'},'payload':'payload',
             'binding':{'pv':{'uid':'pv'},'pvc':{'uid':'pvc'},'volume':{'uid':'volume'}},'original_replicas':before}
        lines=['LONGHORN_V2_DISK_DATA_READBACK '+json.dumps({'sha256':hashlib.sha256(row['payload'].encode()*262144).hexdigest(),
               'payload_bytes':len(row['payload'].encode())*262144,'binding':row['binding'],'disk_uuids':uuids}) for _ in range(7)]
        lines+=['LONGHORN_V2_DISK_REPLICA_RELOCATION '+json.dumps({'source':D.SOURCE[-1],'target':D.HOST,'before':before,'after':moved}),
                'LONGHORN_V2_DISK_ORIGINAL_TOPOLOGY_RESTORED '+json.dumps(before),
                'LONGHORN_V2_DISK_CLIENT_EXPANSION_PASS host='+D.HOST+' selective_disable=true raw_disks=4',
                'LONGHORN_V2_DISK_REPLACEMENT_REIMPORT_PASS source='+D.SOURCE[-1]+' replica_domain='+D.HOST,
                'LONGHORN_V2_DISK_LIFECYCLE_BUSINESS_PASS','LONGHORN_V2_DISK_LIFECYCLE_SCOPED_CLEAN_PASS','LONGHORN_V2_DISK_LIFECYCLE_FINALIZED',
                'LONGHORN_V2_DISK_EXPECTED_REFUSAL step=longhorn-setup','LONGHORN_V2_DISK_EXPECTED_REFUSAL step=del-node']
        return row,lines

    def test_actual_sha_binding_relocation_and_restoration_are_consumed(self):
        row,lines=self.fixture();self.assertEqual(D.verify_log('\n'.join(lines),row)['readbacks'],7)

    def test_payload_binding_failure_domain_missing_readback_or_cleanup_refused(self):
        for defect in ('payload','binding','domain','missing','cleanup','failure','duplicate','empty-disks','retirement-refusal'):
            with self.subTest(defect=defect):
                row,lines=self.fixture()
                if defect in ('payload','binding'):
                    value=json.loads(lines[0].split(' ',1)[1]);value['sha256' if defect=='payload' else 'binding']='foreign';lines[0]='LONGHORN_V2_DISK_DATA_READBACK '+json.dumps(value)
                elif defect=='domain':
                    i=next(i for i,l in enumerate(lines) if l.startswith('LONGHORN_V2_DISK_REPLICA_RELOCATION '));value=json.loads(lines[i].split(' ',1)[1]);value['after']=value['before'];lines[i]='LONGHORN_V2_DISK_REPLICA_RELOCATION '+json.dumps(value)
                elif defect=='missing':lines=lines[3:]
                elif defect=='cleanup':row['phase']='running'
                elif defect=='duplicate':
                    i=next(i for i,l in enumerate(lines) if l.startswith('LONGHORN_V2_DISK_REPLICA_RELOCATION '));value=json.loads(lines[i].split(' ',1)[1]);value['after']['new']['uid']='replica-0';lines[i]='LONGHORN_V2_DISK_REPLICA_RELOCATION '+json.dumps(value)
                elif defect=='empty-disks':
                    value=json.loads(lines[0].split(' ',1)[1]);value['disk_uuids']={};lines[0]='LONGHORN_V2_DISK_DATA_READBACK '+json.dumps(value)
                elif defect=='retirement-refusal':lines.remove('LONGHORN_V2_DISK_EXPECTED_REFUSAL step=del-node')
                else:lines.append('LONGHORN_FIRST_FAILURE rc=1')
                with self.assertRaises(ValueError):D.verify_log('\n'.join(lines),row)

    def test_data_observer_records_only_original_return_without_additional_api_or_mutation(self):
        scenario=D.Lifecycle.__new__(D.Lifecycle);scenario.workload=SimpleNamespace(identities=Mock(return_value={'pv':'uid'}),data=Mock(return_value='actual-sha'))
        scenario.row=Mock(return_value={'binding':{'pv':'uid'},'payload':'token','disk_uuids':{'host':'uuid'}})
        with patch('builtins.print') as output:self.assertEqual(scenario.data(),'actual-sha')
        scenario.workload.data.assert_called_once_with('token',read=True)
        self.assertEqual(json.loads(output.call_args.args[0].split(' ',1)[1])['sha256'],'actual-sha')

class DrainObservationContracts(unittest.TestCase):
    def fixture(self):
        faults=module('longhorn-faults.py');control=module('longhorn-control-faults.py');token='run:maintenance'
        acks=[{'type':'ack','sequence':i,'sha256':hashlib.sha256(faults.ledger_block(token,i)).hexdigest()} for i in range(4)]
        ledger=control.confirmed(acks,token,faults)
        replicas={n:{'node':n,'uid':n+'-replica'} for n in sorted(M.NODES)};target=sorted(M.NODES)[0]
        rows={'LONGHORN_MAINTENANCE_ACKNOWLEDGEMENTS ':ledger,
              'LONGHORN_MAINTENANCE_LAST_REPLICA_REFUSAL_PASS ':{'node':target,'pdb_uid':'pdb','replicas':{target:replicas[target]},'restored':replicas},
              'LONGHORN_MAINTENANCE_MIGRATION_VERIFIED ':{'node':target,'before':{'uid':'old','node':target},'after':{'uid':'new','node':sorted(M.NODES)[1]}}}
        lines=[p+json.dumps(v) for p,v in rows.items()]
        lines+=['LONGHORN_MAINTENANCE_DRAIN_RESULT '+json.dumps(r) for r in ({'node':target,'refusal_expected':True,'rc':1,'stderr':'Cannot evict pod: would violate disruption budget'}, {'node':target,'refusal_expected':False,'rc':0})]
        lines+=['LONGHORN_V2_MAINTENANCE_DRAIN_'+s for s in ('BUSINESS_PASS','FIXTURES_CLEAN_PASS','QUALIFICATION_SCOPED_CLEAN_PASS','QUALIFICATION_PASS')]
        return lines

    def test_native_pdb_refusal_all_raw_acks_and_actual_application_move(self):
        self.assertEqual(M.verify_drain(ROOT,'\n'.join(self.fixture()))['ledger']['confirmed'],4)

    def test_corrupt_ack_wrong_refusal_and_same_application_pod_are_rejected(self):
        for defect in ('ack','pdb','pod'):
            with self.subTest(defect=defect):
                lines=self.fixture()
                if defect=='ack':value=json.loads(lines[0].split(' ',1)[1]);value['records'][0]['sha256']='0'*64;lines[0]=lines[0].split(' ',1)[0]+' '+json.dumps(value)
                elif defect=='pdb':lines=[l.replace('Cannot evict pod: would violate disruption budget','connection timeout') for l in lines]
                else:value=json.loads(lines[2].split(' ',1)[1]);value['after']['uid']='old';lines[2]=lines[2].split(' ',1)[0]+' '+json.dumps(value)
                with self.assertRaises(ValueError):M.verify_drain(ROOT,'\n'.join(lines))

if __name__=='__main__':unittest.main()
