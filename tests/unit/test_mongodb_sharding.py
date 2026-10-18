"""Sharded fixture boundaries, actual product inputs and failure cleanup contracts."""
import importlib.util
from contextlib import ExitStack, redirect_stdout
from io import StringIO
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
import subprocess
from unittest.mock import patch

from cryptography import x509
import yaml

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('mongodb_sharding', ROOT/'tests/helpers/mongodb-sharding.py')
module = importlib.util.module_from_spec(spec);spec.loader.exec_module(module)


class MongoDBShardingTests(unittest.TestCase):
    def test_scale_empty_donor_accepts_bson_int64_zero_and_refuses_nonzero(self):
        # Live r8.0.32 dbStats.objects was typeof object, String(value) == '0';
        # the shared assert.eq JSON serialization distinguishes Long(0) from 0.
        import shutil
        node=shutil.which('node')
        if not node:self.skipTest('Node is required for the BSON client assertion contract')
        script="""
const assert=(ok)=>{if(!ok)throw Error('assertion');};
assert.eq=(a,b)=>assert(JSON.stringify(a)===JSON.stringify(b));
assert.commandWorked=r=>{assert(r.ok===1);return r;};
for(const value of [0,1,-1]){
 const donor={getDB:()=>({runCommand:()=>({ok:1,objects:{toJSON:()=>String(value),toString:()=>String(value)}})})};
 const row={name:'owned'};
 let passed=false;
 try{CHECK;passed=true;}catch(e){if(e.message!=='assertion')throw e;}
 assert(passed===(value===0));
}
"""
        check=next(row.strip() for row in module.SCALE_DRAIN.splitlines() if 'runCommand({dbStats:1})' in row)
        subprocess.run([node,'-e',script.replace('CHECK',check)],check=True,capture_output=True,text=True)

    def test_scale_direct_shard_uses_existing_cluster_admin_privileges(self):
        # Operator 1.23.1 mgo.go:getRoles omits directShardOperations from
        # databaseAdmin. MongoDB r8.0.32 builtin_roles.yml grants clusterAdmin
        # collStats/dbStats/dropDatabase, but not find/listIndexes/listCollections.
        # Keep user-data reads and full index verification on the mongos path.
        load = module.SCALE_LOAD.split('const donor=new Mongo', 1)[1]
        drain = module.SCALE_DRAIN.split('const donor=new Mongo', 1)[1]
        for script in (load, drain):
            self.assertIn('MONGODB_CLUSTER_ADMIN_USER', script)
            self.assertNotIn('MONGODB_DATABASE_ADMIN_USER', script)
            self.assertNotIn('.countDocuments(', script)
            self.assertNotIn('.getCollectionInfos(', script)
        self.assertIn("runCommand({collStats:'documents'})", load)
        self.assertIn('copied.count', load)
        self.assertIn('copied.nindexes', load)
        self.assertNotIn('copied.getIndexes', load)
        self.assertIn('runCommand({dbStats:1})).objects', drain)
        self.assertIn('runCommand({dropDatabase:1', drain)
        self.assertIn('SHARDING_INDEX_DIGEST', module.SCALE_VERIFY)

    def test_scale_configuration_uses_product_and_refuses_unrelated_edits(self):
        for drift in (False, True):
            with self.subTest(drift=drift), tempfile.TemporaryDirectory() as tmp:
                gate = self.gate(tmp);gate.directory.mkdir()
                original = {'mongodb_replsets': [{'name': 'rs0', 'size': 3}, {'name': 'rs1', 'size': 3}],
                            'mongodb_name': module.NAME, 'mongodb_pvc_size': '7Gi'}
                gate.state['scale'] = {'original_config': yaml.safe_dump(original)}
                config = original | ({'mongodb_pvc_size': '8Gi'} if drift else {})
                path = gate.directory/'config.yml';path.write_text(yaml.safe_dump(config))
                with patch.object(gate, 'product') as product:
                    if drift:
                        with self.assertRaisesRegex(ValueError, 'concurrent configuration'):gate.scale_config(('rs0','rs1','rs2'))
                        product.assert_not_called();self.assertEqual(yaml.safe_load(path.read_text()), config)
                    else:
                        gate.scale_config(('rs0','rs1','rs2'));product.assert_called_once()
                        self.assertEqual(yaml.safe_load(path.read_text())['mongodb_replsets'][-1], {'name':'rs2','size':3})
                        product.reset_mock();gate.scale_config(('rs0','rs1'));product.assert_called_once()
                        self.assertEqual(yaml.safe_load(path.read_text()), original)

    def test_scale_retirement_checks_entire_claim_graph_before_first_delete(self):
        import copy
        for drift in (None, 'drain', 'active', 'binding', 'claim', 'reclaim', 'volume'):
            with self.subTest(drift=drift), tempfile.TemporaryDirectory() as tmp:
                gate = self.gate(tmp);gate.state['scale'] = {'drained': drift != 'drain'}
                claims = [{'kind':'pvc','name':'mongod-data-'+module.NAME+'-rs2-'+str(i),
                           'uid':'claim-'+str(i),'volume':'volume-'+str(i)} for i in range(3)]
                gate.state['resources'] = copy.deepcopy(claims)
                objects = {}
                for r in claims:
                    objects[('pvc',r['name'])] = {'metadata':{'name':r['name'],'uid':r['uid'],'namespace':'mongodb-gate',
                        'labels':{'app.kubernetes.io/instance':module.NAME,
                                  'app.kubernetes.io/managed-by':'percona-server-mongodb-operator'}},
                        'spec':{'storageClassName':'mongodb-longhorn','volumeName':r['volume']}}
                    objects[('pv',r['volume'])] = {'spec':{'storageClassName':'mongodb-longhorn',
                        'persistentVolumeReclaimPolicy':'Delete','claimRef':{'namespace':'mongodb-gate','name':r['name'],'uid':r['uid']}}}
                last = claims[-1]
                if drift == 'binding':objects[('pv',last['volume'])]['spec']['claimRef']['uid'] = 'foreign'
                if drift == 'reclaim':objects[('pv',last['volume'])]['spec']['persistentVolumeReclaimPolicy'] = 'Retain'
                if drift == 'claim':objects[('pvc',last['name'])]['metadata']['uid'] = 'foreign'
                if drift == 'volume':objects[('pvc',last['name'])]['spec']['volumeName'] = 'foreign'
                def get(kind, name=None, *args, **kwargs):
                    if kind == 'pods':return {'items':[]}
                    if kind == 'statefulset':return {'metadata':{}} if drift == 'active' else None
                    return objects.get((kind,name))
                def delete(kind,name,uid):
                    row = next(r for r in claims if r['name'] == name)
                    self.assertEqual(uid,row['uid']);objects.pop(('pvc',name));objects.pop(('pv',row['volume']))
                with patch.object(gate.lab,'get',side_effect=get), patch.object(gate,'delete',side_effect=delete) as remove, \
                        patch.object(gate.lab,'run'), redirect_stdout(StringIO()) as output:
                    if drift:
                        with self.assertRaises(ValueError):gate.scale_volumes()
                        remove.assert_not_called();self.assertNotIn('VOLUMES_CLEAN_PASS',output.getvalue())
                    else:
                        gate.scale_volumes();self.assertEqual(remove.call_count,3)
                        self.assertEqual(gate.state['resources'],[])
                        self.assertIn('VOLUMES_CLEAN_PASS',output.getvalue())

    def test_scale_requires_matching_loaded_and_drained_digest_and_cleanup_for_pass(self):
        import base64
        for failed in (None, 'load', 'drain', 'digest', 'index', 'cleanup', 'interrupt'):
            with self.subTest(failed=failed), tempfile.TemporaryDirectory() as tmp:
                gate = self.gate(tmp);gate.state['healthy'] = True;gate.directory.mkdir()
                gate.directory.joinpath('config.yml').write_text('mongodb_replsets: []\n')
                certs = gate.scenario.fixture.credentials(module.NAME)['items']
                secrets = {o['metadata']['name']:{'data':{'tls.crt':base64.b64encode(o['stringData']['tls.crt'].encode()).decode()}}
                           for o in certs if 'tls.crt' in o['stringData']}
                events = []
                def js(code, marker):
                    events.append(marker)
                    if code == module.SCALE_LOAD and failed == 'load':raise ValueError('load failed')
                    if code == module.SCALE_DRAIN:
                        if failed == 'drain':raise ValueError('drain failed')
                        if failed == 'interrupt':raise SystemExit(143)
                    if code == module.SCALE_CLEAN and failed == 'cleanup':raise ValueError('cleanup failed')
                    digest = 'b' if failed == 'digest' and code == module.SCALE_VERIFY else 'a'
                    index = 'b' if failed == 'index' and code == module.SCALE_VERIFY else 'a'
                    return (marker+'\nMONGODB_SHARDING_DATA_DIGEST sha256='+digest*64+' documents=199\n'+
                            'MONGODB_SHARDING_INDEX_DIGEST sha256='+index*64+' indexes=3\n')
                with ExitStack() as stack, redirect_stdout(StringIO()) as output:
                    for item in [patch.object(gate,'verify'),patch.object(gate,'capture'),patch.object(gate,'health'),
                                 patch.object(gate.scenario,'health'),patch.object(gate.lab,'get',side_effect=lambda k,n,*a:secrets[n]),
                                 patch.object(gate,'client'),patch.object(gate,'scale_config'),
                                 patch.object(gate,'wait_scaled_donor'),
                                 patch.object(gate,'scale_volumes',side_effect=lambda:events.append('retire-volumes')),
                                 patch.object(gate.scenario,'js',side_effect=js),patch.object(gate.scenario,'delete_client'),
                                 patch.object(gate.lab,'diagnostics'),patch.object(gate,'cleanup')]:stack.enter_context(item)
                    if failed == 'interrupt':
                        with self.assertRaises(SystemExit):gate.scale()
                    elif failed:
                        with self.assertRaises(ValueError):gate.scale()
                    else:gate.scale()
                    self.assertEqual('MONGODB_SHARDING_SCALE_PASS' in output.getvalue().splitlines(),failed is None)
                    if failed in ('load','drain','digest','index','interrupt'):self.assertNotIn('retire-volumes',events)
                if failed:self.assertTrue(gate.fault.exists())
                else:self.assertFalse(gate.fault.exists())

    def test_scale_uses_fixed_launcher_and_native_operator_topology_path(self):
        script='source tests/helpers/mongodb-runner.sh\nmongodb_launch(){ printf "%s|%s\\n" "$1" "$2"; }\nmongodb_dispatch --mongodb-sharding-scale\n'
        result=subprocess.run(['bash','-c',script],cwd=ROOT,capture_output=True,text=True,check=True)
        self.assertEqual(result.stdout.strip(),'sharding-scale|MONGODB_SHARDING_SCALE_PASS')
        self.assertIn("assert.eq(2,config.chunks.countDocuments({uuid:metadata.uuid,shard:'rs2'}))",module.SCALE_LOAD)
        for guard in ["config.chunks.countDocuments({shard:'rs2'})", "config.databases.countDocuments({primary:'rs2'})",
                      "assert([dbname,prefix+'_scale'].includes(row.name))", 'runCommand({dbStats:1})).objects']:
            self.assertIn(guard,module.SCALE_DRAIN)
        self.assertNotIn('removeShard:',module.SCALE_DRAIN)

    def test_scale_rejects_missing_new_shard_certificate_before_changing_config(self):
        import base64
        with tempfile.TemporaryDirectory() as tmp:
            gate = self.gate(tmp);gate.state['healthy'] = True
            # A valid certificate for another approved fixture is still wrong
            # for the new shard; namespace ownership alone cannot prove SANs.
            pem = gate.scenario.fixture.credentials()['items'][0]['stringData']['tls.crt']
            secret = {'data':{'tls.crt':base64.b64encode(pem.encode()).decode()}}
            with patch.object(gate,'verify'),patch.object(gate,'capture'),patch.object(gate,'health'), \
                    patch.object(gate.scenario,'health'),patch.object(gate.lab,'get',return_value=secret), \
                    patch.object(gate,'client') as client,patch.object(gate,'scale_config') as product:
                with self.assertRaisesRegex(ValueError,'DNS certificate'):gate.scale()
                client.assert_not_called();product.assert_not_called()
            self.assertFalse(gate.fault.exists());self.assertNotIn('scale',gate.state)

    def test_scale_waits_for_operator_owned_donor_and_refuses_replacement(self):
        for replaced in (False, True):
            with self.subTest(replaced=replaced),tempfile.TemporaryDirectory() as tmp:
                gate=self.gate(tmp);name=module.NAME+'-rs2-0'
                gate.state['resources']=[{'kind':'pod','name':name,'uid':'original'}]
                pod={'metadata':{'name':name,'namespace':'mongodb-gate','uid':'foreign' if replaced else 'original',
                     'labels':{'app.kubernetes.io/instance':module.NAME,
                               'app.kubernetes.io/managed-by':'percona-server-mongodb-operator'}}}
                with patch.object(gate.lab,'get',side_effect=[{'items':[pod]},None,{'items':[]},None]), \
                        patch.object(module.time,'sleep') as pause:
                    if replaced:
                        with self.assertRaisesRegex(ValueError,'replaced donor'):gate.wait_scaled_donor()
                        pause.assert_not_called()
                    else:
                        gate.wait_scaled_donor();pause.assert_called_once_with(5)

    def test_sharding_diagnostics_export_only_controlled_stage_and_numeric_error(self):
        gate = module.ShardingGate(ROOT)
        report = 'MONGODB_CLIENT_ERROR '+json.dumps({'stage':'sharding-populated-migration',
            'name':'MongoServerError','code':13,'line':44,'message':'private-business-material'})
        self.assertEqual(gate.module.client_error(report),
                         ' stage=sharding-populated-migration error=MongoServerError code=13 line=44')
        self.assertEqual(gate.module.client_error(report.replace('sharding-populated-migration', 'private-business-material')), '')

    def test_unbounded_chunk_lookup_uses_expression_order_and_remains_uuid_scoped(self):
        # Numeric query predicates exclude MinKey/MaxKey boundary values in
        # MongoDB 8.0.32. Both ranges need BSON expression order, still scoped
        # to the just-created collection UUID (not another collection's chunk).
        self.assertIn('findOne({uuid:metadata.uuid,$expr:{$and:[', module.SHARD_LAYOUT)
        self.assertIn("{$lte:['$min.tenant',tenant]},{$gt:['$max.tenant',tenant]}", module.SHARD_LAYOUT)
        self.assertNotIn("'min.tenant':{$lte:tenant}", module.SHARD_LAYOUT)

    def gate(self, temporary):
        gate = module.ShardingGate(ROOT)
        gate.base = Path(temporary);gate.lab.directory = Path(temporary)/'clusters/mongodb-gate'
        gate.lab.directory.mkdir(parents=True)
        gate.path = gate.lab.directory/'mongodb-sharding-owned.json'
        gate.fault = gate.lab.directory/'mongodb-fault-sharding.json'
        gate.directory = Path(temporary)/'clusters'/module.DEPLOYMENT
        gate.state = {'name': module.NAME, 'deployment': module.DEPLOYMENT, 'cluster_uid': 'core',
                      'run_id': '0123456789abcdef', 'source_identity': {'source': 'unchanged'}, 'resources': []}
        return gate

    def test_sharded_client_routes_to_mongos_with_target_ca_and_credentials(self):
        gate = module.ShardingGate(ROOT)
        obj = gate.scenario.fixture.client_object(module.NAME, sharded=True)
        container = obj['spec']['containers'][0]
        uri = container['env'][0]['value']
        self.assertIn(module.NAME+'-mongos.mongodb-gate.svc.cluster.local:27017', uri)
        self.assertIn('tls=true&tlsCAFile=/tls/ca.crt', uri)
        self.assertNotIn('replicaSet=', uri)
        self.assertNotIn(gate.scenario.fixture.NAME, json.dumps(obj))
        self.assertEqual(container['envFrom'][0]['secretRef']['name'], module.NAME+'-users')
        secrets = gate.scenario.fixture.credentials(module.NAME)['items']
        cert = x509.load_pem_x509_certificate(secrets[0]['stringData']['tls.crt'].encode())
        sans = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value.get_values_for_type(x509.DNSName)
        for role in ('rs0', 'rs1', 'rs2', 'cfg', 'mongos'):
            self.assertIn(module.NAME+'-'+role+'.mongodb-gate.svc.cluster.local', sans)

    def test_owned_scope_never_accepts_source_wrong_namespace_or_unjournaled_secret(self):
        run_id = '0123456789abcdef'
        obj = {'metadata': {'name': module.NAME+'-tls', 'namespace': 'mongodb-gate',
               'labels': module.LABELS | {'kubeauto.io/test-run': run_id}}}
        self.assertTrue(module.owned(obj, run_id))
        for change in ({'name': 'mongodb-gate-data-tls'}, {'namespace': 'foreign'}, {'labels': {}}):
            self.assertFalse(module.owned({'metadata': obj['metadata'] | change}, run_id))
        self.assertFalse(module.owned(obj, 'ffffffffffffffff'))

    def test_install_uses_separate_inventory_config_credentials_and_router_assertion(self):
        from tests.unit.test_mongodb_delivery import configuration, render
        with tempfile.TemporaryDirectory() as tmp:
            gate = self.gate(tmp);gate.state = None
            config = configuration();config['CLUSTER_NAME'] = 'mongodb-gate'
            original = yaml.safe_dump(config);(gate.lab.directory/'config.yml').write_text(original)
            (gate.lab.directory/'kubectl.kubeconfig').write_text('owned-context')
            calls = []
            def command(argv, **kwargs):
                calls.append(argv)
                if argv[:2] == ['kubecli', 'new']:
                    gate.directory.mkdir();return SimpleNamespace(returncode=0)
                obj = json.loads(kwargs['input'])
                self.assertEqual(obj['metadata']['labels']['kubeauto.io/test-run'], gate.state['run_id'])
                return SimpleNamespace(returncode=0, stdout=json.dumps({'metadata': {'uid': obj['metadata']['name']+'-uid'}}))
            with patch.object(gate.lab, 'verify', return_value={'healthy': True, 'cluster_uid': 'core'}), \
                    patch.object(gate.lab, 'get', return_value={'items': []}), \
                    patch.object(gate, 'source_identity', return_value={'source': 'unchanged'}), \
                    patch.object(gate.scenario, 'health'), patch.object(gate, 'health'), \
                    patch.object(gate, 'product') as product, patch.object(gate, 'client'), \
                    patch.object(gate.scenario, 'cleanup'), patch.object(gate.scenario, 'js') as js, \
                    patch.object(module.subprocess, 'run', side_effect=command):
                gate.install()
            product.assert_called_once()
            self.assertTrue(gate.state['healthy']);self.assertFalse(gate.fault.exists())
            desired = yaml.safe_load(gate.directory.joinpath('config.yml').read_text())
            cr = render(desired, 'cluster')[0]
            self.assertEqual([(r['name'], r['size']) for r in cr['spec']['replsets']], [('rs0', 3), ('rs1', 3)])
            self.assertEqual(cr['spec']['sharding']['configsvrReplSet']['size'], 3)
            self.assertEqual(cr['spec']['sharding']['mongos']['size'], 3)
            self.assertEqual(desired['mongodb_pvc_size'], '2Gi')
            self.assertFalse(desired['mongodb_volume_expansion_enabled'])
            self.assertFalse(cr['spec']['backup']['enabled'])
            self.assertEqual(gate.lab.directory.joinpath('config.yml').read_text(), original)
            self.assertIn('clusters/'+module.DEPLOYMENT, gate.directory.joinpath('hosts').read_text())
            self.assertEqual(gate.directory.joinpath('config.yml').stat().st_mode & 0o777, 0o600)
            self.assertIn("'isdbgrid'", js.call_args.args[0]);self.assertTrue(js.call_args.kwargs['app'])
            self.assertEqual(calls[0], ['kubecli', 'new', module.DEPLOYMENT])

    def test_mongos_and_data_members_allow_password_clients_with_verified_tls(self):
        from tests.unit.test_mongodb_delivery import configuration, render
        c = configuration() | {'mongodb_sharding_enabled': True,
            'mongodb_replsets': [{'name': 'rs0', 'size': 3}, {'name': 'rs1', 'size': 3}]}
        cr = render(c)[0]['spec']
        self.assertEqual(cr['tls']['mode'], 'requireTLS')
        self.assertFalse(cr['tls']['allowInvalidCertificates'])
        for role in [*cr['replsets'], cr['sharding']['configsvrReplSet'], cr['sharding']['mongos']]:
            self.assertEqual(yaml.safe_load(role['configuration'])['net']['tls'],
                             {'allowConnectionsWithoutCertificates': True})

    def test_client_records_intent_before_creation_and_uid_before_wait(self):
        with tempfile.TemporaryDirectory() as tmp:
            gate = self.gate(tmp)
            def create(*args, **kwargs):
                record = json.loads(gate.path.read_text())['client']
                self.assertEqual(record, {'run_id': gate.scenario.prefix, 'uid': None})
                return SimpleNamespace(returncode=0, stdout=json.dumps({'metadata': {'uid': 'owned-client'}}))
            def wait(*args, **kwargs):
                self.assertEqual(json.loads(gate.path.read_text())['client']['uid'], 'owned-client')
            with patch.object(gate.lab, 'get', return_value=None), \
                    patch.object(module.subprocess, 'run', side_effect=create), patch.object(gate.lab, 'run', side_effect=wait):
                gate.client()

    def test_cross_process_client_cleanup_requires_recorded_uid_run_and_exact_target(self):
        import copy
        with tempfile.TemporaryDirectory() as tmp:
            gate = self.gate(tmp)
            obj = gate.scenario.fixture.client_object(module.NAME, sharded=True)
            prefix = 'mongodb_gate_0123456789abcdef'
            obj['metadata'].update(uid='owned-client')
            obj['metadata']['labels']['kubeauto.io/test-run'] = prefix
            obj['spec']['volumes'][0]['secret']['defaultMode'] = 420
            obj['spec']['volumes'].append({'name': 'kube-api-access-fixture', 'projected': {'sources': []}})
            gate.state['client'] = {'uid': 'owned-client', 'run_id': prefix}
            for change in ('valid', 'uid', 'run', 'target', 'unrecorded'):
                current = copy.deepcopy(obj)
                if change == 'uid':current['metadata']['uid'] = 'foreign'
                if change == 'run':current['metadata']['labels']['kubeauto.io/test-run'] = gate.scenario.prefix
                if change == 'target':current['spec']['volumes'][0]['secret']['secretName'] = 'foreign'
                if change == 'unrecorded':gate.state.pop('client')
                with patch.object(gate.lab, 'get', return_value=current), \
                        patch.object(gate.scenario, 'delete_client') as delete, \
                        patch.dict(module.os.environ, {}, clear=True):
                    if change == 'valid':
                        gate.cleanup_client();delete.assert_called_once()
                        self.assertEqual(gate.scenario.client_uid, 'owned-client')
                        self.assertEqual(gate.scenario.prefix, prefix)
                    else:
                        with self.assertRaisesRegex(ValueError, 'refuses'):gate.cleanup_client()
                        delete.assert_not_called()
                gate.scenario.prefix = 'mongodb_gate_fedcba9876543210'

    def test_legacy_client_cleanup_requires_explicit_original_uid_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            gate = self.gate(tmp)
            obj = gate.scenario.fixture.client_object(module.NAME, sharded=True)
            uid, prefix = '5ff8a23e-cf0a-4cdc-bc8d-05039100f311', 'mongodb_gate_754075103a363f0c'
            obj['metadata']['uid'] = uid;obj['metadata']['labels']['kubeauto.io/test-run'] = prefix
            for evidence in (uid+','+prefix, 'invalid', '6ff8a23e-cf0a-4cdc-bc8d-05039100f311,'+prefix):
                with patch.object(gate.lab, 'get', return_value=obj), \
                        patch.object(gate.scenario, 'delete_client') as delete, \
                        patch.dict(module.os.environ, {'MONGODB_SHARDING_RECOVERY_CLIENT': evidence}):
                    if evidence == uid+','+prefix:
                        gate.cleanup_client();delete.assert_called_once()
                    else:
                        with self.assertRaises(ValueError):gate.cleanup_client()
                        delete.assert_not_called()

    def test_recovery_evidence_and_empty_cancel_reach_the_durable_protocol(self):
        runner = ROOT/'tests/helpers/mongodb-runner.sh'
        for sync_rc in (0, 1):
            with tempfile.TemporaryDirectory() as tmp:
                script = r'''
source "$1"
ROOT="$2"
bash() { if [[ "$*" == *sync-kubeauto.sh* ]]; then return "$SYNC_RC"; fi; }
ssh_mongodb() {
  if [[ "$*" == *MONGODB_LEASE_READY* ]]; then
    printf 'MONGODB_LEASE_READY\n'
    IFS= read -r mode
    IFS= read -r evidence
    printf '%s\n%s\n' "$mode" "$evidence" > "$ROOT/protocol"
    [[ "$mode" != CANCEL ]] || return 1
    printf 'MONGODB_DURABLE_LAUNCHED\n'
  fi
}
monitor_remote_job() { return 0; }
remote_job_summary() { printf 'state=exited rc=0\n'; }
mongodb_launch sharding-clean MONGODB_SHARDING_CLEAN_PASS
'''
                env = module.os.environ | {'SYNC_RC': str(sync_rc),
                    'MONGODB_SHARDING_RECOVERY_CLIENT': '5ff8a23e-cf0a-4cdc-bc8d-05039100f311,mongodb_gate_754075103a363f0c'}
                r = subprocess.run(['bash', '-c', script, 'test', str(runner), tmp],
                                   env=env, capture_output=True, text=True, timeout=5)
                self.assertEqual(r.returncode, sync_rc, r.stdout+r.stderr)
                expected = ('CANCEL\n\n' if sync_rc else '--sharding-clean\n'+env['MONGODB_SHARDING_RECOVERY_CLIENT']+'\n')
                self.assertEqual(Path(tmp, 'protocol').read_text(), expected)
        for mode, value in [('sharding-data', env['MONGODB_SHARDING_RECOVERY_CLIENT']),
                            ('sharding-clean', 'invalid; command')]:
            script = 'source "$1"; bash(){ echo STATIC_NOT_ALLOWED; }; mongodb_launch "$2" marker'
            r = subprocess.run(['bash', '-c', script, 'test', str(runner), mode],
                               env=env | {'MONGODB_SHARDING_RECOVERY_CLIENT': value}, capture_output=True, text=True, timeout=5)
            self.assertEqual(r.returncode, 1)
            self.assertNotIn('STATIC_NOT_ALLOWED', r.stdout)

    def test_cleanup_rebound_volume_blocks_every_delete_and_preserves_journal(self):
        with tempfile.TemporaryDirectory() as tmp:
            gate = self.gate(tmp)
            name = 'mongod-data-'+module.NAME+'-rs0-0'
            gate.state['resources'] = [{'kind': 'pvc', 'name': name, 'uid': 'claim', 'volume': 'target-volume'}]
            gate.save()
            claim = {'metadata': {'name': name, 'namespace': 'mongodb-gate', 'uid': 'claim', 'labels': {
                'app.kubernetes.io/instance': module.NAME, 'app.kubernetes.io/managed-by': 'percona-server-mongodb-operator'}}}
            pv = {'spec': {'storageClassName': 'mongodb-longhorn', 'claimRef': {
                'namespace': 'mongodb-gate', 'name': name, 'uid': 'foreign-claim'}}}
            with patch.object(gate, 'capture'), patch.object(gate.lab, 'get', side_effect=[claim, pv]), \
                    patch.object(gate, 'cleanup_client') as client, patch.object(module.subprocess, 'run') as mutate:
                with self.assertRaisesRegex(ValueError, 'rebound volume'):gate.cleanup()
                client.assert_not_called();mutate.assert_not_called()
            self.assertTrue(gate.path.exists())

    def test_uninstalled_cleanup_proves_absence_and_emits_marker_without_mutation(self):
        with tempfile.TemporaryDirectory() as tmp:
            gate = self.gate(tmp);gate.state = None
            with patch.object(gate.lab, 'verify') as verify, \
                    patch.object(gate.lab, 'get', return_value={'items': []}) as get, \
                    patch.object(gate.lab, 'run') as mutate, redirect_stdout(StringIO()) as output:
                gate.cleanup()
                verify.assert_called_once()
                self.assertEqual(get.call_count, len(module.APIS)+2)
                mutate.assert_not_called()
                self.assertIn('MONGODB_SHARDING_CLEAN_PASS', output.getvalue())
            for kind in ('secret', 'pv'):
                obj = ({'metadata': {'name': module.NAME+'-tls'}} if kind == 'secret' else
                       {'spec': {'claimRef': {'namespace': 'mongodb-gate',
                                             'name': 'mongod-data-'+module.NAME+'-rs0-0'}}})
                def get(k, *args, **kwargs):return {'items': [obj] if k == kind else []}
                with patch.object(gate.lab, 'verify'), patch.object(gate.lab, 'get', side_effect=get), \
                        patch.object(gate.lab, 'run') as mutate, redirect_stdout(StringIO()) as output:
                    with self.assertRaisesRegex(ValueError, 'remains? after cleanup'):gate.cleanup()
                    self.assertNotIn('MONGODB_SHARDING_CLEAN_PASS', output.getvalue());mutate.assert_not_called()
            for path in (gate.directory, gate.fault):
                path.parent.mkdir(parents=True, exist_ok=True);path.touch()
                with patch.object(gate.lab, 'verify'), patch.object(gate.lab, 'get') as get:
                    with self.assertRaisesRegex(ValueError, 'unjournaled'):gate.cleanup()
                    get.assert_not_called()
                path.unlink()

    def test_core_only_cleanup_skips_absent_operator_api_but_checks_workloads_and_volumes(self):
        with tempfile.TemporaryDirectory() as tmp:
            gate = self.gate(tmp);gate.state = None
            def get(kind, *args, **kwargs):
                if kind == 'psmdb':raise AssertionError('MongoDB API is not installed')
                return None if kind == 'crd' else {'items': []}
            with patch.object(gate.lab, 'verify'), patch.object(gate.lab, 'get', side_effect=get) as read, \
                    patch.object(module.subprocess, 'run') as mutate, redirect_stdout(StringIO()) as output:
                gate.cleanup()
                self.assertEqual(read.call_count, len(module.APIS)+1)
                self.assertIn('MONGODB_SHARDING_CLEAN_PASS', output.getvalue());mutate.assert_not_called()

    def test_health_requires_independent_volumes_and_three_router_failure_domains(self):
        with tempfile.TemporaryDirectory() as tmp:
            gate = self.gate(tmp)
            folder = gate.directory/'yml/mongodb';folder.mkdir(parents=True);folder.joinpath('input.json').write_text('{}')
            gate.state['source_identity'] = {'pvc': [{'uid': 'source-claim', 'spec': {'volumeName': 'source-volume'}}]}
            gate.state['resources'] = [{'kind': 'pvc', 'uid': 'target-'+str(i), 'volume': 'volume-'+str(i)} for i in range(9)]
            cr = {'spec': {'replsets': [{'name': 'rs0', 'size': 3}, {'name': 'rs1', 'size': 3}],
                          'sharding': {'enabled': True, 'configsvrReplSet': {'size': 3}, 'mongos': {'size': 3}}}}
            routers = [{'metadata': {'labels': {'app.kubernetes.io/instance': module.NAME,
                'app.kubernetes.io/component': 'mongos'}}, 'spec': {'nodeName': 'worker-'+str(i)}} for i in range(3)]
            with patch.object(gate.scenario.product, 'health'), \
                    patch.object(gate.lab, 'get', side_effect=lambda kind, *args, **kwargs: cr if kind == 'psmdb' else {'items': routers}):
                gate.health()
                first = gate.state['resources'][0]
                for field, value in (('volume', 'source-volume'), ('uid', 'source-claim'), ('volume', 'volume-1')):
                    original = first[field];first[field] = value
                    with self.assertRaisesRegex(ValueError, 'distinct data volumes'):gate.health()
                    first[field] = original
                routers[1]['spec']['nodeName'] = routers[0]['spec']['nodeName']
                with self.assertRaisesRegex(ValueError, 'three hostname failure domains'):gate.health()

    def test_failed_or_interrupted_install_keeps_first_error_when_recovery_also_fails(self):
        for error in (ValueError('first-product-error'), SystemExit(143)):
            with tempfile.TemporaryDirectory() as tmp:
                gate = self.gate(tmp);gate.state = None
                (gate.lab.directory/'config.yml').write_text('mongodb_namespace: mongodb-gate\n')
                (gate.lab.directory/'kubectl.kubeconfig').write_text('owned-context')
                def command(argv, **kwargs):
                    if argv[:2] == ['kubecli', 'new']:
                        gate.directory.mkdir();return SimpleNamespace(returncode=0)
                    obj = json.loads(kwargs['input'])
                    return SimpleNamespace(returncode=0, stdout=json.dumps({'metadata': {'uid': obj['metadata']['name']+'-uid'}}))
                with patch.object(gate.lab, 'verify', return_value={'healthy': True, 'cluster_uid': 'core'}), \
                        patch.object(gate.lab, 'get', return_value={'items': []}), \
                        patch.object(gate, 'source_identity', return_value={'source': 'unchanged'}), \
                        patch.object(gate.scenario, 'health'), patch.object(gate.scenario, 'cleanup'), \
                        patch.object(gate, 'product', side_effect=error), \
                        patch.object(gate, 'cleanup', side_effect=ValueError('secondary-cleanup-error')) as cleanup, \
                        patch.object(module.subprocess, 'run', side_effect=command):
                    with self.assertRaises(type(error)) as caught:gate.install()
                    self.assertEqual(str(caught.exception), str(error));cleanup.assert_called_once()
                self.assertTrue(gate.path.exists());self.assertTrue(gate.fault.exists())

    def test_cleanup_requires_absent_target_graph_before_removing_its_journal(self):
        for remaining in (False, True):
            with tempfile.TemporaryDirectory() as tmp:
                gate = self.gate(tmp);gate.directory.mkdir();gate.save()
                record = {'kind': 'secret', 'name': module.NAME+'-tls', 'uid': 'target-secret'}
                gate.state['resources'] = [record]
                obj = {'metadata': {'name': record['name'], 'namespace': 'mongodb-gate', 'uid': 'target-secret',
                    'labels': module.LABELS | {'kubeauto.io/test-run': gate.state['run_id']}}}
                def get(kind, name='', ns=''):
                    return obj if name else {'items': [obj] if remaining and kind == 'secret' else []}
                with patch.object(gate, 'capture'), patch.object(gate, 'verify'), \
                        patch.object(gate.lab, 'get', side_effect=get), patch.object(gate.lab, 'run'), \
                        patch.object(gate, 'cleanup_client'), patch.object(gate.scenario, 'health'), \
                        patch.object(module.subprocess, 'run', return_value=SimpleNamespace(returncode=0)) as delete:
                    if remaining:
                        with self.assertRaisesRegex(ValueError, 'resources remain'):gate.cleanup()
                        self.assertTrue(gate.path.exists())
                    else:
                        gate.cleanup();self.assertFalse(gate.path.exists());self.assertFalse(gate.directory.exists())
                    self.assertEqual(json.loads(delete.call_args.kwargs['input'])['preconditions'], {'uid': 'target-secret'})

    def test_deletion_recovery_requires_exact_uid_version_finalizer_and_source_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            gate = self.gate(tmp)
            current = {'metadata': {'name': module.NAME, 'namespace': 'mongodb-gate', 'uid': 'target',
                'resourceVersion': '13', 'deletionTimestamp': '2026-10-16T00:00:00Z',
                'labels': {'app.kubernetes.io/managed-by': 'kubeauto', 'kubeauto.io/component': 'mongodb'},
                'finalizers': ['percona.com/delete-psmdb-pods-in-order']}}
            with patch.object(gate, 'verify'), patch.object(module.subprocess, 'run',
                    return_value=SimpleNamespace(returncode=0)) as mutate:
                gate.resume_deletion(current, {'uid': 'target'})
                data = json.loads(mutate.call_args.kwargs['input'])
                self.assertEqual([p['path'] for p in data[:3]],
                                 ['/metadata/uid', '/metadata/resourceVersion', '/metadata/finalizers'])
                mutate.reset_mock()
                for change in ({'uid': 'source'}, {'name': 'mongodb-gate-data'}, {'finalizers': ['foreign/finalizer']}):
                    with self.assertRaisesRegex(ValueError, 'ownership or unknown finalizers'):
                        gate.resume_deletion({'metadata': current['metadata'] | change}, {'uid': 'target'})
                mutate.assert_not_called()

            with patch.object(gate, 'verify', side_effect=ValueError('source-changed')), \
                    patch.object(module.subprocess, 'run') as mutate:
                with self.assertRaisesRegex(ValueError, 'source-changed'):gate.resume_deletion(current, {'uid': 'target'})
                mutate.assert_not_called()

    def test_data_compares_migration_digest_and_cleans_only_its_database(self):
        for changed in (False, True):
            with tempfile.TemporaryDirectory() as tmp:
                gate = self.gate(tmp);gate.state['healthy'] = True
                calls = []
                def js(code, marker):
                    calls.append((code, marker))
                    digest = 'b' if changed and marker == 'MONGODB_SHARDING_POST_MIGRATION_PASS' else 'a'
                    return marker+'\nMONGODB_SHARDING_DATA_DIGEST sha256='+digest*64+' documents=199\n'
                def client():gate.scenario.client_uid = 'owned-client'
                with patch.object(gate, 'verify'), patch.object(gate, 'capture'), patch.object(gate, 'health'), \
                        patch.object(gate.scenario, 'health'), patch.object(gate, 'client', side_effect=client), \
                        patch.object(gate.scenario, 'js', side_effect=js), \
                        patch.object(gate.scenario, 'delete_client') as delete, redirect_stdout(StringIO()) as output:
                    if changed:
                        with self.assertRaisesRegex(ValueError, 'cross-shard digest'):gate.data()
                        self.assertNotIn('\nMONGODB_SHARDING_DATA_PASS\n', output.getvalue())
                    else:
                        gate.data();self.assertIn('\nMONGODB_SHARDING_DATA_PASS\n', output.getvalue())
                    delete.assert_called_once();self.assertFalse(gate.fault.exists())
                    self.assertNotIn('workload', gate.state)
                self.assertEqual(len(calls), 5)
                self.assertIn("prefix+'_sharded'", calls[-1][0])
                self.assertIn('dropDatabase:1', calls[-1][0])
                self.assertNotIn('appdb', calls[-1][0])

    def test_data_preserves_first_failure_and_recovery_journal(self):
        for first in (ValueError('first-business-error'), SystemExit(143)):
            with tempfile.TemporaryDirectory() as tmp:
                gate = self.gate(tmp);gate.state['healthy'] = True
                def client():gate.scenario.client_uid = 'owned-client'
                with patch.object(gate, 'verify'), patch.object(gate, 'capture'), patch.object(gate, 'health'), \
                        patch.object(gate.scenario, 'health'), patch.object(gate, 'client', side_effect=client), \
                        patch.object(gate.scenario, 'js', side_effect=[first, ValueError('cleanup-error')]), \
                        patch.object(gate.scenario, 'delete_client') as delete:
                    with self.assertRaises(type(first)) as caught:gate.data()
                    self.assertEqual(str(caught.exception), str(first));delete.assert_not_called()
                self.assertTrue(gate.fault.exists());self.assertEqual(gate.state['workload'], gate.scenario.prefix)
                self.assertEqual(json.loads(gate.path.read_text())['workload'], gate.scenario.prefix)

    def test_data_refuses_unhealthy_or_faulted_baseline_before_client(self):
        for fault in (False, True):
            with tempfile.TemporaryDirectory() as tmp:
                gate = self.gate(tmp);gate.state['healthy'] = fault
                if fault:gate.fault.write_text('{}')
                with patch.object(gate, 'verify'), patch.object(gate, 'capture'), patch.object(gate, 'health'), \
                        patch.object(gate.scenario, 'health'), patch.object(gate, 'client') as client:
                    with self.assertRaisesRegex(ValueError, 'healthy baseline without pending faults'):gate.data()
                    client.assert_not_called()

    def test_role_loss_deletes_only_proven_primary_uid_and_verifies_replacement_and_digest(self):
        for role in ('rs0', 'cfg', 'mongos'):
            with tempfile.TemporaryDirectory() as tmp:
                gate = self.gate(tmp)
                pods = [{'metadata': {'name': module.NAME+'-'+role+'-'+str(i), 'uid': 'old-'+str(i)}} for i in range(3)]
                with patch.object(gate, 'wait_health'), patch.object(gate, 'verify'), patch.object(gate, 'capture'), \
                        patch.object(gate, 'members', return_value=pods), \
                        patch.object(gate, 'hello', side_effect=[{'isWritablePrimary': i == 1} for i in range(3)]), \
                        patch.object(gate, 'delete') as delete, \
                        patch.object(gate.lab, 'get', return_value={'metadata': {'uid': 'replacement'}}), \
                        patch.object(gate.scenario, 'js') as js:
                    gate.role_loss(role)
                    index = 0 if role == 'mongos' else 1
                    delete.assert_called_once_with('pod', pods[index]['metadata']['name'], 'old-'+str(index))
                    self.assertIn('POST_FAULT', js.call_args.args[1])
                    self.assertNotIn('disruption', gate.state)

    def test_local_hello_starting_is_explicit_and_never_counts_as_a_member_response(self):
        gate = module.ShardingGate(ROOT)
        with patch.object(gate.lab, 'run', return_value='{"ok":0,"starting":true}') as run:
            with self.assertRaisesRegex(ValueError, 'member hello failed'):gate.hello('member')
            self.assertEqual(gate.hello('member', allow_starting=True), {'ok': 0, 'starting': True})
            self.assertIn('--nodb', run.call_args.args)
            code = run.call_args.args[-1]
            self.assertIn('serverSelectionTimeoutMS=2000', code)
            self.assertIn('tlsCAFile=/etc/mongodb-ssl/ca.crt', code)
        for state in ({'ok': 0}, {'ok': 0, 'starting': False}, {'ok': 0, 'starting': True, 'unexpected': True}):
            with patch.object(gate.lab, 'run', return_value=json.dumps(state)):
                with self.assertRaises(ValueError):gate.hello('member', allow_starting=True)

    def test_role_loss_preserves_first_failure_and_unrecovered_journal(self):
        with tempfile.TemporaryDirectory() as tmp:
            gate = self.gate(tmp)
            with patch.object(gate, 'wait_health', side_effect=[None, ValueError('secondary-recovery')]), \
                    patch.object(gate, 'verify'), patch.object(gate, 'members', return_value=[
                        {'metadata': {'name': module.NAME+'-mongos-0', 'uid': 'original'}}]), \
                    patch.object(gate, 'delete', side_effect=SystemExit(143)):
                with self.assertRaises(SystemExit) as error:gate.role_loss('mongos')
                self.assertEqual(error.exception.code, 143)
            self.assertEqual(json.loads(gate.path.read_text())['disruption']['uid'], 'original')

    def quorum_fixture(self, gate):
        original = {'podSelector': {'matchLabels': {'app.kubernetes.io/instance': module.NAME}}, 'ingress': []}
        policy = {'metadata': {'name': module.NAME+'-access', 'namespace': 'mongodb-gate', 'uid': 'policy',
                  'labels': gate.scenario.product.LABELS}, 'spec': original}
        modified, deny = gate.module.quorum_policies(policy, gate.scenario.prefix)
        members = [{'metadata': {'name': module.NAME+'-rs0-'+str(i), 'uid': 'member-'+str(i),
                    'labels': {'kubeauto.io/mongodb-quorum-fault': gate.scenario.prefix}}} for i in range(2)]
        gate.state['quorum'] = {'policy_uid': 'policy', 'original_spec': original, 'modified_spec': modified,
            'members': [{'name': m['metadata']['name'], 'uid': m['metadata']['uid']} for m in members],
            'deny': deny['metadata']['name'], 'deny_uid': 'deny', 'deny_spec': deny['spec']}
        deny['metadata']['uid'] = 'deny'
        return policy | {'spec': modified}, members, deny

    def test_quorum_recovery_validates_complete_graph_before_any_mutation(self):
        for changed in ('policy', 'member', 'deny', 'deny-spec', 'omitted-empty-rules', None):
            with tempfile.TemporaryDirectory() as tmp:
                gate = self.gate(tmp);policy, members, deny = self.quorum_fixture(gate)
                if changed == 'policy':policy['metadata']['uid'] = 'foreign'
                if changed == 'member':members[-1]['metadata']['uid'] = 'foreign'
                if changed == 'deny':deny['metadata']['uid'] = 'foreign'
                if changed == 'deny-spec':deny['spec'] = {'podSelector': {}}
                if changed == 'omitted-empty-rules':
                    deny['spec'] = {k:v for k,v in deny['spec'].items() if k not in ('ingress','egress')}
                objects = {module.NAME+'-access': policy, deny['metadata']['name']: deny,
                           **{o['metadata']['name']: o for o in members}}
                def delete(kind, name, uid):objects.pop(name)
                with patch.object(gate, 'verify'), patch.object(gate.lab, 'get', side_effect=lambda k,n,ns:objects.get(n)), \
                        patch.object(gate, 'patch') as patch_api, patch.object(gate, 'delete', side_effect=delete) as delete_api, \
                        patch.object(gate, 'wait_health'), patch.object(gate.scenario, 'js') as js:
                    if changed and changed != 'omitted-empty-rules':
                        with self.assertRaises(ValueError):gate.recover_quorum()
                        patch_api.assert_not_called();delete_api.assert_not_called();js.assert_not_called()
                        self.assertIn('quorum', gate.state)
                    else:
                        gate.recover_quorum()
                        self.assertEqual(patch_api.call_count, 3);delete_api.assert_called_once()
                        self.assertEqual(patch_api.call_args_list[0].args[2], 'policy')
                        self.assertEqual(js.call_args.args[1], 'MONGODB_SHARDING_POST_FAULT_PASS')
                        self.assertNotIn('quorum', gate.state)

    def test_recovery_keeps_baseline_and_verifies_data_before_workload_cleanup(self):
        with tempfile.TemporaryDirectory() as tmp:
            gate = self.gate(tmp);workload = gate.scenario.prefix
            gate.state.update(workload=workload, client={'run_id': workload, 'uid': 'client'}, quorum={ 'owned': True })
            gate.fault.write_text(json.dumps({'run_id': gate.state['run_id']}))
            order = []
            def client():gate.scenario.client_uid = 'client'
            def recover(**kwargs):
                self.assertFalse(kwargs['check_data']);order.append('restore-policy');gate.state.pop('quorum')
            with patch.object(gate, 'verify'), patch.object(gate, 'capture'), \
                    patch.object(gate, 'restore_client', side_effect=client), \
                    patch.object(gate, 'recover_quorum', side_effect=recover), \
                    patch.object(gate, 'wait_health'), patch.object(gate, 'health'), patch.object(gate.scenario, 'health'), \
                    patch.object(gate.scenario, 'js', side_effect=lambda code,marker:order.append(marker)), \
                    patch.object(gate.scenario, 'delete_client', side_effect=lambda:order.append('delete-client')), \
                    patch.object(gate, 'cleanup') as full_cleanup:
                gate.recover()
                full_cleanup.assert_not_called()
            self.assertEqual(order, ['restore-policy', 'MONGODB_SHARDING_POST_FAULT_PASS',
                'MONGODB_SHARDING_DATABASE_CLEAN_PASS', 'delete-client'])
            self.assertFalse(gate.fault.exists())
            self.assertNotIn('workload', json.loads(gate.path.read_text()))

    def test_recovery_refuses_other_client_and_preserves_failed_data_check(self):
        for wrong_client in (True, False):
            with self.subTest(wrong_client=wrong_client), tempfile.TemporaryDirectory() as tmp:
                gate = self.gate(tmp);workload = gate.scenario.prefix
                gate.state.update(workload=workload, client={'run_id': 'foreign' if wrong_client else workload, 'uid': 'client'})
                gate.fault.write_text(json.dumps({'run_id': gate.state['run_id']}))
                gate.scenario.client_uid = 'client'
                with patch.object(gate, 'verify'), patch.object(gate, 'capture'), patch.object(gate, 'restore_client'), \
                        patch.object(gate, 'wait_health'), patch.object(gate, 'recover_quorum') as recover, \
                        patch.object(gate.scenario, 'js', side_effect=ValueError('digest-failed')) as js, \
                        patch.object(gate.scenario, 'delete_client') as delete:
                    with self.assertRaisesRegex(ValueError, 'recorded workload' if wrong_client else 'digest-failed'):
                        gate.recover()
                    if wrong_client:js.assert_not_called()
                    recover.assert_not_called();delete.assert_not_called()
                self.assertTrue(gate.fault.exists());self.assertIn('workload', gate.state)

    def test_quorum_rejects_extra_policy_before_fault_and_preserves_first_interruption(self):
        with tempfile.TemporaryDirectory() as tmp:
            gate = self.gate(tmp)
            policy, members, deny = self.quorum_fixture(gate)
            gate.state.pop('quorum')
            for p in members:p['metadata']['labels'] = {}
            source = {'metadata': {'name': gate.scenario.fixture.NAME+'-access', 'labels': gate.scenario.product.LABELS}, 'spec': {}}
            policy['spec'] = {'podSelector': {'matchLabels': {'app.kubernetes.io/instance': module.NAME}}, 'ingress': []}
            with patch.object(gate, 'wait_health'), patch.object(gate, 'members', return_value=members), \
                    patch.object(gate.lab, 'get', return_value={'items': [policy, source, {'metadata': {'name': 'foreign'}}]}), \
                    patch.object(module.subprocess, 'run') as mutate:
                with self.assertRaisesRegex(ValueError, 'additional or foreign'):gate.quorum_loss()
                mutate.assert_not_called();self.assertNotIn('quorum', gate.state)
            with patch.object(gate, 'wait_health'), patch.object(gate, 'verify'), \
                    patch.object(gate, 'members', return_value=members), \
                    patch.object(gate.lab, 'get', return_value={'items': [policy, source]}), \
                    patch.object(module.subprocess, 'run', return_value=SimpleNamespace(returncode=0, stdout='{"metadata":{"uid":"deny"}}')), \
                    patch.object(gate, 'patch'), patch.object(gate.scenario, 'restart_isolated_members', side_effect=SystemExit(143)), \
                    patch.object(gate, 'recover_quorum', side_effect=ValueError('secondary-recovery')):
                with self.assertRaises(SystemExit) as error:gate.quorum_loss()
                self.assertEqual(error.exception.code, 143)
                self.assertEqual(json.loads(gate.path.read_text())['quorum']['deny_uid'], 'deny')

    def test_quorum_closes_existing_sockets_proves_demotion_and_bounds_actual_routed_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            gate = self.gate(tmp);policy, members, deny = self.quorum_fixture(gate)
            gate.state.pop('quorum')
            for p in members:p['metadata']['labels'] = {}
            pods = members+[{'metadata': {'name': module.NAME+'-rs0-2', 'uid': 'member-2', 'labels': {}}}]
            source = {'metadata': {'name': gate.scenario.fixture.NAME+'-access', 'labels': gate.scenario.product.LABELS}}
            with patch.object(gate, 'wait_health'), patch.object(gate, 'verify'), \
                    patch.object(gate, 'members', return_value=pods), \
                    patch.object(gate.lab, 'get', return_value={'items': [policy, source]}), \
                    patch.object(module.subprocess, 'run', return_value=SimpleNamespace(returncode=0, stdout='{"metadata":{"uid":"deny"}}')) as create_api, \
                    patch.object(gate, 'patch') as patch_api, \
                    patch.object(gate.scenario, 'restart_isolated_members') as reset, \
                    patch.object(gate, 'hello', side_effect=[{'ok':0,'starting':True}]+[{'ok':1,'isWritablePrimary':False}]*5) as hello, \
                    patch.object(module.time, 'sleep'), \
                    patch.object(gate.scenario, 'js') as js, patch.object(gate, 'recover_quorum') as recover:
                gate.quorum_loss()
                self.assertEqual(len(reset.call_args.args[0]), 2);self.assertEqual(hello.call_count, 6)
                deny_spec = json.loads(create_api.call_args.kwargs['input'])['spec']
                self.assertEqual(deny_spec['ingress'], [])
                self.assertEqual(deny_spec['egress'], [
                    {'to': [{'podSelector': {'matchLabels': {'app.kubernetes.io/instance': module.NAME,
                        'app.kubernetes.io/replset': 'cfg'}}}], 'ports': [{'port': 27017, 'protocol': 'TCP'}]},
                    {'ports': [{'port': 53, 'protocol': 'UDP'}, {'port': 53, 'protocol': 'TCP'}]}])
                self.assertEqual(patch_api.call_count, 3);recover.assert_called_once()
                code, marker = js.call_args.args
                self.assertIn("q:{tenant:-1}", code);self.assertIn("maxTimeMS:3000", code)
                self.assertIn('$set:{quorumProbe:prefix}', code)
                self.assertIn("w:'majority',j:true,wtimeout:2000", code)
                self.assertIn('Date.now()-start<15000', code)
                self.assertFalse(js.call_args.kwargs['authenticate'])
                self.assertEqual(marker, 'MONGODB_SHARDING_QUORUM_REFUSAL_PASS majority-write=refused')

    def test_unrecovered_quorum_keeps_business_client_and_first_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            gate = self.gate(tmp);gate.state['healthy'] = True
            def client():gate.scenario.client_uid = 'client'
            def losses():
                gate.state['quorum'] = {'recovery': 'required'};gate.save()
                raise ValueError('first-quorum-error')
            with patch.object(gate, 'verify'), patch.object(gate, 'capture'), patch.object(gate, 'health'), \
                    patch.object(gate.scenario, 'health'), patch.object(gate, 'client', side_effect=client), \
                    patch.object(gate, 'losses', side_effect=losses), patch.object(gate.scenario, 'js') as js, \
                    patch.object(gate.scenario, 'delete_client') as delete:
                with self.assertRaisesRegex(ValueError, 'first-quorum-error'):gate.data(failure=True)
                delete.assert_not_called();self.assertEqual(js.call_count, 2)
            self.assertTrue(gate.fault.exists());self.assertIn('quorum', json.loads(gate.path.read_text()))

    def test_failure_gate_uses_all_roles_and_quorum_and_uid_delete_preconditions(self):
        gate = module.ShardingGate(ROOT)
        with patch.object(gate, 'role_loss') as role, patch.object(gate, 'quorum_loss') as quorum:
            gate.losses()
            self.assertEqual(role.call_args_list, [unittest.mock.call(r) for r in ('rs0', 'cfg', 'mongos')])
            quorum.assert_called_once()
        with patch.object(module.subprocess, 'run', return_value=SimpleNamespace(returncode=0)) as run:
            gate.delete('pod', module.NAME+'-mongos-0', 'recorded-uid')
            self.assertEqual(json.loads(run.call_args.kwargs['input'])['preconditions'], {'uid': 'recorded-uid'})

    def test_data_changed_source_blocks_every_workload_and_journal_mutation(self):
        with tempfile.TemporaryDirectory() as tmp:
            gate = self.gate(tmp);gate.state['healthy'] = True;gate.save()
            before = gate.path.read_bytes()
            with patch.object(gate.lab, 'verify', return_value={'cluster_uid': 'core'}), \
                    patch.object(gate, 'source_identity', return_value={'source': 'replaced'}), \
                    patch.object(gate, 'capture') as capture, patch.object(gate, 'client') as client, \
                    patch.object(gate.scenario, 'js') as js:
                with self.assertRaisesRegex(ValueError, 'source identity changed'):gate.data()
                capture.assert_not_called();client.assert_not_called();js.assert_not_called()
            self.assertEqual(gate.path.read_bytes(), before);self.assertFalse(gate.fault.exists())

    def test_gate_dispatch_static_inventory_and_full_cleanup_are_registered(self):
        runner = (ROOT/'tests/helpers/mongodb-runner.sh').read_text()
        install = (ROOT/'tests/helpers/mongodb-install-gate.sh').read_text()
        static = (ROOT/'tests/helpers/mongodb-static.sh').read_text()
        self.assertIn('--mongodb-sharding-install', runner)
        self.assertIn('--sharding-install|--sharding-scale|--sharding-data|--sharding-failure|--sharding-recover|--sharding-clean)', install)
        self.assertIn('--mongodb-sharding-recover', runner)
        self.assertLess(install.index('mongodb-sharding.py" cleanup'), install.index('mongodb-lab.py" cleanup-product'))
        self.assertIn('mongodb-sharding-gate', static)
        self.assertIn('tests.unit.test_mongodb_sharding', static)


if __name__ == '__main__':unittest.main()
