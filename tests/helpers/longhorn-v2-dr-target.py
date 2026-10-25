#!/usr/bin/env python3
"""Independent single-node DR target; never reuses or wipes the source nodes.

Only the core prerequisite is implemented here. A target core PASS does not
qualify Longhorn, standby synchronization, activation, failback or site HA.
"""
import argparse
import base64
import hashlib
import importlib.util
import inspect
import json
import os
import re
from pathlib import Path
import shlex
import subprocess
import sys
import time
import uuid

import yaml

BASE = Path('/usr/local/kubeauto')
ROOT = Path(__file__).resolve().parents[2]
CLUSTER = 'longhorn-v2-dr-target'
NODE = '192.168.47.149'
DIRECTORY = BASE/'clusters'/CLUSTER
SSH = ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10',
       '-o', 'ServerAliveInterval=5', '-o', 'ServerAliveCountMax=2']


def load(name, base=BASE):
    spec = importlib.util.spec_from_file_location('dr_target_' + name.replace('-', '_'), base/'tests/helpers'/name)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def execute(argv, **kwargs):
    return subprocess.run(argv, check=True, text=True, capture_output=True, timeout=120, **kwargs)


def disk_identity(assessment):
    if assessment['blockers'] or len(assessment['candidates']) != 1:
        raise ValueError('target requires one admitted unused raw disk')
    disk = assessment['candidates'][0]
    paths = sorted(disk['by_id'], key=lambda path: (not Path(path).name.startswith('wwn-'), path))
    if not paths or not disk['identity']:
        raise ValueError('target raw disk lacks stable hardware identity')
    return disk['identity'], disk['size_bytes'], paths[0]


def bootstrap_script():
    return '''import base64,json,os,stat,sys
from pathlib import Path
row=json.load(sys.stdin)
assert Path('/etc/machine-id').read_text().strip()==row['machine_id'], 'target machine changed'
keys=row['keys'];assert keys and len(keys)==len(set(keys))
for key in keys:
 parts=key.split();assert len(parts)>=2 and parts[0] in ('ssh-ed25519','ssh-rsa')
 assert len(base64.b64decode(parts[1],validate=True))>32
parent=Path('/root/.ssh');assert not parent.is_symlink()
if parent.exists():
 info=parent.stat();assert info.st_uid==0 and stat.S_ISDIR(info.st_mode) and not info.st_mode&0o022
else:parent.mkdir(mode=0o700)
path=parent/'authorized_keys';assert not path.is_symlink()
original=''
if path.exists():
 info=path.stat();assert info.st_uid==0 and stat.S_ISREG(info.st_mode) and not info.st_mode&0o022
 original=path.read_text()
lines=original.splitlines();added=[key for key in keys if key not in lines]
if added:
 temporary=parent/'kubeauto-dr-key-bootstrap.tmp'
 fd=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
 try:
  with os.fdopen(fd,'w') as output:
   output.write(original+('\\n' if original and not original.endswith('\\n') else '')+'\\n'.join(added)+'\\n')
   output.flush();os.fsync(output.fileno())
  assert not path.is_symlink();os.replace(temporary,path)
 finally:
  if temporary.exists():temporary.unlink()
print('LONGHORN_V2_DR_TARGET_ROOT_KEY_READY')
'''


def known_host_script():
    return """import base64,json,os,stat,subprocess,sys
from pathlib import Path
row=json.load(sys.stdin);assert row['host']=='192.168.47.149'
parts=row['key'].split();assert len(parts)==2 and parts[0]=='ssh-ed25519'
assert len(base64.b64decode(parts[1],validate=True))>32
path=Path('/root/.ssh/known_hosts');assert not path.is_symlink()
parent=path.parent;assert not parent.is_symlink()
info=parent.stat();assert info.st_uid==0 and stat.S_ISDIR(info.st_mode) and not info.st_mode&0o022
original=path.read_text() if path.exists() else ''
if path.exists():
 info=path.stat();assert info.st_uid==0 and stat.S_ISREG(info.st_mode) and not info.st_mode&0o022
 found=subprocess.run(['ssh-keygen','-F',row['host'],'-f',str(path)],text=True,capture_output=True,timeout=15)
 assert found.returncode in (0,1)
 for line in found.stdout.splitlines():
  if line.startswith('#'):continue
  fields=line.split()
  if len(fields)>=3 and fields[1]==parts[0]:assert fields[2]==parts[1], 'target known host key changed'
 line=row['host']+' '+row['key']
 if line in original.splitlines():sys.exit(0)
else:line=row['host']+' '+row['key']
temporary=path.with_name('kubeauto-dr-known-host.tmp')
fd=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
try:
 with os.fdopen(fd,'w') as output:
  output.write(original+('\\n' if original and not original.endswith('\\n') else '')+line+'\\n')
  output.flush();os.fsync(output.fileno())
 assert not path.is_symlink();os.replace(temporary,path)
finally:
 if temporary.exists():temporary.unlink()
"""


def bootstrap():
    if os.environ.get('LONGHORN_TARGET_PARENT_LEASE_HELD') != 'yes':
        raise ValueError('target key bootstrap requires the fixed runner parent lease')
    # Existing fixed access helper verifies key-only user access; no password is
    # copied to the control host, target, config, journal or output.
    execute(['bash', str(ROOT/'tests/helpers/lab-ssh-bootstrap.sh'), 'ubuntu24@'+NODE])
    pool = load('longhorn-v2-pool.py', ROOT)
    ready = json.loads((ROOT/'tests/evidence/longhorn-v2-dr-target-149-ready-20261020.json').read_text())
    raw = execute(SSH + ['ubuntu24@'+NODE, shlex.join(['sudo', '-n', 'python3', '-c', pool.PROBE])]).stdout
    inventory = json.loads(raw); assessment = pool.assess(inventory)
    expected = ready['observed']['inventory']
    if (assessment['blockers'] or inventory['machine_id'] != expected['machine_id'] or
            disk_identity(assessment) != disk_identity(ready['observed']['assessment'])):
        raise ValueError('target admission changed before root key bootstrap')
    keys = []
    for name in ('id_ed25519.pub', 'id_rsa.pub'):
        local = Path.home()/'.ssh'/name
        if local.is_file(): keys.append(local.read_text().strip())
    public = "from pathlib import Path; import json; print(json.dumps([p.read_text().strip() for p in (Path('/root/.ssh/id_ed25519.pub'),Path('/root/.ssh/id_rsa.pub')) if p.is_file()]))"
    control = json.loads(execute(SSH + ['root@192.168.47.130', shlex.join(['python3', '-c', public])]).stdout)
    if not control or not keys:
        raise ValueError('fixed source/control public keys are unavailable')
    keys = list(dict.fromkeys(keys+control))
    for key in keys:
        parts=key.split()
        if len(parts)<2 or parts[0] not in ('ssh-ed25519','ssh-rsa') or len(base64.b64decode(parts[1],validate=True))<32:
            raise ValueError('invalid public bootstrap key')
    row={'machine_id':expected['machine_id'],'keys':keys}
    result=execute(SSH + ['ubuntu24@'+NODE, shlex.join(['sudo','-n','python3','-c',bootstrap_script()])],input=json.dumps(row))
    if result.stdout.splitlines()!=['LONGHORN_V2_DR_TARGET_ROOT_KEY_READY']:
        raise ValueError('target root key bootstrap lacks its exact result')
    # Learn the target host key over the already authenticated user channel,
    # then install that exact public key on the control host. Never disable
    # host authentication for the subsequent product/transport connections.
    host_key=execute(SSH+['ubuntu24@'+NODE,'cat /etc/ssh/ssh_host_ed25519_key.pub']).stdout.strip().split()
    if len(host_key)<2 or host_key[0]!='ssh-ed25519':
        raise ValueError('target authenticated host key is unavailable')
    known_script=known_host_script()
    execute(SSH+['root@192.168.47.130',shlex.join(['python3','-c',known_script])],
        input=json.dumps({'host':NODE,'key':' '.join(host_key[:2])}))
    execute(SSH + ['root@'+NODE,'true'])
    execute(SSH + ['root@192.168.47.130',shlex.join(SSH+['root@'+NODE,'true'])])
    print('LONGHORN_V2_DR_TARGET_KEY_BOOTSTRAP_PASS',flush=True)


def inventory():
    lines=[]
    for group in ('etcd','kube_master','kube_node'):
        lines += ['['+group+']', NODE+" k8s_nodename='longhorn-node-149'"]
    lines += ['[all:vars]','SECURE_PORT="6443"','CONTAINER_RUNTIME="containerd"',
        'CLUSTER_NETWORK="calico"','PROXY_MODE="ipvs"','SERVICE_CIDR="10.88.0.0/16"',
        'CLUSTER_CIDR="172.29.0.0/16"','NODE_PORT_RANGE="30000-32767"',
        'CLUSTER_DNS_DOMAIN="cluster.local"','bin_dir="/usr/local/bin"',
        'base_dir="/usr/local/kubeauto"','cluster_dir="{{ base_dir }}/clusters/longhorn-v2-dr-target"',
        'ca_dir="/etc/kubernetes/ssl"',"k8s_nodename=''",'ansible_user=root']
    return '\n'.join(lines)+'\n'


def fixture():
    lab=load('longhorn-lab.py')
    lab.BASE=BASE;lab.CLUSTER=CLUSTER;lab.NODES=lab.WORKERS=(NODE,)
    lab.DIRECTORY=DIRECTORY;lab.STATE=DIRECTORY/'longhorn-lab-owner.json'
    lab.inventory=inventory
    sys.modules[lab.__name__]=lab
    lab.core_identity=lambda: core_identity(lab)
    lab.relay_probe=lambda node: load('longhorn-v2-lab.py').relay_probe(lab,node)
    lab.relay_ensure=lambda force=False: relay_ensure(lab,force=force)
    original=lab.transport_fingerprint
    lab.transport_fingerprint=lambda: hashlib.sha256((original()+inspect.getsource(inventory)+inspect.getsource(relay_ensure)+inspect.getsource(load('longhorn-v2-lab.py').relay_probe)).encode()).hexdigest()
    return lab


def host_inputs(lab):
    pool=load('longhorn-v2-pool.py')
    raw=lab.remote(NODE,['python3','-c',pool.PROBE]).stdout
    row={'inventory':json.loads(raw)};row['assessment']=pool.assess(row['inventory'])
    ready=json.loads((BASE/'tests/evidence/longhorn-v2-dr-target-149-ready-20261020.json').read_text())['observed']
    if (row['assessment']['blockers'] or row['inventory']['machine_id']!=ready['inventory']['machine_id'] or
            disk_identity(row['assessment'])!=disk_identity(ready['assessment']) or
            row['inventory']['memory_kib']['MemAvailable']<5*1024**2):
        raise ValueError('independent DR target is not its approved clean hardware baseline')
    print('LONGHORN_V2_DR_TARGET_INPUTS_VERIFIED kernel='+row['inventory']['kernel']+' raw_disk_identity=stable',flush=True)
    public = {key: row['inventory'][key] for key in ('machine_id', 'kernel', 'online_cpus', 'memory_kib')}
    identity, size, path = disk_identity(row['assessment'])
    public.update(host=NODE, raw_disk={'identity': identity, 'size_bytes': size, 'path': path})
    print('LONGHORN_V2_DR_TARGET_INPUTS ' + json.dumps(public, sort_keys=True), flush=True)
    return row


def configure(lab):
    row=host_inputs(lab)
    if lab.STATE.exists() or DIRECTORY.is_symlink():
        raise ValueError('independent DR target requires no previous owner')
    path=DIRECTORY/'config.yml';hosts=DIRECTORY/'hosts'
    if path.is_symlink() or hosts.is_symlink():raise ValueError('symlink target input')
    config=yaml.safe_load(path.read_text())
    if config['longhorn_version']!='1.12.1':raise ValueError('target version differs from source')
    config.update(K8S_VER='1.33.6',CLUSTER_NAME=CLUSTER,ENABLE_SETTING_HOSTNAME=True,
        KUBE_RESERVED_ENABLED='no',SYS_RESERVED_ENABLED='no',longhorn_install='no',
        longhorn_storage_nodes=[],longhorn_v2_nodes=[],longhorn_block_disks={})
    path.write_text(yaml.safe_dump(config,sort_keys=False));hosts.write_text(inventory())
    disk=row['assessment']['candidates'][0];host=row['inventory']
    aliases=sorted(disk['by_id'],key=lambda p:(not Path(p).name.startswith('wwn-'),p))
    raw={'path':aliases[0],'identity':disk['identity'],'size_bytes':disk['size_bytes'],
         'machine_id':host['machine_id'],'original_hostname':host['hostname']}
    lab.write_private(lab.STATE,{'cluster':CLUSTER,'nodes':[NODE],'run_id':str(uuid.uuid4()),
        'clean_start_verified':True,'disks':{},'healthy':False,'raw_disks':{NODE:raw}})
    print('LONGHORN_V2_DR_TARGET_CONFIGURED nodes=1 storage=disabled',flush=True)


def core_identity(lab):
    state=lab.owner();nodes=lab.get('nodes')['items'];cluster=lab.get('namespace','kube-system')['metadata']['uid']
    if (len(nodes)!=1 or nodes[0]['metadata']['name']!='longhorn-node-149' or
            [a['address'] for a in nodes[0]['status']['addresses'] if a['type']=='InternalIP']!=[NODE] or
            not any(c['type']=='Ready' and c['status']=='True' for c in nodes[0]['status']['conditions'])):
        raise ValueError('independent DR core is not its one Ready approved node')
    uids={nodes[0]['metadata']['name']:nodes[0]['metadata']['uid']}
    source=json.loads((BASE/'clusters/longhorn-v2-gate/longhorn-lab-owner.json').read_text())
    if cluster==source['cluster_uid'] or set(uids.values())&set(source['node_uids'].values()):
        raise ValueError('DR target aliases its source cluster or nodes')
    if state.get('cluster_uid') and (state['cluster_uid']!=cluster or state['node_uids']!=uids):
        raise ValueError('DR target core identity changed')
    if not state.get('cluster_uid'):
        state.update(cluster_uid=cluster,node_uids=uids);lab.write_private(lab.STATE,state)
    print('LONGHORN_V2_DR_TARGET_CORE_IDENTITY_PASS nodes=1 independent=true',flush=True)


def relay_ensure(lab,*,force=False):
    state=lab.owner();rows=lab.relay_rows(state);node=NODE
    row=next((r for r in rows if r['node']==node),None)
    if row:
        live=lab.execute(lab.SSH+['-S',row['socket'],'-O','check','root@'+node],check=False)
        if live.returncode:
            lab.remote(node,lab.relay_rule('-C'))
            lab.remote(node,script='test -z "$(ss -H -lnt '+shlex.quote('sport = :'+str(lab.PORT))+')"')
            path=Path(row['socket'])
            if path.exists():path.unlink()
            lab.relay_connect(row);lab.bind_listener(row,state)
    elif force or not lab.relay_probe(node):
        row={'node':node,'socket':str(DIRECTORY/('longhorn-registry-'+node+'.sock'))}
        if Path(row['socket']).exists() or lab.remote(node,lab.relay_rule('-C'),check=False).returncode!=1:
            raise ValueError('foreign target relay socket or rule')
        lab.remote(node,script='test -z "$(ss -H -lnt '+shlex.quote('sport = :'+str(lab.PORT))+')"')
        rows.append(row);state['relays']=rows;lab.write_private(lab.STATE,state)
        lab.relay_connect(row);lab.bind_listener(row,state);lab.remote(node,lab.relay_rule('-A'))
    if not lab.relay_probe(node):raise ValueError('target Registry is not reachable')
    print('LONGHORN_V2_DR_TARGET_REGISTRY_READY nodes=1',flush=True)


def qualification_current(lab):
    evidence=json.loads((BASE/'tests/evidence/longhorn-v2-dr-target-registry-qualification-20261020.json').read_text())
    if (evidence['status']!='pass' or evidence['rc'] or evidence['failure_markers'] or
            evidence['transport_fingerprint']!=lab.transport_fingerprint()):
        raise ValueError('independent DR target requires current transport qualification')
    print('LONGHORN_V2_DR_TARGET_TRANSPORT_QUALIFICATION_CURRENT',flush=True)


def supervise(lab,pid,step):
    identity=lab.process_identity(pid)
    if not identity or identity[0][-2:]!=[str(Path(__file__).resolve()).encode(),b'relay-maintain']:
        raise ValueError('target core requires its exact Registry supervisor')
    command={'download':['download','-D'],'setup':['setup',CLUSTER,'90'],
        'longhorn-download':['download','-E','longhorn'],'longhorn-setup':['setup',CLUSTER,'09']}[step]
    argv=[sys.executable,str(BASE/'kubecli.py'),*command]
    process=subprocess.Popen(argv,stdin=subprocess.DEVNULL,start_new_session=True)
    started=heartbeat=time.monotonic()
    with lab.scoped_cleanup(lambda:lab.stop_product(process,argv)):
        while process.poll() is None:
            if lab.process_identity(pid)!=identity:raise ValueError('target Registry supervisor exited')
            now=time.monotonic()
            if now-started>=7200:raise TimeoutError('target product step exceeded two hours')
            if now-heartbeat>=15:
                print('LONGHORN_V2_DR_TARGET_HEARTBEAT step='+step+' elapsed='+str(int(now-started)),flush=True)
                heartbeat=now
            time.sleep(1)
        if process.returncode:raise ValueError('target product step failed: '+step)


def smoke(lab):
    lab.core_identity();state=lab.owner();namespace='longhorn-dr-core-smoke-'+uuid.uuid4().hex[:16]
    if lab.get('namespace',namespace):raise ValueError('target smoke namespace already exists')
    lab.cache_artifacts({'brinnatt/json-mock:v1.3.1'})
    environment=['env','PRODUCTION_SMOKE_NAMESPACE='+namespace,
        'PRODUCTION_SMOKE_IMAGE=registry.talkschool.cn:5000/longhorn-lab/brinnatt/json-mock:v1.3.1',
        'PRODUCTION_SMOKE_SERVER_NODE=longhorn-node-149','PRODUCTION_SMOKE_CLIENT_NODE=longhorn-node-149']
    result=lab.execute(environment+['bash',BASE/'tests/helpers/kubernetes-production-smoke.sh'],check=False,timeout=1200)
    lab.write_private(DIRECTORY/'core-smoke-result.json',{'rc':result.returncode,'namespace':namespace,
        'stdout':result.stdout,'stderr':result.stderr})
    if result.returncode or not re.search(r'^KUBERNETES_PRODUCTION_SMOKE_PASS ',result.stdout,re.M) or 'PRODUCTION_SMOKE_HTTP_RW_OK' not in result.stdout.splitlines() or lab.get('namespace',namespace):
        raise ValueError('target same-node DNS/ClusterIP HTTP write/read smoke failed')
    print(result.stdout,end='',flush=True)
    state['target_core_smoke_sha256']=hashlib.sha256((BASE/'tests/helpers/kubernetes-production-smoke.sh').read_bytes()).hexdigest()
    lab.write_private(lab.STATE,state)
    print('LONGHORN_V2_DR_TARGET_CORE_SMOKE_PASS nodes=1 cross_node=false',flush=True)


def source_boundary():
    backup=load('longhorn-v2-backups.py');install,lab,store=backup.fixture()
    backup.baseline(install,lab,store)
    if lab.owner().get('relays'):raise ValueError('source retains a Registry relay')
    state=lab.owner();identity={key:state[key] for key in ('run_id','cluster_uid','namespace_uid','node_uids','raw_disks')}
    identity['config_sha256']=hashlib.sha256((lab.DIRECTORY/'config.yml').read_bytes()).hexdigest()
    print('LONGHORN_V2_DR_SOURCE_BOUNDARY sha256='+hashlib.sha256(json.dumps(identity,sort_keys=True).encode()).hexdigest(),flush=True)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode',choices=('bootstrap','host-inputs','configure','relay-qualification','relay-ensure',
        'relay-maintain','relay-clean','qualification-clean','qualification-current','core-identity','supervise','smoke','core-cleanup','source-boundary'))
    parser.add_argument('--relay-pid',type=int,default=0);parser.add_argument('--step',choices=('download','setup','longhorn-download','longhorn-setup'))
    args=parser.parse_args()
    if args.mode=='bootstrap':bootstrap();return
    lab=fixture();lab.require_lease()
    if args.mode=='source-boundary':source_boundary()
    elif args.mode=='host-inputs':host_inputs(lab)
    elif args.mode=='configure':configure(lab)
    elif args.mode=='qualification-current':qualification_current(lab)
    elif args.mode=='supervise':supervise(lab,args.relay_pid,args.step)
    elif args.mode=='smoke':smoke(lab)
    elif args.mode=='core-cleanup':load('longhorn-v2-core.py').cleanup(lab)
    else:{'relay-qualification':lab.relay_qualification,'relay-ensure':lab.relay_ensure,
        'relay-maintain':lab.relay_maintain,'relay-clean':lab.relay_cleanup,'qualification-clean':lab.qualification_clean,'core-identity':lab.core_identity}[args.mode]()


if __name__=='__main__':
    try:main()
    except Exception:
        import tempfile
        import traceback
        with tempfile.NamedTemporaryFile('w',prefix='longhorn-v2-dr-target-failure-',suffix='.txt',dir='/var/tmp',delete=False) as diagnostic:
            traceback.print_exc(file=diagnostic)
            print('LONGHORN_V2_DR_TARGET_FAIL private_diagnostic='+diagnostic.name,file=sys.stderr)
        sys.exit(1)
