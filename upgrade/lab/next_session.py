"""Fixed next-hop lab session; customer behavior always uses config + kubecli.

Run by next-session.sh on the authorized jumper. The shell retains all shared
pool locks while a focused boundary waits for evidence collection or diagnosis.
No generic host inventory, arbitrary SSH or whole-project regression is exposed.
"""
from __future__ import annotations

import argparse
import fcntl
import signal
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time

import yaml

from upgrade.product.launcher import admit_static, UpgradeAdmissionRefused
from upgrade import validate

ROOT = Path(__file__).resolve().parents[2]
STATE = Path('/var/tmp/kubeauto-v13510-lab')
MATRIX = ROOT/'upgrade/v1.35.10/test-matrix.yaml'
SOURCE_COMMIT = '0eac368744cf82fdd401d278858859927a82e387'
MASTERS = ['192.168.122.217', '192.168.122.243', '192.168.122.246']
WORKERS = ['192.168.122.193', '192.168.122.210', '192.168.122.216']
SOURCE_CLUSTER, TARGET_CLUSTER = 'upgrade-v13413-source', 'upgrade-v13510-clean'
PHASES = ['source', 'control', 'nodes-protection', 'nodes', 'source-clean', 'new-install', 'target-clean']
REQUIRED = {'source': range(1, 5), 'control': range(1, 6), 'nodes': range(1, 7), 'nodes-protection': range(1, 7),
            'source-clean': range(1, 8), 'new-install': range(1, 5), 'target-clean': range(1, 5)}
LEDGER = 'kubeauto-v13413-to-v13510-persistent-ledger\n'
LOCK_DIRECTORY = Path('/var/lock')
LOCKS = {9:'kubeauto-ceph-priority.lock',7:'kubeauto-ceph-compute.lock',
         8:'kubeauto-redis-core-pool.lock',6:'kubeauto-v13510-lab.lock'}


def verify_leases():
    for descriptor, name in LOCKS.items():
        path = LOCK_DIRECTORY/name
        if os.fstat(descriptor).st_ino != path.stat().st_ino:
            raise ValueError('fixed lab lease descriptor identity differs')
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)


class CommandFailed(RuntimeError):
    def __init__(self, executable, rc):
        self.rc = rc
        super().__init__('fixed command failed rc='+str(rc)+': '+str(executable))


def sha(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def verify_inputs(state, target):
    """Verify staged files before acquiring node ownership; never fetch inputs."""
    manifest = state/'input-manifest.json'
    if manifest.is_symlink() or not manifest.is_file():
        raise ValueError('fixed input manifest is missing or unsafe')
    data = json.loads(manifest.read_text())
    if data.get('source_commit') != SOURCE_COMMIT or set(data.get('trees', {})) != {'source-product','target-product'}:
        raise ValueError('source commit or staged product set differs')
    if target.resolve() != (state/'target-product').resolve():
        raise ValueError('target checkout is outside the fixed staging directory')
    for name, files in data['trees'].items():
        if not isinstance(files, dict) or not files:
            raise ValueError('staged input tree is incomplete')
        root = state/name
        for relative, digest in files.items():
            path = root/relative
            if (Path(relative).is_absolute() or '..' in Path(relative).parts or
                    path.is_symlink() or not path.resolve().is_relative_to(root.resolve()) or
                    not re.fullmatch('[a-f0-9]{64}', str(digest)) or sha(path) != digest):
                raise ValueError('staged product input differs: '+name+'/'+relative)
    return data


def verify_registry_supply(path):
    from upgrade.lab.next_registry import LAB, scope
    data=json.loads(Path(path).read_text())
    expected=scope()
    records=data.get('records',[])
    if (data.get('ready') is not True or data.get('registry')!=LAB or
            data.get('builds')!=[] or data.get('existing_tags_overwritten')!=[] or
            len(records)!=len(expected) or {r['image'] for r in records}!=set(expected) or
            any(r.get('action') not in ['reuse-equivalent-runtime-and-rootfs','copied-missing-amd64-manifest'] or
                r.get('source_child')!=expected[r['image']]['platforms']['linux/amd64'] for r in records)):
        raise ValueError('fixed lab registry supply is incomplete or differs from the admitted image scope')


def write(path, data):
    path = Path(path)
    temporary = path.with_name(path.name+'.tmp')
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(descriptor, 'w') as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2); stream.write('\n')
            stream.flush(); os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def phase_log(path):
    """Stream one attempt to both its immutable child log and durable parent."""
    if path.exists():
        raise ValueError('phase attempt log already exists')
    sys.stdout.flush(); sys.stderr.flush()
    stdout, stderr = os.dup(1), os.dup(2)
    process = subprocess.Popen(['tee', str(path)], stdin=subprocess.PIPE)
    try:
        os.dup2(process.stdin.fileno(), 1); os.dup2(process.stdin.fileno(), 2)
        yield
    finally:
        sys.stdout.flush(); sys.stderr.flush()
        os.dup2(stdout, 1); os.dup2(stderr, 2)
        os.close(stdout); os.close(stderr)
        process.stdin.close()
        if process.wait(timeout=10) != 0:
            raise RuntimeError('phase log persistence failed')


def inventory(base, cluster):
    sections = []
    for group, hosts in [('etcd', MASTERS), ('kube_master', MASTERS), ('kube_node', WORKERS)]:
        entries = [host + ('' if group == 'etcd' else
                   " k8s_nodename='upgrade-" + ('master-' if group == 'kube_master' else 'worker-') +
                   host.rsplit('.', 1)[1] + "'") for host in hosts]
        sections.append('['+group+']\n'+'\n'.join(entries))
    sections.extend(['[harbor]', '[ex_lb]', '[chrony]', '''[all:vars]
SECURE_PORT=6443
CONTAINER_RUNTIME="containerd"
CLUSTER_NETWORK="calico"
PROXY_MODE="ipvs"
SERVICE_CIDR="10.73.0.0/16"
CLUSTER_CIDR="172.26.0.0/16"
NODE_PORT_RANGE="30000-32767"
CLUSTER_DNS_DOMAIN="cluster.local"
bin_dir="/usr/local/bin"
base_dir="'''+str(base)+'''"
cluster_dir="{{ base_dir }}/clusters/'''+cluster+'''"
ca_dir="/etc/kubernetes/ssl"
ansible_user=root
'''])
    return '\n\n'.join(sections)


def cluster_names(run_id):
    # Qualification and final acceptance own distinct cluster configurations,
    # prepare receipts and journals, even when the same clean pool is reused.
    suffix=hashlib.sha256(run_id.encode()).hexdigest()[:8]
    return SOURCE_CLUSTER+'-'+suffix, TARGET_CLUSTER+'-'+suffix


def stage_upgrade_clients(source, target, run_id):
    """Keep per-run source clients and merge verified packs with hard-link reuse."""
    original=source/'kube-bin'
    backup=source/('kube-bin-source-'+run_id)
    replacement=source/('kube-bin-target-'+run_id)
    if not backup.exists():
        shutil.copytree(target/'kube-bin',replacement,dirs_exist_ok=True)
        original.rename(backup)
    if replacement.exists():replacement.rename(original)

    def copy_distinct(src,dst):
        # rsync -H preserves qualified input deduplication. Unchanged files may
        # already be the same inode; copying those is unnecessary and rejected
        # by shutil.copy2. Changed inputs still use its normal metadata copy.
        if Path(dst).exists() and os.path.samefile(src,dst):return dst
        return shutil.copy2(src,dst)

    shutil.copytree(target/'extra-bin',source/'extra-bin',dirs_exist_ok=True,
                    copy_function=copy_distinct)


def plan(mode, run_id):
    if mode not in ['qualify', 'final'] or not re.fullmatch(r'v13510-[a-z0-9-]{1,48}', run_id):
        raise ValueError('invalid fixed session identity')
    required = [f'UPG-{i:02}' for i in (range(1, 15) if mode == 'final' else range(1, 5))]
    admit_static(MATRIX, required=required)
    matrix = validate.read_mapping(MATRIX)
    if (matrix['meta'].get('execution_enabled') is not True or
            matrix['meta'].get('runner') != 'upgrade/lab/next-session.sh'):
        raise UpgradeAdmissionRefused('fixed next-hop runner is not enabled and bound')
    return {'run_id': run_id, 'mode': mode, 'control': '192.168.122.2',
        'registry': '192.168.122.2:5000', 'masters': MASTERS, 'workers': WORKERS,
        'source_commit': SOURCE_COMMIT, 'source': 'v1.34.13', 'target': 'v1.35.10',
        'clusters':list(cluster_names(run_id)),
        'phases': PHASES, 'builds': [], 'middleware_deep_regressions': [],
        'entry': 'config.yml + kubecli new/setup/upgrade/destroy',
        'cleanup': 'only marked pool nodes after cluster CA verification; no disks or control services',
        'control_home': 'private mount namespace; original SSH directory mounted read-only'}


class Session:
    def __init__(self, mode, run_id):
        self.plan = plan(mode, run_id)
        self.mode, self.run_id = mode, run_id
        self.source_cluster,self.target_cluster=cluster_names(run_id)
        self.state = STATE
        self.source = STATE/'source-product'
        self.target = ROOT
        self.evidence = STATE/'runs'/run_id
        self.owner = STATE/'owner.json'
        self.cluster = None
        self.base = self.source

    def command(self, argv, *, base=None, code=None, capture=False, data=None, log=None):
        environment = {**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'}
        if base:
            environment.update(KUBEAUTO_BASE_PATH=str(base),
                KUBECONFIG=str(base/'clusters'/self.cluster/'kubectl.kubeconfig'),
                PATH=str(base/'kube-bin')+':'+str(base/'extra-bin')+':'+os.environ['PATH'])
        if log:
            # The PDB refusal is an expected negative test. Retain its complete
            # raw output separately, never suppress an unexpected failure.
            with Path(log).open('x') as stream:
                with subprocess.Popen([str(arg) for arg in argv], cwd=code or self.target,
                        env=environment, stdin=subprocess.DEVNULL, stdout=stream,
                        stderr=subprocess.STDOUT, text=True) as process:
                    started = time.monotonic()
                    while True:
                        try:
                            rc = process.wait(timeout=20)
                            break
                        except subprocess.TimeoutExpired:
                            print('NEXT_SESSION_COMMAND_HEARTBEAT log='+Path(log).name+
                                  ' elapsed='+str(int(time.monotonic()-started)), flush=True)
                            if time.monotonic()-started > 3600:
                                process.kill(); process.wait()
                                raise RuntimeError('fixed command timeout; retain diagnostics')
                if rc:
                    raise CommandFailed(argv[0], rc)
            return None
        result = subprocess.run([str(arg) for arg in argv], cwd=code or self.target,
            env=environment, text=True, input=data, stdin=subprocess.DEVNULL if data is None else None,
            stdout=subprocess.PIPE if capture else None,
            stderr=subprocess.PIPE if capture else None, timeout=3600)
        if result.returncode:
            if capture:
                print(result.stderr, file=sys.stderr, flush=True)
            raise CommandFailed(argv[0], result.returncode)
        return result.stdout if capture else None

    def cli(self, base, code, *args, log=None):
        return self.command(['bash', ROOT/'upgrade/lab/isolate-control.sh', STATE,
                             sys.executable, code/'kubecli.py', *args], base=base, code=code, log=log)

    def kubectl(self, *args, capture=True, data=None):
        return self.command([self.base/'kube-bin/kubectl',
            '--kubeconfig='+str(self.base/'clusters'/self.cluster/'kubectl.kubeconfig'),
            '--request-timeout=15s', *args], base=self.base, capture=capture, data=data)

    def host_operation(self, operation):
        extra = {'NEXT_HOST_OPERATION': operation, 'NEXT_RUN_ID': self.run_id}
        if operation in ['owned', 'storage-clean']:
            extra['NEXT_CA_SHA256'] = sha(self.base/'clusters'/self.cluster/'ssl/ca.pem')
        self.command(['ansible-playbook', '-i', self.evidence/'hosts',
            ROOT/'upgrade/lab/next-hosts.yml', '-e', json.dumps(extra)])

    def owner_check(self):
        # Accept only inherited descriptors for the actual shared lock files.
        # Direct invocation cannot silently omit the fixed shell supervisor.
        verify_leases()
        if (self.state.is_symlink() or self.state.stat().st_uid != 0 or
                self.state.stat().st_mode & 0o777 != 0o700):
            raise ValueError('private root-owned staging directory required')
        if self.owner.exists() or self.owner.is_symlink():
            raise ValueError('another lab session boundary exists; do not overwrite or clean it')
        if any((base/'clusters'/cluster).exists() for base, cluster in
               [(self.source, self.source_cluster), (self.target, self.target_cluster)]):
            raise ValueError('foreign cluster directory exists before ownership')
        verify_inputs(self.state, self.target)
        verify_registry_supply(self.target/'upgrade/v1.35.10/evidence/lab-registry-supply.json')
        self.evidence.mkdir(mode=0o700, parents=True, exist_ok=False)
        self.evidence.joinpath('hosts').write_text(inventory(self.source, self.source_cluster))
        write(self.owner, self.plan)
        write(self.evidence/'plan.json', self.plan)

    def select(self, target=False):
        self.base = self.target if target else self.source
        self.cluster = self.target_cluster if target else self.source_cluster

    def configure(self, target=False):
        self.select(target)
        self.cli(self.base, self.base, 'new', self.cluster)
        directory = self.base/'clusters'/self.cluster
        (directory/'hosts').write_text(inventory(self.base, self.cluster))
        data = yaml.safe_load((directory/'config.yml').read_text())
        data.update(K8S_VER='1.35.10' if target else '1.34.13',
            REGISTRY_HOST_IP='192.168.122.2', KUBE_RESERVED_ENABLED='no', SYS_RESERVED_ENABLED='no',
            local_path_provisioner_install='yes', local_path_provisioner_dir='/opt/local-path-provisioner',
            local_path_provisioner_ver='v0.0.37', local_path_storage_class='local-path')
        (directory/'config.yml').write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False))
        (directory/'config.yml').chmod(0o600)
        write(self.evidence/('target-config.json' if target else 'source-config.json'),
              {'path':str(directory/'config.yml'),'sha256':sha(directory/'config.yml')})

    def healthy(self, expected):
        data = json.loads(self.kubectl('get', 'nodes', '-o', 'json'))['items']
        if (len(data) != 6 or {n['status']['nodeInfo']['kubeletVersion'] for n in data} != {expected} or
                any([c.get('status') for c in n['status']['conditions'] if c['type']=='Ready'] != ['True'] for n in data)):
            raise ValueError('actual six-node version or readiness differs from the admitted phase')
        for host in MASTERS:
            self.kubectl('--server=https://'+host+':6443', 'get', '--raw=/readyz')
        pods = json.loads(self.kubectl('get', 'pods', '-A', '-o', 'json'))['items']
        if any(p.get('status', {}).get('phase') not in ['Running', 'Succeeded'] or
               (p['status']['phase']=='Running' and
                [c.get('status') for c in p['status'].get('conditions',[]) if c['type']=='Ready']!=['True']) for p in pods):
            raise ValueError('cluster has an unhealthy workload; do not advance')
        return {'nodes':data,'pods':pods}

    def wait_healthy(self, expected):
        deadline=time.monotonic()+300
        while True:
            try:return self.healthy(expected)
            except (RuntimeError, ValueError) as error:
                if time.monotonic()>=deadline:raise
                print('NEXT_SESSION_HEALTH_WAIT expected='+expected+' reason='+str(error),flush=True)
                time.sleep(10)

    def source_phase(self):
        self.select()
        if not (self.evidence/'source-config.json').exists():
            self.host_operation('admit')
            self.host_operation('claim')
            self.configure()
        else:
            self.host_operation('marked')
            recorded=json.loads((self.evidence/'source-config.json').read_text())
            if sha(recorded['path']) != recorded['sha256']:
                raise ValueError('source configuration changed since this session created it')
        if not (self.evidence/'source-health.json').exists():
            self.cli(self.source, self.source, 'setup', self.cluster, '90')
        observed = self.wait_healthy('v1.34.13')
        write(self.evidence/'source-health.json', observed)
        # A PVC-backed server and a separate controller-managed client give a
        # real service/DNS/storage ledger. No test writes after admission.
        self.create_ledger()
        stage_upgrade_clients(self.source,self.target,self.run_id)
        request = self.evidence/'continuity.json'
        write(request, {'namespace':'upgrade-ledger','service':'ledger','pvc':'ledger',
            'client_selector':'app=upgrade-client','ledger_sha256':hashlib.sha256(LEDGER.encode()).hexdigest()})
        state = Path('/var/tmp/kubeauto-upgrade-'+self.cluster)
        if not Path(str(state)+'.receipt.json').exists():
            self.cli(self.source, self.target, 'upgrade', self.cluster, '--prepare', '--continuity', request)
        receipt = json.loads(Path(str(state)+'.receipt.json').read_text())
        if receipt.get('recovery_point',{}).get('offline_restore_verified') is not True:
            raise ValueError('source snapshot has no offline integrity proof')
        write(self.evidence/'prepared-boundary.json',receipt)
        print('UPGRADE_NEXT_SOURCE_PASS',flush=True)

    def create_ledger(self):
        identity=self.evidence/'ledger-namespace.json'
        if identity.exists():
            recorded=json.loads(identity.read_text())
            actual=json.loads(self.kubectl('get','namespace','upgrade-ledger','-o','json'))
            if actual['metadata']['uid']!=recorded['metadata']['uid']:
                raise ValueError('fixture namespace identity changed')
        else:
            # create, rather than apply, refuses an existing foreign namespace.
            actual=json.loads(self.kubectl('create','namespace','upgrade-ledger','-o','json'))
            write(identity,actual)
        fixture = [obj for obj in yaml.safe_load_all((ROOT/'upgrade/lab/next-ledger.yaml').read_text())
                   if obj['kind']!='Namespace']
        self.kubectl('apply','-f','-', data='---\n'.join(yaml.safe_dump(obj) for obj in fixture),capture=False)
        self.kubectl('-n','upgrade-ledger','rollout','status','deployment/ledger','--timeout=300s')
        self.kubectl('-n','upgrade-ledger','rollout','status','deployment/upgrade-client','--timeout=300s')
        result = self.kubectl('-n','upgrade-ledger','exec','deployment/upgrade-client','--',
            'wget','-T','10','-qO-','http://ledger:8080/ledger')
        if result != LEDGER:
            raise ValueError('source fixture failed the original ledger read')

    def control_phase(self):
        self.select()
        self.cli(self.source,self.target,'upgrade',self.cluster)
        result = json.loads(Path('/var/tmp/kubeauto-upgrade-'+self.cluster+'/first-hop-result.json').read_text())
        if result['success_marker']!='UPGRADE_CONTROL_PLANE_STAGE_PASS':
            raise ValueError('control-plane handoff did not stop at its exact boundary')
        self.healthy('v1.34.13')
        write(self.evidence/'control-boundary.json',result)
        print('UPGRADE_NEXT_CONTROL_PASS',flush=True)

    def protection_phase(self):
        self.select()
        obj={'apiVersion':'policy/v1','kind':'PodDisruptionBudget',
            'metadata':{'name':'upgrade-protection','namespace':'upgrade-ledger'},
            'spec':{'minAvailable':1,'selector':{'matchLabels':{'app':'upgrade-ledger'}}}}
        identity=self.evidence/'protection-pdb.json'
        if not identity.exists():
            pdb=json.loads(self.kubectl('create','-f','-','-o','json',data=json.dumps(obj)))
            write(identity,pdb)
        else:
            pdb=json.loads(self.kubectl('-n','upgrade-ledger','get','pdb','upgrade-protection','-o','json'))
            if pdb['metadata']['uid']!=json.loads(identity.read_text())['metadata']['uid']:
                raise ValueError('protection fixture ownership changed')
        attempt=1+len(list(self.evidence.glob('expected-pdb-refusal-*.log')))
        refusal_log=self.evidence/('expected-pdb-refusal-'+str(attempt)+'.log')
        try:
            self.cli(self.source,self.target,'upgrade',self.cluster,
                     log=refusal_log)
        except CommandFailed:
            raw_log=refusal_log.read_text()
            if 'Cannot evict pod as it would violate the pod\'s disruption budget' not in raw_log:
                raise ValueError('negative run did not report the intended PDB refusal') from None
            state=Path('/var/tmp/kubeauto-upgrade-'+self.cluster)
            journal=json.loads((state/'journal/journal.json').read_text())
            if journal.get('active')!='kubernetes/node/upgrade-worker-193':
                raise ValueError('upgrade stopped outside the intended PDB-owned node') from None
            observed=self.healthy('v1.34.13') if not journal.get('completed') else json.loads(self.kubectl('get','nodes','-o','json'))
            nodes=observed.get('items',observed.get('nodes',[]))
            blocked=next(n for n in nodes if n['metadata']['name']=='upgrade-worker-193')
            if blocked['status']['nodeInfo']['kubeletVersion']!='v1.34.13' or not blocked['spec'].get('unschedulable'):
                raise ValueError('protected node changed its executable or lost its cordon')
            raw=self.kubectl('-n','upgrade-ledger','exec','deployment/upgrade-client','--',
                'wget','-T','10','-qO-','http://ledger:8080/ledger')
            if raw!=LEDGER:raise ValueError('PDB refusal changed the original ledger')
            write(self.evidence/'protection-journal.json',journal)
        else:
            raise ValueError('PDB fixture did not prevent the node upgrade')
        print('UPGRADE_NEXT_PDB_REFUSAL_PASS',flush=True)

    def nodes_phase(self):
        self.select()
        if self.mode=='qualify' and not (self.evidence/'protection-released.json').exists():
            expected=json.loads((self.evidence/'protection-pdb.json').read_text())
            current=json.loads(self.kubectl('-n','upgrade-ledger','get','pdb','upgrade-protection','-o','json'))
            if current['metadata']['uid']!=expected['metadata']['uid']:
                raise ValueError('protection fixture ownership changed')
            self.kubectl('delete','--raw=/apis/policy/v1/namespaces/upgrade-ledger/poddisruptionbudgets/upgrade-protection',
                '-f','-',data=json.dumps({'apiVersion':'v1','kind':'DeleteOptions',
                    'preconditions':{'uid':expected['metadata']['uid']}}))
            write(self.evidence/'protection-released.json',{'uid':expected['metadata']['uid']})
        self.cli(self.source,self.target,'upgrade',self.cluster)
        observed = self.wait_healthy('v1.35.10')
        result = json.loads(Path('/var/tmp/kubeauto-upgrade-'+self.cluster+'/first-hop-result.json').read_text())
        if result['success_marker']!='UPGRADE_NEXT_HOP_STAGE_PASS':
            raise ValueError('node/addon/config handoff is incomplete')
        write(self.evidence/'nodes-health.json',observed)
        write(self.evidence/'nodes-boundary.json',result)
        self.cli(self.source,self.target,'upgrade',self.cluster)
        print('UPGRADE_NEXT_NODES_PASS',flush=True)

    def clean_phase(self, target=False):
        self.select(target)
        self.host_operation('owned')
        # Release fixture volumes while their owning provisioner is healthy.
        # Only this session's namespace was created; its UID is bound above.
        namespaces=json.loads(self.kubectl('get','namespaces','-o','json'))['items']
        for namespace in namespaces:
            if namespace['metadata']['name']!='upgrade-ledger':continue
            identity=self.evidence/'ledger-namespace.json'
            if target or not identity.exists():
                raise ValueError('unowned fixture namespace; cleanup refused')
            recorded=json.loads(identity.read_text())
            if recorded['metadata']['uid']!=namespace['metadata']['uid']:
                raise ValueError('namespace was replaced; cleanup refused')
            self.kubectl('delete','--raw=/api/v1/namespaces/upgrade-ledger','-f','-',
                data=json.dumps({'apiVersion':'v1','kind':'DeleteOptions',
                    'preconditions':{'uid':recorded['metadata']['uid']}}))
            self.kubectl('wait','--for=delete','namespace/upgrade-ledger','--timeout=300s')
        if json.loads(self.kubectl('get','pv','-o','json'))['items']:
            raise ValueError('volumes remain; retain the cluster for scoped storage cleanup')
        self.host_operation('storage-clean')
        self.cli(self.base,self.target,'destroy',self.cluster)
        # Product destroy removes the cluster. The existing lab wipe also
        # reaps KillMode=process runtime children and drops failed unit records.
        # Only these leased nodes are admitted; never include the control host.
        self.command(['bash', ROOT/'tests/helpers/lab-wipe-nodes.sh', '--rocky-only',
                      *MASTERS, *WORKERS], capture=False)
        self.host_operation('clean')
        print('UPGRADE_NEXT_'+('TARGET' if target else 'SOURCE')+'_CLEAN_PASS',flush=True)

    def install_phase(self):
        if not (self.evidence/'target-config.json').exists():
            self.host_operation('clean')
            self.configure(target=True)
        else:
            self.select(target=True)
            self.host_operation('marked')
            recorded=json.loads((self.evidence/'target-config.json').read_text())
            if sha(recorded['path'])!=recorded['sha256']:
                raise ValueError('target configuration changed since this session created it')
        self.cli(self.target,self.target,'setup',self.cluster,'90')
        before=self.wait_healthy('v1.35.10')
        self.cli(self.target,self.target,'setup',self.cluster,'90')
        after=self.wait_healthy('v1.35.10')
        identity=lambda d:{n['metadata']['name']:n['metadata']['uid'] for n in d['nodes']}
        if identity(before)!=identity(after):raise ValueError('second setup changed node identities')
        write(self.evidence/'target-install.json',after)
        print('UPGRADE_NEXT_INSTALL_PASS',flush=True)

    def wait(self, phase, *, failed=False):
        print('NEXT_SESSION_WAIT phase='+phase+' failed='+str(failed).lower()+' lease=held',flush=True)
        signal=self.evidence/'action.json'
        while True:
            if signal.exists():
                try:
                    value=json.loads(signal.read_text())
                    if not isinstance(value,dict):raise ValueError('object required')
                except (ValueError, OSError):
                    # An invalid control message cannot unwind this wait and
                    # release locks while the session still owns live nodes.
                    print('NEXT_SESSION_ACTION_REFUSED malformed signal; lease=retained',flush=True)
                    signal.unlink(missing_ok=True)
                    continue
                signal.unlink()
                if value.get('run_id')!=self.run_id or value.get('phase')!=phase:
                    print('NEXT_SESSION_ACTION_REFUSED foreign session signal',flush=True)
                    continue
                action=value.get('action')
                if action=='cleanup':return 'cleanup'
                if action=='retry' and failed:return 'retry'
                if action=='advance' and not failed:return 'advance'
                print('NEXT_SESSION_ACTION_REFUSED action is invalid at this boundary',flush=True)
                continue
            print('NEXT_SESSION_HEARTBEAT phase='+phase+' boundary='+('failed' if failed else 'accepted-child')+
                  ' pool=leased next=collect-evidence-or-scoped-recovery',flush=True)
            time.sleep(20)

    def preserve_boundary(self, phase, attempt):
        if not self.cluster:return
        state=Path('/var/tmp/kubeauto-upgrade-'+self.cluster)
        if not state.exists():return
        destination=self.evidence/(phase+'.attempt-'+str(attempt)+'.product-state')
        shutil.copytree(state,destination)
        receipt=Path(str(state)+'.receipt.json')
        if receipt.is_file():shutil.copy2(receipt,destination/'prepared-receipt.json')

    def shutdown(self):
        if self.cluster and (self.base/'clusters'/self.cluster/'config.yml').is_file():
            self.clean_phase(target=self.cluster==self.target_cluster)
        self.host_operation('release')
        self.owner.unlink()
        print('NEXT_SESSION_ABORT_CLEAN_VERIFY_PASS',flush=True)

    def guarded_shutdown(self, phase):
        while True:
            try:
                self.shutdown()
                return
            except Exception as error:
                print('NEXT_SESSION_CLEANUP_STOP lease=retained reason='+str(error),flush=True)
                self.wait(phase,failed=True)

    def run(self):
        # Signals cannot release an owned pool with live fixtures. A frontend
        # can request verified cleanup through action.json while the lease stays held.
        for signum in [signal.SIGINT,signal.SIGTERM]:
            signal.signal(signum,lambda signum,frame:print('NEXT_SESSION_SIGNAL lease=retained cleanup=required',flush=True))
        self.owner_check()
        operations=[self.source_phase,self.control_phase,self.protection_phase,self.nodes_phase,self.clean_phase,
                    self.install_phase,lambda:self.clean_phase(target=True)]
        for phase,operation in zip(PHASES,operations):
            if self.mode=='final' and phase=='nodes-protection':continue
            while True:
                started=datetime.now(timezone.utc).isoformat()
                attempt=1+len(list(self.evidence.glob(phase+'.attempt-*.log')))
                log=self.evidence/(phase+'.attempt-'+str(attempt)+'.log')
                try:
                    with phase_log(log):
                        if self.mode=='qualify':
                            admit_static(MATRIX,required=[f'UPG-{i:02}' for i in REQUIRED[phase]])
                        operation()
                        self.preserve_boundary(phase,attempt)
                        self.host_operation('marked')
                        print('NEXT_SESSION_PHASE_SCOPE_VERIFY_PASS phase='+phase+' ownership=retained',flush=True)
                except Exception as error:
                    failure={'run_id':self.run_id,'phase':phase,'attempt':attempt,'log':log.name,
                        'rc':1,'started':started,'reason':str(error),'cleanup':'not-performed-lease-retained'}
                    write(self.evidence/(phase+'.failure-'+str(attempt)+'.json'),failure)
                    write(self.evidence/(phase+'.status.json'),failure)
                    print('NEXT_SESSION_MODULE_STOP phase='+phase+' reason='+str(error),flush=True)
                    action=self.wait(phase,failed=True)
                    if action=='retry':continue
                    # Cleanup still verifies markers and the actual cluster CA.
                    self.guarded_shutdown(phase)
                    raise RuntimeError('session failed; owned cleanup completed') from error
                write(self.evidence/(phase+'.status.json'),{'run_id':self.run_id,'phase':phase,
                    'rc':0,'attempt':attempt,'log':log.name,'started':started,
                    'finished':datetime.now(timezone.utc).isoformat()})
                for suffix in ['exit','finalized']:
                    path=self.evidence/(phase+'.attempt-'+str(attempt)+'.'+suffix)
                    with path.open('x') as stream:
                        stream.write('0\n');stream.flush();os.fsync(stream.fileno())
                break
            if self.mode=='qualify' and phase!=PHASES[-1]:
                if self.wait(phase)=='cleanup':
                    self.guarded_shutdown(phase)
                    raise RuntimeError('qualification ended before completing the frozen plan')
        while True:
            try:
                self.host_operation('release')
                break
            except Exception as error:
                print('NEXT_SESSION_RELEASE_STOP lease=retained reason='+str(error),flush=True)
                self.wait('target-clean',failed=True)
        print('LAB_CLEAN_VERIFY_PASS',flush=True)
        self.owner.unlink()
        print('UPGRADE_NEXT_'+('FINAL' if self.mode=='final' else 'QUALIFICATION')+'_PASS',flush=True)


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation',choices=['plan','run'])
    parser.add_argument('--mode',choices=['qualify','final'],required=True)
    parser.add_argument('--run-id',required=True)
    args=parser.parse_args(argv)
    if args.operation=='plan':print(json.dumps(plan(args.mode,args.run_id),ensure_ascii=False,indent=2))
    else:Session(args.mode,args.run_id).run()


if __name__=='__main__':
    main()
