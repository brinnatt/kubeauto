#!/usr/bin/env python3
"""Own the six-node tools CNI fixture, without changing the installed product."""
import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import shlex
import subprocess
import sys
import tarfile
import time
import uuid

import yaml

ROOT = Path('/tmp/tools-calico-lab')
BASE = ROOT/'product'
SOURCE = Path('/usr/local/kubeauto')
NAME = 'tools-calico-gate'
NODES = tuple('192.168.47.'+str(n) for n in range(131,137))
SSH = ['ssh','-o','BatchMode=yes','-o','ConnectTimeout=8']
PYTHON = '/usr/local/kubeauto/.venv/bin/python'
BINS = (('hub.talkedu.cn/kubeauto/kubeauto-k8s-bin@sha256:8b0a468b83e4f74ad081cb36a0c39fe71d45f45aaf94283361ffa931cc0e1d76','/k8s','kube-bin'),
        ('hub.talkedu.cn/kubeauto/kubeauto-ext-bin@sha256:86d53e65b24595d450ce7f7d83c06914a0f7180641eb32bf5e2e5601b0d0fdfb','/extra','extra-bin'))


def run(argv, **kwargs):
    print('TOOLS_CALICO_FIXTURE_COMMAND '+str(argv[0])+' '+str(argv[1:3]),flush=True)
    return subprocess.run([str(x) for x in argv],check=True,timeout=kwargs.pop('timeout',180),**kwargs)


def inventory(base=BASE):
    result=[]
    for group,addresses in (('etcd',NODES[3:]),('kube_master',NODES[3:]),('kube_node',NODES[:3])):
        result+=['['+group+']']+[ip+" k8s_nodename='tools-calico-"+ip.rsplit('.',1)[1]+"'" for ip in addresses]
    result+=['[all:vars]','SECURE_PORT="6443"','CONTAINER_RUNTIME="containerd"','CLUSTER_NETWORK="calico"',
        'PROXY_MODE="ipvs"','SERVICE_CIDR="10.86.0.0/16"','CLUSTER_CIDR="172.29.0.0/16"',
        'NODE_PORT_RANGE="30000-32767"','CLUSTER_DNS_DOMAIN="cluster.local"','bin_dir="/usr/local/bin"',
        'base_dir="'+str(base)+'"','cluster_dir="{{ base_dir }}/clusters/'+NAME+'"',
        'ca_dir="/etc/kubernetes/ssl"',"k8s_nodename=''",'ansible_user=root']
    return '\n'.join(result)+'\n'


def configure(data):
    if data.get('calico_ver')!='v3.28.4':
        raise ValueError('TOOLS_CALICO_FIXTURE_SOURCE_VERSION_REFUSED')
    result=dict(data)
    for key in result:
        if key.endswith('_install'):result[key]='no'
    result.update(K8S_VER='1.33.6',CLUSTER_NAME=NAME,KUBE_RESERVED_ENABLED='no',
                  SYS_RESERVED_ENABLED='no',SYS_RESERVED_ENFORCE='no',REGISTRY_HOST_IP='192.168.47.130')
    return result


def admit(states):
    if set(states)!=set(NODES):raise ValueError('TOOLS_CALICO_FIXTURE_POOL_REFUSED')
    for address,state in states.items():
        if any(value not in ('inactive','unknown') for value in state['units']):
            raise ValueError('TOOLS_CALICO_FIXTURE_FOREIGN_SERVICE '+address)
        if state['kubernetes'] or state['kubeconfig']:
            raise ValueError('TOOLS_CALICO_FIXTURE_FOREIGN_CONFIGURATION '+address)


def product(*args):
    env=dict(os.environ,PYTHONPATH=str(BASE),KUBEAUTO_BASE_PATH=str(BASE),
             PATH=str(BASE/'kube-bin')+':'+str(BASE/'extra-bin')+':'+str(Path(PYTHON).parent)+':'+os.environ['PATH'])
    run([PYTHON,BASE/'kubecli.py',*args],cwd=BASE,env=env,timeout=1800)


def stage_binaries():
    remote='/tmp/tools-calico-bins-'+uuid.uuid4().hex
    try:
        run([*SSH,'root@192.168.47.138','mkdir '+remote])
        for index,(image,source,target) in enumerate(BINS):
            name='tools-calico-extract-'+str(index)
            command=("set -e; mkdir '"+remote+'/'+target+"'; cid=$(docker create --name "+name+" '"+image+"'); "
                     "trap 'docker rm -f \"$cid\" >/dev/null' EXIT; docker cp \"$cid:"+source+"/.\" '"+remote+'/'+target+"'")
            run([*SSH,'root@192.168.47.138',command])
        run([*SSH,'root@192.168.47.138',"tar -C '"+remote+"' -czf '"+remote+"/bins.tgz' kube-bin extra-bin"])
        run(['scp','-q','-o','BatchMode=yes','root@192.168.47.138:'+remote+'/bins.tgz',ROOT/'bins.tgz'])
        with tarfile.open(ROOT/'bins.tgz') as bundle:
            for member in bundle.getmembers():
                if Path(member.name).is_absolute() or '..' in Path(member.name).parts or member.issym() or member.islnk():
                    raise ValueError('TOOLS_CALICO_BINARY_ARCHIVE_REFUSED')
            bundle.extractall(BASE)
        version=subprocess.check_output([str(BASE/'kube-bin/kubelet'),'--version'],text=True).strip()
        if version!='Kubernetes v1.33.6':raise ValueError('TOOLS_CALICO_KUBELET_VERSION_REFUSED '+version)
        hashes={str(p.relative_to(BASE)):hashlib.sha256(p.read_bytes()).hexdigest()
                for name in ('kube-bin','extra-bin') for p in (BASE/name).rglob('*') if p.is_file()}
        (ROOT/'binary-sha256.json').write_text(json.dumps(hashes,indent=2)+'\n')
    finally:
        run([*SSH,'root@192.168.47.138',"rm -rf '"+remote+"'"])


def probe_authorization(line, saved=None, host=None):
    code = '''import base64,json,pathlib,sys
p=pathlib.Path('/root/.ssh/authorized_keys')
data=json.loads(sys.stdin.read())
line=base64.b64decode(data['line'])
if data['saved'] is None:
    old=p.read_bytes() if p.exists() else b''
    state=dict(old=base64.b64encode(old).decode(),mode=p.stat().st_mode & 0o777 if p.exists() else None)
    new=old+(b'' if not old or old.endswith(b'\\n') else b'\\n')+line
    p.parent.mkdir(mode=0o700,exist_ok=True)
    p.write_bytes(new);p.chmod(0o600)
    print(json.dumps(state))
else:
    state=data['saved'];old=base64.b64decode(state['old'])
    expected=old+(b'' if not old or old.endswith(b'\\n') else b'\\n')+line
    if p.read_bytes()!=expected:raise ValueError('TOOLS_CALICO_PROBE_AUTH_CHANGED')
    if state['mode'] is None:p.unlink()
    else:p.write_bytes(old);p.chmod(state['mode'])
'''
    import base64
    payload=json.dumps(dict(line=base64.b64encode(line).decode(),saved=saved))
    result=run([*SSH,'root@'+(host or NODES[1]),'python3 -c '+shlex.quote(code)], input=payload,capture_output=True,text=True)
    return json.loads(result.stdout) if saved is None else None


@contextlib.contextmanager
def probe_identity(directory):
    keydir=ROOT/'probe-ssh'
    keydir.mkdir(mode=0o700)
    key=keydir/'key'
    line=None
    saved=[]
    try:
        run(['ssh-keygen','-q','-t','ed25519','-N','','-f',key])
        public=key.with_suffix('.pub').read_text().strip()
        line=('from="'+NODES[0]+'",no-agent-forwarding,no-port-forwarding,no-X11-forwarding,no-pty '+public+'\n').encode()
        # Obtain the host identity over the already authenticated management
        # connection, rather than trusting an unauthenticated keyscan.
        hostkeys=[]
        for host in NODES[1:3]:
            hostkey=run([*SSH,'root@'+host,'cat /etc/ssh/ssh_host_ecdsa_key.pub'],capture_output=True,text=True).stdout.strip()
            hostkeys.append(host+' '+hostkey+'\n')
            saved.append((host,probe_authorization(line,host=host)))
        known=keydir/'known_hosts';known.write_text(''.join(hostkeys));known.chmod(0o600)
        run(['scp','-q','-o','BatchMode=yes',key,known,'root@'+NODES[0]+':'+directory+'/'])
        for host in NODES[1:3]:
            probe='ssh -i '+directory+'/key -o IdentitiesOnly=yes -o UserKnownHostsFile='+directory+'/known_hosts -o StrictHostKeyChecking=yes -o BatchMode=yes -o ConnectTimeout=8 root@'+host+' true'
            run([*SSH,'root@'+NODES[0],probe])
        print('TOOLS_CALICO_PROBE_SSH_PASS identity=ephemeral hostkey=authenticated',flush=True)
        yield {'CALICO_PROBE_SSH_KEY':directory+'/key','CALICO_PROBE_KNOWN_HOSTS':directory+'/known_hosts'}
    finally:
        for host,state in reversed(saved):probe_authorization(line,state,host=host)
        shutil.rmtree(keydir)


def business(probe_only=False):
    target='root@'+NODES[0]
    directory='/tmp/tools-calico-business'
    run([*SSH,target,'mkdir -m 700 '+directory])
    try:
        run(['scp','-q','-o','BatchMode=yes',Path(__file__).with_name('CalicoPolicyCli'),Path(__file__).with_name('calico-live-regression.sh'),target+':'+directory+'/'])
        environment={'CALICO_TOOL':directory+'/CalicoPolicyCli',
            'CALICO_CONTEXT':'context-'+NAME,'CALICO_HOST_IP':NODES[0],
            'CALICO_DENY_HOST':NODES[1],
            'CALICO_ALLOW_HOST':NODES[2],
            'CALICO_PROBE_IMAGE':'registry.talkschool.cn:5000/brinnatt/busybox@sha256:94dee190d1e5f7dce9017bfe7113441013efb471bfe3964f031e8906a95df781'}
        with probe_identity(directory) as identity:
            if not probe_only:
                environment.update(identity)
                command='env '+' '.join(k+'='+shlex.quote(v) for k,v in environment.items())+' bash '+directory+'/calico-live-regression.sh'
                run([*SSH,target,command],timeout=900)
    finally:
        run([*SSH,target,'rm -rf '+directory])


def restore_control(original, saved, mode):
    if saved is None:
        if original.exists():original.unlink()
    else:
        original.parent.mkdir(exist_ok=True)
        original.write_bytes(saved)
        original.chmod(mode)


def main():
    if subprocess.check_output(['hostname','-I'],text=True).split()[0]!='192.168.47.130':
        raise ValueError('TOOLS_CALICO_FIXTURE_CONTROL_REFUSED')
    if ROOT.exists():raise ValueError('TOOLS_CALICO_FIXTURE_RESIDUE_REFUSED')
    def interrupted(signum, frame):raise SystemExit(128+signum)
    signal.signal(signal.SIGTERM,interrupted)
    signal.signal(signal.SIGINT,interrupted)
    with contextlib.ExitStack() as stack:
        for name in ('kubeauto-core-pool.lock','kubeauto-redis-core-pool.lock'):
            lease=stack.enter_context(open('/var/lock/'+name,'a'))
            fcntl.flock(lease,fcntl.LOCK_EX|fcntl.LOCK_NB)
        states={}
        probe="import json,pathlib,subprocess; units=['etcd','kubelet','containerd','docker','kube-apiserver']; print(json.dumps(dict(units=[subprocess.run(['systemctl','is-active',u],capture_output=True,text=True).stdout.strip() for u in units],kubernetes=pathlib.Path('/etc/kubernetes').exists(),kubeconfig=pathlib.Path('/root/.kube/config').exists())))"
        for ip in NODES:
            states[ip]=json.loads(subprocess.check_output([*SSH,'root@'+ip,'python3 -c '+shlex.quote(probe)],text=True))
        admit(states)
        ROOT.mkdir(mode=0o700)
        (ROOT/'owner.json').write_text(json.dumps(dict(nodes=NODES,states=states,images=BINS)))
        ignored=shutil.ignore_patterns('.git','.venv','logs','clusters','down','kube-bin','extra-bin','docker-bin','__pycache__','tests','tools','docs','dist','build','upgrade','.idea')
        shutil.copytree(SOURCE,BASE,ignore=ignored)
        (ROOT/'fixture-source-sha256.json').write_text(json.dumps({str(p.relative_to(BASE)):hashlib.sha256(p.read_bytes()).hexdigest() for p in BASE.rglob('*') if p.is_file()},indent=2)+'\n')
        original=Path('/root/.kube/config')
        saved=original.read_bytes() if original.is_file() else None
        mode=original.stat().st_mode & 0o777 if saved is not None else None
        owned=False
        cleanup_errors=[]
        try:
            stage_binaries()
            product('new',NAME)
            cluster=BASE/'clusters'/NAME
            config=cluster/'config.yml'
            config.write_text(yaml.safe_dump(configure(yaml.safe_load(config.read_text())),sort_keys=False))
            (cluster/'hosts').write_text(inventory())
            owned=True
            for step in ('01','02','03','04','05','06'):
                print('TOOLS_CALICO_FIXTURE_STAGE setup-'+step,flush=True)
                product('setup',NAME,step)
            kubeconfig=cluster/'kubectl.kubeconfig'
            kubectl=str(BASE/'kube-bin/kubectl')
            run([kubectl,'--kubeconfig='+str(kubeconfig),'wait','nodes','--all','--for=condition=Ready','--timeout=180s'])
            print('TOOLS_CALICO_FIXTURE_READY kubernetes=1.33.6 calico=3.28.4',flush=True)
            business()
        finally:
            if owned:
                for arguments in (['--rocky-only',*NODES],['--verify','--rocky-only',*NODES]):
                    try:run(['bash',SOURCE/'tests/helpers/lab-wipe-nodes.sh',*arguments],timeout=1200)
                    except Exception as exc:cleanup_errors.append(str(exc))
            restore_control(original,saved,mode)
            archive=Path('/tmp/tools-calico-fixture-evidence.tar.gz')
            with tarfile.open(archive,'w:gz') as bundle:
                for name in ('owner.json','binary-sha256.json','fixture-source-sha256.json'):
                    if (ROOT/name).is_file():bundle.add(ROOT/name,arcname=name)
            # Preserve diagnostics if node cleanup failed; never report clean.
            if cleanup_errors:raise RuntimeError('TOOLS_CALICO_FIXTURE_CLEAN_FAILED '+repr(cleanup_errors))
            shutil.rmtree(ROOT)
            if ROOT.exists():raise ValueError('TOOLS_CALICO_FIXTURE_CLEAN_FAILED')
            print('TOOLS_CALICO_FIXTURE_SCOPED_CLEAN_PASS',flush=True)


if __name__=='__main__':
    if sys.argv[1:]==['--probe-only']:
        with contextlib.ExitStack() as stack:
            for name in ('kubeauto-core-pool.lock','kubeauto-redis-core-pool.lock'):
                lease=stack.enter_context(open('/var/lock/'+name,'a'))
                fcntl.flock(lease,fcntl.LOCK_EX|fcntl.LOCK_NB)
            if ROOT.exists():raise ValueError('TOOLS_CALICO_FIXTURE_RESIDUE_REFUSED')
            ROOT.mkdir(mode=0o700)
            try:business(probe_only=True)
            finally:shutil.rmtree(ROOT)
    elif sys.argv[1:]==['--stage-only']:
        if ROOT.exists():raise ValueError('TOOLS_CALICO_FIXTURE_RESIDUE_REFUSED')
        ROOT.mkdir(mode=0o700);BASE.mkdir()
        try:
            stage_binaries()
            print('TOOLS_CALICO_BINARY_STAGE_PASS',flush=True)
        finally:shutil.rmtree(ROOT)
    elif not sys.argv[1:]:main()
    else:raise SystemExit(2)
