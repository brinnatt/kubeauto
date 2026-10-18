#!/usr/bin/env python3
"""Focused business assertions on the product-created MongoDB baseline."""
import argparse
import base64
import copy
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import re
import secrets
import shutil
import selectors
import signal
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlencode

import yaml


ASSERT = """const assert=(value)=>{if(!value)throw Error('business assertion failed');};
assert.eq=(expected,actual)=>assert(JSON.stringify(expected)===JSON.stringify(actual));
assert.commandWorked=(r)=>{assert(r.ok===1&&!r.writeConcernError&&!(r.writeErrors&&r.writeErrors.length));return r;};
"""
AUTH = """const c=new Mongo(process.env.MONGODB_URI),a=c.getDB('admin');
assert(a.auth({user:process.env.MONGODB_DATABASE_ADMIN_USER,pwd:process.env.MONGODB_DATABASE_ADMIN_PASSWORD}));
"""
APP_AUTH = """const c=new Mongo(process.env.MONGODB_URI),d=c.getDB('appdb');
assert(d.auth({user:'application',pwd:process.env.APP_PASSWORD}));
"""
DATA = """
const col=d.getCollection(prefix+'_documents');
assert.commandWorked(d.runCommand({create:col.getName()}));
assert.commandWorked(d.runCommand({createIndexes:col.getName(),indexes:[{key:{businessId:1},name:'business_unique',unique:true}]}));
assert.commandWorked(d.runCommand({createIndexes:col.getName(),indexes:[{key:{amount:1,businessId:1},name:'amount_business'}]}));
const docs=Array.from({length:200},(_,i)=>({_id:i,businessId:'order-'+i,amount:i%10,owner:prefix,payload:'x'.repeat(1024)}));
assert.commandWorked(d.runCommand({insert:col.getName(),documents:docs,writeConcern:{w:'majority',wtimeout:10000}}));
assert.eq(200,col.countDocuments({owner:prefix}));
assert.eq(900,col.aggregate([{$group:{_id:null,total:{$sum:'$amount'}}}]).toArray()[0].total);
const duplicate=d.runCommand({insert:col.getName(),documents:[{_id:201,businessId:'order-1'}],writeConcern:{w:'majority',wtimeout:10000}});
assert.eq(11000,duplicate.writeErrors[0].code);
assert.commandWorked(d.runCommand({update:col.getName(),updates:[{q:{_id:42},u:{$set:{amount:99}}}],writeConcern:{w:'majority',wtimeout:10000}}));
assert.eq(99,col.findOne({_id:42}).amount);
assert.commandWorked(d.runCommand({delete:col.getName(),deletes:[{q:{_id:199},limit:1}],writeConcern:{w:'majority',wtimeout:10000}}));
assert.eq(199,col.countDocuments({owner:prefix}));
assert.eq(1,col.getIndexes().filter(i=>i.name==='business_unique'&&i.unique).length);
assert.eq(20,col.find({amount:5}).hint('amount_business').itcount());
const types=d.getCollection(prefix+'_types');
assert.commandWorked(d.runCommand({insert:types.getName(),documents:[{_id:'bson',when:ISODate('2026-01-01T00:00:00Z'),
 price:NumberDecimal('19.95'),large:NumberLong('9007199254740993'),oid:ObjectId('0123456789abcdef01234567'),bytes:BinData(0,'YWJj')}],
 writeConcern:{w:'majority',j:true,wtimeout:10000}}));
const stored=types.findOne({_id:'bson'});
assert.eq('2026-01-01T00:00:00.000Z',stored.when.toISOString());
assert.eq('19.95',stored.price.toString());assert.eq('9007199254740993',stored.large.toString());
assert.eq('0123456789abcdef01234567',stored.oid.toHexString());
assert.eq({when:'date',price:'decimal',large:'long',oid:'objectId',bytes:'binData'},types.aggregate([
 {$project:{_id:0,when:{$type:'$when'},price:{$type:'$price'},large:{$type:'$large'},oid:{$type:'$oid'},bytes:{$type:'$bytes'}}}]).toArray()[0]);
const rows=col.find({owner:prefix}).sort({_id:1}).toArray().map(r=>({_id:r._id,businessId:r.businessId,amount:r.amount,payload:r.payload}));
const expected=docs.filter(r=>r._id!==199).map(r=>({_id:r._id,businessId:r.businessId,amount:r._id===42?99:r.amount,payload:r.payload}));
const digest=x=>require('crypto').createHash('sha256').update(JSON.stringify(x)).digest('hex');
assert.eq(digest(expected),digest(rows));
const ttl=d.getCollection(prefix+'_ttl');
assert.commandWorked(d.runCommand({create:ttl.getName()}));
assert.commandWorked(d.runCommand({createIndexes:ttl.getName(),indexes:[{key:{expireAt:1},name:'expiry',expireAfterSeconds:0}]}));
assert.commandWorked(d.runCommand({insert:ttl.getName(),documents:[{_id:'expired',expireAt:ISODate('2000-01-01T00:00:00Z')}],writeConcern:{w:'majority',j:true,wtimeout:10000}}));
const deadline=Date.now()+90000;
while(ttl.countDocuments({})!==0&&Date.now()<deadline){sleep(1000);}
assert.eq(0,ttl.countDocuments({}));
print('MONGODB_DATA_DIGEST sha256='+digest(rows));
print('MONGODB_DATA_ASSERTIONS_PASS documents=199 bson=true indexes=true ttl=true digest=true');
"""
TRANSACTIONS = """
globalThis.mongodbGateStage='transaction-create';
const accounts=prefix+'_accounts',ledger=prefix+'_ledger';
assert.commandWorked(d.runCommand({create:accounts}));
assert.commandWorked(d.runCommand({create:ledger}));
assert.commandWorked(d.runCommand({insert:accounts,documents:[{_id:'a',balance:1000},{_id:'b',balance:1000}],writeConcern:{w:'majority',wtimeout:10000}}));
const session=c.startSession(),sd=session.getDatabase('appdb');
try {
 globalThis.mongodbGateStage='transaction-commit';
 session.startTransaction({readConcern:{level:'snapshot'},writeConcern:{w:'majority',j:true,wtimeout:10000}});
 assert.commandWorked(sd.runCommand({update:accounts,updates:[{q:{_id:'a'},u:{$inc:{balance:-100}}},{q:{_id:'b'},u:{$inc:{balance:100}}}]}));
 assert.commandWorked(sd.runCommand({insert:ledger,documents:[{_id:'committed',amount:100}]}));
 session.commitTransaction();
 assert.eq(900,d.getCollection(accounts).findOne({_id:'a'}).balance);
 assert.eq(1100,d.getCollection(accounts).findOne({_id:'b'}).balance);
 assert.eq(1,d.getCollection(ledger).countDocuments({}));
 globalThis.mongodbGateStage='transaction-abort';
 session.startTransaction({readConcern:{level:'snapshot'},writeConcern:{w:'majority',j:true,wtimeout:10000}});
 assert.commandWorked(sd.runCommand({update:accounts,updates:[{q:{_id:'a'},u:{$inc:{balance:-500}}}]}));
 assert.commandWorked(sd.runCommand({insert:ledger,documents:[{_id:'aborted',amount:500}]}));
 session.abortTransaction();
 assert.eq(900,d.getCollection(accounts).findOne({_id:'a'}).balance);
 assert.eq(null,d.getCollection(ledger).findOne({_id:'aborted'}));
 assert.eq(2000,d.getCollection(accounts).aggregate([{$group:{_id:null,total:{$sum:'$balance'}}}]).toArray()[0].total);
 globalThis.mongodbGateStage='transaction-snapshot';
 session.startTransaction({readConcern:{level:'snapshot'},writeConcern:{w:'majority',j:true,wtimeout:10000}});
 assert.eq(900,sd.getCollection(accounts).findOne({_id:'a'}).balance);
 assert.commandWorked(d.runCommand({update:accounts,updates:[{q:{_id:'a'},u:{$inc:{balance:1}}}],writeConcern:{w:'majority',j:true,wtimeout:10000}}));
 assert.eq(900,sd.getCollection(accounts).findOne({_id:'a'}).balance);
 session.abortTransaction();
 assert.eq(901,d.getCollection(accounts).findOne({_id:'a'}).balance);
 globalThis.mongodbGateStage='transaction-write-concern';
 const start=Date.now();
 let refused=false;
 try {d.runCommand({insert:ledger,documents:[{_id:'unsatisfied-ack'}],writeConcern:{w:4,j:true,wtimeout:1000}});}
 catch(e){assert.eq('MongoWriteConcernError',e.name);assert.eq(100,e.code);refused=true;}
 assert(refused);
 assert(Date.now()-start<5000);
} finally {session.endSession();}
print('MONGODB_TRANSACTIONS_ASSERTIONS_PASS commit=true abort=true snapshot=true journal=true bounded-refusal=true');
"""

JS_ERROR_NAMES = frozenset(('Error', 'TypeError', 'ReferenceError', 'SyntaxError',
    'MongoServerError', 'MongoWriteConcernError', 'MongoServerSelectionError',
    'MongoInvalidArgumentError', 'MongoNetworkError', 'MongoBulkWriteError'))
JS_STAGES = frozenset(('setup', 'transaction-create', 'transaction-commit', 'transaction-abort',
                       'transaction-snapshot', 'transaction-write-concern', 'security-privileges',
                       'security-password', 'security-ca', 'security-plaintext', 'security-hostname', 'maintenance', 'performance',
                       'sharding-create', 'sharding-crud', 'sharding-distributed-commit',
                       'sharding-distributed-abort', 'sharding-populated-migration', 'sharding-quorum',
                       'sharding-scale-populate', 'sharding-scale-drain',
                       'sharding-scale-donor-stats', 'sharding-scale-donor-empty', 'sharding-scale-donor-drop'))


def client_error(output):
    """Export controlled stage identifiers and numeric locations, never messages."""
    for line in reversed(output.splitlines()):
        if not line.startswith('MONGODB_CLIENT_ERROR '):
            continue
        try:
            details = json.loads(line.removeprefix('MONGODB_CLIENT_ERROR '))
        except ValueError:
            continue
        if not isinstance(details, dict):
            continue
        stage, name = details.get('stage'), details.get('name')
        if not isinstance(stage, str) or not isinstance(name, str) or stage not in JS_STAGES or name not in JS_ERROR_NAMES:
            continue
        suffix = ' stage=' + stage + ' error=' + name
        for field in ('code', 'line'):
            value = details.get(field)
            if type(value) is int and 0 <= value <= 10**9:
                suffix += ' ' + field + '=' + str(value)
        return suffix
    return ''
SECURITY = """
const col=prefix+'_documents';
assert.commandWorked(d.runCommand({insert:col,documents:[{_id:'authorized'}],writeConcern:{w:'majority',j:true,wtimeout:10000}}));
globalThis.mongodbGateStage='security-privileges';
function unauthorized(run){
 let rejected=false;
 try {run();}catch(e){assert.eq('MongoServerError',e.name);assert.eq(13,e.code);rejected=true;}
 assert(rejected);
}
unauthorized(()=>c.getDB('admin').runCommand({replSetGetStatus:1}));
unauthorized(()=>c.getDB('forbidden_database').runCommand({find:'private'}));
const anonymous=new Mongo(process.env.MONGODB_URI);
unauthorized(()=>anonymous.getDB('appdb').runCommand({find:col}));
globalThis.mongodbGateStage='security-password';
const wrongPassword=new Mongo(process.env.MONGODB_URI);
let passwordRefused=false;
try {passwordRefused=!wrongPassword.getDB('appdb').auth({user:'application',pwd:'invalid-'+prefix});}
catch(e){assert.eq(18,e.code);passwordRefused=true;}
assert(passwordRefused);
const short=process.env.MONGODB_URI.replace('serverSelectionTimeoutMS=10000','serverSelectionTimeoutMS=3000');
assert(short.includes('&tlsCAFile=/tls/ca.crt'));
function refused(uri){
 let rejected=false;
 try {new Mongo(uri).getDB('admin').runCommand({ping:1});}
 catch(e){assert(e.name!=='MongoParseError');rejected=true;}
 assert(rejected);
}
globalThis.mongodbGateStage='security-ca';
refused(short.replace('&tlsCAFile=/tls/ca.crt',''));
globalThis.mongodbGateStage='security-plaintext';
refused(short.replace('tls=true','tls=false').replace('&tlsCAFile=/tls/ca.crt',''));
globalThis.mongodbGateStage='security-hostname';
refused(mismatchedUri);
assert.eq('authorized',d.getCollection(col).findOne({_id:'authorized'})._id);
print('MONGODB_SECURITY_ASSERTIONS_PASS ca=true hostname=true plaintext-refused=true password=true least-privilege=true');
"""
FAILOVER_WORKER = """
const col=prefix+'_documents';
assert.commandWorked(d.runCommand({delete:col,deletes:[{q:{owner:prefix},limit:0}],writeConcern:{w:'majority',j:true,wtimeout:10000}}));
assert.commandWorked(d.runCommand({insert:col,documents:[{_id:'seed',owner:prefix,value:'acknowledged-before-fault'}],writeConcern:{w:'majority',j:true,wtimeout:10000}}));
print('MONGODB_FAULT_WORKER_READY '+JSON.stringify({primary:c.getDB('admin').runCommand({hello:1}).primary}));
let count=0,errors=0;const deadline=Date.now()+150000;
while(count<200&&Date.now()<deadline){
 if(require('fs').existsSync('/tmp/'+prefix+'.stop'))throw Error('worker interrupted');
 try {
  assert.commandWorked(d.runCommand({update:col,updates:[{q:{_id:'ack-'+count},u:{$set:{owner:prefix,value:count}},upsert:true}],writeConcern:{w:'majority',j:true,wtimeout:3000}}));
  print('MONGODB_ACK '+count);count++;
 }catch(e){errors++;}
 sleep(100);
}
assert.eq(200,count);print('MONGODB_FAULT_WORKER_PASS '+JSON.stringify({acknowledged:count,retryErrors:errors}));
"""

MAINTENANCE_WORKER = """
const col=prefix+'_documents',fs=require('fs'),stop='/tmp/'+prefix+'.stop';
assert.commandWorked(d.runCommand({insert:col,documents:[{_id:'seed',owner:prefix,value:'maintenance-seed'}],writeConcern:{w:'majority',j:true,wtimeout:10000}}));
print('MONGODB_MAINTENANCE_WORKER_READY');
let count=0,errors=0;const deadline=Date.now()+2100000;
const transient=new Set([6,7,50,64,89,91,100,189,262,9001,10058,10107,11600,11602,13435,13436]);
while(!fs.existsSync(stop)&&Date.now()<deadline){
 try{
  assert.commandWorked(d.runCommand({update:col,updates:[{q:{_id:'ack-'+count},u:{$set:{owner:prefix,value:count}},upsert:true}],writeConcern:{w:'majority',j:true,wtimeout:3000}}));
  count++;
 }catch(e){
  if(!transient.has(e.code)&&!['MongoNetworkError','MongoNetworkTimeoutError','MongoServerSelectionError'].includes(e.name))throw e;
  errors++;
 }
 sleep(100);
}
assert(fs.existsSync(stop));assert(count>0);
assert.eq('maintenance-seed',d.getCollection(col).findOne({_id:'seed'}).value);
const expected=Array.from({length:count},(_,i)=>({_id:'ack-'+i,owner:prefix,value:i}));
const rows=new Map(d.getCollection(col).find({_id:{$regex:'^ack-'}}).toArray().map(row=>[row._id,row]));
for(const doc of expected){const actual=rows.get(doc._id);assert(actual);assert.eq(doc.owner,actual.owner);assert.eq(doc.value,actual.value);}
const digest=require('crypto').createHash('sha256').update(JSON.stringify(expected)).digest('hex');
print('MONGODB_MAINTENANCE_LEDGER_PASS '+JSON.stringify({acknowledged:count,retryErrors:errors,sha256:digest}));
"""

# Use the public asynchronous mongosh APIs, with an independently authenticated
# connection per worker. All workers share one clock after connection warmup.
PERFORMANCE_WORKER = """
const fs=require('fs'),samples=[],workers=[],transient=new Set([6,7,50,64,89,91,100,189,262,9001,10058,10107,11600,11602,13435,13436]);
let retries=0;
let uri=process.env.MONGODB_URI;
if(settings.shared_ca){
 // Keep only this fixture's CA in Node's shared store. Per-connection CA files
 // otherwise retain large native TLS stores until GC during reconnect churn.
 assert(uri.includes('&tlsCAFile=/tls/ca.crt&'));
 assert.eq(2,uri.split('&tlsCAFile=').length);
 const tls=require('tls'),ca=fs.readFileSync('/tls/ca.crt','utf8'),X509=require('crypto').X509Certificate;
 tls.setDefaultCACertificates([ca]);
 const roots=tls.getCACertificates('default');
 assert.eq(1,roots.length);assert.eq(new X509(ca).fingerprint256,new X509(roots[0]).fingerprint256);
 uri=uri.replace('&tlsCAFile=/tls/ca.crt','');
 print('MONGODB_CLIENT_CA_STORE_PASS trust=fixture-only');
}
for(let worker=0;worker<settings.concurrency;worker++){
 const connection=await new Mongo(uri),database=connection.getDB('appdb');
 assert.commandWorked(await database.auth({user:'application',pwd:process.env.APP_PASSWORD}));
 const warm=await database.runCommand({find:prefix+'_documents',filter:{_id:worker},limit:1,singleBatch:true,readConcern:{level:'majority'}});
 assert.commandWorked(warm);assert.eq('x'.repeat(1024),warm.cursor.firstBatch[0].payload);
 workers.push({connection,database,worker,cycles:0});
}
print('MONGODB_PERFORMANCE_WORKER_READY');
const started=Date.now(),deadline=started+settings.seconds*1000;
let nextMemory=started;
function reportMemory(force=false){
 const now=Date.now();
 if(!settings.memory_profile||(!force&&now<nextMemory))return;
 nextMemory=now+10000;
 print('MONGODB_CLIENT_MEMORY '+JSON.stringify({phase:settings.phase,elapsed_ms:now-started,
  samples:samples.length,retry_errors:retries,memory:process.memoryUsage(),
  heap_size_limit:require('v8').getHeapStatistics().heap_size_limit,
  node_version:process.versions.node,constrained_memory:process.constrainedMemory()}));
}
reportMemory();
await Promise.all(workers.map(async state=>{
 const database=state.database;
 while(Date.now()<deadline&&!fs.existsSync('/tmp/'+prefix+'.stop')){
  reportMemory();
  for(let operation=0;operation<5;operation++){
   const before=Date.now();let done=false;
   while(!done){
    try{
     if(operation<4){
      const id=(state.worker*257+state.cycles*4+operation)%4096;
      const r=await database.runCommand({find:prefix+'_documents',filter:{_id:id},limit:1,singleBatch:true,readConcern:{level:'majority'},maxTimeMS:3000});
      if(r.ok!==1){const e=Error('read command failed');e.code=r.code;throw e;}
      assert.commandWorked(r);assert.eq(1,r.cursor.firstBatch.length);assert.eq(id,r.cursor.firstBatch[0]._id);
      assert.eq('x'.repeat(1024),r.cursor.firstBatch[0].payload);
     }else{
      const id=settings.phase+'-'+state.worker+'-'+state.cycles;
      // Repeating this exact assignment is safe if the first reply was lost.
      const r=await database.runCommand({update:prefix+'_ledger',updates:[{q:{_id:id},
       u:{$set:{owner:prefix,phase:settings.phase,worker:state.worker,sequence:state.cycles}},upsert:true}],
       writeConcern:{w:'majority',j:true,wtimeout:3000}});
      const failure=r.writeConcernError||(r.writeErrors&&r.writeErrors[0])||(r.ok!==1?r:null);
      if(failure){const e=Error('write confirmation failed');e.code=failure.code;throw e;}
     }
     done=true;
    }catch(e){
     // PSMDB 8.0.32 code133 is not a generic RetriableError. Only the
     // controlled sharded fault phase may wait for a primary to reappear.
     const primarySelection=settings.sharded===true&&e.code===133;
     if(!settings.fault||(!primarySelection&&!transient.has(e.code)&&!['MongoNetworkError','MongoNetworkTimeoutError','MongoServerSelectionError'].includes(e.name)))throw e;
     if(Date.now()>deadline+120000)throw Error('fault workload recovery deadline exceeded');
     retries++;reportMemory();await new Promise(resolve=>setTimeout(resolve,100));
    }
   }
   samples.push(Date.now()-before);
  }
  state.cycles++;
 }
 assert(state.cycles>0);
}));
reportMemory(true);
assert(!fs.existsSync('/tmp/'+prefix+'.stop'));
print('MONGODB_PERFORMANCE_RESULT '+JSON.stringify({phase:settings.phase,concurrency:settings.concurrency,
 seconds:settings.seconds,elapsed_ms:Date.now()-started,retry_errors:retries,
 workers:workers.map(s=>({worker:s.worker,acknowledged:s.cycles})),latency_ms:samples}));
print('MONGODB_PERFORMANCE_WORKER_PASS');
"""


def performance_result(output, settings):
    rows = [json.loads(line.split(' ', 1)[1]) for line in output.splitlines()
            if line.startswith('MONGODB_PERFORMANCE_RESULT ')]
    if len(rows) != 1 or 'MONGODB_PERFORMANCE_WORKER_PASS' not in output.splitlines():
        raise ValueError('MongoDB performance result is incomplete')
    row = rows[0]
    for key in ('phase', 'concurrency', 'seconds'):
        if row.get(key) != settings[key]:raise ValueError('MongoDB performance workload identity changed')
    workers, samples = row['workers'], row['latency_ms']
    if (len(workers) != settings['concurrency'] or
            [w['worker'] for w in workers] != list(range(settings['concurrency'])) or
            any(type(w['acknowledged']) is not int or w['acknowledged'] <= 0 for w in workers) or
            len(samples) != 5 * sum(w['acknowledged'] for w in workers) or
            any(type(s) not in (float, int) or not math.isfinite(s) or s < 0 for s in samples) or
            type(row['retry_errors']) is not int or row['retry_errors'] < 0 or
            (not settings['fault'] and row['retry_errors']) or
            type(row['elapsed_ms']) not in (int, float) or not math.isfinite(row['elapsed_ms']) or
            not settings['seconds'] * 1000 <= row['elapsed_ms'] <= (settings['seconds'] + 130) * 1000):
        raise ValueError('MongoDB performance samples, mix, timing or confirmation ledger are invalid')
    ordered = sorted(samples)
    return {k: v for k, v in row.items() if k != 'latency_ms'} | {
        'operations': len(samples), 'reads': len(samples) * 4 // 5, 'writes': len(samples) // 5,
        'ops_per_second': len(samples) * 1000 / row['elapsed_ms'], 'business_errors': 0,
        'latency_ms': {str(p): ordered[math.ceil(len(ordered) * p / 100) - 1] for p in (50, 95, 99)},
        'samples_sha256': hashlib.sha256(json.dumps(samples).encode()).hexdigest()}


def eviction_refused(result):
    """A transport failure or generic CLI error cannot prove PDB protection."""
    return (result.returncode != 0 and 'TooManyRequests' in result.stderr and
            'disruption budget' in result.stderr.lower())


def ext4_capacity(metadata, mounted, required):
    """statfs excludes ext4 metadata; validate raw blocks without a guessed margin."""
    fields = {}
    for key in ('Block count', 'Block size', 'Overhead clusters'):
        values = re.findall(r'^' + key + r':\s+(\d+)\s*$', metadata, re.MULTILINE)
        if len(values) != 1:
            raise ValueError('MongoDB capacity requires unambiguous ext4 metadata')
        fields[key] = int(values[0])
    # The dedicated Longhorn fixture uses ordinary ext4, not bigalloc clusters.
    if re.search(r'^Filesystem features:.*\bbigalloc\b', metadata, re.MULTILINE):
        raise ValueError('MongoDB capacity fixture does not support ext4 bigalloc')
    block, count = map(int, mounted.split())
    raw, overhead = fields['Block count'], fields['Overhead clusters']
    if (block <= 0 or fields['Block size'] != block or not 0 <= overhead < raw or
            count != raw - overhead or raw * block < required):
        raise ValueError('MongoDB capacity filesystem has not reached the expanded volume size')
    return raw * block, overhead * block


def pressure_plan(output):
    """Bound the dedicated 4-10Gi fixture to 75% usage, retaining at least 1Gi."""
    block, total, available = map(int, output.split())
    total, available = block * total, block * available
    unit = 1024**2
    floor = max(total // 4, 1024**3)
    count = (available - floor) // unit
    if (block <= 0 or not 3*1024**3 < total <= 10*1024**3 or
            not floor < available <= total or not 128 <= count <= 8192):
        raise ValueError('MongoDB disk-pressure fixture has insufficient safe headroom')
    return {'block': block, 'total': total, 'available': available,
            'floor': floor, 'fill_mib': count}


def pressure_step(output, plan, remaining):
    """Re-read live headroom: database/checkpoint growth consumes the same disk."""
    block, total, available = map(int, output.split())
    available *= block
    if (block != plan['block'] or block*total != plan['total'] or
            not plan['floor'] <= available <= plan['total']):
        raise ValueError('MongoDB disk-pressure live filesystem or safe headroom changed')
    # The shell independently enforces floor immediately before dd. Keep a
    # small buffer for concurrent writes without weakening the 16MiB target.
    return min(128, remaining, max(0, (available-plan['floor'])//1024**2-8))


PRESSURE_CREATE = '''set -eu
dir=$1 run=$2
test ! -e "$dir" && test ! -L "$dir"
command -v timeout >/dev/null
umask 077
mkdir -- "$dir"
printf '%s' "$run" > "$dir/owner"
: > "$dir/fill"
'''
PRESSURE_CHUNK = '''set -eu
dir=$1 run=$2 count=$3 floor=$4
test -d "$dir" && test ! -L "$dir"
test ! -L "$dir/owner" && test "$(cat "$dir/owner")" = "$run"
test -f "$dir/fill" && test ! -L "$dir/fill"
mkdir -- "$dir/writing"
trap 'rmdir -- "$dir/writing"' EXIT
set -- $(stat -f -c '%S %a' /data/db)
available=$(( $1 * $2 )) requested=$(( count * 1048576 ))
printf 'MONGODB_PRESSURE_HEADROOM available=%s requested=%s floor=%s\n' "$available" "$requested" "$floor"
test $(( available - requested )) -ge "$floor" || exit 42
timeout -k 5s 20s dd if=/dev/zero of="$dir/fill" bs=1048576 count="$count" oflag=append conv=notrunc,fsync status=none
'''
PRESSURE_CLEAR = '''set -eu
dir=$1 run=$2
if test -e "$dir" || test -L "$dir"; then
 test -d "$dir" && test ! -L "$dir"
 if test ! -e "$dir/owner"; then rmdir -- "$dir"; exit; fi
 test ! -L "$dir/owner" && test "$(cat "$dir/owner")" = "$run"
 count=0
 while test -d "$dir/writing"; do
  test "$count" -lt 35
  count=$((count+1)); sleep 1
 done
 test ! -L "$dir/fill"
 rm -f -- "$dir/fill" "$dir/owner"
 rmdir -- "$dir"
fi
test ! -e "$dir" && test ! -L "$dir"
'''


def rebuild_volume_identity(pod, claim, volume, cluster):
    """Authorize loss of exactly one owned secondary's dynamically provisioned disk."""
    name = pod['metadata']['name']
    labels = pod['metadata'].get('labels', {})
    expected = 'mongod-data-' + name
    ref = volume['spec'].get('claimRef', {})
    if (labels.get('app.kubernetes.io/instance') != cluster or
            labels.get('app.kubernetes.io/replset') != 'rs0' or
            labels.get('app.kubernetes.io/managed-by') != 'percona-server-mongodb-operator' or
            not any(v.get('persistentVolumeClaim', {}).get('claimName') == expected for v in pod['spec']['volumes']) or
            claim['metadata']['name'] != expected or
            claim['metadata'].get('namespace') != pod['metadata']['namespace'] or
            claim['metadata'].get('labels', {}).get('app.kubernetes.io/instance') != cluster or
            claim['spec'].get('storageClassName') != 'mongodb-longhorn' or
            claim['spec'].get('volumeName') != volume['metadata']['name'] or
            volume['spec'].get('storageClassName') != 'mongodb-longhorn' or
            volume['spec'].get('persistentVolumeReclaimPolicy') != 'Delete' or
            volume['spec'].get('csi', {}).get('driver') != 'driver.longhorn.io' or
            (ref.get('namespace'), ref.get('name'), ref.get('uid')) !=
            (claim['metadata']['namespace'], expected, claim['metadata']['uid'])):
        raise ValueError('MongoDB rebuild refuses an unowned or rebound volume')
    return {'pod': name, 'pod_uid': pod['metadata']['uid'], 'claim': expected,
            'claim_uid': claim['metadata']['uid'], 'volume': volume['metadata']['name'],
            'volume_uid': volume['metadata']['uid']}


REBUILD_DATA = """
const expected=Array.from({length:512},(_,i)=>({_id:i,owner:prefix,value:i,payload:'r'.repeat(2048)}));
const digest=x=>require('crypto').createHash('sha256').update(JSON.stringify(x)).digest('hex');
"""


def load(base, filename, name):
    spec = importlib.util.spec_from_file_location(name, base / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def canonical_snapshot(objects):
    """Ignore API bookkeeping and Helm release history; retain data identities."""
    result = {}
    for kind, items in objects.items():
        rows = []
        for obj in items:
            if kind == 'secrets' and obj.get('type') == 'helm.sh/release.v1':
                continue
            meta = obj['metadata']
            row = {'name': meta['name'], 'uid': meta['uid']}
            if kind == 'pods':
                row.update(node=obj['spec']['nodeName'],
                           containers=[{'name': c['name'], 'imageID': c.get('imageID'),
                                        'restartCount': c.get('restartCount')} for c in obj['status'].get('containerStatuses', [])])
            elif kind == 'pvc':
                row.update(spec=obj['spec'], capacity=obj['status'].get('capacity'))
            elif kind == 'secrets':
                row['data_sha256'] = hashlib.sha256(json.dumps(obj.get('data', {}), sort_keys=True).encode()).hexdigest()
            elif kind == 'psmdb':
                row.update(spec=obj['spec'], generation=meta['generation'])
            rows.append(row)
        result[kind] = sorted(rows, key=lambda r: r['name'])
    return result


def quorum_policies(policy, run_id):
    """NetworkPolicies are additive: exclude victims from the existing allow rule.

    A second deny policy alone cannot override an ingress allowance. Egress is
    isolated too, so victim-initiated connections cannot preserve heartbeats.
    """
    if not run_id.startswith('mongodb_gate_') or not run_id[13:].isalnum():
        raise ValueError('invalid MongoDB quorum fault identity')
    key = 'kubeauto.io/mongodb-quorum-fault'
    desired = copy.deepcopy(policy['spec'])
    desired['podSelector'].setdefault('matchExpressions', []).append(
        {'key': key, 'operator': 'NotIn', 'values': [run_id]})
    deny = {'apiVersion': 'networking.k8s.io/v1', 'kind': 'NetworkPolicy',
            'metadata': {'name': 'mongodb-quorum-' + run_id[13:],
                         'namespace': policy['metadata']['namespace'],
                         'labels': {'kubeauto.io/test-run': run_id}},
            'spec': {'podSelector': {'matchLabels': {key: run_id}},
                     'policyTypes': ['Ingress', 'Egress'], 'ingress': [], 'egress': []}}
    return desired, deny


def logging_sample(output):
    """A new maxsize applies on future rotation, not to its historical copy."""
    lines=output.splitlines()
    if len(lines)!=4:raise ValueError('MongoDB logging filesystem sample is malformed')
    current,retained,count=map(int,lines[:3]);block,total,free=map(int,lines[3].split())
    if min(current,retained,count,free)<0 or block<=0 or total<=0 or free>total:
        raise ValueError('MongoDB logging filesystem counters are invalid')
    return current<64*1024**2 and retained>0 and count==1 and free/total>0.4


class Scenario:
    def __init__(self, base):
        self.base = base
        self.fixture = load(base, 'tests/helpers/mongodb-lab.py', 'mongodb_scenario_lab')
        self.product = load(base, 'roles/cluster-addon/files/mongodb.py', 'mongodb_scenario_product')
        self.lab = self.fixture.Lab(base)
        self.ns = self.fixture.CLUSTER
        self.client = 'mongodb-gate-client'
        self.client_uid = None
        self.client_attempted = False
        self.workload_started = False
        self.worker = None
        self.prefix = 'mongodb_gate_' + secrets.token_hex(8)

    def health(self):
        config = json.loads((self.lab.directory / 'yml/mongodb/input.json').read_text())
        self.product.health(config, self.product.API(self.lab.directory / 'kubectl.kubeconfig'))

    def wait_health(self, timeout=300):
        deadline = time.monotonic() + timeout
        while True:
            try:
                self.health()
                return
            except (ValueError, subprocess.TimeoutExpired):
                if time.monotonic() >= deadline:
                    raise ValueError('MongoDB fault recovery deadline exceeded') from None
                print('MONGODB_FAULT_HEARTBEAT stage=member-recovery', flush=True)
                time.sleep(5)

    def client_create(self, cluster_name=None, *, sharded=False):
        if self.lab.get('pod', self.client, self.ns):
            raise ValueError('focused scenario refuses an existing client fixture')
        obj = (self.fixture.client_object(cluster_name, sharded=True) if sharded else
               self.fixture.client_object() if cluster_name is None else self.fixture.client_object(cluster_name))
        obj['metadata']['labels']['kubeauto.io/test-run'] = self.prefix
        self.client_attempted = True
        result = subprocess.run([*self.lab.command, 'create', '-f', '-', '-o', 'json'],
                                input=json.dumps(obj), text=True, capture_output=True, timeout=30)
        if result.returncode:
            raise ValueError('MongoDB client fixture creation failed')
        self.client_uid = json.loads(result.stdout)['metadata']['uid']
        self.lab.run('-n', self.ns, 'wait', '--for=condition=Ready', 'pod/' + self.client, '--timeout=600s', timeout=620)

    def js(self, code, expected, app=False, authenticate=True, asynchronous=False):
        script = ASSERT + "globalThis.mongodbGateStage='setup';\ntry {\n" + ((APP_AUTH if app else AUTH) if authenticate else '') + 'const prefix=' + json.dumps(self.prefix) + ';\n' + code
        script += """\n} catch(e) {
const frame=String(e.stack||'').split('\\n').slice(1).map(s=>s.match(/:(\\d+):\\d+\\)?$/)).find(Boolean);
print('MONGODB_CLIENT_ERROR '+JSON.stringify({stage:globalThis.mongodbGateStage,name:e.name,
 code:typeof e.code==='number'?e.code:null,line:frame?Number(frame[1]):null}));quit(1);
}\n"""
        if asynchronous:
            # mongosh's file parser rejects await inside a top-level try block.
            # Its evaluator awaits the Promise returned by this final call.
            script = 'async function mongodbGateMain(){\n' + script + '\n}\nmongodbGateMain();\n'
        result = subprocess.run([*self.lab.command, '-n', self.ns, 'exec', '-i', self.client, '--',
                                 'mongosh', '--quiet', '--norc', '--nodb', '--file', '/dev/stdin'],
                                input=script, capture_output=True, text=True, timeout=180)
        if result.returncode or expected not in result.stdout.splitlines():
            try:
                self.save_client_failure(result)
            except (ValueError, KeyError, OSError, subprocess.SubprocessError):
                print('MONGODB_CLIENT_DIAGNOSTICS_UNAVAILABLE', file=sys.stderr)
            # Error output can include credentials supplied by a database client.
            raise ValueError('MongoDB business assertion failed marker=' + expected + ' rc=' + str(result.returncode) + client_error(result.stdout))
        print(expected, flush=True)
        return result.stdout

    def save_client_failure(self, result):
        hidden = []
        for obj in self.lab.get('secrets', ns=self.ns)['items']:
            for value in obj.get('data', {}).values():
                hidden.append(value)
                decoded = base64.b64decode(value).decode('utf-8', errors='ignore')
                if len(decoded) >= 4:
                    hidden.extend((decoded, json.dumps(decoded)[1:-1]))
        directory = Path(tempfile.mkdtemp(prefix='kubeauto-mongodb-install.client-'))
        path = directory / 'client-error.txt'
        path.write_text(self.fixture.redact(result.stdout + '\n' + result.stderr, hidden))
        path.chmod(0o600)
        print('MONGODB_CLIENT_DIAGNOSTICS_SAVED directory=' + str(directory), flush=True)

    def snapshot(self):
        kinds = ('pods', 'pvc', 'secrets', 'psmdb')
        return canonical_snapshot({kind: self.lab.get(kind, ns=self.ns)['items'] for kind in kinds})

    def security(self):
        pods = self.lab.get('pods', ns=self.ns)['items']
        pod = next(p for p in pods if p['metadata'].get('labels', {}).get('app.kubernetes.io/instance') == self.fixture.NAME
                   and p['metadata'].get('labels', {}).get('app.kubernetes.io/replset') == 'rs0')
        address = pod['status']['podIP']
        # A Pod IP is an independent endpoint without an IP SAN in the fixture.
        # directConnection prevents replica discovery from replacing the probe.
        uri = f'mongodb://{address}:27017/admin?directConnection=true&tls=true&tlsCAFile=/tls/ca.crt&serverSelectionTimeoutMS=3000'
        self.js('const mismatchedUri=' + json.dumps(uri) + ';\n' + SECURITY,
                'MONGODB_SECURITY_ASSERTIONS_PASS ca=true hostname=true plaintext-refused=true password=true least-privilege=true', app=True)

    def client_label(self, allowed):
        if not self.client_uid:
            raise ValueError('MongoDB network fixture requires the recorded client UID')
        patch = [{'op': 'test', 'path': '/metadata/uid', 'value': self.client_uid},
                 {'op': 'add' if allowed else 'remove', 'path': '/metadata/labels/kubeauto.io~1mongodb-client'}]
        if allowed:
            patch[-1]['value'] = 'true'
        self.lab.run('-n', self.ns, 'patch', 'pod', self.client, '--type=json', '-p', json.dumps(patch))

    def restart_isolated_members(self, members):
        """Close pre-existing replication sockets after installing the deny rule.

        NetworkPolicy does not guarantee termination of existing connections.
        Restart only mongod inside each already isolated, UID-verified Pod;
        the Pod (and therefore its deny label) must retain its identity.
        """
        prepared = []
        for member in members:
            pod = self.lab.get('pod', member['name'], self.ns)
            if (not pod or pod['metadata']['uid'] != member['uid'] or
                    pod['metadata'].get('labels', {}).get('kubeauto.io/mongodb-quorum-fault') != self.prefix):
                raise ValueError('quorum socket reset refuses a changed isolated member')
            status = next((c for c in pod.get('status', {}).get('containerStatuses', []) if c['name'] == 'mongod'), None)
            if not status or 'running' not in status.get('state', {}):
                raise ValueError('quorum socket reset requires a running mongod container')
            prepared.append((member, status['restartCount']))
        for member, before in prepared:
            # The pinned official ps-entry.sh execs mongod as PID 1. A closed
            # exec stream is not evidence: require an observed container restart.
            subprocess.run([*self.lab.command, '-n', self.ns, 'exec', member['name'], '-c', 'mongod', '--',
                            'sh', '-ec', 'test "$(cat /proc/1/comm)" = mongod; kill -TERM 1'],
                           capture_output=True, text=True, timeout=30)
            deadline = time.monotonic() + 90
            while True:
                pod = self.lab.get('pod', member['name'], self.ns)
                if (not pod or pod['metadata']['uid'] != member['uid'] or
                        pod['metadata'].get('labels', {}).get('kubeauto.io/mongodb-quorum-fault') != self.prefix):
                    raise ValueError('quorum socket reset lost the isolated Pod identity')
                status = next((c for c in pod.get('status', {}).get('containerStatuses', []) if c['name'] == 'mongod'), {})
                if status.get('restartCount', 0) > before and 'running' in status.get('state', {}):
                    print('MONGODB_QUORUM_CONNECTION_RESET_PASS pod=' + member['name'] +
                          ' restart-count=' + str(status['restartCount']), flush=True)
                    break
                if time.monotonic() >= deadline:
                    raise ValueError('quorum socket reset did not observe a mongod restart')
                print('MONGODB_QUORUM_HEARTBEAT stage=replication-socket-reset pod=' + member['name'], flush=True)
                time.sleep(2)

    def quorum(self):
        policy_name = self.fixture.NAME + '-access'
        policies = self.lab.get('networkpolicies', ns=self.ns)['items']
        if len(policies) != 1 or policies[0]['metadata']['name'] != policy_name or not self.product.owned(policies[0]):
            raise ValueError('quorum fault requires the sole product-owned network policy')
        policy = policies[0]
        pods = [p for p in self.lab.get('pods', ns=self.ns)['items']
                if p['metadata'].get('labels', {}).get('app.kubernetes.io/instance') == self.fixture.NAME and
                p['metadata'].get('labels', {}).get('app.kubernetes.io/replset') == 'rs0']
        if len(pods) != 3:
            raise ValueError('quorum fault requires exactly three replica-set members')
        key = 'kubeauto.io/mongodb-quorum-fault'
        if any(key in p['metadata'].get('labels', {}) for p in pods):
            raise ValueError('quorum fault refuses existing member fault labels')
        modified, deny = quorum_policies(policy, self.prefix)
        path = self.lab.directory / 'mongodb-fault-quorum.json'
        if path.exists():
            raise ValueError('quorum fault journal requires recovery')
        self.js("assert.commandWorked(d.runCommand({insert:prefix+'_documents',documents:[{_id:'committed',value:42}],writeConcern:{w:'majority',j:true,wtimeout:10000}}));print('MONGODB_QUORUM_BASELINE_PASS');",
                'MONGODB_QUORUM_BASELINE_PASS', app=True)
        state = {'cluster_uid': self.lab.verify()['cluster_uid'], 'run_id': self.prefix,
                 'policy_uid': policy['metadata']['uid'], 'original_spec': policy['spec'],
                 'members': [{'name': p['metadata']['name'], 'uid': p['metadata']['uid']} for p in pods[:2]]}
        self.fixture.write_private(path, state)

        def patch(kind, name, uid, operations):
            self.lab.run('-n', self.ns, 'patch', kind, name, '--type=json', '-p', json.dumps([
                {'op': 'test', 'path': '/metadata/uid', 'value': uid}, *operations]))

        first_error = None
        try:
            result = subprocess.run([*self.lab.command, 'create', '-f', '-', '-o', 'json'],
                                    input=json.dumps(deny), capture_output=True, text=True, timeout=30)
            if result.returncode:
                raise ValueError('quorum deny fixture creation failed')
            state['deny_uid'] = json.loads(result.stdout)['metadata']['uid']
            self.fixture.write_private(path, state)
            patch('networkpolicy', policy_name, state['policy_uid'], [
                {'op': 'test', 'path': '/spec', 'value': policy['spec']},
                {'op': 'replace', 'path': '/spec', 'value': modified}])
            for member in state['members']:
                patch('pod', member['name'], member['uid'], [
                    {'op': 'add', 'path': '/metadata/labels/kubeauto.io~1mongodb-quorum-fault', 'value': self.prefix}])
            self.restart_isolated_members(state['members'])
            # Bound the election check without relying on a fixed sleep. All
            # members must independently stop advertising a writable primary.
            deadline = time.monotonic() + 90
            script = "const a=db.getSiblingDB('admin');if(!a.auth({user:process.env.MONGODB_CLUSTER_ADMIN_USER,pwd:process.env.MONGODB_CLUSTER_ADMIN_PASSWORD}))throw Error('auth');print(JSON.stringify(a.runCommand({hello:1})));"
            while True:
                states = []
                for pod in pods:
                    output = self.lab.run('-n', self.ns, 'exec', pod['metadata']['name'], '-c', 'mongod', '--',
                                          'mongosh', '--quiet', '--norc', '--host', 'localhost', '--tls',
                                          '--tlsCAFile', '/etc/mongodb-ssl/ca.crt', '--eval', script)
                    states.append(json.loads(output.splitlines()[-1]))
                state['observations'] = [{'pod': p['metadata']['name'], 'ok': s.get('ok'),
                                          'isWritablePrimary': s.get('isWritablePrimary', False)}
                                         for p, s in zip(pods, states)]
                self.fixture.write_private(path, state)
                if all(s.get('ok') == 1 and not s.get('isWritablePrimary', False) for s in states):
                    break
                if time.monotonic() >= deadline:
                    raise ValueError('quorum loss left a writable primary beyond the election deadline')
                print('MONGODB_QUORUM_HEARTBEAT stage=primary-demotion', flush=True)
                time.sleep(3)
            self.js("""const uri=process.env.MONGODB_URI.replace('serverSelectionTimeoutMS=10000','serverSelectionTimeoutMS=3000');
const start=Date.now();let refused=false;
try {const probe=new Mongo(uri),d=probe.getDB('appdb');
 if(!d.auth({user:'application',pwd:process.env.APP_PASSWORD}))throw Error('unexpected-authentication-failure');
 const r=d.runCommand({insert:prefix+'_documents',documents:[{_id:'unacknowledged'}],writeConcern:{w:'majority',j:true,wtimeout:2000}});
 refused=r.ok!==1||Boolean(r.writeConcernError)||(r.writeErrors||[]).length>0;
}catch(e){assert(['MongoServerSelectionError','MongoNetworkTimeoutError','MongoNetworkError'].includes(e.name));refused=true;}
assert(refused);assert(Date.now()-start<15000);print('MONGODB_QUORUM_REFUSAL_PASS majority-write=refused');
""", 'MONGODB_QUORUM_REFUSAL_PASS majority-write=refused', authenticate=False)
        except BaseException as exc:
            first_error = exc
            self.fixture.write_private(self.lab.directory / 'mongodb-quorum-observation.json', state)
            raise
        finally:
            try:
                current = self.lab.get('networkpolicy', policy_name, self.ns)
                if current['spec'] != policy['spec']:
                    patch('networkpolicy', policy_name, state['policy_uid'], [
                        {'op': 'test', 'path': '/spec', 'value': modified},
                        {'op': 'replace', 'path': '/spec', 'value': policy['spec']}])
                elif current['metadata']['uid'] != state['policy_uid']:
                    raise ValueError('quorum policy ownership changed')
                for member in state['members']:
                    current = self.lab.get('pod', member['name'], self.ns)
                    if not current or current['metadata']['uid'] != member['uid']:
                        raise ValueError('quorum member identity changed; preserve recovery journal')
                    if key in current['metadata'].get('labels', {}):
                        patch('pod', member['name'], member['uid'], [
                            {'op': 'test', 'path': '/metadata/labels/kubeauto.io~1mongodb-quorum-fault', 'value': self.prefix},
                            {'op': 'remove', 'path': '/metadata/labels/kubeauto.io~1mongodb-quorum-fault'}])
                current = self.lab.get('networkpolicy', deny['metadata']['name'], self.ns)
                if current:
                    meta = current['metadata']
                    if (meta.get('labels', {}).get('kubeauto.io/test-run') != self.prefix or
                            ('deny_uid' in state and meta['uid'] != state['deny_uid'])):
                        raise ValueError('quorum deny fixture ownership changed')
                    options = {'apiVersion': 'v1', 'kind': 'DeleteOptions', 'preconditions': {'uid': meta['uid']}}
                    result = subprocess.run([*self.lab.command, 'delete', '--raw',
                        '/apis/networking.k8s.io/v1/namespaces/' + self.ns + '/networkpolicies/' + meta['name'], '-f', '-'],
                        input=json.dumps(options), capture_output=True, text=True, timeout=30)
                    if result.returncode or self.lab.get('networkpolicy', meta['name'], self.ns):
                        raise ValueError('quorum deny fixture cleanup failed')
                self.wait_health()
                self.js("assert.eq(42,d.getCollection(prefix+'_documents').findOne({_id:'committed'}).value);assert.commandWorked(d.runCommand({insert:prefix+'_documents',documents:[{_id:'recovered'}],writeConcern:{w:'majority',j:true,wtimeout:10000}}));print('MONGODB_QUORUM_RECOVERY_PASS');",
                        'MONGODB_QUORUM_RECOVERY_PASS', app=True)
                path.unlink()
            except BaseException:
                print('MONGODB_QUORUM_RECOVERY_FAIL journal=preserved', file=sys.stderr)
                if first_error is None:
                    raise

    def network_client(self):
        policy = self.lab.get('networkpolicy', self.fixture.NAME + '-access', self.ns)
        if not policy or not self.product.owned(policy) or policy['spec'].get('policyTypes') != ['Ingress']:
            raise ValueError('MongoDB network gate requires the product-owned ingress policy')
        self.js("assert.commandWorked(d.runCommand({insert:prefix+'_documents',documents:[{_id:'network'}],writeConcern:{w:'majority',j:true,wtimeout:10000}}));print('MONGODB_NETWORK_ALLOWED_PASS');",
                'MONGODB_NETWORK_ALLOWED_PASS', app=True)
        try:
            self.client_label(False)
            self.js("""const uri=process.env.MONGODB_URI.replace('serverSelectionTimeoutMS=10000','serverSelectionTimeoutMS=2000');
assert(uri.includes('serverSelectionTimeoutMS=2000'));
const deadline=Date.now()+30000;let denied=false;
while(Date.now()<deadline&&!denied){
 try {const probe=new Mongo(uri);probe.getDB('admin').runCommand({ping:1});sleep(500);}
 catch(e){assert.eq('MongoServerSelectionError',e.name);denied=true;}
}
assert(denied);print('MONGODB_NETWORK_DENIED_PASS transport=blocked');
""", 'MONGODB_NETWORK_DENIED_PASS transport=blocked', authenticate=False)
        finally:
            self.client_label(True)
        # A new mongosh process proves new allowed connections after removing
        # the denial fixture; an existing authenticated socket is insufficient.
        self.js("assert.eq('network',d.getCollection(prefix+'_documents').findOne({_id:'network'})._id);print('MONGODB_NETWORK_RECOVERED_PASS');",
                'MONGODB_NETWORK_RECOVERED_PASS', app=True)

    def network_metrics(self, allowed):
        members = self.maintenance_members()
        addresses = [p['status']['podIP'] for p in members]
        code = 'const addresses=' + json.dumps(addresses) + ';const allowed=' + json.dumps(allowed) + ';\n'
        code += r"""
const get=async address=>await new Promise((resolve,reject)=>{
 const request=require('http').get('http://'+address+':9216/metrics',response=>{
  let data='';response.on('data',chunk=>{data+=chunk;if(data.length>4194304)request.destroy(new Error('response-limit'));});
  response.on('end',()=>resolve({status:response.statusCode,data}));response.on('error',reject);
 });
 request.setTimeout(2500,()=>request.destroy(Object.assign(new Error('deadline'),{code:'GATE_TIMEOUT'})));
 request.on('error',reject);
});
for(const address of addresses){
 let response,denied=false;
 try{response=await get(address);}catch(e){if(allowed)throw e;assert.eq('GATE_TIMEOUT',e.code);denied=true;}
 if(allowed){assert.eq(200,response.status);assert(/^mongodb_up(?:\{[^\n]*\})?\s+1(?:\.0+)?$/m.test(response.data));}
 else assert(denied);
}
print('MONGODB_NETWORK_METRICS_'+(allowed?'ALLOWED':'DENIED')+'_PASS members=3');
"""
        self.js(code, 'MONGODB_NETWORK_METRICS_' + ('ALLOWED' if allowed else 'DENIED') + '_PASS members=3', authenticate=False, asynchronous=True)

    def network(self):
        path = self.lab.directory / 'mongodb-fault-network.json'
        config_path = self.lab.directory / 'config.yml'
        config = yaml.safe_load(config_path.read_text())
        original = config['mongodb_monitoring_namespace']
        if original == self.ns or path.exists():
            raise ValueError('MongoDB network gate requires the original separate monitoring namespace and no pending journal')
        policy = self.lab.get('networkpolicy', self.fixture.NAME + '-access', self.ns)
        if not policy or not self.product.owned(policy):
            raise ValueError('MongoDB network gate requires its owned product policy')
        self.network_client()
        self.network_metrics(False)
        before = self.snapshot()
        journal = {'cluster_uid': self.lab.verify()['cluster_uid'], 'run_id': self.prefix,
                   'policy_uid': policy['metadata']['uid'], 'original_policy': policy['spec'],
                   'original_monitoring_namespace': original, 'applied_monitoring_namespace': self.ns}
        self.fixture.write_private(path, journal)
        first_error = None
        try:
            config['mongodb_monitoring_namespace'] = self.ns
            config_path.write_text(yaml.safe_dump(config, sort_keys=False));config_path.chmod(0o600)
            result = subprocess.run(['kubecli', 'setup', self.ns, '07', '--tags', 'mongodb'], cwd=self.base, timeout=2400)
            if result.returncode:raise ValueError('MongoDB network monitoring configuration did not converge')
            self.network_metrics(True)
            # Monitoring namespace access to 9216 must not grant database access.
            self.client_label(False)
            try:
                self.js("""let denied=false;const uri=process.env.MONGODB_URI.replace('serverSelectionTimeoutMS=10000','serverSelectionTimeoutMS=2000'),deadline=Date.now()+30000;
while(Date.now()<deadline&&!denied){
 try{const probe=new Mongo(uri);probe.getDB('admin').runCommand({ping:1});sleep(500);}
 catch(e){assert.eq('MongoServerSelectionError',e.name);denied=true;}
}
assert(denied);print('MONGODB_NETWORK_MONITOR_DATABASE_DENIED_PASS');""", 'MONGODB_NETWORK_MONITOR_DATABASE_DENIED_PASS', authenticate=False)
            finally:self.client_label(True)
            self.js("assert.commandWorked(d.runCommand({insert:prefix+'_documents',documents:[{_id:'replication-network'}],writeConcern:{w:3,j:true,wtimeout:10000}}));print('MONGODB_NETWORK_REPLICATION_PASS voters=3');", 'MONGODB_NETWORK_REPLICATION_PASS voters=3', app=True)
            backups = load(self.base, 'tests/helpers/mongodb-backups.py', 'mongodb_network_backup')
            gate = backups.BackupGate(self.base, 'logical')
            gate.state = json.loads(gate.path.read_text());gate.validate_identity();gate.request_for_cleanup()
            if gate.verify_objects(persist=False) != gate.state['backup']:
                raise ValueError('MongoDB network backup dependency has changed')
            print('MONGODB_NETWORK_BACKUP_PATH_PASS retained-logical-backup=checksums-verified', flush=True)
        except BaseException as exc:
            first_error = exc
            raise
        finally:
            try:
                if self.lab.verify()['cluster_uid'] != journal['cluster_uid']:
                    raise ValueError('MongoDB network recovery refuses changed lab identity')
                config = yaml.safe_load(config_path.read_text())
                if config['mongodb_monitoring_namespace'] not in (original, self.ns):
                    raise ValueError('MongoDB network recovery refuses concurrent configuration edits')
                current = self.lab.get('networkpolicy', self.fixture.NAME + '-access', self.ns)
                if not current or current['metadata']['uid'] != journal['policy_uid'] or not self.product.owned(current):
                    raise ValueError('MongoDB network recovery refuses changed policy ownership')
                config['mongodb_monitoring_namespace'] = original
                config_path.write_text(yaml.safe_dump(config, sort_keys=False));config_path.chmod(0o600)
                result = subprocess.run(['kubecli', 'setup', self.ns, '07', '--tags', 'mongodb'], cwd=self.base, timeout=2400)
                if result.returncode:raise ValueError('MongoDB network original configuration did not converge')
                restored = self.lab.get('networkpolicy', self.fixture.NAME + '-access', self.ns)
                if restored['spec'] != journal['original_policy']:
                    raise ValueError('MongoDB network original policy was not restored')
                after = self.snapshot()
                if any(before[k] != after[k] for k in ('pvc','secrets','psmdb')):
                    raise ValueError('MongoDB network gate changed database, volume or credential identity')
                self.network_metrics(False)
                self.health();path.unlink()
            except BaseException:
                print('MONGODB_NETWORK_RECOVERY_FAIL journal=preserved', file=sys.stderr)
                if first_error is None:raise

    def rotation_secret(self, record, data):
        """Update supplied credentials without deleting or adopting a Secret."""
        current = self.lab.get('secret', record['name'], self.ns)
        if (not current or current['metadata']['uid'] != record['uid'] or
                not self.product.owned(current) or current['data'] not in record['allowed']):
            raise ValueError('MongoDB rotation refuses changed Secret ownership or material')
        operations = [{'op':'test','path':'/metadata/uid','value':record['uid']},
                      {'op':'test','path':'/metadata/resourceVersion','value':current['metadata']['resourceVersion']},
                      {'op':'replace','path':'/data','value':data}]
        result = subprocess.run([*self.lab.command, '-n', self.ns, 'patch', 'secret', record['name'],
                                 '--type=json', '--patch-file=/dev/stdin'],
                                input=json.dumps(operations), capture_output=True, text=True, timeout=45)
        if result.returncode:
            raise ValueError('MongoDB rotation Secret update failed')

    def rotation_product(self):
        result = subprocess.run(['kubecli','setup',self.ns,'07','--tags','mongodb'], cwd=self.base, timeout=2400)
        if result.returncode:
            raise ValueError('MongoDB rotation product reconciliation failed')

    def rotation_client(self):
        self.delete_client()
        self.client_create()
        self.js("assert.eq('rotation',d.getCollection(prefix+'_documents').findOne({_id:'rotation'})._id);print('MONGODB_ROTATION_BUSINESS_PASS');",
                'MONGODB_ROTATION_BUSINESS_PASS', app=True)

    def rotation_tls_stage(self, records, desired, stage):
        old = {p['metadata']['name']: p['metadata']['uid'] for p in self.maintenance_members()}
        for suffix in ('tls', 'tls-internal'):
            self.rotation_secret(records[suffix], desired[suffix])
        self.rotation_product()
        deadline = time.monotonic() + 600
        while True:
            members = [p for p in self.lab.get('pods', ns=self.ns)['items']
                       if p['metadata'].get('labels', {}).get('app.kubernetes.io/instance') == self.fixture.NAME and
                       p['metadata'].get('labels', {}).get('app.kubernetes.io/replset') == 'rs0']
            if any(p['metadata']['name'] not in old for p in members):
                raise ValueError('MongoDB TLS rotation observed an unexpected member')
            if len(members) == 3 and all(p['metadata']['uid'] != old[p['metadata']['name']] and self.product.ready(p) for p in members):
                break
            if time.monotonic() >= deadline:
                raise ValueError('MongoDB TLS rotation did not roll every member')
            print('MONGODB_ROTATION_HEARTBEAT stage=' + stage, flush=True)
            time.sleep(5)
        self.health()
        self.rotation_client()
        print('MONGODB_ROTATION_TLS_STAGE_PASS stage=' + stage + ' members=3', flush=True)

    def rotation(self):
        path = self.lab.directory / 'mongodb-fault-rotation.json'
        if path.exists():
            raise ValueError('MongoDB rotation requires recovery of its existing journal')
        before = self.snapshot()
        generated = {o['metadata']['name'].removeprefix(self.fixture.NAME+'-'):o
                     for o in self.fixture.credentials()['items']}
        records, original, replacement = {}, {}, {}
        encode = lambda data: {k:base64.b64encode(v.encode()).decode() for k,v in data.items()}
        for suffix in ('users','app','tls','tls-internal'):
            obj = self.lab.get('secret', self.fixture.NAME+'-'+suffix, self.ns)
            if not obj or not self.product.owned(obj):
                raise ValueError('MongoDB rotation requires supplied owned credentials')
            original[suffix] = obj['data']
            replacement[suffix] = encode(generated[suffix]['stringData'])
            # Keep every system username stable; rotate the five passwords.
            if suffix == 'users':
                replacement[suffix] = {k:replacement[suffix][k] if k.endswith('_PASSWORD') else v
                                       for k,v in original[suffix].items()}
            records[suffix] = {'name':obj['metadata']['name'],'uid':obj['metadata']['uid'],
                               'allowed':[original[suffix],replacement[suffix]]}
        old_ca = base64.b64decode(original['tls']['ca.crt']).decode()
        new_ca = base64.b64decode(replacement['tls']['ca.crt']).decode()
        combined = base64.b64encode((old_ca+'\n'+new_ca).encode()).decode()
        def trust(material):
            return {s:material[s]|{'ca.crt':combined} for s in ('tls','tls-internal')}
        old_trust, new_trust = trust(original), trust(replacement)
        for suffix in ('tls','tls-internal'):
            records[suffix]['allowed'] += [old_trust[suffix],new_trust[suffix]]
        journal = {'cluster_uid':self.lab.verify()['cluster_uid'],'run_id':self.prefix,
                   'records':records,'original':original,'replacement':replacement}
        self.js("assert.commandWorked(d.runCommand({insert:prefix+'_documents',documents:[{_id:'rotation'}],writeConcern:{w:'majority',j:true,wtimeout:10000}}));print('MONGODB_ROTATION_SEED_PASS');", 'MONGODB_ROTATION_SEED_PASS',app=True)
        self.fixture.write_private(path,journal)
        first_error = None
        try:
            for suffix in ('users','app'):
                self.rotation_secret(records[suffix],replacement[suffix])
            self.rotation_product()
            self.rotation_client()
            old = {k:base64.b64decode(v).decode() for k,v in original['users'].items()}
            old['APP_PASSWORD'] = base64.b64decode(original['app']['password']).decode()
            # A fresh connection must reject each previous password with code 18.
            self.js('const previous='+json.dumps(old)+';\n'+"""
for(const role of ['BACKUP','CLUSTER_ADMIN','CLUSTER_MONITOR','DATABASE_ADMIN','USER_ADMIN']){
 assert(new Mongo(process.env.MONGODB_URI).getDB('admin').auth({user:process.env['MONGODB_'+role+'_USER'],pwd:process.env['MONGODB_'+role+'_PASSWORD']}));
 const db=new Mongo(process.env.MONGODB_URI).getDB('admin');let denied=false;
 try{denied=!db.auth({user:previous['MONGODB_'+role+'_USER'],pwd:previous['MONGODB_'+role+'_PASSWORD']});}
 catch(e){assert.eq(18,e.code);denied=true;}assert(denied);
}
let denied=false;try{denied=!new Mongo(process.env.MONGODB_URI).getDB('appdb').auth({user:'application',pwd:previous.APP_PASSWORD});}
catch(e){assert.eq(18,e.code);denied=true;}assert(denied);
print('MONGODB_ROTATION_PASSWORD_PASS system=5 application=1 old-refused=true');
""", 'MONGODB_ROTATION_PASSWORD_PASS system=5 application=1 old-refused=true')
            for material,stage in ((old_trust,'trust-both'),(new_trust,'new-leaves'),(replacement,'new-ca-only')):
                self.rotation_tls_stage(records,material,stage)
            hosts = [p['metadata']['name']+'.'+self.fixture.NAME+'-rs0.'+self.ns+'.svc.cluster.local'
                     for p in self.maintenance_members()]
            self.js('const previousCA='+json.dumps(old_ca)+';const hosts='+json.dumps(hosts)+';\n'+"""
const certificateErrors=new Set(['UNABLE_TO_VERIFY_LEAF_SIGNATURE','UNABLE_TO_GET_ISSUER_CERT_LOCALLY','SELF_SIGNED_CERT_IN_CHAIN','DEPTH_ZERO_SELF_SIGNED_CERT']);
for(const host of hosts){
 let denied=false;
 try{await new Promise((resolve,reject)=>{
  const socket=require('tls').connect({host,port:27017,servername:host,ca:previousCA,rejectUnauthorized:true},()=>{socket.end();resolve();});
  socket.setTimeout(3000,()=>socket.destroy(Object.assign(new Error('deadline'),{code:'GATE_TIMEOUT'})));
  socket.on('error',reject);
 });}catch(e){assert(certificateErrors.has(e.code));denied=true;}
 assert(denied);
}
print('MONGODB_ROTATION_CA_PASS new-authenticated=true old-ca-refused=3');
""", 'MONGODB_ROTATION_CA_PASS new-authenticated=true old-ca-refused=3',app=True,asynchronous=True)
        except BaseException as exc:
            first_error=exc
            raise
        finally:
            try:
                if self.lab.verify()['cluster_uid'] != journal['cluster_uid']:
                    raise ValueError('MongoDB rotation recovery refuses changed core identity')
                # Recover through the same overlapping trust path, including a
                # failure before the first TLS stage. Never replace a root directly.
                current = {s:self.lab.get('secret',records[s]['name'],self.ns)['data'] for s in ('tls','tls-internal')}
                transition = {s:current[s]|{'ca.crt':combined} for s in current}
                for suffix in transition:
                    if transition[suffix] not in records[suffix]['allowed']:
                        raise ValueError('MongoDB rotation recovery has unexpected TLS material')
                stages = ((transition,'recover-trust-both'),(old_trust,'recover-original-leaves'),(original,'recover-original-ca'))
                if all(current[s] == original[s] for s in current):
                    stages = ()
                for material,stage in stages:
                    # Skip an identical stage: it has no rollout to observe.
                    if any(self.lab.get('secret',records[s]['name'],self.ns)['data'] != material[s] for s in ('tls','tls-internal')):
                        self.rotation_tls_stage(records,material,stage)
                for suffix in ('users','app'):
                    self.rotation_secret(records[suffix],original[suffix])
                self.rotation_product();self.rotation_client();self.health()
                after=self.snapshot()
                same_cr = lambda rows: [{k:v for k,v in row.items() if k != 'generation'} for row in rows]
                if (any(before[k]!=after[k] for k in ('pvc','secrets')) or
                        same_cr(before['psmdb']) != same_cr(after['psmdb'])):
                    raise ValueError('MongoDB rotation recovery changed source data or credential identity')
                path.unlink()
                print('MONGODB_ROTATION_RECOVERY_PASS original-credentials=true pvc-preserved=true',flush=True)
            except BaseException:
                print('MONGODB_ROTATION_RECOVERY_FAIL journal=preserved',file=sys.stderr)
                if first_error is None:raise

    def stop_worker(self):
        if self.worker is None:
            return
        try:
            if self.worker.poll() is None:
                self.lab.run('-n', self.ns, 'exec', self.client, '--', 'touch', '/tmp/' + self.prefix + '.stop')
            self.worker.communicate(timeout=30)
        except (ValueError, subprocess.SubprocessError):
            # Killing kubectl alone leaves its remote mongosh alive. Remove the
            # exact owned Pod first, then recreate a client to clean run data.
            self.delete_client()
            if self.worker.poll() is None:
                self.worker.kill()
            self.worker.communicate(timeout=5)
            self.worker = None
            self.client_create()
        self.worker = None

    def fault_member(self, primary, role):
        if role not in ('primary', 'secondary'):
            raise ValueError('MongoDB fault role is not allowed')
        pods = [p for p in self.lab.get('pods', ns=self.ns)['items']
                if p['metadata'].get('labels', {}).get('app.kubernetes.io/instance') == self.fixture.NAME and
                p['metadata'].get('labels', {}).get('app.kubernetes.io/replset') == 'rs0']
        primary_name = primary.split('.', 1)[0]
        if len(pods) != 3 or primary_name not in {p['metadata']['name'] for p in pods}:
            raise ValueError('MongoDB fault refuses an unrecognized primary or replica-set identity')
        member = next(p for p in pods if (p['metadata']['name'] == primary_name) == (role == 'primary'))
        path = self.lab.directory / 'mongodb-fault-failover.json'
        if path.exists():
            raise ValueError('MongoDB fault journal requires recovery before another injection')
        owner = self.lab.verify()
        journal = {'cluster_uid': owner['cluster_uid'], 'kind': 'pod-delete', 'namespace': self.ns,
                   'pod': member['metadata']['name'], 'uid': member['metadata']['uid'], 'role': role,
                   'run_id': self.prefix, 'client_uid': self.client_uid}
        self.fixture.write_private(path, journal)
        options = {'apiVersion': 'v1', 'kind': 'DeleteOptions', 'preconditions': {'uid': journal['uid']}}
        endpoint = '/api/v1/namespaces/' + self.ns + '/pods/' + journal['pod']
        result = subprocess.run([*self.lab.command, 'delete', '--raw', endpoint, '-f', '-'],
                                input=json.dumps(options), capture_output=True, text=True, timeout=45)
        if result.returncode:
            raise ValueError('MongoDB owned Pod fault request failed')
        print('MONGODB_FAULT_INJECTED role=' + role + ' pod=' + journal['pod'] + ' uid=' + journal['uid'], flush=True)
        return journal

    def delete_client(self):
        current = self.lab.get('pod', self.client, self.ns)
        if not current:
            self.client_uid = None
            return
        meta = current['metadata']
        if (meta.get('labels', {}).get('kubeauto.io/test-run') != self.prefix or
                (self.client_uid is not None and meta['uid'] != self.client_uid)):
            raise ValueError('MongoDB client ownership changed during scenario')
        options = {'apiVersion': 'v1', 'kind': 'DeleteOptions', 'preconditions': {'uid': meta['uid']}}
        result = subprocess.run([*self.lab.command, 'delete', '--raw',
                                 '/api/v1/namespaces/' + self.ns + '/pods/' + self.client, '-f', '-'],
                                input=json.dumps(options), capture_output=True, text=True, timeout=45)
        if result.returncode:
            raise ValueError('MongoDB owned client deletion failed')
        self.lab.run('-n', self.ns, 'wait', '--for=delete', 'pod/' + self.client, '--timeout=120s', timeout=150)
        if self.lab.get('pod', self.client, self.ns):
            raise ValueError('MongoDB client fixture remains after cleanup')
        self.client_uid = None

    def maintenance_members(self):
        pods = [p for p in self.lab.get('pods', ns=self.ns)['items']
                if p['metadata'].get('labels', {}).get('app.kubernetes.io/instance') == self.fixture.NAME and
                p['metadata'].get('labels', {}).get('app.kubernetes.io/replset') == 'rs0']
        workers = {'mongodb-node-' + ip.rsplit('.', 1)[1] for ip in self.fixture.NODES[3:]}
        if (len(pods) != 3 or len({p['spec'].get('nodeName') for p in pods}) != 3 or
                any(p['spec'].get('nodeName') not in workers or not self.product.ready(p) for p in pods)):
            raise ValueError('MongoDB maintenance requires three healthy members on leased workers')
        return sorted(pods, key=lambda p: p['metadata']['name'])

    def rebuild_data(self, hosts):
        code = REBUILD_DATA + 'const hosts=' + json.dumps(hosts) + ';\n'
        code += """
for(const host of hosts){
 const peer=new Mongo('mongodb://'+host+'/appdb?directConnection=true&readPreference=secondaryPreferred&tls=true&tlsCAFile=/tls/ca.crt&serverSelectionTimeoutMS=5000');
 const db=peer.getDB('appdb');assert(db.auth({user:'application',pwd:process.env.APP_PASSWORD}));
 const col=db.getCollection(prefix+'_documents'),deadline=Date.now()+45000;let rows;
 while(true){
  rows=col.find({owner:prefix}).sort({_id:1}).toArray().map(r=>({_id:r._id,owner:r.owner,value:r.value,payload:r.payload}));
  if(digest(rows)===digest(expected))break;
  assert(Date.now()<deadline);sleep(1000);
 }
 assert.eq(1,col.getIndexes().filter(i=>i.name==='rebuild_value'&&i.unique).length);
}
print('MONGODB_REBUILD_DIGEST sha256='+digest(expected)+' members='+hosts.length);
print('MONGODB_REBUILD_DATA_PASS');
"""
        result = self.js(code, 'MONGODB_REBUILD_DATA_PASS', app=True)
        rows = [row for row in result.splitlines() if row.startswith('MONGODB_REBUILD_DIGEST sha256=')]
        if len(rows) != 1 or len(rows[0].split('sha256=', 1)[1].split()[0]) != 64:
            raise ValueError('MongoDB rebuild did not return a complete data digest')
        print(rows[0], flush=True)

    def wait_rebuild_identity(self, target, timeout=600):
        deadline = time.monotonic() + timeout
        while True:
            claim = self.lab.get('pvc', target['claim'], self.ns)
            pod = self.lab.get('pod', target['pod'], self.ns)
            if (claim and pod and claim['metadata']['uid'] != target['claim_uid'] and
                    claim['spec'].get('volumeName') and claim['spec']['volumeName'] != target['volume'] and
                    pod['metadata']['uid'] != target['pod_uid'] and self.product.ready(pod)):
                return
            if time.monotonic() >= deadline:
                raise ValueError('MongoDB secondary replacement identity deadline exceeded')
            print('MONGODB_REBUILD_HEARTBEAT stage=new-volume-and-member', flush=True)
            time.sleep(5)

    def rebuild(self):
        members = self.maintenance_members()
        hosts = [p['metadata']['name'] + '.' + self.fixture.NAME + '-rs0.' + self.ns + '.svc.cluster.local:27017'
                 for p in members]
        self.js(REBUILD_DATA + "assert.commandWorked(d.runCommand({insert:prefix+'_documents',documents:expected,writeConcern:{w:'majority',j:true,wtimeout:10000}}));assert.commandWorked(d.runCommand({createIndexes:prefix+'_documents',indexes:[{key:{value:1},name:'rebuild_value',unique:true}]}));print('MONGODB_REBUILD_SEED_PASS');", 'MONGODB_REBUILD_SEED_PASS', app=True)
        self.rebuild_data(hosts)
        result = self.js("const s=assert.commandWorked(a.runCommand({replSetGetStatus:1}));const secondary=s.members.find(m=>m.health===1&&m.state===2);assert(secondary);print('MONGODB_REBUILD_SECONDARY '+JSON.stringify(secondary.name));print('MONGODB_REBUILD_SELECTION_PASS');", 'MONGODB_REBUILD_SELECTION_PASS')
        selected = [json.loads(row.split(' ', 1)[1]) for row in result.splitlines() if row.startswith('MONGODB_REBUILD_SECONDARY ')]
        if len(selected) != 1 or selected[0] not in hosts:
            raise ValueError('MongoDB rebuild refuses an unknown secondary endpoint')
        member = members[hosts.index(selected[0])]
        claim = self.lab.get('pvc', 'mongod-data-' + member['metadata']['name'], self.ns)
        volume = self.lab.get('pv', claim['spec']['volumeName'])
        target = rebuild_volume_identity(member, claim, volume, self.fixture.NAME)
        before = self.snapshot()
        path = self.lab.directory / 'mongodb-fault-rebuild.json'
        if path.exists():raise ValueError('MongoDB rebuild requires recovery of its existing journal')
        self.fixture.write_private(path, {'cluster_uid': self.lab.verify()['cluster_uid'],
            'run_id': self.prefix, 'target': target, 'before': before, 'hosts': hosts})
        first_error = None
        try:
            # PVC protection holds the first deletion until the old Pod exits.
            # This is the upstream manual per-member resynchronization procedure.
            for plural, name, uid in [('persistentvolumeclaims', target['claim'], target['claim_uid']),
                                      ('pods', target['pod'], target['pod_uid'])]:
                endpoint = '/api/v1/namespaces/' + self.ns + '/' + plural + '/' + name
                options = {'apiVersion': 'v1', 'kind': 'DeleteOptions', 'preconditions': {'uid': uid}}
                response = subprocess.run([*self.lab.command, 'delete', '--raw', endpoint, '-f', '-'],
                    input=json.dumps(options), text=True, capture_output=True, timeout=45)
                if response.returncode:raise ValueError('MongoDB secondary volume fault request failed')
            print('MONGODB_REBUILD_FAULT_INJECTED secondary=' + target['pod'], flush=True)
            self.wait_rebuild_identity(target)
            self.wait_health(900)
            self.rebuild_data(hosts)
        except BaseException as exc:
            first_error = exc
            raise
        finally:
            try:
                self.wait_health(900)
                after = self.snapshot()
                old = next(c for c in before['pvc'] if c['name'] == target['claim'])
                new = next(c for c in after['pvc'] if c['name'] == target['claim'])
                if old['uid'] != new['uid']:
                    if new['spec']['volumeName'] == target['volume']:
                        raise ValueError('MongoDB rebuild reused the lost persistent volume')
                    self.lab.run('wait', '--for=delete', 'pv/' + target['volume'], '--timeout=300s', timeout=320)
                    rebuilt = self.lab.get('pod', target['pod'], self.ns)
                    if rebuilt['metadata']['uid'] == target['pod_uid']:
                        raise ValueError('MongoDB rebuild retained the original member Pod')
                    if first_error is None:
                        print('MONGODB_REBUILD_VOLUME_PASS new-pvc=true new-pv=true old-pv=absent', flush=True)
                elif first_error is None:
                    raise ValueError('MongoDB rebuild did not replace the secondary volume')
                if ([c for c in before['pvc'] if c['name'] != target['claim']] !=
                        [c for c in after['pvc'] if c['name'] != target['claim']] or
                        before['secrets'] != after['secrets'] or before['psmdb'] != after['psmdb']):
                    raise ValueError('MongoDB rebuild changed protected claims, credentials or cluster configuration')
                self.rebuild_data(hosts)
                path.unlink()
            except BaseException:
                print('MONGODB_REBUILD_RECOVERY_FAIL journal=preserved', file=sys.stderr)
                if first_error is None:raise

    def maintenance_budget(self, allowed, timeout=90):
        deadline = time.monotonic() + timeout
        while True:
            obj = self.lab.get('pdb', self.fixture.NAME + '-mongod-rs0', self.ns)
            if not obj:
                raise ValueError('MongoDB maintenance requires the product PDB')
            labels = obj['metadata'].get('labels', {})
            if (labels.get('app.kubernetes.io/instance') != self.fixture.NAME or
                    labels.get('app.kubernetes.io/managed-by') != 'percona-server-mongodb-operator' or
                    obj['spec'].get('maxUnavailable') != 1):
                raise ValueError('MongoDB maintenance refuses a foreign or unsafe PDB')
            status = obj.get('status', {})
            if (status.get('observedGeneration') == obj['metadata']['generation'] and
                    status.get('disruptionsAllowed') == allowed and status.get('expectedPods') == 3):
                return obj
            if time.monotonic() >= deadline:
                raise ValueError('MongoDB maintenance PDB convergence deadline exceeded')
            print('MONGODB_MAINTENANCE_HEARTBEAT stage=pdb allowed=' + str(allowed), flush=True)
            time.sleep(2)

    def maintenance_evict(self, member, dry_run=False):
        name, uid = member['metadata']['name'], member['metadata']['uid']
        current = self.lab.get('pod', name, self.ns)
        if not current or current['metadata']['uid'] != uid:
            raise ValueError('MongoDB maintenance refuses a replaced eviction target')
        obj = {'apiVersion': 'policy/v1', 'kind': 'Eviction',
               'metadata': {'name': name, 'namespace': self.ns},
               'deleteOptions': {'preconditions': {'uid': uid}}}
        endpoint = '/api/v1/namespaces/' + self.ns + '/pods/' + name + '/eviction'
        if dry_run: endpoint += '?dryRun=All'
        result = subprocess.run([*self.lab.command, 'create', '--raw', endpoint, '-f', '-'],
                                input=json.dumps(obj), capture_output=True, text=True, timeout=45)
        if dry_run:
            if not eviction_refused(result):
                raise ValueError('MongoDB maintenance did not prove second eviction refused by PDB')
            current = self.lab.get('pod', name, self.ns)
            if not current or current['metadata']['uid'] != uid or current['metadata'].get('deletionTimestamp'):
                raise ValueError('MongoDB maintenance dry-run altered the protected member')
            print('MONGODB_MAINTENANCE_PDB_REFUSAL_PASS status=429 protected-uid=preserved', flush=True)
        elif result.returncode:
            raise ValueError('MongoDB maintenance owned eviction request failed')

    def recover_maintenance_node(self, journal):
        node = self.lab.get('node', journal['node'])
        if (node['metadata']['uid'] != journal['node_uid'] or
                node['metadata'].get('labels', {}).get('kubeauto.io/mongodb-maintenance') != self.prefix):
            raise ValueError('MongoDB maintenance node identity changed; preserve fault journal')
        self.lab.run('uncordon', journal['node'])
        self.lab.run('label', 'node', journal['node'], 'kubeauto.io/mongodb-maintenance-')
        if self.lab.get('node', journal['node']).get('spec', {}).get('unschedulable'):
            raise ValueError('MongoDB maintenance node remains cordoned')

    def capacity_claims(self):
        claims = [p for p in self.lab.get('pvc', ns=self.ns)['items'] if
                  p['metadata'].get('labels', {}).get('app.kubernetes.io/instance') == self.fixture.NAME]
        expected = {'mongod-data-' + self.fixture.NAME + '-rs0-' + str(i) for i in range(3)}
        if ({p['metadata']['name'] for p in claims} != expected or
                any(p['spec'].get('storageClassName') != 'mongodb-longhorn' or
                    p.get('status', {}).get('phase') != 'Bound' or not p['spec'].get('volumeName') for p in claims)):
            raise ValueError('MongoDB capacity requires the three owned bound source volumes')
        result = {}
        for claim in claims:
            name, uid, volume = claim['metadata']['name'], claim['metadata']['uid'], claim['spec']['volumeName']
            pv = self.lab.get('pv', volume)
            ref = pv['spec'].get('claimRef', {})
            if (pv['spec'].get('storageClassName') != 'mongodb-longhorn' or
                    pv['spec'].get('csi', {}).get('driver') != 'driver.longhorn.io' or
                    (ref.get('namespace'), ref.get('name'), ref.get('uid')) != (self.ns, name, uid)):
                raise ValueError('MongoDB capacity refuses a foreign or rebound backing volume')
            result[name] = {'uid': uid, 'volume': volume, 'volume_uid': pv['metadata']['uid']}
        return result

    def capacity_finish(self, journal):
        path = self.lab.directory / 'config.yml'
        config = yaml.safe_load(path.read_text())
        cr = self.lab.get('psmdb', self.fixture.NAME, self.ns)
        if (self.lab.verify()['cluster_uid'] != journal['cluster_uid'] or cr['metadata']['uid'] != journal['cr_uid'] or
                not self.product.owned(cr) or self.capacity_claims() != journal['claims'] or
                {key: config.get(key) for key in journal['applied']} != journal['applied'] or
                any(rs['volumeSpec']['persistentVolumeClaim']['resources']['requests']['storage'] !=
                    journal['applied']['mongodb_pvc_size'] for rs in cr['spec']['replsets'])):
            raise ValueError('MongoDB capacity recovery refuses changed identity or configuration')
        # Growth is irreversible: retain the larger declaration on both success
        # and recovery. Only restore the original expansion switch.
        required = self.product.quantity(journal['applied']['mongodb_pvc_size'], 'memory')
        for member in self.maintenance_members():
            claim = journal['claims']['mongod-data-' + member['metadata']['name']]
            volume = claim['volume']
            if not re.fullmatch(r'pvc-[0-9a-f-]{36}', volume):
                raise ValueError('MongoDB capacity refuses an unexpected Longhorn device name')
            worker = next(ip for ip in self.fixture.NODES[3:] if
                          'mongodb-node-' + ip.rsplit('.', 1)[1] == member['spec']['nodeName'])
            # Read-only fixture diagnostics, after PVC/PV and worker ownership
            # checks. They do not expand a device or alter the customer path.
            metadata = subprocess.run(['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10',
                'root@' + worker, 'LC_ALL=C tune2fs -l /dev/longhorn/' + volume],
                capture_output=True, text=True, timeout=30)
            if metadata.returncode:
                raise ValueError('MongoDB capacity ext4 metadata probe failed')
            mount = self.lab.run('-n', self.ns, 'exec', member['metadata']['name'], '-c', 'mongod', '--',
                                 'df', '-P', '/data/db')
            rows = mount.splitlines()
            if len(rows) != 2 or rows[1].split()[0] != '/dev/longhorn/' + volume:
                raise ValueError('MongoDB capacity mounted device does not match its owned volume')
            output = self.lab.run('-n', self.ns, 'exec', member['metadata']['name'], '-c', 'mongod', '--',
                                  'stat', '-f', '-c', '%S %b', '/data/db')
            raw, overhead = ext4_capacity(metadata.stdout, output, required)
            print('MONGODB_CAPACITY_FILESYSTEM_MEMBER_PASS pod=' + member['metadata']['name'] +
                  ' raw-bytes=' + str(raw) + ' metadata-bytes=' + str(overhead), flush=True)
        print('MONGODB_CAPACITY_FILESYSTEM_PASS members=3 pvc-pv-identities=preserved', flush=True)
        if journal['switch_absent']:
            config.pop('mongodb_volume_expansion_enabled', None)
        else:
            config['mongodb_volume_expansion_enabled'] = journal['original_switch']
        path.write_text(yaml.safe_dump(config, sort_keys=False));path.chmod(0o600)
        # Persist the exact permitted recovery state before invoking the product.
        journal['applied'] = {key: config.get(key) for key in journal['applied']}
        self.fixture.write_private(self.lab.directory / 'mongodb-fault-capacity.json', journal)
        result = subprocess.run(['kubecli', 'setup', self.ns, '07', '--tags', 'mongodb'], cwd=self.base, timeout=2400)
        if result.returncode:raise ValueError('MongoDB capacity final product convergence failed')
        self.health()

    def capacity_recover(self):
        path = self.lab.directory / 'mongodb-fault-capacity.json'
        journal = json.loads(path.read_text())
        if not re.fullmatch(r'mongodb_gate_[0-9a-f]{16}', str(journal.get('run_id', ''))) or not journal.get('client_uid'):
            raise ValueError('MongoDB capacity recovery requires its original workload identity')
        config = yaml.safe_load((self.lab.directory / 'config.yml').read_text())
        cr = self.lab.get('psmdb', self.fixture.NAME, self.ns)
        values = {key: config.get(key) for key in journal['applied']}
        unapplied = values == journal['original']
        if (self.lab.verify()['cluster_uid'] != journal['cluster_uid'] or cr['metadata']['uid'] != journal['cr_uid'] or
                not self.product.owned(cr) or self.capacity_claims() != journal['claims'] or
                values not in (journal['original'], journal['applied']) or
                (unapplied and any(rs['volumeSpec']['persistentVolumeClaim']['resources']['requests']['storage'] !=
                    journal['original']['mongodb_pvc_size'] for rs in cr['spec']['replsets']))):
            raise ValueError('MongoDB capacity recovery refuses changed identity or configuration')
        self.prefix = journal['run_id'];self.client_uid = journal['client_uid'];self.client_attempted = True
        current = self.lab.get('pod', self.client, self.ns)
        if current:
            # Removing the recorded client also terminates its remote mongosh.
            self.delete_client()
        result = subprocess.run(['kubecli', 'setup', self.ns, '07', '--tags', 'mongodb'], cwd=self.base, timeout=2400)
        if result.returncode:raise ValueError('MongoDB capacity recovery product convergence failed')
        self.health()
        if not unapplied:self.capacity_finish(journal)
        self.client_create();self.workload_started = True;self.cleanup();self.health()
        path.unlink()
        print('MONGODB_CAPACITY_RECOVER_SCOPED_CLEAN_PASS growth=retained workload=absent', flush=True)
        print('MONGODB_CAPACITY_RECOVER_PASS', flush=True)

    def capacity_expand(self, *, logging=False):
        path = self.lab.directory / 'mongodb-fault-capacity.json'
        config_path = self.lab.directory / 'config.yml'
        config = yaml.safe_load(config_path.read_text())
        size = config['mongodb_pvc_size']
        if path.exists() or not re.fullmatch(r'[1-9]Gi' if logging else r'[1-7]Gi', size):
            raise ValueError('MongoDB focused capacity requires a 1-7Gi fixture without pending recovery')
        desired = '10Gi' if logging else str(int(size[:-2]) + 1) + 'Gi'
        cr = self.lab.get('psmdb', self.fixture.NAME, self.ns)
        journal = {'run_id': self.prefix, 'client_uid': self.client_uid,
                   'cluster_uid': self.lab.verify()['cluster_uid'], 'cr_uid': cr['metadata']['uid'],
                   'claims': self.capacity_claims(), 'switch_absent': 'mongodb_volume_expansion_enabled' not in config,
                   'original_switch': config.get('mongodb_volume_expansion_enabled'),
                   'original': {key: config.get(key) for key in ('mongodb_pvc_size', 'mongodb_volume_expansion_enabled')},
                   'applied': {'mongodb_pvc_size': desired, 'mongodb_volume_expansion_enabled': True}}
        self.fixture.write_private(path, journal)
        with tempfile.TemporaryFile(mode='w+b') as output:
            script = ASSERT + 'try {\n' + APP_AUTH + 'const prefix=' + json.dumps(self.prefix) + ';\n' + MAINTENANCE_WORKER.replace('MONGODB_MAINTENANCE_', 'MONGODB_CAPACITY_')
            script += "\n}catch(e){print('MONGODB_CLIENT_ERROR '+JSON.stringify({stage:'maintenance',name:e.name,code:typeof e.code==='number'?e.code:null}));quit(1);}\n"
            self.worker = subprocess.Popen([*self.lab.command, '-n', self.ns, 'exec', '-i', self.client, '--',
                'mongosh', '--quiet', '--norc', '--nodb', '--file', '/dev/stdin'], stdin=subprocess.PIPE, stdout=output, stderr=output)
            worker = self.worker
            self.worker.stdin.write(script.encode());self.worker.stdin.close();self.worker.stdin = None
            deadline = time.monotonic() + 30
            while b'MONGODB_CAPACITY_WORKER_READY\n' not in os.pread(output.fileno(), 1048576, 0):
                if worker.poll() is not None or time.monotonic() >= deadline:
                    raise ValueError('MongoDB capacity workload did not establish its acknowledged baseline')
                time.sleep(1)
            config.update(journal['applied'])
            config_path.write_text(yaml.safe_dump(config, sort_keys=False));config_path.chmod(0o600)
            result = subprocess.run(['kubecli', 'setup', self.ns, '07', '--tags', 'mongodb'], cwd=self.base, timeout=2400)
            if result.returncode:raise ValueError('MongoDB capacity product expansion failed')
            self.health();self.stop_worker()
            journal['client_uid'] = self.client_uid;self.fixture.write_private(path, journal)
            rows = [line for line in os.pread(output.fileno(), 1048576, 0).decode(errors='replace').splitlines()
                    if line.startswith('MONGODB_CAPACITY_LEDGER_PASS ')]
            if worker.returncode or len(rows) != 1:
                raise ValueError('MongoDB capacity lost its acknowledged-write ledger')
            ledger = json.loads(rows[0].split(' ', 1)[1])
            if (type(ledger.get('acknowledged')) is not int or ledger['acknowledged'] < 1 or
                    type(ledger.get('retryErrors')) is not int or not re.fullmatch('[0-9a-f]{64}', ledger.get('sha256', ''))):
                raise ValueError('MongoDB capacity workload ledger is malformed')
            print(rows[0], flush=True)
        self.capacity_finish(journal)
        self.cleanup();self.health();path.unlink()
        print('MONGODB_CAPACITY_EXPANSION_ASSERTIONS_PASS declared=' + desired + ' ledger=verified filesystem=verified', flush=True)

    def logging(self):
        """Migrate the small runner fixture through product-owned volume/log policy.

        copytruncate needs another copy of the current log. Grow first, using
        the existing acknowledged-write and filesystem gate; never unlink logs.
        The desired policy is persistent and safe to reconcile after interruption.
        """
        path=self.lab.directory/'config.yml'
        config=yaml.safe_load(path.read_text())
        policy={'interval_minutes':1,'max_size_mb':32,'retain':1}
        if (config.get('mongodb_logrotate',{}) not in ({},policy) or
                config.get('mongodb_pvc_size') not in [str(i)+'Gi' for i in range(1,11)]):
            raise ValueError('MongoDB logging refuses an unknown fixture capacity or policy')
        if config['mongodb_pvc_size']!='10Gi':
            self.capacity_expand(logging=True)
            self.client_create();self.workload_started=True
            config=yaml.safe_load(path.read_text())
        claims=self.capacity_claims();before=self.snapshot()
        config['mongodb_logrotate']=policy
        path.write_text(yaml.safe_dump(config,sort_keys=False));path.chmod(0o600)
        result=subprocess.run(['kubecli','setup',self.ns,'07','--tags','mongodb'],cwd=self.base,timeout=2400)
        if result.returncode:raise ValueError('MongoDB logging product convergence failed')
        self.health()
        cr=self.lab.get('psmdb',self.fixture.NAME,self.ns)
        rotation=cr['spec']['logcollector']['logrotate']
        cm=self.lab.get('configmap',self.fixture.NAME+'-logrotate-config',self.ns)
        if (rotation['schedule']!='*/1 * * * *' or not cm or
                cm.get('data',{}).get('mongodb.conf')!=rotation['configuration'] or
                not any(r.get('uid')==cr['metadata']['uid'] and r.get('controller') for r in
                        cm['metadata'].get('ownerReferences',[]))):
            raise ValueError('MongoDB logging native configuration was not consumed')
        deadline=time.monotonic()+300
        while True:
            samples=[]
            for pod in self.maintenance_members():
                sidecar=next(c for c in pod['spec']['containers'] if c['name']=='logrotate')
                if not any(e.get('name')=='LOGROTATE_SCHEDULE' and e.get('value')==rotation['schedule']
                           for e in sidecar.get('env',[])):
                    raise ValueError('MongoDB logging schedule was not consumed by its sidecar')
                raw=self.lab.run('-n',self.ns,'exec',pod['metadata']['name'],'-c','mongod','--',
                    'sh','-ec','stat -c %s /data/db/logs/mongod.full.log; if test -f /data/db/logs/mongod.full.log.1; then stat -c %s /data/db/logs/mongod.full.log.1; else echo 0; fi; set -- /data/db/logs/mongod.full.log.[0-9]*; if test -e "$1"; then echo "$#"; else echo 0; fi; stat -f -c "%S %b %a" /data/db')
                samples.append(logging_sample(raw))
            if all(samples):break
            if time.monotonic()>=deadline:raise ValueError('MongoDB logging rotation/space recovery deadline exceeded')
            print('MONGODB_LOGGING_HEARTBEAT stage=native-rotation',flush=True);time.sleep(5)
        after=self.snapshot()
        if (claims!=self.capacity_claims() or before['pvc']!=after['pvc'] or
                before['secrets']!=after['secrets'] or
                [(c['name'],c['uid']) for c in before['psmdb']]!=[(c['name'],c['uid']) for c in after['psmdb']]):
            raise ValueError('MongoDB logging changed source volume or credential identity')
        self.js(DATA,'MONGODB_DATA_ASSERTIONS_PASS documents=199 bson=true indexes=true ttl=true digest=true',app=True)
        Performance(self).backup_dependencies()
        print('MONGODB_LOGGING_ASSERTIONS_PASS native-rotation=true members=3 free-space=verified identities=preserved',flush=True)

    def pressure_exec(self, journal, script, *args):
        member = journal['member']
        pod = self.lab.get('pod', member['name'], self.ns)
        cr = self.lab.get('psmdb', self.fixture.NAME, self.ns)
        if (not re.fullmatch(r'mongodb_gate_[0-9a-f]{16}', journal['run_id']) or
                self.lab.verify()['cluster_uid'] != journal['cluster_uid'] or
                not cr or cr['metadata']['uid'] != journal['cr_uid'] or not self.product.owned(cr) or
                not pod or pod['metadata']['uid'] != member['uid'] or
                pod['spec'].get('nodeName') != member['node'] or self.capacity_claims() != journal['claims']):
            raise ValueError('MongoDB disk-pressure refuses changed source, Pod or volume identity')
        directory = '/data/db/.kubeauto-capacity-' + journal['run_id']
        result = subprocess.run([*self.lab.command, '-n', self.ns, 'exec', member['name'], '-c', 'mongod', '--',
            'sh', '-ec', script, 'pressure', directory, journal['run_id'], *map(str, args)],
            capture_output=True, text=True, timeout=60)
        if result.returncode:
            stage = {PRESSURE_CREATE:'create', PRESSURE_CHUNK:'chunk', PRESSURE_CLEAR:'clear'}.get(script,'verify')
            # Keep exact bounded-fixture evidence without exporting arbitrary API output.
            headroom = [line for line in result.stdout.splitlines() if re.fullmatch(
                r'MONGODB_PRESSURE_HEADROOM available=[0-9]+ requested=[0-9]+ floor=[0-9]+',line)]
            self.fixture.write_private(self.lab.directory/'mongodb-capacity-pressure-command.json',
                {'run_id':journal['run_id'],'member':member,'stage':stage,'rc':result.returncode,'headroom':headroom})
            raise ValueError('MongoDB disk-pressure command failed stage='+stage+' rc='+str(result.returncode)+
                             ' '+(' '.join(headroom) if headroom else 'headroom=unavailable'))
        return result.stdout.strip()

    def pressure_restore(self, journal):
        self.pressure_exec(journal, PRESSURE_CLEAR)
        member = journal['member']['name']
        output = self.lab.run('-n', self.ns, 'exec', member, '-c', 'mongod', '--',
                              'stat', '-f', '-c', '%S %b %a', '/data/db')
        block, total, available = map(int, output.split())
        original = journal['filesystem']
        if (block * total != original['total'] or block * available < original['floor']):
            raise ValueError('MongoDB disk-pressure free-space recovery not proven')
        self.wait_health()
        print('MONGODB_CAPACITY_PRESSURE_RECOVERY_PASS filler=absent headroom=safe', flush=True)

    def capacity_pressure_recover(self):
        path = self.lab.directory / 'mongodb-fault-capacity-pressure.json'
        journal = json.loads(path.read_text())
        self.prefix = journal['run_id'];self.client_uid = journal['client_uid'];self.client_attempted = True
        # Validate the complete graph before deleting a recorded workload client.
        self.pressure_exec(journal, 'test ! -L "$1"')
        if self.lab.get('pod', self.client, self.ns):self.delete_client()
        self.pressure_restore(journal)
        try:
            self.client_create()
        finally:
            if self.client_uid:
                journal['client_uid'] = self.client_uid;self.fixture.write_private(path, journal)
        self.workload_started = True;self.cleanup();self.health();path.unlink()
        print('MONGODB_CAPACITY_PRESSURE_RECOVER_SCOPED_CLEAN_PASS', flush=True)
        print('MONGODB_CAPACITY_PRESSURE_RECOVER_PASS', flush=True)

    def capacity_pressure(self, observer=None):
        path = self.lab.directory / 'mongodb-fault-capacity-pressure.json'
        if path.exists():raise ValueError('MongoDB disk-pressure requires its previous recovery first')
        members = self.maintenance_members()
        output = self.js("print('MONGODB_CAPACITY_PRIMARY '+a.runCommand({hello:1}).primary);\nprint('MONGODB_CAPACITY_PRIMARY_PASS');",
                         'MONGODB_CAPACITY_PRIMARY_PASS')
        hosts = [line.split(' ', 1)[1] for line in output.splitlines() if line.startswith('MONGODB_CAPACITY_PRIMARY ')]
        if len(hosts) != 1 or hosts[0].split('.', 1)[0] not in {p['metadata']['name'] for p in members}:
            raise ValueError('MongoDB disk-pressure did not identify the owned primary')
        target = next(p for p in members if p['metadata']['name'] != hosts[0].split('.', 1)[0])
        name = target['metadata']['name']
        filesystem = pressure_plan(self.lab.run('-n', self.ns, 'exec', name, '-c', 'mongod', '--',
                                               'stat', '-f', '-c', '%S %b %a', '/data/db'))
        cr = self.lab.get('psmdb', self.fixture.NAME, self.ns)
        journal = {'run_id': self.prefix, 'client_uid': self.client_uid,
            'cluster_uid': self.lab.verify()['cluster_uid'], 'cr_uid': cr['metadata']['uid'],
            'claims': self.capacity_claims(), 'filesystem': filesystem,
            'member': {'name': name, 'uid': target['metadata']['uid'], 'node': target['spec']['nodeName']}}
        self.fixture.write_private(path, journal)
        first_error = None
        try:
            self.pressure_exec(journal, PRESSURE_CREATE)
            remaining, confirmed = filesystem['fill_mib'], 0
            while remaining:
                live = self.lab.run('-n', self.ns, 'exec', name, '-c', 'mongod', '--',
                                    'stat', '-f', '-c', '%S %b %a', '/data/db')
                count = pressure_step(live, filesystem, remaining)
                if not count:break
                self.pressure_exec(journal, PRESSURE_CHUNK, count, filesystem['floor'])
                remaining -= count
                # Each pressure step has a real three-member journaled write.
                self.js("assert.commandWorked(d.runCommand({update:prefix+'_ledger',updates:[{q:{_id:'pressure'},u:{$set:{step:" +
                        str(confirmed) + "}},upsert:true}],writeConcern:{w:3,j:true,wtimeout:10000}}));\nprint('MONGODB_CAPACITY_PRESSURE_WRITE_PASS');",
                        'MONGODB_CAPACITY_PRESSURE_WRITE_PASS', app=True)
                confirmed += 1
                print('MONGODB_CAPACITY_PRESSURE_HEARTBEAT remaining-mib=' + str(remaining), flush=True)
            output = self.lab.run('-n', self.ns, 'exec', name, '-c', 'mongod', '--',
                                  'stat', '-f', '-c', '%S %b %a', '/data/db')
            block, total, available = map(int, output.split())
            if (block * total != filesystem['total'] or block * available < 1024**3 or
                    block * available > filesystem['floor'] + 16*1024**2):
                raise ValueError('MongoDB disk-pressure did not reach its bounded 75% target')
            code = REBUILD_DATA + """
assert.commandWorked(d.runCommand({createIndexes:prefix+'_documents',indexes:[{key:{value:1},name:'rebuild_value',unique:true}]}));
assert.commandWorked(d.runCommand({insert:prefix+'_documents',documents:expected,writeConcern:{w:3,j:true,wtimeout:15000}}));
print('MONGODB_CAPACITY_PRESSURE_DATA_PASS');
"""
            self.js(code, 'MONGODB_CAPACITY_PRESSURE_DATA_PASS', app=True)
            self.rebuild_data([p['metadata']['name'] + '.' + self.fixture.NAME + '-rs0.' + self.ns + '.svc.cluster.local:27017' for p in members])
            self.health()
            if observer:observer(True)
            print('MONGODB_CAPACITY_PRESSURE_ASSERTIONS_PASS writes=' + str(confirmed) +
                  ' free-bytes=' + str(block*available) + ' data-members=3 digest=verified', flush=True)
        except BaseException as exc:
            first_error = exc
            raise
        finally:
            try:
                self.pressure_restore(journal);path.unlink()
            except BaseException:
                print('MONGODB_CAPACITY_PRESSURE_RECOVERY_FAIL journal=preserved', file=sys.stderr)
                if first_error is None:raise
        if observer:observer(False)

    def lag_local_state(self, member):
        pod = self.lab.get('pod', member['name'], self.ns)
        if not pod or pod['metadata']['uid'] != member['uid']:
            raise ValueError('MongoDB replication-lag member identity changed')
        script = """const a=db.getSiblingDB('admin');
if(!a.auth({user:process.env.MONGODB_DATABASE_ADMIN_USER,pwd:process.env.MONGODB_DATABASE_ADMIN_PASSWORD}))throw Error('auth');
db.getMongo().setReadPref('secondaryPreferred');
const s=a.runCommand({replSetGetStatus:1});if(s.ok!==1)throw Error('status');
// This diagnostic observes the isolated member, not a majority snapshot.
const result=a.getSiblingDB('appdb').runCommand({aggregate:""" + json.dumps(self.prefix+'_documents') + """,
 pipeline:[{$match:{owner:""" + json.dumps(self.prefix) + """}},{$count:'n'}],cursor:{},
 readConcern:{level:'local'},maxTimeMS:5000});
if(result.ok!==1||result.cursor.firstBatch.length>1)throw Error('local-count');
const count=result.cursor.firstBatch.length?result.cursor.firstBatch[0].n:0;
print(JSON.stringify({state:s.myState,applied:s.optimes.appliedOpTime.ts.toString(),
 wallTime:s.optimes.lastAppliedWallTime.getTime(),count}));
"""
        output = self.lab.run('-n', self.ns, 'exec', member['name'], '-c', 'mongod', '--',
                              'mongosh', '--quiet', '--norc', '--host', 'localhost', '--tls',
                              '--tlsCAFile', '/etc/mongodb-ssl/ca.crt', '--eval', script, timeout=15)
        state = json.loads(output.splitlines()[-1])
        if (state.get('state') != 2 or not isinstance(state.get('applied'), str) or
                type(state.get('wallTime')) is not int or type(state.get('count')) is not int):
            raise ValueError('MongoDB replication-lag local secondary state is not ready')
        return state

    def lag_patch(self, kind, name, uid, operations):
        self.lab.run('-n', self.ns, 'patch', kind, name, '--type=json', '-p', json.dumps([
            {'op':'test','path':'/metadata/uid','value':uid}, *operations]))

    def lag_restore(self, journal):
        """Validate every resource first; recover partial injection/cleanup safely."""
        key = 'kubeauto.io/mongodb-quorum-fault'
        policy_name = self.fixture.NAME+'-access'
        policy = self.lab.get('networkpolicy', policy_name, self.ns)
        member = journal['member'];pod = self.lab.get('pod', member['name'], self.ns)
        cr = self.lab.get('psmdb', self.fixture.NAME, self.ns)
        policies = self.lab.get('networkpolicies', ns=self.ns)['items']
        deny = next((p for p in policies if p['metadata']['name']==journal['deny']['metadata']['name']), None)
        if (not re.fullmatch(r'mongodb_gate_[0-9a-f]{16}', journal['run_id']) or
                self.lab.verify()['cluster_uid'] != journal['cluster_uid'] or not cr or
                cr['metadata']['uid'] != journal['cr_uid'] or not self.product.owned(cr) or
                self.capacity_claims() != journal['claims'] or not policy or
                policy['metadata']['uid'] != journal['policy_uid'] or not self.product.owned(policy) or
                policy['spec'] not in (journal['original_spec'],journal['modified_spec']) or
                not pod or pod['metadata']['uid'] != member['uid'] or
                pod['metadata'].get('labels',{}).get(key) not in (None,journal['run_id']) or
                any(p['metadata']['name'] not in (policy_name,journal['deny']['metadata']['name']) for p in policies) or
                (deny and (deny['metadata']['uid'] != journal.get('deny_uid') or
                    deny['metadata'].get('labels',{}).get('kubeauto.io/test-run') != journal['run_id'] or
                    {'ingress': [], 'egress': [], **deny['spec']} !=
                    {'ingress': [], 'egress': [], **journal['deny']['spec']}))):
            raise ValueError('MongoDB replication-lag recovery refuses changed fault graph')
        if policy['spec'] != journal['original_spec']:
            self.lag_patch('networkpolicy',policy_name,journal['policy_uid'],[
                {'op':'test','path':'/spec','value':journal['modified_spec']},
                {'op':'replace','path':'/spec','value':journal['original_spec']}])
        if pod['metadata'].get('labels',{}).get(key) == journal['run_id']:
            self.lag_patch('pod',member['name'],member['uid'],[
                {'op':'test','path':'/metadata/labels/kubeauto.io~1mongodb-quorum-fault','value':journal['run_id']},
                {'op':'remove','path':'/metadata/labels/kubeauto.io~1mongodb-quorum-fault'}])
        if deny:
            options = {'apiVersion':'v1','kind':'DeleteOptions','preconditions':{
                'uid':journal['deny_uid'],'resourceVersion':deny['metadata']['resourceVersion']}}
            result = subprocess.run([*self.lab.command,'delete','--raw','/apis/networking.k8s.io/v1/namespaces/'+
                self.ns+'/networkpolicies/'+deny['metadata']['name'],'-f','-'],input=json.dumps(options),
                capture_output=True,text=True,timeout=30)
            if result.returncode:raise ValueError('MongoDB replication-lag deny cleanup failed')
        self.wait_health()
        current = self.lab.get('networkpolicy',policy_name,self.ns)
        if (current['spec'] != journal['original_spec'] or
                self.lab.get('networkpolicy',journal['deny']['metadata']['name'],self.ns) or
                self.lab.get('pod',member['name'],self.ns)['metadata'].get('labels',{}).get(key)):
            raise ValueError('MongoDB replication-lag fault residue remains')
        print('MONGODB_CAPACITY_LAG_RECOVERY_PASS policy=restored fault=absent quorum=healthy',flush=True)

    def capacity_lag_recover(self):
        path = self.lab.directory/'mongodb-fault-capacity-lag.json'
        journal = json.loads(path.read_text())
        self.prefix = journal['run_id'];self.client_uid = journal['client_uid'];self.client_attempted = True
        current = self.lab.get('pod',self.client,self.ns)
        if current and (current['metadata']['uid'] != self.client_uid or
                current['metadata'].get('labels',{}).get('kubeauto.io/test-run') != self.prefix):
            raise ValueError('MongoDB replication-lag recovery refuses a foreign client')
        self.lag_restore(journal)
        if current:self.delete_client()
        try:self.client_create()
        finally:
            if self.client_uid:
                journal['client_uid'] = self.client_uid;self.fixture.write_private(path,journal)
        self.workload_started = True;self.cleanup();self.health();path.unlink()
        print('MONGODB_CAPACITY_LAG_RECOVER_SCOPED_CLEAN_PASS',flush=True)
        print('MONGODB_CAPACITY_LAG_RECOVER_PASS',flush=True)

    def capacity_lag(self, observer=None):
        path = self.lab.directory/'mongodb-fault-capacity-lag.json'
        policies = self.lab.get('networkpolicies',ns=self.ns)['items']
        if (path.exists() or len(policies) != 1 or policies[0]['metadata']['name'] != self.fixture.NAME+'-access' or
                not self.product.owned(policies[0])):
            raise ValueError('MongoDB replication-lag requires the sole product policy and no old fault')
        policy = policies[0];members = self.maintenance_members()
        output = self.js("print('MONGODB_CAPACITY_PRIMARY '+a.runCommand({hello:1}).primary);\nprint('MONGODB_CAPACITY_PRIMARY_PASS');",
                         'MONGODB_CAPACITY_PRIMARY_PASS')
        hosts = [line.split(' ',1)[1] for line in output.splitlines() if line.startswith('MONGODB_CAPACITY_PRIMARY ')]
        if len(hosts) != 1 or hosts[0].split('.',1)[0] not in {p['metadata']['name'] for p in members}:
            raise ValueError('MongoDB replication-lag did not identify the owned primary')
        target = next(p for p in members if p['metadata']['name'] != hosts[0].split('.',1)[0])
        key = 'kubeauto.io/mongodb-quorum-fault'
        if any(key in p['metadata'].get('labels',{}) for p in members):
            raise ValueError('MongoDB replication-lag refuses an existing member fault')
        modified,deny = quorum_policies(policy,self.prefix)
        journal = {'run_id':self.prefix,'client_uid':self.client_uid,'cluster_uid':self.lab.verify()['cluster_uid'],
            'cr_uid':self.lab.get('psmdb',self.fixture.NAME,self.ns)['metadata']['uid'],'claims':self.capacity_claims(),
            'policy_uid':policy['metadata']['uid'],'original_spec':policy['spec'],'modified_spec':modified,'deny':deny,
            'member':{'name':target['metadata']['name'],'uid':target['metadata']['uid']}}
        self.fixture.write_private(path,journal)
        first_error = None
        try:
            result = subprocess.run([*self.lab.command,'create','-f','-','-o','json'],input=json.dumps(deny),
                                    capture_output=True,text=True,timeout=30)
            if result.returncode:raise ValueError('MongoDB replication-lag deny fixture creation failed')
            journal['deny_uid'] = json.loads(result.stdout)['metadata']['uid'];self.fixture.write_private(path,journal)
            self.lag_patch('networkpolicy',policy['metadata']['name'],journal['policy_uid'],[
                {'op':'test','path':'/spec','value':policy['spec']},{'op':'replace','path':'/spec','value':modified}])
            self.lag_patch('pod',journal['member']['name'],journal['member']['uid'],[
                {'op':'add','path':'/metadata/labels/kubeauto.io~1mongodb-quorum-fault','value':self.prefix}])
            self.restart_isolated_members([journal['member']])
            deadline = time.monotonic()+90
            while True:
                try:
                    before = self.lag_local_state(journal['member']);break
                except (ValueError,subprocess.TimeoutExpired):
                    if time.monotonic()>=deadline:raise
                    print('MONGODB_CAPACITY_LAG_HEARTBEAT stage=isolated-secondary-startup',flush=True);time.sleep(2)
            if before['count'] != 0:raise ValueError('MongoDB replication-lag run-ID data already exists')
            code = REBUILD_DATA + """
assert.commandWorked(d.runCommand({createIndexes:prefix+'_documents',indexes:[{key:{value:1},name:'rebuild_value',unique:true}]}));
assert.commandWorked(d.runCommand({insert:prefix+'_documents',documents:expected,writeConcern:{w:'majority',j:true,wtimeout:15000}}));
assert.eq(512,d.getCollection(prefix+'_documents').countDocuments({owner:prefix}));
print('MONGODB_CAPACITY_LAG_COMMITTED_PASS documents=512 majority-journal=true');
"""
            self.js(code,'MONGODB_CAPACITY_LAG_COMMITTED_PASS documents=512 majority-journal=true',app=True)
            after = self.lag_local_state(journal['member'])
            if after['count'] != 0 or after['applied'] != before['applied']:
                raise ValueError('MongoDB replication-lag did not prove the isolated member stalled')
            journal['lag'] = {'before':before,'after':after,'majority_committed':512}
            self.fixture.write_private(path,journal)
            print('MONGODB_CAPACITY_LAG_STALLED_PASS absent-committed-documents=512 local-optime=unchanged',flush=True)
            if observer:observer(True)
            self.lag_restore(journal)
            self.rebuild_data([p['metadata']['name']+'.'+self.fixture.NAME+'-rs0.'+self.ns+'.svc.cluster.local:27017' for p in members])
            print('MONGODB_CAPACITY_LAG_ASSERTIONS_PASS stalled=true caught-up=true members=3 digest=verified',flush=True)
        except BaseException as exc:
            first_error = exc;raise
        finally:
            try:
                self.lag_restore(journal)
                path.unlink()
            except BaseException:
                print('MONGODB_CAPACITY_LAG_RECOVERY_FAIL journal=preserved',file=sys.stderr)
                if first_error is None:raise
        if observer:observer(False)

    def maintenance(self):
        path = self.lab.directory / 'mongodb-fault-maintenance.json'
        if path.exists():
            raise ValueError('MongoDB maintenance requires recovery of its existing journal')
        members = self.maintenance_members()
        node_name = members[0]['spec']['nodeName']
        node = self.lab.get('node', node_name)
        if node['spec'].get('unschedulable') or 'kubeauto.io/mongodb-maintenance' in node['metadata'].get('labels', {}):
            raise ValueError('MongoDB maintenance refuses an already cordoned or labelled node')
        self.maintenance_budget(1)
        claims = self.snapshot()['pvc']
        original_uids = {p['metadata']['name']: p['metadata']['uid'] for p in members}
        journal = {'cluster_uid': self.lab.verify()['cluster_uid'], 'run_id': self.prefix,
                   'node': node_name, 'node_uid': node['metadata']['uid'], 'client_uid': self.client_uid,
                   'node_recovered': False, 'members': original_uids}
        self.fixture.write_private(path, journal)
        first_error = None
        with tempfile.TemporaryFile(mode='w+b') as output:
            script = ASSERT + 'try {\n' + APP_AUTH + 'const prefix=' + json.dumps(self.prefix) + ';\n' + MAINTENANCE_WORKER
            script += "\n}catch(e){print('MONGODB_CLIENT_ERROR '+JSON.stringify({stage:'maintenance',name:e.name,code:typeof e.code==='number'?e.code:null}));quit(1);}\n"
            try:
                self.worker = subprocess.Popen([*self.lab.command, '-n', self.ns, 'exec', '-i', self.client, '--',
                    'mongosh', '--quiet', '--norc', '--nodb', '--file', '/dev/stdin'],
                    stdin=subprocess.PIPE, stdout=output, stderr=output)
                process = self.worker
                self.worker.stdin.write(script.encode());self.worker.stdin.close();self.worker.stdin = None
                deadline = time.monotonic() + 30
                while True:
                    if b'MONGODB_MAINTENANCE_WORKER_READY\n' in os.pread(output.fileno(), 1048576, 0):break
                    if process.poll() is not None or time.monotonic() >= deadline:
                        details = client_error(os.pread(output.fileno(), 1048576, 0).decode(errors='replace'))
                        raise ValueError('MongoDB maintenance workload failed to establish its baseline' + details)
                    time.sleep(1)
                self.lab.run('label', 'node', node_name, 'kubeauto.io/mongodb-maintenance=' + self.prefix)
                self.lab.run('drain', node_name, '--ignore-daemonsets', '--delete-emptydir-data',
                    '--pod-selector=app.kubernetes.io/instance=' + self.fixture.NAME + ',app.kubernetes.io/replset=rs0',
                    '--timeout=180s', '--grace-period=30', timeout=210)
                self.maintenance_budget(0)
                remaining = next(p for p in members if p['spec']['nodeName'] != node_name)
                self.maintenance_evict(remaining, dry_run=True)
                self.recover_maintenance_node(journal)
                journal['node_recovered'] = True;self.fixture.write_private(path, journal)
                self.wait_health(600)
                print('MONGODB_MAINTENANCE_DRAIN_PASS pdb=true node-restored=true', flush=True)
                for name in sorted(original_uids):
                    self.maintenance_budget(1)
                    member = next(p for p in self.maintenance_members() if p['metadata']['name'] == name)
                    old_uid = member['metadata']['uid']
                    self.maintenance_evict(member)
                    deadline = time.monotonic() + 180
                    while True:
                        current = self.lab.get('pod', name, self.ns)
                        if current and current['metadata']['uid'] != old_uid:break
                        if time.monotonic() >= deadline:
                            raise ValueError('MongoDB maintenance member replacement deadline exceeded')
                        time.sleep(2)
                    self.wait_health(600)
                    print('MONGODB_MAINTENANCE_ROLL_MEMBER_PASS pod=' + name, flush=True)
                self.stop_worker()
                output.seek(0)
                worker_output = output.read().decode(errors='replace')
                rows = [line for line in worker_output.splitlines()
                        if line.startswith('MONGODB_MAINTENANCE_LEDGER_PASS ')]
                if process.returncode or len(rows) != 1:
                    raise ValueError('MongoDB maintenance workload lost its acknowledged-write ledger' + client_error(worker_output))
                ledger = json.loads(rows[0].split(' ', 1)[1])
                if (not isinstance(ledger.get('acknowledged'), int) or ledger['acknowledged'] < 1 or
                        not isinstance(ledger.get('retryErrors'), int) or
                        not isinstance(ledger.get('sha256'), str) or len(ledger['sha256']) != 64):
                    raise ValueError('MongoDB maintenance workload ledger is malformed')
                print('MONGODB_MAINTENANCE_LEDGER_PASS ' + json.dumps(ledger, sort_keys=True), flush=True)
                if self.snapshot()['pvc'] != claims:
                    raise ValueError('MongoDB maintenance changed retained claim identities or capacity')
            except BaseException as exc:
                first_error = exc
                raise
            finally:
                try:
                    self.stop_worker()
                    if not journal['node_recovered']:
                        current = self.lab.get('node', node_name)
                        if current['metadata'].get('labels', {}).get('kubeauto.io/mongodb-maintenance') == self.prefix:
                            self.recover_maintenance_node(journal)
                        elif current['metadata']['uid'] != journal['node_uid'] or current['spec'].get('unschedulable'):
                            raise ValueError('MongoDB maintenance cannot prove original node scheduling state')
                    self.wait_health(600)
                    path.unlink()
                except BaseException:
                    print('MONGODB_MAINTENANCE_RECOVERY_FAIL journal=preserved', file=sys.stderr)
                    if first_error is None:raise

    def failover(self):
        for role in ('primary', 'secondary'):
            self.wait_health()
            script = ASSERT + APP_AUTH + 'const prefix=' + json.dumps(self.prefix) + ';\n' + FAILOVER_WORKER
            self.worker = subprocess.Popen([*self.lab.command, '-n', self.ns, 'exec', '-i', self.client, '--',
                                             'mongosh', '--quiet', '--norc', '--nodb', '--file', '/dev/stdin'],
                                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
            self.worker.stdin.write(script.encode())
            self.worker.stdin.close()
            self.worker.stdin = None
            with selectors.DefaultSelector() as selector:
                selector.register(self.worker.stdout, selectors.EVENT_READ)
                deadline = time.monotonic() + 30
                primary = None
                while time.monotonic() < deadline:
                    if not selector.select(timeout=1):
                        continue
                    line = self.worker.stdout.readline()
                    if not line:
                        break
                    if line.startswith(b'MONGODB_FAULT_WORKER_READY '):
                        primary = json.loads(line.split(b' ', 1)[1])['primary']
                        break
            if primary is None:
                raise ValueError('MongoDB fault worker did not establish its pre-fault acknowledged baseline')
            first_error = None
            try:
                journal = self.fault_member(primary, role)
                stdout, _ = self.worker.communicate(timeout=180)
                stdout = stdout.decode()
                if self.worker.returncode or not any(line.startswith('MONGODB_FAULT_WORKER_PASS ') for line in stdout.splitlines()):
                    raise ValueError('MongoDB workload failed during member fault')
                acknowledged = [int(line.split()[1]) for line in stdout.splitlines() if line.startswith('MONGODB_ACK ')]
                if acknowledged != list(range(200)):
                    raise ValueError('MongoDB acknowledged-write ledger is incomplete or duplicated')
                self.worker = None
                self.wait_health()
                old = self.lab.get('pod', journal['pod'], self.ns)
                if not old or old['metadata']['uid'] == journal['uid']:
                    raise ValueError('MongoDB member was not replaced after the injected loss')
                check = """const col=d.getCollection(prefix+'_documents');
assert.eq('acknowledged-before-fault',col.findOne({_id:'seed'}).value);
assert.eq(201,col.countDocuments({owner:prefix}));
for(let i=0;i<200;i++){assert.eq(i,col.findOne({_id:'ack-'+i}).value);}
"""
                if role == 'primary':
                    check += 'assert(c.getDB("admin").runCommand({hello:1}).primary!==' + json.dumps(primary) + ');\n'
                self.js(check + "print('MONGODB_ACK_LEDGER_PASS');", 'MONGODB_ACK_LEDGER_PASS', app=True)
                print('MONGODB_FAILOVER_ROLE_PASS role=' + role + ' acknowledged=201 replacement-uid=verified', flush=True)
            except BaseException as exc:
                first_error = exc
                raise
            finally:
                try:
                    self.stop_worker()
                    self.wait_health()
                    path = self.lab.directory / 'mongodb-fault-failover.json'
                    if path.exists():
                        saved = json.loads(path.read_text())
                        current = self.lab.get('pod', saved['pod'], self.ns)
                        if saved['run_id'] != self.prefix or not current or current['metadata']['uid'] == saved['uid']:
                            raise ValueError('MongoDB fault recovery identity is not proven')
                        path.unlink()
                except BaseException:
                    print('MONGODB_FAULT_RECOVERY_FAIL journal=preserved', file=sys.stderr)
                    if first_error is None:
                        raise

    def cleanup(self):
        self.stop_worker()
        if not self.client_attempted and self.client_uid is None:
            return
        current = self.lab.get('pod', self.client, self.ns)
        if not current and self.client_uid is None:
            return
        if not current or current['metadata'].get('labels', {}).get('kubeauto.io/test-run') != self.prefix:
            raise ValueError('MongoDB client ownership changed during scenario')
        if self.client_uid is not None and current['metadata']['uid'] != self.client_uid:
            raise ValueError('MongoDB client ownership changed during scenario')
        if self.workload_started:
            self.clean_workload()
        self.delete_client()

    def clean_workload(self):
        self.js("const d=c.getDB('appdb');for(const suffix of ['_documents','_accounts','_ledger','_types','_ttl']){const r=d.runCommand({drop:prefix+suffix});if(r.ok!==1)assert.eq(26,r.code);}\nprint('MONGODB_WORKLOAD_CLEAN_PASS');",
                'MONGODB_WORKLOAD_CLEAN_PASS')

    def execute(self, module):
        owner = self.lab.verify()
        if owner.get('healthy') is not True:
            raise ValueError('focused scenario requires a verified healthy product baseline')
        if list(self.lab.directory.glob('*fault*.json')):
            raise ValueError('focused scenario requires recovery of the owning fault journal first')
        self.health()
        before = self.snapshot() if module == 'idempotence' else None
        try:
            if module == 'idempotence':
                result = subprocess.run(['kubecli', 'setup', self.ns, '07', '--tags', 'mongodb'], cwd=self.base, timeout=2400)
                if result.returncode:
                    raise ValueError('second product execution failed')
                self.health()
                if before != self.snapshot():
                    raise ValueError('second product execution changed data/Pod/PVC/Secret identities or configuration')
                print('MONGODB_IDEMPOTENCE_ASSERTIONS_PASS', flush=True)
            else:
                self.client_create()
                self.workload_started = True
                if module == 'data':
                    self.js(DATA, 'MONGODB_DATA_ASSERTIONS_PASS documents=199 bson=true indexes=true ttl=true digest=true', app=True)
                elif module == 'transactions':
                    self.js(TRANSACTIONS, 'MONGODB_TRANSACTIONS_ASSERTIONS_PASS commit=true abort=true snapshot=true journal=true bounded-refusal=true', app=True)
                elif module == 'security':
                    self.security()
                elif module == 'failover':
                    self.failover()
                elif module == 'network-client':
                    self.network_client()
                elif module == 'network':
                    self.network()
                elif module == 'quorum':
                    self.quorum()
                elif module == 'maintenance':
                    self.maintenance()
                elif module == 'rebuild':
                    self.rebuild()
                elif module == 'rotation':
                    self.rotation()
                elif module == 'replica-scale':
                    ReplicaScale(self).run()
                elif module == 'capacity-expand':
                    self.capacity_expand()
                elif module == 'logging':
                    self.logging()
                elif module == 'capacity-pressure':
                    self.capacity_pressure()
                elif module == 'capacity-lag':
                    self.capacity_lag()
        except BaseException:
            try:
                self.cleanup()
            except (ValueError, OSError, subprocess.SubprocessError):
                print('MONGODB_SCOPED_CLEAN_FAIL reason=owning-client-recovery-required', file=sys.stderr)
            raise
        else:
            self.cleanup()
        self.health()
        marker = module.upper().replace('-', '_')
        print('MONGODB_' + marker + '_SCOPED_CLEAN_PASS', flush=True)
        print('MONGODB_' + marker + '_PASS', flush=True)


def observability_objects(name, workers, source, backup_age):
    """Pinned Prometheus/KSM; namespace discovery and node metrics, no Secrets/proxy."""
    if (not re.fullmatch(r'mongodb-observe-[0-9a-f]{16}', name) or source != 'mongodb-gate-data' or
            workers != {'mongodb-node-' + ip.rsplit('.', 1)[1]: ip
                        for ip in ('192.168.122.212', '192.168.122.165', '192.168.122.238')} or
            type(backup_age) is not int or backup_age < 1):
        raise ValueError('MongoDB monitoring fixture is outside its declared scope')
    ns = 'mongodb-gate';labels = {'kubeauto.io/component': 'mongodb-observability',
        'kubeauto.io/test-run': name.removeprefix('mongodb-observe-')}
    def obj(kind, suffix='', api='v1', namespaced=True):
        return {'apiVersion': api, 'kind': kind, 'metadata': {
            'name': name + suffix, **({'namespace': ns} if namespaced else {}), 'labels': dict(labels)}}
    resources = [obj('ServiceAccount'), obj('ServiceAccount', '-state')]
    for suffix, rules in [
            ('', [{'apiGroups': [''], 'resources': ['services', 'endpoints', 'pods'], 'verbs': ['get', 'list', 'watch']}]),
            ('-state', [{'apiGroups': ['psmdb.percona.com'], 'resources': ['perconaservermongodbbackups'],
                         'verbs': ['list', 'watch']}])]:
        role = obj('Role', suffix, 'rbac.authorization.k8s.io/v1');role['rules'] = rules
        binding = obj('RoleBinding', suffix, 'rbac.authorization.k8s.io/v1')
        binding.update(roleRef={'apiGroup': 'rbac.authorization.k8s.io', 'kind': 'Role', 'name': name + suffix},
            subjects=[{'kind': 'ServiceAccount', 'name': name + suffix, 'namespace': ns}])
        resources.extend((role, binding))
    for suffix, rules in [
            ('', [{'apiGroups': [''], 'resources': ['nodes/metrics'], 'resourceNames': sorted(workers), 'verbs': ['get']}]),
            ('-state', [{'apiGroups': ['apiextensions.k8s.io'], 'resources': ['customresourcedefinitions'],
                         'verbs': ['list', 'watch']}])]:
        role = obj('ClusterRole', suffix, 'rbac.authorization.k8s.io/v1', False);role['rules'] = rules
        binding = obj('ClusterRoleBinding', suffix, 'rbac.authorization.k8s.io/v1', False)
        binding.update(roleRef={'apiGroup': 'rbac.authorization.k8s.io', 'kind': 'ClusterRole', 'name': name + suffix},
            subjects=[{'kind': 'ServiceAccount', 'name': name + suffix, 'namespace': ns}])
        resources.extend((role, binding))
    crs = {'kind': 'CustomResourceStateMetrics', 'spec': {'resources': [{
        'groupVersionKind': {'group': 'psmdb.percona.com', 'version': 'v1', 'kind': 'PerconaServerMongoDBBackup'},
        'labelsFromPath': {'namespace': ['metadata', 'namespace'], 'backup': ['metadata', 'name'],
                          'cluster': ['spec', 'clusterName']},
        'metrics': [
            {'name': 'mongodb_backup_completed', 'help': 'Completed backup time in Unix seconds.',
             'each': {'type': 'Gauge', 'gauge': {'path': ['status', 'completed']}}},
            {'name': 'mongodb_backup_state', 'help': 'Operator backup state.',
             'each': {'type': 'StateSet', 'stateSet': {'labelName': 'state', 'path': ['status', 'state'],
                                                      'list': ['ready', 'error', 'running', 'waiting', 'rejected']}}}]}]}}
    pvc = 'namespace="mongodb-gate",persistentvolumeclaim=~"mongod-data-' + source + '-rs0-[0-2]"'
    rules = {'groups': [{'name': 'mongodb', 'rules': [
        {'alert': 'MongoDBMemberUnavailable', 'expr': 'min by(member_idx) (mongodb_members_health{job="mongodb"}) < 1', 'for': '5s'},
        # Exporter 0.53 emits BSON DateTime in milliseconds.
        {'alert': 'MongoDBReplicationLag', 'expr': 'max(mongodb_members_optimeDate{job="mongodb"}) - min(mongodb_members_optimeDate{job="mongodb"}) > 10000', 'for': '5s'},
        {'alert': 'MongoDBVolumeLowSpace', 'expr': 'min(kubelet_volume_stats_available_bytes{' + pvc +
            '} / kubelet_volume_stats_capacity_bytes{' + pvc + '}) < 0.30', 'for': '5s'},
        {'alert': 'MongoDBBackupStale', 'expr': 'time() - max(kube_customresource_mongodb_backup_completed{cluster="' +
            source + '"}) > ' + str(backup_age), 'for': '5s'}]}]}
    token = '/var/run/secrets/kubernetes.io/serviceaccount/token'
    ca = '/var/run/secrets/kubernetes.io/serviceaccount/ca.crt'
    prom = {'global': {'scrape_interval': '5s', 'evaluation_interval': '5s'},
        'rule_files': ['/etc/prometheus/alerts.yml'], 'scrape_configs': [
        {'job_name': 'mongodb', 'kubernetes_sd_configs': [{'role': 'endpoints', 'namespaces': {'names': [ns]}}],
         'relabel_configs': [
             {'source_labels': ['__meta_kubernetes_service_name'], 'regex': source + '-metrics', 'action': 'keep'},
             {'source_labels': ['__meta_kubernetes_endpoint_port_name'], 'regex': 'metrics', 'action': 'keep'},
             {'source_labels': ['__meta_kubernetes_pod_name'], 'target_label': 'pod'}]},
        {'job_name': 'mongodb-backup-state', 'static_configs': [{'targets': [name + '-state:8080']}]},
        {'job_name': 'mongodb-volume', 'scheme': 'https', 'metrics_path': '/metrics',
         'authorization': {'credentials_file': token}, 'tls_config': {'ca_file': ca},
         'static_configs': [{'targets': [address + ':10250'], 'labels': {'node': node}}
                            for node, address in sorted(workers.items())]}]}
    config = obj('ConfigMap');config['data'] = {
        'prometheus.yml': yaml.safe_dump(prom), 'alerts.yml': yaml.safe_dump(rules),
        'custom.yaml': yaml.safe_dump(crs)};resources.append(config)
    for suffix, image, args, port in [
            ('', 'prometheus:v3.13.1-distroless', ['--config.file=/etc/prometheus/prometheus.yml',
                '--storage.tsdb.path=/prometheus', '--storage.tsdb.retention.time=2h'], 9090),
            ('-state', 'kube-state-metrics:v2.18.0', ['--custom-resource-state-only=true',
                '--custom-resource-state-config-file=/etc/prometheus/custom.yaml', '--namespaces=' + ns], 8080)]:
        pod = obj('Pod', suffix);pod['metadata']['labels']['kubeauto.io/monitoring-role'] = name + suffix
        pod['spec'] = {'serviceAccountName': name + suffix, 'restartPolicy': 'Always',
            'containers': [{'name': 'monitor', 'image': 'registry.talkschool.cn:5000/mongodb-lab/brinnatt/' + image,
                'args': args, 'ports': [{'containerPort': port}],
                'volumeMounts': [{'name': 'config', 'mountPath': '/etc/prometheus', 'readOnly': True},
                                 {'name': 'data', 'mountPath': '/prometheus'}],
                'resources': {'requests': {'cpu': '100m', 'memory': '128Mi'},
                              'limits': {'cpu': '1', 'memory': '512Mi'}},
                'readinessProbe': {'httpGet': {'path': '/-/ready' if not suffix else '/healthz', 'port': port},
                                   'periodSeconds': 5}}],
            'volumes': [{'name': 'config', 'configMap': {'name': name}}, {'name': 'data', 'emptyDir': {}}]}
        service = obj('Service', suffix);service['spec'] = {
            'selector': {'kubeauto.io/monitoring-role': name + suffix}, 'ports': [{'port': port, 'targetPort': port}]}
        resources.extend((pod, service))
    return resources


class Observability:
    """Real scrapes, monitor privileges and native fault/backup alert transitions."""
    APIS = {'Pod': ('/api/v1', 'pods', True), 'Service': ('/api/v1', 'services', True),
        'ServiceAccount': ('/api/v1', 'serviceaccounts', True), 'ConfigMap': ('/api/v1', 'configmaps', True),
        **{kind: ('/apis/rbac.authorization.k8s.io/v1', plural, kind.startswith('Role'))
           for kind, plural in [('Role', 'roles'), ('RoleBinding', 'rolebindings'),
                                ('ClusterRole', 'clusterroles'), ('ClusterRoleBinding', 'clusterrolebindings')]}}

    def __init__(self, scenario):
        self.s = scenario
        self.path = scenario.lab.directory / 'mongodb-observability.json'
        self.state = json.loads(self.path.read_text()) if self.path.exists() else None

    def save(self):
        self.s.fixture.write_private(self.path, self.state)

    def boundary(self):
        result = self.s.snapshot();result.pop('pods')
        # Enabling a fresh backup advances CR generations. The restored spec,
        # UID, every volume and every credential must still match exactly.
        for row in result['psmdb']:row.pop('generation', None)
        return result

    def endpoint(self, obj):
        api, plural, namespaced = self.APIS[obj['kind']]
        return api + ('/namespaces/' + self.s.ns if namespaced else '') + '/' + plural + '/' + obj['metadata']['name']

    def product(self):
        result = subprocess.run(['kubecli', 'setup', self.s.ns, '07', '--tags', 'mongodb'],
                                cwd=self.s.base, timeout=2400)
        if result.returncode:raise ValueError('MongoDB monitoring product configuration failed')
        self.s.health()

    def query(self, expression):
        raw = self.s.lab.run('get', '--raw', '/api/v1/namespaces/' + self.s.ns +
            '/services/http:' + self.state['name'] + ':9090/proxy/api/v1/query?' + urlencode({'query': expression}))
        data = json.loads(raw)
        if data.get('status') != 'success' or data['data']['resultType'] != 'vector':
            raise ValueError('MongoDB monitoring query lacks a successful vector result')
        return data['data']['result']

    def wait_alerts(self, names, active, timeout=180):
        deadline = time.monotonic() + timeout
        while True:
            matches = self.query('ALERTS{alertstate="firing",alertname=~"' + '|'.join(names) + '"}')
            found = {row['metric']['alertname'] for row in matches if float(row['value'][1]) == 1}
            if (found == set(names) if active else not found):
                print('MONGODB_ALERT_TRANSITION_PASS alerts=' + ','.join(names) +
                      ' state=' + ('firing' if active else 'resolved'), flush=True)
                return
            if time.monotonic() >= deadline:raise ValueError('MongoDB real alert transition deadline exceeded')
            print('MONGODB_OBSERVABILITY_HEARTBEAT alerts=' + ','.join(names) +
                  ' awaiting=' + ('firing' if active else 'resolved'), flush=True)
            time.sleep(5)

    def scrape(self):
        expressions = {
            'database': 'count(up{job="mongodb"} == 1)',
            'member': 'count(mongodb_members_health{job="mongodb"})',
            'lag': 'count(mongodb_members_optimeDate{job="mongodb"})',
            'volume': 'count(kubelet_volume_stats_capacity_bytes{namespace="mongodb-gate",persistentvolumeclaim=~"mongod-data-mongodb-gate-data-rs0-[0-2]"})',
            'backup': 'count(kube_customresource_mongodb_backup_completed{cluster="mongodb-gate-data"})'}
        deadline = time.monotonic() + 180
        while True:
            try:
                counts = {name: self.query(expr) for name, expr in expressions.items()}
                values = {name: float(rows[0]['value'][1]) if len(rows) == 1 else 0
                          for name, rows in counts.items()}
                if (values['database'] == 3 and values['member'] == 9 and values['lag'] == 9 and
                        values['volume'] >= 3 and values['backup'] >= 1):
                    print('MONGODB_OBSERVABILITY_SCRAPE_ASSERTIONS_PASS ' + json.dumps(values, sort_keys=True), flush=True)
                    return
            except (ValueError, subprocess.SubprocessError):
                if time.monotonic() >= deadline:raise
            if time.monotonic() >= deadline:raise ValueError('MongoDB real scrape coverage is incomplete')
            print('MONGODB_OBSERVABILITY_HEARTBEAT stage=real-scrape', flush=True);time.sleep(5)

    def client(self):
        self.s.client_create();self.state['client_uid'] = self.s.client_uid;self.save()

    def clean_client(self):
        self.state['client_deletion_authorized'] = True;self.save()
        self.s.cleanup()
        self.state['client_uid'] = None;self.state.pop('client_deletion_authorized', None);self.save()

    def privileges(self):
        self.client();self.s.workload_started = True
        try:
            self.s.js("""const c=new Mongo(process.env.MONGODB_URI),a=c.getDB('admin');
assert(a.auth({user:process.env.MONGODB_CLUSTER_MONITOR_USER,pwd:process.env.MONGODB_CLUSTER_MONITOR_PASSWORD}));
assert.commandWorked(a.runCommand({serverStatus:1}));assert.commandWorked(a.runCommand({replSetGetStatus:1}));
const roles=assert.commandWorked(a.runCommand({connectionStatus:1})).authInfo.authenticatedUserRoles;
assert.eq(['admin.clusterMonitor','admin.directShardOperations','admin.explainRole','local.read'],
 roles.map(r=>r.db+'.'+r.role).sort());
// Operator 1.23.1 explainRole grants find across databases, including pbmConfig.
// Prove authorization with an impossible _id filter; never return stored values.
for(const [database,collection] of [['appdb',prefix+'_documents'],['admin','pbmConfig']]){
 const r=assert.commandWorked(c.getDB(database).runCommand({find:collection,filter:{_id:{$exists:false}},limit:1}));
 assert.eq([],r.cursor.firstBatch);
}
let rejected=false;
try {c.getDB('appdb').runCommand({insert:prefix+'_documents',documents:[{_id:'forbidden'}]});}
catch(e){assert.eq('MongoServerError',e.name);assert.eq(13,e.code);rejected=true;}
assert(rejected);
print('MONGODB_MONITOR_PRIVILEGES_PASS roles=upstream-exact diagnostics-and-find=allowed application-write=denied');
""", 'MONGODB_MONITOR_PRIVILEGES_PASS roles=upstream-exact diagnostics-and-find=allowed application-write=denied', authenticate=False)
        finally:self.clean_client()

    def fault(self, kind):
        names = ('MongoDBMemberUnavailable', 'MongoDBReplicationLag') if kind == 'lag' else ('MongoDBVolumeLowSpace',)
        self.wait_alerts(names, False)
        self.client();self.s.workload_started = True
        try:
            operation = self.s.capacity_lag if kind == 'lag' else self.s.capacity_pressure
            operation(observer=lambda active: self.wait_alerts(names, active))
        finally:
            # Fault methods restore their own graph before workload deletion.
            if list(self.s.lab.directory.glob('*fault*.json')):
                raise ValueError('MongoDB monitoring fault requires its owning recovery')
            self.clean_client()

    def backup_gate(self):
        module = load(self.s.base, 'tests/helpers/mongodb-backups.py', 'mongodb_observability_backup')
        gate = module.BackupGate(self.s.base, 'logical')
        gate.path = self.s.lab.directory / 'mongodb-observability-backup.json'
        if self.state:
            gate.run_id = self.state['backup_run_id']
            if gate.path.exists() and json.loads(gate.path.read_text())['run_id'] != gate.run_id:
                raise ValueError('MongoDB monitoring refuses a foreign backup journal')
        return gate

    def backup(self):
        self.wait_alerts(('MongoDBBackupStale',), True, timeout=240)
        gate = self.backup_gate()
        gate.execute()
        self.wait_alerts(('MongoDBBackupStale',), False)
        print('MONGODB_OBSERVABILITY_BACKUP_ASSERTIONS_PASS fresh-product-backup=true objects=sha256-verified', flush=True)

    def cleanup(self):
        if not self.path.exists():return
        self.state = json.loads(self.path.read_text());q = self.state
        if (self.path.is_symlink() or not re.fullmatch(r'mongodb-observe-[0-9a-f]{16}', q['name']) or
                self.s.lab.verify()['cluster_uid'] != q['cluster_uid']):
            raise ValueError('MongoDB monitoring cleanup refuses a foreign journal/core')
        expected = observability_objects(q['name'], q['workers'], self.s.fixture.NAME, q['backup_age'])
        if [row['document'] for row in q['resources']] != expected:
            raise ValueError('MongoDB monitoring cleanup document boundary changed')
        self.s.prefix = q['workload'];self.s.client_uid = q.get('client_uid')
        for kind in ('lag', 'pressure'):
            path = self.s.lab.directory / ('mongodb-fault-capacity-' + kind + '.json')
            if path.exists():
                fault = json.loads(path.read_text())
                if fault['run_id'] != self.s.prefix or fault['client_uid'] != self.s.client_uid:
                    raise ValueError('MongoDB monitoring cleanup refuses another fault owner')
                (self.s.capacity_lag_recover if kind == 'lag' else self.s.capacity_pressure_recover)()
                q['client_uid'] = None;self.save()
        if q.get('client_uid'):
            current = self.s.lab.get('pod', self.s.client, self.s.ns)
            if not current and not q.get('client_deletion_authorized'):
                raise ValueError('MongoDB monitoring lost its original workload client')
            if current:
                self.s.client_attempted = True;self.s.workload_started = True;self.clean_client()
        plan = []
        for row in q['resources']:
            doc = row['document'];current = self.s.lab.get(doc['kind'], doc['metadata']['name'],
                self.s.ns if self.APIS[doc['kind']][2] else '')
            if current and (not row['uid'] or current['metadata']['uid'] != row['uid'] or
                    any(current['metadata'].get('labels', {}).get(k) != v for k,v in doc['metadata']['labels'].items())):
                raise ValueError('MongoDB monitoring cleanup found a replaced or foreign resource')
            if row['uid'] and not current and not q.get('deletion_authorized'):
                raise ValueError('MongoDB monitoring fixture disappeared before cleanup authorization')
            plan.append(row)
        q['deletion_authorized'] = True;self.save()
        for row in reversed(plan):
            if row['uid']:
                doc = row['document'];self.s.lab.delete_uid(self.endpoint(doc), row['uid'])
                self.s.lab.run('wait', '--for=delete', doc['kind'] + '/' + doc['metadata']['name'],
                    *(('-n', self.s.ns) if self.APIS[doc['kind']][2] else ()), '--timeout=120s', timeout=140)
                row['uid'] = None;self.save()
        gate = self.backup_gate()
        if gate.path.exists():gate.cleanup()
        path = self.s.lab.directory / 'config.yml';current = yaml.safe_load(path.read_text())
        if current not in (q['original_config'], q['applied_config']):
            raise ValueError('MongoDB monitoring cleanup refuses concurrent configuration edits')
        path.write_text(yaml.safe_dump(q['original_config'], sort_keys=False));path.chmod(0o600);self.product()
        if self.boundary() != q['before'] or list(self.s.lab.directory.glob('*fault*.json')):
            raise ValueError('MongoDB monitoring cleanup changed source or retained a fault')
        Performance(self.s).backup_dependencies()
        self.path.unlink()
        print('MONGODB_OBSERVABILITY_SCOPED_CLEAN_PASS monitoring-rbac-clients-faults-backup-fixture=absent source=unchanged', flush=True)

    def run(self, focused=None):
        owner = self.s.lab.verify();self.s.health()
        if (self.path.exists() or list(self.s.lab.directory.glob('*fault*.json')) or
                self.backup_gate().path.exists() or self.s.lab.get('pod', self.s.client, self.s.ns)):
            raise ValueError('MongoDB monitoring requires a clean owned scope')
        config = yaml.safe_load((self.s.lab.directory / 'config.yml').read_text())
        workers = {n['metadata']['name']: next(a['address'] for a in n['status']['addresses'] if a['type'] == 'InternalIP')
            for n in self.s.lab.get('nodes')['items'] if n['metadata']['name'] in
            {'mongodb-node-' + ip.rsplit('.', 1)[1] for ip in self.s.fixture.NODES[3:]}}
        from datetime import datetime
        completed = [datetime.fromisoformat(o['status']['completed'].replace('Z', '+00:00')).timestamp()
            for o in self.s.lab.get('psmdb-backup', ns=self.s.ns)['items']
            if o['spec']['clusterName'] == self.s.fixture.NAME and o.get('status', {}).get('state') == 'ready']
        if not completed:raise ValueError('MongoDB monitoring needs a real accepted backup timestamp')
        before = self.boundary()
        name = 'mongodb-observe-' + secrets.token_hex(8)
        age = max(1, int(time.time()-max(completed))+20)
        objects = observability_objects(name, workers, self.s.fixture.NAME, age)
        applied = config | {'mongodb_monitoring_namespace': self.s.ns}
        self.state = {'name': name, 'cluster_uid': owner['cluster_uid'], 'original_config': config,
            'applied_config': applied, 'before': before, 'workload': self.s.prefix, 'client_uid': None,
            'workers': workers, 'backup_age': age, 'backup_run_id': secrets.token_hex(8),
            'resources': [{'document': o, 'uid': None} for o in objects]}
        # Refuse collisions across every API before changing the product path.
        for row in self.state['resources']:
            doc = row['document'];kind = doc['kind']
            if self.s.lab.get(kind, doc['metadata']['name'], self.s.ns if self.APIS[kind][2] else ''):
                raise ValueError('MongoDB monitoring fixture resource already exists')
        self.save();first_error = None
        try:
            path = self.s.lab.directory / 'config.yml'
            path.write_text(yaml.safe_dump(applied, sort_keys=False));path.chmod(0o600);self.product()
            for row in self.state['resources']:
                result = subprocess.run([*self.s.lab.command, 'create', '-f', '-', '-o', 'json'],
                    input=json.dumps(row['document']), capture_output=True, text=True, timeout=30)
                if result.returncode:raise ValueError('MongoDB monitoring fixture create failed')
                row['uid'] = json.loads(result.stdout)['metadata']['uid'];self.save()
            for suffix in ('', '-state'):
                self.s.lab.run('-n', self.s.ns, 'wait', '--for=condition=Ready', 'pod/' + name + suffix,
                               '--timeout=180s', timeout=200)
            self.privileges();self.scrape()
            if focused in (None, 'backup'):self.backup()
            if focused in (None, 'lag'):self.fault('lag')
            if focused in (None, 'capacity'):self.fault('capacity')
        except BaseException as exc:
            first_error = exc;raise
        finally:
            try:self.cleanup()
            except BaseException:
                print('MONGODB_OBSERVABILITY_RECOVERY_FAIL journal=preserved', file=sys.stderr)
                if first_error is None:raise
        print('MONGODB_OBSERVABILITY_' + focused.upper() + '_PASS' if focused else 'MONGODB_OBSERVABILITY_PASS', flush=True)


class WorkerRestart:
    """One real reboot of a leased worker, with durable node and boot identity."""
    def __init__(self, scenario):
        self.s = scenario
        self.path = scenario.lab.directory / 'mongodb-fault-node.json'
        self.state = json.loads(self.path.read_text()) if self.path.exists() else None

    def verify(self):
        state = self.state
        allowed = {'mongodb-node-' + ip.rsplit('.', 1)[1]: ip for ip in self.s.fixture.NODES[3:]}
        owner = json.loads(self.s.lab.owner_path.read_text())
        nodes = self.s.lab.get('nodes')['items']
        uid = self.s.fixture.core_identity(nodes, self.s.lab.get('namespace')['items'], owner, allow_unready=True)
        if (not state or self.path.is_symlink() or state['node'] not in allowed or
                state['address'] != allowed[state['node']] or
                state['run_id'] != self.s.prefix or state['client_uid'] != self.s.client_uid or
                uid != state['cluster_uid'] or {n['metadata']['name']: n['metadata']['uid'] for n in nodes} != state['node_uids'] or
                not re.fullmatch(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}', state['boot_id'])):
            raise ValueError('MongoDB worker reboot journal is outside its lease or client scope')
        node = self.s.lab.get('node', state['node'])
        addresses = [a['address'] for a in node['status']['addresses'] if a['type'] == 'InternalIP']
        if (node['metadata']['uid'] != state['node_uid'] or addresses != [state['address']] or
                self.node_spec(node) != state['node_spec']):
            raise ValueError('MongoDB reboot worker identity or scheduling configuration changed')
        return node

    @staticmethod
    def node_spec(node):
        spec = copy.deepcopy(node['spec'])
        # The node controller adds these during a reboot and removes them on
        # recovery. All customer taints and scheduling flags remain protected.
        spec['taints'] = [t for t in spec.get('taints', []) if t['key'] not in
                          ('node.kubernetes.io/not-ready', 'node.kubernetes.io/unreachable')]
        return spec

    def probe(self):
        return subprocess.run(['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=5',
            '-o', 'ServerAliveInterval=5', '-o', 'ServerAliveCountMax=1',
            'root@' + self.state['address'], 'cat /proc/sys/kernel/random/boot_id'],
            capture_output=True, text=True, timeout=15)

    def registry_ready(self):
        relay = load(self.s.base, 'tests/helpers/mongodb-registry-relay.py', 'mongodb_reboot_registry')
        row = next((r for r in relay.journal() if r['node'] == self.state['address']), None)
        try:
            if row and relay.execute([*relay.SSH, '-S', row['socket'], '-O', 'check',
                                      'root@' + row['node']], check=False).returncode:
                return False
            return relay.probe(self.state['address'])
        except subprocess.TimeoutExpired:
            return False

    def start(self):
        if self.path.exists():raise ValueError('MongoDB prior worker reboot requires recovery')
        owner = self.s.lab.verify()
        members = self.s.maintenance_members()
        client = self.s.lab.get('pod', self.s.client, self.s.ns)
        if not client or client['metadata']['uid'] != self.s.client_uid:
            raise ValueError('MongoDB reboot requires the original workload client')
        member = next(p for p in members if p['spec']['nodeName'] != client['spec']['nodeName'])
        name = member['spec']['nodeName'];node = self.s.lab.get('node', name)
        address = next(ip for ip in self.s.fixture.NODES[3:] if name == 'mongodb-node-' + ip.rsplit('.', 1)[1])
        self.state = {'run_id': self.s.prefix, 'client_uid': self.s.client_uid, 'cluster_uid': owner['cluster_uid'],
            'node': name, 'address': address, 'node_uid': node['metadata']['uid'], 'node_spec': self.node_spec(node),
            'node_uids': {n['metadata']['name']:n['metadata']['uid'] for n in self.s.lab.get('nodes')['items']},
            'boot_id': '00000000-0000-0000-0000-000000000000', 'issued': False}
        self.verify()
        result = self.probe()
        if result.returncode or not re.fullmatch(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}', result.stdout.strip()):
            raise ValueError('MongoDB reboot could not read the original boot identity')
        self.state['boot_id'] = result.stdout.strip()
        self.s.fixture.write_private(self.path, self.state)
        self.verify()
        self.state['issued'] = True;self.s.fixture.write_private(self.path, self.state)
        result = subprocess.run(['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=5', 'root@' + address,
            "nohup sh -c 'sleep 2; systemctl reboot' >/dev/null 2>&1 </dev/null &"],
            capture_output=True, text=True, timeout=20)
        if result.returncode:raise ValueError('MongoDB worker reboot request has an ambiguous result; journal retained')
        print('MONGODB_WORKER_REBOOT_REQUESTED node=' + name, flush=True)

    def recover(self):
        if not self.path.exists():return
        self.state = json.loads(self.path.read_text())
        deadline = time.monotonic() + 300
        while True:
            node = self.verify()
            try:result = self.probe()
            except subprocess.TimeoutExpired:result = None
            boot = result.stdout.strip() if result and result.returncode == 0 else ''
            if (re.fullmatch(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}', boot) and
                    (boot != self.state['boot_id'] or not self.state['issued']) and
                    not any(t['key'] in ('node.kubernetes.io/not-ready','node.kubernetes.io/unreachable')
                            for t in node['spec'].get('taints', [])) and
                    self.s.product.ready(node) and self.registry_ready()):
                self.s.wait_health(timeout=420)
                self.path.unlink()
                print('MONGODB_WORKER_REBOOT_PASS boot-id=changed node-uid-and-flags=retained' if self.state['issued'] else
                      'MONGODB_WORKER_REBOOT_CLEAN_PASS reboot=not-issued', flush=True)
                return
            if time.monotonic() >= deadline:raise ValueError('MongoDB worker reboot recovery deadline exceeded')
            print('MONGODB_WORKER_REBOOT_HEARTBEAT node=' + self.state['node'], flush=True)
            time.sleep(5)


class Performance:
    """Fixed workload, measured latency and independently re-read write ledger."""
    def __init__(self, scenario):
        self.s = scenario
        self.restart = WorkerRestart(scenario)
        self.path = scenario.lab.directory / 'mongodb-performance.json'

    def backup_dependencies(self):
        backups = load(self.s.base, 'tests/helpers/mongodb-backups.py', 'mongodb_performance_backups')
        for kind in ('logical', 'physical', 'pitr'):
            path = self.s.lab.directory / ('mongodb-backup-' + kind + '.json')
            if not path.exists():continue
            if path.is_symlink():raise ValueError('MongoDB performance refuses a linked backup journal')
            gate = backups.BackupGate(self.s.base, 'logical' if kind == 'pitr' else kind, pitr=kind == 'pitr')
            gate.state = json.loads(path.read_text());gate.validate_identity()
            if (any(key in gate.state for key in ('client_recovery', 'target', 'original_config')) or
                    gate.state.get('backup') is None):
                raise ValueError('MongoDB performance backup fixture requires its owning recovery')
            for row in gate.state['resources']:
                if row['kind'] != 'pod':continue
                pod = self.s.lab.get('pod', row['name'], self.s.ns)
                if (not pod or pod['metadata']['uid'] != row['uid'] or not gate.owned(pod) or
                        pod['spec']['restartPolicy'] != 'Always'):
                    raise ValueError('MongoDB performance requires reboot-safe owned backup fixture Pods')
                self.s.lab.run('-n', self.s.ns, 'wait', '--for=condition=Ready', 'pod/' + row['name'], '--timeout=180s', timeout=200)
            gate.mc('ready', 'gate');gate.request_for_cleanup()
            if gate.verify_objects(persist=False) != gate.state['backup']:
                raise ValueError('MongoDB performance backup dependency data changed')
        print('MONGODB_PERFORMANCE_BACKUP_DEPENDENCIES_PASS owned-pods=ready retained-objects=checksums-verified', flush=True)

    def recover(self, *, interrupted=False):
        if not self.path.exists():return
        state = json.loads(self.path.read_text())
        if (self.path.is_symlink() or not re.fullmatch(r'mongodb_gate_[0-9a-f]{16}',state['run_id'])):
            raise ValueError('MongoDB performance recovery journal is invalid')
        self.s.prefix = state['run_id'];self.s.client_uid = state.get('client_uid')
        current = self.s.lab.get('pod',self.s.client,self.s.ns)
        if current and (not self.s.client_uid or current['metadata']['uid'] != self.s.client_uid or
                current['metadata'].get('labels',{}).get('kubeauto.io/test-run') != self.s.prefix):
            raise ValueError('MongoDB performance recovery refuses an unrecorded client')
        self.s.stop_worker()
        if self.s.client_uid != state.get('client_uid'):
            state['client_uid']=self.s.client_uid;self.s.fixture.write_private(self.path,state)
        current=self.s.lab.get('pod',self.s.client,self.s.ns)
        self.restart.recover()
        self.backup_dependencies()
        if self.s.lab.verify()['cluster_uid'] != state['cluster_uid']:
            raise ValueError('MongoDB performance core identity changed')
        self.s.health();before_cleanup=self.s.snapshot();before_cleanup.pop('pods')
        if before_cleanup != state['before']:
            raise ValueError('MongoDB performance recovery source identity changed before data cleanup')
        self.s.client_attempted = bool(current)
        if interrupted and current:
            self.s.delete_client();current=None
            state['client_uid']=None;self.s.fixture.write_private(self.path,state)
        if state.get('data_started'):
            if not current:
                self.s.client_create();state['client_uid']=self.s.client_uid
                self.s.fixture.write_private(self.path,state)
            self.s.clean_workload()
        self.s.delete_client()
        self.s.health();after=self.s.snapshot();after.pop('pods')
        if after != state['before']:
            raise ValueError('MongoDB performance recovery changed source volumes, credentials or configuration')
        self.path.unlink()
        print('MONGODB_PERFORMANCE_SCOPED_CLEAN_PASS data-client-fault=absent source=retained',flush=True)

    def resources(self):
        members = self.s.maintenance_members();result = {}
        client=self.s.lab.get('pod',self.s.client,self.s.ns)
        if not client or client['metadata']['uid']!=self.s.client_uid:
            raise ValueError('MongoDB performance resource sample lost its client identity')
        selected=members+[client]
        for node in sorted({p['spec']['nodeName'] for p in selected}):
            summary = json.loads(self.s.lab.run('get', '--raw', '/api/v1/nodes/' + node + '/proxy/stats/summary'))
            for item in summary.get('pods', []):
                ref = item['podRef']
                member = next((p for p in selected if p['metadata']['name'] == ref['name'] and ref['namespace'] == self.s.ns), None)
                if member is None:continue
                if ref['uid'] != member['metadata']['uid']:raise ValueError('MongoDB resource sample has a stale member UID')
                result[ref['name']] = {'uid': ref['uid'], 'node': node,
                    'cpu_ns': sum(c['cpu']['usageCoreNanoSeconds'] for c in item['containers']),
                    'memory_bytes': sum(c['memory']['workingSetBytes'] for c in item['containers']),
                    'pvc_used_bytes': sum(v['usedBytes'] for v in item.get('volume', []) if 'pvcRef' in v),
                    'rx_bytes': item['network']['rxBytes'], 'tx_bytes': item['network']['txBytes']}
        if len(result) != 4 or any(result[p['metadata']['name']]['pvc_used_bytes'] <= 0 for p in members):
            raise ValueError('MongoDB performance lacks three member/volume and client resource samples')
        return result

    def phase(self, settings, *, inject=None):
        code = 'const settings=' + json.dumps(settings) + ';\n' + PERFORMANCE_WORKER
        if settings['fault']:
            script = ASSERT + 'async function run(){try{const prefix=' + json.dumps(self.s.prefix) + ';\n' + code
            script += "\n}catch(e){print('MONGODB_CLIENT_ERROR '+JSON.stringify({stage:'performance',name:e.name,code:typeof e.code==='number'?e.code:null}));quit(1);}}\nrun();\n"
            # Recovery can outlast the workload. Spool both streams so a large
            # latency result cannot fill a pipe and stall kubectl's exec stream.
            with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
                def read(stream):
                    return os.pread(stream.fileno(), os.fstat(stream.fileno()).st_size, 0).decode(errors='replace')
                try:
                    self.s.worker = subprocess.Popen([*self.s.lab.command, '-n', self.s.ns, 'exec', '-i', self.s.client, '--',
                        'mongosh', '--quiet', '--norc', '--nodb', '--file', '/dev/stdin'],
                        stdin=subprocess.PIPE, stdout=stdout, stderr=stderr, bufsize=0)
                    self.s.worker.stdin.write(script.encode());self.s.worker.stdin.close();self.s.worker.stdin = None
                    deadline = time.monotonic() + 90
                    while b'MONGODB_PERFORMANCE_WORKER_READY' not in os.pread(stdout.fileno(), 4096, 0).splitlines():
                        if self.s.worker.poll() is not None:
                            raise ValueError('MongoDB fault performance worker failed before readiness')
                        if time.monotonic() >= deadline:raise ValueError('MongoDB fault performance warmup deadline exceeded')
                        time.sleep(0.1)
                    if inject is None:
                        self.restart.start();self.restart.recover()
                    else:inject()
                    self.s.worker.communicate(timeout=settings['seconds'] + 150)
                    output = read(stdout)
                    if self.s.worker.returncode:
                        raise ValueError('MongoDB fault performance client failed' + client_error(output))
                except BaseException:
                    self.s.save_client_failure(argparse.Namespace(stdout=read(stdout), stderr=read(stderr)))
                    raise
                self.s.worker = None
        else:
            output = self.s.js(code, 'MONGODB_PERFORMANCE_WORKER_PASS', authenticate=False, asynchronous=True)
        if settings.get('shared_ca'):
            marker = 'MONGODB_CLIENT_CA_STORE_PASS trust=fixture-only'
            if output.splitlines().count(marker) != 1:
                raise ValueError('MongoDB stability client CA store verification is missing or duplicated')
            print(marker, flush=True)
        if settings.get('memory_profile'):
            for line in output.splitlines():
                if line.startswith('MONGODB_CLIENT_MEMORY '):
                    print(line, flush=True)
        result = performance_result(output, settings)
        # Exact per-worker sequences bind client confirmations to durable data.
        check = 'const result=' + json.dumps(result) + ";const d=c.getDB('appdb'),col=d.getCollection(prefix+'_ledger');\n"
        check += """let total=0;
for(const w of result.workers){
 const rows=col.find({phase:result.phase,worker:w.worker}).sort({sequence:1}).batchSize(256);
 let count=0;
 while(rows.hasNext()){
  const row=rows.next();
  assert.eq(result.phase+'-'+w.worker+'-'+count,row._id);assert.eq(prefix,row.owner);assert.eq(count,row.sequence);
  count++;
 }
 assert.eq(w.acknowledged,count);
 total+=count;
}
assert.eq(result.writes,total);assert.eq(4096,d.getCollection(prefix+'_documents').countDocuments({}));
print('MONGODB_PERFORMANCE_LEDGER_PASS');
"""
        self.s.js(check, 'MONGODB_PERFORMANCE_LEDGER_PASS')
        print('MONGODB_PERFORMANCE_PHASE_PASS ' + json.dumps(result, sort_keys=True), flush=True)
        return result

    def run(self, *, focused=False, node_only=False):
        self.s.lab.verify();self.s.health()
        if self.path.exists() or list(self.s.lab.directory.glob('*fault*.json')):
            raise ValueError('MongoDB performance requires all prior faults and workload recovered')
        if not focused:self.backup_dependencies()
        before = self.s.snapshot();before.pop('pods')
        state={'run_id':self.s.prefix,'cluster_uid':self.s.lab.verify()['cluster_uid'],'before':before,'client_uid':None,'data_started':False}
        self.s.fixture.write_private(self.path,state)
        results = [];first_error=None
        try:
            self.s.client_create();state['client_uid']=self.s.client_uid;state['data_started']=True
            self.s.fixture.write_private(self.path,state)
            seed = """const documents=Array.from({length:4096},(_,i)=>({_id:i,payload:'x'.repeat(1024)}));
assert.commandWorked(d.runCommand({insert:prefix+'_documents',documents,writeConcern:{w:'majority',j:true,wtimeout:10000}}));
print('MONGODB_PERFORMANCE_SEED_PASS');"""
            self.s.js(seed, 'MONGODB_PERFORMANCE_SEED_PASS', app=True)
            resources_before = self.resources()
            for concurrency in (() if node_only else (1, 4, 16)):
                results.append(self.phase({'phase':'steady-'+str(concurrency),'concurrency':concurrency,'seconds':3 if focused else 30,'fault':False}))
            resources_after = self.resources()
            for name, row in resources_after.items():
                if row['uid'] != resources_before[name]['uid'] or row['cpu_ns'] < resources_before[name]['cpu_ns']:
                    raise ValueError('MongoDB steady performance resource counters reset')
            if not focused:results.append(self.phase({'phase':'node-loss','concurrency':4,'seconds':180,'fault':True}))
        except BaseException as exc:
            first_error=exc
            raise
        finally:
            try:self.recover()
            except BaseException:
                print('MONGODB_PERFORMANCE_RECOVERY_FAIL journal=preserved',file=sys.stderr)
                if first_error is None:raise
        self.s.health();after = self.s.snapshot();after.pop('pods')
        if before != after:raise ValueError('MongoDB performance changed source volumes, credentials or configuration')
        print('MONGODB_PERFORMANCE_PROFILE ' + json.dumps({'dataset_documents':4096,'payload_bytes':1024,
            'read_write_mix':[80,20],'tls':True,'write_concern':'majority+j','phases':results,
            'resource_before':resources_before,'resource_after':resources_after,
            'scope':'lab baseline; network-backed replicated CSI and client CPU included; no production throughput guarantee'},sort_keys=True),flush=True)
        print('MONGODB_PERFORMANCE_NODE_PASS' if node_only else
              'MONGODB_PERFORMANCE_CLIENT_PASS' if focused else 'MONGODB_PERFORMANCE_PASS', flush=True)


class OwnershipPVC:
    """Use a real independent mounted volume to disprove implicit PVC adoption."""
    deployment = 'mongodb-ownership-gate'

    def __init__(self, scenario):
        self.s = scenario
        self.path = scenario.lab.directory / 'mongodb-ownership-pvc.json'
        self.directory = scenario.base / 'clusters' / self.deployment
        self.state = json.loads(self.path.read_text()) if self.path.exists() else None

    def save(self):
        self.s.fixture.write_private(self.path, self.state)

    def verify(self):
        state = self.state
        if (not state or self.path.is_symlink() or self.directory.is_symlink() or
                self.s.lab.verify()['cluster_uid'] != state['cluster_uid'] or
                not re.fullmatch('[0-9a-f]{16}', state['run_id']) or
                state['name'] != 'mongodb-pvc-' + state['run_id'] or
                state['claim'] != 'mongod-data-' + state['name'] + '-rs0-0' or
                state['pod'] != 'mongodb-pvc-probe-' + state['run_id']):
            raise ValueError('MongoDB PVC ownership fixture journal changed')
        if self.s.lab.get('psmdb', state['name'], self.s.ns):
            raise ValueError('MongoDB PVC ownership request unexpectedly created a database; preserve volume')
        for kind in ('pod', 'pvc'):
            obj = self.s.lab.get(kind, state['pod'] if kind == 'pod' else state['claim'], self.s.ns)
            row = state.get('records', {}).get(kind)
            if obj and (not row or obj['metadata']['uid'] != row['uid'] or
                    obj['metadata'].get('labels', {}) != state['labels']):
                raise ValueError('MongoDB PVC ownership fixture was replaced or relabelled')
            if kind == 'pvc' and obj and row.get('volume') and obj['spec'].get('volumeName') != row['volume']:
                raise ValueError('MongoDB PVC ownership fixture binding changed')
            if kind == 'pvc' and obj and not row.get('volume') and obj['spec'].get('volumeName'):
                row['volume'] = obj['spec']['volumeName'];self.save()
        if state.get('records', {}).get('pvc', {}).get('volume'):
            pv = self.s.lab.get('pv', state['records']['pvc']['volume'])
            if pv:
                spec = pv['spec'];ref = spec.get('claimRef', {})
                if (spec.get('storageClassName') != 'mongodb-longhorn' or
                        spec.get('persistentVolumeReclaimPolicy') != 'Delete' or
                        spec.get('csi', {}).get('driver') != 'driver.longhorn.io' or
                        (ref.get('namespace'), ref.get('name'), ref.get('uid')) !=
                        (self.s.ns, state['claim'], state['records']['pvc']['uid'])):
                    raise ValueError('MongoDB PVC ownership fixture PV was rebound')

    def cleanup(self):
        self.verify()
        for kind, plural in (('pod', 'pods'), ('pvc', 'persistentvolumeclaims')):
            name = self.state['pod'] if kind == 'pod' else self.state['claim']
            if self.s.lab.get(kind, name, self.s.ns):
                self.s.lab.delete_uid('/api/v1/namespaces/' + self.s.ns + '/' + plural + '/' + name,
                    self.state['records'][kind]['uid'])
                self.s.lab.run('-n', self.s.ns, 'wait', '--for=delete', kind + '/' + name, '--timeout=300s', timeout=320)
        volume = self.state.get('records', {}).get('pvc', {}).get('volume')
        if volume:self.s.lab.run('wait', '--for=delete', 'pv/' + volume, '--timeout=300s', timeout=320)
        if any(self.s.lab.get(kind, self.state[key], self.s.ns) for kind,key in (('pod','pod'),('pvc','claim'))) or volume and self.s.lab.get('pv', volume):
            raise ValueError('MongoDB PVC ownership fixtures remain after cleanup')
        self.s.health()
        if self.s.snapshot() != self.state['before']:
            raise ValueError('MongoDB PVC ownership test changed the accepted source or retained fixtures')
        if self.directory.exists():shutil.rmtree(self.directory)
        self.path.unlink()
        print('MONGODB_OWNERSHIP_PVC_SCOPED_CLEAN_PASS independent-fixtures=absent source=unchanged', flush=True)

    def run(self):
        self.s.health()
        if self.path.exists() or self.directory.exists() or list(self.s.lab.directory.glob('*fault*.json')):
            raise ValueError('MongoDB PVC ownership test requires clean owned scope')
        run_id = secrets.token_hex(8);name = 'mongodb-pvc-' + run_id
        labels = {'app.kubernetes.io/managed-by': 'independent-volume-owner', 'kubeauto.io/test-run': run_id}
        self.state = {'run_id': run_id, 'name': name, 'claim': 'mongod-data-' + name + '-rs0-0',
            'pod': 'mongodb-pvc-probe-' + run_id, 'labels': labels,
            'cluster_uid': self.s.lab.verify()['cluster_uid'], 'before': self.s.snapshot(), 'records': {}}
        self.save();first_error = None
        try:
            result = subprocess.run(['kubecli', 'new', self.deployment], cwd=self.s.base, timeout=120)
            if result.returncode:raise ValueError('MongoDB PVC ownership deployment preparation failed')
            config = yaml.safe_load((self.s.lab.directory / 'config.yml').read_text())
            config.update(CLUSTER_NAME=self.deployment, mongodb_name=name, mongodb_requests=[],
                mongodb_backup_enabled=False, mongodb_pitr_enabled=False)
            self.directory.joinpath('config.yml').write_text(yaml.safe_dump(config, sort_keys=False))
            self.directory.joinpath('hosts').write_text(self.s.fixture.inventory(self.deployment))
            shutil.copyfile(self.s.lab.directory / 'kubectl.kubeconfig', self.directory / 'kubectl.kubeconfig')
            for filename in ('config.yml', 'hosts', 'kubectl.kubeconfig'):self.directory.joinpath(filename).chmod(0o600)
            claim = {'apiVersion': 'v1', 'kind': 'PersistentVolumeClaim',
                'metadata': {'name': self.state['claim'], 'namespace': self.s.ns, 'labels': labels},
                'spec': {'storageClassName': 'mongodb-longhorn', 'accessModes': ['ReadWriteOnce'],
                    'resources': {'requests': {'storage': '1Gi'}}}}
            pod = {'apiVersion': 'v1', 'kind': 'Pod',
                'metadata': {'name': self.state['pod'], 'namespace': self.s.ns, 'labels': labels},
                'spec': {'restartPolicy': 'Never', 'automountServiceAccountToken': False,
                    'nodeSelector': {'kubernetes.io/hostname': 'mongodb-node-212'},
                    'securityContext': {'runAsUser': 1001, 'runAsGroup': 1001, 'fsGroup': 1001},
                    'containers': [{'name': 'owner',
                        'image': 'registry.talkschool.cn:5000/brinnatt/percona-server-mongodb:8.0.32-14',
                        'command': ['bash', '-ec', 'printf %s ' + run_id + ' > /owner/sentinel; trap "exit 0" TERM INT; sleep 3600 & wait'],
                        'resources': {'requests': {'cpu': '10m', 'memory': '32Mi'}, 'limits': {'cpu': '200m', 'memory': '128Mi'}},
                        'volumeMounts': [{'name': 'owner', 'mountPath': '/owner'}]}],
                    'volumes': [{'name': 'owner', 'persistentVolumeClaim': {'claimName': self.state['claim']}}]}}
            for kind,obj in (('pvc',claim),('pod',pod)):
                if self.s.lab.get(kind, obj['metadata']['name'], self.s.ns):
                    raise ValueError('MongoDB PVC ownership refuses pre-existing independent fixture')
                result = subprocess.run([*self.s.lab.command, 'create', '-f', '-', '-o', 'json'],
                    input=json.dumps(obj), capture_output=True, text=True, timeout=30)
                if result.returncode:raise ValueError('MongoDB independent volume owner fixture failed')
                self.state['records'][kind] = {'uid': json.loads(result.stdout)['metadata']['uid']};self.save()
            self.s.lab.run('-n', self.s.ns, 'wait', '--for=condition=Ready', 'pod/' + self.state['pod'], '--timeout=300s', timeout=320)
            self.state['records']['pvc']['volume'] = self.s.lab.get('pvc', self.state['claim'], self.s.ns)['spec']['volumeName'];self.save()
            self.verify()
            before = self.s.lab.get('pvc', self.state['claim'], self.s.ns)
            sentinel = self.s.lab.run('-n', self.s.ns, 'exec', self.state['pod'], '--', 'cat', '/owner/sentinel')
            if sentinel != run_id:raise ValueError('MongoDB independent volume owner data missing before request')
            result = subprocess.run(['kubecli', 'setup', self.deployment, '07', '--tags', 'mongodb'],
                cwd=self.s.base, capture_output=True, text=True, timeout=240)
            if result.returncode == 0 or 'refusing to adopt an existing or foreign MongoDB PVC' not in result.stdout + result.stderr:
                raise ValueError('MongoDB product did not explicitly refuse independent PVC before installation')
            self.verify()
            after = self.s.lab.get('pvc', self.state['claim'], self.s.ns)
            if before != after or self.s.lab.run('-n', self.s.ns, 'exec', self.state['pod'], '--', 'cat', '/owner/sentinel') != sentinel:
                raise ValueError('MongoDB product changed the independent PVC or its data')
            print('MONGODB_OWNERSHIP_PVC_REFUSAL_PASS product-rc=nonzero foreign-pvc-and-data=unchanged database=absent', flush=True)
        except BaseException as exc:
            first_error = exc
            raise
        finally:
            try:self.cleanup()
            except BaseException:
                print('MONGODB_OWNERSHIP_PVC_RECOVERY_FAIL journal=preserved', file=sys.stderr)
                if first_error is None:raise
        print('MONGODB_OWNERSHIP_PVC_PASS', flush=True)


class OwnershipResources:
    """Independent owners exercise product refusal and shared-CRD cleanup guards."""
    deployment = 'mongodb-ownership-gate'

    def __init__(self, scenario):
        self.s = scenario
        self.path = scenario.lab.directory / 'mongodb-ownership-resources.json'
        self.shadow = scenario.lab.directory / 'mongodb-ownership-shadow.json'
        self.directory = scenario.base / 'clusters' / self.deployment
        self.state = json.loads(self.path.read_text()) if self.path.exists() else None

    def save(self):
        self.s.fixture.write_private(self.path, self.state)

    def endpoint(self, kind, name, namespace):
        root = self.state['name']
        allowed = {
            ('namespace', root, ''): '/api/v1/namespaces/' + root,
            ('psmdb', root, root): '/apis/psmdb.percona.com/v1/namespaces/' + root + '/perconaservermongodbs/' + root,
            ('service', root + '-metrics', self.s.ns): '/api/v1/namespaces/' + self.s.ns + '/services/' + root + '-metrics',
            ('networkpolicy', root + '-access', self.s.ns): '/apis/networking.k8s.io/v1/namespaces/' + self.s.ns + '/networkpolicies/' + root + '-access'}
        try:return allowed[(kind, name, namespace)]
        except KeyError:raise ValueError('MongoDB independent resource is outside its owner scope') from None

    def namespace_identity(self):
        row = next((r for r in self.state['resources'] if r['kind'] == 'namespace'), None) if self.state else None
        return (self.state['name'], row['uid'], self.state['labels'] | {
            'kubernetes.io/metadata.name': self.state['name']}) if row else None

    def verify(self):
        state = self.state
        if (not state or self.path.is_symlink() or self.directory.is_symlink() or self.shadow.is_symlink() or
                not re.fullmatch('[0-9a-f]{16}', state['run_id']) or
                state['name'] != 'mongodb-owner-' + state['run_id'] or
                state['labels'] != {'app.kubernetes.io/managed-by': 'independent-resource-owner', 'kubeauto.io/test-run': state['run_id']} or
                self.s.lab.verify(independent_namespace=self.namespace_identity())['cluster_uid'] != state['cluster_uid']):
            raise ValueError('MongoDB independent resource ownership journal changed')
        if self.shadow.exists() and hashlib.sha256(self.shadow.read_bytes()).hexdigest() != state.get('shadow_sha256'):
            raise ValueError('MongoDB cleanup shadow journal was changed externally')
        seen = set()
        for row in state['resources']:
            if row['kind'] in seen or row['endpoint'] != self.endpoint(row['kind'], row['name'], row['namespace']):
                raise ValueError('MongoDB independent resource deletion endpoint changed')
            seen.add(row['kind'])
            obj = self.s.lab.get(row['kind'], row['name'], row['namespace'])
            expected_labels = state['labels'] | ({'kubernetes.io/metadata.name': state['name']} if row['kind'] == 'namespace' else {})
            if obj and (obj['metadata']['uid'] != row['uid'] or
                    obj['metadata'].get('labels', {}) != expected_labels or
                    obj['metadata'].get('finalizers')):
                raise ValueError('MongoDB independent resource was replaced, relabelled or acquired finalizers')
        if self.s.lab.get('psmdb', state['name'], self.s.ns):
            raise ValueError('MongoDB product unexpectedly created the ownership target')
        if self.s.lab.get('namespace', state['name']):
            recorded = {(r['kind'], r['name'], r['uid']) for r in state['resources'] if r['namespace'] == state['name']}
            for obj in self.s.lab.namespace_objects(state['name']):
                meta = obj['metadata']
                if (obj['kind'], meta['name']) in (('ServiceAccount', 'default'), ('ConfigMap', 'kube-root-ca.crt')):
                    continue
                kind = 'psmdb' if obj['kind'] == 'PerconaServerMongoDB' else obj['kind'].lower()
                if (kind, meta['name'], meta['uid']) not in recorded:
                    raise ValueError('MongoDB independent namespace contains an unrecorded resource')

    def cleanup(self):
        self.verify()  # Authorize the complete independent graph first.
        for row in reversed(self.state['resources']):
            if self.s.lab.get(row['kind'], row['name'], row['namespace']):
                self.s.lab.delete_uid(row['endpoint'], row['uid'])
                self.s.lab.run(*(['-n', row['namespace']] if row['namespace'] else []),
                    'wait', '--for=delete', row['kind'] + '/' + row['name'], '--timeout=180s', timeout=200)
        if any(self.s.lab.get(r['kind'], r['name'], r['namespace']) for r in self.state['resources']):
            raise ValueError('MongoDB independent resources remain after owner cleanup')
        self.s.health()
        if self.s.snapshot() != self.state['before']:
            raise ValueError('MongoDB ownership refusal changed source data, credentials or identity')
        if self.shadow.exists():
            if hashlib.sha256(self.shadow.read_bytes()).hexdigest() != self.state.get('shadow_sha256'):
                raise ValueError('MongoDB cleanup shadow journal was changed externally')
            self.shadow.unlink()
        if self.directory.exists():shutil.rmtree(self.directory)
        self.path.unlink()
        print('MONGODB_OWNERSHIP_RESOURCES_SCOPED_CLEAN_PASS foreign-fixtures=absent source=unchanged', flush=True)

    def create(self, kind, obj, endpoint):
        ns = obj['metadata'].get('namespace', '')
        name = obj['metadata']['name']
        if endpoint != self.endpoint(kind, name, ns):
            raise ValueError('MongoDB independent creation endpoint is outside its scope')
        if self.s.lab.get(kind, name, ns):
            raise ValueError('MongoDB independent owner refuses a pre-existing fixture')
        row = {'kind': kind, 'name': name, 'namespace': ns, 'uid': None, 'endpoint': endpoint}
        self.state['resources'].append(row);self.save()
        result = subprocess.run([*self.s.lab.command, 'create', '-f', '-', '-o', 'json'],
            input=json.dumps(obj), capture_output=True, text=True, timeout=30)
        if result.returncode:raise ValueError('MongoDB independent ownership resource creation failed')
        current = json.loads(result.stdout)
        row['uid'] = current['metadata']['uid']
        self.save()
        return current

    def reject(self, config, expected):
        path = self.directory / 'config.yml'
        path.write_text(yaml.safe_dump(config, sort_keys=False));path.chmod(0o600)
        result = subprocess.run(['kubecli', 'setup', self.deployment, '07', '--tags', 'mongodb'],
            cwd=self.s.base, capture_output=True, text=True, timeout=240)
        if result.returncode == 0 or expected not in result.stdout + result.stderr:
            raise ValueError('MongoDB ownership negative control lacks its exact product refusal')
        self.verify()

    def run(self):
        self.s.health()
        if self.path.exists() or self.directory.exists() or self.shadow.exists() or list(self.s.lab.directory.glob('*fault*.json')):
            raise ValueError('MongoDB independent ownership controls require a clean owned scope')
        self.s.lab.bind_product()
        owner = self.s.lab.verify();crd = 'perconaservermongodbs.psmdb.percona.com'
        if crd not in owner.get('created_crds', []) or crd not in owner.get('product_identity', {}).get('crds', {}):
            raise ValueError('MongoDB ownership cleanup control requires its recorded created CRD identity')
        run_id = secrets.token_hex(8);name = 'mongodb-owner-' + run_id
        self.state = {'run_id': run_id, 'name': name, 'cluster_uid': owner['cluster_uid'],
            'labels': {'app.kubernetes.io/managed-by': 'independent-resource-owner', 'kubeauto.io/test-run': run_id},
            'before': self.s.snapshot(), 'resources': []}
        self.save();first_error = None
        try:
            result = subprocess.run(['kubecli', 'new', self.deployment], cwd=self.s.base, timeout=120)
            if result.returncode:raise ValueError('MongoDB ownership deployment preparation failed')
            config = yaml.safe_load((self.s.lab.directory / 'config.yml').read_text())
            config.update(CLUSTER_NAME=self.deployment, mongodb_name=name, mongodb_requests=[],
                mongodb_backup_enabled=False, mongodb_pitr_enabled=False)
            self.directory.joinpath('hosts').write_text(self.s.fixture.inventory(self.deployment))
            shutil.copyfile(self.s.lab.directory / 'kubectl.kubeconfig', self.directory / 'kubectl.kubeconfig')
            for filename in ('hosts', 'kubectl.kubeconfig'):self.directory.joinpath(filename).chmod(0o600)
            namespace = {'apiVersion': 'v1', 'kind': 'Namespace', 'metadata': {'name': name, 'labels': self.state['labels']}}
            self.create('namespace', namespace, '/api/v1/namespaces/' + name)
            before = self.s.lab.get('namespace', name)
            self.reject(config | {'mongodb_namespace': name}, 'database namespace must be explicitly labelled for kubeauto MongoDB ownership')
            if self.s.lab.get('namespace', name) != before:
                raise ValueError('MongoDB product changed the foreign namespace')
            print('MONGODB_OWNERSHIP_NAMESPACE_PASS product-rc=nonzero foreign-object=unchanged', flush=True)
            for kind,plural,api,suffix,spec in (
                    ('service','services','/api/v1','-metrics',{'ports':[{'name':'metrics','port':9216}]}),
                    ('networkpolicy','networkpolicies','/apis/networking.k8s.io/v1','-access',
                     {'podSelector':{'matchLabels':{'app.kubernetes.io/instance':name}},'policyTypes':['Ingress'],'ingress':[]})):
                obj = {'apiVersion': 'v1' if kind == 'service' else 'networking.k8s.io/v1',
                    'kind': 'Service' if kind == 'service' else 'NetworkPolicy',
                    'metadata': {'name': name + suffix, 'namespace': self.s.ns, 'labels': self.state['labels']}, 'spec': spec}
                self.create(kind, obj, api + '/namespaces/' + self.s.ns + '/' + plural + '/' + name + suffix)
                before = self.s.lab.get(kind, name + suffix, self.s.ns)
                self.reject(config, 'refusing to adopt a foreign MongoDB ' + kind)
                if self.s.lab.get(kind, name + suffix, self.s.ns) != before:
                    raise ValueError('MongoDB product changed the foreign ' + kind)
                # Each product rejection must reach its own owning resource.
                row = self.state['resources'][-1]
                self.s.lab.delete_uid(row['endpoint'], row['uid'])
                self.s.lab.run('-n', self.s.ns, 'wait', '--for=delete', kind + '/' + row['name'], '--timeout=60s', timeout=80)
                print('MONGODB_OWNERSHIP_RESOURCE_PASS kind=' + kind + ' foreign-object=unchanged', flush=True)
            source = self.s.lab.get('psmdb', self.s.fixture.NAME, self.s.ns)
            obj = {'apiVersion': 'psmdb.percona.com/v1', 'kind': 'PerconaServerMongoDB',
                'metadata': {'name': name, 'namespace': name, 'labels': self.state['labels']},
                'spec': source['spec'] | {'pause': True}}
            before = self.create('psmdb', obj, '/apis/psmdb.percona.com/v1/namespaces/' + name + '/perconaservermongodbs/' + name)
            try:self.s.lab.cleanup_product(qualify_only=True, independent_namespace=self.namespace_identity())
            except ValueError as exc:
                if str(exc) != 'MongoDB cleanup CRD has surviving consumers in another namespace':raise
            else:raise ValueError('MongoDB cleanup accepted an external CRD consumer')
            if self.s.lab.get('psmdb', name, name) != before:
                raise ValueError('MongoDB cleanup changed the independent CRD consumer')
            print('MONGODB_OWNERSHIP_CRD_CONSUMER_PASS cleanup=refused foreign-object=unchanged', flush=True)
            # A shadow authorization journal exercises UID mismatch without
            # editing the live shared CRD or the real runner authorization.
            shadow = copy.deepcopy(owner)
            shadow['product_identity']['crds'][crd]['uid'] = 'independent-previous-crd-uid'
            self.s.fixture.write_private(self.shadow, shadow)
            self.state['shadow_sha256'] = hashlib.sha256(self.shadow.read_bytes()).hexdigest();self.save()
            row = self.state['resources'][-1]
            self.s.lab.delete_uid(row['endpoint'], row['uid'])
            self.s.lab.run('-n', name, 'wait', '--for=delete', 'psmdb/' + name, '--timeout=60s', timeout=80)
            guard = self.s.fixture.Lab(self.s.base);guard.owner_path = self.shadow
            original_crd = self.s.lab.get('crd', crd)
            try:guard.cleanup_product(qualify_only=True, independent_namespace=self.namespace_identity())
            except ValueError as exc:
                if str(exc) != 'MongoDB cleanup CRD identity or schema changed':raise
            else:raise ValueError('MongoDB cleanup accepted a changed CRD identity')
            if self.s.lab.get('crd', crd) != original_crd or json.loads(self.s.lab.owner_path.read_text()) != owner:
                raise ValueError('MongoDB cleanup identity refusal changed the live CRD or real authorization')
            # Lab.verify rewrites only the identical authorization data.
            self.state['shadow_sha256'] = hashlib.sha256(self.shadow.read_bytes()).hexdigest();self.save()
            print('MONGODB_OWNERSHIP_CRD_IDENTITY_PASS cleanup=refused live-crd=unchanged shadow-journal=true', flush=True)
        except BaseException as exc:
            first_error = exc
            raise
        finally:
            try:self.cleanup()
            except BaseException:
                print('MONGODB_OWNERSHIP_RESOURCES_RECOVERY_FAIL journal=preserved', file=sys.stderr)
                if first_error is None:raise
        print('MONGODB_OWNERSHIP_RESOURCES_PASS', flush=True)


class ReplicaScale:
    """Product member scaling with owned temporary CSI domains and recovery."""
    def __init__(self, scenario):
        self.s = scenario
        self.path = scenario.lab.directory / 'mongodb-fault-replica-scale.json'
        self.config_path = scenario.lab.directory / 'config.yml'
        storage = load(scenario.base, 'tests/helpers/mongodb-lab-storage.py', 'mongodb_scale_storage')
        self.storage = storage.Fixture(argparse.Namespace(base=scenario.base,
            kubeconfig=scenario.lab.directory / 'kubectl.kubeconfig'))
        self.domain_type = storage.ReplicaDomains
        self.state = None

    def save(self):
        self.s.fixture.write_private(self.path, self.state)

    def verify(self):
        state = json.loads(self.path.read_text())
        cr = self.s.lab.get('psmdb', self.s.fixture.NAME, self.s.ns)
        config = yaml.safe_load(self.config_path.read_text())
        if (self.path.is_symlink() or state.get('cluster_uid') != self.s.lab.verify()['cluster_uid'] or
                cr['metadata']['uid'] != state.get('source_uid') or not self.s.product.owned(cr) or
                cr['spec'].get('sharding', {}).get('enabled') or
                [(r['name'], r['size']) for r in cr['spec']['replsets']] not in ([('rs0', 3)], [('rs0', 5)]) or
                config not in (state['original_config'], state['expanded_config']) or
                not re.fullmatch(r'mongodb_gate_[0-9a-f]{16}', state.get('prefix', '')) or
                state.get('phase', 'active') not in ('active', 'client-removal', 'client-absent')):
            raise ValueError('MongoDB replica scaling refuses changed source, configuration or journal')
        allowed = {self.path.name, 'mongodb-fault-replica-domains.json'}
        if any(p.name not in allowed for p in self.s.lab.directory.glob('*fault*.json')):
            raise ValueError('MongoDB replica scaling refuses unrelated fault residue')
        self.state = state
        return cr

    def verify_members(self, size):
        self.s.js("""
const manager=new Mongo(process.env.MONGODB_URI),admin=manager.getDB('admin');
assert(admin.auth({user:process.env.MONGODB_CLUSTER_ADMIN_USER,pwd:process.env.MONGODB_CLUSTER_ADMIN_PASSWORD}));
const status=assert.commandWorked(admin.runCommand({replSetGetStatus:1}));
const config=assert.commandWorked(admin.runCommand({replSetGetConfig:1})).config;
const size=""" + str(size) + """;
const expected=Array.from({length:size},(_,i)=>'mongodb-gate-data-rs0-'+i+'.mongodb-gate-data-rs0.mongodb-gate.svc.cluster.local:27017');
assert.eq(size,status.members.length);assert.eq(size,config.members.length);
assert(status.members.every(m=>expected.includes(m.name)&&m.health===1&&[1,2].includes(m.state)));
assert(config.members.every(m=>expected.includes(m.host)&&m.votes===1&&!m.arbiterOnly));
assert.eq(1,status.members.filter(m=>m.state===1).length);
assert.eq(Math.floor(size/2)+1,status.writeMajorityCount);
print('MONGODB_REPLICA_SCALE_MEMBERS_PASS size='+size);
""", 'MONGODB_REPLICA_SCALE_MEMBERS_PASS size=' + str(size))

    def product(self, size):
        self.verify()
        self.config_path.write_text(yaml.safe_dump(self.state['expanded_config'] if size == 5 else
            self.state['original_config'], sort_keys=False));self.config_path.chmod(0o600)
        result = subprocess.run(['kubecli', 'setup', self.s.ns, '07', '--tags', 'mongodb'],
            cwd=self.s.base, timeout=5400)
        if result.returncode:
            raise ValueError('MongoDB replica member configuration failed size=' + str(size))
        self.s.health()

    def capture_claims(self):
        """The only new claims permitted were absent before the scale request."""
        self.verify()
        original = {r['spec']['volumeName'] for r in self.state['before']['pvc']}
        for ordinal in (3, 4):
            name = 'mongod-data-' + self.s.fixture.NAME + '-rs0-' + str(ordinal)
            claim = self.s.lab.get('pvc', name, self.s.ns)
            if not claim:continue
            labels = claim['metadata'].get('labels', {})
            previous = self.state['extra_claims'].get(name)
            volume = claim['spec'].get('volumeName')
            if (claim['metadata'].get('namespace') != self.s.ns or
                    labels.get('app.kubernetes.io/instance') != self.s.fixture.NAME or
                    labels.get('app.kubernetes.io/managed-by') != 'percona-server-mongodb-operator' or
                    claim['spec'].get('storageClassName') != 'mongodb-longhorn' or volume in original or
                    previous and (previous['uid'] != claim['metadata']['uid'] or
                        previous.get('volume') not in (None, volume))):
                raise ValueError('MongoDB replica scale refuses a foreign or replaced new claim')
            self.state['extra_claims'][name] = {'uid': claim['metadata']['uid'], 'volume': volume}
        self.save()

    def retire_claims(self):
        self.capture_claims()
        pods = self.s.lab.get('pods', ns=self.s.ns)['items']
        for ordinal in (3, 4):
            name = self.s.fixture.NAME + '-rs0-' + str(ordinal)
            if any(p['metadata']['name'] == name for p in pods):
                raise ValueError('MongoDB replica scale refuses retirement of an active member')
        sts = self.s.lab.get('statefulset', self.s.fixture.NAME + '-rs0', self.s.ns)
        if not sts or sts['spec']['replicas'] != 3:
            raise ValueError('MongoDB replica scale workload still references temporary members')
        # Authorize the complete set before deleting either retained claim.
        for name, row in self.state['extra_claims'].items():
            claim = self.s.lab.get('pvc', name, self.s.ns)
            if claim and (claim['metadata']['uid'] != row['uid'] or
                    claim['spec'].get('volumeName') != row['volume']):
                raise ValueError('MongoDB replica scale claim binding changed before retirement')
            if row['volume']:
                volume = self.s.lab.get('pv', row['volume'])
                if volume:
                    ref = volume['spec'].get('claimRef', {})
                    if (volume['spec'].get('storageClassName') != 'mongodb-longhorn' or
                            volume['spec'].get('persistentVolumeReclaimPolicy') != 'Delete' or
                            volume['spec'].get('csi', {}).get('driver') != 'driver.longhorn.io' or
                            (ref.get('namespace'), ref.get('name'), ref.get('uid')) != (self.s.ns, name, row['uid'])):
                        raise ValueError('MongoDB replica scale refuses a rebound or retained PV')
        for name, row in self.state['extra_claims'].items():
            if self.s.lab.get('pvc', name, self.s.ns):
                self.s.lab.delete_uid('/api/v1/namespaces/' + self.s.ns + '/persistentvolumeclaims/' + name, row['uid'])
            self.s.lab.run('-n', self.s.ns, 'wait', '--for=delete', 'pvc/' + name, '--timeout=300s', timeout=320)
            if row['volume']:
                self.s.lab.run('wait', '--for=delete', 'pv/' + row['volume'], '--timeout=300s', timeout=320)
            if self.s.lab.get('pvc', name, self.s.ns) or row['volume'] and self.s.lab.get('pv', row['volume']):
                raise ValueError('MongoDB temporary member volume remains')
        print('MONGODB_REPLICA_SCALE_VOLUMES_CLEAN_PASS original-three=retained temporary-claims-pvs=absent', flush=True)

    def recover(self):
        if not self.path.exists():
            raise ValueError('MongoDB replica-scale recovery requires its journal')
        self.verify();self.s.prefix = self.state['prefix']
        self.s.client_uid = self.state['client_uid'];self.s.client_attempted = True;self.s.workload_started = True
        phase = self.state.get('phase', 'active')
        current = self.s.lab.get('pod', self.s.client, self.s.ns)
        if (not current and phase == 'active' or current and
                (current['metadata']['uid'] != self.s.client_uid or
                 current['metadata'].get('labels', {}).get('kubeauto.io/test-run') != self.s.prefix) or
                current and phase == 'client-absent'):
            raise ValueError('MongoDB replica-scale recovery refuses a missing or replaced business client')
        if phase == 'active':
            self.recover_members()
            self.s.clean_workload()
            # Persist the verified data/volume boundary BEFORE deleting the client.
            # Only this phase authorizes reentry with an absent original client.
            self.state['phase'] = 'client-removal';self.save()
        if self.state.get('phase') == 'client-removal':
            self.verify_restored_source()
            self.s.delete_client()
            if self.s.lab.get('pod', self.s.client, self.s.ns):
                raise ValueError('MongoDB replica-scale client remains after deletion')
            self.state['phase'] = 'client-absent';self.save()
        self.s.client_uid = None;self.s.client_attempted = False;self.s.workload_started = False
        self.verify_restored_source()
        self.domain_type(self.storage, self.s.prefix.removeprefix('mongodb_gate_')).restore()
        self.s.wait_health(600)
        self.path.unlink()
        print('MONGODB_REPLICA_SCALE_RECOVERY_SCOPED_CLEAN_PASS config-three-volumes-hosts-data=verified', flush=True)

    def verify_restored_source(self):
        self.verify()
        if yaml.safe_load(self.config_path.read_text()) != self.state['original_config']:
            raise ValueError('MongoDB replica-scale original configuration is not restored')
        after = self.s.snapshot()
        before = self.state['before']
        cr = lambda rows: [{k: v for k, v in r.items() if k != 'generation'} for r in rows]
        if before['pvc'] != after['pvc'] or before['secrets'] != after['secrets'] or cr(before['psmdb']) != cr(after['psmdb']):
            raise ValueError('MongoDB replica scaling changed original claims, credentials or cluster spec')

    def recover_members(self):
        self.capture_claims()
        # Initial sync may not have completed on new members. Prove the three
        # original members form a healthy majority before reducing the request.
        self.s.js("""
const manager=new Mongo(process.env.MONGODB_URI),admin=manager.getDB('admin');
assert(admin.auth({user:process.env.MONGODB_CLUSTER_ADMIN_USER,pwd:process.env.MONGODB_CLUSTER_ADMIN_PASSWORD}));
const status=assert.commandWorked(admin.runCommand({replSetGetStatus:1}));
const expected=[0,1,2].map(i=>'mongodb-gate-data-rs0-'+i+'.mongodb-gate-data-rs0.mongodb-gate.svc.cluster.local:27017');
const original=status.members.filter(m=>expected.includes(m.name));
assert.eq(3,original.length);assert(original.every(m=>m.health===1&&[1,2].includes(m.state)));
assert.eq(1,status.members.filter(m=>m.health===1&&m.state===1).length);
print('MONGODB_REPLICA_SCALE_ORIGINAL_MAJORITY_PASS');
""", 'MONGODB_REPLICA_SCALE_ORIGINAL_MAJORITY_PASS')
        self.product(3);self.retire_claims()
        self.verify_members(3);self.verify_restored_source()
        hosts = [self.s.fixture.NAME + '-rs0-' + str(i) + '.' + self.s.fixture.NAME + '-rs0.' +
                 self.s.ns + '.svc.cluster.local:27017' for i in range(3)]
        self.s.rebuild_data(hosts)

    def run(self):
        if self.path.exists():raise ValueError('MongoDB replica scaling requires recovery first')
        self.s.maintenance_members();self.s.health()
        original = yaml.safe_load(self.config_path.read_text())
        if original['mongodb_replsets'] != [{'name': 'rs0', 'size': 3}] or original['mongodb_sharding_enabled']:
            raise ValueError('MongoDB member scaling requires the independent three-member replica set')
        for ordinal in (3, 4):
            if self.s.lab.get('pvc', 'mongod-data-' + self.s.fixture.NAME + '-rs0-' + str(ordinal), self.s.ns):
                raise ValueError('MongoDB member scaling refuses pre-existing temporary claims')
        self.s.js(REBUILD_DATA + "assert.commandWorked(d.runCommand({insert:prefix+'_documents',documents:expected,writeConcern:{w:'majority',j:true,wtimeout:10000}}));assert.commandWorked(d.runCommand({createIndexes:prefix+'_documents',indexes:[{key:{value:1},name:'rebuild_value',unique:true}]}));print('MONGODB_REPLICA_SCALE_SEED_PASS');", 'MONGODB_REPLICA_SCALE_SEED_PASS', app=True)
        self.state = {'prefix': self.s.prefix, 'client_uid': self.s.client_uid,
            'cluster_uid': self.s.lab.verify()['cluster_uid'],
            'source_uid': self.s.lab.get('psmdb', self.s.fixture.NAME, self.s.ns)['metadata']['uid'],
            'original_config': original, 'expanded_config': original | {'mongodb_replsets': [{'name': 'rs0', 'size': 5}]},
            'before': self.s.snapshot(), 'extra_claims': {}}
        self.save()
        first_error = None
        try:
            self.domain_type(self.storage, self.s.prefix.removeprefix('mongodb_gate_')).prepare()
            self.product(5);self.capture_claims()
            pods = [p for p in self.s.lab.get('pods', ns=self.s.ns)['items'] if
                p['metadata'].get('labels', {}).get('app.kubernetes.io/instance') == self.s.fixture.NAME and
                p['metadata'].get('labels', {}).get('app.kubernetes.io/replset') == 'rs0']
            workers = {'mongodb-node-' + ip.rsplit('.', 1)[1] for ip in self.s.fixture.NODES[3:]}
            if (len(pods) != 5 or {p['spec'].get('nodeName') for p in pods} != workers |
                    {'mongodb-node-135', 'mongodb-node-40'} or len(self.state['extra_claims']) != 2):
                raise ValueError('MongoDB member scaling did not create five real hostname domains and claims')
            hosts = [p['metadata']['name'] + '.' + self.s.fixture.NAME + '-rs0.' + self.s.ns +
                '.svc.cluster.local:27017' for p in pods]
            self.s.rebuild_data(hosts)
            self.verify_members(5)
            print('MONGODB_REPLICA_SCALE_FIVE_PASS domains=5 votes-and-majority=verified data-and-index=verified', flush=True)
        except BaseException as exc:
            first_error = exc
            directory = self.s.lab.directory / ('mongodb-replica-scale-failure-' + self.s.prefix.removeprefix('mongodb_gate_'))
            directory.mkdir(mode=0o700, exist_ok=True)
            try:self.s.lab.diagnostics(directory)
            except (ValueError, OSError, subprocess.SubprocessError):pass
            raise
        finally:
            try:self.recover()
            except BaseException:
                # The fault journal owns recovery; generic client cleanup must
                # preserve the data and original client needed to prove it.
                self.s.client_attempted = False;self.s.client_uid = None
                print('MONGODB_REPLICA_SCALE_RECOVERY_FAIL journal=preserved', file=sys.stderr)
                if first_error is None:raise


def capacity_suite(base):
    """Keep all three capacity boundaries mandatory and stop at the first error."""
    for module in ('capacity-expand', 'capacity-pressure', 'capacity-lag'):
        Scenario(base).execute(module)
    print('MONGODB_CAPACITY_SCOPED_CLEAN_PASS expansion-pressure-lag=verified', flush=True)
    print('MONGODB_CAPACITY_PASS', flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('module', choices=('observability', 'observability-scrape', 'observability-lag', 'observability-capacity', 'observability-backup', 'observability-recover', 'performance', 'performance-node', 'performance-client', 'performance-recover', 'ownership', 'ownership-resources', 'ownership-resources-recover', 'ownership-pvc', 'ownership-pvc-recover', 'data', 'transactions', 'security', 'failover', 'idempotence', 'network-client', 'network', 'quorum', 'maintenance', 'rebuild', 'rotation', 'replica-scale', 'replica-scale-recover', 'logging', 'capacity-expand', 'capacity-recover', 'capacity-pressure', 'capacity-pressure-recover', 'capacity-lag', 'capacity-lag-recover', 'capacity'))
    parser.add_argument('--base', type=Path, default=Path('/usr/local/kubeauto'))
    args = parser.parse_args()
    def interrupted(signum, frame):
        raise SystemExit(128 + signum)
    signal.signal(signal.SIGTERM, interrupted)
    try:
        if args.module == 'capacity':
            capacity_suite(args.base)
            return 0
        scenario = Scenario(args.base)
        if args.module.startswith('observability'):
            gate=Observability(scenario)
            if args.module=='observability-recover':
                gate.cleanup();print('MONGODB_OBSERVABILITY_RECOVER_PASS',flush=True)
            else:gate.run(focused=args.module.removeprefix('observability-') if args.module!='observability' else None)
        elif args.module.startswith('performance'):
            gate=Performance(scenario)
            if args.module=='performance-recover':
                gate.recover(interrupted=True)
                print('MONGODB_PERFORMANCE_RECOVER_PASS',flush=True)
            elif args.module=='performance-node':gate.run(node_only=True)
            else:gate.run(focused=args.module=='performance-client')
        elif args.module == 'ownership':
            OwnershipPVC(scenario).run()
            OwnershipResources(scenario).run()
            print('MONGODB_OWNERSHIP_SCOPED_CLEAN_PASS all-independent-owners=clean source=unchanged', flush=True)
            print('MONGODB_OWNERSHIP_PASS', flush=True)
        elif args.module in ('ownership-resources', 'ownership-resources-recover'):
            gate=OwnershipResources(scenario)
            if args.module == 'ownership-resources':gate.run()
            else:
                gate.cleanup()
                print('MONGODB_OWNERSHIP_RESOURCES_RECOVER_PASS', flush=True)
        elif args.module in ('ownership-pvc', 'ownership-pvc-recover'):
            gate=OwnershipPVC(scenario)
            if args.module == 'ownership-pvc':gate.run()
            else:
                gate.cleanup()
                print('MONGODB_OWNERSHIP_PVC_RECOVER_PASS', flush=True)
        elif args.module == 'replica-scale-recover':
            ReplicaScale(scenario).recover()
            print('MONGODB_REPLICA_SCALE_RECOVER_PASS', flush=True)
        elif args.module == 'capacity-recover':scenario.capacity_recover()
        elif args.module == 'capacity-pressure-recover':scenario.capacity_pressure_recover()
        elif args.module == 'capacity-lag-recover':scenario.capacity_lag_recover()
        else:scenario.execute(args.module)
    except (ValueError, OSError, KeyError, subprocess.SubprocessError) as exc:
        print('MONGODB_SCENARIO_FAIL module=' + args.module + ' reason=' + str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
