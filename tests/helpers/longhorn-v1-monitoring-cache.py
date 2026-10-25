#!/usr/bin/env python3
"""Stage the exact published Grafana artifact into the leased V1 node caches.

A verified OCI archive crosses SSH as a bounded file stream; the product still
owns Chart rendering, workload creation and every business acceptance check.
Only temporary archives are removed. Cached immutable images remain reusable.
"""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import shlex
import subprocess
import tempfile
import time

KEY = 'brinnatt/grafana:13.1.1'
REFERENCE = 'registry.talkschool.cn:5000/'+KEY
MARKER = 'LONGHORN_V1_MONITORING_IMAGE_CACHE'


def node_script():
    return r'''import hashlib,json,os,subprocess,sys,tempfile
from pathlib import Path
request=json.loads(sys.argv[1])
assert Path('/etc/machine-id').read_text().strip()==request['machine_id'],'changed cache host'
assert request['ref']=='registry.talkschool.cn:5000/brinnatt/grafana:13.1.1'
ctr=['/usr/local/bin/ctr','--namespace','k8s.io','images']
def digest():
 p=subprocess.run(ctr+['list'],text=True,capture_output=True,check=True,timeout=30)
 rows=[line.split() for line in p.stdout.splitlines()[1:] if line.split() and line.split()[0]==request['ref']]
 assert len(rows)<=1,'ambiguous image tag'
 return rows[0][2] if rows else None
old=digest()
if request['action']=='probe':
 print(json.dumps({'manifest':old}));sys.exit(0)
assert request['action']=='import'
assert old in (None,request['manifest']),'foreign cached image tag'
with tempfile.TemporaryFile() as output:
 h=hashlib.sha256();size=0
 while True:
  data=sys.stdin.buffer.read(1024*1024)
  if not data:break
  size+=len(data)
  assert size<=request['size'],'archive exceeds declared size'
  h.update(data);output.write(data)
 assert size==request['size'] and h.hexdigest()==request['sha256'],'archive transfer checksum differs'
 if old is None:
  output.flush();output.seek(0)
  path='/proc/self/fd/'+str(output.fileno())
  subprocess.run(ctr+['import','--local','--base-name',request['ref'].rsplit(':',1)[0],'--label','io.cri-containerd.image=managed',path],pass_fds=(output.fileno(),),check=True,timeout=600,stdout=subprocess.DEVNULL)
 assert digest()==request['manifest'],'imported image differs from verified manifest'
print(json.dumps({'ref':request['ref'],'manifest':request['manifest'],'sha256':h.hexdigest(),'bytes':size,'cached_before':old is not None,'temporary_files_removed':True},sort_keys=True))
'''


def transfer(lab, node, archive, request):
    command = lab.SSH+['root@'+node, shlex.join(['python3','-c',node_script(),json.dumps(request)])]
    with archive.open('rb') as data, tempfile.TemporaryFile(mode='w+') as out, tempfile.TemporaryFile(mode='w+') as err:
        process = subprocess.Popen(command, stdin=data, stdout=out, stderr=err)
        start = time.monotonic()
        try:
            while process.poll() is None:
                try: process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    if time.monotonic()-start>=900:
                        raise TimeoutError('monitoring archive transfer exceeded 900 seconds')
                    print(MARKER+'_HEARTBEAT node='+node+' elapsed='+str(int(time.monotonic()-start)),flush=True)
            out.seek(0); err.seek(0)
            if process.returncode:
                lab.write_private(lab.DIRECTORY/('monitoring-cache-failure-'+node+'.json'),
                                  {'rc':process.returncode,'stderr':err.read()})
                raise ValueError('monitoring cache transfer failed node='+node)
            row=json.loads(out.read())
            if (row.get('ref')!=REFERENCE or row.get('manifest')!=request['manifest'] or
                    row.get('sha256')!=request['sha256'] or row.get('bytes')!=request['size'] or
                    row.get('temporary_files_removed') is not True):
                raise ValueError('monitoring cache readback differs node='+node)
            print(MARKER+'_NODE '+json.dumps(dict(row,node=node),sort_keys=True),flush=True)
            return row
        finally:
            if process.poll() is None:
                process.terminate()
                try:process.wait(timeout=20)
                except subprocess.TimeoutExpired:process.kill();process.wait()


def stage(lab, cache):
    lab.require_lease();lab.core_identity()
    if tuple(lab.WORKERS)!=('192.168.122.212','192.168.122.165','192.168.122.238'):
        raise ValueError('monitoring image cache requires the exact leased V1 workers')
    # The owning cache verifies publication identities and all eleven mappings.
    cache(lab)
    lab.owner()
    manifest=lab.execute(['skopeo','inspect','--raw','--tls-verify=false',
        'docker://127.0.0.1:5000/'+KEY],timeout=60).stdout
    parsed=json.loads(manifest)
    if parsed.get('schemaVersion')!=2 or not parsed.get('layers') or 'config' not in parsed:
        raise ValueError('monitoring image cache requires its verified platform manifest')
    expected='sha256:'+hashlib.sha256(manifest.encode()).hexdigest()
    before={node:lab.get('node','longhorn-node-'+node.rsplit('.',1)[1])['status']['nodeInfo']['machineID'] for node in lab.WORKERS}
    pending=[]
    for node in lab.WORKERS:
        probe={'action':'probe','ref':REFERENCE,'machine_id':before[node]}
        result=json.loads(lab.remote(node,['python3','-c',node_script(),json.dumps(probe)]).stdout)
        if result['manifest'] is None: pending.append(node)
        elif result['manifest']!=expected: raise ValueError('foreign cached monitoring image node='+node)
    if not pending:
        print(MARKER+'_SCOPED_CLEAN_PASS',flush=True)
        print(MARKER+'_PASS nodes=3 reused=3',flush=True)
        return
    with tempfile.TemporaryDirectory(prefix='kubeauto-longhorn-monitoring-cache-') as directory:
        archive=Path(directory)/'grafana.tar'
        lab.execute(['skopeo','copy','--preserve-digests','--src-tls-verify=false',
            'docker://127.0.0.1:5000/'+KEY,'oci-archive:'+str(archive)+':'+REFERENCE],timeout=600)
        h=hashlib.sha256()
        with archive.open('rb') as stream:
            for chunk in iter(lambda:stream.read(1024*1024),b''):h.update(chunk)
        sha=h.hexdigest()
        request={'action':'import','ref':REFERENCE,'manifest':expected,'size':archive.stat().st_size,'sha256':sha}
        with ThreadPoolExecutor(max_workers=2) as executor:
            jobs=[executor.submit(transfer,lab,node,archive,dict(request,machine_id=before[node])) for node in pending]
            results=[job.result() for job in jobs]
    lab.core_identity()
    print(MARKER+'_SCOPED_CLEAN_PASS',flush=True)
    print(MARKER+'_PASS nodes=3 imported='+str(len(results)),flush=True)
