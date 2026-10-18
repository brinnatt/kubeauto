#!/usr/bin/env python3
"""Product-created sharded fixture, isolated from the accepted replica set.

Operator 1.23.1 owns shard/config replica sets and mongos. This runner owns only
the disposable deployment, its supplied credentials, business client and cleanup.
"""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import signal
import subprocess
import sys
import time

import yaml

NAME = 'mongodb-gate-sharded'
DEPLOYMENT = 'mongodb-sharding-gate'
LABELS = {'kubeauto.io/component': 'mongodb-sharding-fixture', 'kubeauto.io/managed-by': 'kubeauto'}
APIS = {'psmdb': ('/apis/psmdb.percona.com/v1', 'perconaservermongodbs'),
        'statefulset': ('/apis/apps/v1', 'statefulsets'), 'pdb': ('/apis/policy/v1', 'poddisruptionbudgets'),
        'networkpolicy': ('/apis/networking.k8s.io/v1', 'networkpolicies'),
        **{kind: ('/api/v1', plural) for kind, plural in
           [('pod', 'pods'), ('pvc', 'persistentvolumeclaims'), ('service', 'services'),
            ('secret', 'secrets'), ('configmap', 'configmaps')]}}

SHARD_ADMIN = """
const manager=new Mongo(process.env.MONGODB_URI),admin=manager.getDB('admin');
assert(admin.auth({user:process.env.MONGODB_CLUSTER_ADMIN_USER,pwd:process.env.MONGODB_CLUSTER_ADMIN_PASSWORD}));
const dbname=prefix+'_sharded',ns=dbname+'.documents';
assert.eq(['rs0','rs1'],assert.commandWorked(admin.runCommand({listShards:1})).shards.map(s=>s._id).sort());
"""
SHARD_LAYOUT = SHARD_ADMIN + """
globalThis.mongodbGateStage='sharding-create';
assert.eq(0,c.getDB(dbname).getCollectionNames().length);
assert.eq(null,manager.getDB('config').collections.findOne({_id:ns}));
assert.commandWorked(admin.runCommand({shardCollection:ns,key:{tenant:1},unique:true}));
assert.commandWorked(admin.runCommand({split:ns,middle:{tenant:0}}));
const config=manager.getDB('config'),metadata=config.collections.findOne({_id:ns});
assert(metadata&&!metadata.dropped);
for(const [tenant,to] of [[-1,'rs0'],[1,'rs1']]){
 // Query comparison predicates type-bracket numeric operands and exclude the
 // unbounded MinKey/MaxKey chunks. Expression comparison uses BSON ordering.
 const chunk=config.chunks.findOne({uuid:metadata.uuid,$expr:{$and:[
  {$lte:['$min.tenant',tenant]},{$gt:['$max.tenant',tenant]}]}});
 assert(chunk);
 if(chunk.shard!==to)assert.commandWorked(admin.runCommand({moveChunk:ns,find:{tenant},to,_waitForDelete:true,maxTimeMS:60000}));
}
print('MONGODB_SHARDING_LAYOUT_PASS');
"""
SHARD_DATA = """
globalThis.mongodbGateStage='sharding-crud';
const d=c.getDB(prefix+'_sharded'),col=d.getCollection('documents');
const wc={w:'majority',j:true,wtimeout:10000};
const expected=Array.from({length:200},(_,i)=>({_id:i,tenant:i-100,value:1000,payload:'sharded-'+i}));
assert.commandWorked(d.runCommand({insert:'documents',documents:expected,writeConcern:wc}));
assert.commandWorked(d.runCommand({createIndexes:'documents',indexes:[{key:{value:1},name:'business_value'}],writeConcern:wc}));
const duplicate=d.runCommand({insert:'documents',documents:[{_id:999,tenant:-1,value:0}],writeConcern:wc});
assert.eq(11000,duplicate.writeErrors[0].code);assert.eq(200,col.countDocuments({}));
assert.commandWorked(d.runCommand({update:'documents',updates:[{q:{tenant:42},u:{$set:{value:2000}}}],writeConcern:wc}));
expected.find(r=>r.tenant===42).value=2000;
assert.commandWorked(d.runCommand({delete:'documents',deletes:[{q:{tenant:99},limit:1}],writeConcern:wc}));
expected.pop();
globalThis.mongodbGateStage='sharding-distributed-commit';
const session=c.startSession(),sd=session.getDatabase(d.getName());
try{
 session.startTransaction({readConcern:{level:'snapshot'},writeConcern:wc});
 for(const [tenant,delta] of [[-1,-100],[1,100]]){
  assert.commandWorked(sd.runCommand({update:'documents',updates:[{q:{tenant},u:{$inc:{value:delta}}}]}));
 }
 session.commitTransaction();
 expected.find(r=>r.tenant===-1).value-=100;expected.find(r=>r.tenant===1).value+=100;
 globalThis.mongodbGateStage='sharding-distributed-abort';
 session.startTransaction({readConcern:{level:'snapshot'},writeConcern:wc});
 for(const tenant of [-1,1])assert.commandWorked(sd.runCommand({update:'documents',updates:[{q:{tenant},u:{$inc:{value:500}}}]}));
 session.abortTransaction();
}finally{session.endSession();}
const rows=col.find({}).sort({_id:1}).toArray().map(r=>({_id:r._id,tenant:r.tenant,value:r.value,payload:r.payload}));
const digest=x=>require('crypto').createHash('sha256').update(JSON.stringify(x)).digest('hex');
assert.eq(digest(expected),digest(rows));
assert.eq(1,col.getIndexes().filter(i=>i.name==='business_value').length);
print('MONGODB_SHARDING_DATA_DIGEST sha256='+digest(rows)+' documents='+rows.length);
print('MONGODB_SHARDING_CRUD_TRANSACTION_PASS');
"""
SHARD_MIGRATE = SHARD_ADMIN + """
const config=manager.getDB('config'),metadata=config.collections.findOne({_id:ns});
assert(metadata&&!metadata.dropped);assert.eq({tenant:1},metadata.key);assert.eq(true,metadata.unique);
const placement=()=>config.chunks.find({uuid:metadata.uuid}).sort({'min.tenant':1}).toArray().map(r=>r.shard);
assert.eq(['rs0','rs1'],placement());
globalThis.mongodbGateStage='sharding-populated-migration';
for(const [tenant,to] of [[-1,'rs1'],[1,'rs0']]){
 assert.commandWorked(admin.runCommand({moveChunk:ns,find:{tenant},to,_waitForDelete:true,maxTimeMS:60000}));
}
assert.eq(['rs1','rs0'],placement());
print('MONGODB_SHARDING_POPULATED_MIGRATION_PASS');
"""
SHARD_VERIFY = """
const d=c.getDB(prefix+'_sharded'),col=d.getCollection('documents');
const expected=Array.from({length:199},(_,i)=>({_id:i,tenant:i-100,value:i===142?2000:i===99?900:i===101?1100:1000,payload:'sharded-'+i}));
const rows=col.find({}).sort({_id:1}).toArray().map(r=>({_id:r._id,tenant:r.tenant,value:r.value,payload:r.payload}));
const digest=x=>require('crypto').createHash('sha256').update(JSON.stringify(x)).digest('hex');
assert.eq(digest(expected),digest(rows));
assert.eq(100,col.countDocuments({tenant:{$lt:0}}));assert.eq(99,col.countDocuments({tenant:{$gte:0}}));
assert.eq(1,col.getIndexes().filter(i=>i.name==='business_value').length);
print('MONGODB_SHARDING_DATA_DIGEST sha256='+digest(rows)+' documents='+rows.length);
print('MONGODB_SHARDING_POST_MIGRATION_PASS');
"""
FAULT_VERIFY = SHARD_VERIFY.replace('POST_MIGRATION', 'POST_FAULT')
SHARD_CLEAN = SHARD_ADMIN + """
const config=manager.getDB('config'),metadata=config.collections.findOne({_id:ns});
const d=c.getDB(dbname);
assert.commandWorked(d.runCommand({dropDatabase:1,writeConcern:{w:'majority',wtimeout:10000}}));
assert.eq(0,d.getCollectionNames().length);
assert.eq(null,config.databases.findOne({_id:dbname}));
assert.eq(null,config.collections.findOne({_id:ns}));
if(metadata)assert.eq(0,config.chunks.countDocuments({uuid:metadata.uuid}));
print('MONGODB_SHARDING_DATABASE_CLEAN_PASS');
"""

SCALE_ADMIN = SHARD_ADMIN.replace("['rs0','rs1']", "['rs0','rs1','rs2']")
SCALE_INDEX = """
const indexes=col.getIndexes().sort((a,b)=>a.name.localeCompare(b.name)).map(index=>
 Object.fromEntries(Object.entries(index).sort(([a],[b])=>a.localeCompare(b))));
print('MONGODB_SHARDING_INDEX_DIGEST sha256='+digest(indexes)+' indexes='+indexes.length);
"""
SCALE_DATA = SHARD_DATA + SCALE_INDEX
SCALE_VERIFY = SHARD_VERIFY + SCALE_INDEX
SCALE_LOAD = SCALE_ADMIN + """
globalThis.mongodbGateStage='sharding-scale-populate';
const config=manager.getDB('config'),metadata=config.collections.findOne({_id:ns});
assert(metadata&&!metadata.dropped);
for(const tenant of [-1,1])assert.commandWorked(admin.runCommand({moveChunk:ns,find:{tenant},to:'rs2',_waitForDelete:true,maxTimeMS:60000}));
assert.eq(2,config.chunks.countDocuments({uuid:metadata.uuid,shard:'rs2'}));
// An empty database also exercises primary-shard metadata migration. No
// unsharded collection is created, so moveCollection is not applicable here.
assert.eq(null,config.databases.findOne({_id:prefix+'_scale'}));
assert.commandWorked(admin.runCommand({enableSharding:prefix+'_scale',primaryShard:'rs2'}));
assert.eq('rs2',config.databases.findOne({_id:prefix+'_scale'}).primary);
const donor=new Mongo('mongodb://mongodb-gate-sharded-rs2-0.mongodb-gate-sharded-rs2.mongodb-gate.svc.cluster.local:27017/admin?replicaSet=rs2&tls=true&tlsCAFile=/tls/ca.crt&serverSelectionTimeoutMS=10000');
// MongoDB 8 requires directShardOperations on direct shard commands. The
// Operator gives that role to clusterAdmin, not databaseAdmin. Use its existing
// collStats privilege; full index definitions and data are checked via mongos.
assert(donor.getDB('admin').auth({user:process.env.MONGODB_CLUSTER_ADMIN_USER,pwd:process.env.MONGODB_CLUSTER_ADMIN_PASSWORD}));
globalThis.mongodbGateStage='sharding-scale-donor-stats';
const copied=assert.commandWorked(donor.getDB(dbname).runCommand({collStats:'documents'}));
assert.eq(199,copied.count);
assert.eq(c.getDB(dbname).getCollection('documents').getIndexes().length,copied.nindexes);
print('MONGODB_SHARDING_SCALE_POPULATED_PASS chunks=2 donor=rs2');
"""
SCALE_DRAIN = SCALE_ADMIN + """
globalThis.mongodbGateStage='sharding-scale-drain';
const config=manager.getDB('config'),metadata=config.collections.findOne({_id:ns});
assert(metadata&&!metadata.dropped);
for(const [tenant,to] of [[-1,'rs0'],[1,'rs1']]){
 const chunk=config.chunks.findOne({uuid:metadata.uuid,$expr:{$and:[{$lte:['$min.tenant',tenant]},{$gt:['$max.tenant',tenant]}]}});
 assert(chunk);
 if(chunk.shard!==to)assert.commandWorked(admin.runCommand({moveChunk:ns,find:{tenant},to,_waitForDelete:true,maxTimeMS:60000}));
}
for(const name of [dbname,prefix+'_scale']){
 const entry=config.databases.findOne({_id:name});
 if(entry&&entry.primary==='rs2')assert.commandWorked(admin.runCommand({movePrimary:name,to:'rs0',maxTimeMS:60000}));
}
assert.eq(0,config.chunks.countDocuments({shard:'rs2'}));
assert.eq(0,config.databases.countDocuments({primary:'rs2'}));
// The Operator refuses any non-system donor database, including empty
// collections left by range migration. Only this run's now-empty databases
// may be dropped locally, after the catalog proves no remaining ownership.
const uri='mongodb://mongodb-gate-sharded-rs2-0.mongodb-gate-sharded-rs2.mongodb-gate.svc.cluster.local:27017/admin?replicaSet=rs2&tls=true&tlsCAFile=/tls/ca.crt&serverSelectionTimeoutMS=10000';
const donor=new Mongo(uri),da=donor.getDB('admin');
assert(da.auth({user:process.env.MONGODB_CLUSTER_ADMIN_USER,pwd:process.env.MONGODB_CLUSTER_ADMIN_PASSWORD}));
const system=['admin','config','local'];
const databases=assert.commandWorked(da.runCommand({listDatabases:1,nameOnly:true})).databases.filter(r=>!system.includes(r.name));
globalThis.mongodbGateStage='sharding-scale-donor-empty';
for(const row of databases){
 assert([dbname,prefix+'_scale'].includes(row.name));
 // dbStats returns BSON Int64 even for zero. Compare its exact decimal value,
 // without JSON.stringify's distinction between Long(0) and JavaScript 0.
 assert.eq('0',String(assert.commandWorked(donor.getDB(row.name).runCommand({dbStats:1})).objects));
}
globalThis.mongodbGateStage='sharding-scale-donor-drop';
for(const row of databases)assert.commandWorked(donor.getDB(row.name).runCommand({dropDatabase:1,writeConcern:{w:'majority',wtimeout:10000}}));
assert.eq(0,assert.commandWorked(da.runCommand({listDatabases:1,nameOnly:true})).databases.filter(r=>!system.includes(r.name)).length);
print('MONGODB_SHARDING_SCALE_DRAIN_PASS chunks=0 primary-databases=0 user-databases=0');
"""
SCALE_CLEAN = SHARD_CLEAN.replace("print('MONGODB_SHARDING_DATABASE_CLEAN_PASS');", """
assert.commandWorked(c.getDB(prefix+'_scale').runCommand({dropDatabase:1,writeConcern:{w:'majority',wtimeout:10000}}));
assert.eq(null,config.databases.findOne({_id:prefix+'_scale'}));
print('MONGODB_SHARDING_DATABASE_CLEAN_PASS');
""")


def candidate(name):
    return (name == NAME or name.startswith(NAME + '-') or name == 'internal-' + NAME + '-users' or
            name.startswith('mongod-data-' + NAME + '-'))


def owned(obj, run_id):
    meta = obj['metadata'];labels = meta.get('labels', {})
    if meta.get('namespace') != 'mongodb-gate' or not candidate(meta['name']):
        return False
    if all(labels.get(k) == v for k, v in LABELS.items()):
        return labels.get('kubeauto.io/test-run') == run_id
    if labels.get('app.kubernetes.io/instance') == NAME:
        return labels.get('app.kubernetes.io/managed-by') == 'percona-server-mongodb-operator'
    return (meta['name'] in (NAME, NAME + '-access', NAME + '-metrics') and
            labels.get('app.kubernetes.io/managed-by') == 'kubeauto' and
            labels.get('kubeauto.io/component') == 'mongodb')


class ShardingGate:
    def __init__(self, base):
        spec = importlib.util.spec_from_file_location('mongodb_sharding_scenarios', base / 'tests/helpers/mongodb-scenarios.py')
        module = importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        self.scenario = module.Scenario(base)
        self.lab, self.base, self.module = self.scenario.lab, base, module
        self.path = self.lab.directory / 'mongodb-sharding-owned.json'
        self.fault = self.lab.directory / 'mongodb-fault-sharding.json'
        self.directory = base / 'clusters' / DEPLOYMENT
        self.state = json.loads(self.path.read_text()) if self.path.exists() else None

    def save(self):
        self.scenario.fixture.write_private(self.path, self.state)

    def source_identity(self):
        source = self.scenario.fixture.NAME
        result = {}
        for kind in ('pods', 'pvc', 'secrets', 'psmdb'):
            result[kind] = [obj for obj in self.lab.get(kind, ns=self.scenario.ns)['items'] if
                obj['metadata']['name'] == source or obj['metadata']['name'].startswith(source + '-') or
                obj['metadata']['name'] == 'internal-' + source + '-users' or
                obj['metadata']['name'].startswith('mongod-data-' + source + '-')]
        return self.module.canonical_snapshot(result)

    def verify(self):
        owner = self.lab.verify()
        if (not self.state or owner['cluster_uid'] != self.state['cluster_uid'] or
                self.state.get('name') != NAME or self.state.get('deployment') != DEPLOYMENT or
                not re.fullmatch('[0-9a-f]{16}', str(self.state.get('run_id', ''))) or self.directory.is_symlink()):
            raise ValueError('MongoDB sharding journal does not match its leased deployment')
        if self.fault.exists() and json.loads(self.fault.read_text()) != {'run_id': self.state['run_id']}:
            raise ValueError('MongoDB sharding fault journal has a different owner')
        if self.source_identity() != self.state['source_identity']:
            raise ValueError('MongoDB sharding source identity changed')

    def capture(self):
        self.verify()
        known = {(r['kind'], r['name']): r for r in self.state['resources']}
        for kind in APIS:
            for obj in self.lab.get(kind, ns=self.scenario.ns)['items']:
                meta = obj['metadata'];name = meta['name']
                if not candidate(name):
                    continue
                if not owned(obj, self.state['run_id']):
                    raise ValueError('MongoDB sharding refuses a foreign target resource')
                previous = known.get((kind, name))
                if previous and previous['uid'] not in (None, meta['uid']):
                    parents = {r['uid'] for r in meta.get('ownerReferences', [])}
                    allowed = {r['uid'] for r in known.values() if r['kind'] in ('psmdb', 'statefulset')}
                    if kind not in ('pod', 'statefulset', 'pdb') or not parents.intersection(allowed):
                        raise ValueError('MongoDB sharding target resource identity changed')
                record = {'kind': kind, 'name': name, 'uid': meta['uid']}
                if kind == 'pvc':
                    if obj['spec'].get('storageClassName') != 'mongodb-longhorn':
                        raise ValueError('MongoDB sharding target has a foreign StorageClass')
                    record['volume'] = obj['spec'].get('volumeName')
                known[(kind, name)] = record
        self.state['resources'] = list(known.values());self.save()

    def product(self):
        result = subprocess.run(['kubecli', 'setup', DEPLOYMENT, '07', '--tags', 'mongodb'], cwd=self.base, timeout=5400)
        self.capture()
        if result.returncode:
            raise ValueError('MongoDB sharding product configuration failed')

    def health(self, shards=('rs0', 'rs1')):
        config = json.loads((self.directory / 'yml/mongodb/input.json').read_text())
        self.scenario.product.health(config, self.scenario.product.API(self.directory / 'kubectl.kubeconfig'))
        cr = self.lab.get('psmdb', NAME, self.scenario.ns)
        if (cr['spec']['sharding']['enabled'] is not True or
                [(rs['name'], rs['size']) for rs in cr['spec']['replsets']] != [(name, 3) for name in shards] or
                cr['spec']['sharding']['configsvrReplSet']['size'] != 3 or
                cr['spec']['sharding']['mongos']['size'] != 3):
            raise ValueError('MongoDB sharding topology differs from the two-shard matrix')
        claims = [r for r in self.state['resources'] if r['kind'] == 'pvc']
        source = self.state['source_identity']['pvc']
        expected_claims = 3 * (len(shards) + 1)
        if (len(claims) != expected_claims or len({r.get('volume') for r in claims}) != expected_claims or
                any(not r.get('volume') for r in claims) or
                {r['uid'] for r in claims}.intersection(r['uid'] for r in source) or
                {r.get('volume') for r in claims}.intersection(r['spec']['volumeName'] for r in source)):
            raise ValueError('MongoDB sharding requires distinct data volumes for every declared member')
        routers = [p for p in self.lab.get('pods', ns=self.scenario.ns)['items'] if
                   p['metadata'].get('labels', {}).get('app.kubernetes.io/instance') == NAME and
                   p['metadata'].get('labels', {}).get('app.kubernetes.io/component') == 'mongos']
        if len(routers) != 3 or len({p['spec'].get('nodeName') for p in routers}) != 3:
            raise ValueError('MongoDB sharding routers are not in three hostname failure domains')

    def client(self):
        if self.lab.get('pod', self.scenario.client, self.scenario.ns):
            raise ValueError('MongoDB sharding refuses an existing business client')
        self.state['client'] = {'run_id': self.scenario.prefix, 'uid': None};self.save()
        obj = self.scenario.fixture.client_object(NAME, sharded=True)
        obj['metadata']['labels']['kubeauto.io/test-run'] = self.scenario.prefix
        self.scenario.client_attempted = True
        result = subprocess.run([*self.lab.command, 'create', '-f', '-', '-o', 'json'],
            input=json.dumps(obj), capture_output=True, text=True, timeout=30)
        if result.returncode:
            raise ValueError('MongoDB sharding client creation failed')
        self.scenario.client_uid = json.loads(result.stdout)['metadata']['uid']
        self.state['client']['uid'] = self.scenario.client_uid;self.save()
        self.lab.run('-n', self.scenario.ns, 'wait', '--for=condition=Ready', 'pod/' + self.scenario.client,
                     '--timeout=180s', timeout=200)

    def install(self):
        owner = self.lab.verify();self.scenario.health()
        if (owner.get('healthy') is not True or self.state or self.directory.exists() or
                list(self.lab.directory.glob('*fault*.json'))):
            raise ValueError('MongoDB sharding install requires a healthy baseline and no target or pending fault')
        for kind in APIS:
            if any(candidate(o['metadata']['name']) for o in self.lab.get(kind, ns=self.scenario.ns)['items']):
                raise ValueError('MongoDB sharding refuses existing unjournaled target resources')
        self.state = {'name': NAME, 'deployment': DEPLOYMENT, 'cluster_uid': owner['cluster_uid'],
            'run_id': secrets.token_hex(8), 'source_identity': self.source_identity(), 'resources': [], 'healthy': False}
        self.save();self.scenario.fixture.write_private(self.fault, {'run_id': self.state['run_id']})
        try:
            result = subprocess.run(['kubecli', 'new', DEPLOYMENT], cwd=self.base, timeout=120)
            if result.returncode:
                raise ValueError('MongoDB sharding deployment creation failed')
            config = yaml.safe_load((self.lab.directory / 'config.yml').read_text())
            config.update(CLUSTER_NAME=DEPLOYMENT, mongodb_name=NAME, mongodb_sharding_enabled=True,
                mongodb_replsets=[{'name': 'rs0', 'size': 3}, {'name': 'rs1', 'size': 3}],
                mongodb_pvc_size='2Gi', mongodb_volume_expansion_enabled=False,
                mongodb_config_size=3, mongodb_mongos_size=3, mongodb_requests=[],
                mongodb_backup_enabled=False, mongodb_pitr_enabled=False,
                mongodb_users_secret=NAME+'-users', mongodb_tls_secret=NAME+'-tls',
                mongodb_tls_internal_secret=NAME+'-tls-internal', mongodb_encryption_key_secret=NAME+'-key',
                mongodb_users=[{'name': 'application', 'db': 'appdb',
                    'passwordSecretRef': {'name': NAME+'-app', 'key': 'password'},
                    'roles': [{'name': 'readWrite', 'db': 'appdb'}]}])
            path = self.directory / 'config.yml';path.write_text(yaml.safe_dump(config, sort_keys=False));path.chmod(0o600)
            self.directory.joinpath('hosts').write_text(self.scenario.fixture.inventory(DEPLOYMENT))
            self.directory.joinpath('hosts').chmod(0o600)
            shutil.copyfile(self.lab.directory/'kubectl.kubeconfig', self.directory/'kubectl.kubeconfig')
            self.directory.joinpath('kubectl.kubeconfig').chmod(0o600)
            for obj in self.scenario.fixture.credentials(NAME)['items']:
                obj['metadata']['labels'] = LABELS | {'kubeauto.io/test-run': self.state['run_id']}
                record = {'kind': 'secret', 'name': obj['metadata']['name'], 'uid': None}
                self.state['resources'].append(record);self.save()
                result = subprocess.run([*self.lab.command, 'create', '-f', '-', '-o', 'json'],
                    input=json.dumps(obj), capture_output=True, text=True, timeout=30)
                if result.returncode:
                    raise ValueError('MongoDB sharding credentials creation failed')
                record['uid'] = json.loads(result.stdout)['metadata']['uid'];self.save()
            self.product();self.health();self.client();self.scenario.workload_started = True
            self.scenario.js("assert.eq('isdbgrid',c.getDB('admin').runCommand({hello:1}).msg);assert.commandWorked(d.runCommand({insert:prefix+'_documents',documents:[{_id:'router',value:'majority'}],writeConcern:{w:'majority',j:true,wtimeout:10000}}));assert.eq('majority',d.getCollection(prefix+'_documents').findOne({_id:'router'}).value);print('MONGODB_SHARDING_ROUTER_BUSINESS_PASS');",
                             'MONGODB_SHARDING_ROUTER_BUSINESS_PASS', app=True)
            self.scenario.cleanup();self.verify();self.scenario.health();self.health()
            self.state.pop('client', None)
            self.state['healthy'] = True;self.save();self.fault.unlink()
        except BaseException:
            try:
                # The entire target is disposable; its recorded client can be
                # removed even when a failed TLS connection prevents data cleanup.
                self.cleanup()
            except (ValueError, OSError, subprocess.SubprocessError):
                print('MONGODB_SHARDING_RECOVERY_FAIL journal=preserved', file=sys.stderr)
            raise
        print('MONGODB_SHARDING_INSTALL_SCOPED_CLEAN_PASS retained=healthy-independent-sharded-baseline', flush=True)
        print('MONGODB_SHARDING_INSTALL_PASS', flush=True)

    def cleanup(self):
        if self.state is None:
            self.lab.verify()
            if self.directory.exists() or self.directory.is_symlink() or self.fault.exists():
                raise ValueError('MongoDB sharding cleanup refuses an unjournaled deployment')
            self.assert_target_absent()
            print('MONGODB_SHARDING_CLEAN_PASS target-volumes=absent mutation=none', flush=True)
            return
        self.capture()
        plan, volumes = [], set()
        # Validate the complete graph before the first destructive API request.
        for record in self.state['resources']:
            obj = self.lab.get(record['kind'], record['name'], self.scenario.ns)
            if obj and (not owned(obj, self.state['run_id']) or obj['metadata']['uid'] != record['uid']):
                raise ValueError('MongoDB sharding cleanup refuses changed ownership')
            if record['kind'] == 'pvc' and record.get('volume'):
                pv = self.lab.get('pv', record['volume'])
                if pv:
                    ref = pv['spec'].get('claimRef', {})
                    if (pv['spec'].get('storageClassName') != 'mongodb-longhorn' or
                            (ref.get('namespace'), ref.get('name'), ref.get('uid')) !=
                            (self.scenario.ns, record['name'], record['uid'])):
                        raise ValueError('MongoDB sharding cleanup refuses a rebound volume')
                volumes.add(record['volume'])
            if obj:
                plan.append(record)
        if self.state.get('quorum'):
            self.scenario.prefix = self.state['workload']
            self.recover_quorum(check_data=False)
        self.cleanup_client()
        for record in sorted(plan, key=lambda r: (r['kind'] != 'psmdb', r['kind'] == 'pvc')):
            kind, name = record['kind'], record['name']
            obj = self.lab.get(kind, name, self.scenario.ns)
            if obj is None:
                continue
            if obj['metadata']['uid'] != record['uid']:
                raise ValueError('MongoDB sharding target changed during cleanup')
            meta = obj['metadata']
            if kind == 'psmdb' and meta.get('deletionTimestamp'):
                finalizers = meta.get('finalizers', [])
                if finalizers:
                    self.resume_deletion(obj, record)
            api, plural = APIS[kind]
            body = {'apiVersion': 'v1', 'kind': 'DeleteOptions', 'preconditions': {'uid': record['uid']}}
            if not meta.get('deletionTimestamp'):
                result = subprocess.run([*self.lab.command, 'delete', '--raw',
                    api+'/namespaces/'+self.scenario.ns+'/'+plural+'/'+name, '-f', '-'],
                    input=json.dumps(body), capture_output=True, text=True, timeout=30)
                if result.returncode:
                    raise ValueError('MongoDB sharding scoped deletion failed')
            self.lab.run('-n', self.scenario.ns, 'wait', '--for=delete', kind+'/'+name, '--timeout=600s', timeout=620)
        for volume in volumes:
            self.lab.run('wait', '--for=delete', 'pv/'+volume, '--timeout=300s', timeout=320)
        self.assert_target_absent()
        if self.directory.exists():
            shutil.rmtree(self.directory)
        self.scenario.health();self.verify()
        self.path.unlink()
        if self.fault.exists():self.fault.unlink()
        self.state = None
        print('MONGODB_SHARDING_CLEAN_PASS target-volumes=absent source-preserved=true', flush=True)

    def data(self, failure=False):
        module = 'FAILURE' if failure else 'DATA'
        self.verify();self.capture();self.health();self.scenario.health()
        if not self.state.get('healthy') or list(self.lab.directory.glob('*fault*.json')):
            raise ValueError('MongoDB sharding data requires a healthy baseline without pending faults')
        self.state['workload'] = self.scenario.prefix;self.save()
        self.scenario.fixture.write_private(self.fault, {'run_id': self.state['run_id']})
        first_error = None
        try:
            self.client()
            # This is a normal administrative/business client on the product's
            # TLS mongos endpoint; the operator remains the topology owner.
            self.scenario.js(SHARD_LAYOUT, 'MONGODB_SHARDING_LAYOUT_PASS')
            before = self.scenario.js(SHARD_DATA, 'MONGODB_SHARDING_CRUD_TRANSACTION_PASS')
            if failure:
                self.losses()
            else:
                self.scenario.js(SHARD_MIGRATE, 'MONGODB_SHARDING_POPULATED_MIGRATION_PASS')
            after = self.scenario.js(FAULT_VERIFY if failure else SHARD_VERIFY,
                'MONGODB_SHARDING_POST_FAULT_PASS' if failure else 'MONGODB_SHARDING_POST_MIGRATION_PASS')
            digests = lambda text: [s for s in text.splitlines() if s.startswith('MONGODB_SHARDING_DATA_DIGEST ')]
            if len(digests(before)) != 1 or digests(before) != digests(after):
                raise ValueError('MongoDB sharding migration changed the routed cross-shard digest')
            print(digests(after)[0], flush=True)
        except BaseException as exc:
            first_error = exc
            raise
        finally:
            try:
                if self.state.get('quorum') or self.state.get('disruption'):
                    raise ValueError('MongoDB sharding fault recovery is incomplete; preserve client and workload')
                if self.scenario.client_uid is not None:
                    self.scenario.js(SHARD_CLEAN,
                                     'MONGODB_SHARDING_DATABASE_CLEAN_PASS')
                    self.scenario.delete_client()
                self.verify();self.health();self.scenario.health()
                self.state.pop('workload');self.save();self.fault.unlink()
                self.state.pop('client', None);self.save()
                print('MONGODB_SHARDING_'+module+'_SCOPED_CLEAN_PASS database=absent source-preserved=true', flush=True)
            except BaseException:
                print('MONGODB_SHARDING_'+module+'_RECOVERY_FAIL journal=preserved', file=sys.stderr)
                if first_error is None:raise
        print('MONGODB_SHARDING_'+module+'_PASS', flush=True)

    def scale_config(self, shards):
        path = self.directory / 'config.yml'
        config = yaml.safe_load(path.read_text())
        allowed = [[{'name': n, 'size': 3} for n in names] for names in
                   [('rs0', 'rs1'), ('rs0', 'rs1', 'rs2')]]
        original = yaml.safe_load(self.state['scale']['original_config'])
        if (config.get('mongodb_replsets') not in allowed or
                config | {'mongodb_replsets': original['mongodb_replsets']} != original):
            raise ValueError('MongoDB shard scaling refuses concurrent configuration edits')
        config['mongodb_replsets'] = [{'name': n, 'size': 3} for n in shards]
        path.write_text(yaml.safe_dump(config, sort_keys=False));path.chmod(0o600)
        self.product()

    def scale_volumes(self):
        """Retire only the drained shard's retained claims, after its STS is gone."""
        if not self.state.get('scale', {}).get('drained'):
            raise ValueError('MongoDB shard volume retirement requires a proven drained donor')
        prefix = NAME + '-rs2'
        if self.lab.get('statefulset', prefix, self.scenario.ns) or any(
                o['metadata']['name'].startswith(prefix + '-') for o in
                self.lab.get('pods', ns=self.scenario.ns)['items']):
            raise ValueError('MongoDB shard volume retirement refuses an active donor')
        claims = [r for r in self.state['resources'] if r['kind'] == 'pvc' and
                  r['name'].startswith('mongod-data-' + prefix + '-')]
        if len(claims) != 3:
            raise ValueError('MongoDB shard retirement requires exactly three journaled claims')
        for record in claims:
            obj = self.lab.get('pvc', record['name'], self.scenario.ns)
            if obj and (not owned(obj, self.state['run_id']) or obj['metadata']['uid'] != record['uid'] or
                        obj['spec'].get('volumeName') != record.get('volume') or
                        obj['spec'].get('storageClassName') != 'mongodb-longhorn'):
                raise ValueError('MongoDB shard retirement refuses a changed claim')
            if not record.get('volume'):
                raise ValueError('MongoDB shard retirement requires the bound volume identity')
            pv = self.lab.get('pv', record['volume'])
            if pv:
                ref = pv['spec'].get('claimRef', {})
                if (pv['spec'].get('storageClassName') != 'mongodb-longhorn' or
                        pv['spec'].get('persistentVolumeReclaimPolicy') != 'Delete' or
                        (ref.get('namespace'), ref.get('name'), ref.get('uid')) !=
                        (self.scenario.ns, record['name'], record['uid'])):
                    raise ValueError('MongoDB shard retirement refuses a rebound or retained volume')
        # Validate all three bindings before deleting the first claim.
        for record in claims:
            if self.lab.get('pvc', record['name'], self.scenario.ns):
                self.delete('pvc', record['name'], record['uid'])
            self.lab.run('-n', self.scenario.ns, 'wait', '--for=delete', 'pvc/' + record['name'], '--timeout=300s', timeout=320)
            self.lab.run('wait', '--for=delete', 'pv/' + record['volume'], '--timeout=300s', timeout=320)
            if self.lab.get('pvc', record['name'], self.scenario.ns) or self.lab.get('pv', record['volume']):
                raise ValueError('MongoDB shard retirement still has a claim or volume')
        self.state['resources'] = [r for r in self.state['resources'] if r not in claims];self.save()
        print('MONGODB_SHARDING_SCALE_VOLUMES_CLEAN_PASS retired-claims-pvs=absent', flush=True)

    def wait_scaled_donor(self):
        deadline = time.monotonic() + 600
        known = {(r['kind'], r['name']): r['uid'] for r in self.state['resources']}
        while True:
            objects = [('pod', p) for p in self.lab.get('pods', ns=self.scenario.ns)['items']
                       if p['metadata']['name'].startswith(NAME + '-rs2-')]
            sts = self.lab.get('statefulset', NAME + '-rs2', self.scenario.ns)
            if sts:objects.append(('statefulset', sts))
            for kind, obj in objects:
                meta = obj['metadata']
                if not owned(obj, self.state['run_id']) or known.get((kind, meta['name'])) != meta['uid']:
                    raise ValueError('MongoDB shard removal encountered a replaced donor workload')
            if not objects:return
            if time.monotonic() >= deadline:
                raise ValueError('MongoDB shard removal left an active donor workload')
            print('MONGODB_SHARDING_SCALE_HEARTBEAT stage=operator-removes-donor', flush=True)
            time.sleep(5)

    def scale(self):
        self.verify();self.capture();self.health();self.scenario.health()
        if (not self.state.get('healthy') or self.state.get('client') or self.state.get('scale') or
                list(self.lab.directory.glob('*fault*.json'))):
            raise ValueError('MongoDB shard scaling requires a clean healthy two-shard baseline')
        # A fresh fixture includes rs2 SANs. Refuse an older certificate before
        # publishing a topology that it cannot authenticate.
        import base64
        from cryptography import x509
        for suffix in ('tls', 'tls-internal'):
            obj = self.lab.get('secret', NAME + '-' + suffix, self.scenario.ns)
            cert = x509.load_pem_x509_certificate(base64.b64decode(obj['data']['tls.crt']))
            sans = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
            if '*.' + NAME + '-rs2.mongodb-gate.svc.cluster.local' not in sans.get_values_for_type(x509.DNSName):
                raise ValueError('MongoDB shard scaling requires the new shard DNS certificate before setup')
        self.state['workload'] = self.scenario.prefix
        self.state['scale'] = {'original_config': (self.directory / 'config.yml').read_text(), 'drained': False}
        self.save();self.scenario.fixture.write_private(self.fault, {'run_id': self.state['run_id']})
        first_error = None
        try:
            self.client()
            self.scenario.js(SHARD_LAYOUT, 'MONGODB_SHARDING_LAYOUT_PASS')
            before = self.scenario.js(SCALE_DATA, 'MONGODB_SHARDING_CRUD_TRANSACTION_PASS')
            self.scale_config(('rs0', 'rs1', 'rs2'));self.health(('rs0', 'rs1', 'rs2'))
            self.scenario.js(SCALE_LOAD, 'MONGODB_SHARDING_SCALE_POPULATED_PASS chunks=2 donor=rs2')
            loaded = self.scenario.js(SCALE_VERIFY, 'MONGODB_SHARDING_POST_MIGRATION_PASS')
            self.scenario.js(SCALE_DRAIN, 'MONGODB_SHARDING_SCALE_DRAIN_PASS chunks=0 primary-databases=0 user-databases=0')
            self.state['scale']['drained'] = True;self.save()
            self.scale_config(('rs0', 'rs1'))
            # listShards is checked through the authenticated TLS router before
            # any retained PVC is retired. The Operator owns removeShard/STS.
            after = self.scenario.js(SHARD_ADMIN + SCALE_VERIFY, 'MONGODB_SHARDING_POST_MIGRATION_PASS')
            digests = lambda output: [r for r in output.splitlines() if r.startswith(
                ('MONGODB_SHARDING_DATA_DIGEST ', 'MONGODB_SHARDING_INDEX_DIGEST '))]
            if len(digests(before)) != 2 or not (digests(before) == digests(loaded) == digests(after)):
                raise ValueError('MongoDB shard scaling changed the populated data or index boundary')
            for row in digests(after):print(row, flush=True)
            self.wait_scaled_donor();self.scale_volumes();self.health()
            self.scenario.js(SCALE_CLEAN, 'MONGODB_SHARDING_DATABASE_CLEAN_PASS')
            self.scenario.delete_client()
            (self.directory / 'config.yml').write_text(self.state['scale']['original_config'])
            self.state.pop('client', None);self.state.pop('workload');self.state.pop('scale')
            self.save();self.fault.unlink();self.verify();self.scenario.health()
        except BaseException as exc:
            first_error = exc
            directory = self.lab.directory / ('mongodb-sharding-scale-failure-' + self.state['run_id'])
            directory.mkdir(mode=0o700, exist_ok=True)
            try:self.lab.diagnostics(directory)
            except (ValueError, OSError, subprocess.SubprocessError):pass
            raise
        finally:
            if first_error is not None:
                # This whole sharded deployment is a disposable runner fixture.
                # Use its existing graph/UID guarded cleanup, preserving source
                # and backup services; never improvise a destructive shrink.
                try:self.cleanup()
                except BaseException:print('MONGODB_SHARDING_SCALE_RECOVERY_FAIL journal=preserved', file=sys.stderr)
        print('MONGODB_SHARDING_SCALE_SCOPED_CLEAN_PASS temporary-shard-volumes-data=absent source=preserved', flush=True)
        print('MONGODB_SHARDING_SCALE_PASS', flush=True)

    def wait_health(self, timeout=600):
        deadline = time.monotonic() + timeout
        while True:
            self.verify()
            try:
                self.health()
                return
            except (ValueError, subprocess.TimeoutExpired):
                if time.monotonic() >= deadline:
                    raise ValueError('MongoDB sharding fault recovery deadline exceeded') from None
                print('MONGODB_SHARDING_HEARTBEAT stage=role-recovery', flush=True)
                time.sleep(5)

    def members(self, role):
        labels = {'app.kubernetes.io/instance': NAME,
                  'app.kubernetes.io/component' if role == 'mongos' else 'app.kubernetes.io/replset': role}
        pods = [o for o in self.lab.get('pods', ns=self.scenario.ns)['items'] if
                all(o['metadata'].get('labels', {}).get(k) == v for k, v in labels.items())]
        if len(pods) != 3 or not all(self.scenario.product.ready(p) for p in pods):
            raise ValueError('MongoDB sharding fault requires three healthy owned role members')
        known = {r['name']: r['uid'] for r in self.state['resources'] if r['kind'] == 'pod'}
        if any(not owned(p, self.state['run_id']) or known.get(p['metadata']['name']) != p['metadata']['uid'] for p in pods):
            raise ValueError('MongoDB sharding role member identity differs from its journal')
        return sorted(pods, key=lambda p: p['metadata']['name'])

    def hello(self, pod, *, allow_starting=False):
        # Local, TLS-verified diagnostics continue while only this member's
        # network is isolated; no customer configuration is changed here.
        script = """try {
const c=new Mongo('mongodb://localhost:27017/admin?directConnection=true&tls=true&tlsCAFile=/etc/mongodb-ssl/ca.crt&serverSelectionTimeoutMS=2000');
const a=c.getDB('admin');
if(!a.auth({user:process.env.MONGODB_CLUSTER_ADMIN_USER,pwd:process.env.MONGODB_CLUSTER_ADMIN_PASSWORD}))throw Error('auth');
print(JSON.stringify(a.runCommand({hello:1})));
}catch(e){
if(!['MongoNetworkError','MongoNetworkTimeoutError','MongoServerSelectionError'].includes(e.name))throw e;
print(JSON.stringify({ok:0,starting:true}));
}"""
        output = self.lab.run('-n', self.scenario.ns, 'exec', pod, '-c', 'mongod', '--',
            'mongosh', '--quiet', '--norc', '--nodb', '--eval', script, timeout=30)
        state = json.loads(output.splitlines()[-1])
        if state.get('ok') != 1 and not (allow_starting and state == {'ok': 0, 'starting': True}):
            raise ValueError('MongoDB sharding member hello failed')
        return state

    def delete(self, kind, name, uid):
        api, plural = APIS[kind]
        result = subprocess.run([*self.lab.command, 'delete', '--raw',
            api+'/namespaces/'+self.scenario.ns+'/'+plural+'/'+name, '-f', '-'],
            input=json.dumps({'apiVersion': 'v1', 'kind': 'DeleteOptions', 'preconditions': {'uid': uid}}),
            capture_output=True, text=True, timeout=30)
        if result.returncode:
            raise ValueError('MongoDB sharding UID-bound fault deletion failed')

    def role_loss(self, role):
        self.wait_health();pods = self.members(role)
        if role == 'mongos':
            victim = pods[0]
        else:
            primaries = [p for p in pods if self.hello(p['metadata']['name']).get('isWritablePrimary') is True]
            if len(primaries) != 1:
                raise ValueError('MongoDB sharding fault cannot prove exactly one role primary')
            victim = primaries[0]
        meta = victim['metadata']
        self.state['disruption'] = {'role': role, 'pod': meta['name'], 'uid': meta['uid']};self.save()
        first_error = None
        try:
            self.verify()
            self.delete('pod', meta['name'], meta['uid'])
            self.wait_health()
            current = self.lab.get('pod', meta['name'], self.scenario.ns)
            if not current or current['metadata']['uid'] == meta['uid']:
                raise ValueError('MongoDB sharding lost role was not replaced')
            self.scenario.js(FAULT_VERIFY, 'MONGODB_SHARDING_POST_FAULT_PASS')
            print('MONGODB_SHARDING_ROLE_LOSS_PASS role='+role+' replacement-uid=verified digest=verified', flush=True)
        except BaseException as exc:
            first_error = exc
            raise
        finally:
            try:
                self.wait_health();self.capture()
                self.state.pop('disruption');self.save()
            except BaseException:
                print('MONGODB_SHARDING_ROLE_RECOVERY_FAIL journal=preserved', file=sys.stderr)
                if first_error is None:raise

    def patch(self, kind, name, uid, operations):
        self.lab.run('-n', self.scenario.ns, 'patch', kind, name, '--type=json', '-p', json.dumps([
            {'op': 'test', 'path': '/metadata/uid', 'value': uid}, *operations]))

    def quorum_loss(self):
        self.wait_health();pods = self.members('rs0')
        policies = self.lab.get('networkpolicy', ns=self.scenario.ns)['items']
        allowed = {NAME+'-access', self.scenario.fixture.NAME+'-access'}
        if {p['metadata']['name'] for p in policies} != allowed or not all(self.scenario.product.owned(p) for p in policies):
            raise ValueError('MongoDB sharding quorum refuses additional or foreign network policies')
        policy = next(p for p in policies if p['metadata']['name'] == NAME+'-access')
        key = 'kubeauto.io/mongodb-quorum-fault'
        if any(key in p['metadata'].get('labels', {}) for p in pods):
            raise ValueError('MongoDB sharding quorum refuses existing fault labels')
        modified, deny = self.module.quorum_policies(policy, self.scenario.prefix)
        # A restarting shard must read clusterID/global settings from cfg before
        # it listens. Keep that control path and DNS; no rs0 peer is reachable.
        deny['spec']['egress'] = [
            {'to': [{'podSelector': {'matchLabels': {
                'app.kubernetes.io/instance': NAME, 'app.kubernetes.io/replset': 'cfg'}}}],
             'ports': [{'port': 27017, 'protocol': 'TCP'}]},
            {'ports': [{'port': 53, 'protocol': 'UDP'}, {'port': 53, 'protocol': 'TCP'}]}]
        state = {'policy_uid': policy['metadata']['uid'], 'original_spec': policy['spec'], 'modified_spec': modified,
                 'members': [{'name': p['metadata']['name'], 'uid': p['metadata']['uid']} for p in pods[:2]],
                 'deny': deny['metadata']['name'], 'deny_spec': deny['spec']}
        self.state['quorum'] = state;self.save()
        first_error = None
        try:
            self.verify()
            result = subprocess.run([*self.lab.command, 'create', '-f', '-', '-o', 'json'],
                input=json.dumps(deny), capture_output=True, text=True, timeout=30)
            if result.returncode:
                raise ValueError('MongoDB sharding quorum deny creation failed')
            state['deny_uid'] = json.loads(result.stdout)['metadata']['uid'];self.save()
            self.patch('networkpolicy', policy['metadata']['name'], state['policy_uid'], [
                {'op': 'test', 'path': '/spec', 'value': policy['spec']}, {'op': 'replace', 'path': '/spec', 'value': modified}])
            for member in state['members']:
                self.patch('pod', member['name'], member['uid'], [
                    {'op': 'add', 'path': '/metadata/labels/kubeauto.io~1mongodb-quorum-fault', 'value': self.scenario.prefix}])
            self.scenario.restart_isolated_members(state['members'])
            deadline = time.monotonic()+90
            while True:
                states = [self.hello(p['metadata']['name'], allow_starting=True) for p in pods]
                state['observations'] = [{'pod': p['metadata']['name'], 'ok': s.get('ok'),
                    'isWritablePrimary': s.get('isWritablePrimary', False), 'starting': s.get('starting', False)}
                    for p, s in zip(pods, states)];self.save()
                if all(s.get('ok') == 1 and not s.get('isWritablePrimary', False) for s in states):break
                if time.monotonic() >= deadline:
                    raise ValueError('MongoDB sharding majority loss left a writable primary')
                print('MONGODB_SHARDING_HEARTBEAT stage=primary-demotion', flush=True);time.sleep(3)
            self.scenario.js("""
globalThis.mongodbGateStage='sharding-quorum';
const uri=process.env.MONGODB_URI.replace('serverSelectionTimeoutMS=10000','serverSelectionTimeoutMS=3000&socketTimeoutMS=6000');
const start=Date.now();let refused=false;
const unavailable=[6,7,50,64,89,91,133,189,11600,11602,13435,13436];
try{
 const probe=new Mongo(uri),a=probe.getDB('admin');
 assert(a.auth({user:process.env.MONGODB_DATABASE_ADMIN_USER,pwd:process.env.MONGODB_DATABASE_ADMIN_PASSWORD}));
 const r=probe.getDB(prefix+'_sharded').runCommand({update:'documents',updates:[{q:{tenant:-1},u:{$set:{quorumProbe:prefix}}}],
  maxTimeMS:3000,writeConcern:{w:'majority',j:true,wtimeout:2000}});
 if(r.ok!==1){assert(unavailable.includes(r.code));refused=true;}
 else if(r.writeConcernError){assert(unavailable.includes(r.writeConcernError.code));refused=true;}
}catch(e){assert(['MongoServerSelectionError','MongoNetworkTimeoutError','MongoNetworkError'].includes(e.name)||
 (['MongoServerError','MongoWriteConcernError'].includes(e.name)&&unavailable.includes(e.code)));refused=true;}
assert(refused);assert(Date.now()-start<15000);
print('MONGODB_SHARDING_QUORUM_REFUSAL_PASS majority-write=refused');
""", 'MONGODB_SHARDING_QUORUM_REFUSAL_PASS majority-write=refused', authenticate=False)
        except BaseException as exc:
            first_error = exc
            raise
        finally:
            try:
                self.recover_quorum()
            except BaseException:
                print('MONGODB_SHARDING_QUORUM_RECOVERY_FAIL journal=preserved', file=sys.stderr)
                if first_error is None:raise

    def recover_quorum(self, check_data=True):
        self.verify();state = self.state['quorum']
        policy = self.lab.get('networkpolicy', NAME+'-access', self.scenario.ns)
        if (not policy or not self.scenario.product.owned(policy) or policy['metadata']['uid'] != state['policy_uid'] or
                policy['spec'] not in (state['original_spec'], state['modified_spec'])):
            raise ValueError('MongoDB sharding quorum policy identity changed')
        labelled = []
        for member in state['members']:
            current = self.lab.get('pod', member['name'], self.scenario.ns)
            if not current or current['metadata']['uid'] != member['uid']:
                raise ValueError('MongoDB sharding quorum member identity changed')
            if key_value := current['metadata'].get('labels', {}).get('kubeauto.io/mongodb-quorum-fault'):
                if key_value != self.scenario.prefix:
                    raise ValueError('MongoDB sharding quorum member fault owner changed')
                labelled.append(member)
        deny = self.lab.get('networkpolicy', state['deny'], self.scenario.ns)
        if deny:
            meta = deny['metadata']
            if (meta.get('labels', {}).get('kubeauto.io/test-run') != self.scenario.prefix or
                    state.get('deny_uid') not in (None, meta['uid']) or
                    {'ingress': [], 'egress': [], **deny['spec']} !=
                    {'ingress': [], 'egress': [], **state['deny_spec']}):
                raise ValueError('MongoDB sharding quorum deny identity changed')
        # The complete policy/member/deny graph is validated before mutation.
        if policy['spec'] != state['original_spec']:
            self.patch('networkpolicy', NAME+'-access', state['policy_uid'], [
                {'op': 'test', 'path': '/spec', 'value': state['modified_spec']},
                {'op': 'replace', 'path': '/spec', 'value': state['original_spec']}])
        for member in labelled:
            self.patch('pod', member['name'], member['uid'], [
                {'op': 'test', 'path': '/metadata/labels/kubeauto.io~1mongodb-quorum-fault', 'value': self.scenario.prefix},
                {'op': 'remove', 'path': '/metadata/labels/kubeauto.io~1mongodb-quorum-fault'}])
        if deny:
            meta = deny['metadata']
            self.delete('networkpolicy', meta['name'], meta['uid'])
            if self.lab.get('networkpolicy', meta['name'], self.scenario.ns):
                raise ValueError('MongoDB sharding quorum deny cleanup incomplete')
        self.wait_health()
        if check_data:
            self.scenario.js("""
const d=c.getDB(prefix+'_sharded'),row=d.documents.findOne({tenant:-1});
assert(row&&(row.quorumProbe===undefined||row.quorumProbe===prefix));
assert.commandWorked(d.runCommand({update:'documents',updates:[{q:{tenant:-1},u:{$unset:{quorumProbe:''}}}],
 writeConcern:{w:'majority',j:true,wtimeout:10000}}));
print('MONGODB_SHARDING_QUORUM_PROBE_RECONCILED_PASS');
""", 'MONGODB_SHARDING_QUORUM_PROBE_RECONCILED_PASS')
            self.scenario.js(FAULT_VERIFY, 'MONGODB_SHARDING_POST_FAULT_PASS')
        self.state.pop('quorum');self.save()
        print('MONGODB_SHARDING_QUORUM_RECOVERY_PASS policy=restored members=healthy data-check='+str(check_data).lower(), flush=True)

    def losses(self):
        for role in ('rs0', 'cfg', 'mongos'):
            self.role_loss(role)
        self.quorum_loss()

    def recover(self):
        """Restore only a journaled workload/fault, retaining both healthy clusters."""
        self.verify();self.capture()
        workload = self.state.get('workload', '')
        record = self.state.get('client', {})
        if (not self.fault.exists() or not re.fullmatch(r'mongodb_gate_[0-9a-f]{16}', workload) or
                record.get('run_id') != workload or not record.get('uid') or self.state.get('disruption')):
            raise ValueError('MongoDB sharding recovery requires its recorded workload and client')
        self.restore_client()
        if self.scenario.client_uid is None:
            raise ValueError('MongoDB sharding recovery cannot find its original client')
        if self.state.get('quorum'):
            self.recover_quorum(check_data=False)
        self.wait_health()
        self.scenario.js(FAULT_VERIFY, 'MONGODB_SHARDING_POST_FAULT_PASS')
        self.scenario.js(SHARD_CLEAN, 'MONGODB_SHARDING_DATABASE_CLEAN_PASS')
        self.scenario.delete_client()
        self.verify();self.health();self.scenario.health()
        self.state.pop('workload');self.state.pop('client');self.save();self.fault.unlink()
        print('MONGODB_SHARDING_RECOVER_SCOPED_CLEAN_PASS baseline=healthy database=absent source-preserved=true', flush=True)
        print('MONGODB_SHARDING_RECOVER_PASS', flush=True)

    def cleanup_client(self):
        self.restore_client()
        if self.scenario.client_uid is not None:
            self.scenario.delete_client()

    def restore_client(self):
        """Validate the original client before using or deleting it in another process."""
        current = self.lab.get('pod', self.scenario.client, self.scenario.ns)
        record = self.state.get('client')
        if not current:
            return
        # A pre-journal failed run requires explicit evidence from its original
        # diagnostic observation, never adoption of the current same-name Pod.
        recovery = os.environ.get('MONGODB_SHARDING_RECOVERY_CLIENT', '')
        if record is None and recovery:
            if not re.fullmatch(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12},mongodb_gate_[0-9a-f]{16}', recovery):
                raise ValueError('MongoDB sharding client recovery evidence is invalid')
            uid, run_id = recovery.split(',')
            record = {'uid': uid, 'run_id': run_id}
        if (not record or not re.fullmatch(r'mongodb_gate_[0-9a-f]{16}', record['run_id']) or
                current['metadata'].get('labels', {}).get('kubeauto.io/test-run') != record['run_id'] or
                record['uid'] not in (None, current['metadata']['uid'])):
            raise ValueError('MongoDB sharding client recovery refuses unrecorded or changed identity')
        expected = self.scenario.fixture.client_object(NAME, sharded=True)['spec']
        actual = current['spec']
        ca = next((v.get('secret', {}) for v in actual.get('volumes', []) if v['name'] == 'ca'), {})
        expected_ca = expected['volumes'][0]['secret']
        if (actual['containers'][0].get('env') != expected['containers'][0]['env'] or
                actual['containers'][0].get('envFrom') != expected['containers'][0]['envFrom'] or
                any(ca.get(key) != expected_ca[key] for key in ('secretName', 'items'))):
            raise ValueError('MongoDB sharding client recovery refuses another target')
        self.scenario.prefix = record['run_id'];self.scenario.client_uid = current['metadata']['uid']

    def assert_target_absent(self):
        for kind in APIS:
            if kind == 'psmdb' and self.lab.get('crd', 'perconaservermongodbs.psmdb.percona.com') is None:
                continue
            if any(candidate(o['metadata']['name']) for o in self.lab.get(kind, ns=self.scenario.ns)['items']):
                raise ValueError('MongoDB sharding target resources remain after cleanup')
        for pv in self.lab.get('pv')['items']:
            ref = pv['spec'].get('claimRef', {})
            if ref.get('namespace') == self.scenario.ns and candidate(ref.get('name', '')):
                raise ValueError('MongoDB sharding target volume remains after cleanup')

    def resume_deletion(self, obj, record):
        """After complete cleanup preflight, resume only the disposable CR deletion."""
        self.verify()
        meta = obj['metadata'];ordered = 'percona.com/delete-psmdb-pods-in-order'
        if (not meta.get('deletionTimestamp') or meta['name'] != NAME or
                not owned(obj, self.state['run_id']) or meta['uid'] != record['uid'] or
                meta.get('finalizers') != [ordered]):
            raise ValueError('MongoDB sharding finalizer recovery refuses changed ownership or unknown finalizers')
        patch = [{'op': 'test', 'path': '/metadata/uid', 'value': meta['uid']},
                 {'op': 'test', 'path': '/metadata/resourceVersion', 'value': meta['resourceVersion']},
                 {'op': 'test', 'path': '/metadata/finalizers', 'value': [ordered]},
                 {'op': 'remove', 'path': '/metadata/finalizers/0'}]
        result = subprocess.run([*self.lab.command, '-n', self.scenario.ns, 'patch', 'psmdb', NAME,
            '--type=json', '--patch-file=/dev/stdin'], input=json.dumps(patch), capture_output=True, text=True, timeout=30)
        if result.returncode:
            raise ValueError('MongoDB sharding ordered deletion recovery failed')
        print('MONGODB_SHARDING_DELETION_RESUMED disposable-target=true source-preserved=true', flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=('install', 'data', 'failure', 'scale', 'recover', 'cleanup'))
    parser.add_argument('--base', type=Path, default=Path('/usr/local/kubeauto'))
    args = parser.parse_args()
    def interrupted(signum, frame):raise SystemExit(128 + signum)
    signal.signal(signal.SIGTERM, interrupted)
    try:
        gate = ShardingGate(args.base)
        {'install': gate.install, 'data': gate.data, 'failure': lambda: gate.data(failure=True), 'scale': gate.scale,
         'recover': gate.recover, 'cleanup': gate.cleanup}[args.mode]()
    except (ValueError, OSError, KeyError, subprocess.SubprocessError) as exc:
        print('MONGODB_SHARDING_FAIL reason=' + str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
