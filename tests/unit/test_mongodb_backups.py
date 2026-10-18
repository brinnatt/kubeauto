"""Backup fixture TLS, immutable requests, private metadata and recovery scope."""
import importlib.util
import base64
import copy
import hashlib
import json
import os
from contextlib import ExitStack, nullcontext, redirect_stdout
from io import StringIO
from pathlib import Path
import tempfile
import threading
import unittest
import subprocess
from types import SimpleNamespace
from unittest.mock import patch

from cryptography import x509
from cryptography.hazmat.primitives.asymmetric import padding
import yaml

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('mongodb_backups', ROOT / 'tests/helpers/mongodb-backups.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class MongoDBBackupTests(unittest.TestCase):
    def test_completed_upgrade_recovery_checks_forward_identity_without_a_deleted_backup_journal(self):
        for drift in (None, 'source', 'config', 'schema', 'backup', 'binary'):
            with self.subTest(drift=drift), tempfile.TemporaryDirectory() as tmp:
                g=self.gate(tmp);g.lab.owner_path=Path(tmp,'owner.json');events=[]
                old={'mongodb_operator_ver':'1.23.0','mongodb_operator_chart_ver':'1.23.1',
                     'mongodb_ver':'8.0.26-11','mongodb_fluentbit_ver':'5.0.9-1'}
                applied=old|{'mongodb_operator_ver':'1.23.1','mongodb_operator_chart_ver':'1.23.2',
                             'mongodb_ver':'8.0.32-14','mongodb_fluentbit_ver':'5.1.2-1','mongodb_crd_upgrade':False}
                source={'cr_uid':'source'};new={'crd':{'uid':'original','versions_sha256':'new'}}
                q={'stage':'post-upgrade-backup-restored','cluster_uid':'core','client_uid':None,
                   'source':source,'original_config':old,'applied_config':applied,
                   'old_crds':{'crd':{'uid':'original','versions_sha256':'old'}},'new_crds':new,
                   'workload':'mongodb_gate_'+'a'*16}
                journal=g.lab.directory/'mongodb-upgrade.json';g.scenario.fixture.write_private(journal,q)
                g.config_path.write_text(yaml.safe_dump(old if drift=='config' else applied))
                if drift=='backup':g.path.write_text('{}')
                with ExitStack() as stack:
                    setup=stack.enter_context(patch.object(g,'setup_product'))
                    cleanup=stack.enter_context(patch.object(g,'cleanup'))
                    for ctx in (patch.object(g.lab,'verify',return_value={'cluster_uid':'core','healthy':True}),
                        patch.object(g,'source_identity',return_value={} if drift=='source' else source),
                        patch.object(g,'upgrade_crds',return_value={} if drift=='schema' else new),
                        patch.object(g.scenario,'health'),patch.object(g.scenario,'client_create'),
                        patch.object(g.scenario,'delete_client'),
                        patch.object(g,'upgrade_versions',side_effect=ValueError('binary') if drift=='binary' else None),
                        patch.object(g.lab,'bind_product',side_effect=lambda:events.append('bind-construction')),
                        patch.object(g,'bind_upgrade_crds',side_effect=lambda q:events.append('bind-forward')),
                        patch.object(g.lab,'cleanup_product',side_effect=lambda:events.append('retire'))):
                        stack.enter_context(ctx)
                    if drift:
                        with self.assertRaises(ValueError):g.upgrade_recover()
                        self.assertNotIn('retire',events);self.assertTrue(journal.exists())
                    else:
                        g.upgrade_recover();self.assertFalse(journal.exists())
                        self.assertEqual(events,['bind-construction','bind-forward','retire'])
                    setup.assert_not_called();cleanup.assert_not_called()

    def test_forward_upgrade_recovery_removes_only_proven_new_product_without_downgrade(self):
        for drift in (None, 'schema', 'source', 'receipt', 'config', 'binary'):
            with self.subTest(drift=drift), tempfile.TemporaryDirectory() as tmp:
                g = self.gate(tmp)
                g.lab.owner_path = Path(tmp) / 'mongodb-lab-owner.json'
                old_config = {'mongodb_operator_ver':'1.23.0','mongodb_operator_chart_ver':'1.23.1',
                              'mongodb_ver':'8.0.26-11','mongodb_fluentbit_ver':'5.0.9-1'}
                new_config = old_config | {'mongodb_operator_ver':'1.23.1','mongodb_operator_chart_ver':'1.23.2',
                    'mongodb_ver':'8.0.32-14','mongodb_fluentbit_ver':'5.1.2-1','mongodb_crd_upgrade':False}
                old = {'crd':{'uid':'crd-uid','versions_sha256':'old'}}
                new = {'crd':{'uid':'crd-uid','versions_sha256':'new'}}
                source = {'cr_uid':'source-uid'}
                owner = {'cluster_uid':'core','healthy':True,'product_identity':{
                    'source_uid':'source-uid','crds':{} if drift=='receipt' else old}}
                q = {'cluster_uid':'core','stage':'server','client_uid':None,'source':source,
                    'old_crds':old,'new_crds':new,'original_config':old_config,'applied_config':new_config,
                    'workload':'mongodb_gate_'+'a'*16}
                journal=g.lab.directory/'mongodb-upgrade.json';g.scenario.fixture.write_private(journal,q)
                g.scenario.fixture.write_private(g.path,{'cluster_uid':'core'})
                g.config_path.write_text(yaml.safe_dump(old_config if drift=='config' else new_config))
                events=[]
                def setup():
                    self.assertEqual(yaml.safe_load(g.config_path.read_text()),new_config)
                    events.append('product-forward')
                def retire():
                    self.assertEqual(json.loads(g.lab.owner_path.read_text())['product_identity']['crds'],new)
                    events.append('retire-forward')
                with ExitStack() as stack:
                    for ctx in (patch.object(g.lab,'verify',return_value=owner),
                        patch.object(g,'source_identity',return_value={} if drift=='source' else source),
                        patch.object(g,'upgrade_crds',return_value=old if drift=='schema' else new),
                        patch.object(g,'validate_identity'),patch.object(g.scenario,'health'),
                        patch.object(g.scenario,'client_create'),patch.object(g.scenario,'delete_client'),
                        patch.object(g.scenario,'clean_workload'),patch.object(g,'verify_objects'),
                        patch.object(g,'upgrade_versions',side_effect=ValueError('binary') if drift=='binary' else None),
                        patch.object(g,'setup_product',side_effect=setup),patch.object(g,'cleanup'),
                        patch.object(g.lab,'cleanup_product',side_effect=retire)):
                        stack.enter_context(ctx)
                    if drift:
                        with self.assertRaises(ValueError):g.upgrade_recover()
                        self.assertNotIn('retire-forward',events);self.assertTrue(journal.exists())
                    else:
                        g.upgrade_recover();self.assertEqual(events,['product-forward','retire-forward'])
                        self.assertFalse(journal.exists())

    def test_upgrade_recovery_restores_only_an_unapplied_operator_stage(self):
        for drift in (None, 'stage', 'configuration', 'source', 'crd', 'binary', 'backup'):
            with self.subTest(drift=drift), tempfile.TemporaryDirectory() as tmp:
                g = self.gate(tmp)
                old = {'mongodb_operator_ver': '1.23.0', 'mongodb_operator_chart_ver': '1.23.1',
                       'mongodb_ver': '8.0.26-11', 'mongodb_fluentbit_ver': '5.0.9-1', 'mongodb_crd_upgrade': False}
                applied = old | {'mongodb_operator_ver': '1.23.1',
                                 'mongodb_operator_chart_ver': '1.23.2', 'mongodb_crd_upgrade': True}
                source = {'uid': 'original-source'}
                crds = {'name': {'uid': 'original-crd', 'versions_sha256': 'old'}}
                q = {'cluster_uid': 'core', 'source': source, 'old_crds': crds,
                     'workload': 'mongodb_gate_' + 'a'*16, 'client_uid': None,
                     'original_config': old, 'applied_config': applied,
                     'stage': 'server' if drift == 'stage' else 'operator'}
                journal = g.lab.directory / 'mongodb-upgrade.json'
                g.scenario.fixture.write_private(journal, q)
                g.scenario.fixture.write_private(g.path, {'cluster_uid': 'core'})
                g.config_path.write_text(yaml.safe_dump(applied | ({'foreign': True} if drift == 'configuration' else {})))
                mutations = []
                def setup():
                    self.assertEqual(yaml.safe_load(g.config_path.read_text()), old)
                    mutations.append('product')
                def cleanup():mutations.append('backup-clean')
                with ExitStack() as stack:
                    for context in (patch.object(g, 'validate_identity'),
                        patch.object(g.lab, 'verify', return_value={'cluster_uid': 'core', 'healthy': True}),
                        patch.object(g, 'source_identity', return_value={'uid': 'foreign'} if drift == 'source' else source),
                        patch.object(g, 'upgrade_crds', return_value={'changed': True} if drift == 'crd' else crds),
                        patch.object(g, 'upgrade_versions', side_effect=ValueError('binary') if drift == 'binary' else None),
                        patch.object(g, 'verify_objects', side_effect=ValueError('backup') if drift == 'backup' else None),
                        patch.object(g.scenario, 'health'), patch.object(g.scenario, 'client_create'),
                        patch.object(g.scenario, 'clean_workload'), patch.object(g.scenario, 'delete_client'),
                        patch.object(g, 'setup_product', side_effect=setup), patch.object(g, 'cleanup', side_effect=cleanup)):
                        stack.enter_context(context)
                    if drift:
                        with self.assertRaises(ValueError):g.upgrade_recover()
                        self.assertTrue(journal.exists());self.assertEqual(mutations, [])
                    else:
                        g.upgrade_recover()
                        self.assertFalse(journal.exists());self.assertEqual(mutations, ['product', 'backup-clean'])

    def assert_client_syntax(self, gate, code, marker, **kwargs):
        with patch.object(gate.scenario_module.subprocess,'run',return_value=SimpleNamespace(
                returncode=0,stdout=marker+'\n',stderr='')) as client_run:
            type(gate.scenario).js(gate.scenario,code,marker,**kwargs)
        subprocess.run([module.shutil.which('node'),'--check'],input=client_run.call_args.kwargs['input'],
            capture_output=True,text=True,check=True)

    def test_cleaned_stability_target_retires_rebooted_pods_only_under_original_statefulset(self):
        for drift in (None,'active-data','client','topology','controller','controller-name','two-owners','statefulset','secret'):
            with self.subTest(drift=drift),tempfile.TemporaryDirectory() as tmp:
                g=self.gate(tmp);name='mongodb-gate-logical-restore';owner=name+'-cfg';podname=owner+'-2'
                def meta(n,uid):return {'name':n,'uid':uid,'namespace':'mongodb-gate',
                    'labels':module.LABELS|{'kubeauto.io/test-run':g.state['run_id']}}
                cr={'metadata':meta(name,'cr'),'spec':{'secrets':{'users':name+'-users'}}}
                cr['metadata']['annotations']={'kubeauto.io/encryption-key-sha256':'a'*64}
                sts={'metadata':meta(owner,'changed' if drift=='statefulset' else 'controller')}
                pod={'metadata':meta(podname,'new')}
                pod['metadata']['ownerReferences']=[{'kind':'StatefulSet','name':'foreign' if drift=='controller-name' else owner,
                    'uid':'foreign' if drift=='controller' else 'controller','controller':True}]
                if drift=='two-owners':pod['metadata']['ownerReferences']*=2
                secret={'metadata':meta(name+'-users','changed' if drift=='secret' else 'secret')}
                g.state['target']={'name':name,'cr_uid':'cr','key_sha256':'a'*64,'sharded':True,'resources':[
                    {'kind':'statefulset','name':owner,'uid':'controller'},{'kind':'pod','name':podname,'uid':'old'},
                    {'kind':'secret','name':name+'-users','uid':'secret'}]}
                g.state['stability']={'topology':'source' if drift=='topology' else 'sharded',
                    'data_cleaned':drift!='active-data','client_uid':'active' if drift=='client' else None}
                rows={'psmdb':[cr],'statefulset':[sts],'pod':[pod],'secret':[secret]}
                with patch.object(g.lab,'get',side_effect=lambda kind,*a,**kw:cr if a else {'items':rows.get(kind,[])}):
                    if drift:
                        with self.assertRaisesRegex(ValueError,'resource was replaced'):g.capture_target()
                    else:
                        g.capture_target()
                        self.assertEqual(next(r['uid'] for r in g.state['target']['resources'] if r['kind']=='pod'),'new')

    def test_shard_uuid_probe_uses_existing_local_system_monitor_and_rejects_wrong_uuid(self):
        probe=module.SHARD_UUID_VERIFY
        harness="""const metadata={uuid:'catalog-uuid'},dbname='test',targetName='owned';
const process={env:{MONGODB_CLUSTER_MONITOR_USER:'monitor-local',MONGODB_CLUSTER_MONITOR_PASSWORD:'private'}};
let checked=0;const assert=(v)=>{if(!v)throw Error('assert');};
assert.eq=(a,b)=>assert(JSON.stringify(a)===JSON.stringify(b));
assert.commandWorked=r=>{assert(r.ok===1);return r;};
class Mongo{constructor(uri){assert(uri.includes('directConnection=true'));assert(uri.includes('tls=true'));assert(!uri.includes('tlsAllowInvalid'));}
getDB(name){return {auth:q=>{assert.eq('admin',name);assert.eq({user:'monitor-local',pwd:'private'},q);if(MODE==='auth-refused')throw Error('auth');return true;},
runCommand:q=>{assert.eq(dbname,name);assert.eq({listCollections:1,filter:{name:'documents'}},q);checked++;
return {ok:1,cursor:{firstBatch:[{info:{uuid:MODE==='wrong-uuid'?'wrong':'catalog-uuid'}}]}};}};}}
"""
        for mode in ('normal','auth-refused','wrong-uuid'):
            with self.subTest(mode=mode):
                script='const MODE='+json.dumps(mode)+';'+harness+probe+';assert.eq(2,checked);'
                result=subprocess.run([module.shutil.which('node'),'-e',script],capture_output=True,text=True)
                self.assertEqual(result.returncode==0,mode=='normal')

    def test_target_capture_preserves_backup_and_restore_as_distinct_immutable_requests(self):
        from tests.unit.test_mongodb_delivery import configuration
        for legacy in (False,True):
            for drift in (None,'spec','annotation','uid','destination'):
                with self.subTest(legacy=legacy,drift=drift),tempfile.TemporaryDirectory() as tmp:
                    g=self.gate(tmp);g.base=Path(tmp)
                    name='mongodb-gate-logical-restore';request_name=name+'-'+g.state['run_id']
                    config=configuration()|{'mongodb_name':name,'mongodb_namespace':'mongodb-gate','mongodb_backup_enabled':True}
                    directory=Path(tmp,'clusters/mongodb-restore-logical');directory.mkdir(parents=True)
                    (directory/'config.yml').write_text(yaml.safe_dump(config))
                    key='a'*64;cr={'metadata':{'name':name,'uid':'11111111-1111-1111-1111-111111111111',
                        'namespace':'mongodb-gate','labels':g.scenario.product.LABELS,
                        'annotations':{'kubeauto.io/encryption-key-sha256':key}},'spec':{'secrets':{'users':name+'-users'}}}
                    backup={'kind':'backup','name':request_name,'type':'logical'}
                    restore={'kind':'restore','name':request_name,'cluster_uid':cr['metadata']['uid'],'backup_name':request_name}
                    desired_backup=g.scenario.product.operation_object(config,backup)
                    desired_restore=g.scenario.product.operation_object(config,restore)
                    obj=copy.deepcopy(desired_backup);obj['metadata']['uid']='backup-original'
                    obj['status']={'destination':'s3://owned/backup','state':'ready'}
                    r=copy.deepcopy(desired_restore);r['metadata']['uid']='restore-original'
                    g.state['target']={'name':name,'deployment':'mongodb-restore-logical','key_sha256':key,
                        'cr_uid':cr['metadata']['uid'],'sharded':True,'request':restore,'desired_request':desired_restore,
                        'resources':[{'kind':'psmdb-backup','name':request_name,'uid':'backup-original'}]}
                    g.state['sharding_restore']={'backup':{'destination':'s3://owned/backup'}}
                    if not legacy:g.state['target']['operation_requests']={'psmdb-backup':{'request':backup,'desired':desired_backup}}
                    if drift=='spec':obj['spec']['type']='physical'
                    if drift=='annotation':obj['metadata']['annotations']['kubeauto.io/request-sha256']='foreign'
                    if drift=='uid':obj['metadata']['uid']='replacement'
                    if drift=='destination':obj['status']['destination']='s3://foreign/backup'
                    objects={'psmdb':[cr],'psmdb-backup':[obj],'psmdb-restore':[r]}
                    def get(kind,*args,**kwargs):return cr if args and args[0]==name else {'items':objects.get(kind,[])}
                    with patch.object(g.lab,'get',side_effect=get):
                        if drift and (legacy or drift!='destination'):
                            with self.assertRaises(ValueError):g.capture_target()
                        else:
                            g.capture_target()
                            self.assertEqual({x['kind'] for x in g.state['target']['resources']},
                                             {'psmdb','psmdb-backup','psmdb-restore'})

    def test_upgrade_uses_two_product_changes_continuous_ledger_and_both_backup_restores(self):
        from unittest.mock import Mock
        for failure in (None,'operator','server','data','old-schema','backup-changed','workload-ended'):
            with self.subTest(failure=failure),tempfile.TemporaryDirectory() as tmp:
                g=self.gate(tmp);g.base=Path(tmp);events=[];programs=[]
                config={'mongodb_operator_ver':'1.23.0','mongodb_operator_chart_ver':'1.23.1',
                        'mongodb_ver':'8.0.26-11','mongodb_fluentbit_ver':'5.0.9-1'}
                g.config_path.write_text(yaml.safe_dump(config));g.scenario.worker=Mock();g.scenario.worker.poll.return_value=None
                if failure=='workload-ended':g.scenario.worker.poll.side_effect=[None,0]
                source={'uid':'source'};old={'crd':{'uid':'crd-uid','versions_sha256':'old'}}
                new={'crd':{'uid':'crd-uid','versions_sha256':'old' if failure=='old-schema' else 'new'}}
                def setup():
                    current=yaml.safe_load(g.config_path.read_text());stage='operator' if current['mongodb_ver']=='8.0.26-11' else 'server'
                    events.append(stage)
                    self.assertEqual(current['mongodb_operator_ver'],'1.23.1')
                    self.assertEqual(current['mongodb_crd_upgrade'],stage=='operator')
                    if failure==stage:raise ValueError('first-'+stage)
                def client():g.scenario.client_uid='owned-client'
                def js(code,marker,**kwargs):
                    programs.append((code,marker,kwargs))
                    if failure=='data' and marker=='MONGODB_UPGRADE_DATA_INDEX_PASS':raise ValueError('first-data')
                def phase(settings,*,inject):
                    self.assertEqual(settings,{'phase':'upgrade','concurrency':4,'seconds':3600,'fault':True})
                    inject();events.append('ledger');return {'acknowledged':'verified'}
                performance=Mock();performance.phase.side_effect=phase
                fresh=Mock();fresh.execute.side_effect=lambda:events.append('new-backup')
                fresh.restore_independent.side_effect=lambda:events.append('new-restore')
                fresh.cleanup.side_effect=lambda:events.append('new-clean')
                with ExitStack() as stack,redirect_stdout(StringIO()) as output:
                    for context in (patch.object(g.scenario,'health'),patch.object(g,'upgrade_crds',side_effect=lambda v:old if v=='1.23.1' else new),
                        patch.object(g,'execute',side_effect=lambda:events.append('old-backup')),patch.object(g,'source_identity',return_value=source),
                        patch.object(g,'verify_objects',side_effect=[{'verified':'backup'},{'verified':'changed' if failure=='backup-changed' else 'backup'}]),
                        patch.object(g.scenario,'client_create',side_effect=client),patch.object(g,'upgrade_versions',side_effect=[{'pod':'old'},{'pod':'new'}]),
                        patch.object(g.scenario,'js',side_effect=js),patch.object(g,'setup_product',side_effect=setup),
                        patch.object(g,'upgrade_observe',return_value=nullcontext()),patch.object(g.scenario_module,'Performance',return_value=performance),
                        patch.object(g.scenario,'stop_worker'),patch.object(g.scenario,'wait_health'),patch.object(g.scenario,'clean_workload'),
                        patch.object(g.scenario,'delete_client'),patch.object(g,'diagnostics'),
                        patch.object(g,'restore_independent',side_effect=lambda:events.append('old-restore')),
                        patch.object(g,'cleanup',side_effect=lambda:events.append('old-clean')),patch.object(module,'BackupGate',return_value=fresh),
                        patch.object(g,'bind_upgrade_crds'),patch.object(g.lab,'cleanup_product',side_effect=lambda:events.append('product-clean')),
                        patch.object(module.Path,'unlink',autospec=True)):
                        stack.enter_context(context)
                    if failure:
                        with self.assertRaises(ValueError):g.upgrade()
                        self.assertNotIn('MONGODB_UPGRADE_PASS',output.getvalue());self.assertNotIn('product-clean',events)
                    else:
                        g.upgrade()
                        self.assertEqual(events,['old-backup','operator','server','ledger','old-restore','old-clean',
                                                 'new-backup','new-restore','new-clean','product-clean'])
                        self.assertIn('MONGODB_UPGRADE_PASS',output.getvalue().splitlines())
                        for program,marker,kwargs in programs:
                            self.assert_client_syntax(g,program,marker,**kwargs)

    def test_upgrade_fixed_entry_and_exact_chart_schema_refuse_new_schema_as_old(self):
        script='source tests/helpers/mongodb-runner.sh\nmongodb_launch(){ printf "%s|%s\\n" "$1" "$2"; }\nmongodb_dispatch --mongodb-upgrade'
        r=subprocess.run(['bash','-euc',script],cwd=ROOT,capture_output=True,text=True,check=True)
        self.assertEqual(r.stdout.strip(),'upgrade|MONGODB_UPGRADE_PASS')
        focused = script.replace('--mongodb-upgrade', '--mongodb-upgrade-existing')
        result = subprocess.run(['bash', '-euc', focused], cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), 'upgrade-existing|MONGODB_UPGRADE_PASS')
        installer = ROOT.joinpath('tests/helpers/mongodb-install-gate.sh').read_text()
        branch = installer.split('if [[ "$MODE" == --upgrade-existing ]]; then', 1)[1].split('\nfi', 1)[0]
        with tempfile.TemporaryDirectory() as tmp:
            calls = Path(tmp, 'calls')
            entry = Path(tmp, 'python')
            entry.write_text('#!/bin/bash\nprintf "%s|%s\\n" "$1" "$2" >> "$calls"\n')
            entry.chmod(0o700)
            result = subprocess.run(['bash', '-euc', 'PY=$1; BASE=/product; ' + branch,
                                     'contract', str(entry)], env=os.environ | {'calls': str(calls)},
                                    capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(calls.read_text().splitlines(), [
                '/product/tests/helpers/mongodb-lab-storage.py|health',
                '/product/tests/helpers/mongodb-lab.py|cache',
                '/product/tests/helpers/mongodb-backups.py|upgrade'])
        with patch.object(module.sys,'argv',['mongodb-backups.py','upgrade','--base',str(ROOT)]), \
                patch.object(module.BackupGate,'upgrade') as run,patch.object(module.signal,'signal'):
            self.assertEqual(module.main(),0);run.assert_called_once()
        import tarfile
        for version in ('1.23.1','1.23.2'):
            with tempfile.TemporaryDirectory() as tmp:
                g=self.gate(tmp)
                with tarfile.open(ROOT/'roles/cluster-addon/files'/('psmdb-operator-'+version+'.tgz')) as archive:
                    rows={o['metadata']['name']:o for o in yaml.safe_load_all(archive.extractfile('psmdb-operator/crds/crd.yaml'))}
                for o in rows.values():o['metadata']['uid']='original-'+o['metadata']['name']
                with patch.object(g.lab,'get',side_effect=lambda kind,name:rows[name]):
                    self.assertEqual(set(g.upgrade_crds(version)),set(rows))
                    with self.assertRaisesRegex(ValueError,'actual CRD schema'):g.upgrade_crds('1.23.2' if version=='1.23.1' else '1.23.1')

    def test_upgrade_recovery_fixed_dispatch_reaches_owning_helper(self):
        script = ('source tests/helpers/mongodb-runner.sh\n'
                  'mongodb_launch(){ printf "%s|%s\\n" "$1" "$2"; }\n'
                  'mongodb_dispatch --mongodb-upgrade-recover')
        result = subprocess.run(['bash', '-euc', script], cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), 'upgrade-recover|MONGODB_UPGRADE_RECOVER_PASS')
        installer = ROOT.joinpath('tests/helpers/mongodb-install-gate.sh').read_text()
        # Execute the real case statement, with local stand-ins for its CLI calls.
        branch = installer.split('\ncase "$MODE" in\n', 1)[1].split('\nesac', 1)[0]
        with tempfile.TemporaryDirectory() as tmp:
            calls = Path(tmp, 'calls')
            entry = Path(tmp, 'python')
            entry.write_text('#!/bin/bash\nprintf "%s|%s\\n" "$1" "$2" >> "$calls"\n')
            entry.chmod(0o700)
            result = subprocess.run(['bash', '-euc',
                'MODE=--upgrade-recover; BASE=/product; PY=$1; case "$MODE" in\n' + branch + '\nesac',
                'contract', str(entry)], env=os.environ | {'calls': str(calls)},
                capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(calls.exists(), 'upgrade-recover never reached its owning helper')
            self.assertEqual(calls.read_text().splitlines(), [
                '/product/tests/helpers/mongodb-lab.py|cache',
                '/product/tests/helpers/mongodb-backups.py|upgrade-recover'])
        with patch.object(module.sys, 'argv', ['mongodb-backups.py', 'upgrade-recover', '--base', str(ROOT)]), \
                patch.object(module.BackupGate, 'upgrade_recover') as run, patch.object(module.signal, 'signal'):
            self.assertEqual(module.main(), 0)
            run.assert_called_once()

    def test_upgrade_rolling_observer_refuses_lost_quorum_and_preserves_first_error(self):
        for ready in (1,2,3):
            with self.subTest(ready=ready),tempfile.TemporaryDirectory() as tmp:
                g=self.gate(tmp)
                pods=[{'metadata':{'name':g.scenario.fixture.NAME+'-rs0-'+str(i),'uid':str(i),
                    'labels':{'app.kubernetes.io/instance':g.scenario.fixture.NAME,'app.kubernetes.io/replset':'rs0'}},
                    'spec':{'containers':[{'name':'mongod','image':'pinned'}]}} for i in range(3)]
                with patch.object(g.lab,'get',return_value={'items':pods}), \
                        patch.object(g.scenario.product,'ready',side_effect=lambda p:int(p['metadata']['uid'])<ready),redirect_stdout(StringIO()):
                    if ready==1:
                        with self.assertRaisesRegex(ValueError,'two-ready-member'):
                            with g.upgrade_observe():self.fail('invalid baseline admitted')
                    else:
                        with g.upgrade_observe():pass
                        data=json.loads(Path(tmp,'mongodb-upgrade-observations.json').read_text())
                        self.assertTrue(data);self.assertTrue(all(sum(x['ready'] for x in row['members'])>=2 for row in data))
                        with self.assertRaisesRegex(ValueError,'first-product-error'):
                            with g.upgrade_observe():raise ValueError('first-product-error')

    def test_upgrade_binary_check_requires_each_actual_member_image_and_fcv(self):
        for drift in (None,'operator','server','pod','ready'):
            with self.subTest(drift=drift),tempfile.TemporaryDirectory() as tmp:
                g=self.gate(tmp)
                cr={'spec':{'crVersion':'wrong' if drift=='operator' else '1.23.0',
                    'image':'pinned:'+('wrong' if drift=='server' else '8.0.26-11')}}
                operator={'spec':{'template':{'spec':{'containers':[{'image':'pinned:1.23.0'}]}}}}
                pods=[{'metadata':{'name':'member-'+str(i),'uid':str(i),'labels':{
                    'app.kubernetes.io/instance':g.scenario.fixture.NAME,'app.kubernetes.io/replset':'rs0'}},
                    'spec':{'containers':[{'name':'mongod','image':'pinned:'+('wrong' if drift=='pod' else '8.0.26-11')}]}} for i in range(3)]
                def get(kind,*a,**kw):return cr if kind=='psmdb' else operator if kind=='deployment' else {'items':pods}
                with patch.object(g.lab,'get',side_effect=get),patch.object(g.scenario.product,'ready',return_value=drift!='ready'), \
                        patch.object(g.scenario,'js') as js:
                    if drift:
                        with self.assertRaises(ValueError):g.upgrade_versions('1.23.0','8.0.26-11')
                        js.assert_not_called()
                    else:
                        self.assertEqual(len(g.upgrade_versions('1.23.0','8.0.26-11')),3)
                        program=js.call_args.args[0]
                        self.assertIn('getParameter:1,featureCompatibilityVersion:1',program)
                        self.assertIn('buildInfo:1',program);self.assertIn('i<3',program)
                        self.assert_client_syntax(g,program,js.call_args.args[1],**js.call_args.kwargs)
                        for actual in ('8.0.26-11','8.0.26','8.0.26-10'):
                            harness='''const assert=v=>{if(!v)throw Error('assertion');};
assert.eq=(a,b)=>assert(a===b);assert.commandWorked=r=>{assert(r.ok===1);return r;};
const print=()=>{};let checks=0;
class Mongo{getDB(){return {auth:()=>true,runCommand:q=>q.buildInfo?
 (checks++,{ok:1,version:ACTUAL}):{ok:1,featureCompatibilityVersion:{version:'8.0'}}};}}
'''.replace('ACTUAL',json.dumps(actual))
                            result=subprocess.run([module.shutil.which('node')],input=harness+program+'\nassert.eq(3,checks);',
                                                  text=True,capture_output=True)
                            self.assertEqual(result.returncode==0,actual=='8.0.26-11',actual)

    def test_stability_runs_both_topologies_with_ledger_and_all_three_faults(self):
        from unittest.mock import Mock
        for failure,focused in [(None,False)]+[(f,True) for f in (None,'member','operator','node','data','cleanup','interrupt')]:
            with self.subTest(failure=failure),tempfile.TemporaryDirectory() as tmp:
                g=self.gate(tmp);g.base=Path(tmp);g.state['backup']={'verified':True};g.save()
                path=Path(tmp,'clusters/mongodb-restore-logical/config.yml');path.parent.mkdir(parents=True);path.write_text('{}')
                events=[];programs=[];clock=[0];settings=[]
                def tick():clock[0]+=10;return clock[0]
                def event(name):
                    events.append(name)
                    if name==failure:raise ValueError('first-'+name)
                    if failure=='interrupt' and name=='operator':raise SystemExit(143)
                def prepare(**kwargs):
                    self.assertEqual(kwargs,{'sharded':True});g.state['target']={'name':'mongodb-gate-logical-restore',
                        'deployment':'mongodb-restore-logical'};event('prepare')
                def client(*a,**kw):g.scenario.client_uid='client-uid';g.scenario.worker=Mock();g.scenario.worker.poll.return_value=None
                def js(code,marker,**kwargs):
                    programs.append((code,marker,kwargs))
                    if marker=='MONGODB_STABILITY_FINAL_DATA_PASS':event('data')
                def cleanup(**kwargs):
                    event('cleanup');g.state.pop('stability',None)
                    fault=g.lab.directory/'mongodb-fault-stability.json'
                    if fault.exists():fault.unlink()
                def target_clean():g.state.pop('target',None)
                def phase(config,*,inject):
                    settings.append(config);inject();event('ledger');return {'phase':config['phase']}
                performance=Mock();performance.phase.side_effect=phase
                performance.restart.start.side_effect=lambda:event('node')
                with ExitStack() as stack,redirect_stdout(StringIO()) as output:
                    for context in (patch.object(g,'validate_identity'),patch.object(g.scenario,'health'),
                        patch.object(g,'request_for_cleanup'),patch.object(g,'verify_objects',return_value={'verified':True}),
                        patch.object(g,'prepare_target',side_effect=prepare),patch.object(g,'target_setup'),
                        patch.object(g,'stability_boundary',return_value={'identity':'stable'}),patch.object(g,'source_identity',return_value={'uid':'source'}),
                        patch.object(g.scenario,'client_create',side_effect=client),patch.object(g.scenario,'js',side_effect=js),
                        patch.object(g.scenario_module,'Performance',return_value=performance),patch.object(g,'stability_health'),
                        patch.object(g,'stability_restart',side_effect=lambda name:event(name)),patch.object(module.time,'monotonic',side_effect=tick),
                        patch.object(module.time,'sleep'),patch.object(g,'stability_cleanup',side_effect=cleanup),
                        patch.object(g,'cleanup_target',side_effect=target_clean),patch.object(g,'diagnostics')):
                        stack.enter_context(context)
                    if failure:
                        with self.assertRaises(SystemExit if failure=='interrupt' else ValueError):g.stability(focused=focused)
                        self.assertNotIn('MONGODB_STABILITY_FAULTS_PASS',output.getvalue())
                    else:
                        g.stability(focused=focused)
                        self.assertEqual([c['phase'] for c in settings],['stability-source','stability-sharded'])
                        self.assertEqual([c['sharded'] for c in settings],[False,True])
                        self.assertTrue(all(c['seconds']==(180 if focused else 1800) and c['concurrency']==4 and c['fault'] for c in settings))
                        self.assertTrue(all(c['memory_profile'] is True for c in settings))
                        self.assertTrue(all(c['shared_ca'] is True for c in settings))
                        self.assertEqual(events.count('member'),2);self.assertEqual(events.count('operator'),2)
                        self.assertEqual(events.count('node'),2);self.assertEqual(events.count('ledger'),2)
                        self.assertIn('MONGODB_STABILITY_FAULTS_PASS' if focused else 'MONGODB_STABILITY_PASS',output.getvalue().splitlines())
                        self.assertNotIn('MONGODB_STABILITY_PASS' if focused else 'MONGODB_STABILITY_FAULTS_PASS',output.getvalue().splitlines())
                        for code,marker,kwargs in programs:
                            self.assert_client_syntax(g,code,marker,**kwargs)
                self.assertIn('cleanup',events)

    def test_stability_recovery_refuses_source_or_client_replacement_before_deletion(self):
        for drift in ('source','client','boundary','fault'):
            with self.subTest(drift=drift),tempfile.TemporaryDirectory() as tmp:
                g=self.gate(tmp);q={'workload':'mongodb_gate_0123456789abcdef','topology':'source',
                    'source':{'uid':'source'},'client_uid':'owned','before':{'identity':'before'}}
                g.state['stability']=q
                if drift=='fault':g.scenario.fixture.write_private(Path(tmp,'mongodb-fault-stability.json'),{'foreign':True})
                pod={'metadata':{'uid':'foreign' if drift=='client' else 'owned','labels':{'kubeauto.io/test-run':q['workload']}}}
                with patch.object(g,'source_identity',return_value={'uid':'changed' if drift=='source' else 'source'}), \
                        patch.object(g.scenario_module.WorkerRestart,'recover'),patch.object(g,'validate_identity'), \
                        patch.object(g,'stability_pod_recover'),patch.object(g,'stability_health'), \
                        patch.object(g,'stability_boundary',return_value={'identity':'changed' if drift=='boundary' else 'before'}), \
                        patch.object(g.lab,'get',return_value=pod),patch.object(g.scenario,'delete_client') as delete:
                    with self.assertRaises(ValueError):g.stability_cleanup(interrupted=True)
                delete.assert_not_called();self.assertIn('stability',g.state)

    def test_stability_cleanup_recreates_only_its_client_for_the_correct_topology(self):
        for topology in ('source','sharded'):
            for interrupted,cleaned in ((False,False),(True,False),(True,True)):
                with self.subTest(topology=topology,interrupted=interrupted,cleaned=cleaned),tempfile.TemporaryDirectory() as tmp:
                    g=self.gate(tmp);events=[]
                    q={'workload':'mongodb_gate_0123456789abcdef','topology':topology,'source':{'uid':'source'},
                       'client_uid':'old','before':{'identity':'stable'},'data_cleaned':cleaned}
                    g.state['stability']=q
                    g.state['target']={'name':'mongodb-gate-logical-restore'}
                    fault=Path(tmp,'mongodb-fault-stability.json')
                    g.scenario.fixture.write_private(fault,{'cluster_uid':g.state['cluster_uid'],
                        'run_id':g.state['run_id'],'workload':q['workload'],'topology':topology})
                    current=[{'metadata':{'uid':'old','labels':{'kubeauto.io/test-run':q['workload']}}}]
                    def delete():events.append('delete');current[0]=None;g.scenario.client_uid=None
                    def create(name,**kwargs):
                        events.append('create');self.assertEqual(name,g.state['target']['name'] if topology=='sharded' else None)
                        self.assertEqual(kwargs,{'sharded':topology=='sharded'});g.scenario.client_uid='new'
                    with ExitStack() as stack,redirect_stdout(StringIO()) as output:
                        for context in (patch.object(g,'source_identity',return_value=q['source']),
                            patch.object(g.scenario_module.WorkerRestart,'recover',side_effect=lambda:events.append('node-recover')),
                            patch.object(g,'validate_identity'),patch.object(g,'stability_recover_init'),patch.object(g,'stability_pod_recover'),patch.object(g,'stability_health'),
                            patch.object(g,'stability_boundary',return_value=q['before']),patch.object(g.lab,'get',side_effect=lambda *a,**kw:current[0]),
                            patch.object(g.scenario,'client_create',side_effect=create),patch.object(g.scenario,'delete_client',side_effect=delete),
                            patch.object(g.scenario,'clean_workload',side_effect=lambda:events.append('data-clean')),
                            patch.object(g,'cleanup_target',side_effect=lambda:events.append('target-clean')),patch.object(g.scenario,'health')):
                            stack.enter_context(context)
                        g.stability_cleanup(interrupted=interrupted)
                    expected=['node-recover']+(['delete','create'] if interrupted and not cleaned else [])
                    expected+=([] if cleaned else ['data-clean'])+['delete']+(['target-clean'] if topology=='sharded' else [])
                    self.assertEqual(events,expected);self.assertFalse(fault.exists());self.assertNotIn('stability',g.state)
                    self.assertIn('MONGODB_STABILITY_PHASE_SCOPED_CLEAN_PASS',output.getvalue())

    def test_stability_init_recovery_recreates_only_original_router_for_exact_upstream_error(self):
        for drift in (None,'pod','controller','owner','error','multiple'):
            with self.subTest(drift=drift),tempfile.TemporaryDirectory() as tmp:
                g=self.gate(tmp);g.state['stability']={'topology':'sharded'}
                name='mongodb-gate-logical-restore-mongos';podname=name+'-1'
                g.state['target']={'name':'mongodb-gate-logical-restore','cr_uid':'cr','resources':[
                    {'kind':'pod','name':podname,'uid':'pod'}, {'kind':'statefulset','name':name,'uid':'owner'}]}
                pod={'metadata':{'name':podname,'uid':'foreign' if drift=='pod' else 'pod',
                    'ownerReferences':[{'uid':'foreign' if drift=='owner' else 'owner','controller':True}]},
                    'status':{'initContainerStatuses':[{'name':'mongo-init','state':{'waiting':{'reason':'CrashLoopBackOff'}}}]}}
                owner={'metadata':{'uid':'foreign' if drift=='controller' else 'owner',
                    'ownerReferences':[{'uid':'cr','controller':True}]}}
                log='other init error' if drift=='error' else "chown: changing ownership of '/opt/percona/logcollector/logrotate/conf.d': Operation not permitted"
                with patch.object(g.lab,'get',side_effect=lambda kind,*a,**kw:{'items':[pod]*(2 if drift=='multiple' else 1)} if kind=='pods' else owner), \
                        patch.object(g.lab,'run',return_value=log),patch.object(g.lab,'delete_uid') as delete, \
                        patch.object(g,'stability_pod_recover') as recover,redirect_stdout(StringIO()):
                    if drift:
                        with self.assertRaises(ValueError):g.stability_recover_init()
                        delete.assert_not_called();recover.assert_not_called()
                    else:
                        g.stability_recover_init()
                        delete.assert_called_once_with('/api/v1/namespaces/mongodb-gate/pods/'+podname,'pod')
                        recover.assert_called_once_with(replacement=True)
                        self.assertEqual(json.loads(g.path.read_text())['stability']['pod_fault']['uid'],'pod')

    def test_stability_pod_recovery_preserves_controller_and_records_exact_target_replacement(self):
        for drift in (None,'controller','receipt','operator-spec'):
            with self.subTest(drift=drift),tempfile.TemporaryDirectory() as tmp:
                g=self.gate(tmp);operator={'uid':'operator','spec':{'template':'pinned'}}
                kind='replicaset' if drift=='operator-spec' else 'statefulset'
                fault={'owner_kind':kind,'owner_name':'controller','owner_uid':'owner','pod':'member',
                    'uid':'old','operator_uid':'operator' if kind=='replicaset' else None}
                g.state['stability']={'topology':'sharded','before':{'operator':operator},'pod_fault':fault}
                record={'kind':'pod','name':'member','uid':'foreign' if drift=='receipt' else 'old'}
                g.state['target']={'resources':[record]}
                pods=[{'metadata':{'name':'member' if i==0 else 'member-'+str(i),'uid':'new'+str(i),
                    'ownerReferences':[{'uid':'owner','controller':True}]}} for i in range(1 if kind=='replicaset' else 3)]
                def get(resource,*a,**kw):
                    if resource=='pods':return {'items':pods}
                    if resource=='deployment':return {'metadata':{'uid':'operator'},'spec':{'template':'changed'}}
                    return {'metadata':{'uid':'foreign' if drift=='controller' else 'owner'}}
                with patch.object(g.lab,'get',side_effect=get),patch.object(g.scenario.product,'ready',return_value=True),patch.object(g,'stability_health'):
                    if drift:
                        with self.assertRaises(ValueError):g.stability_pod_recover(replacement=True)
                        self.assertIn('pod_fault',g.state['stability'])
                    else:
                        g.stability_pod_recover(replacement=True);self.assertEqual(record['uid'],'new0')
                        self.assertNotIn('pod_fault',g.state['stability'])

    def test_stability_fixed_runner_modes_do_not_confuse_focused_or_recovery_with_full_acceptance(self):
        for mode,marker in (('stability','MONGODB_STABILITY_PASS'),('stability-faults','MONGODB_STABILITY_FAULTS_PASS'),
                            ('stability-recover','MONGODB_STABILITY_RECOVER_PASS')):
            script='source tests/helpers/mongodb-runner.sh\nmongodb_launch(){ printf "%s|%s\\n" "$1" "$2"; }\nmongodb_dispatch --mongodb-'+mode
            r=subprocess.run(['bash','-euc',script],cwd=ROOT,capture_output=True,text=True,check=True)
            self.assertEqual(r.stdout.strip(),mode+'|'+marker)
            with patch.object(module.sys,'argv',['mongodb-backups.py',mode,'--base',str(ROOT)]), \
                    patch.object(module.BackupGate,'stability') as run,patch.object(module.BackupGate,'stability_cleanup') as recover, \
                    patch.object(module.BackupGate,'cleanup_target'),patch.object(module.Path,'read_text',return_value='{}'), \
                    patch.object(module.signal,'signal'),redirect_stdout(StringIO()):
                self.assertEqual(module.main(),0)
                if mode=='stability-recover':recover.assert_called_once_with(interrupted=True);run.assert_not_called()
                else:run.assert_called_once_with(focused=mode=='stability-faults');recover.assert_not_called()

    def test_stability_member_fault_requires_cr_controller_and_pod_owner_before_deletion(self):
        for topology in ('source','sharded'):
            for drift in (None,'cr','pod','label','ready'):
                with self.subTest(topology=topology,drift=drift),tempfile.TemporaryDirectory() as tmp:
                    g=self.gate(tmp);g.state['source_cluster_uid']='source'
                    g.state['stability']={'topology':topology}
                    g.state['target']={'name':'mongodb-gate-logical-restore','cr_uid':'target'}
                    name=g.state['target']['name'] if topology=='sharded' else g.scenario.fixture.NAME
                    owner={'metadata':{'name':name+'-rs0','uid':'owner','ownerReferences':[{
                        'uid':'foreign' if drift=='cr' else 'target' if topology=='sharded' else 'source','controller':True}]}}
                    pod={'metadata':{'name':name+'-rs0-0','uid':'pod','labels':{'app.kubernetes.io/instance':
                        'foreign' if drift=='label' else name},'ownerReferences':[{'uid':'foreign' if drift=='pod' else 'owner','controller':True}]}}
                    with patch.object(g,'stability_health'),patch.object(g.lab,'get',side_effect=lambda kind,*a:owner if kind=='statefulset' else pod), \
                            patch.object(g.scenario.product,'ready',return_value=drift!='ready'), \
                            patch.object(g.lab,'delete_uid') as delete,patch.object(g,'stability_pod_recover') as recover,redirect_stdout(StringIO()):
                        if drift:
                            with self.assertRaises(ValueError):g.stability_restart('member')
                            delete.assert_not_called();recover.assert_not_called()
                        else:
                            g.stability_restart('member');delete.assert_called_once_with('/api/v1/namespaces/mongodb-gate/pods/'+name+'-rs0-0','pod')
                            recover.assert_called_once_with(replacement=True)

    def test_rollback_refusals_require_exact_product_reason_fcv_and_unchanged_resources(self):
        for failure in (None,'accepted','generic','ready','changed','interrupted','fcv'):
            with self.subTest(failure=failure),tempfile.TemporaryDirectory() as tmp:
                g=self.gate(tmp);g.base=Path(tmp)
                g.state['target']={'name':'mongodb-gate-logical-restore','deployment':'mongodb-restore-logical'}
                p=Path(tmp,'clusters/mongodb-restore-logical/config.yml');p.parent.mkdir(parents=True)
                original={'mongodb_ver':'8.0.32-14','mongodb_operator_ver':'1.23.1',
                          'mongodb_operator_chart_ver':'1.23.2'}
                p.write_text(yaml.safe_dump(original,sort_keys=False));p.chmod(0o600);calls=[]
                def run(command,**kwargs):
                    calls.append((command,yaml.safe_load(p.read_text())))
                    if failure=='interrupted':raise SystemExit(143)
                    layer='server' if len(calls)==1 else 'operator'
                    reason='MongoDB '+layer+' downgrade is refused; restore a compatible backup into a separate cluster'
                    return SimpleNamespace(returncode=0 if failure=='accepted' else 1,
                        stdout='timeout' if failure=='generic' else reason+(' MONGODB_DATA_PLANE_READY' if failure=='ready' else ''),stderr='')
                output=StringIO()
                with patch.object(g.scenario,'client_create'),patch.object(g.scenario,'delete_client') as clean, \
                        patch.object(g.scenario,'js',side_effect=ValueError('FCV transition') if failure=='fcv' else None) as js, \
                        patch.object(g,'rollback_snapshot',side_effect=[['before'],['changed' if failure=='changed' else 'before'],['before']]), \
                        patch.object(module.subprocess,'run',side_effect=run),redirect_stdout(output):
                    if failure:
                        with self.assertRaises(SystemExit if failure=='interrupted' else ValueError):g.rollback_refusals()
                    else:g.rollback_refusals()
                clean.assert_called_once();self.assertEqual(yaml.safe_load(p.read_text()),original)
                self.assertEqual(p.stat().st_mode&0o777,0o600)
                if failure is None:
                    self.assertEqual(len(calls),2);self.assertEqual(js.call_count,4)
                    self.assertEqual(calls[0][1]['mongodb_ver'],'8.0.26-11')
                    self.assertEqual(calls[1][1]['mongodb_operator_ver'],'1.23.0')
                    self.assertEqual(calls[1][1]['mongodb_operator_chart_ver'],'1.23.1')
                    self.assertTrue(all(c[0]==['kubecli','setup','mongodb-restore-logical','07','--tags','mongodb'] for c in calls))
                    self.assertEqual(output.getvalue().count('MONGODB_ROLLBACK_REFUSAL_PASS'),2)
                    code=js.call_args.args[0]
                    self.assertIn('getParameter:1,featureCompatibilityVersion:1',code)
                    self.assertIn('f.targetVersion',code);self.assertIn('f.previousVersion',code)
                    marker='MONGODB_ROLLBACK_FCV_PASS version=8.0 transition=absent target-data=unchanged'
                    # Parse the actual authenticated client program. Checking
                    # only the fragment misses duplicate bindings from AUTH.
                    self.assert_client_syntax(g,code,marker)
                else:self.assertNotIn('MONGODB_ROLLBACK_REFUSAL_PASS',output.getvalue())

    def test_rollback_snapshot_requires_full_owned_target_graph_and_operator_identity(self):
        for drift in (None,'uid','pod','claim','secret'):
            with self.subTest(drift=drift),tempfile.TemporaryDirectory() as tmp:
                g=self.gate(tmp);target='mongodb-gate-logical-restore'
                g.state['target']={'name':target,'cr_uid':'target-uid'}
                rows={kind:[{'metadata':{'name':target+'-'+str(i),'namespace':'mongodb-gate',
                    'uid':kind+str(i),'labels':module.LABELS|{'kubeauto.io/test-run':g.state['run_id']}}}
                    for i in range(count)] for kind,count in (('pods',3),('pvc',3),('secrets',5),('psmdb',1))}
                rows['psmdb'][0]['metadata'].update(name=target,uid='foreign' if drift=='uid' else 'target-uid')
                if drift in ('pod','claim','secret'):rows[{'pod':'pods','claim':'pvc','secret':'secrets'}[drift]].pop()
                def get(kind,*a,**kw):
                    return {'metadata':{'uid':'operator-uid'},'spec':{'image':'pinned'}} if kind=='deployment' else {'items':rows[kind]}
                with patch.object(g.lab,'get',side_effect=get),patch.object(g,'canonical_snapshot',return_value={'safe':'snapshot'}):
                    if drift:
                        with self.assertRaisesRegex(ValueError,'graph is incomplete'):g.rollback_snapshot()
                    else:self.assertEqual(g.rollback_snapshot(),{'safe':'snapshot','operator':{'uid':'operator-uid','spec':{'image':'pinned'}}})

    def test_rollback_refusal_failure_cleans_target_without_requesting_restore(self):
        with tempfile.TemporaryDirectory() as tmp:
            g=self.gate(tmp);g.state['backup']={'verified':True};g.save()
            with patch.object(g,'validate_identity'),patch.object(g.scenario,'health'), \
                    patch.object(g,'request_for_cleanup'),patch.object(g,'verify_objects',return_value={'verified':True}), \
                    patch.object(g,'prepare_target'),patch.object(g,'rollback_refusals',side_effect=ValueError('first-refusal-error')), \
                    patch.object(g,'diagnostics'),patch.object(g,'cleanup_target') as clean, \
                    patch.object(g,'target_request') as restore,redirect_stdout(StringIO()) as output:
                with self.assertRaisesRegex(ValueError,'first-refusal-error'):g.restore_independent(rollback=True)
            clean.assert_called_once();restore.assert_not_called();self.assertNotIn('MONGODB_ROLLBACK_PASS',output.getvalue())

    def test_rollback_pass_requires_real_restore_data_checks_and_cleanup(self):
        for failure in (None,'request','data','cleanup','backup-changed'):
            with self.subTest(failure=failure),tempfile.TemporaryDirectory() as tmp:
                g=self.gate(tmp);g.base=Path(tmp);g.state['backup']={'destination':'s3://owned/backup/pbm'};g.save()
                target={'name':'mongodb-gate-logical-restore','deployment':'mongodb-restore-logical',
                        'cr_uid':'new-target','source_identity':{'source':'unchanged'}}
                p=Path(tmp,'clusters/mongodb-restore-logical/config.yml');p.parent.mkdir(parents=True);p.write_text('{}')
                events=[]
                def prepare():g.state['target']=target;events.append('prepare')
                def stage(name):
                    events.append(name)
                    if failure==name:raise ValueError('first-'+name)
                verifications=[]
                def verify(**kwargs):
                    verifications.append(kwargs)
                    if len(verifications)>1:
                        stage('backup-check')
                        if failure=='backup-changed':return {'changed':True}
                    return g.state['backup']
                with ExitStack() as stack,redirect_stdout(StringIO()) as output:
                    for context in (
                        patch.object(g,'validate_identity'),patch.object(g.scenario,'health'),
                        patch.object(g,'request_for_cleanup'),patch.object(g,'verify_objects',side_effect=verify),
                        patch.object(g,'prepare_target',side_effect=prepare),patch.object(g,'rollback_refusals',side_effect=lambda:stage('refusals')),
                        patch.object(g.scenario.product,'operation_object',return_value={}),
                        patch.object(g.scenario.product,'check_operation_identity'),patch.object(g.scenario.product,'operation_complete',return_value=True),
                        patch.object(g,'target_setup',side_effect=lambda:stage('request')),patch.object(g.lab,'get',return_value={'status':{}}),
                        patch.object(g,'target_health'),patch.object(g,'trace_restore',return_value=nullcontext()),
                        patch.object(g.scenario,'client_create'),patch.object(g.scenario,'js',side_effect=lambda *a,**kw:stage('data')),
                        patch.object(g,'source_identity',return_value=target['source_identity']),patch.object(g.scenario,'cleanup'),
                        patch.object(g,'diagnostics'),patch.object(g,'cleanup_target',side_effect=lambda:stage('cleanup'))):
                        stack.enter_context(context)
                    if failure:
                        with self.assertRaisesRegex(ValueError,'changed backup objects' if failure=='backup-changed' else 'first-'+failure):g.restore_independent(rollback=True)
                        self.assertNotIn('MONGODB_ROLLBACK_PASS',output.getvalue())
                    else:
                        g.restore_independent(rollback=True)
                        self.assertEqual(events,['prepare','refusals','request','data','cleanup','backup-check'])
                        self.assertIn('MONGODB_ROLLBACK_PASS',output.getvalue().splitlines())

    def test_rollback_fixed_runner_uses_logical_restore_with_refusals(self):
        script='source tests/helpers/mongodb-runner.sh\nmongodb_launch(){ printf "%s|%s\\n" "$1" "$2"; }\nmongodb_dispatch --mongodb-rollback\n'
        r=subprocess.run(['bash','-c',script],cwd=ROOT,capture_output=True,text=True,check=True)
        self.assertEqual(r.stdout.strip(),'rollback|MONGODB_ROLLBACK_PASS')
        with patch.object(module.sys,'argv',['mongodb-backups.py','rollback','--base',str(ROOT)]), \
                patch.object(module.BackupGate,'restore_independent') as run,patch.object(module.signal,'signal'):
            self.assertEqual(module.main(),0)
        run.assert_called_once_with(rollback=True)

    def test_storage_client_recovery_only_replaces_the_recorded_stateless_pod_and_rechecks_all_data(self):
        for drift in (None,'already-always','known-replacement','unrecorded','source','document','boundary','objects'):
            with self.subTest(drift=drift),tempfile.TemporaryDirectory() as tmp:
                g=self.gate(tmp);name=g.state['name']+'-client';labels=module.LABELS|{'kubeauto.io/test-run':g.state['run_id']}
                pod={'apiVersion':'v1','kind':'Pod','metadata':{'name':name,'uid':'old','labels':labels},'spec':{'restartPolicy':'Never'}}
                desired={'apiVersion':'v1','kind':'Pod','metadata':{'name':name,'namespace':g.scenario.ns,'labels':labels},'spec':{'restartPolicy':'Always'}}
                source={'cr_uid':'source'};g.state.update(source_cluster_uid='source',backup={'objects':['verified']},resources=[{'kind':'pod','name':name,'uid':'old'}])
                receipt={'old_uid':'old','new_uid':None,'source':source,'boundary':'boundary','document':desired,
                         'document_sha256':hashlib.sha256(json.dumps(desired,sort_keys=True).encode()).hexdigest()}
                if drift=='already-always':pod['spec']['restartPolicy']='Always'
                if drift=='unrecorded':pod['metadata']['uid']='unknown'
                if drift in ('document','boundary','known-replacement'):
                    g.state['client_recovery']=receipt
                    if drift=='document':receipt['document']['spec']['privileged']=True
                    if drift=='boundary':receipt['boundary']='changed'
                    if drift=='known-replacement':
                        pod['metadata']['uid']='new';pod['spec']['restartPolicy']='Always'
                        receipt['new_uid']='new';g.state['resources'][0]['uid']='new'
                g.save();current=[pod];removed=[];created=[]
                def delete(*args):removed.append(args);current[0]=None
                def create(*args,**kwargs):
                    created.append(kwargs['obj']);current[0]=copy.deepcopy(kwargs['obj']);current[0]['metadata']['uid']='new'
                    return json.dumps(current[0])
                with patch.object(g,'validate_identity'),patch.object(g.scenario,'health'), \
                        patch.object(g,'source_identity',return_value={'cr_uid':'changed' if drift=='source' else 'source'}), \
                        patch.object(g,'client_boundary',return_value='boundary'),patch.object(g.lab,'get',side_effect=lambda *a,**kw:current[0]), \
                        patch.object(g.lab,'delete_uid',side_effect=delete),patch.object(g.lab,'run'),patch.object(g,'api_input',side_effect=create), \
                        patch.object(g,'mc'),patch.object(g,'request_for_cleanup'), \
                        patch.object(g,'verify_objects',return_value={'objects':['changed' if drift=='objects' else 'verified']}),redirect_stdout(StringIO()):
                    if drift in ('unrecorded','source','document','boundary','objects'):
                        with self.assertRaises(ValueError):g.recover_storage_client()
                        if drift!='objects':self.assertEqual(removed,[]);self.assertEqual(created,[])
                    else:
                        g.recover_storage_client();self.assertNotIn('client_recovery',g.state)
                        self.assertEqual(bool(removed),drift is None);self.assertEqual(bool(created),drift is None)
                        if created:
                            self.assertEqual(created,[desired]);self.assertEqual(removed,[('/api/v1/namespaces/mongodb-gate/pods/'+name,'old')])
                            self.assertEqual(g.state['resources'][0]['uid'],'new')

    def test_cleanup_child_result_requires_exact_exit_business_cleanup_and_expected_failure(self):
        markers='MONGODB_CLEANUP_PROBE_BUSINESS_PASS\nMONGODB_CLEANUP_PROBE_READY\nMONGODB_CLEANUP_PROBE_SCOPED_CLEAN_PASS\n'
        for outcome,rc in (('success',0),('failure',1),('interruption',143)):
            for drift in (None,'exit','business','cleanup','extra-failure','duplicate','signal'):
                with self.subTest(outcome=outcome,drift=drift):
                    output=markers+('MONGODB_BACKUP_FAIL reason=intentional MongoDB cleanup qualification failure\n' if outcome=='failure' else '')
                    if drift=='business':output=output.replace('MONGODB_CLEANUP_PROBE_BUSINESS_PASS\n','')
                    if drift=='cleanup':output=output.replace('MONGODB_CLEANUP_PROBE_SCOPED_CLEAN_PASS\n','')
                    if drift=='extra-failure':output+='MONGODB_CLEANUP_PROBE_RECOVERY_FAIL journal=preserved\n'
                    if drift=='duplicate':output+='MONGODB_CLEANUP_PROBE_READY\n'
                    invalid=drift is not None and (drift!='signal' or outcome=='interruption')
                    if invalid:
                        with self.assertRaises(ValueError):module.cleanup_probe_result(output,outcome,rc+1 if drift=='exit' else rc,drift!='signal')
                    else:module.cleanup_probe_result(output,outcome,rc,True)

    def test_cleanup_child_success_failure_and_signal_all_reach_owned_target_cleanup(self):
        for outcome in ('success','failure','interruption'):
            with self.subTest(outcome=outcome),tempfile.TemporaryDirectory() as tmp:
                g=self.gate(tmp);g.save()
                q={'run_id':'fedcba9876543210','child_pid':module.os.getpid(),'active':outcome,'backup_run_id':g.state['run_id']}
                g.scenario.fixture.write_private(Path(tmp,'mongodb-cleanup-qualification.json'),q)
                def prepare():g.state['target']={'name':'target'}
                with patch.object(module.sys,'stdin',StringIO(q['run_id']+'\n')),patch.object(g,'validate_identity'), \
                        patch.object(g,'prepare_target',side_effect=prepare),patch.object(g.scenario,'client_create'), \
                        patch.object(g.scenario,'js'),patch.object(g,'cleanup_target') as cleanup, \
                        patch.object(module.time,'sleep',side_effect=SystemExit(143)),redirect_stdout(StringIO()) as output:
                    if outcome=='failure':
                        with self.assertRaisesRegex(ValueError,'intentional'):g.cleanup_probe(outcome)
                    elif outcome=='interruption':
                        with self.assertRaises(SystemExit) as stopped:g.cleanup_probe(outcome)
                        self.assertEqual(stopped.exception.code,143)
                    else:g.cleanup_probe(outcome)
                    cleanup.assert_called_once()
                    self.assertIn('MONGODB_CLEANUP_PROBE_SCOPED_CLEAN_PASS',output.getvalue().splitlines())

    def test_cleanup_child_rejects_wrong_parent_receipt_before_target_creation(self):
        for drift in ('pid','token','outcome','backup'):
            with self.subTest(drift=drift),tempfile.TemporaryDirectory() as tmp:
                g=self.gate(tmp);g.save()
                q={'run_id':'fedcba9876543210','child_pid':module.os.getpid()+(1 if drift=='pid' else 0),
                   'active':'failure' if drift=='outcome' else 'success',
                   'backup_run_id':'foreign' if drift=='backup' else g.state['run_id']}
                g.scenario.fixture.write_private(Path(tmp,'mongodb-cleanup-qualification.json'),q)
                with patch.object(module.sys,'stdin',StringIO(('foreign' if drift=='token' else q['run_id'])+'\n')), \
                        patch.object(g,'validate_identity'),patch.object(g,'prepare_target') as prepare:
                    with self.assertRaises(ValueError):g.cleanup_probe('success')
                    prepare.assert_not_called()

    def test_cleanup_finish_checks_foreign_source_nodes_and_backup_before_fixture_deletion(self):
        for drift in (None,'foreign','source','nodes','backup','journal'):
            with self.subTest(drift=drift),tempfile.TemporaryDirectory() as tmp:
                g=self.gate(tmp);g.save();run_id='fedcba9876543210';name='mongodb-cleanup-independent-'+run_id
                foreign={'metadata':{'uid':'independent'},'data':{'receipt':run_id}}
                q={'run_id':run_id,'backup_run_id':g.state['run_id'],'name':name if drift!='journal' else 'foreign',
                   'source':{'cr':'source'},'nodes':{'worker':{'uid':'worker','spec':{}}},'foreign':foreign}
                path=Path(tmp,'mongodb-cleanup-qualification.json');g.scenario.fixture.write_private(path,q)
                removed=[]
                def get(kind,*args,**kwargs):
                    if kind=='nodes':return {'items':[{'metadata':{'name':'worker','uid':'changed' if drift=='nodes' else 'worker'},'spec':{}}]}
                    if kind=='pod':return None
                    if kind=='configmap':return None if removed else foreign|({'data':{'receipt':'changed'}} if drift=='foreign' else {})
                    raise AssertionError(kind)
                with patch.object(g,'validate_identity'),patch.object(g,'cleanup_target'),patch.object(g.lab,'get',side_effect=get), \
                        patch.object(g,'source_identity',return_value={'cr':'changed' if drift=='source' else 'source'}), \
                        patch.object(g,'verify_objects',return_value='changed' if drift=='backup' else None), \
                        patch.object(g.lab,'delete_uid',side_effect=lambda *a:removed.append(a)) as delete, \
                        patch.object(g.scenario,'health'),redirect_stdout(StringIO()):
                    if drift:
                        with self.assertRaises(ValueError):g.cleanup_qualification_finish()
                        delete.assert_not_called();self.assertTrue(path.exists())
                    else:
                        g.cleanup_qualification_finish();self.assertFalse(path.exists())
                        self.assertEqual(removed,[('/api/v1/namespaces/mongodb-gate/configmaps/'+name,'independent')])

    def test_cleanup_fixed_modes_keep_focused_success_separate_from_complete_module(self):
        script='source tests/helpers/mongodb-runner.sh\nmongodb_launch(){ printf "%s|%s\\n" "$1" "$2"; }\n'
        script+='\n'.join('mongodb_dispatch --mongodb-'+m for m in ('cleanup','cleanup-success','cleanup-failure','cleanup-recover'))
        result=subprocess.run(['bash','-euc',script],cwd=ROOT,capture_output=True,text=True,check=True)
        self.assertEqual(result.stdout.splitlines(),['cleanup|MONGODB_CLEANUP_PASS',
            'cleanup-success|MONGODB_CLEANUP_SUCCESS_PASS','cleanup-failure|MONGODB_CLEANUP_FAILURE_PASS',
            'cleanup-recover|MONGODB_CLEANUP_RECOVER_PASS'])
        with patch.object(module.sys,'argv',['mongodb-backups.py','cleanup-failure','--base',str(ROOT)]), \
                patch.object(module.BackupGate,'cleanup_qualification') as run,patch.object(module.signal,'signal'):
            self.assertEqual(module.main(),0);run.assert_called_once_with(only='failure')

    def test_cleanup_waits_for_csi_prerequisite_before_starting_any_child(self):
        with tempfile.TemporaryDirectory() as tmp:
            g=self.gate(tmp);g.state['backup']={'objects':'verified'};g.save()
            with patch.object(g,'validate_identity'),patch.object(g.scenario,'health'), \
                    patch.object(g,'request_for_cleanup'),patch.object(g,'verify_objects',return_value=g.state['backup']), \
                    patch.object(g.lab,'get',side_effect=lambda k,*a,**kw:{'items':[]} if k=='nodes' else None), \
                    patch.object(g,'source_identity',return_value={'source':'owned'}), \
                    patch.object(g,'api_input',return_value=json.dumps({'metadata':{'uid':'foreign-canary'}})), \
                    patch.object(g,'target_capacity',side_effect=ValueError('capacity-prerequisite')) as capacity, \
                    patch.object(module.subprocess,'Popen') as child,patch.object(g,'cleanup_qualification_finish') as cleanup:
                with self.assertRaisesRegex(ValueError,'capacity-prerequisite'):g.cleanup_qualification(only='failure')
                capacity.assert_called_once_with(wait_timeout=180);child.assert_not_called();cleanup.assert_called_once()

    def test_refusal_target_marker_program_runs_with_the_actual_default_admin_prelude(self):
        import shutil
        node=shutil.which('node')
        if not node:self.skipTest('Node is required for MongoDB client contracts')
        scenario=module.BackupGate(ROOT,'logical').scenario
        code='''const assert=value=>{if(!value)throw Error('assertion');};
assert.eq=(a,b)=>assert(JSON.stringify(a)===JSON.stringify(b));
const print=console.log,prefix='owned';
class Mongo{getDB(name){return name==='admin'?{auth:()=>true}:{getCollection:()=>({
countDocuments:()=>1,findOne:query=>query._id==='after-backup'?{value:'must-disappear'}:null})};}}
'''
        # Use the prelude loaded by the helper, rather than declaring d in the test.
        prelude=scenario.js.__func__.__globals__['AUTH']
        for drift in (None,'extra-document','marker-changed','backup-restored'):
            with self.subTest(drift=drift):
                fixture=code
                if drift=='extra-document':fixture=fixture.replace('countDocuments:()=>1','countDocuments:()=>2')
                if drift=='marker-changed':fixture=fixture.replace("value:'must-disappear'","value:'changed'")
                if drift=='backup-restored':fixture=fixture.replace("{value:'must-disappear'}:null","{value:'must-disappear'}:{value:'restored'}")
                result=subprocess.run([node,'-e',fixture+prelude+module.REFUSAL_TARGET_DATA],capture_output=True,text=True)
                if drift:
                    self.assertNotEqual(result.returncode,0)
                    self.assertNotIn('MONGODB_BACKUP_REFUSAL_TARGET_DATA_PASS original-marker=unchanged',result.stdout.splitlines())
                else:
                    self.assertEqual(result.returncode,0,result.stderr)
                    self.assertIn('MONGODB_BACKUP_REFUSAL_TARGET_DATA_PASS original-marker=unchanged',result.stdout.splitlines())

    def test_refusal_product_policy_uid_retires_only_after_product_proves_absence(self):
        for drift in (None,'changed-uid','foreign','remains'):
            with self.subTest(drift=drift),tempfile.TemporaryDirectory() as tmp:
                gate=self.gate(tmp);gate.base=Path(tmp);name='mongodb-gate-logical-restore'
                directory=Path(tmp)/'clusters/mongodb-restore-logical';directory.mkdir(parents=True)
                original={'mongodb_network_policy_enabled':True}
                directory.joinpath('config.yml').write_text(yaml.safe_dump(original))
                record={'kind':'networkpolicy','name':name+'-access','uid':'old-policy'}
                gate.state['target']={'name':name,'deployment':'mongodb-restore-logical','resources':[record],
                    'refusal':{'original_config':original,'phase':'quorum'}}
                policy={'metadata':{'name':name+'-access','namespace':'mongodb-gate',
                    'uid':'other' if drift=='changed-uid' else 'old-policy',
                    'labels':{} if drift=='foreign' else {'app.kubernetes.io/managed-by':'kubeauto','kubeauto.io/component':'mongodb'}}}
                current=[policy]
                def setup():
                    if drift!='remains':current[0]=None
                with patch.object(gate.lab,'get',side_effect=lambda *a:current[0]),patch.object(gate,'target_setup',side_effect=setup) as product:
                    if drift:
                        with self.assertRaises(ValueError):gate.refusal_config({'mongodb_network_policy_enabled':False})
                        self.assertEqual(gate.state['target']['resources'],[record])
                        if drift in ('foreign','changed-uid'):product.assert_not_called()
                    else:
                        gate.refusal_config({'mongodb_network_policy_enabled':False});product.assert_called_once()
                        self.assertEqual(gate.state['target']['resources'],[])
                        # Restoring the original product config can now capture
                        # its new policy UID; the old one is no longer adopted.
                        product.reset_mock();gate.refusal_config({});product.assert_called_once()
                        self.assertIs(yaml.safe_load(directory.joinpath('config.yml').read_text())['mongodb_network_policy_enabled'],True)

    def test_disaster_source_loss_requires_every_resource_and_volume_absent(self):
        for remaining in (None, 'last-secret', 'old-pv'):
            with self.subTest(remaining=remaining), tempfile.TemporaryDirectory() as tmp:
                gate=self.gate(tmp)
                gate.state['disaster']={'lost':{'resources':[
                    {'kind':'psmdb','name':'old'},
                    {'kind':'pvc','name':'old-claim','volume_name':'old-pv'},
                    {'kind':'secret','name':'last-secret'}]}}
                def get(kind,name,*args):return {'metadata':{'uid':'still-present'}} if name==remaining else None
                with patch.object(gate.lab,'get',side_effect=get):
                    if remaining:
                        with self.assertRaisesRegex(ValueError,'still has'):gate.disaster_absent()
                    else:gate.disaster_absent()

    def test_sharded_restore_proves_both_shard_boundaries_catalog_and_failure_cleanup(self):
        from datetime import datetime, timezone
        epoch=int(datetime(2026,10,16,tzinfo=timezone.utc).timestamp())
        for failure in (None,'backup','oplog','restore','catalog','interrupt','cleanup'):
            with self.subTest(failure=failure),tempfile.TemporaryDirectory() as tmp:
                gate=self.gate(tmp);gate.base=Path(tmp)
                Path(tmp,'tests').symlink_to(ROOT/'tests',target_is_directory=True)
                gate.state.update(bucket=gate.state['name'],backup={'accepted':'unchanged'})
                gate.save();events=[];programs=[];prefix='sharding/'+gate.state['run_id']
                directory=Path(tmp,'clusters/mongodb-restore-logical')
                def prepare(**kwargs):
                    self.assertEqual(kwargs,{'fresh_credentials':True,'backup_prefix':prefix,'sharded':True})
                    directory.mkdir(parents=True);directory.joinpath('config.yml').write_text('{}')
                    gate.state['target']={'name':'mongodb-gate-logical-restore','deployment':'mongodb-restore-logical',
                                          'cr_uid':'owned-target'}
                    gate.save();events.append('prepare')
                def js(code,marker,**kwargs):
                    programs.append(code)
                    if failure=='catalog' and 'RESTORED_CATALOG_PASS' in marker:raise ValueError('catalog')
                    if failure=='interrupt' and 'RESTORED_BOUNDARY_PASS' in marker:raise SystemExit(143)
                    if marker=='MONGODB_SHARDING_PITR_BOUNDARY_PASS':
                        return 'MONGODB_SHARDING_PITR_BOUNDARY '+json.dumps({'target_utc':'2026-10-16 00:00:00','target_epoch':epoch})
                    return marker
                def request(row):
                    events.append(row['kind'])
                    if failure==row['kind']:raise ValueError(failure)
                    return {}
                def mc(*args):
                    if args[-1].endswith(prefix+'/'):return ''
                    return '\n'.join(json.dumps({'type':'file','status':'success',
                        'key':prefix+'/pbmPitr/'+rs+'/oplog.s2','size':7})
                        for rs in (['rs0','rs1'] if failure=='oplog' else ['cfg','rs0','rs1']))
                def clean_target():events.append('clean-target');gate.state.pop('target',None);gate.save()
                real_cleanup=gate.cleanup_sharding_restore
                def clean_prefix():
                    events.append('clean-prefix')
                    if failure=='cleanup':raise ValueError('cleanup')
                    real_cleanup()
                with ExitStack() as stack,redirect_stdout(StringIO()) as output:
                    for item in [patch.object(gate,'validate_identity'),patch.object(gate.scenario,'health'),
                        patch.object(gate,'request_for_cleanup'),patch.object(gate,'prepare_target',side_effect=prepare),
                        patch.object(gate,'target_setup'),patch.object(gate,'target_health'),
                        patch.object(gate,'target_request',side_effect=request),patch.object(gate,'trace_restore',return_value=nullcontext()),
                        patch.object(gate,'verify_objects',side_effect=lambda *a,**kw:{'destination':'s3://owned/backup'} if kw.get('prefix') else {'accepted':'unchanged'}),
                        patch.object(gate.scenario,'client_create'),patch.object(gate.scenario,'delete_client'),
                        patch.object(gate.scenario,'js',side_effect=js),patch.object(gate,'mc',side_effect=mc),
                        patch.object(gate.lab,'get',return_value={'status':{'latestRestorableTime':'2026-10-16T00:01:00Z'}}),
                        patch.object(gate.lab,'run',return_value='a'*64+'  -'),patch.object(gate,'diagnostics'),
                        patch.object(gate,'cleanup_target',side_effect=clean_target),
                        patch.object(gate,'cleanup_sharding_restore',side_effect=clean_prefix)]:stack.enter_context(item)
                    if failure=='interrupt':
                        with self.assertRaises(SystemExit):gate.sharding_restore()
                    elif failure:
                        with self.assertRaises(ValueError):gate.sharding_restore()
                    else:gate.sharding_restore()
                    self.assertEqual('MONGODB_SHARDING_RESTORE_PASS' in output.getvalue().splitlines(),failure is None)
                self.assertEqual(events[-2:],['clean-target','clean-prefix'])
                self.assertEqual('sharding_restore' in gate.state,failure=='cleanup')
                if failure is None:
                    self.assertEqual(events,['prepare','backup','restore','clean-target','clean-prefix'])
                    import shutil
                    node=shutil.which('node')
                    self.assertIsNotNone(node,'Node is required for generated client syntax')
                    for program in programs:
                        subprocess.run([node,'--check'],input='const c={},a={};\n'+program,
                                       capture_output=True,text=True,check=True)

    def test_sharded_restore_fixed_cli_uses_logical_fixture(self):
        script='source tests/helpers/mongodb-runner.sh\nmongodb_launch(){ printf "%s|%s\\n" "$1" "$2"; }\nmongodb_dispatch --mongodb-sharding-restore\n'
        result=subprocess.run(['bash','-c',script],cwd=ROOT,capture_output=True,text=True,check=True)
        self.assertEqual(result.stdout.strip(),'sharding-restore|MONGODB_SHARDING_RESTORE_PASS')
        with patch.object(module.sys,'argv',['mongodb-backups.py','sharding-restore','--base',str(ROOT)]), \
                patch.object(module.BackupGate,'sharding_restore') as run,patch.object(module.signal,'signal'):
            self.assertEqual(module.main(),0)
        run.assert_called_once()

    def test_sharded_client_receipt_is_persisted_and_replaced_client_blocks_cleanup(self):
        with tempfile.TemporaryDirectory() as tmp:
            gate=self.gate(tmp);gate.base=Path(tmp)
            gate.state['target']={'name':'mongodb-gate-logical-restore','deployment':'mongodb-restore-logical',
                                  'sharded':True,'resources':[]}
            def create(*args,**kwargs):gate.scenario.client_uid='original-client'
            with patch.object(gate.scenario,'client_create',side_effect=create):gate.sharded_client_create()
            self.assertEqual(json.loads(gate.path.read_text())['target']['client_uid'],'original-client')
            for uid in (None,'replacement'):
                gate.state['target']['client_uid']=uid
                with patch.object(gate,'validate_identity'),patch.object(gate,'capture_target') as capture, \
                        patch.object(gate.lab,'get',return_value={'metadata':{'uid':'original-client'}}), \
                        self.assertRaisesRegex(ValueError,'client identity changed'):
                    gate.cleanup_target()
                capture.assert_not_called()

    def test_sharded_prefix_cleanup_refuses_foreign_prefix_or_active_target(self):
        for drift in (None,'foreign','active'):
            with self.subTest(drift=drift),tempfile.TemporaryDirectory() as tmp:
                gate=self.gate(tmp);gate.state['bucket']=gate.state['name']
                prefix='sharding/'+gate.state['run_id']
                gate.state['sharding_restore']={'prefix':'other' if drift=='foreign' else prefix}
                if drift=='active':gate.state['target']={}
                with patch.object(gate,'validate_identity'),patch.object(gate,'mc',return_value='') as mc:
                    if drift:
                        with self.assertRaises(ValueError):gate.cleanup_sharding_restore()
                        mc.assert_not_called()
                    else:
                        gate.cleanup_sharding_restore()
                        self.assertNotIn('sharding_restore',gate.state)
                        self.assertEqual(mc.call_args_list[0].args,('rm','--recursive','--force',
                            'gate/'+gate.state['bucket']+'/'+prefix+'/'))

    def test_disaster_objects_verify_without_source_or_backup_api(self):
        for drift in (None,'size','digest','duplicate','foreign-prefix'):
            with self.subTest(drift=drift),tempfile.TemporaryDirectory() as tmp:
                gate=self.gate(tmp);gate.state['bucket']=gate.state['name']
                prefix='disaster/'+gate.state['run_id'];key=prefix+'/backup/rs0/file'
                row={'key':key,'size':7,'sha256':'a'*64}
                gate.state['disaster']={'prefix':'foreign' if drift=='foreign-prefix' else prefix,'backup':{'objects':[row]}}
                listing={'type':'file','status':'success','key':key,'size':8 if drift=='size' else 7}
                output=json.dumps(listing)+ ('\n'+json.dumps(listing) if drift=='duplicate' else '')
                with patch.object(gate,'mc',return_value=output),patch.object(gate.lab,'get') as api, \
                        patch.object(gate.lab,'run',return_value=('b' if drift=='digest' else 'a')*64+'  -'):
                    if drift:
                        with self.assertRaises(ValueError):gate.disaster_objects()
                    else:gate.disaster_objects()
                    api.assert_not_called()

    def test_disaster_verification_lists_bucket_to_preserve_mc_relative_key_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            gate=self.gate(tmp);gate.kind='physical';gate.state['bucket']=gate.state['name']
            prefix='disaster/'+gate.state['run_id'];pbm='2026-10-16T01:56:00Z'
            destination='s3://'+gate.state['bucket']+'/'+prefix+'/'+pbm
            meta={'type':'physical','status':'done','name':pbm,'mongodb_version':'8.0.32-14',
                  'pbm_version':'2.15.0','fcv':'8.0','replsets':[{'name':'rs0','status':'done'}]}
            rows=[{'type':'file','status':'success','key':key,'size':7} for key in
                  ('backup/retained-data',prefix+'/'+pbm+'.pbm.json',prefix+'/'+pbm+'/rs0/file')]
            requested=[]
            def mc(*args):
                requested.append(args)
                if args[0]=='cat':return json.dumps(meta)
                # This models pinned mc: asking for a nested path strips that
                # path from JSON keys, while the bucket preserves full keys.
                selected=rows if args[-1]=='gate/'+gate.state['bucket']+'/' else [
                    row|{'key':row['key'].removeprefix(prefix+'/')} for row in rows[1:]]
                return '\n'.join(json.dumps(row) for row in selected)
            backup={'metadata':{'uid':'new-backup'},'status':{'state':'ready','completed':pbm,
                    'pbmName':pbm,'destination':destination}}
            with patch.object(gate,'mc',side_effect=mc),patch.object(gate.lab,'get',return_value=backup), \
                    patch.object(gate.lab,'run',return_value='a'*64+'  -'):
                verified=gate.verify_objects(False,request_name='disaster-backup',prefix=prefix)
                self.assertEqual(len(verified['objects']),2)
                gate.state['disaster']={'prefix':prefix,'backup':verified}
                gate.disaster_objects()
            listings=[args[-1] for args in requested if args[0]=='ls']
            self.assertEqual(listings,['gate/'+gate.state['bucket']+'/']*2)

    def test_disaster_inventory_distinguishes_pbm_coordination_from_backup_data(self):
        for drift in (None, 'extra-data', 'missing', 'size', 'duplicate', 'digest'):
            with self.subTest(drift=drift), tempfile.TemporaryDirectory() as tmp:
                gate = self.gate(tmp)
                gate.state['bucket'] = gate.state['name']
                prefix = 'disaster/' + gate.state['run_id']
                key = prefix + '/2026-10-16T01:56:00Z/rs0/file'
                expected = {'key': key, 'size': 7, 'sha256': 'a'*64}
                gate.state['disaster'] = {'prefix': prefix, 'backup': {'objects': [expected]}}
                rows = [{'type': 'file', 'status': 'success', 'key': key,
                         'size': 8 if drift == 'size' else 7}]
                if drift == 'missing': rows = []
                if drift == 'duplicate': rows *= 2
                if drift == 'extra-data':
                    rows.append({'type': 'file', 'status': 'success', 'key': key + '-extra', 'size': 1})
                rows.extend({'type': 'file', 'status': 'success', 'key': prefix + '/' + name, 'size': size}
                            for name, size in (('.pbm.init', 6), ('.pbm.restore/restore/cluster.hb', 10),
                                               ('.pbm.restore/restore.json', 200)))
                with patch.object(gate, 'mc', return_value='\n'.join(map(json.dumps, rows))), \
                        patch.object(gate.lab, 'get') as api, \
                        patch.object(gate.lab, 'run', return_value=('b' if drift == 'digest' else 'a')*64+'  -'):
                    if drift:
                        with self.assertRaises(ValueError): gate.disaster_objects()
                    else:
                        gate.disaster_objects()
                    api.assert_not_called()

    def test_disaster_cleanup_refuses_foreign_prefix_escrow_or_active_target(self):
        for drift in (None,'prefix','symlink','changed','permissions','target'):
            with self.subTest(drift=drift),tempfile.TemporaryDirectory() as tmp:
                gate=self.gate(tmp);gate.state['bucket']=gate.state['name']
                escrow=Path(tmp)/('mongodb-disaster-escrow-'+gate.state['run_id']+'.json')
                escrow.write_text('private-escrow');escrow.chmod(0o600)
                gate.state['disaster']={'prefix':'foreign' if drift=='prefix' else 'disaster/'+gate.state['run_id'],
                    'escrow_sha256':hashlib.sha256(escrow.read_bytes()).hexdigest()}
                if drift=='symlink':
                    escrow.unlink();escrow.symlink_to(Path(tmp)/'foreign')
                if drift=='changed':escrow.write_text('foreign')
                if drift=='permissions':escrow.chmod(0o644)
                if drift=='target':gate.state['target']={}
                with patch.object(gate,'validate_identity'),patch.object(gate,'mc',return_value='') as mc:
                    if drift:
                        with self.assertRaises(ValueError):gate.cleanup_disaster()
                        mc.assert_not_called();self.assertIn('disaster',gate.state)
                    else:
                        gate.cleanup_disaster();self.assertNotIn('disaster',gate.state);self.assertFalse(escrow.exists())
                        self.assertEqual(mc.call_args_list[0].args,('rm','--recursive','--force',
                            'gate/'+gate.state['bucket']+'/disaster/'+gate.state['run_id']+'/'))

    def test_disaster_prefix_validation_precedes_backup_api(self):
        with tempfile.TemporaryDirectory() as tmp:
            gate=self.gate(tmp)
            with patch.object(gate.lab,'get') as get,patch.object(gate,'mc') as mc:
                with self.assertRaisesRegex(ValueError,'foreign prefix'):
                    gate.verify_objects(False,prefix='disaster/other-run')
                get.assert_not_called();mc.assert_not_called()

    def test_disaster_fixed_launcher_and_cli_share_physical_fixture(self):
        script='source tests/helpers/mongodb-runner.sh\nmongodb_launch(){ printf "%s|%s\\n" "$1" "$2"; }\nmongodb_dispatch --mongodb-disaster-recovery\n'
        result=subprocess.run(['bash','-c',script],cwd=ROOT,capture_output=True,text=True,check=True)
        self.assertEqual(result.stdout.strip(),'disaster-recovery|MONGODB_DISASTER_RECOVERY_PASS')
        with patch.object(module.sys,'argv',['mongodb-backups.py','disaster-recovery','--base',str(ROOT)]), \
                patch.object(module.BackupGate,'disaster') as run,patch.object(module.signal,'signal'):
            self.assertEqual(module.main(),0);run.assert_called_once()

    def test_disaster_success_requires_source_loss_restore_data_and_cleanup(self):
        for failed in (None,'prepare','backup','loss','objects','restore','data','interrupt','cleanup'):
            with self.subTest(failed=failed),tempfile.TemporaryDirectory() as tmp:
                gate=self.gate(tmp);gate.base=Path(tmp);gate.kind='physical';gate.state['kind']='physical'
                gate.state.update(bucket=gate.state['name'],backup={'accepted':'unchanged'})
                gate.save();events=[];generation=[]
                def prepare(**kwargs):
                    if failed=='prepare':raise ValueError('prepare')
                    generation.append(kwargs);n=len(generation)
                    name='mongodb-gate-physical-restore'
                    directory=Path(tmp)/'clusters/mongodb-restore-physical';directory.mkdir(parents=True,exist_ok=True)
                    directory.joinpath('config.yml').write_text('{}')
                    gate.state['target']={'name':name,'deployment':'mongodb-restore-physical','cr_uid':str(n),
                        'resources':[{'kind':'psmdb','name':name,'uid':'cr-'+str(n)},
                                     {'kind':'pvc','name':'claim','uid':'claim-'+str(n),'volume_name':'pv-'+str(n)}]}
                    gate.save();events.append('prepare-'+str(n))
                def request(row):
                    events.append(row['kind'])
                    if row['kind']==failed:raise ValueError(failed)
                    return {'metadata':{'finalizers':['internal.percona.com/release-lock']}}
                def absent():
                    events.append('absent')
                    if failed=='loss':raise ValueError('loss')
                def objects():
                    events.append('objects')
                    if failed=='objects':raise ValueError('objects')
                def js(code,marker,**kwargs):
                    if 'RECOVERED_DATA_PASS' in marker:
                        events.append('data')
                        if failed=='interrupt':raise SystemExit(143)
                        if failed=='data':raise ValueError('data')
                def clean_target():
                    events.append('clean-target');gate.state.pop('target',None);gate.save()
                real_cleanup=gate.cleanup_disaster
                def clean_disaster():
                    events.append('clean-disaster')
                    if failed=='cleanup':raise ValueError('cleanup')
                    real_cleanup()
                with ExitStack() as stack,redirect_stdout(StringIO()) as output:
                    for item in [patch.object(gate,'validate_identity'),patch.object(gate.scenario,'health'),
                        patch.object(gate,'request_for_cleanup'),patch.object(gate,'mc',return_value=''),
                        patch.object(gate,'verify_objects',side_effect=lambda *a,**kw:
                            {'destination':'s3://owned/disaster/backup','objects':[]} if kw.get('prefix') else {'accepted':'unchanged'}),
                        patch.object(gate,'prepare_target',side_effect=prepare),patch.object(gate,'target_setup'),
                        patch.object(gate.scenario,'client_create'),patch.object(gate.scenario,'delete_client'),
                        patch.object(gate.scenario,'js',side_effect=js),
                        patch.object(gate.lab,'get',return_value={'data':{'private':'cHJpdmF0ZQ=='}}),
                        patch.object(gate,'target_request',side_effect=request),patch.object(gate,'delete_request'),
                        patch.object(gate,'capture_target'),patch.object(gate,'cleanup_target',side_effect=clean_target),
                        patch.object(gate,'cleanup_disaster',side_effect=clean_disaster),
                        patch.object(gate,'disaster_absent',side_effect=absent),patch.object(gate,'disaster_objects',side_effect=objects),
                        patch.object(gate,'trace_restore',return_value=nullcontext()),patch.object(gate,'target_health'),
                        patch.object(gate,'diagnostics')]:stack.enter_context(item)
                    if failed=='interrupt':
                        with self.assertRaises(SystemExit):gate.disaster()
                    elif failed:
                        with self.assertRaises(ValueError):gate.disaster()
                    else:gate.disaster()
                    self.assertEqual('MONGODB_DISASTER_RECOVERY_PASS' in output.getvalue().splitlines(),failed is None)
                    self.assertEqual(events[-2:],['clean-target','clean-disaster'])
                    if failed!='cleanup':self.assertNotIn('disaster',gate.state)
                    else:self.assertIn('disaster',gate.state)
                if failed is None:
                    self.assertEqual(generation[0],{'fresh_credentials':True,'backup_prefix':'disaster/'+gate.state['run_id']})
                    self.assertIn('escrow',generation[1]);self.assertLess(events.index('absent'),events.index('prepare-2'))
                    self.assertLess(events.index('objects'),events.index('prepare-2'))
                    self.assertNotEqual(gate.state['disaster_result']['source_uid'],gate.state['disaster_result']['target_uid'])
                    self.assertEqual(gate.state['backup'],{'accepted':'unchanged'})

    def test_refusal_requires_terminal_matching_native_cause(self):
        causes = {'credentials': 'InvalidAccessKeyId: invalid account', 'storage': 'dial tcp: connection refused',
                  'oplog': 'there is no oplogs that can cover the date/time or no oplogs at all'}
        for phase, message in causes.items():
            self.assertEqual(module.refusal_reason(phase, {'state':'error','error':message}),
                             hashlib.sha256(message.encode()).hexdigest())
            for state in ('ready', 'waiting', 'running', ''):
                with self.assertRaises(ValueError):module.refusal_reason(phase, {'state':state,'error':message})
            for other in ('operation deadline exceeded', 'x509: certificate signed by unknown authority',
                          'authentication failed', *[v for k,v in causes.items() if k!=phase]):
                with self.assertRaises(ValueError):module.refusal_reason(phase, {'state':'error','error':other})

    def test_refusal_preflight_requires_current_owned_config_and_fresh_native_agent_cause(self):
        for phase,drift in ((p,d) for p in ('credentials','storage') for d in (None,'uid','foreign','config','credentials','old-log','other-target','other-member','timeout','success','request-exists')):
            with self.subTest(phase=phase,drift=drift),tempfile.TemporaryDirectory() as tmp:
                gate=self.gate(tmp);name='mongodb-gate-logical-restore'
                applied={'mongodb_backup_storage_name':'main','mongodb_backup_storage_type':'minio',
                    'mongodb_backup_credentials_secret':'wrong','mongodb_backup_prefix':'refusal/run',
                    'mongodb_backup_bucket':'bucket','mongodb_backup_endpoint':'https://store'}
                gate.state['target']={'name':name,'cr_uid':'target','deployment':'mongodb-restore-logical',
                    'refusal_'+phase+'_rejection':{'rc':0 if drift=='success' else 1},
                    'refusal':{'phase':phase,'started_at':'2026-10-16T00:00:00+00:00','applied_config':applied}}
                gate.base=Path(tmp);directory=gate.base/'clusters/mongodb-restore-logical';directory.mkdir(parents=True)
                directory.joinpath('config.yml').write_text(yaml.safe_dump(applied|({'changed':True} if drift=='config' else {})))
                cr={'metadata':{'uid':'foreign' if drift=='uid' else 'target','generation':2},
                    'spec':{'backup':{'enabled':True,'storages':{'main':{'type':'minio','minio':{
                        'credentialsSecret':'other' if drift=='credentials' else 'wrong','prefix':'refusal/run',
                        'bucket':'bucket','endpointUrl':'https://store','insecureSkipTLSVerify':False}}}}}}
                record={'controller':'psmdb-controller','PerconaServerMongoDB':{
                    'name':'other' if drift=='other-target' else name,'namespace':'mongodb-gate'},
                    'node':('other' if drift=='other-member' else name)+'-rs0-0.'+name+'-rs0.mongodb-gate.svc.cluster.local:27017',
                    'err':'operation deadline exceeded' if drift=='timeout' else ('storage check failed: The Access Key Id you provided does not exist in our records.' if phase=='credentials' else 'storage check failed: dial tcp: connection refused')}
                log=('2026-10-15T23:59:59Z' if drift=='old-log' else '2026-10-16T00:00:05Z')+'\tINFO\tPBM\tAgent is not OK\t'+json.dumps(record)
                def get(kind,*args):return cr if kind=='psmdb' else ({'metadata':{'uid':'request'}} if drift=='request-exists' else None)
                with patch.object(gate.lab,'get',side_effect=get),patch.object(gate.lab,'run',return_value=log), \
                        patch.object(gate.scenario.product,'owned',return_value=drift!='foreign'):
                    if drift:
                        with self.assertRaises(ValueError):gate.refusal_preflight(phase,{'name':'request'})
                    else:
                        result=gate.refusal_preflight(phase,{'name':'request'})
                        self.assertEqual(result['state'],'blocked-before-request')
                        self.assertFalse(result['request_created']);self.assertEqual(result['generation'],2)

    def test_refusal_configuration_preserves_original_and_records_request_before_product(self):
        from tests.unit.test_mongodb_delivery import configuration
        for drift in (False,True):
            with self.subTest(drift=drift),tempfile.TemporaryDirectory() as tmp:
                gate=self.gate(tmp);gate.base=Path(tmp)
                name='mongodb-gate-logical-restore';directory=Path(tmp,'clusters','mongodb-restore-logical')
                directory.mkdir(parents=True)
                original=configuration()|{'mongodb_name':name,'mongodb_backup_enabled':True,'mongodb_requests':[]}
                gate.state['target']={'name':name,'deployment':'mongodb-restore-logical',
                    'refusal':{'original_config':original,'phase':'credentials'}}
                path=directory/'config.yml';current=original|({'mongodb_pvc_size':'100Ti'} if drift else {})
                path.write_text(yaml.safe_dump(current));request={'kind':'backup','name':name+'-0123456789abcdef-credentials','type':'logical'}
                def reject(phase):
                    saved=json.loads(gate.path.read_text())['target']
                    self.assertEqual(saved['refusal_requests'][request['name']]['request'],request)
                    self.assertEqual(yaml.safe_load(path.read_text())['mongodb_requests'],[request])
                    return 'expected rejection'
                with patch.object(gate,'expected_target_failure',side_effect=reject) as product:
                    if drift:
                        with self.assertRaisesRegex(ValueError,'concurrent target'):gate.refusal_config({},request,expected=True)
                        product.assert_not_called();self.assertEqual(yaml.safe_load(path.read_text()),current)
                    else:
                        self.assertEqual(gate.refusal_config({'mongodb_request_timeout':60},request,expected=True),'expected rejection')
                        self.assertEqual(gate.state['target']['refusal']['original_config'],original)

    def test_refusal_quorum_recovery_validates_last_member_before_first_mutation(self):
        for drift in (None,'omitted-empty','last-member','policy-uid','policy-spec','source','run-id'):
            with self.subTest(drift=drift),tempfile.TemporaryDirectory() as tmp:
                gate=self.gate(tmp);name='mongodb-gate-logical-restore';prefix=gate.state['source_prefix']
                key='kubeauto.io/mongodb-quorum-fault'
                members=[{'name':name+'-rs0-'+str(i),'uid':str(i)} for i in range(2)]
                spec={'podSelector':{'matchLabels':{key:prefix}},'ingress':[],'egress':[],'policyTypes':['Ingress','Egress']}
                fault={'kind':'quorum','run_id':'foreign' if drift=='run-id' else prefix,
                       'name':name+'-refusal-deny','uid':'deny','spec':spec,'members':members}
                gate.state['target']={'name':name,'source_identity':{'source':'fixed'},'refusal':{'fault':fault}}
                objects={('pod',r['name']):{'metadata':{'name':r['name'],'uid':r['uid'],'labels':{key:prefix}}} for r in members}
                objects[('networkpolicy',fault['name'])]={'metadata':{'name':fault['name'],'uid':'foreign' if drift=='policy-uid' else 'deny',
                    'namespace':'mongodb-gate','labels':module.LABELS|{'kubeauto.io/test-run':gate.state['run_id']}},
                    'spec':{} if drift=='policy-spec' else spec}
                if drift=='omitted-empty':objects[('networkpolicy',fault['name'])]['spec']={k:v for k,v in spec.items() if k not in ('ingress','egress')}
                if drift=='last-member':objects[('pod',members[-1]['name'])]['metadata']['uid']='foreign'
                with patch.object(gate,'source_identity',return_value={} if drift=='source' else {'source':'fixed'}), \
                        patch.object(gate.lab,'get',side_effect=lambda k,n,ns:objects[(k,n)]), \
                        patch.object(gate,'api_input') as mutate,patch.object(gate.lab,'run'):
                    if drift not in (None,'omitted-empty'):
                        with self.assertRaises(ValueError):gate.refusal_fault_recover()
                        mutate.assert_not_called();self.assertIn('fault',gate.state['target']['refusal'])
                    else:
                        gate.refusal_fault_recover();self.assertEqual(mutate.call_count,3)
                        self.assertNotIn('fault',gate.state['target']['refusal'])

    def test_refusal_storage_recovery_refuses_foreign_service_without_patching(self):
        for drift in (None,'uid','selector','labels'):
            with self.subTest(drift=drift),tempfile.TemporaryDirectory() as tmp:
                gate=self.gate(tmp);original={'original':'selector'};applied=original|{'fault':'run'}
                fault={'kind':'service','uid':'service','original':original,'applied':applied}
                gate.state['target']={'source_identity':{},'refusal':{'fault':fault}}
                obj={'metadata':{'name':gate.state['name'],'uid':'foreign' if drift=='uid' else 'service',
                    'namespace':'mongodb-gate','labels':{} if drift=='labels' else
                    module.LABELS|{'kubeauto.io/test-run':gate.state['run_id']}},
                    'spec':{'selector':{} if drift=='selector' else applied}}
                with patch.object(gate,'source_identity',return_value={}),patch.object(gate.lab,'get',return_value=obj), \
                        patch.object(gate,'api_input') as mutate,patch.object(gate,'mc'):
                    if drift:
                        with self.assertRaises(ValueError):gate.refusal_fault_recover()
                        mutate.assert_not_called()
                    else:
                        gate.refusal_fault_recover();mutate.assert_called_once()
                        self.assertEqual(mutate.call_args.kwargs['obj'][-1]['value'],original)

    def test_refusal_request_ownership_accepts_only_fixed_phase_names(self):
        target='mongodb-gate-logical-restore';run_id='0123456789abcdef'
        for kind in ('PerconaServerMongoDBBackup','PerconaServerMongoDBRestore'):
            for phase in ('credentials','storage','quorum','oplog','foreign'):
                obj={'kind':kind,'metadata':{'name':target+'-'+run_id+'-'+phase,'namespace':'mongodb-gate',
                     'labels':{'app.kubernetes.io/managed-by':'kubeauto','kubeauto.io/component':'mongodb'}}}
                self.assertEqual(module.target_resource_owned(obj,target,run_id),phase!='foreign')

    def test_refusal_cleans_failed_target_and_never_emits_pass_on_partial_or_interrupted_run(self):
        for failed in ('target','credentials','storage','quorum','oplog','interrupt','tail','oplog-only'):
            with self.subTest(failed=failed),tempfile.TemporaryDirectory() as tmp:
                gate=self.gate(tmp);gate.base=Path(tmp);gate.state['bucket']=gate.state['name']
                backup={'metadata':{'last_write_ts':{'T':100,'I':1}},'destination':'s3://bucket/backup/full'}
                gate.state['backup']=backup;gate.save();events=[]
                def prepare():
                    if failed=='target':raise ValueError('first failure')
                    name='mongodb-gate-logical-restore';directory=Path(tmp,'clusters','mongodb-restore-logical')
                    directory.mkdir(parents=True);directory.joinpath('config.yml').write_text('{}')
                    gate.state['target']={'name':name,'deployment':'mongodb-restore-logical','cr_uid':'target'}
                def config(settings,request=None,**kwargs):
                    if kwargs.get('expected'):
                        phase=gate.state['target']['refusal']['phase'];events.append(phase)
                        if failed==phase:raise ValueError('first failure')
                        if failed=='interrupt':raise SystemExit(143)
                def get(kind,name=None,ns=None):
                    if kind=='secret':return {'metadata':{'uid':'credentials'}} if name==gate.state['name']+'-credentials' else None
                    if kind=='service':return {'metadata':{'uid':'service'},'spec':{'selector':{}}}
                    if kind=='endpoints':return {'subsets':[]}
                    if kind=='psmdb-backup':
                        phase=gate.state['target']['refusal']['phase']
                        return {'status':{'state':'error','error':'InvalidAccessKeyId' if phase=='credentials' else 'connection refused'}}
                    if kind=='psmdb-restore':return {'status':{'state':'error','error':'there is no oplogs that can cover the date/time or no oplogs at all'}}
                def quorum():
                    if failed=='quorum':raise ValueError('first failure')
                    gate.state['target']['refusal']['results']['quorum']={}
                with ExitStack() as stack,redirect_stdout(StringIO()) as output:
                    for item in [patch.object(gate,'validate_identity'),patch.object(gate.scenario,'health'),
                                 patch.object(gate,'request_for_cleanup'),patch.object(gate,'verify_objects',return_value=backup),
                                 patch.object(gate,'mc',return_value=''),patch.object(gate,'prepare_target',side_effect=prepare),
                                 patch.object(gate.lab,'get',side_effect=get),patch.object(gate,'capture_target'),
                                 patch.object(gate,'api_input'),patch.object(gate,'owned',return_value=True),
                                 patch.object(gate,'refusal_config',side_effect=config),patch.object(gate,'refusal_quorum',side_effect=quorum),
                                 patch.object(gate,'refusal_fault_recover',side_effect=lambda:events.append('recover-fault')),
                                 patch.object(gate,'cleanup_target',side_effect=lambda:events.append('clean-target')),
                                 patch.object(gate,'refusal_prefix_cleanup'),
                                 patch.object(gate,'delete_request'),patch.object(gate,'diagnostics'),
                                 patch.object(gate,'target_health'),patch.object(gate.scenario,'client_create'),
                                 patch.object(gate.scenario,'js'),patch.object(gate.scenario,'delete_client')]:stack.enter_context(item)
                    if failed=='interrupt':
                        with self.assertRaises(SystemExit):gate.refusal()
                    elif failed=='tail':
                        gate.refusal(focus='tail')
                        self.assertNotIn('credentials',events);self.assertNotIn('storage',events)
                        self.assertEqual(set(gate.state['refusal_results']),{'quorum','oplog'})
                        self.assertIn('MONGODB_BACKUP_REFUSAL_TAIL_PASS',output.getvalue().splitlines())
                    elif failed=='oplog-only':
                        gate.refusal(focus='oplog')
                        self.assertNotIn('credentials',events);self.assertNotIn('storage',events)
                        self.assertEqual(set(gate.state['refusal_results']),{'oplog'})
                        self.assertIn('MONGODB_BACKUP_REFUSAL_OPLOG_ONLY_PASS',output.getvalue().splitlines())
                        self.assertNotIn('MONGODB_BACKUP_REFUSAL_TAIL_PASS',output.getvalue().splitlines())
                    else:
                        with self.assertRaisesRegex(ValueError,'first failure'):gate.refusal()
                    self.assertEqual(events[-1],'clean-target')
                    self.assertNotIn('MONGODB_BACKUP_REFUSAL_PASS',output.getvalue().splitlines())

    def test_refusal_dispatch_uses_fixed_runner_and_logical_backup_gate(self):
        script='source tests/helpers/mongodb-runner.sh\nmongodb_launch(){ printf "%s|%s\\n" "$1" "$2"; }\nmongodb_dispatch --mongodb-backup-refusal\n'
        result=subprocess.run(['bash','-c',script],cwd=ROOT,capture_output=True,text=True,check=True)
        self.assertEqual(result.stdout.strip(),'backup-refusal|MONGODB_BACKUP_REFUSAL_PASS')
        result=subprocess.run(['bash','-c',script.replace('--mongodb-backup-refusal\n','--mongodb-backup-refusal-tail\n')],cwd=ROOT,capture_output=True,text=True,check=True)
        self.assertEqual(result.stdout.strip(),'backup-refusal-tail|MONGODB_BACKUP_REFUSAL_TAIL_PASS')
        result=subprocess.run(['bash','-c',script.replace('--mongodb-backup-refusal\n','--mongodb-backup-refusal-oplog\n')],cwd=ROOT,capture_output=True,text=True,check=True)
        self.assertEqual(result.stdout.strip(),'backup-refusal-oplog|MONGODB_BACKUP_REFUSAL_OPLOG_ONLY_PASS')
        with patch.object(module.sys,'argv',['mongodb-backups.py','backup-refusal-tail','--base',str(ROOT)]), \
                patch.object(module,'BackupGate') as gate:
            self.assertEqual(module.main(),0)
            gate.assert_called_once_with(ROOT,'logical',pitr=False)
            gate.return_value.refusal.assert_called_once_with(focus='tail')
        with patch.object(module.sys,'argv',['mongodb-backups.py','backup-refusal-oplog','--base',str(ROOT)]), \
                patch.object(module,'BackupGate') as gate:
            self.assertEqual(module.main(),0)
            gate.assert_called_once_with(ROOT,'logical',pitr=False)
            gate.return_value.refusal.assert_called_once_with(focus='oplog')

    def test_refusal_prefix_cleanup_requires_target_absence_and_unchanged_manual_backup(self):
        for drift in (None,'target','backup','residue'):
            with self.subTest(drift=drift),tempfile.TemporaryDirectory() as tmp:
                gate=self.gate(tmp);gate.state.update(bucket=gate.state['name'],backup={'manual':'sha256'},refusal_pending=True)
                if drift=='target':gate.state['target']={'owned':'target'}
                listing=json.dumps({'status':'success','type':'file','key':'survivor'}) if drift=='residue' else ''
                with patch.object(gate,'validate_identity'),patch.object(gate,'verify_objects',return_value={} if drift=='backup' else gate.state['backup']), \
                        patch.object(gate,'mc',side_effect=lambda *args:listing if args[0]=='ls' else '') as mc:
                    if drift:
                        with self.assertRaises(ValueError):gate.refusal_prefix_cleanup()
                        self.assertIn('refusal_pending',gate.state)
                        if drift in ('target','backup'):mc.assert_not_called()
                    else:
                        gate.refusal_prefix_cleanup();self.assertNotIn('refusal_pending',gate.state)
                        self.assertEqual(mc.call_args_list[0].args,('rm','--recursive','--force',
                            'gate/'+gate.state['bucket']+'/refusal/'+gate.state['run_id']+'/'))

    def test_retention_native_cleanup_reenables_storage_then_restores_original_config_last(self):
        with tempfile.TemporaryDirectory() as tmp:
            gate=self.gate(tmp);events=[]
            backup={'manual':'frozen'};obj={'metadata':{'name':'scheduled','uid':'scheduled-uid'}}
            gate.state.update(backup=backup,retention={'records':{'scheduled':{'uid':'scheduled-uid','backup':{'saved':True}}}})
            def configure():
                events.append('enable-storage');gate.state['original_config']={'mongodb_backup_enabled':False}
            def remove(obj):events.append('native-delete')
            with ExitStack() as stack,redirect_stdout(StringIO()):
                for item in [patch.object(gate,'validate_identity'),patch.object(gate,'retention_fault'),
                             patch.object(gate,'recover_retention_request'),patch.object(gate,'retention_pause'),
                             patch.object(gate,'cleanup_target'),patch.object(gate,'cleanup_client'),
                             patch.object(gate,'retention_records',side_effect=lambda:[obj] if 'native-delete' not in events else []),
                             patch.object(gate,'configure',side_effect=configure),
                             patch.object(gate,'delete_request',side_effect=remove),
                             patch.object(gate,'retention_expired',return_value=True),
                             patch.object(gate,'verify_objects',return_value=backup),
                             patch.object(gate,'restore_configuration',side_effect=lambda:events.append('restore-config')),
                             patch.object(gate.scenario,'health')]:stack.enter_context(item)
                gate.retention_recover()
            self.assertEqual(events,['enable-storage','native-delete','restore-config'])

    def test_retained_restore_accepts_only_its_converged_paused_storage_transition(self):
        from tests.unit.test_mongodb_delivery import configuration,render
        for drift in (None,'omitted-false','skip-tls','schedule','live-schedule','config','bucket','uid','non-retention'):
            with self.subTest(drift=drift),tempfile.TemporaryDirectory() as tmp:
                gate=self.gate(tmp);name='scheduled';backup={'uid':'scheduled','objects':['sha256']}
                c=configuration()|{'mongodb_name':gate.scenario.fixture.NAME,'mongodb_namespace':'mongodb-gate','mongodb_backup_enabled':True,
                    'mongodb_backup_schedule_enabled':False,'mongodb_backup_storage_type':'minio',
                    'mongodb_backup_bucket':'owned-bucket','mongodb_backup_prefix':'backup',
                    'mongodb_backup_endpoint':gate.state['name']+'.mongodb-gate.svc.cluster.local:9000',
                    'mongodb_backup_credentials_secret':gate.state['name']+'-credentials',
                    'mongodb_backup_ca_secret':gate.state['name']+'-tls'}
                source=render(c)[0];source['metadata']['uid']='source'
                gate.state.update(source_cluster_uid='source',bucket='owned-bucket',backup={'manual':'fixed'},
                    original_config={'mongodb_backup_enabled':False},applied_config=c,
                    desired_request={'spec':{'storageName':c['mongodb_backup_storage_name']}},
                    retention={'retained':{'name':name,'backup':backup},'records':{name:{'backup':backup}}})
                config=dict(c)
                if drift=='schedule':gate.state['applied_config']['mongodb_backup_schedule_enabled']=True
                if drift=='config':config['mongodb_backup_bucket']='foreign'
                if drift=='bucket':source['spec']['backup']['storages'][c['mongodb_backup_storage_name']]['minio']['bucket']='foreign'
                if drift=='uid':source['metadata']['uid']='foreign'
                storage=source['spec']['backup']['storages'][c['mongodb_backup_storage_name']]['minio']
                if drift=='omitted-false':storage.pop('insecureSkipTLSVerify')
                if drift=='skip-tls':storage['insecureSkipTLSVerify']=True
                if drift=='live-schedule':source['spec']['backup']['tasks'][0]['enabled']=True
                gate.config_path.write_text(yaml.safe_dump(config));gate.save()
                with patch.object(gate,'validate_identity'),patch.object(gate.scenario,'health'), \
                        patch.object(gate.lab,'get',return_value=source),patch.object(gate,'request_for_cleanup'), \
                        patch.object(gate,'retention_records',return_value=[{'metadata':{'name':name}}]), \
                        patch.object(gate,'verify_objects',return_value=backup), \
                        patch.object(gate,'prepare_target',side_effect=ValueError('stop-before-target')) as target, \
                        patch.object(gate,'cleanup_target'),patch.object(gate,'diagnostics'):
                    accepted=drift in (None,'omitted-false')
                    expected='stop-before-target' if accepted else 'accepted backup and no pending recovery'
                    with self.assertRaisesRegex(ValueError,expected):gate.restore_independent(retention=drift!='non-retention')
                    self.assertEqual(target.called,accepted)
    def test_retention_request_repair_preserves_manual_backup_and_uses_native_owned_deletion(self):
        from tests.unit.test_mongodb_delivery import configuration
        for drift in (None,'original','uid','objects','finalizer','invented-finalizer','source'):
            with self.subTest(drift=drift),tempfile.TemporaryDirectory() as tmp:
                gate=self.gate(tmp);expected=gate.scenario.fixture.NAME+'-logical-'+gate.state['run_id']
                c=configuration()|{'mongodb_name':gate.scenario.fixture.NAME,'mongodb_namespace':'mongodb-gate',
                                   'mongodb_backup_enabled':True}
                request={'kind':'backup','name':gate.scenario.fixture.NAME+'-logical-'+'e'*16,'type':'logical'}
                desired=gate.scenario.product.operation_object(c,request)
                current=copy.deepcopy(desired);current['metadata'].update(uid='abandoned',resourceVersion='10')
                current['metadata']['finalizers']=['internal.percona.com/release-lock']
                current['status']={'state':'ready'}
                source={'metadata':{'uid':'source','labels':gate.scenario.product.LABELS}}
                gate.state.update(request=request,desired_request=desired,source_cluster_uid='source',
                    backup={'uid':'original'},retention={'source_identity':{'stable':'source'}})
                events=[]
                def verify(**kw):
                    if kw['request_name']==expected:return {'uid':'different' if drift=='original' else 'original'}
                    if drift=='objects' and events:return {'uid':'changed-object'}
                    return {'uid':'abandoned'}
                def configure():
                    events.append('configure')
                    gate.state['request']={'kind':'backup','name':expected,'type':'logical'}
                    if drift=='uid':current['metadata']['uid']='replaced'
                    if drift=='finalizer':current['metadata']['finalizers']=['foreign']
                    if drift=='invented-finalizer':current['metadata']['finalizers']=['percona.com/release-backup-lock']
                with patch.object(gate,'source_identity',return_value={'changed':True} if drift=='source' else {'stable':'source'}), \
                        patch.object(gate.lab,'get',side_effect=lambda kind,*a,**kw:source if kind=='psmdb' else current), \
                        patch.object(gate,'verify_objects',side_effect=verify),patch.object(gate,'configure',side_effect=configure), \
                        patch.object(gate,'api_input',side_effect=lambda *a,**kw:events.append('patch')) as submit, \
                        patch.object(gate,'delete_request',side_effect=lambda o:events.append('delete')) as delete, \
                        patch.object(gate,'retention_expired',return_value=True):
                    if drift:
                        with self.assertRaises(ValueError):gate.recover_retention_request()
                        delete.assert_not_called();submit.assert_not_called()
                    else:
                        gate.recover_retention_request()
                        self.assertEqual(events,['configure','patch','delete'])
                        self.assertTrue(gate.state['retention']['abandoned_request']['cleaned'])
                        self.assertEqual(gate.state['backup'],{'uid':'original'})
                        patch_ops=submit.call_args.kwargs['obj']
                        self.assertEqual(patch_ops[0],{'op':'test','path':'/metadata/uid','value':'abandoned'})
                        self.assertEqual(patch_ops[-1]['value'],
                                         ['internal.percona.com/release-lock','percona.com/delete-backup'])

    def test_reused_backup_configuration_keeps_journal_request_instead_of_constructor_run_id(self):
        from tests.unit.test_mongodb_delivery import configuration
        with tempfile.TemporaryDirectory() as tmp:
            gate=self.gate(tmp);gate.run_id='e'*16
            config=configuration();gate.config_path.write_text(yaml.safe_dump(config))
            gate.state['bucket']='owned'
            folder=Path(tmp,'yml/mongodb');folder.mkdir(parents=True);folder.joinpath('input.json').write_text('{}')
            expected=gate.scenario.fixture.NAME+'-logical-'+gate.state['run_id']
            with patch.object(gate.lab,'get',return_value={'metadata':{'uid':'source'}}), \
                    patch.object(gate.scenario.product,'operation_object',return_value={}),patch.object(gate,'setup_product'):
                gate.configure(schedule=True)
            self.assertEqual(gate.state['request']['name'],expected)
            self.assertEqual(yaml.safe_load(gate.config_path.read_text())['mongodb_requests'][0]['name'],expected)

    def test_retention_stop_signals_only_verified_scenario_and_preserves_durable_parent(self):
        self.assert_stop_scope('retention')

    def test_refusal_stop_signals_only_verified_scenario_and_preserves_durable_parent(self):
        self.assert_stop_scope('backup-refusal')

    def test_rollback_stop_signals_only_verified_scenario_and_preserves_durable_parent(self):
        self.assert_stop_scope('rollback')
        self.assert_stop_scope('stability')
        self.assert_stop_scope('stability-faults')

    def assert_stop_scope(self, mode):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);proc=root/'proc';proc.mkdir();state=root/'state';state.write_text('100')
            base=Path('/usr/local/kubeauto')
            rows={100:(1,['bash',str(base/'tests/helpers/run-durable-gate.sh'),'/tmp/kubeauto-mongodb-gate',
                              'MONGODB_GATE_EXIT','bash',str(base/'tests/helpers/mongodb-install-gate.sh'),'--'+mode]),
                  101:(100,['bash',str(base/'tests/helpers/mongodb-install-gate.sh'),'--'+mode]),
                  102:(101,['python',str(base/'tests/helpers/mongodb-backups.py'),mode,'--base',str(base)]),
                  103:(102,['python','kubecli.py','setup']),104:(101,['python','registry-relay.py']),
                  900:(1,['python','foreign'])}
            for pid,(parent,args) in rows.items():
                entry=proc/str(pid);entry.mkdir();(entry/'cmdline').write_bytes(('\0'.join(args)+'\0').encode())
                fields=['S',str(parent)]+['0']*17+[str(pid*10)]
                (entry/'stat').write_text(str(pid)+' (python) '+' '.join(fields))
            with patch.object(module.os,'kill') as killed:
                module.stop_retention(base,mode=mode,proc=proc,state=state)
                self.assertEqual([c.args for c in killed.call_args_list],[(102,module.signal.SIGKILL),(103,module.signal.SIGKILL)])
            (proc/'100'/'cmdline').write_bytes(b'bash\0foreign\0')
            with patch.object(module.os,'kill') as killed:
                with self.assertRaisesRegex(ValueError,'unrelated durable'):module.stop_retention(base,mode=mode,proc=proc,state=state)
                killed.assert_not_called()

    def test_refusal_stop_fixed_dispatch_never_syncs_or_launches_a_second_gate(self):
        script='''source tests/helpers/mongodb-runner.sh
ROOT=$PWD
ssh_mongodb(){ printf '%s\\n' "$*"; cat >/dev/null; }
mongodb_launch(){ return 91; }
mongodb_dispatch --mongodb-backup-refusal-stop
'''
        result=subprocess.run(['bash','-c',script],cwd=ROOT,capture_output=True,text=True,check=True)
        self.assertEqual(result.stdout.strip(),'/usr/local/kubeauto/.venv/bin/python - backup-refusal-stop')
        with patch.object(module.sys,'argv',['mongodb-backups.py','backup-refusal-stop','--base',str(ROOT)]), \
                patch.object(module,'stop_retention') as stop,patch.object(module.BackupGate,'__init__') as constructor, \
                patch.object(module.signal,'signal'):
            self.assertEqual(module.main(),0);stop.assert_called_once_with(ROOT,mode='backup-refusal')
            constructor.assert_not_called()
        with self.assertRaises(ValueError):module.stop_retention(ROOT,mode='foreign')

    def test_rollback_stop_fixed_dispatch_never_syncs_or_constructs_a_gate(self):
        script='''source tests/helpers/mongodb-runner.sh
ROOT=$PWD
ssh_mongodb(){ printf '%s\\n' "$*"; cat >/dev/null; }
mongodb_launch(){ return 91; }
mongodb_dispatch --mongodb-rollback-stop
'''
        result=subprocess.run(['bash','-c',script],cwd=ROOT,capture_output=True,text=True,check=True)
        self.assertEqual(result.stdout.strip(),'/usr/local/kubeauto/.venv/bin/python - rollback-stop')
        with patch.object(module.sys,'argv',['mongodb-backups.py','rollback-stop','--base',str(ROOT)]), \
                patch.object(module,'stop_retention') as stop,patch.object(module.BackupGate,'__init__') as constructor, \
                patch.object(module.signal,'signal'):
            self.assertEqual(module.main(),0);stop.assert_called_once_with(ROOT,mode='rollback')
            constructor.assert_not_called()

    def test_stability_stop_dispatch_preserves_active_source_and_durable_parent(self):
        for mode in ('stability','stability-faults'):
            script='source tests/helpers/mongodb-runner.sh\nROOT=$PWD\nssh_mongodb(){ printf "%s\\n" "$*"; cat >/dev/null; }\nmongodb_launch(){ return 91; }\nmongodb_dispatch --mongodb-'+mode+'-stop\n'
            result=subprocess.run(['bash','-c',script],cwd=ROOT,capture_output=True,text=True,check=True)
            self.assertEqual(result.stdout.strip(),'/usr/local/kubeauto/.venv/bin/python - '+mode+'-stop')
            with patch.object(module.sys,'argv',['mongodb-backups.py',mode+'-stop','--base',str(ROOT)]), \
                    patch.object(module,'stop_retention') as stop,patch.object(module.BackupGate,'__init__') as constructor, \
                    patch.object(module.signal,'signal'):
                self.assertEqual(module.main(),0);stop.assert_called_once_with(ROOT,mode=mode)
                constructor.assert_not_called()

    def test_retention_recovery_cancels_requests_only_after_product_stops_schedule(self):
        for refused in (False,True):
            with self.subTest(refused=refused),tempfile.TemporaryDirectory() as tmp:
                gate=self.gate(tmp)
                applied={'mongodb_backup_enabled':True,'mongodb_backup_schedule_enabled':True}
                gate.state.update(original_config={'mongodb_backup_enabled':False,'mongodb_backup_schedule_enabled':False},
                    applied_config=applied,source_cluster_uid='source',retention={})
                gate.config_path.write_text(yaml.safe_dump(applied))
                source={'metadata':{'uid':'source'},'spec':{'backup':{'tasks':[{'enabled':True}]}}}
                stopped=copy.deepcopy(source);stopped['spec']['backup']['tasks'][0]['enabled']=False
                pending={'metadata':{'name':'pending'},'status':{'state':'requested'}}
                events=[]
                process=SimpleNamespace(pid=123,poll=lambda:1 if refused else None,wait=lambda **kw:0)
                def get(*args,**kwargs):
                    events.append('observe');return source if events.count('observe')==1 else stopped
                with patch.object(module.subprocess,'Popen',return_value=process) as popen,patch.object(module.time,'sleep'), \
                        patch.object(module.os,'killpg') as killpg, \
                        patch.object(gate.lab,'get',side_effect=get),patch.object(gate,'retention_records',side_effect=[[pending],[]]), \
                        patch.object(gate,'delete_request',side_effect=lambda o:events.append('delete')):
                    if refused:
                        with self.assertRaisesRegex(ValueError,'before cancellation'):gate.retention_pause(cancel_pending=True)
                        self.assertNotIn('delete',events)
                    else:
                        gate.retention_pause(cancel_pending=True)
                        self.assertEqual(events[:3],['observe','observe','delete'])
                        self.assertTrue(popen.call_args.kwargs['start_new_session'])
                        killpg.assert_called_once_with(123,module.signal.SIGTERM)

    def scheduled_backup(self):
        return {'kind':'PerconaServerMongoDBBackup','metadata':{
            'name':'cron-mongodb-gate-dat-20261016043000-abc12','namespace':'mongodb-gate','uid':'scheduled',
            'creationTimestamp':'2026-10-16T04:30:00Z','finalizers':['percona.com/delete-backup'],
            'labels':{'percona.com/cluster':'mongodb-gate-data','percona.com/backup-ancestor':'daily-full',
                      'percona.com/backup-type':'cron'}},
            'spec':{'clusterName':'mongodb-gate-data','storageName':'main','type':'logical',
                    'compressionType':'gzip','startingDeadlineSeconds':120},
            'status':{'state':'ready','pbmName':'2026-10-16T04:30:02Z','s3':{'secretAccessKey':'PRIVATE'}}}

    def test_scheduled_backup_identity_requires_pinned_source_schedule_spec_time_uid_and_finalizer(self):
        obj=self.scheduled_backup()
        record=module.scheduled_backup_record(obj,'mongodb-gate-data','main','2026-10-16T04:29:00+00:00')
        self.assertEqual(record['uid'],'scheduled')
        self.assertNotIn('PRIVATE',json.dumps(record))
        for field in ('name','uid','namespace','creationTimestamp','finalizers','labels','spec'):
            with self.subTest(field=field):
                bad=copy.deepcopy(obj)
                if field=='spec':bad['spec']['storageName']='foreign'
                elif field=='labels':bad['metadata']['labels']['percona.com/backup-ancestor']='foreign'
                elif field=='finalizers':bad['metadata'][field]=[]
                elif field=='creationTimestamp':bad['metadata'][field]='2026-10-16T03:00:00Z'
                else:bad['metadata'][field]='foreign' if field!='uid' else ''
                with self.assertRaises(ValueError):
                    module.scheduled_backup_record(bad,'mongodb-gate-data','main','2026-10-16T04:29:00+00:00')

    def test_retention_records_freeze_uid_spec_and_verified_data_before_cleanup(self):
        with tempfile.TemporaryDirectory() as tmp:
            gate=self.gate(tmp);obj=self.scheduled_backup()
            gate.state.update(desired_request={'spec':{'storageName':'main'}},retention={
                'started':'2026-10-16T04:29:00+00:00','source_identity':{'uid':'source'},'records':{}})
            with patch.object(gate,'source_identity',return_value={'uid':'source'}), \
                    patch.object(gate.lab,'get',return_value={'items':[obj]}), \
                    patch.object(gate,'verify_objects',return_value={'uid':'scheduled','objects':['sha256']}) as verify:
                gate.retention_records(verify=True);gate.retention_records(verify=True)
                verify.assert_called_once_with(persist=False,request_name=obj['metadata']['name'])
                self.assertEqual(gate.state['retention']['records'][obj['metadata']['name']]['backup']['objects'],['sha256'])
                obj['metadata']['uid']='replaced'
                with self.assertRaisesRegex(ValueError,'identity changed'):gate.retention_records(verify=True)
                self.assertEqual(verify.call_count,1)

    def test_retention_expiry_requires_absence_of_both_metadata_and_every_data_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            gate=self.gate(tmp);gate.state['bucket']='owned'
            backup={'metadata':{'name':'2026-10-16T04:30:02Z'}}
            for key,expired in (('backup/manual.pbm.json',True),
                                ('backup/2026-10-16T04:30:02Z.pbm.json',False),
                                ('backup/2026-10-16T04:30:02Z/rs0/data',False)):
                with patch.object(gate,'mc',return_value=json.dumps({'status':'success','type':'file','key':key})):
                    self.assertEqual(gate.retention_expired(backup),expired)
            with patch.object(gate,'mc',return_value='{"status":"error"}'):
                with self.assertRaises(ValueError):gate.retention_expired(backup)

    def test_retention_pause_interruption_restores_only_its_declared_one_bit_transition(self):
        for concurrent in (False,True):
            with self.subTest(concurrent=concurrent),tempfile.TemporaryDirectory() as tmp:
                gate=self.gate(tmp)
                original={'mongodb_backup_enabled':False,'mongodb_backup_schedule_enabled':False}
                applied={'mongodb_backup_enabled':True,'mongodb_backup_schedule_enabled':False}
                before=applied|{'mongodb_backup_schedule_enabled':True}
                gate.state.update(original_config=original,applied_config=applied,
                    source_cluster_uid='source',retention={'pause_from':before})
                current=before|({'mongodb_backup_enabled':'foreign-edit'} if concurrent else {})
                gate.config_path.write_text(yaml.safe_dump(current))
                with patch.object(gate,'validate_identity'),patch.object(gate.lab,'get',return_value={'metadata':{'uid':'source'}}), \
                        patch.object(gate,'remove_request'),patch.object(gate,'setup_product') as setup,patch.object(gate.scenario,'health'):
                    if concurrent:
                        with self.assertRaisesRegex(ValueError,'concurrent edits'):gate.restore_configuration()
                        setup.assert_not_called();self.assertEqual(yaml.safe_load(gate.config_path.read_text()),current)
                    else:
                        gate.restore_configuration();self.assertEqual(yaml.safe_load(gate.config_path.read_text()),original)

    def test_retained_restore_checks_selected_request_and_frozen_data_before_target_creation(self):
        for changed in (False,True):
            with self.subTest(changed=changed),tempfile.TemporaryDirectory() as tmp:
                gate=self.gate(tmp);name=self.scheduled_backup()['metadata']['name'];snapshot={'uid':'scheduled','objects':['original']}
                gate.state.update(backup={'manual':'fixed'},retention={'retained':{'name':name,'backup':snapshot},
                    'records':{name:{'backup':snapshot}}});gate.save()
                with patch.object(gate,'validate_identity'),patch.object(gate.scenario,'health'), \
                        patch.object(gate,'request_for_cleanup'),patch.object(gate,'retention_records',return_value=[{'metadata':{'name':name}}]), \
                        patch.object(gate,'verify_objects',return_value={'changed':True} if changed else snapshot) as verify, \
                        patch.object(gate,'prepare_target',side_effect=ValueError('stop-before-target')) as target, \
                        patch.object(gate,'diagnostics'),patch.object(gate,'cleanup_target'):
                    with self.assertRaisesRegex(ValueError,'changed backup objects' if changed else 'stop-before-target'):
                        gate.restore_independent(retention=True)
                    verify.assert_called_once_with(persist=False,request_name=name)
                    self.assertEqual(target.call_count,0 if changed else 1)
                    self.assertEqual(gate.state['backup'],{'manual':'fixed'})

    def test_retention_modes_reach_fixed_launcher_with_exact_markers(self):
        result=module.subprocess.run(['bash','-euc','''source tests/helpers/mongodb-runner.sh
mongodb_launch() { printf '%s|%s\\n' "$1" "$2"; }
mongodb_dispatch --mongodb-retention
mongodb_dispatch --mongodb-retention-recover
'''],cwd=ROOT,capture_output=True,text=True,check=True)
        self.assertEqual(result.stdout.splitlines(),['retention|MONGODB_RETENTION_PASS','retention-recover|MONGODB_RETENTION_RECOVER_PASS'])

    def test_retention_schedule_input_validates_and_renders_count_two_with_storage_deletion(self):
        from tests.unit.test_mongodb_delivery import configuration,render,product
        c=configuration()|{'mongodb_backup_enabled':True,'mongodb_backup_storage_type':'minio',
            'mongodb_backup_bucket':'owned-bucket','mongodb_backup_prefix':'backup','mongodb_backup_credentials_secret':'backup-creds',
            'mongodb_backup_endpoint':'minio.mongodb.svc.cluster.local:9000','mongodb_backup_ca_secret':'backup-ca',
            'mongodb_backup_schedule_enabled':True,'mongodb_backup_schedule':'* * * * *',
            'mongodb_backup_schedule_type':'logical','mongodb_backup_retention_count':2}
        product.validate(c)
        task,=render(c)[0]['spec']['backup']['tasks']
        self.assertTrue(task['enabled']);self.assertEqual(task['schedule'],'* * * * *')
        self.assertEqual(task['retention'],{'type':'count','count':2,'deleteFromStorage':True})

    def test_retention_cleanup_validates_all_request_identities_before_first_deletion(self):
        with tempfile.TemporaryDirectory() as tmp:
            gate=self.gate(tmp);gate.state['retention']={}
            with patch.object(gate,'validate_identity'),patch.object(gate,'retention_fault'), \
                    patch.object(gate,'recover_retention_request'),patch.object(gate,'retention_pause'),patch.object(gate,'restore_configuration'), \
                    patch.object(gate,'cleanup_target'),patch.object(gate,'cleanup_client'), \
                    patch.object(gate,'retention_records',side_effect=ValueError('changed second UID')), \
                    patch.object(gate,'delete_request') as delete:
                with self.assertRaisesRegex(ValueError,'changed second UID'):gate.retention_recover()
                delete.assert_not_called()

    def test_retention_needs_real_expiry_retained_restore_and_scoped_cleanup_before_pass(self):
        for failed in (None,'restore','cleanup','interrupt'):
            with self.subTest(failed=failed),tempfile.TemporaryDirectory() as tmp:
                gate=self.gate(tmp)
                gate.state.update(backup={'manual':'fixed'},source_cluster_uid='source');gate.save()
                events=[]
                def records(**kwargs):
                    rows=[]
                    for index in range(3):
                        name='scheduled-'+str(index);snapshot={'destination':'s3://owned/'+name}
                        gate.state['retention']['records'][name]={'name':name,'created':str(index),'backup':snapshot}
                        if index:rows.append({'metadata':{'name':name,'creationTimestamp':str(index)},'status':{'state':'ready'}})
                    return rows
                def restore(**kwargs):
                    self.assertTrue(kwargs['retention']);events.append('restore')
                    self.assertIn('original_config',gate.state)  # Keep MainStorage until native cleanup.
                    if failed=='restore':raise ValueError('restore failed')
                    if failed=='interrupt':raise SystemExit(143)
                def recover():
                    events.append('cleanup')
                    if failed=='cleanup':raise ValueError('cleanup failed')
                output=StringIO()
                patches=[patch.object(gate,'validate_identity'),patch.object(gate.lab,'verify',return_value={'healthy':True}),
                    patch.object(gate.scenario,'health'),patch.object(gate,'request_for_cleanup'),
                    patch.object(gate,'verify_objects',return_value=gate.state['backup']),
                    patch.object(gate.lab,'get',return_value={'items':[]}),patch.object(gate,'source_identity',return_value={'uid':'source'}),
                    patch.object(gate,'retention_fault'),patch.object(gate.scenario,'client_create'),
                    patch.object(gate.scenario,'js'),patch.object(gate,'configure',side_effect=lambda **kw:gate.state.update(original_config={'mongodb_backup_enabled':False})),
                    patch.object(gate,'retention_records',side_effect=records),patch.object(gate,'retention_expired',return_value=True),
                    patch.object(gate,'retention_pause'),patch.object(gate,'restore_configuration',side_effect=lambda:gate.state.pop('original_config',None)),patch.object(gate,'cleanup_client'),
                    patch.object(gate,'restore_independent',side_effect=restore),patch.object(gate,'retention_recover',side_effect=recover),
                    patch.object(gate,'diagnostics')]
                with ExitStack() as stack,redirect_stdout(output):
                    for item in patches:stack.enter_context(item)
                    if failed=='interrupt':
                        with self.assertRaises(SystemExit):gate.retention()
                    elif failed:
                        with self.assertRaisesRegex(ValueError,failed+' failed'):gate.retention()
                    else:gate.retention()
                self.assertEqual(events,['restore','cleanup'])
                self.assertEqual('MONGODB_RETENTION_PASS' in output.getvalue().splitlines(),failed is None)

    def test_wrong_key_requires_physical_backup_before_any_mutation(self):
        with tempfile.TemporaryDirectory() as tmp:
            gate = self.gate(tmp)
            with patch.object(gate, 'target_capacity') as capacity:
                with self.assertRaisesRegex(ValueError, 'physical backup'):
                    gate.prepare_target(wrong_key=True)
                capacity.assert_not_called()

    def test_encryption_failure_requires_native_key_error_and_failed_named_restore(self):
        error = 'Decrypted data integrity check has failed. Probably the encryption key was wrong.'
        self.assertTrue(module.encryption_failure({'state': 'error', 'pbmName': 'restore'}, error))
        self.assertTrue(module.encryption_failure({'state': 'error', 'pbmName': 'restore'},
            'EncryptionKeyDB: WiredTiger metadata corruption detected or an invalid encryption key is used'))
        for status, logs in (({'state': 'ready', 'pbmName': 'restore'}, error),
                             ({'state': 'error'}, error),
                             ({'state': 'error', 'pbmName': 'restore'}, 'connection refused'),
                             ({'state': 'error', 'pbmName': 'restore'}, 'no space left on device')):
            self.assertFalse(module.encryption_failure(status, logs))

    def test_encryption_accepts_only_complete_native_openssl_keydb_branch(self):
        lines = ['Opening WiredTiger keydb at /data/db/keydb',
                 'WiredTiger error: libcrypto: error:1C800064:Provider routines::bad decrypt',
                 'WiredTiger error: setting return code to WT_PANIC']
        status = {'state': 'error', 'pbmName': 'restore'}
        self.assertTrue(module.encryption_failure(status, '\n'.join(lines)))
        for index in range(3):
            self.assertFalse(module.encryption_failure(status, '\n'.join(
                line for i, line in enumerate(lines) if i != index)))
        self.assertFalse(module.encryption_failure(status, 'TLS: bad decrypt\n' + lines[0] + '\n' + lines[2]))

    def test_encryption_observation_preserves_identity_hash_and_flags_without_raw_logs(self):
        with tempfile.TemporaryDirectory() as tmp:
            gate = self.gate(tmp)
            gate.state['target'] = {'name': 'target', 'cr_uid': 'target-uid',
                'wrong_key_rejection': {'rc': 1, 'output_sha256': 'fixed'}}
            pod = {'metadata': {'name': 'target-rs0-0', 'uid': 'pod-uid',
                'labels': {'app.kubernetes.io/instance': 'target'}}}
            native = 'PRIVATE\n/data/db/keydb\nlibcrypto: bad decrypt\nsetting return code to WT_PANIC'
            status = {'state': 'error', 'pbmName': 'restore'}
            with patch.object(gate.lab, 'get', return_value={'items': [pod]}), \
                    patch.object(module, 'target_resource_owned', return_value=True), \
                    patch.object(module.subprocess, 'run', return_value=SimpleNamespace(
                        returncode=0, stdout=native)) as run:
                self.assertTrue(gate.observe_encryption_failure({'metadata': {'uid': 'restore-uid'}, 'status': status}))
            observation = gate.state['encryption_observation']
            self.assertEqual(observation['restore_uid'], 'restore-uid')
            self.assertEqual(observation['rejection']['rc'], 1)
            self.assertEqual(observation['members'][0]['uid'], 'pod-uid')
            self.assertEqual(observation['members'][0]['files'][0]['sha256'], hashlib.sha256(native.encode()).hexdigest())
            self.assertNotIn('PRIVATE', json.dumps(observation))
            self.assertTrue(any('/data/db/pbm.restore.log' in call.args[0] for call in run.call_args_list))
            gate.state.pop('target');gate.save()
            self.assertEqual(json.loads(gate.path.read_text())['encryption_observation'], observation)

    def test_encryption_observation_refuses_foreign_or_unreadable_or_split_native_evidence(self):
        for owned, rc, split in ((False, 0, False), (True, 1, False), (True, 0, True)):
            with self.subTest(owned=owned, rc=rc, split=split), tempfile.TemporaryDirectory() as tmp:
                gate = self.gate(tmp)
                gate.state['target'] = {'name': 'target', 'cr_uid': 'target-uid'}
                pod = {'metadata': {'name': 'target-rs0-0', 'uid': 'pod-uid',
                    'labels': {'app.kubernetes.io/instance': 'target'}}}
                # Evidence from different files must not be combined into a native failure.
                outputs = [SimpleNamespace(returncode=rc, stdout=text) for text in (
                    '/data/db/keydb', 'libcrypto: bad decrypt', 'setting return code to WT_PANIC')]
                with patch.object(gate.lab, 'get', return_value={'items': [pod]}), \
                        patch.object(module, 'target_resource_owned', return_value=owned), \
                        patch.object(module.subprocess, 'run', side_effect=outputs) as run:
                    if not owned:
                        with self.assertRaisesRegex(ValueError, 'foreign target Pod'):
                            gate.observe_encryption_failure({'status': {'state': 'error', 'pbmName': 'restore'}})
                        run.assert_not_called()
                    else:
                        self.assertFalse(gate.observe_encryption_failure({'status': {'state': 'error', 'pbmName': 'restore'}}))

    def test_expected_rejection_never_streams_raw_product_output_or_accepts_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            gate = self.gate(tmp)
            gate.state['target'] = {'deployment': 'mongodb-restore-physical'}
            for rc in (0, 1):
                output = StringIO()
                with patch.object(module.subprocess, 'run', return_value=SimpleNamespace(
                        returncode=rc, stdout='PRIVATE expected FAIL', stderr='PRIVATE')), \
                        patch.object(gate, 'capture_target'), redirect_stdout(output):
                    if rc:
                        self.assertIn('PRIVATE', gate.expected_target_failure('wrong_key'))
                    else:
                        with self.assertRaisesRegex(ValueError, 'unexpectedly succeeded'):
                            gate.expected_target_failure('wrong_key')
                self.assertEqual(output.getvalue(), '')
                self.assertNotIn('PRIVATE', json.dumps(gate.state))
                self.assertEqual(gate.state['target']['wrong_key_rejection']['rc'], rc)

    def test_missing_key_stops_before_mutation_and_always_restores_product_configuration(self):
        for changed, rejection in ((False, '核验 MongoDB 资源所有权、凭据及故障域 MongoDB Kubernetes/client request failed'),
                                   (True, '核验 MongoDB 资源所有权、凭据及故障域 MongoDB Kubernetes/client request failed'),
                                   (False, 'network timeout')):
            with self.subTest(changed=changed, rejection=rejection), tempfile.TemporaryDirectory() as tmp:
                gate = self.gate(tmp);gate.base = Path(tmp)
                gate.state['target'] = {'name': 'mongodb-gate-physical-restore',
                    'deployment': 'mongodb-restore-physical', 'source_identity': {'uid': 'source'}}
                path = Path(tmp, 'clusters/mongodb-restore-physical/config.yml')
                path.parent.mkdir(parents=True);path.write_text('mongodb_encryption_key_secret: original\n')
                original = path.read_text()
                with patch.object(gate.lab, 'get', return_value={}), \
                        patch.object(gate, 'target_declaration', side_effect=[['before'], ['changed' if changed else 'before']]), \
                        patch.object(gate, 'source_identity', return_value={'uid': 'source'}), \
                        patch.object(gate, 'expected_target_failure', return_value=rejection), \
                        patch.object(gate, 'target_setup') as setup, patch.object(gate, 'target_health') as health:
                    if changed or rejection == 'network timeout':
                        with self.assertRaisesRegex(ValueError, 'did not stop'):
                            gate.missing_key()
                    else:gate.missing_key()
                self.assertEqual(path.read_text(), original)
                setup.assert_called_once();health.assert_called_once()

    def test_encryption_negative_control_failure_cannot_reach_positive_control(self):
        with tempfile.TemporaryDirectory() as tmp:
            gate = self.gate(tmp);gate.kind = 'physical'
            gate.state['backup'] = {'checksum': 'fixed'};gate.save()
            events = []
            with patch.object(gate, 'validate_identity'), patch.object(gate.lab, 'verify', return_value={'healthy': True}), \
                    patch.object(gate.scenario, 'health'), patch.object(gate, 'request_for_cleanup'), \
                    patch.object(gate, 'verify_objects', return_value={'checksum': 'fixed'}), \
                    patch.object(gate, 'prepare_target', side_effect=ValueError('first-error')), \
                    patch.object(gate, 'diagnostics', side_effect=lambda: events.append('diagnostics')), \
                    patch.object(gate, 'cleanup_target', side_effect=lambda: events.append('cleanup')), \
                    patch.object(gate, 'restore_independent') as restore:
                with self.assertRaisesRegex(ValueError, 'first-error'):gate.encryption()
                restore.assert_not_called()
            self.assertEqual(events, ['diagnostics', 'cleanup'])

    def test_encryption_requires_correct_key_restore_after_failed_target_cleanup(self):
        for positive_failure in (False, True):
            with self.subTest(positive_failure=positive_failure), tempfile.TemporaryDirectory() as tmp:
                gate = self.gate(tmp);gate.base = Path(tmp);gate.kind = 'physical'
                gate.state['backup'] = {'destination': 's3://owned/backup/name'};gate.save()
                path = Path(tmp, 'clusters/mongodb-restore-physical/config.yml')
                path.parent.mkdir(parents=True);path.write_text('{}')
                events = []
                def prepare(**kwargs):
                    self.assertIs(kwargs['wrong_key'], True)
                    gate.state['target'] = {'name': 'mongodb-gate-physical-restore',
                        'deployment': 'mongodb-restore-physical', 'cr_uid': 'target-uid', 'source_identity': {}}
                    events.append('wrong-target')
                def get(kind, *args, **kwargs):
                    if kind == 'psmdb-restore':return {'status': {'state': 'error', 'pbmName': 'restore'}}
                    if kind == 'pod':return {'items': [{'metadata': {'name': 'target-rs0-0',
                        'labels': {'app.kubernetes.io/instance': 'mongodb-gate-physical-restore'}}}]}
                    return {}
                def cleanup():
                    self.assertTrue(gate.state['target']['key_failure_proven'])
                    events.append('failed-target-clean')
                def positive():
                    events.append('correct-key-restore')
                    if positive_failure:raise ValueError('positive control failed')
                output = StringIO()
                with patch.object(gate, 'validate_identity'), patch.object(gate.lab, 'verify', return_value={'healthy': True}), \
                        patch.object(gate.scenario, 'health'), patch.object(gate, 'request_for_cleanup'), \
                        patch.object(gate, 'verify_objects', return_value=gate.state['backup']), \
                        patch.object(gate, 'prepare_target', side_effect=prepare), patch.object(gate, 'missing_key'), \
                        patch.object(gate.scenario.product, 'operation_object', return_value={}), \
                        patch.object(gate.scenario.product, 'check_operation_identity'), \
                        patch.object(gate, 'trace_restore', return_value=nullcontext()), \
                        patch.object(gate, 'expected_target_failure'), patch.object(gate.lab, 'get', side_effect=get), \
                        patch.object(gate, 'observe_encryption_failure', return_value=True), \
                        patch.object(gate, 'cleanup_target', side_effect=cleanup), \
                        patch.object(gate, 'restore_independent', side_effect=positive), redirect_stdout(output):
                    if positive_failure:
                        with self.assertRaisesRegex(ValueError, 'positive control failed'):gate.encryption()
                    else:gate.encryption()
                self.assertEqual(events, ['wrong-target', 'failed-target-clean', 'correct-key-restore'])
                self.assertEqual('MONGODB_ENCRYPTION_PASS' in output.getvalue().splitlines(), not positive_failure)

    def test_restore_capacity_uses_per_host_advertisement_and_maximum_precedence(self):
        with tempfile.TemporaryDirectory() as tmp:
            gate = self.gate(tmp)
            gate.config_path.write_text(yaml.safe_dump({'mongodb_storage_class': 'mongodb-longhorn', 'mongodb_pvc_size': '4Gi'}))
            rows = [{'storageClassName': 'mongodb-longhorn', 'nodeTopology': {'matchLabels': {
                'kubernetes.io/hostname': 'mongodb-node-' + suffix}}, 'capacity': '8Gi'} for suffix in ('212', '165', '238')]
            with patch.object(gate.lab, 'get', return_value={'items': rows}):
                gate.target_capacity()
                rows[0]['capacity'] = '1431096525'
                with self.assertRaisesRegex(ValueError, 'all three worker'):gate.target_capacity()
                rows[0]['maximumVolumeSize'] = '4Gi'
                gate.target_capacity()
                rows[0].update(capacity='20Gi', maximumVolumeSize='1Gi')
                with self.assertRaisesRegex(ValueError, 'all three worker'):gate.target_capacity()
                rows[0]['maximumVolumeSize'] = '0'
                with self.assertRaisesRegex(ValueError, 'all three worker'):gate.target_capacity()

    def test_restore_capacity_missing_or_duplicate_host_is_not_extra_capacity(self):
        with tempfile.TemporaryDirectory() as tmp:
            gate = self.gate(tmp)
            gate.config_path.write_text(yaml.safe_dump({'mongodb_storage_class': 'mongodb-longhorn', 'mongodb_pvc_size': '4Gi'}))
            row = {'storageClassName': 'mongodb-longhorn', 'nodeTopology': {'matchLabels': {
                'kubernetes.io/hostname': 'mongodb-node-212'}}, 'capacity': '100Gi'}
            for rows, reason in (([], 'all three worker'), ([row], 'all three worker'), ([row, row], 'duplicate host')):
                with patch.object(gate.lab, 'get', return_value={'items': rows}):
                    with self.assertRaisesRegex(ValueError, reason):gate.target_capacity()

    def test_csi_capacity_wait_keeps_threshold_and_stops_on_timeout_or_invalid_topology(self):
        with tempfile.TemporaryDirectory() as tmp:
            g=self.gate(tmp)
            g.config_path.write_text(yaml.safe_dump({'mongodb_storage_class':'mongodb-longhorn','mongodb_pvc_size':'7Gi'}))
            healthy=[{'storageClassName':'mongodb-longhorn','nodeTopology':{'matchLabels':{
                'kubernetes.io/hostname':'mongodb-node-'+host}},'capacity':'8Gi'} for host in ('212','165','238')]
            low=copy.deepcopy(healthy);low[0]['capacity']='1Gi'
            with patch.object(g.lab,'get',side_effect=[{'items':low},{'items':healthy}]), \
                    patch.object(module.time,'sleep') as sleep,patch.object(module.time,'monotonic',side_effect=[0,1]), \
                    redirect_stdout(StringIO()) as output:
                g.target_capacity(wait_timeout=180);sleep.assert_called_once_with(5)
                self.assertIn('1073741824',output.getvalue());self.assertIn('requested-bytes=7516192768',output.getvalue())
            for rows,reason in ((low,'all three worker'),([healthy[0],healthy[0]],'duplicate host')):
                with patch.object(g.lab,'get',return_value={'items':rows}),patch.object(module.time,'sleep') as sleep, \
                        patch.object(module.time,'monotonic',side_effect=[0,181]),redirect_stdout(StringIO()):
                    with self.assertRaisesRegex(ValueError,reason):g.target_capacity(wait_timeout=180)
                    sleep.assert_not_called()

    def test_capacity_failure_precedes_all_independent_target_mutation(self):
        with tempfile.TemporaryDirectory() as tmp:
            gate = self.gate(tmp);gate.base = Path(tmp)
            with patch.object(gate, 'target_capacity', side_effect=ValueError('no-capacity')), \
                    patch.object(gate, 'api_input') as api, patch.object(module.subprocess, 'run') as run:
                with self.assertRaisesRegex(ValueError, 'no-capacity'):gate.prepare_target()
                self.assertNotIn('target', gate.state)
                api.assert_not_called();run.assert_not_called()

    def test_restore_timeline_excludes_source_secrets_and_arbitrary_pod_material(self):
        target = 'mongodb-gate-physical-restore'
        def obj(kind, name, **fields):
            return {'kind': kind, 'metadata': {'name': name, 'namespace': 'mongodb-gate',
                'uid': name + '-uid', 'resourceVersion': '9'}, **fields}
        pod = obj('Pod', target + '-rs0-0', spec={'nodeName': 'worker', 'env': 'private-material'},
            status={'podIP': '10.0.0.1', 'phase': 'Running', 'message': 'private-material',
                    'containerStatuses': [{'name': 'mongod', 'ready': False, 'restartCount': 1}]})
        sts = obj('StatefulSet', target + '-rs0', spec={'replicas': 3})
        sts['metadata']['annotations'] = {'percona.com/restore-in-progress': 'true', 'other': 'private-material'}
        endpoints = obj('EndpointSlice', 'generated-name', endpoints=[{
            'addresses': ['10.0.0.1'], 'hostname': target + '-rs0-0', 'conditions': {'ready': True}}])
        endpoints['metadata']['labels'] = {'kubernetes.io/service-name': target + '-rs0'}
        wrong_ns = obj('Pod', target + '-foreign');wrong_ns['metadata']['namespace'] = 'foreign'
        rows = module.restore_timeline({'items': [pod, sts, endpoints, wrong_ns,
            obj('Secret', target + '-users', data={'password': 'private-material'}),
            obj('Pod', 'mongodb-gate-data-rs0-0')]}, target)
        self.assertEqual([row['kind'] for row in rows], ['Pod', 'StatefulSet', 'EndpointSlice'])
        self.assertEqual(rows[0]['containers'][0]['restartCount'], 1)
        self.assertEqual(rows[1]['restore_annotation'], 'true')
        self.assertEqual(rows[2]['endpoints'][0]['hostname'], target + '-rs0-0')
        self.assertNotIn('private-material', json.dumps(rows))

    def test_physical_restore_trace_is_private_read_only_and_stops_on_first_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            gate = self.gate(tmp);gate.kind = 'physical'
            gate.state['target'] = {'name': 'mongodb-gate-physical-restore'}
            gate.diagnostic_directory = Path(tmp)
            sampled = threading.Event()
            def capture(*args, **kwargs):
                self.assertEqual(kwargs['timeout'], 5)
                self.assertEqual(args[2], 'get')
                sampled.set()
                return '{"items":[]}'
            with patch.object(gate.lab, 'run', side_effect=capture):
                with self.assertRaisesRegex(ValueError, 'original-error'):
                    with gate.trace_restore():
                        self.assertTrue(sampled.wait(2))
                        raise ValueError('original-error')
            self.assertFalse(any(t.name == 'mongodb-restore-diagnostics' for t in threading.enumerate()))
            path, = Path(tmp).glob('physical-restore-timeline*.jsonl')
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(json.loads(path.read_text())['objects'], [])

    def test_physical_restore_negative_and_positive_traces_preserve_separate_timelines(self):
        with tempfile.TemporaryDirectory() as tmp:
            gate = self.gate(tmp);gate.kind = 'physical'
            gate.state['target'] = {'name': 'mongodb-gate-physical-restore'}
            gate.diagnostic_directory = Path(tmp)
            with patch.object(gate.lab, 'run', return_value='{"items":[]}'):
                for _ in range(2):
                    with gate.trace_restore():pass
            paths = list(Path(tmp).glob('physical-restore-timeline*.jsonl'))
            self.assertEqual(len(paths), 2)
            self.assertTrue(all(path.stat().st_mode & 0o777 == 0o600 for path in paths))

    def test_failed_target_deletion_resumes_only_its_exact_ordered_finalizer(self):
        name = 'mongodb-gate-physical-restore'
        ordered = 'percona.com/delete-psmdb-pods-in-order'
        with tempfile.TemporaryDirectory() as tmp:
            gate = self.gate(tmp)
            gate.state['target'] = {'name': name, 'cr_uid': 'target', 'source_identity': {'source': 'unchanged'}}
            current = {'metadata': {'name': name, 'namespace': 'mongodb-gate', 'uid': 'target',
                'resourceVersion': '12', 'deletionTimestamp': '2026-10-15T15:16:26Z',
                'finalizers': [ordered], 'labels': {'app.kubernetes.io/managed-by': 'kubeauto',
                                                 'kubeauto.io/component': 'mongodb'}}}
            with patch.object(gate, 'source_identity', return_value={'source': 'unchanged'}), \
                    patch.object(gate, 'api_input') as submit:
                gate.resume_target_deletion('psmdb', current)
                patch_data = submit.call_args.kwargs['obj']
                self.assertEqual(patch_data[:3], [
                    {'op': 'test', 'path': '/metadata/uid', 'value': 'target'},
                    {'op': 'test', 'path': '/metadata/resourceVersion', 'value': '12'},
                    {'op': 'test', 'path': '/metadata/finalizers', 'value': [ordered]}])
                self.assertEqual(patch_data[-1], {'op': 'remove', 'path': '/metadata/finalizers/0'})
                submit.reset_mock()
                for change in ({'uid': 'foreign'}, {'name': 'mongodb-gate-data'},
                               {'finalizers': [ordered, 'foreign/finalizer']}, {'labels': {}}):
                    altered = {'metadata': current['metadata'] | change}
                    with self.assertRaisesRegex(ValueError, 'ownership or finalizers'):
                        gate.resume_target_deletion('psmdb', altered)
                submit.assert_not_called()
                with patch.object(gate, 'source_identity', return_value={'source': 'changed'}):
                    with self.assertRaisesRegex(ValueError, 'ownership or finalizers'):
                        gate.resume_target_deletion('psmdb', current)
                submit.assert_not_called()
                current['metadata'].pop('deletionTimestamp')
                gate.resume_target_deletion('psmdb', current)
                submit.assert_not_called()

    def test_physical_restore_accepts_recreated_pdb_only_under_verified_controller_graph(self):
        name='mongodb-gate-physical-restore'
        labels={'app.kubernetes.io/instance':name,
                'app.kubernetes.io/managed-by':'percona-server-mongodb-operator'}
        def obj(kind,suffix,uid,parent):
            return {'kind':kind,'metadata':{'name':name+suffix,'namespace':'mongodb-gate',
                    'uid':uid,'labels':labels,'ownerReferences':[{'uid':parent}]}}
        sts=obj('StatefulSet','-rs0','new-sts','target-cr')
        pdb=obj('PodDisruptionBudget','-mongod-rs0','new-pdb','new-sts')
        pod=obj('Pod','-rs0-0','new-pod','new-sts')
        for parent in ('new-sts','foreign-sts'):
            pdb['metadata']['ownerReferences']=[{'uid':parent}]
            with tempfile.TemporaryDirectory() as tmp:
                gate=self.gate(tmp)
                gate.state['target']={'name':name,'cr_uid':'target-cr','request':{'kind':'restore'},
                    'resources':[{'kind':k,'name':o['metadata']['name'],'uid':'old-'+k}
                                 for k,o in [('statefulset',sts),('pdb',pdb),('pod',pod)]]}
                objects={'statefulset':[sts],'pdb':[pdb],'pod':[pod]}
                with patch.object(gate.lab,'get',side_effect=lambda kind,*args,**kwargs:None if args and args[0]==name else {'items':objects.get(kind,[])}):
                    if parent=='foreign-sts':
                        with self.assertRaisesRegex(ValueError,'resource was replaced'):gate.capture_target()
                    else:
                        gate.capture_target()
                        records={r['kind']:r for r in gate.state['target']['resources']}
                        self.assertEqual(records['pdb']['uid'],'new-pdb')
                        self.assertEqual(records['pod']['uid'],'new-pod')

    def test_pitr_uses_separate_journal_and_enables_oplog_before_baseline_request(self):
        self.assertNotEqual(module.BackupGate(ROOT,'logical',pitr=True).path,
                            module.BackupGate(ROOT,'logical').path)
        from tests.unit.test_mongodb_delivery import configuration
        with tempfile.TemporaryDirectory() as tmp:
            gate=self.gate(tmp);config=configuration();gate.config_path.write_text(yaml.safe_dump(config))
            gate.state['bucket']='bucket';folder=Path(tmp,'yml/mongodb');folder.mkdir(parents=True)
            folder.joinpath('input.json').write_text('{}')
            def product():
                current=yaml.safe_load(gate.config_path.read_text())
                self.assertIs(current['mongodb_pitr_enabled'],True)
                self.assertIs(current['mongodb_backup_schedule_enabled'], False)
                self.assertEqual(current['mongodb_pitr_oplog_span_min'],1)
                self.assertEqual(current['mongodb_requests'][0]['kind'],'backup')
                self.assertIn('original_config',json.loads(gate.path.read_text()))
            with patch.object(gate.lab,'get',return_value={'metadata':{'uid':'source'}}), \
                    patch.object(gate.scenario.product,'operation_object',return_value={}), \
                    patch.object(gate,'setup_product',side_effect=product):
                gate.configure(pitr=True)
            self.assertEqual(gate.state['original_config']['mongodb_pitr_oplog_span_min'],
                             config['mongodb_pitr_oplog_span_min'])

    def test_pitr_requires_latest_restorable_time_beyond_target_before_accepting_oplog(self):
        boundary={'target_utc':'2026-10-15 00:00:02','target_epoch':1792022402}
        output='MONGODB_PITR_BOUNDARY '+json.dumps(boundary)
        with tempfile.TemporaryDirectory() as tmp:
            gate=self.gate(tmp);gate.state.update(request={'name':'backup'},bucket='bucket')
            with patch.object(gate.scenario,'js',return_value=output), \
                    patch.object(gate.lab,'get',return_value={'status':{'latestRestorableTime':'2026-10-15T00:00:03Z'}}), \
                    patch.object(module.time,'monotonic',side_effect=[0,481]),patch.object(gate,'mc') as mc:
                with self.assertRaisesRegex(ValueError,'oplog coverage'):gate.pitr_window()
                mc.assert_not_called()
            self.assertNotIn('pitr',gate.state)

    def test_pitr_reads_nonzero_owned_oplog_and_checksums_before_persisting_coverage(self):
        boundary={'target_utc':'2026-10-15 00:00:02','target_epoch':1792022402}
        with tempfile.TemporaryDirectory() as tmp:
            gate=self.gate(tmp);gate.state.update(request={'name':'backup'},bucket='bucket')
            listing=json.dumps({'status':'success','type':'file','key':'backup/pbmPitr/rs0/chunk','size':64})
            with patch.object(gate.scenario,'js',return_value='MONGODB_PITR_BOUNDARY '+json.dumps(boundary)), \
                    patch.object(gate.lab,'get',return_value={'status':{'latestRestorableTime':'2026-10-15T00:00:04Z'}}), \
                    patch.object(gate,'mc',return_value=listing), \
                    patch.object(gate.lab,'run',return_value='a'*64+'  -') as checksum:
                gate.pitr_window()
            self.assertEqual(gate.state['pitr']['oplog_objects'][0]['sha256'],'a'*64)
            self.assertIn('pipefail',checksum.call_args.args)
            self.assertEqual(json.loads(gate.path.read_text())['pitr']['target_utc'],boundary['target_utc'])

    def test_failed_target_cleanup_repairs_source_rendering_through_product_before_health(self):
        with tempfile.TemporaryDirectory() as tmp:
            gate = self.gate(tmp);gate.base = Path(tmp)
            gate.state['target'] = {'name':'mongodb-gate-logical-restore','deployment':'mongodb-restore-logical',
                                    'source_identity':{},'resources':[]}
            gate.config_path.write_text(yaml.safe_dump({'mongodb_name':'mongodb-gate-data','mongodb_namespace':'mongodb-gate'}))
            rendered = Path(tmp,'yml/mongodb/input.json');rendered.parent.mkdir(parents=True)
            rendered.write_text(json.dumps({'mongodb_name':'mongodb-gate-logical-restore'}))
            order=[]
            with patch.object(gate,'validate_identity'), patch.object(gate,'capture_target'), \
                    patch.object(gate,'source_identity',return_value={}), \
                    patch.object(gate,'setup_product',side_effect=lambda:order.append('source-product')), \
                    patch.object(gate.scenario,'delete_client'), \
                    patch.object(gate.scenario,'health',side_effect=lambda:order.append('source-health')):
                gate.cleanup_target()
            self.assertEqual(order,['source-product','source-health'])
            self.assertNotIn('target',gate.state)

    def test_target_health_requires_independent_claims_and_volumes(self):
        with tempfile.TemporaryDirectory() as tmp:
            gate = self.gate(tmp);gate.base = Path(tmp)
            folder = Path(tmp, 'clusters/mongodb-restore-logical/yml/mongodb');folder.mkdir(parents=True)
            folder.joinpath('input.json').write_text('{}')
            claims = [{'kind':'pvc','uid':'target-'+str(i),'volume_name':'target-pv-'+str(i)} for i in range(3)]
            gate.state['target'] = {'deployment':'mongodb-restore-logical','resources':claims,
                'source_identity':{'claims':{'source':{'uid':'source-uid','volume':'source-pv'}}}}
            with patch.object(gate.scenario.product,'health'):
                gate.target_health()
                claims[0]['volume_name'] = 'source-pv'
                with self.assertRaisesRegex(ValueError,'separate from source'):gate.target_health()
                claims[0]['volume_name'] = 'target-pv-0'
                claims[0]['uid'] = 'source-uid'
                with self.assertRaisesRegex(ValueError,'separate from source'):gate.target_health()

    def test_sharded_target_health_requires_nine_unique_claims_separate_from_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            gate=self.gate(tmp);gate.base=Path(tmp)
            folder=Path(tmp,'clusters/mongodb-restore-logical/yml/mongodb');folder.mkdir(parents=True)
            folder.joinpath('input.json').write_text('{}')
            claims=[{'kind':'pvc','uid':'claim-'+str(i),'volume_name':'pv-'+str(i)} for i in range(9)]
            gate.state['target']={'deployment':'mongodb-restore-logical','sharded':True,'resources':claims,
                'source_identity':{'claims':{'original':{'uid':'source','volume':'source-pv'}}}}
            with patch.object(gate.scenario.product,'health'):
                gate.target_health()
                claims.pop()
                with self.assertRaises(ValueError):gate.target_health()

    def test_sharded_backup_metadata_requires_cfg_and_both_shards_exactly(self):
        meta={'type':'logical','status':'done','name':'backup',
            'replsets':[{'name':rs,'status':'done'} for rs in ('cfg','rs0','rs1')]}
        original=module.backup_metadata(meta,'logical')
        self.assertEqual(module.backup_metadata(meta,'logical',expected_replsets=['cfg','rs0','rs1']),original)
        for names in (['rs0','rs1'],['cfg','rs0','rs1','rs2'],['cfg','rs0','rs0']):
            with self.subTest(names=names),self.assertRaises(ValueError):
                module.backup_metadata(meta|{'replsets':[{'name':n,'status':'done'} for n in names]},
                                      'logical',expected_replsets=['cfg','rs0','rs1'])

    def test_independent_target_refuses_source_and_foreign_resource_ownership(self):
        name, run_id = 'mongodb-gate-logical-restore', '0123456789abcdef'
        def obj(n, labels, namespace='mongodb-gate'):
            return {'metadata': {'name': n, 'namespace': namespace, 'labels': labels}}
        operator = {'app.kubernetes.io/instance': name,
                    'app.kubernetes.io/managed-by': 'percona-server-mongodb-operator'}
        product = {'kubeauto.io/component': 'mongodb', 'app.kubernetes.io/managed-by': 'kubeauto'}
        private = module.LABELS | {'kubeauto.io/test-run': run_id}
        for resource in (obj(name, product), obj(name+'-access', product),
                         obj(name+'-rs0-0', operator), obj('mongod-data-'+name+'-rs0-0', operator),
                         obj('internal-'+name+'-users', operator), obj(name+'-tls', private)):
            self.assertTrue(module.target_resource_owned(resource, name, run_id))
        for resource in (obj('mongodb-gate-data-rs0-0', operator), obj(name+'-rs0-0', {}),
                         obj(name+'-tls', module.LABELS), obj(name, product, 'foreign')):
            self.assertFalse(module.target_resource_owned(resource, name, run_id))

    def test_independent_target_configuration_has_no_application_user_and_new_tls(self):
        self.check_target_credentials('logical', False)

    def test_wrong_key_target_is_valid_independent_and_preserves_source_credentials(self):
        self.check_target_credentials('physical', True)

    def test_disaster_target_generates_new_credentials_or_reuses_complete_escrow(self):
        self.check_target_credentials('physical',False,fresh=True)
        self.check_target_credentials('physical',False,escrowed=True)

    def test_sharded_restore_target_uses_nine_small_independent_volumes_and_router(self):
        self.check_target_credentials('logical',False,fresh=True,sharded=True)

    def test_target_credential_modes_refuse_ambiguity_before_mutation(self):
        with tempfile.TemporaryDirectory() as tmp:
            gate=self.gate(tmp);gate.kind='physical'
            with patch.object(gate,'target_capacity') as capacity,patch.object(gate,'api_input') as api:
                with self.assertRaisesRegex(ValueError,'mutually exclusive'):
                    gate.prepare_target(fresh_credentials=True,escrow={})
                capacity.assert_not_called();api.assert_not_called()

    def check_target_credentials(self, kind, wrong_key, *, fresh=False, escrowed=False, sharded=False):
        with tempfile.TemporaryDirectory() as tmp:
            gate = self.gate(tmp);gate.base = Path(tmp)
            gate.kind = kind
            deployment = 'mongodb-restore-' + kind
            gate.state['bucket'] = gate.state['name']
            source_config={'mongodb_namespace': 'mongodb-gate', 'mongodb_pvc_size': '10Gi',
                           'mongodb_volume_expansion_enabled': True}
            gate.config_path.write_text(yaml.safe_dump(source_config))
            gate.lab.directory.joinpath('hosts').write_text('owned-inventory')
            gate.lab.directory.joinpath('kubectl.kubeconfig').write_text('owned-context')
            source = {o['metadata']['name']: {'data': {
                k: base64.b64encode(v.encode()).decode() for k,v in o['stringData'].items()}}
                for o in gate.scenario.fixture.credentials()['items']}
            original = copy.deepcopy(source)
            escrow={o['metadata']['name'].removeprefix('mongodb-gate-'+kind+'-restore-'):{
                k:base64.b64encode(v.encode()).decode() for k,v in o['stringData'].items()}
                for o in gate.scenario.fixture.credentials('mongodb-gate-'+kind+'-restore')['items']} if escrowed else None
            def get(kind, name='', ns=''):
                if not name:return {'items': []}
                return source[name]
            def new(*args, **kwargs):
                Path(tmp, 'clusters', deployment).mkdir(parents=True)
                return SimpleNamespace(returncode=0)
            created = []
            def create(*args, obj):
                created.append(obj)
                return json.dumps({'metadata': {'uid': obj['metadata']['name'] + '-uid'}})
            with patch.object(gate.lab, 'get', side_effect=get), patch.object(gate, 'source_identity', return_value={}), \
                    patch.object(module.subprocess, 'run', side_effect=new), patch.object(gate, 'api_input', side_effect=create), \
                    patch.object(gate, 'target_setup'), patch.object(gate, 'target_health'), patch.object(gate, 'target_capacity') as capacity, \
                    patch.object(gate.scenario, 'client_create') as client, patch.object(gate.scenario, 'js') as js, \
                    patch.object(gate.scenario, 'delete_client'):
                gate.prepare_target(wrong_key=wrong_key,fresh_credentials=fresh,escrow=escrow,
                    backup_prefix=('sharding/' if sharded else 'disaster/')+gate.state['run_id'] if fresh or escrowed else 'backup',
                    sharded=sharded)
            target = gate.state['target']['name']
            config_path = Path(tmp, 'clusters', deployment, 'config.yml')
            config = yaml.safe_load(config_path.read_text())
            self.assertEqual(config['mongodb_users'], [])
            self.assertEqual(config['mongodb_name'], target)
            self.assertEqual(config['mongodb_tls_secret'], target + '-tls')
            self.assertIs(config['mongodb_backup_schedule_enabled'], False)
            self.assertIs(config['mongodb_backup_enabled'], True)
            self.assertEqual(yaml.safe_load(gate.config_path.read_text()),source_config)
            if kind == 'logical':
                capacity.assert_called_once_with('2Gi')
                self.assertEqual(config['mongodb_pvc_size'],'2Gi')
                self.assertIs(config['mongodb_volume_expansion_enabled'],False)
            else:
                capacity.assert_called_once_with()
                self.assertEqual(config['mongodb_pvc_size'],'10Gi')
                self.assertIs(config['mongodb_volume_expansion_enabled'],True)
            inventory = config_path.with_name('hosts').read_text()
            self.assertIn('cluster_dir="{{ base_dir }}/clusters/' + deployment + '"', inventory)
            self.assertNotIn('clusters/mongodb-gate"', inventory)
            if sharded:
                client.assert_called_once_with(target,sharded=True)
                self.assertEqual(config['mongodb_pvc_size'],'2Gi')
                self.assertEqual(config['mongodb_replsets'],[{'name':'rs0','size':3},{'name':'rs1','size':3}])
                self.assertIs(config['mongodb_sharding_enabled'],True)
                self.assertIs(config['mongodb_volume_expansion_enabled'],False)
                self.assertEqual(config['mongodb_config_size'],3)
                self.assertEqual(config['mongodb_mongos_size'],3)
                self.assertIs(gate.state['target']['sharded'],True)
            else:client.assert_called_once_with(target)
            self.assertTrue(any(c.kwargs.get('authenticate') is False for c in js.call_args_list))
            users = next(o for o in created if o['metadata']['name'] == target+'-users')
            if fresh or escrowed:self.assertNotEqual(users['data'],source['mongodb-gate-data-users']['data'])
            else:self.assertEqual(users['data'],source['mongodb-gate-data-users']['data'])
            if escrowed:
                for obj in created:self.assertEqual(obj['data'],escrow[obj['metadata']['name'].removeprefix(target+'-')])
            key = next(o for o in created if o['metadata']['name'] == target+'-key')['data']['encryption-key']
            self.assertEqual(len(base64.b64decode(base64.b64decode(key))), 32)
            self.assertEqual(key == source['mongodb-gate-data-key']['data']['encryption-key'], not (wrong_key or fresh or escrowed))
            self.assertEqual(gate.state['target']['key_sha256'], hashlib.sha256(base64.b64decode(key)).hexdigest())
            self.assertEqual(source, original)
            self.assertEqual(config_path.stat().st_mode & 0o777, 0o600)
            self.assertTrue(Path(tmp, 'mongodb-fault-target-' + kind + '.json').exists())

    def test_independent_target_rebound_pv_blocks_all_deletion(self):
        with tempfile.TemporaryDirectory() as tmp:
            gate = self.gate(tmp);gate.base = Path(tmp)
            name = 'mongodb-gate-logical-restore';claim = 'mongod-data-' + name + '-rs0-0'
            gate.state['target'] = {'name': name, 'deployment': 'mongodb-restore-logical', 'source_identity': {},
                'resources': [{'kind': 'pvc', 'name': claim, 'uid': 'owned-claim', 'volume_name': 'owned-pv'}]}
            obj = {'metadata': {'name': claim, 'namespace': 'mongodb-gate', 'uid': 'owned-claim', 'labels': {
                'app.kubernetes.io/instance': name, 'app.kubernetes.io/managed-by': 'percona-server-mongodb-operator'}}}
            volume = {'spec': {'storageClassName': 'mongodb-longhorn', 'claimRef': {
                'namespace': 'mongodb-gate', 'name': claim, 'uid': 'foreign-claim'}}}
            with patch.object(gate, 'validate_identity'), patch.object(gate, 'capture_target'), \
                    patch.object(gate.lab, 'get', side_effect=[obj, volume]), \
                    patch.object(gate, 'api_input') as delete, patch.object(gate.scenario, 'delete_client') as client:
                with self.assertRaisesRegex(ValueError, 'rebound volume'):gate.cleanup_target()
                delete.assert_not_called();client.assert_not_called()

    def test_independent_restore_failure_and_interrupt_preserve_first_error_and_clean_target(self):
        for error in (ValueError('first-target-error'), SystemExit(143)):
            with tempfile.TemporaryDirectory() as tmp:
                gate = self.gate(tmp);gate.state['backup'] = {'verified': True};gate.save()
                with patch.object(gate, 'validate_identity'), patch.object(gate.scenario, 'health'), \
                        patch.object(gate, 'request_for_cleanup'), patch.object(gate, 'verify_objects', return_value={'verified': True}), \
                        patch.object(gate, 'prepare_target', side_effect=error), patch.object(gate, 'diagnostics'), \
                        patch.object(gate, 'cleanup_target', side_effect=ValueError('secondary-cleanup-error')) as cleanup:
                    with self.assertRaises(type(error)) as caught:gate.restore_independent()
                    self.assertEqual(str(caught.exception), str(error));cleanup.assert_called_once()

    def test_mc_uses_pinned_cli_config_directory_for_ca_loading(self):
        with tempfile.TemporaryDirectory() as tmp:
            gate = self.gate(tmp)
            with patch.object(module.subprocess, 'run', return_value=SimpleNamespace(
                    returncode=0, stdout='ready', stderr='')) as run:
                self.assertEqual(gate.mc('ready', 'gate'), 'ready')
            command = run.call_args.args[0]
            self.assertEqual(command[command.index('mc'):],
                             ['mc', '--config-dir', '/tmp/mc', 'ready', 'gate'])

    def test_storage_failure_keeps_redacted_output_and_exact_action_before_cleanup(self):
        with tempfile.TemporaryDirectory() as tmp:
            gate = self.gate(tmp)
            gate.diagnostic_directory = Path(tmp)
            private = 'private-storage-value'
            encoded = base64.b64encode(private.encode()).decode()
            result = SimpleNamespace(returncode=1, stdout='https://access:' + private + '@storage\n' + encoded, stderr='TLS error')
            with patch.object(module.subprocess, 'run', return_value=result), \
                    patch.object(gate.lab, 'get', return_value={'data': {'value': encoded}}):
                with self.assertRaisesRegex(ValueError, 'action=ready rc=1') as error:
                    gate.mc('ready', 'gate')
            saved = Path(tmp, 'object-storage-client.txt')
            self.assertNotIn(private, saved.read_text());self.assertNotIn(encoded, saved.read_text())
            self.assertNotIn(private, str(error.exception))
            self.assertEqual(saved.stat().st_mode & 0o777, 0o600)

    def test_storage_output_is_withheld_when_redaction_inputs_cannot_be_obtained(self):
        with tempfile.TemporaryDirectory() as tmp:
            gate = self.gate(tmp);gate.diagnostic_directory = Path(tmp)
            with patch.object(module.subprocess, 'run', return_value=SimpleNamespace(
                    returncode=1, stdout='private-storage-value', stderr='')), \
                    patch.object(gate.lab, 'get', return_value=None):
                with self.assertRaises(ValueError): gate.mc('mb', 'gate/bucket')
            self.assertNotIn('private-storage-value', Path(tmp, 'object-storage-client.txt').read_text())

    def test_first_failure_diagnostics_precede_fixture_cleanup_and_preserve_cause(self):
        with tempfile.TemporaryDirectory() as tmp:
            gate = self.gate(tmp);order = []
            def diagnostic():
                order.append('diagnostic');raise ValueError('secondary-diagnostic-error')
            with patch.object(gate, 'prepare', side_effect=ValueError('first-storage-error')), \
                    patch.object(gate, 'diagnostics', side_effect=diagnostic), \
                    patch.object(gate, 'restore_configuration', side_effect=lambda: order.append('restore-config')), \
                    patch.object(gate, 'cleanup_client', side_effect=lambda: order.append('clean-client')), \
                    patch.object(gate, 'cleanup', side_effect=lambda: order.append('clean-fixture')):
                with self.assertRaisesRegex(ValueError, 'first-storage-error'): gate.execute()
            self.assertEqual(order, ['diagnostic', 'restore-config', 'clean-client', 'clean-fixture'])

    def gate(self, directory):
        gate = module.BackupGate(ROOT, 'logical')
        gate.lab.directory = Path(directory)
        gate.path = Path(directory) / 'mongodb-backup-logical.json'
        gate.config_path = Path(directory) / 'config.yml'
        gate.state = {'run_id': '0123456789abcdef', 'kind': 'logical', 'cluster_uid': 'owned-core',
                      'name': 'mongodb-backup-0123456789abcdef', 'resources': [], 'backup': None,
                      'source_prefix': 'mongodb_gate_0123456789abcdef'}
        return gate

    def test_fixture_tls_ca_hostname_secrets_and_pinned_images(self):
        objects = module.backup_objects('0123456789abcdef', 'mongodb-gate')
        credentials, tls, claim, service, server, client = objects
        ca = x509.load_pem_x509_certificate(tls['stringData']['ca.crt'].encode())
        cert = x509.load_pem_x509_certificate(tls['stringData']['public.crt'].encode())
        ca.public_key().verify(cert.signature, cert.tbs_certificate_bytes, padding.PKCS1v15(), cert.signature_hash_algorithm)
        self.assertEqual(cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value.get_values_for_type(x509.DNSName),
                         [service['metadata']['name'] + '.mongodb-gate.svc.cluster.local'])
        self.assertEqual(claim['spec']['storageClassName'], 'mongodb-longhorn')
        self.assertEqual(claim['spec']['resources']['requests']['storage'], '8Gi')
        self.assertEqual(service['spec']['selector'], server['metadata']['labels'])
        for pod, suffix in ((server, 'minio:RELEASE.2025-04-08T15-41-24Z'),
                            (client, 'minio-mc:RELEASE.2025-04-08T15-39-49Z')):
            self.assertTrue(pod['spec']['containers'][0]['image'].endswith(suffix))
            self.assertFalse(pod['spec']['automountServiceAccountToken'])
            self.assertNotIn(credentials['stringData']['AWS_SECRET_ACCESS_KEY'], json.dumps(pod))
        self.assertEqual(client['spec']['volumes'][0]['secret']['items'], [{'key': 'ca.crt', 'path': 'CAs/gate.crt'}])
        self.assertEqual(client['spec']['containers'][0]['volumeMounts'],
                         [{'name': 'ca', 'mountPath': '/tmp/mc/certs', 'readOnly': True}])
        self.assertNotIn('--certs-dir', json.dumps(client))
        self.assertNotIn('--insecure', json.dumps(client))
        with self.assertRaises(ValueError):
            module.backup_objects('0123456789abcdef', 'redis-gate')

    def test_mc_records_support_indented_json_but_reject_partial_or_failed_messages(self):
        rows = [{'status': 'success', 'type': 'file', 'key': 'backup/file', 'size': 12}, {'status': 'success', 'type': 'folder'}]
        for indent in (None, 2):
            self.assertEqual(module.json_messages('\n'.join(json.dumps(row, indent=indent) for row in rows)), rows)
        for invalid in ('[]', '{"status":"error","error":{"message":"secret"}}', json.dumps(rows[0]) + '\n{'):
            with self.assertRaises(ValueError) as error:
                module.json_messages(invalid)
            self.assertNotIn('secret', str(error.exception))

    def test_metadata_requires_every_replica_done_and_does_not_persist_credentials(self):
        meta = {'type': 'logical', 'name': 'backup', 'status': 'done', 'replsets': [{'status': 'done'}],
                'pbm_version': '2.15.0', 'store': {'secretAccessKey': 'private-only'}}
        self.assertNotIn('private-only', json.dumps(module.backup_metadata(meta, 'logical')))
        for field, value in [('status', 'running'), ('replsets', [{'status': 'error'}]), ('replsets', []), ('type', 'physical')]:
            with self.assertRaises(ValueError):
                module.backup_metadata(meta | {field: value}, 'logical')

    def test_physical_metadata_requires_exact_server_version_and_single_expected_replica_set(self):
        meta={'type':'physical','name':'backup','status':'done','mongodb_version':'8.0.32-14',
              'replsets':[{'name':'rs0','status':'done'}],'store':{'secretAccessKey':'private-only'}}
        result=module.backup_metadata(meta,'physical')
        self.assertEqual(result['replsets'],['rs0'])
        self.assertNotIn('private-only',json.dumps(result))
        for change in ({'mongodb_version':'8.0.26-11'}, {'replsets':[{'name':'foreign','status':'done'}]},
                       {'replsets':[{'name':'rs0','status':'done'}]*2}):
            with self.assertRaisesRegex(ValueError,'version or replica-set topology'):
                module.backup_metadata(meta|change,'physical')

    def test_cleanup_validates_whole_journal_before_deleting_anything(self):
        with tempfile.TemporaryDirectory() as tmp:
            gate = self.gate(tmp)
            # Reverse iteration sees the owned Pod before the foreign tail.
            gate.state['resources'] = [{'kind': 'secret', 'name': 'foreign', 'uid': 'foreign'},
                                       {'kind': 'pod', 'name': gate.state['name'], 'uid': 'owned'}]
            gate.save()
            obj = {'metadata': {'uid': 'owned', 'labels': module.LABELS | {'kubeauto.io/test-run': gate.state['run_id']}}}
            with patch.object(gate.lab, 'verify', return_value={'cluster_uid': 'owned-core'}), \
                    patch.object(gate.lab, 'get', return_value=obj), patch.object(gate, 'api_input') as submit:
                with self.assertRaisesRegex(ValueError, 'foreign resource'):
                    gate.cleanup()
                submit.assert_not_called()
            self.assertTrue(gate.path.exists())

    def test_cleanup_response_lost_reclaims_only_run_owned_resource_by_uid(self):
        with tempfile.TemporaryDirectory() as tmp:
            gate = self.gate(tmp)
            gate.state['resources'] = [{'kind': 'secret', 'name': gate.state['name'] + '-credentials', 'uid': None}]
            gate.save()
            obj = {'metadata': {'uid': 'created-response-lost', 'labels': module.LABELS | {'kubeauto.io/test-run': gate.state['run_id']}}}
            with patch.object(gate.lab, 'verify', return_value={'cluster_uid': 'owned-core'}), \
                    patch.object(gate.lab, 'get', return_value=obj), patch.object(gate.lab, 'run'), \
                    patch.object(gate, 'api_input') as submit:
                gate.cleanup()
                self.assertEqual(submit.call_args.kwargs['obj']['preconditions'], {'uid': 'created-response-lost'})
            self.assertFalse(gate.path.exists())

    def test_cleanup_replacement_or_core_change_preserves_journal(self):
        for core, labels, uid in [('foreign-core', module.LABELS, 'original'),
                                  ('owned-core', {}, 'original'), ('owned-core', module.LABELS, 'replacement')]:
            with tempfile.TemporaryDirectory() as tmp:
                gate = self.gate(tmp)
                gate.state['resources'] = [{'kind': 'pod', 'name': gate.state['name'], 'uid': 'original'}]
                gate.save()
                obj = {'metadata': {'uid': uid, 'labels': labels | {'kubeauto.io/test-run': gate.state['run_id']}}}
                with patch.object(gate.lab, 'verify', return_value={'cluster_uid': core}), \
                        patch.object(gate.lab, 'get', return_value=obj), patch.object(gate, 'api_input') as submit:
                    with self.assertRaises(ValueError): gate.cleanup()
                    submit.assert_not_called()
                self.assertTrue(gate.path.exists())

    def test_configuration_recovery_refuses_concurrent_edits(self):
        with tempfile.TemporaryDirectory() as tmp:
            gate = self.gate(tmp)
            gate.state.update(original_config={'mongodb_backup_enabled': False},
                              applied_config={'mongodb_backup_enabled': True}, source_cluster_uid='source')
            gate.config_path.write_text(yaml.safe_dump({'mongodb_backup_enabled': 'external-edit'}))
            with patch.object(gate.lab, 'verify', return_value={'cluster_uid': 'owned-core'}), \
                    patch.object(gate.lab, 'get', return_value={'metadata': {'uid': 'source'}}), \
                    patch.object(gate, 'setup_product') as setup, patch.object(gate, 'remove_request') as remove:
                with self.assertRaisesRegex(ValueError, 'concurrent edits'): gate.restore_configuration()
                setup.assert_not_called();remove.assert_not_called()

    def test_configuration_recovery_removes_a_previously_absent_schedule_switch(self):
        for already_restored in (False, True):
            with self.subTest(already_restored=already_restored), tempfile.TemporaryDirectory() as tmp:
                gate = self.gate(tmp)
                gate.state.update(original_config={'mongodb_backup_schedule_enabled': None},
                    original_absent=['mongodb_backup_schedule_enabled'],
                    applied_config={'mongodb_backup_schedule_enabled': False}, source_cluster_uid='source')
                config = {'unrelated': 'preserve'}
                if not already_restored:config['mongodb_backup_schedule_enabled'] = False
                gate.config_path.write_text(yaml.safe_dump(config))
                with patch.object(gate.lab, 'verify', return_value={'cluster_uid': 'owned-core'}), \
                        patch.object(gate.lab, 'get', return_value={'metadata': {'uid': 'source'}}), \
                        patch.object(gate, 'setup_product'), patch.object(gate, 'remove_request'), \
                        patch.object(gate.scenario, 'health'):
                    gate.restore_configuration()
                self.assertEqual(yaml.safe_load(gate.config_path.read_text()), {'unrelated': 'preserve'})
                self.assertNotIn('original_absent', json.loads(gate.path.read_text()))

    def test_configuration_recovery_is_reentrant_and_uses_product_before_acceptance(self):
        with tempfile.TemporaryDirectory() as tmp:
            gate = self.gate(tmp)
            gate.state.update(original_config={'mongodb_backup_enabled': False},
                              applied_config={'mongodb_backup_enabled': True}, source_cluster_uid='source')
            gate.config_path.write_text(yaml.safe_dump({'mongodb_backup_enabled': False, 'unrelated': 'preserve'}))
            with patch.object(gate.lab, 'verify', return_value={'cluster_uid': 'owned-core'}), \
                    patch.object(gate.lab, 'get', return_value={'metadata': {'uid': 'source'}}), \
                    patch.object(gate, 'setup_product') as setup, patch.object(gate, 'remove_request'), \
                    patch.object(gate.scenario, 'health') as health:
                gate.restore_configuration()
                setup.assert_called_once();health.assert_called_once()
                self.assertEqual(yaml.safe_load(gate.config_path.read_text()), {'mongodb_backup_enabled': False, 'unrelated': 'preserve'})
                self.assertNotIn('original_config', json.loads(gate.path.read_text()))

    def test_sigterm_or_backup_failure_runs_recovery_and_keeps_first_error(self):
        for error in (ValueError('first-backup-failure'), SystemExit(143)):
            with tempfile.TemporaryDirectory() as tmp:
                gate = self.gate(tmp)
                with patch.object(gate, 'prepare', side_effect=error), patch.object(gate, 'diagnostics'), \
                        patch.object(gate, 'restore_configuration') as restore, \
                        patch.object(gate.scenario, 'cleanup') as client_clean, patch.object(gate, 'cleanup') as clean:
                    with self.assertRaises(type(error)): gate.execute()
                    restore.assert_called_once();client_clean.assert_called_once();clean.assert_called_once()

    def test_ready_backup_requires_readable_data_for_the_exact_pbm_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            gate = self.gate(tmp)
            gate.state.update(bucket=gate.state['name'], request={'name': 'owned-request'})
            pbm_name = '2026-10-15T12:00:00Z'
            backup = {'metadata': {'uid': 'backup-uid'}, 'status': {'state': 'ready', 'completed': 'now',
                'pbmName': pbm_name, 'destination': 's3://' + gate.state['bucket'] + '/backup/' + pbm_name}}
            meta = {'type': 'logical', 'name': pbm_name, 'status': 'done', 'fcv': '8.0',
                    'pbm_version': '2.15.0', 'replsets': [{'name': 'rs0', 'status': 'done'}],
                    'store': {'secretAccessKey': 'must-never-persist'}}
            rows = [{'status': 'success', 'type': 'file', 'key': 'backup/' + pbm_name + '.pbm.json', 'size': 456}]
            def mc(*args):
                return '\n'.join(json.dumps(r, indent=2) for r in rows) if args[0] == 'ls' else json.dumps(meta)
            with patch.object(gate.lab, 'get', return_value=backup), patch.object(gate, 'mc', side_effect=mc), \
                    patch.object(gate.lab, 'run', return_value='a' * 64 + '  -') as checksum:
                with self.assertRaisesRegex(ValueError, 'metadata and data'):
                    gate.verify_objects()
                checksum.assert_not_called()
                rows.append({'status': 'success', 'type': 'file', 'key': 'backup/' + pbm_name + '/rs0/dump.gz', 'size': 100})
                gate.verify_objects()
                self.assertEqual(len(gate.state['backup']['objects']), 2)
                self.assertNotIn('must-never-persist', gate.path.read_text())
                self.assertEqual(checksum.call_count, 2)
                for call in checksum.call_args_list:
                    self.assertIn('mc --config-dir /tmp/mc cat "$1" | sha256sum', call.args)
                    self.assertIn('pipefail', call.args)

    def test_cleanup_recovers_persisted_client_identity_and_workload(self):
        with tempfile.TemporaryDirectory() as tmp:
            gate = self.gate(tmp)
            gate.state.update(source_prefix='mongodb_gate_0123456789abcdef', client_attempted=True,
                              client_uid='old-client-uid', workload_started=True)
            with patch.object(gate.scenario, 'cleanup') as cleanup, patch.object(gate.lab, 'get', return_value={'metadata': {}}):
                gate.cleanup_client()
                cleanup.assert_called_once()
                self.assertEqual(gate.scenario.prefix, 'mongodb_gate_0123456789abcdef')
                self.assertEqual(gate.scenario.client_uid, 'old-client-uid')
                self.assertTrue(gate.scenario.workload_started)
                self.assertFalse(json.loads(gate.path.read_text())['client_attempted'])

    def test_cleanup_recovers_interruption_between_client_deletion_and_journal_save(self):
        with tempfile.TemporaryDirectory() as tmp:
            gate = self.gate(tmp)
            gate.state.update(source_prefix='mongodb_gate_0123456789abcdef', client_attempted=True,
                              client_uid='old-client-uid', workload_started=True)
            def create(): gate.scenario.client_uid = 'replacement-cleanup-client'
            with patch.object(gate.lab, 'get', return_value=None), \
                    patch.object(gate.scenario, 'client_create', side_effect=create) as client, \
                    patch.object(gate.scenario, 'cleanup') as cleanup:
                gate.cleanup_client()
                client.assert_called_once();cleanup.assert_called_once()
                self.assertEqual(gate.scenario.client_uid, 'replacement-cleanup-client')
                self.assertFalse(json.loads(gate.path.read_text())['client_attempted'])

    def test_cleanup_reentry_waits_for_journaled_volume_after_pvc_is_gone(self):
        with tempfile.TemporaryDirectory() as tmp:
            gate = self.gate(tmp)
            name = gate.state['name'] + '-data'
            gate.state['resources'] = [{'kind': 'pvc', 'name': name, 'uid': 'claim-uid', 'volume_name': 'owned-pv'}]
            gate.save()
            volume = {'spec': {'storageClassName': 'mongodb-longhorn',
                              'claimRef': {'name': name, 'namespace': 'mongodb-gate', 'uid': 'claim-uid'}}}
            with patch.object(gate.lab, 'verify', return_value={'cluster_uid': 'owned-core'}), \
                    patch.object(gate.lab, 'get', side_effect=lambda kind, *args: volume if kind == 'pv' else None), \
                    patch.object(gate.lab, 'run') as run, patch.object(gate, 'api_input') as submit:
                gate.cleanup()
                submit.assert_not_called()
                self.assertIn('pv/owned-pv', run.call_args.args)
            self.assertFalse(gate.path.exists())

    def test_cleanup_reentry_refuses_a_rebound_volume(self):
        with tempfile.TemporaryDirectory() as tmp:
            gate = self.gate(tmp)
            name = gate.state['name'] + '-data'
            gate.state['resources'] = [{'kind': 'pvc', 'name': name, 'uid': 'claim-uid', 'volume_name': 'owned-pv'}]
            gate.save()
            volume = {'spec': {'storageClassName': 'mongodb-longhorn',
                              'claimRef': {'name': name, 'namespace': 'mongodb-gate', 'uid': 'foreign-claim'}}}
            with patch.object(gate.lab, 'verify', return_value={'cluster_uid': 'owned-core'}), \
                    patch.object(gate.lab, 'get', side_effect=lambda kind, *args: volume if kind == 'pv' else None), \
                    patch.object(gate.lab, 'run') as run, patch.object(gate, 'api_input') as submit:
                with self.assertRaisesRegex(ValueError, 'foreign volume'): gate.cleanup()
                run.assert_not_called();submit.assert_not_called()
            self.assertTrue(gate.path.exists())

    def test_failed_first_create_response_is_recovered_through_execute(self):
        with tempfile.TemporaryDirectory() as tmp:
            gate = self.gate(tmp)
            gate.state = None
            objects = module.backup_objects('0123456789abcdef', 'mongodb-gate')
            gate.run_id = '0123456789abcdef'
            current = {}
            def get(kind, name='', ns=''):
                return current.get((kind, name))
            def submit(*args, obj):
                if args[0] == 'create':
                    created = json.loads(json.dumps(obj))
                    created['metadata']['uid'] = 'first-secret-uid'
                    current[('secret', created['metadata']['name'])] = created
                    raise ValueError('first-create-response-lost')
                self.assertEqual(obj['preconditions'], {'uid': 'first-secret-uid'})
                current.clear()
                return ''
            with patch.object(module, 'backup_objects', return_value=objects), \
                    patch.object(gate.lab, 'verify', return_value={'healthy': True, 'cluster_uid': 'owned-core'}), \
                    patch.object(gate.lab, 'get', side_effect=get), patch.object(gate.lab, 'run'), \
                    patch.object(gate, 'api_input', side_effect=submit), patch.object(gate.scenario, 'health'), \
                    patch.object(gate, 'diagnostics'):
                with self.assertRaisesRegex(ValueError, 'first-create-response-lost'):
                    gate.execute()
            self.assertFalse(current)
            self.assertFalse(gate.path.exists())

    def test_restore_requires_unchanged_remote_artifact_before_creating_client(self):
        with tempfile.TemporaryDirectory() as tmp:
            gate = self.gate(tmp)
            gate.state['backup'] = {'objects': [{'sha256': 'accepted'}]}
            gate.save()
            with patch.object(gate.lab, 'verify', return_value={'healthy': True, 'cluster_uid': 'owned-core'}), \
                    patch.object(gate.scenario, 'health'), patch.object(gate, 'request_for_cleanup'), \
                    patch.object(gate, 'verify_objects', return_value={'objects': [{'sha256': 'changed'}]}), \
                    patch.object(gate.scenario, 'client_create') as create:
                with self.assertRaisesRegex(ValueError, 'checksums changed'): gate.restore()
                create.assert_not_called()
            self.assertEqual(json.loads(gate.path.read_text())['backup']['objects'][0]['sha256'], 'accepted')

    def test_restore_failure_preserves_fault_and_blocks_unsafe_configuration_rollback(self):
        with tempfile.TemporaryDirectory() as tmp:
            gate = self.gate(tmp)
            gate.state.update(backup={'objects': []}, source_cluster_uid='source-uid',
                              request={'name': 'mongodb-gate-data-logical-0123456789abcdef'})
            gate.save()
            with patch.object(gate.lab, 'verify', return_value={'healthy': True, 'cluster_uid': 'owned-core'}), \
                    patch.object(gate.lab, 'get', return_value={'metadata': {'uid': 'source-uid'}}), \
                    patch.object(gate.scenario, 'health'), patch.object(gate, 'request_for_cleanup'), \
                    patch.object(gate, 'verify_objects', return_value=gate.state['backup']), \
                    patch.object(gate.scenario, 'client_create'), patch.object(gate.scenario, 'js'), \
                    patch.object(gate, 'configure', side_effect=ValueError('first-restore-failure')) as configure, \
                    patch.object(gate, 'restore_configuration') as rollback, patch.object(gate, 'cleanup_client') as clean:
                with self.assertRaisesRegex(ValueError, 'first-restore-failure'): gate.restore()
                rollback.assert_not_called();clean.assert_not_called()
                self.assertEqual(configure.call_args.args[0]['cluster_uid'], 'source-uid')
            fault = json.loads(Path(tmp, 'mongodb-fault-restore.json').read_text())
            self.assertEqual(fault['source_uid'], 'source-uid')
            gate.state['active_operation'] = 'restore'
            with self.assertRaisesRegex(ValueError, 'owning fault recovery'): gate.remove_request()

    def test_restore_success_requires_data_assertion_then_product_and_client_cleanup(self):
        with tempfile.TemporaryDirectory() as tmp:
            gate = self.gate(tmp)
            gate.state.update(backup={'objects': []}, source_cluster_uid='source-uid',
                              request={'name': 'mongodb-gate-data-logical-0123456789abcdef'})
            gate.save()
            with patch.object(gate.lab, 'verify', return_value={'healthy': True, 'cluster_uid': 'owned-core'}), \
                    patch.object(gate.lab, 'get', return_value={'metadata': {'uid': 'source-uid'}}), \
                    patch.object(gate.scenario, 'health'), patch.object(gate, 'request_for_cleanup'), \
                    patch.object(gate, 'verify_objects', return_value=gate.state['backup']), \
                    patch.object(gate.scenario, 'client_create'), patch.object(gate.scenario, 'js') as js, \
                    patch.object(gate, 'configure'), patch.object(gate, 'restore_configuration') as rollback, \
                    patch.object(gate, 'cleanup_client') as clean:
                with redirect_stdout(StringIO()) as output:
                    gate.restore()
                self.assertIn('MONGODB_LOGICAL_RESTORE_INPLACE_PASS', output.getvalue())
                self.assertNotIn('\nMONGODB_LOGICAL_RESTORE_PASS\n', '\n' + output.getvalue())
                self.assertIn('post-backup-write=absent', js.call_args.args[1])
                self.assertIn('backup_value', js.call_args.args[0])
                rollback.assert_called_once();clean.assert_called_once()
            self.assertTrue(json.loads(gate.path.read_text())['restore_complete'])
            self.assertFalse(Path(tmp, 'mongodb-fault-restore.json').exists())

    def test_backup_cleanup_cannot_discard_an_unverified_restore_fault(self):
        with tempfile.TemporaryDirectory() as tmp:
            gate = self.gate(tmp)
            gate.save()
            fault = Path(tmp, 'mongodb-fault-restore.json')
            fault.write_text(json.dumps({'cluster_uid': 'owned-core', 'run_id': gate.state['run_id'],
                'backup_journal': gate.path.name, 'type': 'logical'}))
            with patch.object(gate.lab, 'verify', return_value={'cluster_uid': 'owned-core'}), \
                    patch.object(gate.lab, 'get') as get, patch.object(gate, 'api_input') as submit:
                with self.assertRaisesRegex(ValueError, 'requires data recovery'): gate.cleanup()
                get.assert_not_called();submit.assert_not_called()
            self.assertTrue(fault.exists());self.assertTrue(gate.path.exists())


if __name__ == '__main__':
    unittest.main()
