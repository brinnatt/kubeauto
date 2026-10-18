"""Business gate result, credential redaction, cleanup and idempotence boundaries."""
import copy
import base64
import importlib.util
import json
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('mongodb_scenarios', ROOT / 'tests/helpers/mongodb-scenarios.py')
scenarios = importlib.util.module_from_spec(spec)
spec.loader.exec_module(scenarios)


class MongoDBScenarioTests(unittest.TestCase):
    def test_performance_ledger_streams_every_confirmation_under_a_bounded_heap(self):
        s = scenarios.Scenario(ROOT);gate = scenarios.Performance(s)
        result = {'phase': 'upgrade', 'workers': [{'worker': i, 'acknowledged': 100000} for i in range(4)],
                  'writes': 400000}
        with patch.object(s, 'js') as js, patch.object(scenarios, 'performance_result', return_value=result), \
                redirect_stdout(StringIO()):
            gate.phase({'fault': False, 'phase': 'upgrade', 'concurrency': 4, 'seconds': 3600})
        program = js.call_args.args[0]
        harness = '''const prefix='mongodb_gate_lazy',print=()=>{};let checked=0;
const c={getDB:name=>({getCollection:name=>({
 find:q=>{let i=0;const count=100000+(MODE==='extra'&&q.worker===0?1:MODE==='missing'&&q.worker===0?-1:0);
 const cursor={sort:()=>cursor,batchSize:n=>{assert(n>0&&n<=256);return cursor;},
 toArray:()=>{throw Error('unbounded ledger materialization');},hasNext:()=>i<count,next:()=>{
 const sequence=i++;checked++;return {_id:'upgrade-'+q.worker+'-'+sequence,
 owner:MODE==='owner'&&sequence===0?'foreign':prefix,
 sequence:MODE==='sequence'&&sequence===0?1:sequence};}};return cursor;},countDocuments:()=>4096
})})};
'''
        for mode in ('normal', 'owner', 'sequence', 'extra', 'missing'):
            with self.subTest(mode=mode):
                run = scenarios.subprocess.run([scenarios.shutil.which('node'), '--max-old-space-size=64'],
                    input=scenarios.ASSERT+'const MODE='+json.dumps(mode)+';\n'+harness+program+
                          '\nassert.eq(400000,checked);', capture_output=True, text=True, timeout=20)
                self.assertEqual(run.returncode == 0, mode == 'normal', run.stderr)

    def test_client_creation_keeps_uid_and_bounds_cold_pull_before_business(self):
        s = scenarios.Scenario(ROOT)
        with patch.object(s.lab, 'get', return_value=None), patch.object(s.lab, 'run') as run, \
                patch.object(scenarios.subprocess, 'run', return_value=SimpleNamespace(
                    returncode=0, stdout=json.dumps({'metadata': {'uid': 'client-uid'}}))):
            s.client_create()
        self.assertEqual(s.client_uid, 'client-uid')
        self.assertIn('--timeout=600s', run.call_args.args)
        self.assertEqual(run.call_args.kwargs['timeout'], 620)

    def test_logging_sample_keeps_one_historical_file_without_assuming_retroactive_size_limit(self):
        self.assertTrue(scenarios.logging_sample('1024\n2388808662\n1\n4096 100000 90000\n'))
        for sample in ('1024\n2048\n0\n4096 100000 90000',
                       '1024\n2048\n2\n4096 100000 90000',
                       '1024\n2048\n1\n4096 100000 10000',
                       '100000000\n2048\n1\n4096 100000 90000'):
            with self.subTest(sample=sample):self.assertFalse(scenarios.logging_sample(sample))
        for sample in ('1024\n2048\n4096 100000 90000','1024\n2048\n1\n4096 0 10',
                       '1024\n2048\n1\n4096 100000 100001'):
            with self.subTest(sample=sample),self.assertRaises(ValueError):scenarios.logging_sample(sample)

    def test_logging_migration_grows_before_policy_and_checks_native_consumption(self):
        import yaml
        policy={'interval_minutes':1,'max_size_mb':32,'retain':1}
        for failure in (None,'owner','configmap','schedule','identity','product'):
            with self.subTest(failure=failure),tempfile.TemporaryDirectory() as tmp:
                s=scenarios.Scenario(ROOT);s.lab.directory=Path(tmp)
                path=Path(tmp,'config.yml');path.write_text('mongodb_pvc_size: 7Gi\n')
                cr={'metadata':{'uid':'source'},'spec':{'logcollector':{'logrotate':{
                    'schedule':'*/1 * * * *','configuration':'native conf'}}}}
                cm={'metadata':{'ownerReferences':[{'uid':'foreign' if failure=='owner' else 'source','controller':True}]},
                    'data':{'mongodb.conf':'wrong' if failure=='configmap' else 'native conf'}}
                member={'metadata':{'name':'owned'},'spec':{'containers':[{'name':'logrotate',
                    'env':[{'name':'LOGROTATE_SCHEDULE','value':'bad' if failure=='schedule' else '*/1 * * * *'}]}]}}
                snapshot={'pvc':['claim'],'secrets':['secret'],'psmdb':[{'name':'data','uid':'source'}]}
                after=copy.deepcopy(snapshot)
                if failure=='identity':after['pvc']=['rebound']
                calls=[]
                def expand(**kwargs):
                    self.assertEqual(kwargs,{'logging':True})
                    self.assertNotIn('mongodb_logrotate',yaml.safe_load(path.read_text()))
                    path.write_text('mongodb_pvc_size: 10Gi\n');calls.append('growth')
                def product(*args,**kwargs):
                    self.assertEqual(calls,['growth']);calls.append('policy')
                    config=yaml.safe_load(path.read_text());self.assertEqual(config['mongodb_pvc_size'],'10Gi')
                    self.assertEqual(config['mongodb_logrotate'],policy)
                    return SimpleNamespace(returncode=int(failure=='product'))
                with patch.object(s,'capacity_expand',side_effect=expand),patch.object(s,'client_create'), \
                        patch.object(s,'capacity_claims',return_value={'claim':'same'}), \
                        patch.object(s,'snapshot',side_effect=[snapshot,after]),patch.object(s,'health'), \
                        patch.object(s,'maintenance_members',return_value=[member]*3), \
                        patch.object(s.lab,'get',side_effect=[cr,cm]), \
                        patch.object(s.lab,'run',return_value='1024\n2388808662\n1\n4096 100000 90000\n'), \
                        patch.object(s,'js') as js,patch.object(scenarios.Performance,'backup_dependencies') as backups, \
                        patch.object(scenarios.subprocess,'run',side_effect=product):
                    if failure:
                        with self.assertRaises(ValueError):s.logging()
                        js.assert_not_called();backups.assert_not_called()
                    else:
                        s.logging();js.assert_called_once();backups.assert_called_once()
                self.assertEqual(calls,['growth','policy'])

    def test_logging_refuses_unowned_fixture_policy_before_mutation(self):
        import yaml
        for values in ({'mongodb_pvc_size':'100Gi'},{'mongodb_pvc_size':'7Gi','mongodb_logrotate':{'retain':99}}):
            with tempfile.TemporaryDirectory() as tmp:
                s=scenarios.Scenario(ROOT);s.lab.directory=Path(tmp)
                Path(tmp,'config.yml').write_text(yaml.safe_dump(values))
                with patch.object(s,'capacity_expand') as expand,patch.object(scenarios.subprocess,'run') as run:
                    with self.assertRaisesRegex(ValueError,'unknown fixture'):s.logging()
                    expand.assert_not_called();run.assert_not_called()

    def test_monitoring_system_user_matches_official_read_roles_and_shell_refusals(self):
        s=scenarios.Scenario(ROOT);g=scenarios.Observability(s)
        with patch.object(g,'client'),patch.object(g,'clean_client') as clean,patch.object(s,'js') as js:
            g.privileges();program=js.call_args.args[0];clean.assert_called_once()
        harness='''const prefix='mongodb_gate_test';const print=()=>{};
const mockRoles=[{role:'clusterMonitor',db:'admin'},{role:'directShardOperations',db:'admin'},
 {role:'explainRole',db:'admin'},{role:'read',db:'local'}];
if(MODE==='extra-role')mockRoles.push({role:'readWriteAnyDatabase',db:'admin'});
let reads=0,writes=0;
class Mongo{getDB(database){return {auth:()=>true,runCommand:q=>{
 if(q.connectionStatus)return {ok:1,authInfo:{authenticatedUserRoles:mockRoles}};
 if(q.serverStatus||q.replSetGetStatus)return {ok:MODE==='diagnostic-denied'?0:1};
 if(q.find){reads++;if(MODE==='read-denied')throw Object.assign(Error(),{code:13});
  return {ok:1,cursor:{firstBatch:[]}};}
 if(q.insert){writes++;if(MODE==='write-allowed')return {ok:1};
  throw Object.assign(Error(),{name:'MongoServerError',code:MODE==='wrong-error'?18:13});}
 throw Error('unexpected-command');}};}}
'''
        for mode in ('normal','extra-role','diagnostic-denied','read-denied','write-allowed','wrong-error'):
            with self.subTest(mode=mode):
                result=scenarios.subprocess.run([scenarios.shutil.which('node')],
                    input=scenarios.ASSERT+'const MODE='+json.dumps(mode)+';\n'+harness+program+
                          '\nassert.eq(2,reads);assert.eq(1,writes);',text=True,capture_output=True)
                self.assertEqual(result.returncode==0,mode=='normal',mode+': '+result.stderr)

    def monitoring_workers(self):
        return {'mongodb-node-'+ip.rsplit('.',1)[1]:ip
                for ip in ('192.168.122.212','192.168.122.165','192.168.122.238')}

    def test_monitoring_uses_real_exporter_kubelet_and_backup_state_with_read_only_rbac(self):
        name='mongodb-observe-'+'a'*16
        objects=scenarios.observability_objects(name,self.monitoring_workers(),'mongodb-gate-data',60)
        self.assertEqual(len(objects),15)
        self.assertTrue(all(o['metadata'].get('namespace') in (None,'mongodb-gate') for o in objects))
        roles=[o for o in objects if o['kind'] in ('Role','ClusterRole')]
        for obj in roles:
            for rule in obj['rules']:
                self.assertTrue(set(rule['verbs'])<={'get','list','watch'})
                self.assertFalse(set(rule['resources'])&{'secrets','nodes/proxy','pods/exec','*'})
        metrics=next(rule for o in roles for rule in o['rules'] if rule['resources']==['nodes/metrics'])
        self.assertEqual(metrics['resourceNames'],sorted(self.monitoring_workers()))
        config=next(o['data'] for o in objects if o['kind']=='ConfigMap')
        prom=scenarios.yaml.safe_load(config['prometheus.yml'])
        kubelet=next(j for j in prom['scrape_configs'] if j['job_name']=='mongodb-volume')
        self.assertEqual(kubelet['scheme'],'https');self.assertEqual(kubelet['metrics_path'],'/metrics')
        self.assertNotIn('insecure_skip_verify',kubelet['tls_config'])
        self.assertEqual({x['targets'][0] for x in kubelet['static_configs']},
                         {ip+':10250' for ip in self.monitoring_workers().values()})
        custom=scenarios.yaml.safe_load(config['custom.yaml'])['spec']['resources'][0]
        self.assertEqual(custom['metrics'][0]['each']['gauge']['path'],['status','completed'])
        alerts=scenarios.yaml.safe_load(config['alerts.yml'])['groups'][0]['rules']
        self.assertEqual({r['alert'] for r in alerts},
            {'MongoDBMemberUnavailable','MongoDBReplicationLag','MongoDBVolumeLowSpace','MongoDBBackupStale'})
        self.assertIn('min by(member_idx)',alerts[0]['expr'])
        self.assertNotIn('up == 0',config['alerts.yml'])
        for pod in (o for o in objects if o['kind']=='Pod'):
            self.assertEqual(pod['spec']['restartPolicy'],'Always')
            container=pod['spec']['containers'][0]
            self.assertNotIn('secret',json.dumps(container))
            self.assertTrue(container['image'].startswith('registry.talkschool.cn:5000/mongodb-lab/brinnatt/'))

    def test_monitoring_rejects_foreign_node_address_and_fixture_before_building_objects(self):
        for field in ('name','address','source','age'):
            with self.subTest(field=field):
                workers=self.monitoring_workers()
                if field=='address':workers['mongodb-node-212']='192.168.122.217'
                with self.assertRaises(ValueError):
                    scenarios.observability_objects('foreign' if field=='name' else 'mongodb-observe-'+'a'*16,
                        workers,'foreign' if field=='source' else 'mongodb-gate-data',True if field=='age' else 60)

    def test_monitoring_query_requires_successful_vector_and_encodes_promql(self):
        s=scenarios.Scenario(ROOT);g=scenarios.Observability(s);g.state={'name':'mongodb-observe-'+'a'*16}
        valid={'status':'success','data':{'resultType':'vector','result':[{'value':[1,'3']}]}}
        with patch.object(s.lab,'run',return_value=json.dumps(valid)) as run:
            self.assertEqual(g.query('count(up{job="mongodb"} == 1)'),valid['data']['result'])
            self.assertIn('query=count%28up%7Bjob%3D%22mongodb%22%7D+%3D%3D+1%29',run.call_args.args[-1])
        for data in ({'status':'error'},valid|{'data':{'resultType':'scalar','result':[1,'3']}}):
            with patch.object(s.lab,'run',return_value=json.dumps(data)),self.assertRaises(ValueError):
                g.query('up')

    def test_monitoring_alerts_require_all_actual_firing_series_and_empty_resolved_state(self):
        s=scenarios.Scenario(ROOT);g=scenarios.Observability(s)
        row=lambda name:{'metric':{'alertname':name},'value':[1,'1']}
        for active,answers in [(True,[[row('A')],[row('A'),row('B')]]),(False,[[row('A')],[]])]:
            with patch.object(g,'query',side_effect=answers) as query,patch.object(scenarios.time,'sleep') as pause:
                g.wait_alerts(('A','B'),active)
                self.assertEqual(query.call_count,2);pause.assert_called_once_with(5)
        with patch.object(g,'query',return_value=[]),patch.object(scenarios.time,'monotonic',side_effect=[0,2]), \
                self.assertRaisesRegex(ValueError,'deadline'):
            g.wait_alerts(('A',),True,timeout=1)

    def test_monitoring_cleanup_validates_every_record_before_deleting_first_resource(self):
        for drift in ('uid','document','unrecorded'):
            with self.subTest(drift=drift),tempfile.TemporaryDirectory() as tmp:
                s=scenarios.Scenario(ROOT);s.lab.directory=Path(tmp);g=scenarios.Observability(s)
                name='mongodb-observe-'+'a'*16
                docs=scenarios.observability_objects(name,self.monitoring_workers(),s.fixture.NAME,60)
                g.state={'name':name,'cluster_uid':'core','workers':self.monitoring_workers(),'backup_age':60,
                    'workload':s.prefix,'client_uid':None,'resources':[{'document':o,'uid':str(i)} for i,o in enumerate(docs)]}
                if drift=='document':g.state['resources'][-1]['document']['metadata']['name']='foreign'
                if drift=='unrecorded':g.state['resources'][-1]['uid']=None
                g.save()
                def get(kind,name,namespace):
                    doc=next((o for o in docs if o['kind']==kind and o['metadata']['name']==name),None)
                    index=docs.index(doc)
                    uid='changed' if drift=='uid' and index==len(docs)-1 else str(index)
                    return doc|{'metadata':doc['metadata']|{'uid':uid}}
                with patch.object(s.lab,'verify',return_value={'cluster_uid':'core'}), \
                        patch.object(s.lab,'get',side_effect=get),patch.object(s.lab,'delete_uid') as delete:
                    with self.assertRaises(ValueError):g.cleanup()
                    delete.assert_not_called();self.assertTrue(g.path.exists())

    def test_monitoring_boundary_allows_generation_advance_but_retains_actual_source_spec(self):
        s=scenarios.Scenario(ROOT);g=scenarios.Observability(s)
        value={'pods':[],'pvc':[{'uid':'volume'}],'secrets':[{'data_sha256':'password'}],
               'psmdb':[{'uid':'source','generation':4,'spec':{'backup':False}}]}
        with patch.object(s,'snapshot',side_effect=lambda:copy.deepcopy(value)):
            before=g.boundary();value['psmdb'][0]['generation']=9
            self.assertEqual(before,g.boundary())
            value['psmdb'][0]['spec']['backup']=True
            self.assertNotEqual(before,g.boundary())

    def test_monitoring_fixed_modes_preserve_focused_marker_and_actual_cli_dispatch(self):
        for suffix in ('','-scrape','-lag','-capacity','-backup','-recover'):
            mode='observability'+suffix;marker='MONGODB_'+mode.upper().replace('-','_')+'_PASS'
            script='source tests/helpers/mongodb-runner.sh\nmongodb_launch(){ printf "%s|%s\\n" "$1" "$2"; }\nmongodb_dispatch --mongodb-'+mode
            r=scenarios.subprocess.run(['bash','-euc',script],cwd=ROOT,capture_output=True,text=True,check=True)
            self.assertEqual(r.stdout.strip(),mode+'|'+marker)
            with patch.object(scenarios.sys,'argv',['mongodb-scenarios.py',mode]), \
                    patch.object(scenarios,'Scenario'),patch.object(scenarios,'Observability') as gate, \
                    patch.object(scenarios.signal,'signal'):
                self.assertEqual(scenarios.main(),0)
                if suffix=='-recover':gate.return_value.cleanup.assert_called_once()
                else:gate.return_value.run.assert_called_once_with(focused=suffix.removeprefix('-') or None)

    def test_performance_rechecks_all_backup_dependencies_and_refuses_unsafe_reuse(self):
        for drift in (None, 'uid', 'owner', 'never', 'receipt', 'target', 'config', 'objects', 'symlink'):
            with self.subTest(drift=drift), tempfile.TemporaryDirectory() as tmp:
                s=scenarios.Scenario(ROOT);s.lab.directory=Path(tmp);g=scenarios.Performance(s)
                state={'resources':[{'kind':'pod','name':'backup-client','uid':'recorded'}], 'backup':{'sha256':'original'}}
                if drift=='receipt':state['client_recovery']={}
                if drift=='target':state['target']={}
                if drift=='config':state['original_config']={}
                for kind in ('logical','physical','pitr'):
                    path=Path(tmp)/('mongodb-backup-'+kind+'.json');path.write_text(json.dumps(state))
                    if drift=='symlink':
                        original=path.with_suffix('.original');path.rename(original);path.symlink_to(original)
                gate=MagicMock();gate.owned.return_value=drift!='owner'
                gate.verify_objects.return_value={'sha256':'changed' if drift=='objects' else 'original'}
                backups=SimpleNamespace(BackupGate=MagicMock(return_value=gate))
                pod={'metadata':{'uid':'changed' if drift=='uid' else 'recorded'},
                     'spec':{'restartPolicy':'Never' if drift=='never' else 'Always'}}
                with patch.object(scenarios,'load',return_value=backups),patch.object(s.lab,'get',return_value=pod), \
                        patch.object(s.lab,'run') as run:
                    if drift:
                        with self.assertRaises(ValueError):g.backup_dependencies()
                        if drift!='objects':gate.verify_objects.assert_not_called();run.assert_not_called()
                    else:
                        g.backup_dependencies()
                        self.assertEqual(backups.BackupGate.call_args_list[-1].kwargs,{'pitr':True})
                        self.assertEqual(gate.mc.call_count,3);self.assertEqual(gate.verify_objects.call_count,3)
                        self.assertEqual(run.call_count,3);self.assertEqual(gate.request_for_cleanup.call_count,3)

    def test_worker_reboot_waits_for_owned_registry_master_and_http_health(self):
        s=scenarios.Scenario(ROOT);g=scenarios.WorkerRestart(s);g.state={'address':s.fixture.NODES[3]}
        row={'node':g.state['address'],'socket':'owned.sock'}
        for master,endpoint in ((0,True),(1,True),(0,False)):
            relay=SimpleNamespace(journal=lambda:[row],SSH=['ssh'],
                execute=lambda *a,**kw:SimpleNamespace(returncode=master),probe=lambda node:endpoint)
            with patch.object(scenarios,'load',return_value=relay):
                self.assertEqual(g.registry_ready(),master==0 and endpoint)

    def test_fault_performance_retains_client_error_before_recovery_can_remove_it(self):
        s=scenarios.Scenario(ROOT);g=scenarios.Performance(s)
        message='MONGODB_CLIENT_ERROR '+json.dumps({'stage':'performance','name':'MongoServerError','code':13})+'\n'
        original=scenarios.subprocess.Popen
        code="import sys;sys.stdin.read();print('MONGODB_PERFORMANCE_WORKER_READY',flush=True);sys.stdout.write("+repr(message)+");sys.stderr.write('private stderr');sys.exit(1)"
        with patch.object(scenarios.subprocess,'Popen',side_effect=lambda *a,**kw:original([scenarios.sys.executable,'-c',code],**kw)), \
                patch.object(g.restart,'start'),patch.object(g.restart,'recover'),patch.object(s,'save_client_failure') as save:
            with self.assertRaisesRegex(ValueError,'stage=performance error=MongoServerError code=13'):
                g.phase({'fault':True,'seconds':180,'concurrency':4,'phase':'node-loss'})
            self.assertEqual(save.call_args.args[0].stdout,'MONGODB_PERFORMANCE_WORKER_READY\n'+message)
            self.assertEqual(save.call_args.args[0].stderr,'private stderr')

    def test_fault_performance_drains_large_results_while_waiting_for_node_recovery(self):
        s=scenarios.Scenario(ROOT);g=scenarios.Performance(s);original=scenarios.subprocess.Popen
        code="import sys;sys.stdin.read();print('MONGODB_PERFORMANCE_WORKER_READY',flush=True);sys.stdout.write('x'*2097152);sys.stderr.write('y'*2097152)"
        def recovered():
            # The workload finishes before node/CSI recovery. Neither stream may
            # require the main thread to read a pipe for the child to exit.
            self.assertEqual(s.worker.wait(timeout=3),0)
        result={'phase':'node-loss','workers':[]}
        with patch.object(scenarios.subprocess,'Popen',side_effect=lambda *a,**kw:original([scenarios.sys.executable,'-c',code],**kw)), \
                patch.object(g.restart,'start'),patch.object(g.restart,'recover',side_effect=recovered), \
                patch.object(s,'js'),patch.object(scenarios,'performance_result',return_value=result) as parse, \
                patch.object(s,'save_client_failure') as save:
            self.assertEqual(g.phase({'fault':True,'seconds':180}),result)
            self.assertEqual(parse.call_args.args[0],'MONGODB_PERFORMANCE_WORKER_READY\n'+'x'*2097152)
            save.assert_not_called();self.assertIsNone(s.worker)

    def test_soak_performance_uses_injected_faults_and_duration_based_process_timeout(self):
        s=scenarios.Scenario(ROOT);g=scenarios.Performance(s);original=scenarios.subprocess.Popen
        code="import sys;sys.stdin.read();print('MONGODB_PERFORMANCE_WORKER_READY',flush=True)"
        from unittest.mock import Mock
        calls=[]
        def spawn(*a,**kw):
            child=original([scenarios.sys.executable,'-c',code],**kw)
            child.communicate=Mock(wraps=child.communicate);calls.append(child);return child
        injection=Mock()
        with patch.object(scenarios.subprocess,'Popen',side_effect=spawn),patch.object(g.restart,'start') as start, \
                patch.object(g.restart,'recover') as recover,patch.object(s,'js') as ledger, \
                patch.object(scenarios,'performance_result',return_value={'phase':'soak','workers':[]}),redirect_stdout(StringIO()):
            g.phase({'fault':True,'seconds':1800},inject=injection)
        injection.assert_called_once();start.assert_not_called();recover.assert_not_called()
        calls[0].communicate.assert_called_once_with(timeout=1950)
        self.assertEqual(ledger.call_args.args[1],'MONGODB_PERFORMANCE_LEDGER_PASS');self.assertIsNone(s.worker)

    def test_fault_phase_requires_ca_store_marker_and_retains_opted_in_memory_diagnostics(self):
        from unittest.mock import Mock
        marker = 'MONGODB_CLIENT_CA_STORE_PASS trust=fixture-only'
        profile = 'MONGODB_CLIENT_MEMORY ' + json.dumps({'phase': 'contract', 'memory': {'rss': 1}})
        for count in (0, 1, 2):
            with self.subTest(markers=count):
                s = scenarios.Scenario(ROOT);g = scenarios.Performance(s)
                original = scenarios.subprocess.Popen
                lines = ['MONGODB_PERFORMANCE_WORKER_READY', profile] + [marker] * count
                code = 'import sys;sys.stdin.read();print(' + repr('\n'.join(lines)) + ',flush=True)'
                result = {'phase': 'contract', 'workers': []}
                with patch.object(scenarios.subprocess, 'Popen', side_effect=lambda *a, **kw: original(
                        [scenarios.sys.executable, '-c', code], **kw)), patch.object(s, 'js'), \
                        patch.object(scenarios, 'performance_result', return_value=result) as parse, \
                        redirect_stdout(StringIO()) as output:
                    settings = {'fault': True, 'seconds': 1, 'shared_ca': True, 'memory_profile': True}
                    if count != 1:
                        with self.assertRaisesRegex(ValueError, 'CA store verification'):
                            g.phase(settings, inject=Mock())
                        parse.assert_not_called()
                    else:
                        self.assertEqual(g.phase(settings, inject=Mock()), result)
                        self.assertIn(marker, output.getvalue().splitlines())
                        self.assertIn(profile, output.getvalue().splitlines())

    def test_node_only_performance_entry_does_not_sign_off_complete_case(self):
        script='source tests/helpers/mongodb-runner.sh\nmongodb_launch(){ printf "%s|%s\\n" "$1" "$2"; }\nmongodb_dispatch --mongodb-performance-node'
        result=scenarios.subprocess.run(['bash','-euc',script],cwd=ROOT,capture_output=True,text=True,check=True)
        self.assertEqual(result.stdout.strip(),'performance-node|MONGODB_PERFORMANCE_NODE_PASS')
        with patch.object(scenarios.sys,'argv',['mongodb-scenarios.py','performance-node']),patch.object(scenarios,'Scenario'), \
                patch.object(scenarios,'Performance') as gate,patch.object(scenarios.signal,'signal'):
            self.assertEqual(scenarios.main(),0);gate.return_value.run.assert_called_once_with(node_only=True)

    def test_performance_client_program_checks_real_async_control_flow_mix_and_data(self):
        import shutil
        node=shutil.which('node');self.assertIsNotNone(node,'Node is mandatory for MongoDB client programs')
        prelude="""let clock=0;Date.now=()=>clock;const print=console.log,prefix='mongodb_gate_contract';
class Mongo{getDB(){return {auth:async()=>({ok:1}),runCommand:async command=>{
 clock+=100;
 if(command.find)return {ok:1,cursor:{firstBatch:[{_id:command.filter._id,payload:'x'.repeat(1024)}]}};
 return {ok:1};
}}}}
"""
        for concurrency in (1,4,16):
            for drift in (None,'wrong-payload','auth','write-failure'):
                with self.subTest(concurrency=concurrency,drift=drift):
                    fixture=prelude
                    if drift=='wrong-payload':fixture=fixture.replace("payload:'x'.repeat(1024)","payload:'corrupt'")
                    if drift=='auth':fixture=fixture.replace('auth:async()=>({ok:1})','auth:async()=>({ok:0})')
                    if drift=='write-failure':fixture=fixture.replace('return {ok:1};','return {ok:1,writeErrors:[{code:11000}]};')
                    settings={'phase':'contract','concurrency':concurrency,'seconds':30,'fault':False,
                              'memory_profile': concurrency == 1}
                    code=fixture+scenarios.ASSERT+'const settings='+json.dumps(settings)+';\n'
                    code+='async function main(){'+scenarios.PERFORMANCE_WORKER+'}\nmain().catch(()=>process.exit(1));'
                    result=scenarios.subprocess.run([node,'-e',code],capture_output=True,text=True,timeout=15)
                    if drift:
                        self.assertNotEqual(result.returncode,0)
                        self.assertNotIn('MONGODB_PERFORMANCE_WORKER_PASS',result.stdout.splitlines())
                    else:
                        self.assertEqual(result.returncode,0,result.stderr)
                        actual=scenarios.performance_result(result.stdout,settings)
                        self.assertEqual(actual['reads'],actual['writes']*4)
                        self.assertEqual(actual['business_errors'],0)
                        self.assertEqual(len(actual['workers']),concurrency)
                        profiles = [json.loads(line.split(' ', 1)[1]) for line in result.stdout.splitlines()
                                    if line.startswith('MONGODB_CLIENT_MEMORY ')]
                        if settings['memory_profile']:
                            self.assertGreaterEqual(len(profiles), 3)
                            self.assertLessEqual(len(profiles), 5)
                            self.assertEqual(profiles[0]['samples'], 0)
                            self.assertEqual(profiles[-1]['samples'], actual['operations'])
                            self.assertEqual(profiles[-1]['retry_errors'], actual['retry_errors'])
                            for row in profiles:
                                self.assertEqual(set(row), {'phase', 'elapsed_ms', 'samples', 'retry_errors',
                                    'memory', 'heap_size_limit', 'node_version', 'constrained_memory'})
                                self.assertEqual(row['phase'], settings['phase'])
                                self.assertEqual(set(row['memory']), {'rss', 'heapTotal', 'heapUsed', 'external', 'arrayBuffers'})
                                self.assertTrue(all(type(v) is int and v >= 0 for v in row['memory'].values()))
                                self.assertGreater(row['heap_size_limit'], 0)
                        else:
                            self.assertEqual(profiles, [])

    def test_stability_shared_ca_keeps_exact_trust_and_rejects_wrong_ca_and_hostname(self):
        s = scenarios.Scenario(ROOT)
        cert = s.fixture.credentials()['items'][0]['stringData']
        wrong_ca = s.fixture.credentials()['items'][0]['stringData']['ca.crt']
        uri = 'mongodb://fixture.invalid/admin?tls=true&tlsCAFile=/tls/ca.crt&serverSelectionTimeoutMS=10000'
        prelude = '''const tls=require('tls'),fs=require('fs');
const fixture=FIXTURE,wrongCA=WRONG_CA,originalRead=fs.readFileSync;
fs.readFileSync=(path,...args)=>path==='/tls/ca.crt'?fixture['ca.crt']:originalRead(path,...args);
process.env.MONGODB_URI=URI;
const print=console.log,prefix='mongodb_gate_contract';let clock=0;Date.now=()=>clock;
const server=tls.createServer({key:fixture['tls.key'],cert:fixture['tls.crt']},socket=>socket.end());
server.on('tlsClientError',()=>{});
async function probe(options,expected){
 await new Promise((resolve,reject)=>{
  const socket=tls.connect({host:'127.0.0.1',port:server.address().port,servername:'localhost',...options});
  socket.once('secureConnect',()=>{socket.end();expected?reject(Error('unexpected TLS acceptance')):resolve();});
  socket.once('error',e=>{socket.destroy();e.code===expected?resolve():reject(e);});
 });
}
class Mongo{
 constructor(uri){assert.eq(process.env.MONGODB_URI.replace('&tlsCAFile=/tls/ca.crt',''),uri);}
 getDB(){return {auth:async()=>{
  const roots=tls.getCACertificates('default'),X509=require('crypto').X509Certificate;
  assert.eq(1,roots.length);assert.eq(new X509(fixture['ca.crt']).fingerprint256,new X509(roots[0]).fingerprint256);
  await probe({},null);
  await probe({ca:wrongCA},'UNABLE_TO_VERIFY_LEAF_SIGNATURE');
  await probe({servername:'wrong.invalid'},'ERR_TLS_CERT_ALTNAME_INVALID');
  print('MONGODB_CLIENT_CA_REFUSAL_CONTRACT_PASS');return {ok:1};
 },runCommand:async command=>{clock+=100;
  return command.find?{ok:1,cursor:{firstBatch:[{_id:command.filter._id,payload:'x'.repeat(1024)}]}}:{ok:1};
 }};}
}
'''
        prelude = prelude.replace('FIXTURE', json.dumps(cert)).replace('WRONG_CA', json.dumps(wrong_ca)).replace('URI;', json.dumps(uri) + ';')
        settings = {'phase': 'stability-contract', 'concurrency': 1, 'seconds': 1, 'fault': False, 'shared_ca': True}
        code = prelude + scenarios.ASSERT + 'const settings=' + json.dumps(settings) + ';\n'
        code += 'async function worker(){' + scenarios.PERFORMANCE_WORKER + '}\n'
        code += "server.listen(0,'127.0.0.1',async()=>{try{await worker();}catch(e){console.error(e.message);process.exitCode=1;}finally{server.close();}});"
        result = scenarios.subprocess.run([scenarios.shutil.which('node')], input=code,
                                           capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('MONGODB_CLIENT_CA_REFUSAL_CONTRACT_PASS', result.stdout.splitlines())
        self.assertIn('MONGODB_CLIENT_CA_STORE_PASS trust=fixture-only', result.stdout.splitlines())
        self.assertGreater(scenarios.performance_result(result.stdout, settings)['writes'], 0)
        for changed in (uri.replace('&tlsCAFile=/tls/ca.crt', ''),
                        uri.replace('/tls/ca.crt', '/other/ca.crt'), uri + '&tlsCAFile=/other/ca.crt'):
            with self.subTest(uri=changed):
                invalid = code.replace(json.dumps(uri), json.dumps(changed), 1)
                result = scenarios.subprocess.run([scenarios.shutil.which('node')], input=invalid,
                                                  capture_output=True, text=True, timeout=15)
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn('MONGODB_CLIENT_CA_STORE_PASS trust=fixture-only', result.stdout.splitlines())
                self.assertNotIn('MONGODB_PERFORMANCE_WORKER_PASS', result.stdout.splitlines())

    def test_sharded_fault_primary_selection_retries_are_scoped_bounded_and_preserve_assertions(self):
        import shutil
        node=shutil.which('node');self.assertIsNotNone(node)
        for operation,fault,sharded,code,permanent,corrupt in (
                ('read',True,True,133,False,False),('write',True,True,133,False,False),
                ('read',False,True,133,False,False),('read',True,False,133,False,False),
                ('read',True,True,13,False,False),('write',True,True,11000,False,False),
                ('read',True,True,133,True,False),('read',True,True,133,False,True)):
            with self.subTest(operation=operation,fault=fault,sharded=sharded,code=code,
                              permanent=permanent,corrupt=corrupt):
                settings={'phase':'contract','concurrency':1,'seconds':1,'fault':fault,'sharded':sharded}
                fixture="""let clock=0,reads=0,failures=0;Date.now=()=>clock;
global.setTimeout=(done,ms)=>{clock+=ms;done();};const print=console.log,prefix='mongodb_gate_contract';
class Mongo{getDB(){return {auth:async()=>({ok:1}),runCommand:async command=>{
 clock+=100;if(command.find)reads++;
 if(reads>1 && ("""+json.dumps(permanent)+"""||failures===0) &&
    ("""+json.dumps(operation)+"""==='read'?Boolean(command.find):Boolean(command.update))){
  failures++;return command.find?{ok:0,code:"""+str(code)+"""}:{ok:1,writeConcernError:{code:"""+str(code)+"""}};
 }
 if(command.find)return {ok:1,cursor:{firstBatch:[{_id:command.filter._id,
  payload:"""+json.dumps(corrupt)+"""&&failures?'corrupt':'x'.repeat(1024)}]}};
 return {ok:1};
}}}}
"""
                program=fixture+scenarios.ASSERT+'const settings='+json.dumps(settings)+';\n'
                program+='async function main(){'+scenarios.PERFORMANCE_WORKER+'}\nmain().catch(e=>{console.error(e.message);process.exit(1);});'
                result=scenarios.subprocess.run([node,'-e',program],capture_output=True,text=True,timeout=15)
                accepted=fault and sharded and code==133 and not permanent and not corrupt
                if accepted:
                    self.assertEqual(result.returncode,0,result.stderr)
                    actual=scenarios.performance_result(result.stdout,settings)
                    self.assertEqual(actual['retry_errors'],1)
                    self.assertGreater(actual['writes'],0)
                    self.assertEqual(actual['reads'],actual['writes']*4)
                else:
                    self.assertNotEqual(result.returncode,0)
                    self.assertNotIn('MONGODB_PERFORMANCE_WORKER_PASS',result.stdout.splitlines())
                    if permanent:self.assertIn('recovery deadline exceeded',result.stderr)

    def test_performance_result_refuses_missing_samples_duplicates_drift_and_hidden_errors(self):
        settings={'phase':'steady-1','concurrency':1,'seconds':30,'fault':False}
        original=settings|{'elapsed_ms':30000,'workers':[{'worker':0,'acknowledged':1}],
            'latency_ms':[1,2,3,4,100],'retry_errors':0}
        original.pop('fault')
        for drift in (None,'samples','negative','nan','duplicate-worker','missing-worker','phase','elapsed','retry','missing-terminal','duplicate-result'):
            with self.subTest(drift=drift):
                row=copy.deepcopy(original)
                if drift=='samples':row['latency_ms'].pop()
                if drift=='negative':row['latency_ms'][0]=-1
                if drift=='nan':row['latency_ms'][0]=float('nan')
                if drift=='duplicate-worker':row['workers']*=2
                if drift=='missing-worker':row['workers']=[]
                if drift=='phase':row['phase']='stale'
                if drift=='elapsed':row['elapsed_ms']=29999
                if drift=='retry':row['retry_errors']=1
                line='MONGODB_PERFORMANCE_RESULT '+json.dumps(row)+'\n'
                output=line+('' if drift=='missing-terminal' else 'MONGODB_PERFORMANCE_WORKER_PASS\n')
                if drift=='duplicate-result':output+=line
                if drift:
                    with self.assertRaises(ValueError):scenarios.performance_result(output,settings)
                else:
                    result=scenarios.performance_result(output,settings)
                    self.assertEqual(result['latency_ms'],{'50':3,'95':100,'99':100})

    def test_performance_modes_bind_fixed_runner_without_signing_off_the_focused_client(self):
        script='''source tests/helpers/mongodb-runner.sh
mongodb_launch(){ printf '%s|%s\\n' "$1" "$2"; }
mongodb_dispatch --mongodb-performance
mongodb_dispatch --mongodb-performance-client
mongodb_dispatch --mongodb-performance-recover
'''
        result=scenarios.subprocess.run(['bash','-euc',script],cwd=ROOT,capture_output=True,text=True,check=True)
        self.assertEqual(result.stdout.splitlines(),['performance|MONGODB_PERFORMANCE_PASS',
            'performance-client|MONGODB_PERFORMANCE_CLIENT_PASS','performance-recover|MONGODB_PERFORMANCE_RECOVER_PASS'])
        for mode in ('performance','performance-client','performance-recover'):
            with patch.object(scenarios.sys,'argv',['mongodb-scenarios.py',mode]),patch.object(scenarios,'Scenario'), \
                    patch.object(scenarios,'Performance') as gate,patch.object(scenarios.signal,'signal'):
                self.assertEqual(scenarios.main(),0)
                if mode=='performance-recover':gate.return_value.recover.assert_called_once_with(interrupted=True)
                else:gate.return_value.run.assert_called_once_with(focused=mode=='performance-client')

    def test_worker_reboot_refuses_changed_lease_node_client_and_scheduling_before_ssh(self):
        for drift in (None,'address','uid','other-node','client','flags','boot','core'):
            with self.subTest(drift=drift),tempfile.TemporaryDirectory() as tmp:
                s=scenarios.Scenario(ROOT);s.lab.directory=Path(tmp);s.lab.owner_path=Path(tmp,'owner.json')
                s.lab.owner_path.write_text('{}');s.client_uid='client';s.prefix='mongodb_gate_0123456789abcdef'
                g=scenarios.WorkerRestart(s);node={'metadata':{'name':'mongodb-node-212','uid':'worker'},
                    'spec':{},'status':{'addresses':[{'type':'InternalIP','address':'192.168.122.212'}]}}
                g.state={'node':'mongodb-node-212','address':'192.168.122.212','node_uid':'worker',
                    'node_uids':{'mongodb-node-212':'worker'},'node_spec':{'taints':[]},'cluster_uid':'core',
                    'client_uid':'client','run_id':s.prefix,'boot_id':'01234567-0123-0123-0123-0123456789ab'}
                if drift=='address':g.state['address']='192.168.122.1'
                if drift=='uid':node['metadata']['uid']='foreign'
                if drift=='other-node':g.state['node_uids']['extra']='foreign'
                if drift=='client':g.state['client_uid']='foreign'
                if drift=='flags':node['spec']['unschedulable']=True
                if drift=='boot':g.state['boot_id']='invalid'
                with patch.object(s.fixture,'core_identity',return_value='changed' if drift=='core' else 'core'), \
                        patch.object(s.lab,'get',side_effect=lambda k,*a:{'items':[node]} if k=='nodes' else {'items':[]} if k=='namespace' else node), \
                        patch.object(scenarios.subprocess,'run') as ssh:
                    if drift:
                        with self.assertRaises(ValueError):g.verify()
                    else:
                        self.assertEqual(g.verify(),node)
                        node['spec']['taints']=[{'key':'node.kubernetes.io/unreachable','effect':'NoExecute'}]
                        self.assertEqual(g.verify(),node)
                    ssh.assert_not_called()

    def test_performance_resources_bind_database_client_and_volume_counters_to_live_uids(self):
        s=scenarios.Scenario(ROOT);s.client_uid='client'
        members=[{'metadata':{'name':'member-'+str(i),'uid':str(i)},'spec':{'nodeName':'worker'}} for i in range(3)]
        client={'metadata':{'name':s.client,'uid':'client'},'spec':{'nodeName':'worker'}}
        summary={'pods':[{'podRef':{'name':p['metadata']['name'],'uid':p['metadata']['uid'],'namespace':s.ns},
            'containers':[{'cpu':{'usageCoreNanoSeconds':100},'memory':{'workingSetBytes':200}}],
            'volume':[{'pvcRef':{'name':'owned'},'usedBytes':300}] if p in members else [],
            'network':{'rxBytes':10,'txBytes':20}} for p in members+[client]]}
        for drift in (None,'client-uid','stale-pod','missing-client','missing-volume'):
            with self.subTest(drift=drift):
                obj=copy.deepcopy(summary);current=copy.deepcopy(client)
                if drift=='client-uid':current['metadata']['uid']='changed'
                if drift=='stale-pod':obj['pods'][0]['podRef']['uid']='changed'
                if drift=='missing-client':obj['pods'].pop()
                if drift=='missing-volume':obj['pods'][0]['volume']=[]
                with patch.object(s,'maintenance_members',return_value=members),patch.object(s.lab,'get',return_value=current), \
                        patch.object(s.lab,'run',return_value=json.dumps(obj)):
                    if drift:
                        with self.assertRaises(ValueError):scenarios.Performance(s).resources()
                    else:
                        result=scenarios.Performance(s).resources();self.assertEqual(len(result),4)
                        self.assertEqual(result[s.client]['memory_bytes'],200)


    def test_performance_recovery_checks_source_and_client_before_any_data_removal(self):
        for drift in (None,'client','source','core'):
            with self.subTest(drift=drift),tempfile.TemporaryDirectory() as tmp:
                s=scenarios.Scenario(ROOT);s.lab.directory=Path(tmp);s.client_uid='client'
                g=scenarios.Performance(s)
                state={'run_id':s.prefix,'client_uid':'client','cluster_uid':'core','before':{'pvc':['original']},'data_started':True}
                s.fixture.write_private(g.path,state)
                pod={'metadata':{'uid':'foreign' if drift=='client' else 'client','labels':{'kubeauto.io/test-run':s.prefix}}}
                with patch.object(s.lab,'get',return_value=pod),patch.object(s,'stop_worker'), \
                        patch.object(g.restart,'recover'),patch.object(s.lab,'verify',return_value={'cluster_uid':'changed' if drift=='core' else 'core'}), \
                        patch.object(s,'snapshot',side_effect=lambda:{'pods':[],'pvc':['changed' if drift=='source' else 'original']}), \
                        patch.object(s,'health'),patch.object(s,'clean_workload') as clean,patch.object(s,'delete_client') as delete:
                    if drift:
                        with self.assertRaises(ValueError):g.recover()
                        clean.assert_not_called();delete.assert_not_called();self.assertTrue(g.path.exists())
                    else:
                        g.recover();clean.assert_called_once();delete.assert_called_once();self.assertFalse(g.path.exists())

    def test_ownership_resource_modes_preserve_full_and_focused_acceptance_boundaries(self):
        script='''source tests/helpers/mongodb-runner.sh
mongodb_launch(){ printf '%s|%s\\n' "$1" "$2"; }
mongodb_dispatch --mongodb-ownership
mongodb_dispatch --mongodb-ownership-resources
mongodb_dispatch --mongodb-ownership-resources-recover
'''
        result=scenarios.subprocess.run(['bash','-euc',script],cwd=ROOT,capture_output=True,text=True,check=True)
        self.assertEqual(result.stdout.splitlines(),['ownership|MONGODB_OWNERSHIP_PASS',
            'ownership-resources|MONGODB_OWNERSHIP_RESOURCES_PASS',
            'ownership-resources-recover|MONGODB_OWNERSHIP_RESOURCES_RECOVER_PASS'])
        for mode in ('ownership','ownership-resources','ownership-resources-recover'):
            with patch.object(scenarios.sys,'argv',['mongodb-scenarios.py',mode]), \
                    patch.object(scenarios,'Scenario'),patch.object(scenarios,'OwnershipResources') as resources, \
                    patch.object(scenarios,'OwnershipPVC') as pvc,patch.object(scenarios.signal,'signal'):
                self.assertEqual(scenarios.main(),0)
                (resources.return_value.cleanup if mode.endswith('-recover') else resources.return_value.run).assert_called_once()
                if mode=='ownership':pvc.return_value.run.assert_called_once()
                else:pvc.assert_not_called()

    def ownership_resources(self,tmp):
        scenario=scenarios.Scenario(ROOT);scenario.lab.directory=Path(tmp,'clusters/mongodb-gate')
        scenario.base=Path(tmp);scenario.lab.directory.mkdir(parents=True)
        scenario.lab.directory.joinpath('config.yml').write_text('mongodb_install: yes\nmongodb_namespace: mongodb-gate\n')
        scenario.lab.directory.joinpath('kubectl.kubeconfig').write_text('test-kubeconfig')
        gate=scenarios.OwnershipResources(scenario);run='0123456789abcdef';name='mongodb-owner-'+run
        gate.state={'run_id':run,'name':name,'cluster_uid':'core','before':{'source':'unchanged'},
            'labels':{'app.kubernetes.io/managed-by':'independent-resource-owner','kubeauto.io/test-run':run},'resources':[]}
        gate.save()
        return scenario,gate

    def test_ownership_resources_validate_all_endpoints_and_namespace_contents_before_any_deletion(self):
        for drift in (None,'last-uid','lost-receipt','labels','endpoint','name','namespace','duplicate','unknown-object','finalizer','unexpected-cr','shadow'):
            with self.subTest(drift=drift),tempfile.TemporaryDirectory() as tmp:
                s,g=self.ownership_resources(tmp);objects={};root=g.state['name'];labels=g.state['labels']
                for kind,name,ns in (('namespace',root,''),('service',root+'-metrics',s.ns)):
                    g.state['resources'].append({'kind':kind,'name':name,'namespace':ns,'uid':kind,
                        'endpoint':g.endpoint(kind,name,ns)})
                    objects[kind,name]={'kind':'Namespace' if kind=='namespace' else 'Service',
                        'metadata':{'name':name,'uid':kind,'labels':labels|({'kubernetes.io/metadata.name':root} if kind=='namespace' else {})}}
                if drift=='last-uid':objects['service',root+'-metrics']['metadata']['uid']='foreign'
                if drift=='lost-receipt':g.state['resources'][-1]['uid']=None
                if drift=='labels':objects['service',root+'-metrics']['metadata']['labels']={}
                if drift=='endpoint':g.state['resources'][-1]['endpoint']='/api/v1/namespaces/foreign'
                if drift=='name':g.state['resources'][-1]['name']='foreign'
                if drift=='namespace':g.state['resources'][-1]['namespace']='foreign'
                if drift=='duplicate':g.state['resources'].append(copy.deepcopy(g.state['resources'][-1]))
                if drift=='finalizer':objects['service',root+'-metrics']['metadata']['finalizers']=['independent/controller']
                if drift=='unexpected-cr':objects['psmdb',root]={'metadata':{'uid':'unexpected'}}
                if drift=='shadow':g.shadow.write_text('unrecorded')
                foreign=[{'kind':'Secret','metadata':{'name':'foreign','uid':'foreign'}}] if drift=='unknown-object' else []
                g.save()
                def delete(endpoint,uid):
                    for key,obj in list(objects.items()):
                        if obj['metadata']['uid']==uid:objects.pop(key)
                with patch.object(s.lab,'get',side_effect=lambda k,n='',*a,**kw:objects.get((k,n))), \
                        patch.object(s.lab,'verify',return_value={'cluster_uid':'core'}), \
                        patch.object(s.lab,'namespace_objects',return_value=foreign), \
                        patch.object(s.lab,'delete_uid',side_effect=delete) as mutate,patch.object(s.lab,'run'), \
                        patch.object(s,'health'),patch.object(s,'snapshot',return_value={'source':'unchanged'}):
                    if drift:
                        with self.assertRaises(ValueError):g.cleanup()
                        mutate.assert_not_called();self.assertTrue(g.path.exists())
                    else:
                        g.cleanup();self.assertEqual(objects,{});self.assertFalse(g.path.exists())

    def test_ownership_resource_product_and_cleanup_refusals_restore_independent_scope_on_failure_or_signal(self):
        from contextlib import ExitStack,redirect_stdout
        from io import StringIO
        for failure in (None,'generic-rejection','interrupt','created-cr'):
            with self.subTest(failure=failure),tempfile.TemporaryDirectory() as tmp:
                s,g=self.ownership_resources(tmp);g.path.unlink();g.state=None;objects={};commands=[]
                run='0123456789abcdef';name='mongodb-owner-'+run
                crd='perconaservermongodbs.psmdb.percona.com'
                owner={'cluster_uid':'core','created_crds':[crd],'product_identity':{'crds':{crd:{'uid':'original'}}}}
                s.lab.owner_path = s.lab.directory / 'mongodb-lab-owner.json'
                s.fixture.write_private(s.lab.owner_path, owner)
                shadow_guard=SimpleNamespace(cleanup_product=lambda **kwargs:(_ for _ in ()).throw(ValueError('MongoDB cleanup CRD identity or schema changed')))
                def get(kind,objname='',ns='',**kwargs):
                    if kind=='psmdb' and objname==s.fixture.NAME:return {'spec':{'pause':False}}
                    return objects.get((kind,objname,ns))
                def command(args,**kwargs):
                    commands.append(args)
                    if args[:2]==['kubecli','new']:
                        g.directory.mkdir(parents=True);return SimpleNamespace(returncode=0)
                    if args[:2]==['kubecli','setup']:
                        self.assertEqual(args,['kubecli','setup','mongodb-ownership-gate','07','--tags','mongodb'])
                        config=scenarios.yaml.safe_load(g.directory.joinpath('config.yml').read_text())
                        if config['mongodb_namespace']==name:
                            message='database namespace must be explicitly labelled for kubeauto MongoDB ownership'
                        else:
                            kind='service' if ('service',name+'-metrics',s.ns) in objects else 'networkpolicy'
                            message='refusing to adopt a foreign MongoDB '+kind
                            if failure=='interrupt':raise SystemExit(143)
                            if failure=='generic-rejection':message='timeout'
                            if failure=='created-cr':objects['psmdb',name,s.ns]={'metadata':{'uid':'unexpected'}}
                        return SimpleNamespace(returncode=1,stdout=message,stderr='')
                    obj=json.loads(kwargs['input']);kind={'Namespace':'namespace','Service':'service','NetworkPolicy':'networkpolicy','PerconaServerMongoDB':'psmdb'}[obj['kind']]
                    meta=obj['metadata'];meta['uid']=kind+'-uid';ns=meta.get('namespace','')
                    if kind=='namespace':meta['labels']=meta['labels']|{'kubernetes.io/metadata.name':name}
                    objects[kind,meta['name'],ns]=obj
                    return SimpleNamespace(returncode=0,stdout=json.dumps(obj))
                def delete(endpoint,uid):
                    for key,obj in list(objects.items()):
                        if obj['metadata']['uid']==uid:objects.pop(key)
                with ExitStack() as stack,redirect_stdout(StringIO()) as output:
                    for ctx in (patch.object(scenarios.secrets,'token_hex',return_value=run),
                            patch.object(scenarios.subprocess,'run',side_effect=command),patch.object(s.lab,'get',side_effect=get),
                            patch.object(s.lab,'bind_product'),patch.object(s.lab,'verify',return_value=owner),
                            patch.object(s.lab,'namespace_objects',side_effect=lambda ns:[o for (k,n,namespace),o in objects.items() if namespace==ns]),
                            patch.object(s.lab,'cleanup_product',side_effect=ValueError('MongoDB cleanup CRD has surviving consumers in another namespace')),
                            patch.object(s.fixture,'Lab',return_value=shadow_guard),patch.object(s.lab,'delete_uid',side_effect=delete),
                            patch.object(s.lab,'run'),patch.object(s,'health'),patch.object(s,'snapshot',return_value={'source':'unchanged'})):
                        stack.enter_context(ctx)
                    if failure=='interrupt':
                        with self.assertRaises(SystemExit):g.run()
                    elif failure:
                        with self.assertRaises(ValueError):g.run()
                    else:
                        g.run();self.assertIn('MONGODB_OWNERSHIP_RESOURCES_PASS',output.getvalue().splitlines())
                        self.assertIn('MONGODB_OWNERSHIP_CRD_IDENTITY_PASS cleanup=refused live-crd=unchanged shadow-journal=true',output.getvalue().splitlines())
                    if failure=='created-cr':
                        self.assertTrue(g.path.exists());self.assertTrue(objects)
                    else:
                        self.assertFalse(g.path.exists());self.assertEqual(objects,{})
                    if failure:self.assertNotIn('MONGODB_OWNERSHIP_RESOURCES_PASS',output.getvalue().splitlines())

    def test_ownership_pvc_modes_bind_fixed_runner_and_actual_cli_branch(self):
        script='''source tests/helpers/mongodb-runner.sh
mongodb_launch(){ printf '%s|%s\\n' "$1" "$2"; }
mongodb_dispatch --mongodb-ownership-pvc
mongodb_dispatch --mongodb-ownership-pvc-recover
'''
        result=scenarios.subprocess.run(['bash','-euc',script],cwd=ROOT,capture_output=True,text=True,check=True)
        self.assertEqual(result.stdout.splitlines(),['ownership-pvc|MONGODB_OWNERSHIP_PVC_PASS',
            'ownership-pvc-recover|MONGODB_OWNERSHIP_PVC_RECOVER_PASS'])
        for mode in ('ownership-pvc','ownership-pvc-recover'):
            with patch.object(scenarios.sys,'argv',['mongodb-scenarios.py',mode]), \
                    patch.object(scenarios,'Scenario'),patch.object(scenarios,'OwnershipPVC') as gate, \
                    patch.object(scenarios.signal,'signal'):
                self.assertEqual(scenarios.main(),0)
                (gate.return_value.run if mode=='ownership-pvc' else gate.return_value.cleanup).assert_called_once()

    def ownership_gate(self,tmp):
        scenario=scenarios.Scenario(ROOT);scenario.lab.directory=Path(tmp,'clusters/mongodb-gate')
        scenario.base=Path(tmp);scenario.lab.directory.mkdir(parents=True)
        scenario.lab.directory.joinpath('config.yml').write_text('mongodb_install: yes\n')
        scenario.lab.directory.joinpath('kubectl.kubeconfig').write_text('test-kubeconfig')
        gate=scenarios.OwnershipPVC(scenario)
        run='0123456789abcdef';name='mongodb-pvc-'+run
        gate.state={'run_id':run,'name':name,'claim':'mongod-data-'+name+'-rs0-0',
            'pod':'mongodb-pvc-probe-'+run,'cluster_uid':'core','before':{'unchanged':True},
            'labels':{'app.kubernetes.io/managed-by':'independent-volume-owner','kubeauto.io/test-run':run},
            'records':{'pod':{'uid':'pod'},'pvc':{'uid':'claim','volume':'pv'}}}
        gate.save()
        return scenario,gate

    def test_ownership_pvc_checks_entire_foreign_graph_before_any_cleanup(self):
        for drift in (None,'pod','pvc','labels','binding','pv','cr','core'):
            with self.subTest(drift=drift),tempfile.TemporaryDirectory() as tmp:
                scenario,gate=self.ownership_gate(tmp);removed=set();labels=gate.state['labels']
                def get(kind,name='',*a,**kw):
                    if kind=='psmdb':return {'metadata':{'uid':'unexpected-root'}} if drift=='cr' else None
                    if kind in ('pod','pvc'):
                        if kind in removed:return None
                        obj={'metadata':{'uid':'foreign' if drift==kind else ('pod' if kind=='pod' else 'claim'),
                            'labels':{} if drift=='labels' else labels}}
                        if kind=='pvc':obj['spec']={'volumeName':'changed' if drift=='binding' else 'pv'}
                        return obj
                    if kind=='pv':
                        if 'pvc' in removed:return None
                        return {'spec':{'storageClassName':'mongodb-longhorn','persistentVolumeReclaimPolicy':'Delete',
                            'csi':{'driver':'driver.longhorn.io'},'claimRef':{'namespace':'mongodb-gate',
                                'name':gate.state['claim'],'uid':'foreign' if drift=='pv' else 'claim'}}}
                    raise AssertionError(kind)
                def delete(endpoint,uid):removed.add('pod' if '/pods/' in endpoint else 'pvc')
                with patch.object(scenario.lab,'get',side_effect=get), \
                        patch.object(scenario.lab,'verify',return_value={'cluster_uid':'foreign' if drift=='core' else 'core'}), \
                        patch.object(scenario.lab,'delete_uid',side_effect=delete) as deletion,patch.object(scenario.lab,'run'), \
                        patch.object(scenario,'health'),patch.object(scenario,'snapshot',return_value={'unchanged':True}):
                    if drift:
                        with self.assertRaises(ValueError):gate.cleanup()
                        deletion.assert_not_called();self.assertTrue(gate.path.exists())
                    else:
                        gate.cleanup();self.assertFalse(gate.path.exists());self.assertEqual(removed,{'pod','pvc'})

    def test_ownership_pvc_uses_real_config_entry_and_preserves_data_across_rejection(self):
        from contextlib import ExitStack,redirect_stdout
        from io import StringIO
        for failure in (None,'generic-rejection','unexpected-success','created-cr','interrupted'):
            with self.subTest(failure=failure),tempfile.TemporaryDirectory() as tmp:
                scenario,gate=self.ownership_gate(tmp);gate.path.unlink();gate.state=None
                objects={};commands=[];run='0123456789abcdef';created_cr=[False]
                def get(kind,name='',*a,**kw):
                    if kind=='psmdb':return {'metadata':{'uid':'unexpected'}} if created_cr[0] else None
                    return objects.get((kind,name))
                def command(args,**kwargs):
                    commands.append(args)
                    if args[:2]==['kubecli','new']:
                        gate.directory.mkdir(parents=True)
                        return SimpleNamespace(returncode=0)
                    if args[:2]==['kubecli','setup']:
                        self.assertTrue(('pvc','mongod-data-mongodb-pvc-'+run+'-rs0-0') in objects)
                        self.assertEqual(args,['kubecli','setup','mongodb-ownership-gate','07','--tags','mongodb'])
                        if failure=='interrupted':raise SystemExit(143)
                        created_cr[0]=failure=='created-cr'
                        return SimpleNamespace(returncode=0 if failure=='unexpected-success' else 1,
                            stdout='other failure' if failure=='generic-rejection' else
                            'refusing to adopt an existing or foreign MongoDB PVC',stderr='')
                    obj=json.loads(kwargs['input']);kind='pvc' if obj['kind']=='PersistentVolumeClaim' else 'pod'
                    self.assertEqual(obj['metadata']['labels']['app.kubernetes.io/managed-by'],'independent-volume-owner')
                    if kind=='pod':
                        self.assertFalse(obj['spec']['automountServiceAccountToken'])
                        self.assertNotIn('env',obj['spec']['containers'][0])
                        self.assertEqual(obj['spec']['volumes'][0]['persistentVolumeClaim']['claimName'],'mongod-data-mongodb-pvc-'+run+'-rs0-0')
                    obj['metadata']['uid']=kind+'-uid'
                    if kind=='pvc':obj['spec']['volumeName']='pv'
                    objects[(kind,obj['metadata']['name'])]=obj
                    if kind=='pvc':objects[('pv','pv')]={'spec':{'storageClassName':'mongodb-longhorn',
                        'persistentVolumeReclaimPolicy':'Delete','csi':{'driver':'driver.longhorn.io'},
                        'claimRef':{'namespace':'mongodb-gate','name':obj['metadata']['name'],'uid':'pvc-uid'}}}
                    return SimpleNamespace(returncode=0,stdout=json.dumps(obj))
                def delete(endpoint,uid):
                    kind='pod' if '/pods/' in endpoint else 'pvc';objects.pop((kind,endpoint.rsplit('/',1)[1]))
                    if kind=='pvc':objects.pop(('pv','pv'))
                with ExitStack() as stack,redirect_stdout(StringIO()) as output:
                    for context in (patch.object(scenarios.secrets,'token_hex',return_value=run),
                        patch.object(scenarios.subprocess,'run',side_effect=command),patch.object(scenario.lab,'get',side_effect=get),
                        patch.object(scenario.lab,'verify',return_value={'cluster_uid':'core'}),
                        patch.object(scenario.lab,'run',return_value=run),patch.object(scenario.lab,'delete_uid',side_effect=delete),
                        patch.object(scenario,'health'),patch.object(scenario,'snapshot',return_value={'unchanged':True})):
                        stack.enter_context(context)
                    if failure=='interrupted':
                        with self.assertRaises(SystemExit):gate.run()
                    elif failure:
                        with self.assertRaises(ValueError):gate.run()
                    else:gate.run()
                if failure=='created-cr':
                    self.assertTrue(objects);self.assertTrue(gate.path.exists())
                else:self.assertFalse(objects);self.assertFalse(gate.path.exists())
                self.assertEqual('MONGODB_OWNERSHIP_PVC_PASS' in output.getvalue().splitlines(),failure is None)

    def scale_gate(self, tmp):
        import yaml
        from tests.unit.test_mongodb_delivery import configuration
        scenario=scenarios.Scenario(ROOT);scenario.lab.directory=Path(tmp)
        gate=scenarios.ReplicaScale(scenario)
        original=configuration();original['mongodb_replsets']=[{'name':'rs0','size':3}]
        gate.config_path.write_text(yaml.safe_dump(original))
        gate.state={'prefix':'mongodb_gate_0123456789abcdef','client_uid':'client','cluster_uid':'core',
            'source_uid':'source','original_config':original,
            'expanded_config':original|{'mongodb_replsets':[{'name':'rs0','size':5}]},
            'before':{'pvc':[],'secrets':[],'psmdb':[]},'extra_claims':{}}
        gate.save()
        return scenario,gate

    def test_replica_scale_modes_reach_product_runner_and_exact_recovery_marker(self):
        script='''source tests/helpers/mongodb-runner.sh
mongodb_launch(){ printf '%s|%s\\n' "$1" "$2"; }
mongodb_dispatch --mongodb-replica-scale
mongodb_dispatch --mongodb-replica-scale-recover
'''
        result=scenarios.subprocess.run(['bash','-euc',script],cwd=ROOT,capture_output=True,text=True,check=True)
        self.assertEqual(result.stdout.splitlines(),['replica-scale|MONGODB_REPLICA_SCALE_PASS',
            'replica-scale-recover|MONGODB_REPLICA_SCALE_RECOVER_PASS'])
        with patch.object(scenarios.sys,'argv',['mongodb-scenarios.py','replica-scale-recover']), \
                patch.object(scenarios,'Scenario') as factory,patch.object(scenarios,'ReplicaScale') as scale, \
                patch.object(scenarios.signal,'signal'):
            self.assertEqual(scenarios.main(),0);scale.assert_called_once_with(factory.return_value)
            scale.return_value.recover.assert_called_once();factory.return_value.execute.assert_not_called()

    def test_replica_scale_product_rejects_identity_config_and_topology_drift_before_mutation(self):
        for drift in (None,'source','foreign','config','seven','sharding','fault'):
            with self.subTest(drift=drift),tempfile.TemporaryDirectory() as tmp:
                scenario,gate=self.scale_gate(tmp)
                cr={'metadata':{'uid':'changed' if drift=='source' else 'source',
                    'labels':{} if drift=='foreign' else scenario.product.LABELS},
                    'spec':{'replsets':[{'name':'rs0','size':7 if drift=='seven' else 3}],
                        'sharding':{'enabled':drift=='sharding'}}}
                if drift=='config':gate.config_path.write_text('unrelated: true\n')
                if drift=='fault':Path(tmp,'foreign-fault.json').write_text('{}')
                before=gate.config_path.read_bytes()
                with patch.object(scenario.lab,'get',return_value=cr), \
                        patch.object(scenario.lab,'verify',return_value={'cluster_uid':'core'}), \
                        patch.object(scenarios.subprocess,'run',return_value=SimpleNamespace(returncode=0)) as run, \
                        patch.object(scenario,'health'):
                    if drift:
                        with self.assertRaises(ValueError):gate.product(5)
                        run.assert_not_called();self.assertEqual(gate.config_path.read_bytes(),before)
                    else:
                        gate.product(5)
                        self.assertEqual(run.call_args.args[0],['kubecli','setup','mongodb-gate','07','--tags','mongodb'])

    def test_replica_scale_whole_volume_set_validates_before_first_retirement(self):
        for drift in (None,'last-claim','last-pv','retained','pod','statefulset'):
            with self.subTest(drift=drift),tempfile.TemporaryDirectory() as tmp:
                scenario,gate=self.scale_gate(tmp);removed=set()
                names=['mongod-data-mongodb-gate-data-rs0-'+str(i) for i in (3,4)]
                gate.state['extra_claims']={name:{'uid':'uid-'+name,'volume':'volume-'+name} for name in names}
                def get(kind,name='',*args,**kwargs):
                    if kind=='pods':return {'items':[{'metadata':{'name':'mongodb-gate-data-rs0-4'}}] if drift=='pod' else []}
                    if kind=='statefulset':return {'spec':{'replicas':5 if drift=='statefulset' else 3}}
                    if kind=='pvc':
                        if name in removed:return None
                        return {'metadata':{'uid':'foreign' if drift=='last-claim' and name==names[-1] else 'uid-'+name},
                            'spec':{'volumeName':'volume-'+name}}
                    if kind=='pv':
                        claim=name.removeprefix('volume-')
                        if claim in removed:return None
                        return {'spec':{'storageClassName':'mongodb-longhorn',
                            'persistentVolumeReclaimPolicy':'Retain' if drift=='retained' else 'Delete',
                            'csi':{'driver':'driver.longhorn.io'},'claimRef':{'namespace':'mongodb-gate',
                                'name':claim,'uid':'foreign' if drift=='last-pv' and claim==names[-1] else 'uid-'+claim}}}
                    raise AssertionError(kind)
                with patch.object(gate,'capture_claims'),patch.object(scenario.lab,'get',side_effect=get), \
                        patch.object(scenario.lab,'delete_uid',side_effect=lambda endpoint,uid:removed.add(endpoint.rsplit('/',1)[1])) as delete, \
                        patch.object(scenario.lab,'run'):
                    if drift:
                        with self.assertRaises(ValueError):gate.retire_claims()
                        delete.assert_not_called()
                    else:
                        gate.retire_claims();self.assertEqual(removed,set(names))

    def test_replica_scale_claim_capture_refuses_replacement_and_old_source_volume(self):
        for drift in (None,'foreign','uid','volume','original','namespace'):
            with self.subTest(drift=drift),tempfile.TemporaryDirectory() as tmp:
                scenario,gate=self.scale_gate(tmp)
                name='mongod-data-mongodb-gate-data-rs0-3'
                gate.state['extra_claims'][name]={'uid':'old','volume':'new-volume'}
                gate.state['before']['pvc']=[{'spec':{'volumeName':'source-volume'}}]
                claim={'metadata':{'name':name,'namespace':'foreign' if drift=='namespace' else 'mongodb-gate',
                    'uid':'changed' if drift=='uid' else 'old','labels':{
                        'app.kubernetes.io/instance':'foreign' if drift=='foreign' else 'mongodb-gate-data',
                        'app.kubernetes.io/managed-by':'percona-server-mongodb-operator'}},
                    'spec':{'storageClassName':'mongodb-longhorn','volumeName':
                        'source-volume' if drift=='original' else ('replaced-volume' if drift=='volume' else 'new-volume')}}
                with patch.object(gate,'verify'),patch.object(scenario.lab,'get',side_effect=lambda kind,n,*a:
                        claim if n==name else None):
                    if drift:
                        with self.assertRaises(ValueError):gate.capture_claims()
                    else:gate.capture_claims();self.assertEqual(gate.state['extra_claims'][name]['volume'],'new-volume')

    def test_replica_scale_run_preserves_first_failure_and_keeps_unrecovered_client(self):
        from contextlib import ExitStack
        for failed in (None,'domains','product','digest','interrupt','recovery','both'):
            with self.subTest(failed=failed),tempfile.TemporaryDirectory() as tmp:
                scenario,gate=self.scale_gate(tmp);gate.path.unlink();scenario.client_uid='client'
                events=[];domains=SimpleNamespace(prepare=lambda:events.append('domains'))
                def prepare():
                    events.append('domains')
                    if failed in ('domains','both'):raise ValueError('first failure')
                    if failed=='interrupt':raise SystemExit(143)
                domains.prepare=prepare
                def product(size):
                    events.append('product-'+str(size))
                    if failed=='product':raise ValueError('first failure')
                def digest(hosts):
                    events.append('digest')
                    if failed=='digest':raise ValueError('first failure')
                def recover():
                    events.append('recover')
                    if failed in ('recovery','both'):raise ValueError('recovery failed')
                def capture():gate.state['extra_claims']={'third':{},'fourth':{}}
                pods=[{'metadata':{'name':'mongodb-gate-data-rs0-'+str(i),'labels':{
                    'app.kubernetes.io/instance':'mongodb-gate-data','app.kubernetes.io/replset':'rs0'}},
                    'spec':{'nodeName':'mongodb-node-'+host}} for i,host in enumerate(('212','165','238','135','40'))]
                def get(kind,*args,**kwargs):
                    if kind=='pvc':return None
                    if kind=='psmdb':return {'metadata':{'uid':'source'}}
                    if kind=='pods':return {'items':pods}
                    raise AssertionError(kind)
                with ExitStack() as stack:
                    for context in (patch.object(scenario,'maintenance_members'),patch.object(scenario,'health'),
                        patch.object(scenario,'js'),patch.object(scenario,'snapshot',return_value={'pvc':[],'secrets':[],'psmdb':[]}),
                        patch.object(scenario.lab,'verify',return_value={'cluster_uid':'core'}),patch.object(scenario.lab,'get',side_effect=get),
                        patch.object(scenario.lab,'diagnostics'),patch.object(scenario,'rebuild_data',side_effect=digest),
                        patch.object(gate,'domain_type',return_value=domains),patch.object(gate,'product',side_effect=product),
                        patch.object(gate,'verify_members'),
                        patch.object(gate,'capture_claims',side_effect=capture),patch.object(gate,'recover',side_effect=recover)):
                        stack.enter_context(context)
                    if failed=='interrupt':
                        with self.assertRaises(SystemExit):gate.run()
                    elif failed:
                        with self.assertRaisesRegex(ValueError,'first failure' if failed!='recovery' else 'recovery failed'):gate.run()
                    else:gate.run()
                self.assertEqual(events[-1],'recover')
                if failed in ('both','recovery'):
                    self.assertFalse(scenario.client_attempted);self.assertIsNone(scenario.client_uid);self.assertTrue(gate.path.exists())

    def test_replica_scale_recovery_reenters_only_after_durable_data_cleanup(self):
        from contextlib import ExitStack
        for interrupted in (None,'members','data-clean','delete','save-after-delete','domains','health'):
            with self.subTest(interrupted=interrupted),tempfile.TemporaryDirectory() as tmp:
                scenario,gate=self.scale_gate(tmp)
                client={'metadata':{'uid':'client','labels':{'kubeauto.io/test-run':gate.state['prefix']}}}
                present=[True];once=[False];events=[]
                def step(name):
                    events.append(name)
                    if name==interrupted and not once[0]:
                        once[0]=True;raise SystemExit(143)
                def verify():gate.state=json.loads(gate.path.read_text())
                def delete():
                    present[0]=False;step('delete')
                save=gate.save
                def persist():
                    if gate.state.get('phase')=='client-absent':step('save-after-delete')
                    save()
                domains=SimpleNamespace(restore=lambda:step('domains'))
                with ExitStack() as stack:
                    for context in (patch.object(gate,'verify',side_effect=verify),
                        patch.object(gate,'recover_members',side_effect=lambda:step('members')),
                        patch.object(scenario,'clean_workload',side_effect=lambda:step('data-clean')),
                        patch.object(scenario,'delete_client',side_effect=delete),
                        patch.object(gate,'verify_restored_source',side_effect=lambda:step('source')),
                        patch.object(scenario.lab,'get',side_effect=lambda *a:client if present[0] else None),
                        patch.object(gate,'save',side_effect=persist),patch.object(gate,'domain_type',return_value=domains),
                        patch.object(scenario,'wait_health',side_effect=lambda *a:step('health'))):
                        stack.enter_context(context)
                    if interrupted:
                        with self.assertRaises(SystemExit):gate.recover()
                        self.assertTrue(gate.path.exists())
                        phase=json.loads(gate.path.read_text()).get('phase','active')
                        self.assertEqual(phase,'active' if interrupted in ('members','data-clean') else
                            'client-removal' if interrupted in ('delete','save-after-delete') else 'client-absent')
                        gate.recover()
                    else:gate.recover()
                self.assertFalse(gate.path.exists());self.assertFalse(present[0]);self.assertFalse(scenario.client_attempted)
                self.assertEqual(events.count('members'),2 if interrupted in ('members','data-clean') else 1)

    def test_replica_scale_recovery_refuses_missing_unverified_or_replaced_client(self):
        for phase,current in (('active',None),('client-removal','foreign'),('client-absent','client')):
            with self.subTest(phase=phase,current=current),tempfile.TemporaryDirectory() as tmp:
                scenario,gate=self.scale_gate(tmp);gate.state['phase']=phase;gate.save()
                pod=None if current is None else {'metadata':{'uid':current,
                    'labels':{'kubeauto.io/test-run':gate.state['prefix']}}}
                with patch.object(gate,'verify'),patch.object(scenario.lab,'get',return_value=pod), \
                        patch.object(gate,'recover_members') as members,patch.object(scenario,'delete_client') as delete, \
                        patch.object(gate,'domain_type') as domains:
                    with self.assertRaises(ValueError):gate.recover()
                    members.assert_not_called();delete.assert_not_called();domains.assert_not_called()
                self.assertTrue(gate.path.exists())

    def test_replica_scale_recovery_checks_original_source_before_deleting_client(self):
        with tempfile.TemporaryDirectory() as tmp:
            scenario,gate=self.scale_gate(tmp);gate.state['phase']='client-removal';gate.save()
            pod={'metadata':{'uid':'client','labels':{'kubeauto.io/test-run':gate.state['prefix']}}}
            with patch.object(gate,'verify'),patch.object(scenario.lab,'get',return_value=pod), \
                    patch.object(gate,'verify_restored_source',side_effect=ValueError('source changed')), \
                    patch.object(scenario,'delete_client') as delete:
                with self.assertRaisesRegex(ValueError,'source changed'):gate.recover()
                delete.assert_not_called()
            self.assertTrue(gate.path.exists())

    def test_replica_scale_member_recovery_requires_entire_original_boundary_and_digest(self):
        from contextlib import ExitStack
        for failed in (None,'capture','majority','product','volumes','votes','source','digest'):
            with self.subTest(failed=failed),tempfile.TemporaryDirectory() as tmp:
                scenario,gate=self.scale_gate(tmp);events=[]
                def step(name):
                    events.append(name)
                    if failed==name:raise ValueError('failed '+name)
                def digest(hosts):
                    self.assertEqual(len(hosts),3);self.assertTrue(hosts[-1].startswith('mongodb-gate-data-rs0-2.'))
                    step('digest')
                with ExitStack() as stack:
                    cleanup=stack.enter_context(patch.object(scenario,'cleanup'))
                    for context in (patch.object(gate,'capture_claims',side_effect=lambda:step('capture')),
                        patch.object(scenario,'js',side_effect=lambda *a:step('majority')),
                        patch.object(gate,'product',side_effect=lambda size:step('product') if size==3 else self.fail('unsafe size')),
                        patch.object(gate,'retire_claims',side_effect=lambda:step('volumes')),
                        patch.object(gate,'verify_members',side_effect=lambda size:step('votes') if size==3 else self.fail('unsafe size')),
                        patch.object(gate,'verify_restored_source',side_effect=lambda:step('source')),
                        patch.object(scenario,'rebuild_data',side_effect=digest)):
                        stack.enter_context(context)
                    if failed:
                        with self.assertRaisesRegex(ValueError,'failed '+failed):gate.recover_members()
                    else:gate.recover_members()
                    cleanup.assert_not_called()
                expected=['capture','majority','product','volumes','votes','source','digest']
                self.assertEqual(events,expected if failed is None else expected[:expected.index(failed)+1])

    def test_replica_scale_membership_assertion_checks_votes_majority_and_all_hosts_in_node(self):
        import shutil
        node=shutil.which('node')
        self.assertIsNotNone(node,'MongoDB BSON and membership contracts require Node on PATH')
        scenario=scenarios.Scenario(ROOT)
        for size in (3,5):
            with patch.object(scenario,'js') as js:
                gate=scenarios.ReplicaScale(scenario);gate.verify_members(size)
            code=js.call_args.args[0]
            for drift in (None,'votes','majority','health','foreign','primary','arbiter'):
                hosts=['mongodb-gate-data-rs0-'+str(i)+'.mongodb-gate-data-rs0.mongodb-gate.svc.cluster.local:27017' for i in range(size)]
                members=[{'name':host,'health':1,'state':1 if i==0 else 2} for i,host in enumerate(hosts)]
                config=[{'host':host,'votes':1} for host in hosts]
                majority=size//2+1
                if drift=='votes':config[-1]['votes']=0
                if drift=='majority':majority-=1
                if drift=='health':members[-1]['health']=0
                if drift=='foreign':config[-1]['host']='foreign'
                if drift=='primary':members[-1]['state']=1
                if drift=='arbiter':config[-1]['arbiterOnly']=True
                prelude="const assert=require('assert');assert.eq=assert.strictEqual;assert.commandWorked=x=>x;"
                prelude+="const fixtureStatus="+json.dumps({'members':members,'writeMajorityCount':majority})+",fixtureConfig="+json.dumps({'config':{'members':config}})+";"
                prelude+="global.Mongo=function(){this.getDB=()=>({auth:()=>true,runCommand:x=>x.replSetGetStatus?fixtureStatus:fixtureConfig});};global.print=()=>{};"
                result=scenarios.subprocess.run([node,'-e',prelude+code],capture_output=True,text=True)
                with self.subTest(size=size,drift=drift):self.assertEqual(result.returncode==0,drift is None,result.stderr)

    def test_capacity_modes_reach_fixed_launcher_with_their_exact_terminal_markers(self):
        script = '''source tests/helpers/mongodb-runner.sh
mongodb_launch() { printf '%s|%s\\n' "$1" "$2"; }
mongodb_dispatch --mongodb-capacity-expand
mongodb_dispatch --mongodb-capacity-recover
mongodb_dispatch --mongodb-capacity-pressure
mongodb_dispatch --mongodb-capacity-pressure-recover
mongodb_dispatch --mongodb-capacity-lag
mongodb_dispatch --mongodb-capacity-lag-recover
mongodb_dispatch --mongodb-capacity
'''
        result = scenarios.subprocess.run(['bash', '-euc', script], cwd=ROOT, capture_output=True, text=True, check=True)
        self.assertEqual(result.stdout.splitlines(), ['capacity-expand|MONGODB_CAPACITY_EXPAND_PASS',
                                                     'capacity-recover|MONGODB_CAPACITY_RECOVER_PASS',
                                                     'capacity-pressure|MONGODB_CAPACITY_PRESSURE_PASS',
                                                     'capacity-pressure-recover|MONGODB_CAPACITY_PRESSURE_RECOVER_PASS',
                                                     'capacity-lag|MONGODB_CAPACITY_LAG_PASS',
                                                     'capacity-lag-recover|MONGODB_CAPACITY_LAG_RECOVER_PASS',
                                                     'capacity|MONGODB_CAPACITY_PASS'])

    def test_full_capacity_requires_all_three_scoped_scenarios_and_stops_on_first_error(self):
        import contextlib
        import io
        for failed in (None,'capacity-expand','capacity-pressure','capacity-lag'):
            calls=[]
            def execute(module):
                calls.append(module)
                if module==failed:raise ValueError('first module failure')
            output=io.StringIO()
            with self.subTest(failed=failed),patch.object(scenarios,'Scenario') as factory,contextlib.redirect_stdout(output):
                factory.return_value.execute.side_effect=execute
                if failed:
                    with self.assertRaisesRegex(ValueError,'first module failure'):scenarios.capacity_suite(ROOT)
                    self.assertNotIn('MONGODB_CAPACITY_PASS',output.getvalue())
                    self.assertEqual(calls[-1],failed)
                else:
                    scenarios.capacity_suite(ROOT)
                    self.assertEqual(calls,['capacity-expand','capacity-pressure','capacity-lag'])
                    self.assertIn('MONGODB_CAPACITY_SCOPED_CLEAN_PASS',output.getvalue())
                    self.assertIn('MONGODB_CAPACITY_PASS',output.getvalue())

    def test_lag_local_probe_requires_same_secondary_and_reads_local_applied_position(self):
        scenario = scenarios.Scenario(ROOT)
        member = {'name':'member','uid':'original'}
        with patch.object(scenario.lab,'get',return_value={'metadata':{'uid':'original'}}) as get, \
                patch.object(scenario.lab,'run',return_value=json.dumps(
                    {'state':2,'applied':'Timestamp(1,2)','wallTime':1000,'count':0})) as run:
            self.assertEqual(scenario.lag_local_state(member)['count'],0)
            script = run.call_args.args[-1]
            self.assertIn("setReadPref('secondaryPreferred')",script)
            self.assertIn('s.optimes.appliedOpTime.ts.toString()',script)
            self.assertIn(scenario.prefix+'_documents',script)
            self.assertIn('MONGODB_DATABASE_ADMIN_PASSWORD',script)
            self.assertIn("readConcern:{level:'local'},maxTimeMS:5000",script)
            self.assertEqual(run.call_args.kwargs['timeout'],15)
            run.reset_mock();get.return_value = {'metadata':{'uid':'replacement'}}
            with self.assertRaisesRegex(ValueError,'identity changed'):scenario.lag_local_state(member)
            run.assert_not_called()

    def test_lag_restore_checks_entire_graph_before_any_patch_or_delete(self):
        scenario = scenarios.Scenario(ROOT);prefix = 'mongodb_gate_0123456789abcdef'
        policy = {'metadata':{'name':scenario.fixture.NAME+'-access','namespace':scenario.ns,'uid':'policy',
                             'labels':scenario.product.LABELS},'spec':{'podSelector':{},'policyTypes':['Ingress'],'ingress':[]}}
        modified,deny = scenarios.quorum_policies(policy,prefix)
        deny['metadata'].update(uid='deny',resourceVersion='rv')
        pod = {'metadata':{'uid':'member','labels':{'kubeauto.io/mongodb-quorum-fault':prefix}}}
        cr = {'metadata':{'uid':'cr','labels':scenario.product.LABELS}}
        journal = {'run_id':prefix,'cluster_uid':'core','cr_uid':'cr','claims':{},'policy_uid':'policy',
            'original_spec':copy.deepcopy(policy['spec']),'modified_spec':modified,'deny':copy.deepcopy(deny),'deny_uid':'deny',
            'member':{'name':'member','uid':'member'}}
        policy['spec'] = modified
        deleted = []
        def get(kind,name='',*args,**kw):
            if kind=='psmdb':return cr
            if kind=='pod':return pod
            if kind=='networkpolicies':return {'items':[policy]+([] if deleted else [deny])}
            return policy if name==policy['metadata']['name'] else (None if deleted else deny)
        with patch.object(scenario.lab,'get',side_effect=get), \
                patch.object(scenario.lab,'verify',return_value={'cluster_uid':'core'}), \
                patch.object(scenario,'capacity_claims',return_value={}),patch.object(scenario,'lag_patch') as patcher, \
                patch.object(scenarios.subprocess,'run') as run:
            deny['spec']['ingress'] = [{'ports':[{'port':27017}]}]
            with self.assertRaisesRegex(ValueError,'changed fault graph'):scenario.lag_restore(journal)
            patcher.assert_not_called();run.assert_not_called()
            deny['spec'] = copy.deepcopy(journal['deny']['spec'])
            deny['spec']['egress'] = [{}]
            with self.assertRaisesRegex(ValueError,'changed fault graph'):scenario.lag_restore(journal)
            patcher.assert_not_called();run.assert_not_called()
            deny['spec'] = copy.deepcopy(journal['deny']['spec']);pod['metadata']['uid'] = 'replacement'
            with self.assertRaisesRegex(ValueError,'changed fault graph'):scenario.lag_restore(journal)
            patcher.assert_not_called();run.assert_not_called()
            pod['metadata']['uid'] = 'member'
            # NetworkPolicy empty rule slices use omitempty in the pinned API.
            # Absence is deny-all, whereas a list containing {} permits traffic.
            deny['spec'].pop('ingress');deny['spec'].pop('egress')
            def apply(kind,name,uid,operations):
                if kind=='networkpolicy':policy['spec'] = copy.deepcopy(journal['original_spec'])
                else:pod['metadata']['labels'].pop('kubeauto.io/mongodb-quorum-fault')
            def delete(*args,**kwargs):
                self.assertEqual(json.loads(kwargs['input'])['preconditions'],{'uid':'deny','resourceVersion':'rv'})
                deleted.append(True)
                return SimpleNamespace(returncode=0)
            patcher.side_effect = apply;run.side_effect = delete
            with patch.object(scenario,'wait_health'):
                scenario.lag_restore(journal)
            self.assertEqual(policy['spec'],journal['original_spec'])
            self.assertEqual(patcher.call_count,2)
            self.assertEqual(run.call_count,1)

    def test_lag_recovery_refuses_foreign_client_before_fault_graph_changes(self):
        scenario = scenarios.Scenario(ROOT)
        with tempfile.TemporaryDirectory() as tmp:
            scenario.lab.directory = Path(tmp)
            journal = {'run_id':'mongodb_gate_0123456789abcdef','client_uid':'original'}
            Path(tmp,'mongodb-fault-capacity-lag.json').write_text(json.dumps(journal))
            with patch.object(scenario.lab,'get',return_value={'metadata':{'uid':'foreign'}}), \
                    patch.object(scenario,'lag_restore') as restore,patch.object(scenario,'delete_client') as delete:
                with self.assertRaisesRegex(ValueError,'foreign client'):scenario.capacity_lag_recover()
                restore.assert_not_called();delete.assert_not_called()
            self.assertTrue(Path(tmp,'mongodb-fault-capacity-lag.json').exists())

    def test_lag_first_error_survives_failed_graph_recovery_with_journal_preserved(self):
        scenario = scenarios.Scenario(ROOT);members = [
            {'metadata':{'name':scenario.fixture.NAME+'-rs0-'+str(i),'uid':str(i)}} for i in range(3)]
        policy = {'metadata':{'name':scenario.fixture.NAME+'-access','namespace':scenario.ns,'uid':'policy',
                             'labels':scenario.product.LABELS},'spec':{'podSelector':{},'policyTypes':['Ingress'],'ingress':[]}}
        with tempfile.TemporaryDirectory() as tmp:
            scenario.lab.directory = Path(tmp);scenario.client_uid = 'client'
            with patch.object(scenario.lab,'get',side_effect=lambda kind,*a,**k:{'items':[policy]} if kind=='networkpolicies' else {'metadata':{'uid':'cr'}}), \
                    patch.object(scenario,'maintenance_members',return_value=members), \
                    patch.object(scenario,'js',return_value='MONGODB_CAPACITY_PRIMARY '+scenario.fixture.NAME+'-rs0-0.host:27017\n'), \
                    patch.object(scenario.lab,'verify',return_value={'cluster_uid':'core'}), \
                    patch.object(scenario,'capacity_claims',return_value={}), \
                    patch.object(scenarios.subprocess,'run',return_value=SimpleNamespace(returncode=1)), \
                    patch.object(scenario,'lag_restore',side_effect=ValueError('secondary cleanup failure')):
                with self.assertRaisesRegex(ValueError,'deny fixture creation failed'):scenario.capacity_lag()
            journal = json.loads(Path(tmp,'mongodb-fault-capacity-lag.json').read_text())
            self.assertEqual(journal['member']['uid'],'1')
            self.assertEqual(journal['client_uid'],'client')
            self.assertNotEqual(journal['original_spec'],journal['modified_spec'])

    def test_lag_requires_absent_majority_data_and_unchanged_local_optime_then_three_member_digest(self):
        scenario = scenarios.Scenario(ROOT);members = [
            {'metadata':{'name':scenario.fixture.NAME+'-rs0-'+str(i),'uid':str(i)}} for i in range(3)]
        policy = {'metadata':{'name':scenario.fixture.NAME+'-access','namespace':scenario.ns,'uid':'policy',
                             'labels':scenario.product.LABELS},'spec':{'podSelector':{},'policyTypes':['Ingress'],'ingress':[]}}
        for after,valid in [({'state':2,'applied':'old','wallTime':1000,'count':0},True),
                            ({'state':2,'applied':'new','wallTime':2000,'count':0},False),
                            ({'state':2,'applied':'old','wallTime':1000,'count':512},False)]:
            with self.subTest(valid=valid,after=after),tempfile.TemporaryDirectory() as tmp:
                scenario.lab.directory = Path(tmp);scenario.client_uid = 'client'
                with patch.object(scenario.lab,'get',side_effect=lambda kind,*a,**k:{'items':[policy]} if kind=='networkpolicies' else {'metadata':{'uid':'cr'}}), \
                        patch.object(scenario,'maintenance_members',return_value=members), \
                        patch.object(scenario,'js',return_value='MONGODB_CAPACITY_PRIMARY '+scenario.fixture.NAME+'-rs0-0.host:27017\n') as write, \
                        patch.object(scenario.lab,'verify',return_value={'cluster_uid':'core'}), \
                        patch.object(scenario,'capacity_claims',return_value={}),patch.object(scenario,'lag_patch'), \
                        patch.object(scenario,'restart_isolated_members') as restart, \
                        patch.object(scenario,'lag_local_state',side_effect=[{'state':2,'applied':'old','wallTime':1000,'count':0},after]), \
                        patch.object(scenarios.subprocess,'run',return_value=SimpleNamespace(returncode=0,stdout='{"metadata":{"uid":"deny"}}')), \
                        patch.object(scenario,'lag_restore') as restore,patch.object(scenario,'rebuild_data') as digest:
                    if valid:
                        scenario.capacity_lag()
                        self.assertEqual(len(digest.call_args.args[0]),3)
                        self.assertEqual(restore.call_count,2)
                    else:
                        with self.assertRaisesRegex(ValueError,'did not prove'):scenario.capacity_lag()
                        digest.assert_not_called()
                    self.assertEqual(len(restart.call_args.args[0]),1)
                    self.assertIn("w:'majority',j:true",write.call_args.args[0])

    def test_pressure_plan_accepts_migrated_ten_gib_fixture_with_same_safe_watermark(self):
        plan=scenarios.pressure_plan('4096 2559319 1958895')
        self.assertEqual(plan['floor'],plan['total']//4)
        self.assertGreaterEqual(plan['floor'],1024**3)
        self.assertGreater(plan['fill_mib'],4096)
        self.assertLessEqual(plan['fill_mib'],8192)
        self.assertLess(plan['available']-plan['fill_mib']*1024**2-plan['floor'],1024**2)
        for raw in ('4096 2621441 2200000','4096 2559319 600000','4096 2559319 2600000'):
            with self.subTest(raw=raw),self.assertRaises(ValueError):scenarios.pressure_plan(raw)

    def test_pressure_plan_keeps_at_least_one_gib_and_refuses_unsafe_inputs(self):
        plan = scenarios.pressure_plan('4096 1269672 850000')
        self.assertGreaterEqual(plan['floor'], 1024**3)
        self.assertGreaterEqual(plan['available']-plan['fill_mib']*1024**2, plan['floor'])
        self.assertLess(plan['available']-plan['fill_mib']*1024**2-plan['floor'], 1024**2)
        for text in ('4096 1269672 260000', '4096 1269672 1300000', '4096 1269672 318000',
                     '4096 3000000 2000000', '0 1269672 850000', '4096 262144 240000'):
            with self.assertRaises(ValueError):scenarios.pressure_plan(text)

    def test_pressure_step_rechecks_growth_instead_of_replaying_stale_initial_budget(self):
        # Actual last-chunk failure: 121MiB would cross the safety floor by
        # 1257472 bytes after concurrent database activity consumed headroom.
        plan = {'block':4096,'total':6257475584,'floor':1564368896}
        available = 1689989120
        self.assertLess(available-121*1024**2,plan['floor'])
        count = scenarios.pressure_step('4096 1527704 '+str(available//4096),plan,121)
        self.assertEqual(count,111)
        self.assertGreaterEqual(available-count*1024**2,plan['floor']+8*1024**2)
        self.assertLessEqual(available-count*1024**2,plan['floor']+16*1024**2)
        self.assertEqual(scenarios.pressure_step('4096 1527704 '+str((plan['floor']+8*1024**2)//4096),plan,121),0)
        for text in ('1024 6110816 1650379','4096 1527705 412595','4096 1527704 1'):
            with self.assertRaisesRegex(ValueError,'live filesystem'):scenarios.pressure_step(text,plan,121)

    def test_pressure_shell_checks_headroom_releases_writer_lock_and_rejects_foreign_owner(self):
        import os
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)/'fixture';binpath = Path(tmp)/'bin';binpath.mkdir()
            stat = binpath/'stat';stat.write_text('#!/bin/sh\nprintf "4096 850000\\n"\n');stat.chmod(0o755)
            dd = binpath/'dd';dd.write_text('#!/bin/sh\nexit 9\n');dd.chmod(0o755)
            env = dict(os.environ, PATH=str(binpath)+':'+os.environ['PATH'])
            run = 'mongodb_gate_0123456789abcdef'
            def command(script, *args):
                return scenarios.subprocess.run(['sh','-ec',script,'pressure',str(directory),run,*map(str,args)],
                    capture_output=True,text=True,timeout=10,env=env)
            self.assertEqual(command(scenarios.PRESSURE_CREATE).returncode, 0)
            self.assertNotEqual(command(scenarios.PRESSURE_CREATE).returncode, 0)
            self.assertNotEqual(command(scenarios.PRESSURE_CHUNK,128,1024**3).returncode, 0)
            self.assertFalse((directory/'writing').exists())
            # This guard must fail before even the deliberately failing dd.
            dd.write_text('#!/bin/sh\nprintf invoked > "'+str(Path(tmp)/'invoked')+'"\n');dd.chmod(0o755)
            refused = command(scenarios.PRESSURE_CHUNK,128,4*1024**3)
            self.assertEqual(refused.returncode, 42)
            self.assertIn('MONGODB_PRESSURE_HEADROOM available=3481600000 requested=134217728 floor=4294967296', refused.stdout)
            self.assertFalse((Path(tmp)/'invoked').exists())
            self.assertFalse((directory/'writing').exists())
            (directory/'owner').write_text('foreign')
            self.assertNotEqual(command(scenarios.PRESSURE_CLEAR).returncode, 0)
            self.assertTrue((directory/'fill').exists())
            (directory/'owner').write_text(run)
            self.assertEqual(command(scenarios.PRESSURE_CLEAR).returncode, 0)
            self.assertFalse(directory.exists())
            self.assertEqual(command(scenarios.PRESSURE_CLEAR).returncode, 0)

    def test_pressure_command_failure_preserves_only_numeric_diagnostics(self):
        scenario = scenarios.Scenario(ROOT)
        journal = {'run_id':'mongodb_gate_0123456789abcdef','cluster_uid':'core','cr_uid':'cr',
            'member':{'name':'member','uid':'original','node':'mongodb-node-212'},'claims':{}}
        cr = {'metadata':{'uid':'cr','labels':scenario.product.LABELS}}
        pod = {'metadata':{'uid':'original'},'spec':{'nodeName':'mongodb-node-212'}}
        headroom = 'MONGODB_PRESSURE_HEADROOM available=100 requested=10 floor=99'
        with tempfile.TemporaryDirectory() as tmp:
            scenario.lab.directory = Path(tmp)
            with patch.object(scenario.lab,'verify',return_value={'cluster_uid':'core'}), \
                    patch.object(scenario.lab,'get',side_effect=lambda kind,*a,**k:pod if kind=='pod' else cr), \
                    patch.object(scenario,'capacity_claims',return_value={}), \
                    patch.object(scenarios.subprocess,'run',return_value=SimpleNamespace(returncode=42,
                        stdout=headroom+'\nprivate-output',stderr='private-error')):
                with self.assertRaisesRegex(ValueError,'stage=chunk rc=42'):
                    scenario.pressure_exec(journal,scenarios.PRESSURE_CHUNK,128,99)
            path = Path(tmp,'mongodb-capacity-pressure-command.json')
            record = json.loads(path.read_text())
            self.assertEqual(record['headroom'],[headroom])
            self.assertEqual(record['member'],journal['member'])
            self.assertNotIn('private',path.read_text())
            self.assertEqual(path.stat().st_mode & 0o777,0o600)

    def test_pressure_refuses_replaced_member_before_any_remote_file_command(self):
        scenario = scenarios.Scenario(ROOT)
        journal = {'run_id':'mongodb_gate_0123456789abcdef','cluster_uid':'core','cr_uid':'cr',
            'member':{'name':'member','uid':'original','node':'mongodb-node-212'},'claims':{}}
        cr = {'metadata':{'uid':'cr','labels':scenario.product.LABELS}}
        pod = {'metadata':{'uid':'foreign'},'spec':{'nodeName':'mongodb-node-212'}}
        with patch.object(scenario.lab,'verify',return_value={'cluster_uid':'core'}), \
                patch.object(scenario.lab,'get',side_effect=lambda kind,*a,**k:pod if kind=='pod' else cr), \
                patch.object(scenarios.subprocess,'run') as run:
            with self.assertRaisesRegex(ValueError,'changed source'):scenario.pressure_exec(journal,scenarios.PRESSURE_CLEAR)
            run.assert_not_called()

    def test_pressure_first_error_and_failed_cleanup_keep_identity_journal(self):
        scenario = scenarios.Scenario(ROOT)
        members = [{'metadata':{'name':scenario.fixture.NAME+'-rs0-'+str(i),'uid':'uid-'+str(i)},
                    'spec':{'nodeName':'mongodb-node-'+str(node)}} for i,node in enumerate((212,165,238))]
        with tempfile.TemporaryDirectory() as tmp:
            scenario.lab.directory = Path(tmp);scenario.client_uid = 'client'
            with patch.object(scenario,'maintenance_members',return_value=members), \
                    patch.object(scenario,'js',return_value='MONGODB_CAPACITY_PRIMARY '+scenario.fixture.NAME+'-rs0-0.host:27017\n'), \
                    patch.object(scenario.lab,'run',return_value='4096 1269672 850000'), \
                    patch.object(scenario.lab,'get',return_value={'metadata':{'uid':'cr'}}), \
                    patch.object(scenario.lab,'verify',return_value={'cluster_uid':'core'}), \
                    patch.object(scenario,'capacity_claims',return_value={'claims':'same'}), \
                    patch.object(scenario,'pressure_exec',side_effect=ValueError('first filler failure')), \
                    patch.object(scenario,'pressure_restore',side_effect=ValueError('cleanup failure')):
                with self.assertRaisesRegex(ValueError,'first filler failure'):scenario.capacity_pressure()
            journal = json.loads(Path(tmp,'mongodb-fault-capacity-pressure.json').read_text())
            self.assertEqual(journal['client_uid'],'client')
            self.assertEqual(journal['member']['uid'],'uid-1')
            self.assertEqual(journal['claims'],{'claims':'same'})

    def test_pressure_success_checks_three_members_and_removes_only_its_journal(self):
        scenario = scenarios.Scenario(ROOT)
        members = [{'metadata':{'name':scenario.fixture.NAME+'-rs0-'+str(i),'uid':'uid-'+str(i)},
                    'spec':{'nodeName':'mongodb-node-'+str(node)}} for i,node in enumerate((212,165,238))]
        plan = scenarios.pressure_plan('4096 1269672 850000')
        available,remaining = 850000,plan['fill_mib']
        readings = ['4096 1269672 850000']
        while remaining:
            readings.append('4096 1269672 '+str(available))
            count = scenarios.pressure_step(readings[-1],plan,remaining)
            if not count:break
            remaining -= count;available -= count*256
        readings.append('4096 1269672 '+str(available))
        with tempfile.TemporaryDirectory() as tmp:
            scenario.lab.directory = Path(tmp);scenario.client_uid = 'client'
            with patch.object(scenario,'maintenance_members',return_value=members), \
                    patch.object(scenario,'js',return_value='MONGODB_CAPACITY_PRIMARY '+scenario.fixture.NAME+'-rs0-0.host:27017\n') as js, \
                    patch.object(scenario.lab,'run',side_effect=readings), \
                    patch.object(scenario.lab,'get',return_value={'metadata':{'uid':'cr'}}), \
                    patch.object(scenario.lab,'verify',return_value={'cluster_uid':'core'}), \
                    patch.object(scenario,'capacity_claims',return_value={'claims':'same'}), \
                    patch.object(scenario,'pressure_exec') as fill, patch.object(scenario,'pressure_restore') as restore, \
                    patch.object(scenario,'rebuild_data') as digest, patch.object(scenario,'health'):
                scenario.capacity_pressure()
            chunks = [call.args[2] for call in fill.call_args_list if call.args[1]==scenarios.PRESSURE_CHUNK]
            self.assertEqual(sum(chunks),plan['fill_mib']-8)
            self.assertTrue(all(1<=count<=128 for count in chunks))
            self.assertEqual(js.call_count,len(chunks)+2)
            self.assertEqual(len(digest.call_args.args[0]),3)
            restore.assert_called_once()
            self.assertFalse(Path(tmp,'mongodb-fault-capacity-pressure.json').exists())

    def test_capacity_tracks_exact_claim_and_pv_identity_and_rejects_rebinding(self):
        scenario = scenarios.Scenario(ROOT)
        claims = [{'metadata': {'name': 'mongod-data-'+scenario.fixture.NAME+'-rs0-'+str(i),
                               'uid': 'claim-'+str(i), 'labels': {'app.kubernetes.io/instance': scenario.fixture.NAME}},
                   'spec': {'volumeName': 'pv-'+str(i), 'storageClassName': 'mongodb-longhorn'},
                   'status': {'phase': 'Bound'}} for i in range(3)]
        volumes = {'pv-'+str(i): {'metadata': {'uid': 'volume-'+str(i)}, 'spec': {
            'storageClassName': 'mongodb-longhorn', 'csi': {'driver': 'driver.longhorn.io'},
            'claimRef': {'namespace': scenario.ns, 'name': claims[i]['metadata']['name'], 'uid': 'claim-'+str(i)}}}
                   for i in range(3)}
        def get(kind, name='', **kwargs):return {'items': claims} if kind == 'pvc' else volumes[name]
        with patch.object(scenario.lab, 'get', side_effect=get):
            identity = scenario.capacity_claims()
            self.assertEqual(len(identity), 3)
            self.assertEqual(identity[claims[0]['metadata']['name']]['volume_uid'], 'volume-0')
            volumes['pv-0']['spec']['claimRef']['uid'] = 'foreign'
            with self.assertRaisesRegex(ValueError, 'rebound backing'):scenario.capacity_claims()

    def test_capacity_interruption_before_workload_keeps_original_and_desired_recovery_states(self):
        scenario = scenarios.Scenario(ROOT)
        with tempfile.TemporaryDirectory() as tmp:
            scenario.lab.directory = Path(tmp);scenario.client_uid = 'client-owned'
            Path(tmp, 'config.yml').write_text('mongodb_pvc_size: 4Gi\n')
            with patch.object(scenario.lab, 'get', return_value={'metadata': {'uid': 'cr-owned'}}), \
                    patch.object(scenario.lab, 'verify', return_value={'cluster_uid': 'core'}), \
                    patch.object(scenario, 'capacity_claims', return_value={'claims': 'unchanged'}), \
                    patch.object(scenarios.subprocess, 'Popen', side_effect=SystemExit(143)):
                with self.assertRaises(SystemExit):scenario.capacity_expand()
            journal = json.loads(Path(tmp, 'mongodb-fault-capacity.json').read_text())
            self.assertEqual(journal['original'], {'mongodb_pvc_size': '4Gi', 'mongodb_volume_expansion_enabled': None})
            self.assertEqual(journal['applied'], {'mongodb_pvc_size': '5Gi', 'mongodb_volume_expansion_enabled': True})
            self.assertTrue(journal['switch_absent'])
            self.assertEqual(journal['client_uid'], 'client-owned')
            self.assertEqual(Path(tmp, 'config.yml').read_text(), 'mongodb_pvc_size: 4Gi\n')

    def test_capacity_finish_requires_real_filesystem_growth_and_retains_expanded_declaration(self):
        import yaml
        scenario = scenarios.Scenario(ROOT)
        with tempfile.TemporaryDirectory() as tmp:
            scenario.lab.directory = Path(tmp)
            config = {'mongodb_pvc_size': '5Gi', 'mongodb_volume_expansion_enabled': True}
            Path(tmp, 'config.yml').write_text(yaml.safe_dump(config))
            claims = {'mongod-data-member': {'volume': 'pvc-01234567-0123-0123-0123-0123456789ab'}}
            journal = {'cluster_uid': 'core', 'cr_uid': 'owned', 'claims': claims,
                       'applied': config.copy(), 'switch_absent': True}
            cr = {'metadata': {'uid': 'owned', 'labels': scenario.product.LABELS}, 'spec': {'replsets': [
                {'volumeSpec': {'persistentVolumeClaim': {'resources': {'requests': {'storage': '5Gi'}}}}}]}}
            with patch.object(scenario.lab, 'verify', return_value={'cluster_uid': 'core'}), \
                    patch.object(scenario.lab, 'get', return_value=cr), \
                    patch.object(scenario, 'capacity_claims', return_value=claims), \
                    patch.object(scenario, 'maintenance_members', return_value=[{'metadata': {'name': 'member'}, 'spec': {'nodeName': 'mongodb-node-212'}}]*3), \
                    patch.object(scenario.lab, 'run') as stat, patch.object(scenario, 'health'), \
                    patch.object(scenarios.subprocess, 'run', return_value=SimpleNamespace(returncode=0,
                        stdout='Block count: 1048576\nBlock size: 4096\nOverhead clusters: 41048\n')) as product:
                mount = 'Filesystem 1024-blocks Used Available Capacity Mounted on\n/dev/longhorn/' + claims['mongod-data-member']['volume'] + ' 5078688 1 2 1% /data/db\n'
                stat.side_effect = [mount, '4096 1007528']
                with self.assertRaisesRegex(ValueError, 'filesystem'):scenario.capacity_finish(journal)
                self.assertEqual(yaml.safe_load(Path(tmp, 'config.yml').read_text()), config)
                self.assertTrue(all(call.args[0][0] == 'ssh' for call in product.call_args_list))
                product.return_value.stdout = 'Block count: 1310720\nBlock size: 4096\nOverhead clusters: 41048\n'
                stat.side_effect = [mount.replace('/dev/longhorn/', '/foreign/')]
                with self.assertRaisesRegex(ValueError, 'mounted device'):scenario.capacity_finish(journal)
                self.assertEqual(yaml.safe_load(Path(tmp, 'config.yml').read_text()), config)
                self.assertTrue(all(call.args[0][0] == 'ssh' for call in product.call_args_list))
                stat.side_effect = [mount, '4096 1269672']*3
                scenario.capacity_finish(journal)
                self.assertEqual(yaml.safe_load(Path(tmp, 'config.yml').read_text()), {'mongodb_pvc_size': '5Gi'})
                self.assertEqual(product.call_args.args[0], ['kubecli', 'setup', scenario.ns, '07', '--tags', 'mongodb'])

    def test_ext4_usable_blocks_subtract_metadata_without_a_percentage_assumption(self):
        metadata = 'Filesystem features: has_journal extent\nBlock count: 1310720\nBlock size: 4096\nOverhead clusters: 41048\n'
        self.assertEqual(scenarios.ext4_capacity(metadata, '4096 1269672', 5*1024**3),
                         (5*1024**3, 41048*4096))
        for mounted, required in [('4096 1269672', 6*1024**3), ('4096 1048576', 5*1024**3),
                                  ('1024 1269672', 5*1024**3), ('4096 0', 5*1024**3)]:
            with self.assertRaisesRegex(ValueError, 'filesystem'):
                scenarios.ext4_capacity(metadata, mounted, required)
        for invalid in [metadata+'Block count: 1\n', metadata.replace('Overhead clusters: 41048\n',''),
                        metadata.replace('extent', 'extent bigalloc')]:
            with self.assertRaises(ValueError):scenarios.ext4_capacity(invalid, '4096 1269672', 5*1024**3)

    def test_failed_capacity_product_keeps_larger_config_and_recovery_journal(self):
        import io
        import yaml
        scenario = scenarios.Scenario(ROOT)
        with tempfile.TemporaryDirectory() as tmp:
            scenario.lab.directory = Path(tmp);scenario.client_uid = 'original-client'
            Path(tmp, 'config.yml').write_text('mongodb_pvc_size: 4Gi\n')
            def start(*args, **kwargs):
                kwargs['stdout'].write(b'MONGODB_CAPACITY_WORKER_READY\n');kwargs['stdout'].flush()
                return SimpleNamespace(stdin=io.BytesIO(), poll=lambda: None)
            with patch.object(scenario.lab, 'get', return_value={'metadata': {'uid': 'cr'}}), \
                    patch.object(scenario.lab, 'verify', return_value={'cluster_uid': 'core'}), \
                    patch.object(scenario, 'capacity_claims', return_value={'claims': 'same'}), \
                    patch.object(scenarios.subprocess, 'Popen', side_effect=start), \
                    patch.object(scenarios.subprocess, 'run', return_value=SimpleNamespace(returncode=1)), \
                    patch.object(scenario, 'capacity_finish') as finish:
                with self.assertRaisesRegex(ValueError, 'product expansion failed'):scenario.capacity_expand()
                finish.assert_not_called()
            self.assertEqual(yaml.safe_load(Path(tmp, 'config.yml').read_text()),
                             {'mongodb_pvc_size': '5Gi', 'mongodb_volume_expansion_enabled': True})
            self.assertTrue(Path(tmp, 'mongodb-fault-capacity.json').exists())

    def test_capacity_recovery_refuses_foreign_client_before_any_product_or_delete_command(self):
        import yaml
        scenario = scenarios.Scenario(ROOT)
        with tempfile.TemporaryDirectory() as tmp:
            scenario.lab.directory = Path(tmp)
            config = {'mongodb_pvc_size': '4Gi', 'mongodb_volume_expansion_enabled': False}
            Path(tmp, 'config.yml').write_text(yaml.safe_dump(config))
            journal = {'run_id': 'mongodb_gate_0123456789abcdef', 'client_uid': 'original-client',
                'cluster_uid': 'core', 'cr_uid': 'cr', 'claims': {'claims': 'same'}, 'original': config,
                'applied': {'mongodb_pvc_size': '5Gi', 'mongodb_volume_expansion_enabled': True}}
            Path(tmp, 'mongodb-fault-capacity.json').write_text(json.dumps(journal))
            cr = {'metadata': {'uid': 'cr', 'labels': scenario.product.LABELS}, 'spec': {'replsets': [
                {'volumeSpec': {'persistentVolumeClaim': {'resources': {'requests': {'storage': '4Gi'}}}}}]}}
            pod = {'metadata': {'uid': 'foreign-client', 'labels': {'kubeauto.io/test-run': journal['run_id']}}}
            with patch.object(scenario.lab, 'get', side_effect=lambda kind,*args,**kw:cr if kind=='psmdb' else pod), \
                    patch.object(scenario.lab, 'verify', return_value={'cluster_uid': 'core'}), \
                    patch.object(scenario, 'capacity_claims', return_value={'claims': 'same'}), \
                    patch.object(scenarios.subprocess, 'run') as command:
                with self.assertRaisesRegex(ValueError, 'ownership changed'):scenario.capacity_recover()
                command.assert_not_called()
                self.assertTrue(Path(tmp, 'mongodb-fault-capacity.json').exists())

    def test_async_network_probe_is_wrapped_for_the_mongosh_file_parser(self):
        scenario = scenarios.Scenario(ROOT)
        members = [{'status': {'podIP': '127.0.0.' + str(i)}} for i in range(1, 4)]
        with patch.object(scenario, 'maintenance_members', return_value=members), \
                patch.object(scenarios.subprocess, 'run', return_value=SimpleNamespace(
                    returncode=0, stdout='MONGODB_NETWORK_METRICS_DENIED_PASS members=3\n')) as run:
            scenario.network_metrics(False)
        script = run.call_args.kwargs['input']
        self.assertTrue(script.startswith('async function mongodbGateMain(){\n'))
        self.assertTrue(script.endswith('\n}\nmongodbGateMain();\n'))
        self.assertIn('response=await get(address)', script)
        self.assertIn("assert.eq('GATE_TIMEOUT',e.code)", script)
        self.assertIn("print('MONGODB_CLIENT_ERROR '", script)

    def test_rotation_secret_refuses_changed_uid_or_material_before_mutation(self):
        scenario=scenarios.Scenario(ROOT)
        record={'name':'supplied','uid':'owned','allowed':[{'password':'old'},{'password':'new'}]}
        for uid,data in [('foreign',{'password':'old'}),('owned',{'password':'concurrent'})]:
            current={'metadata':{'uid':uid,'labels':scenario.product.LABELS,'resourceVersion':'1'},'data':data}
            with patch.object(scenario.lab,'get',return_value=current),patch.object(scenarios.subprocess,'run') as run:
                with self.assertRaisesRegex(ValueError,'ownership or material'):
                    scenario.rotation_secret(record,{'password':'new'})
                run.assert_not_called()

    def test_rotation_secret_sends_uid_version_and_private_data_only_on_stdin(self):
        scenario=scenarios.Scenario(ROOT);data={'password':'private-new-value'}
        record={'name':'supplied','uid':'owned','allowed':[{'password':'old'},data]}
        current={'metadata':{'uid':'owned','resourceVersion':'2','labels':scenario.product.LABELS},'data':{'password':'old'}}
        with patch.object(scenario.lab,'get',return_value=current), \
                patch.object(scenarios.subprocess,'run',return_value=SimpleNamespace(returncode=0)) as run:
            scenario.rotation_secret(record,data)
        self.assertNotIn('private-new-value',' '.join(run.call_args.args[0]))
        self.assertIn('--patch-file=/dev/stdin',run.call_args.args[0])
        self.assertEqual(json.loads(run.call_args.kwargs['input']),[
            {'op':'test','path':'/metadata/uid','value':'owned'},
            {'op':'test','path':'/metadata/resourceVersion','value':'2'},
            {'op':'replace','path':'/data','value':data}])

    def test_rotation_partial_failure_restores_original_passwords_and_tolerates_cr_generation_increase(self):
        scenario=scenarios.Scenario(ROOT)
        objects={o['metadata']['name']:o for o in scenario.fixture.credentials()['items']}
        for obj in objects.values():
            obj['data']={k:base64.b64encode(v.encode()).decode() for k,v in obj.pop('stringData').items()}
            obj['metadata'].update(uid=obj['metadata']['name']+'-uid',resourceVersion='1')
        original=copy.deepcopy(objects);before={'pvc':[],'secrets':[],'psmdb':[{'name':'source','uid':'source-uid','spec':{},'generation':1}]}
        after=copy.deepcopy(before);after['psmdb'][0]['generation']=3
        def update(record,data):objects[record['name']]['data']=data
        with tempfile.TemporaryDirectory() as tmp:
            scenario.lab.directory=Path(tmp)
            with patch.object(scenario,'snapshot',side_effect=[before,after]), \
                    patch.object(scenario.lab,'get',side_effect=lambda kind,name,ns:objects[name]), \
                    patch.object(scenario.lab,'verify',return_value={'cluster_uid':'owned-core'}), \
                    patch.object(scenario,'rotation_secret',side_effect=update), \
                    patch.object(scenario,'rotation_product',side_effect=[ValueError('product-failed'),None]), \
                    patch.object(scenario,'rotation_client'),patch.object(scenario,'health'),patch.object(scenario,'js'):
                with self.assertRaisesRegex(ValueError,'product-failed'):scenario.rotation()
            self.assertFalse(Path(tmp,'mongodb-fault-rotation.json').exists())
        self.assertEqual(objects,original)

    def test_tls_rotation_does_not_accept_old_ready_members_or_abort_on_transient_missing_member(self):
        scenario=scenarios.Scenario(ROOT)
        members=[{'metadata':{'name':'db-'+str(i),'uid':'old-'+str(i)}} for i in range(3)]
        def new(i):
            return {'metadata':{'name':'db-'+str(i),'uid':'new-'+str(i),'labels':{
                'app.kubernetes.io/instance':scenario.fixture.NAME,'app.kubernetes.io/replset':'rs0'}}}
        with patch.object(scenario,'maintenance_members',return_value=members), \
                patch.object(scenario,'rotation_secret'),patch.object(scenario,'rotation_product'), \
                patch.object(scenario.lab,'get',side_effect=[{'items':[new(0),new(1)]},{'items':[new(i) for i in range(3)]}]), \
                patch.object(scenario.product,'ready',return_value=True),patch.object(scenario,'health'), \
                patch.object(scenario,'rotation_client'),patch.object(scenarios.time,'sleep') as wait:
            scenario.rotation_tls_stage({'tls':{},'tls-internal':{}},{'tls':{},'tls-internal':{}},'test')
        wait.assert_called_once_with(5)

    def test_network_product_failure_restores_original_monitoring_namespace_and_policy(self):
        scenario=scenarios.Scenario(ROOT)
        policy={'metadata':{'uid':'owned-policy'},'spec':{'ingress':[]}}
        with tempfile.TemporaryDirectory() as tmp:
            scenario.lab.directory=Path(tmp);config=Path(tmp,'config.yml')
            config.write_text('mongodb_monitoring_namespace: monitoring\n')
            with patch.object(scenario.lab,'get',return_value=policy), \
                    patch.object(scenario.lab,'verify',return_value={'cluster_uid':'owned-core'}), \
                    patch.object(scenario.product,'owned',return_value=True), \
                    patch.object(scenario,'network_client'),patch.object(scenario,'network_metrics') as metrics, \
                    patch.object(scenario,'snapshot',return_value={'pvc':[],'secrets':[],'psmdb':[]}), \
                    patch.object(scenario,'health'),patch.object(scenarios.subprocess,'run',side_effect=[
                        SimpleNamespace(returncode=1),SimpleNamespace(returncode=0)]) as product:
                with self.assertRaisesRegex(ValueError,'configuration did not converge'):scenario.network()
                self.assertEqual(product.call_count,2)
                self.assertEqual(product.call_args.args[0],['kubecli','setup','mongodb-gate','07','--tags','mongodb'])
                self.assertEqual(metrics.call_args_list,[unittest.mock.call(False),unittest.mock.call(False)])
            self.assertEqual(scenarios.yaml.safe_load(config.read_text())['mongodb_monitoring_namespace'],'monitoring')
            self.assertFalse(Path(tmp,'mongodb-fault-network.json').exists())

    def test_network_recovery_preserves_journal_if_policy_identity_changes(self):
        scenario=scenarios.Scenario(ROOT)
        policy={'metadata':{'uid':'owned-policy'},'spec':{}}
        with tempfile.TemporaryDirectory() as tmp:
            scenario.lab.directory=Path(tmp);Path(tmp,'config.yml').write_text('mongodb_monitoring_namespace: monitoring\n')
            with patch.object(scenario.lab,'get',side_effect=[policy,policy|{'metadata':{'uid':'foreign'}}]), \
                    patch.object(scenario.lab,'verify',return_value={'cluster_uid':'owned-core'}), \
                    patch.object(scenario.product,'owned',return_value=True),patch.object(scenario,'network_client'), \
                    patch.object(scenario,'network_metrics'),patch.object(scenario,'snapshot'), \
                    patch.object(scenarios.subprocess,'run',return_value=SimpleNamespace(returncode=1)) as product:
                with self.assertRaisesRegex(ValueError,'configuration did not converge'):scenario.network()
                self.assertEqual(product.call_count,1)
            self.assertTrue(Path(tmp,'mongodb-fault-network.json').exists())

    def test_rebuild_refuses_foreign_claim_driver_reclaim_policy_or_rebound_pv(self):
        cluster='mongodb-gate-data';name=cluster+'-rs0-1';claim_name='mongod-data-'+name
        pod={'metadata':{'name':name,'uid':'pod-owned','namespace':'mongodb-gate','labels':{
            'app.kubernetes.io/instance':cluster,'app.kubernetes.io/replset':'rs0',
            'app.kubernetes.io/managed-by':'percona-server-mongodb-operator'}},
            'spec':{'volumes':[{'persistentVolumeClaim':{'claimName':claim_name}}]}}
        claim={'metadata':{'name':claim_name,'uid':'claim-owned','namespace':'mongodb-gate',
                           'labels':{'app.kubernetes.io/instance':cluster}},
               'spec':{'storageClassName':'mongodb-longhorn','volumeName':'pv-owned'}}
        pv={'metadata':{'name':'pv-owned','uid':'volume-owned'},'spec':{
            'storageClassName':'mongodb-longhorn','persistentVolumeReclaimPolicy':'Delete',
            'csi':{'driver':'driver.longhorn.io'},
            'claimRef':{'namespace':'mongodb-gate','name':claim_name,'uid':'claim-owned'}}}
        result=scenarios.rebuild_volume_identity(pod,claim,pv,cluster)
        self.assertEqual(result['claim_uid'],'claim-owned')
        variants=[]
        for key,value in [('persistentVolumeReclaimPolicy','Retain'),('csi',{'driver':'foreign'}),
                          ('claimRef',{'namespace':'foreign','name':claim_name,'uid':'claim-owned'})]:
            changed=copy.deepcopy(pv);changed['spec'][key]=value;variants.append((pod,claim,changed))
        changed=copy.deepcopy(claim);changed['metadata']['uid']='rebound';variants.append((pod,changed,pv))
        changed=copy.deepcopy(pod);changed['metadata']['labels']['app.kubernetes.io/instance']='foreign'
        variants.append((changed,claim,pv))
        for objects in variants:
            with self.assertRaisesRegex(ValueError,'unowned or rebound'):
                scenarios.rebuild_volume_identity(*objects,cluster)

    def test_rebuild_wait_cannot_accept_old_terminating_ready_pod_or_reused_volume(self):
        scenario=scenarios.Scenario(ROOT)
        target={'pod':'db-1','pod_uid':'pod-old','claim':'data-db-1','claim_uid':'claim-old','volume':'pv-old'}
        claim={'metadata':{'uid':'claim-old'},'spec':{'volumeName':'pv-old'}}
        pod={'metadata':{'uid':'pod-old'}}
        for new_claim,new_pod in [(claim,pod),
                (claim|{'metadata':{'uid':'claim-new'}},pod|{'metadata':{'uid':'pod-new'}}),
                (claim|{'metadata':{'uid':'claim-new'},'spec':{'volumeName':'pv-new'}},pod)]:
            with patch.object(scenario.lab,'get',side_effect=[new_claim,new_pod]), \
                    patch.object(scenario.product,'ready',return_value=True), \
                    patch.object(scenarios.time,'monotonic',side_effect=[0,601]):
                with self.assertRaisesRegex(ValueError,'identity deadline'):
                    scenario.wait_rebuild_identity(target)
        with patch.object(scenario.lab,'get',side_effect=[
                claim|{'metadata':{'uid':'claim-new'},'spec':{'volumeName':'pv-new'}},
                pod|{'metadata':{'uid':'pod-new'}}]),patch.object(scenario.product,'ready',return_value=True):
            scenario.wait_rebuild_identity(target)

    def test_rebuild_deletes_only_selected_uid_bound_claim_then_pod_and_waits_for_new_identity(self):
        scenario=scenarios.Scenario(ROOT);name=scenario.fixture.NAME+'-rs0-1'
        members=[{'metadata':{'name':scenario.fixture.NAME+'-rs0-'+str(i),'uid':'pod-'+str(i)}} for i in range(3)]
        host=name+'.'+scenario.fixture.NAME+'-rs0.'+scenario.ns+'.svc.cluster.local:27017'
        target={'pod':name,'pod_uid':'pod-1','claim':'mongod-data-'+name,
                'claim_uid':'old','volume':'pv-old','volume_uid':'volume-old'}
        before={'pvc':[{'name':target['claim'],'uid':'old','spec':{'volumeName':'pv-old'}}],
                'secrets':[],'psmdb':[]}
        after=copy.deepcopy(before);after['pvc'][0].update(uid='new',spec={'volumeName':'pv-new'})
        order=[]
        def get(kind,*args):
            if kind=='pvc':return {'spec':{'volumeName':'pv-old'}}
            if kind=='pv':return {}
            if kind=='pod':return {'metadata':{'uid':'new-pod'}}
            raise AssertionError(kind)
        def request(command,**kwargs):
            options=json.loads(kwargs['input'])
            order.append((command[-3],options['preconditions']['uid']))
            return SimpleNamespace(returncode=0)
        with tempfile.TemporaryDirectory() as tmp:
            scenario.lab.directory=Path(tmp)
            with patch.object(scenario,'maintenance_members',return_value=members), \
                    patch.object(scenario,'js',return_value='MONGODB_REBUILD_SECONDARY '+json.dumps(host)), \
                    patch.object(scenario,'rebuild_data'),patch.object(scenario.lab,'get',side_effect=get), \
                    patch.object(scenarios,'rebuild_volume_identity',return_value=target), \
                    patch.object(scenario,'snapshot',side_effect=[before,after]), \
                    patch.object(scenario.lab,'verify',return_value={'cluster_uid':'owned'}), \
                    patch.object(scenarios.subprocess,'run',side_effect=request), \
                    patch.object(scenario,'wait_rebuild_identity',side_effect=lambda _:order.append('new-identity')), \
                    patch.object(scenario,'wait_health',side_effect=lambda _:order.append('health')), \
                    patch.object(scenario.lab,'run') as wait:
                scenario.rebuild()
                wait.assert_called_once_with('wait','--for=delete','pv/pv-old','--timeout=300s',timeout=320)
            self.assertEqual(order[:4],[
                ('/api/v1/namespaces/mongodb-gate/persistentvolumeclaims/'+target['claim'],'old'),
                ('/api/v1/namespaces/mongodb-gate/pods/'+name,'pod-1'),'new-identity','health'])
            self.assertFalse(Path(tmp,'mongodb-fault-rebuild.json').exists())

    def test_maintenance_worker_startup_failure_removes_unmutated_fault_journal(self):
        scenario=scenarios.Scenario(ROOT)
        members=[{'metadata':{'name':'db-'+str(i),'uid':'uid-'+str(i)},
                  'spec':{'nodeName':'mongodb-node-'+str(i)}} for i in range(3)]
        node={'metadata':{'uid':'owned-node','labels':{}},'spec':{}}
        with tempfile.TemporaryDirectory() as tmp:
            scenario.lab.directory=Path(tmp)
            with patch.object(scenario,'maintenance_members',return_value=members), \
                    patch.object(scenario.lab,'get',return_value=node), patch.object(scenario,'maintenance_budget'), \
                    patch.object(scenario,'snapshot',return_value={'pvc':[]}), \
                    patch.object(scenario.lab,'verify',return_value={'cluster_uid':'owned-core'}), \
                    patch.object(scenarios.subprocess,'Popen',side_effect=OSError('worker-start-failed')), \
                    patch.object(scenario,'wait_health'), patch.object(scenario.lab,'run') as mutate:
                with self.assertRaisesRegex(OSError,'worker-start-failed'):scenario.maintenance()
                mutate.assert_not_called()
            self.assertFalse(Path(tmp,'mongodb-fault-maintenance.json').exists())

    def test_maintenance_unsafe_pdb_stops_before_any_eviction(self):
        scenario=scenarios.Scenario(ROOT)
        pdb={'metadata':{'labels':{'app.kubernetes.io/instance':scenario.fixture.NAME,
                                  'app.kubernetes.io/managed-by':'percona-server-mongodb-operator'}},
             'spec':{'maxUnavailable':2}}
        with patch.object(scenario.lab,'get',return_value=pdb),patch.object(scenario.lab,'run') as run:
            with self.assertRaisesRegex(ValueError,'unsafe PDB'):scenario.maintenance_budget(1)
            run.assert_not_called()

    def test_pdb_refusal_requires_api_reason_and_budget_cause(self):
        valid='Error from server (TooManyRequests): Cannot evict pod as it would violate the pod disruption budget.'
        self.assertTrue(scenarios.eviction_refused(SimpleNamespace(returncode=1,stderr=valid)))
        for code, error in [(0,valid),(1,'connection refused'),(1,'TooManyRequests: server is overloaded'),
                            (1,'forbidden: disruption budget')]:
            self.assertFalse(scenarios.eviction_refused(SimpleNamespace(returncode=code,stderr=error)))

    def test_eviction_refuses_replaced_uid_before_api_and_uses_server_dry_run(self):
        scenario=scenarios.Scenario(ROOT)
        member={'metadata':{'name':'mongodb-gate-data-rs0-0','uid':'owned'}}
        with patch.object(scenario.lab,'get',return_value={'metadata':{'uid':'foreign'}}), \
                patch.object(scenarios.subprocess,'run') as run:
            with self.assertRaisesRegex(ValueError,'replaced eviction'):scenario.maintenance_evict(member)
            run.assert_not_called()
        with patch.object(scenario.lab,'get',return_value=member), patch.object(scenarios.subprocess,'run',return_value=
                SimpleNamespace(returncode=1,stderr='TooManyRequests: disruption budget')) as run:
            scenario.maintenance_evict(member,dry_run=True)
            self.assertIn('/eviction?dryRun=All',run.call_args.args[0][-3])
            self.assertEqual(json.loads(run.call_args.kwargs['input'])['deleteOptions']['preconditions'],{'uid':'owned'})

    def test_maintenance_cannot_uncordon_a_foreign_node_or_change_its_label(self):
        scenario=scenarios.Scenario(ROOT)
        journal={'node':'mongodb-node-212','node_uid':'owned'}
        for uid,labels in [('foreign',{'kubeauto.io/mongodb-maintenance':scenario.prefix}),('owned',{})]:
            with patch.object(scenario.lab,'get',return_value={'metadata':{'uid':uid,'labels':labels}}), \
                    patch.object(scenario.lab,'run') as run:
                with self.assertRaisesRegex(ValueError,'node identity changed'):scenario.recover_maintenance_node(journal)
                run.assert_not_called()

    def test_quorum_closes_existing_connections_only_in_uid_owned_isolated_containers(self):
        scenario = scenarios.Scenario(ROOT)
        member = {'name': 'db-0', 'uid': 'owned'}
        pod = {'metadata': {'uid': 'owned', 'labels': {'kubeauto.io/mongodb-quorum-fault': scenario.prefix}},
               'status': {'containerStatuses': [{'name': 'mongod', 'restartCount': 2, 'state': {'running': {}}}]}}
        restarted = copy.deepcopy(pod)
        restarted['status']['containerStatuses'][0]['restartCount'] = 3
        with patch.object(scenario.lab, 'get', side_effect=[pod, pod, restarted]), \
                patch.object(scenarios.subprocess, 'run', return_value=SimpleNamespace(returncode=1)) as run, \
                patch.object(scenarios.time, 'sleep'):
            scenario.restart_isolated_members([member])
        command = run.call_args.args[0]
        self.assertEqual(command[-3:], ['sh', '-ec', 'test "$(cat /proc/1/comm)" = mongod; kill -TERM 1'])
        self.assertIn('db-0', command)
        for replacement in (pod | {'metadata': {'uid': 'foreign', 'labels': pod['metadata']['labels']}},
                            pod | {'metadata': {'uid': 'owned', 'labels': {}}}):
            with patch.object(scenario.lab, 'get', return_value=replacement), \
                    patch.object(scenarios.subprocess, 'run') as run:
                with self.assertRaisesRegex(ValueError, 'changed isolated member'):
                    scenario.restart_isolated_members([member])
                run.assert_not_called()

    def test_quorum_cannot_treat_signal_return_as_replication_socket_reset(self):
        scenario = scenarios.Scenario(ROOT)
        member = {'name': 'db-0', 'uid': 'owned'}
        pod = {'metadata': {'uid': 'owned', 'labels': {'kubeauto.io/mongodb-quorum-fault': scenario.prefix}},
               'status': {'containerStatuses': [{'name': 'mongod', 'restartCount': 0, 'state': {'running': {}}}]}}
        with patch.object(scenario.lab, 'get', return_value=pod), \
                patch.object(scenarios.subprocess, 'run', return_value=SimpleNamespace(returncode=0)), \
                patch.object(scenarios.time, 'monotonic', side_effect=[0, 91]):
            with self.assertRaisesRegex(ValueError, 'did not observe a mongod restart'):
                scenario.restart_isolated_members([member])

    def test_security_requires_unauthorized_exception_for_each_privilege_boundary(self):
        code = scenarios.SECURITY
        self.assertIn("catch(e){assert.eq('MongoServerError',e.name);assert.eq(13,e.code);rejected=true;}", code)
        self.assertIn('assert(rejected);', code)
        self.assertIn("unauthorized(()=>c.getDB('admin').runCommand({replSetGetStatus:1}))", code)
        self.assertIn("unauthorized(()=>c.getDB('forbidden_database').runCommand({find:'private'}))", code)
        self.assertIn("unauthorized(()=>anonymous.getDB('appdb').runCommand({find:col}))", code)

    def test_transactions_require_mongosh_write_concern_exception_and_bounded_refusal(self):
        # mongosh 2.10 runCommandWithCheck throws MongoWriteConcernError;
        # a successful command or another exception must never satisfy refusal.
        code = scenarios.TRANSACTIONS.split("globalThis.mongodbGateStage='transaction-write-concern';", 1)[1]
        self.assertIn('try {d.runCommand(', code)
        self.assertIn("catch(e){assert.eq('MongoWriteConcernError',e.name);assert.eq(100,e.code);refused=true;}", code)
        self.assertIn('assert(refused);', code)
        self.assertIn('assert(Date.now()-start<5000);', code)
        self.assertNotIn('impossible.writeConcernError', code)

    def test_quorum_isolation_excludes_victims_from_allow_and_blocks_both_directions(self):
        policy = {'metadata': {'namespace': 'mongodb-gate'}, 'spec': {
            'podSelector': {'matchLabels': {'app.kubernetes.io/instance': 'mongodb-gate-data'}},
            'policyTypes': ['Ingress'], 'ingress': [{'from': [{'podSelector': {}}]}]}}
        original = copy.deepcopy(policy)
        modified, deny = scenarios.quorum_policies(policy, 'mongodb_gate_01234567')
        self.assertEqual(policy, original)
        self.assertEqual(modified['ingress'], policy['spec']['ingress'])
        expression = modified['podSelector']['matchExpressions'][0]
        self.assertEqual(expression['operator'], 'NotIn')
        self.assertEqual(expression['values'], ['mongodb_gate_01234567'])
        self.assertEqual(deny['spec']['podSelector']['matchLabels'], {expression['key']: expression['values'][0]})
        self.assertEqual(set(deny['spec']['policyTypes']), {'Ingress', 'Egress'})
        self.assertEqual(deny['spec']['ingress'], [])
        self.assertEqual(deny['spec']['egress'], [])
        with self.assertRaises(ValueError):
            scenarios.quorum_policies(policy, 'foreign')

    def test_quorum_refuses_an_additional_policy_before_injecting_fault(self):
        scenario = scenarios.Scenario(ROOT)
        with patch.object(scenario.lab, 'get', return_value={'items': [{}, {}]}), \
                patch.object(scenario, 'js') as js, patch.object(scenarios.subprocess, 'run') as run:
            with self.assertRaisesRegex(ValueError, 'sole product-owned'):
                scenario.quorum()
        js.assert_not_called()
        run.assert_not_called()

    def test_quorum_failed_creation_still_restores_and_checks_committed_data(self):
        scenario = scenarios.Scenario(ROOT)
        policy = {'metadata': {'namespace': scenario.ns, 'name': scenario.fixture.NAME + '-access',
                              'uid': 'allow-policy', 'labels': scenario.product.LABELS},
                  'spec': {'podSelector': {'matchLabels': {}}, 'policyTypes': ['Ingress'], 'ingress': []}}
        pods = [{'metadata': {'name': 'db-' + str(i), 'uid': 'uid-' + str(i), 'labels': {
            'app.kubernetes.io/instance': scenario.fixture.NAME, 'app.kubernetes.io/replset': 'rs0'}}} for i in range(3)]
        def get(kind, name='', ns=''):
            if kind == 'networkpolicies': return {'items': [policy]}
            if kind == 'pods': return {'items': pods}
            if kind == 'pod': return next(p for p in pods if p['metadata']['name'] == name)
            if kind == 'networkpolicy': return policy if name == policy['metadata']['name'] else None
            raise AssertionError(kind)
        with tempfile.TemporaryDirectory() as tmp:
            scenario.lab.directory = Path(tmp)
            def create(*args, **kwargs):
                state = json.loads(Path(tmp, 'mongodb-fault-quorum.json').read_text())
                self.assertEqual(state['cluster_uid'], 'owned-core')
                self.assertEqual(state['members'], [{'name': 'db-0', 'uid': 'uid-0'}, {'name': 'db-1', 'uid': 'uid-1'}])
                return SimpleNamespace(returncode=1)
            with patch.object(scenario.lab, 'get', side_effect=get), \
                    patch.object(scenario.lab, 'verify', return_value={'cluster_uid': 'owned-core'}), \
                    patch.object(scenario, 'js') as js, patch.object(scenario, 'wait_health') as health, \
                    patch.object(scenarios.subprocess, 'run', side_effect=create):
                with self.assertRaisesRegex(ValueError, 'deny fixture creation failed'):
                    scenario.quorum()
            health.assert_called_once()
            self.assertIn('MONGODB_QUORUM_RECOVERY_PASS', js.call_args.args[1])
            self.assertFalse(Path(tmp, 'mongodb-fault-quorum.json').exists())

    def test_quorum_assertion_failure_restores_policy_labels_and_uid_owned_deny(self):
        scenario = scenarios.Scenario(ROOT)
        policy = {'metadata': {'namespace': scenario.ns, 'name': scenario.fixture.NAME + '-access',
                              'uid': 'allow-policy', 'labels': scenario.product.LABELS},
                  'spec': {'podSelector': {'matchLabels': {}}, 'policyTypes': ['Ingress'], 'ingress': []}}
        original = copy.deepcopy(policy)
        pods = [{'metadata': {'name': 'db-' + str(i), 'uid': 'uid-' + str(i), 'labels': {
            'app.kubernetes.io/instance': scenario.fixture.NAME, 'app.kubernetes.io/replset': 'rs0'}}} for i in range(3)]
        fixtures = {}
        def get(kind, name='', ns=''):
            if kind == 'networkpolicies': return {'items': [copy.deepcopy(policy)]}
            if kind == 'pods': return {'items': copy.deepcopy(pods)}
            if kind == 'pod': return copy.deepcopy(next(p for p in pods if p['metadata']['name'] == name))
            if kind == 'networkpolicy': return copy.deepcopy(policy if name == policy['metadata']['name'] else fixtures.get(name))
            raise AssertionError(kind)
        def api(*args, **kwargs):
            if 'exec' in args: return '{"ok":1,"isWritablePrimary":false}'
            self.assertEqual(args[2], 'patch')
            kind, name, operations = args[3], args[4], json.loads(args[-1])
            obj = policy if kind == 'networkpolicy' else next(p for p in pods if p['metadata']['name'] == name)
            self.assertEqual(operations[0]['value'], obj['metadata']['uid'])
            for op in operations[1:]:
                if op['path'] == '/spec':
                    if op['op'] == 'test': self.assertEqual(obj['spec'], op['value'])
                    else: obj['spec'] = op['value']
                else:
                    key = op['path'].split('/')[-1].replace('~1', '/')
                    if op['op'] == 'test': self.assertEqual(obj['metadata']['labels'][key], op['value'])
                    elif op['op'] == 'add': obj['metadata']['labels'][key] = op['value']
                    else: obj['metadata']['labels'].pop(key)
            return ''
        def submit(command, **kwargs):
            if 'create' in command:
                obj = json.loads(kwargs['input']);obj['metadata']['uid'] = 'deny-policy'
                fixtures[obj['metadata']['name']] = obj
                return SimpleNamespace(returncode=0, stdout=json.dumps(obj))
            self.assertEqual(json.loads(kwargs['input'])['preconditions'], {'uid': 'deny-policy'})
            fixtures.clear()
            return SimpleNamespace(returncode=0)
        with tempfile.TemporaryDirectory() as tmp:
            scenario.lab.directory = Path(tmp)
            with patch.object(scenario.lab, 'get', side_effect=get), \
                    patch.object(scenario.lab, 'run', side_effect=api), \
                    patch.object(scenario.lab, 'verify', return_value={'cluster_uid': 'owned-core'}), \
                    patch.object(scenario, 'js', side_effect=[None, ValueError('first-refusal-assertion'), None]), \
                    patch.object(scenario, 'wait_health'), patch.object(scenario, 'restart_isolated_members') as reset, \
                    patch.object(scenarios.subprocess, 'run', side_effect=submit):
                with self.assertRaisesRegex(ValueError, 'first-refusal-assertion'):
                    scenario.quorum()
            self.assertFalse(Path(tmp, 'mongodb-fault-quorum.json').exists())
            reset.assert_called_once()
        self.assertEqual(policy, original)
        self.assertFalse(fixtures)
        self.assertTrue(all('kubeauto.io/mongodb-quorum-fault' not in p['metadata']['labels'] for p in pods))

    def test_network_label_changes_are_atomic_against_the_owned_client_uid(self):
        scenario = scenarios.Scenario(ROOT)
        with self.assertRaisesRegex(ValueError, 'recorded client UID'):
            scenario.client_label(False)
        scenario.client_uid = 'owned-client'
        with patch.object(scenario.lab, 'run') as run:
            for allowed in (False, True):
                scenario.client_label(allowed)
                patch_body = json.loads(run.call_args.args[-1])
                self.assertEqual(patch_body[0], {'op': 'test', 'path': '/metadata/uid', 'value': 'owned-client'})
                self.assertEqual(patch_body[1]['path'], '/metadata/labels/kubeauto.io~1mongodb-client')
                self.assertEqual(patch_body[1]['op'], 'add' if allowed else 'remove')

    def test_network_denial_failure_restores_label_and_uses_an_unauthenticated_probe(self):
        scenario = scenarios.Scenario(ROOT)
        policy = {'metadata': {'labels': scenario.product.LABELS}, 'spec': {'policyTypes': ['Ingress']}}
        with patch.object(scenario.lab, 'get', return_value=policy), patch.object(scenario, 'client_label') as label, \
                patch.object(scenario, 'js', side_effect=[None, ValueError('first-network-failure')]) as js:
            with self.assertRaisesRegex(ValueError, 'first-network-failure'):
                scenario.network_client()
        self.assertEqual([call.args for call in label.call_args_list], [(False,), (True,)])
        self.assertFalse(js.call_args_list[1].kwargs['authenticate'])

    def test_zero_exit_without_business_marker_is_failure_and_credentials_are_not_relayed(self):
        scenario = scenarios.Scenario(ROOT)
        for rc, stdout in ((0, ''), (1, 'EXPECTED\n')):
            with patch.object(scenarios.subprocess, 'run', return_value=SimpleNamespace(
                    returncode=rc, stdout=stdout, stderr='password=must-never-be-reported')) as run, \
                    patch.object(scenario, 'save_client_failure') as diagnostics:
                with self.assertRaisesRegex(ValueError, 'business assertion failed') as error:
                    scenario.js('print("EXPECTED")', 'EXPECTED')
                diagnostics.assert_called_once()
            self.assertNotIn('must-never', str(error.exception))
            self.assertEqual(run.call_args.args[0][-2:], ['--file', '/dev/stdin'])
            self.assertIn('--nodb', run.call_args.args[0])
            self.assertEqual(run.call_args.kwargs['timeout'], 180)
            self.assertNotIn('APP_PASSWORD', ' '.join(run.call_args.args[0]))

    def test_business_failure_reports_only_controlled_stage_type_code_and_location(self):
        scenario = scenarios.Scenario(ROOT)
        output = 'MONGODB_CLIENT_ERROR ' + json.dumps({'stage': 'transaction-write-concern',
            'name': 'MongoWriteConcernError', 'code': 100, 'line': 41,
            'message': 'password=must-never-be-reported'})
        with patch.object(scenarios.subprocess, 'run', return_value=SimpleNamespace(
                returncode=1, stdout=output, stderr='password=must-never-be-reported')) as run, \
                patch.object(scenario, 'save_client_failure'):
            with self.assertRaisesRegex(ValueError, 'stage=transaction-write-concern error=MongoWriteConcernError code=100 line=41') as error:
                scenario.js('throw Error("private");', 'EXPECTED')
        self.assertNotIn('must-never', str(error.exception))
        self.assertIn('catch(e)', run.call_args.kwargs['input'])
        for value in ({'stage': [], 'name': 'Error'}, {'stage': 'setup', 'name': 'password=secret'},
                      {'stage': 'password=secret', 'name': 'Error'}, [], {'stage': 'setup', 'name': 'Error', 'code': 'secret'}):
            suffix = scenarios.client_error('MONGODB_CLIENT_ERROR ' + json.dumps(value))
            self.assertNotIn('secret', suffix)
        self.assertEqual(scenarios.client_error('MONGODB_CLIENT_ERROR invalid'), '')
        for stage in ('sharding-scale-donor-stats','sharding-scale-donor-empty','sharding-scale-donor-drop'):
            suffix=scenarios.client_error('MONGODB_CLIENT_ERROR '+json.dumps({
                'stage':stage,'name':'Error','line':248,'message':'password=secret'}))
            self.assertEqual(suffix,' stage='+stage+' error=Error line=248')

    def test_client_diagnostics_preserve_parser_errors_and_redact_all_secret_values(self):
        scenario = scenarios.Scenario(ROOT)
        secret = 'private-credential-123'
        encoded = base64.b64encode(secret.encode()).decode()
        with tempfile.TemporaryDirectory() as tmp:
            destination = Path(tmp, 'kubeauto-mongodb-install.client-test');destination.mkdir(mode=0o700)
            with patch.object(scenario.lab, 'get', return_value={'items': [{'data': {'key': encoded}}]}), \
                    patch.object(scenarios.tempfile, 'mkdtemp', return_value=str(destination)):
                scenario.save_client_failure(SimpleNamespace(stdout='SyntaxError: parser failure',
                    stderr=secret+' '+encoded+' mongodb://user:pass@host password=hidden'))
            output = destination.joinpath('client-error.txt').read_text()
            self.assertIn('SyntaxError: parser failure', output)
            for value in [secret, encoded, 'user:pass', 'hidden']:
                self.assertNotIn(value, output)
            self.assertEqual(destination.joinpath('client-error.txt').stat().st_mode & 0o777, 0o600)

    def test_changed_client_uid_blocks_cleanup_before_data_or_pod_deletion(self):
        scenario = scenarios.Scenario(ROOT)
        scenario.client_uid = 'owned-client'
        with patch.object(scenario.lab, 'get', return_value={'metadata': {'uid': 'foreign-client'}}), \
                patch.object(scenario, 'js') as js, patch.object(scenario.lab, 'run') as run:
            with self.assertRaisesRegex(ValueError, 'ownership changed'):
                scenario.cleanup()
        js.assert_not_called()
        run.assert_not_called()

    def test_create_response_interruption_can_clean_only_matching_run_label(self):
        scenario = scenarios.Scenario(ROOT)
        scenario.client_attempted = True
        pod = {'metadata': {'uid': 'created-before-interruption', 'labels': {'kubeauto.io/test-run': scenario.prefix}}}
        with patch.object(scenario.lab, 'get', side_effect=[pod, pod, None]), \
                patch.object(scenario, 'js') as js, patch.object(scenario.lab, 'run'), \
                patch.object(scenarios.subprocess, 'run', return_value=SimpleNamespace(returncode=0)) as run:
            scenario.cleanup()
        js.assert_not_called()
        self.assertIn('delete', run.call_args.args[0])
        self.assertEqual(json.loads(run.call_args.kwargs['input'])['preconditions']['uid'], 'created-before-interruption')

    def test_fault_refuses_foreign_primary_unknown_role_and_existing_journal(self):
        scenario = scenarios.Scenario(ROOT)
        pods = [{'metadata': {'name': 'db-' + str(i), 'uid': 'uid-' + str(i), 'labels': {
            'app.kubernetes.io/instance': scenario.fixture.NAME, 'app.kubernetes.io/replset': 'rs0'}}} for i in range(3)]
        with tempfile.TemporaryDirectory() as tmp, patch.object(scenario.lab, 'get', return_value={'items': pods}), \
                patch.object(scenarios.subprocess, 'run') as run:
            scenario.lab.directory = Path(tmp)
            for primary, role in [('foreign.svc', 'primary'), ('db-0.svc', 'all')]:
                with self.assertRaises(ValueError):
                    scenario.fault_member(primary, role)
            Path(tmp, 'mongodb-fault-failover.json').write_text('{}')
            with self.assertRaisesRegex(ValueError, 'journal requires recovery'):
                scenario.fault_member('db-0.svc', 'primary')
        run.assert_not_called()

    def test_fault_journal_is_durable_before_delete_and_uid_is_precondition(self):
        scenario = scenarios.Scenario(ROOT)
        scenario.client_uid = 'owned-client'
        pods = [{'metadata': {'name': 'db-' + str(i), 'uid': 'uid-' + str(i), 'labels': {
            'app.kubernetes.io/instance': scenario.fixture.NAME, 'app.kubernetes.io/replset': 'rs0'}}} for i in range(3)]
        with tempfile.TemporaryDirectory() as tmp:
            scenario.lab.directory = Path(tmp)
            path = Path(tmp, 'mongodb-fault-failover.json')
            def delete(command, **kwargs):
                journal = json.loads(path.read_text())
                self.assertEqual(journal['uid'], 'uid-0')
                self.assertEqual(journal['cluster_uid'], 'owned-cluster')
                self.assertEqual(journal['run_id'], scenario.prefix)
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                self.assertEqual(json.loads(kwargs['input'])['preconditions'], {'uid': 'uid-0'})
                self.assertEqual(command[-3:], ['/api/v1/namespaces/mongodb-gate/pods/db-0', '-f', '-'])
                return SimpleNamespace(returncode=1)
            with patch.object(scenario.lab, 'get', return_value={'items': pods}), \
                    patch.object(scenario.lab, 'verify', return_value={'cluster_uid': 'owned-cluster'}), \
                    patch.object(scenarios.subprocess, 'run', side_effect=delete):
                with self.assertRaisesRegex(ValueError, 'fault request failed'):
                    scenario.fault_member('db-0.svc', 'primary')
            self.assertTrue(path.exists())

    def test_worker_timeout_stops_remote_pod_before_killing_local_exec(self):
        from unittest.mock import Mock
        scenario = scenarios.Scenario(ROOT)
        worker = scenario.worker = Mock()
        worker.poll.return_value = None
        worker.communicate.side_effect = [scenarios.subprocess.TimeoutExpired('exec', 30), (b'', b'')]
        order = []
        worker.kill.side_effect = lambda: order.append('kill-local')
        with patch.object(scenario.lab, 'run'), \
                patch.object(scenario, 'delete_client', side_effect=lambda: order.append('delete-remote')), \
                patch.object(scenario, 'client_create', side_effect=lambda: order.append('recreate-cleanup-client')):
            scenario.stop_worker()
        self.assertEqual(order, ['delete-remote', 'kill-local', 'recreate-cleanup-client'])
        self.assertIsNone(scenario.worker)

    def test_assertion_failure_and_interrupt_cleanup_preserve_the_first_error(self):
        for error in (ValueError('first assertion'), KeyboardInterrupt()):
            scenario = scenarios.Scenario(ROOT)
            with patch.object(scenario.lab, 'verify', return_value={'healthy': True}), \
                    patch.object(scenario, 'health'), patch.object(scenario, 'client_create'), \
                    patch.object(scenario, 'js', side_effect=error), patch.object(scenario, 'cleanup') as cleanup:
                with self.assertRaises(type(error)):
                    scenario.execute('data')
            cleanup.assert_called_once()

    def test_idempotence_snapshot_ignores_bookkeeping_but_detects_rollouts_restarts_and_secret_changes(self):
        objects = {'pods': [{'metadata': {'name': 'db-0', 'uid': 'pod'}, 'spec': {'nodeName': 'node'},
                             'status': {'containerStatuses': [{'name': 'mongod', 'imageID': 'sha256:pinned', 'restartCount': 0}]}}],
                   'secrets': [{'metadata': {'name': 'auth', 'uid': 'secret'}, 'data': {'password': 'encoded-only'}},
                               {'metadata': {'name': 'helm-v1', 'uid': 'helm'}, 'type': 'helm.sh/release.v1'}]}
        before = scenarios.canonical_snapshot(objects)
        copy_objects = copy.deepcopy(objects)
        copy_objects['pods'][0]['metadata']['resourceVersion'] = '999'
        copy_objects['secrets'][1]['metadata']['name'] = 'helm-v2'
        self.assertEqual(before, scenarios.canonical_snapshot(copy_objects))
        for change in ('uid', 'restart', 'secret'):
            other = copy.deepcopy(objects)
            if change == 'uid':
                other['pods'][0]['metadata']['uid'] = 'replacement'
            elif change == 'restart':
                other['pods'][0]['status']['containerStatuses'][0]['restartCount'] = 1
            else:
                other['secrets'][0]['data']['password'] = 'different'
            self.assertNotEqual(before, scenarios.canonical_snapshot(other))
        self.assertNotIn('encoded-only', str(before))
