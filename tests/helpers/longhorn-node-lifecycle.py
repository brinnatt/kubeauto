#!/usr/bin/env python3
"""LONGHORN-25: add and safely retire one explicitly released V2 host as V1.

The original six-node owner and Node UIDs never change. A private fixture
namespace accepts exactly its original inventory or the product's one-node
extension. This gate is sequenced after the V2 source retirement, not beside it.
"""
import argparse
import copy
from datetime import datetime
import hashlib
import importlib.util
import inspect
import json
from pathlib import Path
import re
import secrets
import subprocess
import sys
import tempfile
import time
import uuid

import yaml

HOST = '192.168.47.148'
NAME = 'longhorn-node-148'
NS = 'longhorn-system'
RECORD = 'longhorn-node-lifecycle.json'
BUSINESS = 'longhorn-node-lifecycle-business.json'
REFUSAL = 'Longhorn node must be unschedulable with all disks evacuated and removed'
RELEASE_MARKER = 'LONGHORN_V2_SOURCE_HOST_RELEASE_PASS'


def worker_certificates(lab, name):
    files = {'ca.pem':'ca.pem', 'kubelet.pem':name+'-kubelet.pem',
             'kubelet-key.pem':name+'-kubelet-key.pem'}
    result = {}
    for target, source in files.items():
        path = lab.DIRECTORY/'ssl'/source
        if path.is_symlink() or not path.is_file(): raise ValueError('missing original worker certificate source')
        result[target] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def release_worker_certificates(request, root='/'):
    """Restore an initially empty disposable worker after product role cleanup.

    clean_node intentionally preserves the shared ssl directory. This fixture
    may release only the three exact files copied by create-kubelet-kubeconfig.
    """
    import hashlib
    from pathlib import Path
    import subprocess
    root = Path(root)
    if ((root/'etc/machine-id').read_text().strip() != request['machine_id'] or
            (root/'proc/sys/kernel/random/boot_id').read_text().strip() != request['boot_id'] or
            set(request['expected']) != {'ca.pem','kubelet.pem','kubelet-key.pem'}):
        raise ValueError('retired worker certificate identity changed')
    for unit in ('kubelet','kube-proxy','kube-apiserver','kube-controller-manager','kube-scheduler','etcd'):
        result = subprocess.run(['systemctl','is-active',unit],capture_output=True,text=True,timeout=10)
        if result.stdout.strip() not in ('inactive','unknown'):
            raise ValueError('retired worker still has a certificate consumer')
    directory = root/'etc/kubernetes'; ssl = directory/'ssl'
    if directory.is_symlink() or ssl.is_symlink(): raise ValueError('symlink retired worker certificate directory')
    if not directory.exists(): return
    if not directory.is_dir() or set(p.name for p in directory.iterdir()) - {'ssl'}:
        raise ValueError('foreign retired worker configuration remains')
    paths = list(ssl.iterdir()) if ssl.exists() else []
    if any(p.is_symlink() or not p.is_file() or p.name not in request['expected'] or
           hashlib.sha256(p.read_bytes()).hexdigest() != request['expected'][p.name] for p in paths):
        raise ValueError('foreign retired worker certificate remains')
    # Validate the entire closure before removing the first owned file.
    for path in paths: path.unlink()
    if ssl.exists(): ssl.rmdir()
    directory.rmdir()


def retire_worker_certificates(lab, host, name, machine, boot, expected):
    if lab.get('node', name) or host in (lab.DIRECTORY/'hosts').read_text():
        raise ValueError('worker certificates require completed product node/inventory retirement')
    lab.remote(host, ['python3','-c',inspect.getsource(release_worker_certificates)+
        '\nimport json,sys\nrelease_worker_certificates(json.load(sys.stdin))\n'],
        stdin=json.dumps({'machine_id':machine,'boot_id':boot,'expected':expected}))


def load(filename):
    spec = importlib.util.spec_from_file_location('node25_'+filename.replace('-', '_'), Path(__file__).with_name(filename))
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def extended_inventory(original):
    """Match ClusterManager._add_to_hosts_section without accepting arbitrary INI."""
    if original.count('[kube_node]\n') != 1 or HOST in original:
        raise ValueError('node extension requires the exact original six-node inventory')
    lines = original.splitlines()
    start = lines.index('[kube_node]')
    end = next((i for i in range(start+1, len(lines)) if lines[i].startswith('[')), len(lines))
    lines.insert(end, HOST+" k8s_nodename='"+NAME+"'")
    return '\n'.join(lines)+'\n'


def release_proof(path):
    """Validate durable 54 host-release evidence; a matrix PASS alone is insufficient.

    A parent emits this small receipt only after source cluster retirement and
    releases its pool ownership before dispatching 25 under the inherited locks.
    Logs remain local to the runner and are hashed before any host mutation.
    """
    path = Path(path)
    if path.is_symlink() or not path.is_file(): raise ValueError('missing regular V2 release proof')
    row = json.loads(path.read_text())
    if (row.get('status') != 'pass' or row.get('signed_scope') != 'v2-source-host-release' or
            row.get('host') != HOST or row.get('durable') != {'exit': 0, 'finalized': 0} or
            row.get('cleanup') != {'host_clean_verified': True, 'raw_disk_clean_verified': True,
                                   'page_pool_restored': True} or
            RELEASE_MARKER not in row.get('markers', [])):
        raise ValueError('V2 release proof is incomplete or outside the receiving host scope')
    uuid.UUID(row['owner_run_id']); uuid.UUID(row['source_cluster_uid'])
    stamp = datetime.fromisoformat(row['released_at'].replace('Z', '+00:00'))
    if stamp.tzinfo is None: raise ValueError('release timestamp needs timezone')
    raw = row['original_raw_disk']
    if (not re.fullmatch('[0-9a-f]{32}', raw.get('machine_id', '')) or
            not raw.get('path', '').startswith('/dev/disk/by-id/') or not raw.get('identity') or
            raw.get('size_bytes') != 100*1024**3):
        raise ValueError('release proof lacks the original stable raw-disk identity')
    logs = row.get('raw_logs', [])
    if not logs: raise ValueError('release proof requires durable raw logs')
    found = False
    for log in logs:
        source = Path(log['path'])
        if (not source.is_absolute() or source.is_symlink() or not source.is_file() or
                hashlib.sha256(source.read_bytes()).hexdigest() != log['sha256']):
            raise ValueError('V2 release raw log identity changed')
        if RELEASE_MARKER+' host='+HOST in source.read_text().splitlines(): found = True
    if not found: raise ValueError('V2 release marker absent from hashed raw logs')
    return row


def disk_plan(proof, collected, run_id):
    """Recheck the released stable identity against a fresh read-only probe."""
    inv, assessment = collected['inventory'], collected['assessment']
    if collected['host'] != HOST or assessment['blockers'] or len(assessment['candidates']) != 1:
        raise ValueError('receiving host is not clean and exclusively available')
    disk, original = assessment['candidates'][0], proof['original_raw_disk']
    if (inv['machine_id'] != original['machine_id'] or disk['identity'] != original['identity'] or
            disk['size_bytes'] != original['size_bytes'] or original['path'] not in disk['by_id'] or
            str(inv['hugepages_2mi']) != '0'):
        raise ValueError('released host/disk identity or restored page pool differs')
    disk_uuid = str(uuid.uuid5(uuid.UUID(run_id), 'node25:'+HOST))
    return {'node': HOST, 'kernel': '/dev/'+disk['name'], 'device': original['path'],
            'identity': disk['identity'], 'size': disk['size_bytes'], 'uuid': disk_uuid,
            'machine_id': inv['machine_id'], 'path': '/data/kubeauto-longhorn', 'phase': 'planned',
            'fstab_line': 'UUID='+disk_uuid+' /data/kubeauto-longhorn ext4 defaults 0 2 # kubeauto-longhorn-node25:'+run_id}


def journal(lab):
    path = lab.DIRECTORY/RECORD
    if path.is_symlink(): raise ValueError('symlink node lifecycle journal')
    row = json.loads(path.read_text())
    if row['run_id'] != lab.owner()['run_id'] or row['host'] != HOST or row['name'] != NAME:
        raise ValueError('foreign node lifecycle journal')
    return row


def fixture(lab=None):
    """Private adapter; shared longhorn-lab constants and original owner stay intact."""
    lab = lab if lab is not None else load('longhorn-lab.py')
    original_nodes, original_workers = lab.NODES, lab.WORKERS
    original_inventory = lab.inventory()
    extended = extended_inventory(original_inventory)
    lab.NODES = original_nodes+(HOST,)

    def owner():
        if lab.DIRECTORY.is_symlink() or lab.STATE.is_symlink(): raise ValueError('symlink fixture owner')
        state = json.loads(lab.STATE.read_text())
        if (state['cluster'] != lab.CLUSTER or tuple(state['nodes']) != original_nodes or
                state['clean_start_verified'] is not True):
            raise ValueError('node lifecycle cannot adopt another core owner')
        uuid.UUID(state['run_id'])
        actual = (lab.DIRECTORY/'hosts').read_text()
        if actual not in (original_inventory, extended): raise ValueError('foreign node lifecycle inventory')
        lab.WORKERS = original_workers+(HOST,) if actual == extended else original_workers
        return state

    def core():
        state = owner()
        actual = lab.get('nodes')['items']
        expected = {'longhorn-node-'+ip.rsplit('.', 1)[1]: ip for ip in original_nodes}
        record = journal(lab) if (lab.DIRECTORY/RECORD).exists() else None
        if any(n['metadata']['name'] == NAME for n in actual):
            if not record or not record.get('node_uid'): raise ValueError('unconfirmed receiving Node UID')
            expected[NAME] = HOST
        if {n['metadata']['name'] for n in actual} != set(expected): raise ValueError('foreign or missing core Node')
        if lab.get('namespace', 'kube-system')['metadata']['uid'] != state['cluster_uid']:
            raise ValueError('source cluster identity changed')
        for node in actual:
            name = node['metadata']['name']
            expected_uid = record['node_uid'] if name == NAME else state['node_uids'][name]
            if (node['metadata']['uid'] != expected_uid or
                    [a['address'] for a in node['status']['addresses'] if a['type'] == 'InternalIP'] != [expected[name]] or
                    not any(c['type'] == 'Ready' and c['status'] == 'True' for c in node['status']['conditions'])):
                raise ValueError('original/receiving Node identity or readiness changed')
        print('LONGHORN_NODE_CORE_IDENTITY_PASS nodes='+str(len(actual)), flush=True)

    lab.owner, lab.core_identity = owner, core
    # Minimal Ubuntu may lack curl. Reuse the already-qualified V2 Python probe
    # with this private adapter's exact seven-host transport allowlist.
    v2 = load('longhorn-v2-lab.py')
    lab.relay_probe = lambda node: v2.relay_probe(lab, node)
    lab.supervise_core = lambda pid, step, expected_refusal=None: supervise(lab, pid, step, expected_refusal=expected_refusal)
    return lab


def supervise(lab, pid, step, *, expected_refusal=None):
    lab.owner()
    identity = lab.process_identity(pid)
    if not identity or identity[0][-2:] != [str(Path(__file__).resolve()).encode(), b'relay-maintain']:
        raise ValueError('node lifecycle requires its exact Registry supervisor')
    commands = {'add-node': ['add-node', lab.CLUSTER, HOST, NAME],
                'del-node': ['del-node', lab.CLUSTER, HOST],
                'del-master': ['del-master', lab.CLUSTER, lab.owner()['nodes'][0]],
                'longhorn-setup': ['setup', lab.CLUSTER, '09']}
    command = [sys.executable, str(lab.BASE/'kubecli.py'), *commands[step]]
    with tempfile.TemporaryFile(mode='w+') as output:
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, start_new_session=True,
            stdout=output if expected_refusal else None, stderr=subprocess.STDOUT if expected_refusal else None)
        started = heartbeat = time.monotonic()
        with lab.scoped_cleanup(lambda: lab.stop_product(process, command)):
            while process.poll() is None:
                if lab.process_identity(pid) != identity: raise ValueError('node relay exited during product operation')
                now = time.monotonic()
                if now-started >= 7200: raise TimeoutError('node lifecycle product deadline exceeded')
                if now-heartbeat >= 15:
                    print('LONGHORN_NODE_PRODUCT_HEARTBEAT step='+step+' elapsed='+str(int(now-started)), flush=True)
                    heartbeat = now
                time.sleep(1)
            if expected_refusal:
                output.seek(0); observed = output.read()
                if not process.returncode or 'LONGHORN_PRODUCT_FAIL reason='+expected_refusal not in observed:
                    lab.write_private(lab.DIRECTORY/('node25-refusal-'+uuid.uuid4().hex+'.json'),
                                      {'rc': process.returncode, 'output': observed})
                    raise ValueError('owning Longhorn removal guard did not refuse')
                print('LONGHORN_NODE_PRODUCT_REMOVAL_REFUSAL step='+step, flush=True)
            elif process.returncode: raise ValueError('node lifecycle product step failed rc='+str(process.returncode))


def host_script():
    """Underlay marker and fstab are test-owned; the default guard is product-owned."""
    return r'''import json,os,pathlib,subprocess,sys
r=json.load(sys.stdin);p=pathlib.Path(r['path']);f=pathlib.Path('/etc/fstab');m=p/'.kubeauto-node25-owner'
assert str(p)=='/data/kubeauto-longhorn' and not any(x.is_symlink() for x in (p,p.parent,f,m))
owned=r['fstab_line']+'\n';lines=f.read_text().splitlines(True)
assert sum(x==owned for x in lines)<=1 and not any(str(p) in x and not x.lstrip().startswith('#') and x!=owned for x in lines), 'foreign fstab'
def run(*a,check=True):return subprocess.run(a,text=True,capture_output=True,check=check)
def mounted():
 x=run('findmnt','-J','--mountpoint',str(p),'-o','TARGET,UUID,FSTYPE',check=False)
 if x.returncode==1:return False
 assert x.returncode==0 and json.loads(x.stdout)['filesystems']==[{'target':str(p),'uuid':r['uuid'],'fstype':'ext4'}], 'mount identity changed'
 return True
if sys.argv[1]=='mount':
 assert not p.exists() and owned not in lines
 p.mkdir(mode=0o755);m.write_text(r['uuid']+'\n')
 with f.open('a') as s:s.write(owned);s.flush();os.fsync(s.fileno())
 run('mount',str(p));assert mounted()
elif sys.argv[1]=='clean':
 if mounted():run('umount',str(p))
 if p.exists():
  assert p.is_dir() and not m.is_symlink() and m.read_text()==r['uuid']+'\n'
  assert {x.name for x in p.iterdir()}=={m.name}, 'foreign mount underlay'
 if owned in lines:
  t=f.with_name('.fstab.kubeauto-node25.part')
  with t.open('x') as s:s.writelines(x for x in lines if x!=owned);s.flush();os.fsync(s.fileno())
  os.chmod(t,f.stat().st_mode & 0o777);os.chown(t,f.stat().st_uid,f.stat().st_gid);t.replace(f)
 if p.exists():m.unlink();p.rmdir()
 run('systemctl','daemon-reload')
else:raise ValueError('unknown node fixture action')
'''


def filesystem_disk_identity(request, *, root='/'):
    """Read the original filesystem and Longhorn's independently generated ID."""
    import subprocess
    root = Path(root)
    path = root/request['path'].lstrip('/')
    config = path/'longhorn-disk.cfg'
    if (request['path'] != '/data/kubeauto-longhorn' or not request['device'].startswith('/dev/disk/by-id/') or
            any(p.is_symlink() for p in (path, path.parent, config)) or not config.is_file()):
        raise ValueError('partial add disk configuration is not a regular owned file')
    observed = subprocess.run(['findmnt', '-J', '--mountpoint', str(path), '-o', 'TARGET,UUID,FSTYPE'],
        capture_output=True, text=True, check=True, timeout=30)
    if json.loads(observed.stdout).get('filesystems') != [
            {'target': str(path), 'uuid': request['uuid'], 'fstype': 'ext4'}]:
        raise ValueError('partial add filesystem mount identity changed')
    observed = subprocess.run(['blkid', '-s', 'UUID', '-o', 'value', request['device']],
        capture_output=True, text=True, check=True, timeout=30)
    if observed.stdout.strip() != request['uuid']:
        raise ValueError('partial add stable device filesystem identity changed')
    content = config.read_bytes()
    native = json.loads(content).get('diskUUID')
    if not isinstance(native, str) or not native:
        raise ValueError('partial add native disk configuration has no UUID')
    return {'disk_uuid': native, 'config_sha256': hashlib.sha256(content).hexdigest()}


class Lifecycle:
    def __init__(self, lab, pid):
        self.lab, self.pid = lab, pid
        self.path = lab.DIRECTORY/RECORD
        self.workload = load('longhorn-upgrade-lifecycle.py').Workload(lab, 'node25')
        self.workload.path = lab.DIRECTORY/BUSINESS

    def row(self): return journal(self.lab)
    def save(self, row): self.lab.write_private(self.path, row)

    def admit(self, proof_path):
        lab = self.lab
        lab.require_lease(); lab.core_identity(); lab.installation_health()
        if self.path.exists() or self.path.is_symlink() or self.workload.path.exists():
            raise ValueError('node lifecycle is not an adoption/resume entry')
        proof = release_proof(proof_path)
        collected = load('longhorn-v2-pool.py').collect(HOST)
        disk = disk_plan(proof, collected, lab.owner()['run_id'])
        lab.execute(['bash', lab.BASE/'tests/helpers/lab-wipe-nodes.sh', '--verify', '--rocky-only', HOST], timeout=180)
        disk['parent_identity'] = lab.check_blank_disk(HOST, disk)
        disk['parent_created'] = disk['parent_identity'] is None
        expected = {name: hashlib.sha256(data).hexdigest() for name, data in lab.host_files().items()}
        host = json.loads(lab.remote(HOST, ['python3', '-c', lab.host_state_script()],
            stdin=json.dumps({'action': 'capture', 'expected': expected})).stdout)
        self.save({'host': HOST, 'name': NAME, 'run_id': lab.owner()['run_id'],
            'release_proof': proof, 'release_sha256': hashlib.sha256(Path(proof_path).read_bytes()).hexdigest(),
            'config': (lab.DIRECTORY/'config.yml').read_text(), 'inventory': (lab.DIRECTORY/'hosts').read_text(),
            'disk': disk, 'host_before': host, 'host_expected': expected,
            'multipath': {'original': load('longhorn-v2-install.py').multipath(lab, HOST, 'probe'), 'phase': 'planned'},
            'hostname': collected['inventory']['hostname'], 'phase': 'admitted', 'payload': secrets.token_hex(16)})
        print('LONGHORN_NODE_RECEIVING_HOST_ADMITTED host='+HOST, flush=True)

    def config(self, include):
        row = self.row(); config = yaml.safe_load(row['config'])
        owner = self.lab.owner()
        if config['longhorn_storage_nodes'] != list(owner.get('storage_nodes', owner['nodes'][3:])):
            raise ValueError('node lifecycle requires the original three data nodes')
        expanded = copy.deepcopy(config); expanded['longhorn_storage_nodes'].append(HOST)
        actual = yaml.safe_load((self.lab.DIRECTORY/'config.yml').read_text())
        if actual not in (config, expanded): raise ValueError('foreign configuration change during node lifecycle')
        if include: config = expanded
        load('longhorn-upgrade-lifecycle.py').atomic_config(self.lab, config)

    def host_identity(self):
        actual = self.lab.remote(HOST, ['cat', '/etc/machine-id']).stdout.strip()
        if actual != self.row()['disk']['machine_id']: raise ValueError('receiving machine identity changed')

    def partial_add_identity(self, *, committed=False):
        lab = self.lab; row = self.row()
        if (committed and (not row.get('node_uid') or row['phase'] != 'joined') or
                not committed and (row.get('node_uid') or row['phase'] != 'adding')):
            raise ValueError('partial add requires original uncommitted adding intent')
        if row['disk']['phase'] != 'mounted': raise ValueError('partial join lacks mounted disk intent')
        self.host_identity()
        node = lab.get('node', NAME)
        if not node:
            if lab.get('nodes.longhorn.io', NAME, NS):
                raise ValueError('partial join has an unconfirmed native Node')
            raise ValueError('partial add Kubernetes Node is missing')
        if committed and node['metadata']['uid'] != row['node_uid']:
            raise ValueError('committed receiving Node identity changed')
        info = node['status']['nodeInfo']
        if (info['machineID'] != row['disk']['machine_id'] or
                info['bootID'] != row['multipath']['original']['boot_id'] or
                [a['address'] for a in node['status']['addresses'] if a['type'] == 'InternalIP'] != [HOST]):
            raise ValueError('partial join host/boot/address identity differs')
        expected = worker_certificates(lab, NAME)
        probe = lab.remote(HOST, ['python3', '-c',
            'import hashlib,json,pathlib\n'
            'p=pathlib.Path("/etc/kubernetes/ssl")\n'
            'names=("ca.pem","kubelet.pem","kubelet-key.pem")\n'
            'assert not p.is_symlink() and all((p/n).is_file() and not (p/n).is_symlink() for n in names)\n'
            'print(json.dumps({n:hashlib.sha256((p/n).read_bytes()).hexdigest() for n in names}))\n'])
        if json.loads(probe.stdout) != expected:
            raise ValueError('partial join worker certificates differ from issued sources')
        state = lab.owner()
        original = [n for n in lab.get('nodes')['items'] if n['metadata']['name'] != NAME]
        if ({n['metadata']['name']: n['metadata']['uid'] for n in original} != state['node_uids'] or
                lab.get('namespace', 'kube-system')['metadata']['uid'] != state['cluster_uid']):
            raise ValueError('partial join original core identity changed')
        return node, expected, state

    def recover_partial_join(self):
        """Bind a failed add's exact host before its product-owned retirement.

        add-node commits its temporary inventory only after the entire playbook
        succeeds. A failed Longhorn health wait can therefore leave a real
        Kubernetes Node outside the committed inventory. This recovery requires
        the original creation intent, machine/boot identity and issued worker
        certificates; a matching resource name alone never authorizes cleanup.
        """
        lab = self.lab; row = self.row()
        if row.get('node_uid') or row['phase'] != 'adding': return
        if not lab.get('node', NAME):
            if lab.get('nodes.longhorn.io', NAME, NS):
                raise ValueError('partial join has an unconfirmed native Node')
            return
        node, expected, state = self.partial_add_identity()
        # Recovery of this observed failure is deliberately narrower than normal
        # evacuation: no native storage Node or data reference may be adopted.
        if lab.get('nodes.longhorn.io', NAME, NS):
            raise ValueError('partial join native storage requires separate evacuation diagnosis')
        result = lab.execute([sys.executable, lab.BASE/'roles/longhorn/files/longhorn.py', 'guard',
            '--kubeconfig', lab.DIRECTORY/'kubectl.kubeconfig', '--node-ip', HOST], check=False)
        if result.returncode or 'LONGHORN_REMOVAL_GUARD_PASS' not in result.stdout.splitlines():
            raise ValueError('partial join is not empty under the product removal guard')
        actual = (lab.DIRECTORY/'hosts').read_text()
        extended = extended_inventory(row['inventory'])
        if actual not in (row['inventory'], extended): raise ValueError('foreign partial join inventory')
        config = yaml.safe_load(row['config'])
        expanded = copy.deepcopy(config); expanded['longhorn_storage_nodes'].append(HOST)
        if yaml.safe_load((lab.DIRECTORY/'config.yml').read_text()) not in (config, expanded):
            raise ValueError('foreign partial join configuration')
        row.update(worker_certificates=expected, partial_join_recovery={
                'node': node, 'inventory_before': actual, 'inventory_after': extended,
                'reason': 'failed product add-node before temporary inventory commit',
                'guard': 'LONGHORN_REMOVAL_GUARD_PASS'})
        self.save(row)
        # Materialize only the already-created, identity-proven worker so that
        # normal kubecli del-node can own drain, host cleanup and inventory exit.
        if actual != extended:
            temporary = lab.DIRECTORY/'hosts.node25-recovery.part'
            with temporary.open('x') as stream: stream.write(extended)
            temporary.chmod(0o600); temporary.replace(lab.DIRECTORY/'hosts')
        row.update(node_uid=node['metadata']['uid'], phase='partial-joined')
        self.save(row)
        print('LONGHORN_NODE_PARTIAL_JOIN_RECOVERY_BOUND host='+HOST, flush=True)

    def resume_business(self, proof_path):
        """Retry only the original mounted, data-verified product add operation."""
        lab = self.lab; row = self.row()
        if row['phase'] == 'joined' and row.get('node_uid') and row.get('native_uid'):
            return self.resume_retirement(proof_path)
        if (hashlib.sha256(Path(proof_path).read_bytes()).hexdigest() != row['release_sha256'] or
                release_proof(proof_path) != row['release_proof']):
            raise ValueError('partial add original release proof changed')
        node, certificates, _ = self.partial_add_identity()
        import inspect
        script = inspect.getsource(filesystem_disk_identity)+'\nimport json,sys,hashlib\nfrom pathlib import Path\nprint(json.dumps(filesystem_disk_identity(json.load(sys.stdin))))\n'
        disk_identity = json.loads(lab.remote(HOST, ['python3', '-c', script],
            stdin=json.dumps(row['disk'])).stdout)
        native = lab.get('nodes.longhorn.io', NAME, NS)
        disks = native.get('spec', {}).get('disks', {}) if native else {}
        status = (native.get('status', {}).get('diskStatus') or {}) if native else {}
        if (not native or native['metadata'].get('deletionTimestamp') or len(disks) != 1 or
                next(iter(disks.values())).get('path') != row['disk']['path'] or
                next(iter(disks.values())).get('diskType', 'filesystem') != 'filesystem' or
                (status.get(next(iter(disks))) or {}).get('diskUUID') != disk_identity['disk_uuid']):
            raise ValueError('partial add native filesystem identity is not proven')
        original = yaml.safe_load(row['config'])
        expanded = copy.deepcopy(original); expanded['longhorn_storage_nodes'].append(HOST)
        if ((lab.DIRECTORY/'hosts').read_text() != row['inventory'] or
                yaml.safe_load((lab.DIRECTORY/'config.yml').read_text()) != expanded):
            raise ValueError('partial add inventory or expanded configuration changed')
        intent = {'node_uid': node['metadata']['uid'], 'native_uid': native['metadata']['uid'],
                  'disk_identity': disk_identity, 'certificates': certificates}
        if row.get('add_retry_intent', intent) != intent:
            raise ValueError('partial add retry identities changed')
        self.workload.identities(); self.workload.data(row['payload'], read=True)
        replicas = lab.replica_topology(self.workload.journal(), 3)
        if not replicas or any(replica['node'] == NAME for replica in replicas.values()):
            raise ValueError('partial add original three-replica baseline is not healthy')
        row['add_retry_intent'] = intent; self.save(row)
        print('LONGHORN_NODE_PARTIAL_ADD_RETRY_VERIFIED host='+HOST, flush=True)
        supervise(lab, self.pid, 'add-node')
        self.complete_add(replicas, (intent['node_uid'], intent['native_uid']))

    def resume_retirement(self, proof_path):
        """Consume the completed add/refusal prefix, then finish its owned exit."""
        import inspect
        lab = self.lab; row = self.row()
        prefix = json.loads((lab.BASE/'tests/evidence/longhorn-node-retirement-prefix-20261024.json').read_text())
        if (prefix['status'] != 'completed-prefix-only' or prefix['journal_sha256'] !=
                hashlib.sha256(self.path.read_bytes()).hexdigest() or
                prefix['product_sha256'] != hashlib.sha256((lab.BASE/'roles/longhorn/files/longhorn.py').read_bytes()).hexdigest()):
            raise ValueError('completed add prefix journal or product changed')
        for name, expected in prefix['functions'].items():
            function = globals()[name] if name == 'supervise' else getattr(Lifecycle, name)
            if hashlib.sha256(inspect.getsource(function).encode()).hexdigest() != expected:
                raise ValueError('completed add prefix owning function changed')
        if set(prefix['functions']) != {'complete_add', 'refusal', 'supervise'}:
            raise ValueError('completed add prefix function boundary incomplete')
        for entry in prefix['remote_receipts']:
            path = Path(entry['path'])
            if (not path.is_absolute() or '..' in path.parts or path.is_symlink() or not path.is_file() or
                    hashlib.sha256(path.read_bytes()).hexdigest() != entry['sha256']):
                raise ValueError('completed add prefix receipt changed')
        lines = Path(prefix['business_log']).read_text().splitlines()
        markers = ('LONGHORN_NODE_DATA_EXPANSION_VERIFIED replicas=4 host='+HOST,
                   'LONGHORN_NODE_PRODUCT_REMOVAL_REFUSAL step=del-node',
                   'LONGHORN_NODE_PRODUCT_REMOVAL_REFUSAL step=del-master')
        if (not any(r['path'] == prefix['business_log'] and r['sha256'] == prefix['business_log_sha256']
                    for r in prefix['remote_receipts']) or
                any(lines.count(marker) != 1 for marker in markers)):
            raise ValueError('completed add prefix lacks original business assertions')
        if (hashlib.sha256(Path(proof_path).read_bytes()).hexdigest() != row['release_sha256'] or
                release_proof(proof_path) != row['release_proof']):
            raise ValueError('completed add original release proof changed')
        node, certificates, _ = self.partial_add_identity(committed=True)
        native = self.native(); intent = row['add_retry_intent']
        script = inspect.getsource(filesystem_disk_identity)+'\nimport json,sys,hashlib\nfrom pathlib import Path\nprint(json.dumps(filesystem_disk_identity(json.load(sys.stdin))))\n'
        disk_identity = json.loads(lab.remote(HOST, ['python3', '-c', script], stdin=json.dumps(row['disk'])).stdout)
        if (intent['node_uid'] != node['metadata']['uid'] or intent['native_uid'] != native['metadata']['uid'] or
                intent['disk_identity'] != disk_identity or certificates != prefix['certificates'] or
                certificates['ca.pem'] != intent['certificates']['ca.pem']):
            raise ValueError('completed add original disk or certificates changed')
        original = yaml.safe_load(row['config']); expanded = copy.deepcopy(original)
        expanded['longhorn_storage_nodes'].append(HOST)
        if ((lab.DIRECTORY/'hosts').read_text() != extended_inventory(row['inventory']) or
                yaml.safe_load((lab.DIRECTORY/'config.yml').read_text()) not in (original, expanded)):
            raise ValueError('completed add product inventory or configuration changed')
        self.workload.identities(); self.workload.data(row['payload'], read=True)
        replicas = lab.replica_topology(self.workload.journal(), 3)
        if not replicas or any(r['node'] == NAME for r in replicas.values()):
            raise ValueError('completed add evacuated three-replica baseline is not healthy')
        print('LONGHORN_NODE_COMPLETED_ADD_PREFIX_VERIFIED log_sha256='+prefix['business_log_sha256'], flush=True)
        self.retire()
        self.workload.detach(); self.workload.mount('reader', 1); self.workload.data(row['payload'], read=True)
        self.clean()
        print('LONGHORN_NODE_LIFECYCLE_PASS', flush=True)

    def native(self):
        row = self.row(); obj = self.lab.get('nodes.longhorn.io', NAME, NS)
        if not obj or not row.get('native_uid') or obj['metadata']['uid'] != row['native_uid']:
            raise ValueError('receiving Longhorn Node identity changed or unconfirmed')
        return obj

    def disable_default_disk(self):
        """Perform the documented native label change before emptying the node."""
        node = self.lab.get('node', NAME); row = self.row()
        if not node or node['metadata']['uid'] != row['node_uid']:
            raise ValueError('default-disk retirement Kubernetes Node changed')
        meta = node['metadata']; label = 'node.longhorn.io/create-default-disk'
        annotation = 'node.longhorn.io/default-disks-config'
        expected = [{'name': name, 'path': disk['path'], 'allowScheduling': True,
                     'storageReserved': disk.get('storageReserved', 0)} for name, disk in row['native_disks'].items()]
        if (meta.get('labels', {}).get('kubeauto.io/component') != 'longhorn' or
                meta['labels'].get(label) not in ('config', 'false') or
                json.loads(meta.get('annotations', {}).get(annotation, 'null')) != expected):
            raise ValueError('default-disk retirement bootstrap metadata changed')
        if meta['labels'][label] == 'config':
            self.lab.kubectl('patch', 'node', NAME, '--type=json', '--patch-file=/dev/stdin', stdin=json.dumps([
                {'op': 'test', 'path': '/metadata/uid', 'value': meta['uid']},
                {'op': 'test', 'path': '/metadata/resourceVersion', 'value': meta['resourceVersion']},
                {'op': 'test', 'path': '/metadata/labels/node.longhorn.io~1create-default-disk', 'value': 'config'},
                {'op': 'replace', 'path': '/metadata/labels/node.longhorn.io~1create-default-disk', 'value': 'false'}]))
        current = self.lab.get('node', NAME)
        if not current or current['metadata']['uid'] != meta['uid'] or current['metadata']['labels'].get(label) != 'false':
            raise ValueError('default-disk retirement bootstrap label did not converge')
        print('LONGHORN_NODE_DEFAULT_DISK_CREATION_DISABLED host='+HOST, flush=True)

    def native_patch(self, **changes):
        obj = self.native(); spec = copy.deepcopy(obj['spec']); spec.update(changes)
        load('longhorn-disk-lifecycle.py').patch(self.lab, obj, spec)

    def empty(self):
        """Use the product's full native guard as the empty-node predicate."""
        self.native()
        result = self.lab.execute([sys.executable, self.lab.BASE/'roles/longhorn/files/longhorn.py', 'guard',
            '--kubeconfig', self.lab.DIRECTORY/'kubectl.kubeconfig', '--node-ip', HOST], check=False)
        return result.returncode == 0 and 'LONGHORN_REMOVAL_GUARD_PASS' in result.stdout.splitlines()

    def refusal(self, step):
        lab = self.lab; target = NAME if step == 'del-node' else 'longhorn-node-'+lab.owner()['nodes'][0].rsplit('.', 1)[1]
        obj = lab.get('nodes.longhorn.io', target, NS)
        # Prove that the first, non-mutating guard must refuse before invoking
        # a destructive CLI. Never rely on an assumed control-node default.
        if not obj or not (obj['spec'].get('allowScheduling', True) or obj['spec'].get('disks')):
            raise ValueError('removal refusal prerequisite not proven; do not invoke destructive CLI')
        before = {'hosts': (lab.DIRECTORY/'hosts').read_text(), 'binding': self.workload.identities(),
                  'native_uid': obj['metadata']['uid'], 'spec': obj['spec']}
        lab.core_identity(); self.workload.data(self.row()['payload'], read=True)
        supervise(lab, self.pid, step, expected_refusal=REFUSAL)
        after = lab.get('nodes.longhorn.io', target, NS)
        if (not after or after['metadata']['uid'] != before['native_uid'] or after['spec'] != before['spec'] or
                (lab.DIRECTORY/'hosts').read_text() != before['hosts'] or self.workload.identities() != before['binding']):
            raise ValueError('refused removal changed protected identities')
        lab.core_identity(); self.workload.data(self.row()['payload'], read=True)

    def business(self):
        lab = self.lab; row = self.row(); disk = row['disk']
        if row['phase'] != 'admitted': raise ValueError('business entry requires fresh admission')
        self.host_identity()
        # add-node embeds Longhorn host preparation; qualify and stop only the
        # admitted host's unused multipath service/socket before disk mutation.
        entry = row['multipath']
        if entry['phase'] != 'planned': raise ValueError('node lifecycle requires fresh multipath admission')
        entry['phase'] = 'stop-intent'; self.save(row)
        entry['current'] = load('longhorn-v2-install.py').multipath(lab, HOST, 'stop', expected=entry['original'])
        entry['phase'] = 'stopped'; self.save(row)
        self.workload.prepare(); self.workload.data(row['payload'])
        original = lab.replica_topology(self.workload.journal(), 3)
        if not original: raise ValueError('original three-replica placement not proven')
        # Record intent before each destructive step; unknown/lost responses
        # remain journaled and are never inferred from a convenient name.
        if lab.check_blank_disk(HOST, disk) != disk['parent_identity']:
            raise ValueError('receiving disk parent changed after admission')
        if disk['parent_created']:
            disk['parent_identity'] = json.loads(lab.remote(HOST, ['python3', '-c', lab.disk_parent_script(), 'create'],
                stdin=json.dumps(disk)).stdout); self.save(row)
        disk['phase'] = 'formatting'; self.save(row)
        lab.remote(HOST, ['mkfs.ext4', '-F', '-U', disk['uuid'], '-L', 'kca-node25', disk['device']], timeout=180)
        lab.remote(HOST, ['python3', '-c', host_script(), 'mount'], stdin=json.dumps(disk))
        disk['phase'] = 'mounted'; row['phase'] = 'adding'; self.save(row)
        self.config(True); supervise(lab, self.pid, 'add-node')
        self.complete_add(original)

    def complete_add(self, original, expected=None):
        lab = self.lab; row = self.row()
        node = lab.get('node', NAME); native = lab.get('nodes.longhorn.io', NAME, NS)
        if not node or not native: raise ValueError('product add-node did not create both Node identities')
        if expected and (node['metadata']['uid'], native['metadata']['uid']) != expected:
            raise ValueError('partial add identities changed during product retry')
        row = self.row(); row.update(node_uid=node['metadata']['uid'], native_uid=native['metadata']['uid'], phase='joined')
        declarations = native['spec'].get('disks', {})
        if len(declarations) != 1 or next(iter(declarations.values()))['path'] != lab.DATA:
            raise ValueError('receiving native Node has unexpected disk declarations')
        row['native_disks'] = declarations
        self.save(row); lab.core_identity(); lab.installation_health()
        replicas = load('longhorn-maintenance.py').scale(lab, self.workload.journal(), 4)
        if (not all(replicas.get(name) == value for name, value in original.items()) or
                len([r for r in replicas.values() if r['node'] == NAME]) != 1):
            raise ValueError('receiving node did not expand the existing replica set')
        self.workload.data(row['payload'], read=True)
        print('LONGHORN_NODE_DATA_EXPANSION_VERIFIED replicas=4 host='+HOST, flush=True)
        self.refusal('del-node'); self.refusal('del-master')
        self.retire()
        self.workload.detach(); self.workload.mount('reader', 1); self.workload.data(row['payload'], read=True)
        self.clean()
        print('LONGHORN_NODE_LIFECYCLE_PASS', flush=True)

    def retire(self):
        lab = self.lab; row = self.row()
        self.host_identity()
        if not row.get('node_uid'):
            if lab.get('node', NAME) or lab.get('nodes.longhorn.io', NAME, NS):
                raise ValueError('unconfirmed receiving node creation; retain journal for diagnosis')
            return
        node = lab.get('node', NAME)
        if not node:
            if (lab.DIRECTORY/'hosts').read_text() != row['inventory']:
                raise ValueError('receiving Node missing before product retirement completed')
            if row['phase'] != 'retired' and row['phase'] != 'clean': self.finish_retirement()
            return
        if node['metadata']['uid'] != row['node_uid']: raise ValueError('replacement receiving Node refuses retirement')
        if row['phase'] == 'partial-joined':
            if lab.get('nodes.longhorn.io', NAME, NS):
                raise ValueError('partial join acquired native storage before retirement')
            self.config(False)
            supervise(lab, self.pid, 'del-node')
            lab.wait('node25-partial-kubernetes-retired', 180, lambda: not lab.get('node', NAME))
            self.finish_retirement()
            return
        self.native_patch(allowScheduling=False, evictionRequested=True)
        if self.workload.path.exists():
            business = self.workload.journal()
            if business.get('volume'):
                load('longhorn-maintenance.py').scale(lab, business, 3)
                lab.wait('node25-replica-evacuated', 900, lambda:
                    all(r['node'] != NAME for r in (lab.replica_topology(business, 3) or {'pending': {'node': NAME}}).values()))
                self.workload.data(row['payload'], read=True)
        # The product guard also checks disks, so remove declarations only
        # after every actual replica/backing-image reference has disappeared.
        native = self.native()
        disabled = copy.deepcopy(row['native_disks'])
        for disk in disabled.values(): disk['allowScheduling'] = False
        if native['spec'].get('disks') not in (row['native_disks'], disabled, {}):
            raise ValueError('foreign receiving disk declaration prevents node retirement')
        disk_ids = {s.get('diskUUID') for s in native.get('status', {}).get('diskStatus', {}).values()}
        disk_ids.discard(None)
        for kind in ('replicas', 'backingimagemanagers', 'backingimagedatasources'):
            for obj in lab.get(kind+'.longhorn.io', namespace=NS)['items']:
                if (obj['spec'].get('nodeID') == NAME or obj['spec'].get('diskUUID') in disk_ids or
                        obj['spec'].get('diskID') in disk_ids):
                    raise ValueError('receiving data reference remains; stop before disk removal')
        for image in lab.get('backingimages.longhorn.io', namespace=NS)['items']:
            if disk_ids & (set(image['spec'].get('diskFileSpecMap', {})) | set(image.get('status', {}).get('diskFileStatusMap', {}))):
                raise ValueError('receiving backing-image disk reference remains')
        if native['spec'].get('disks'):
            self.native_patch(disks=disabled)
            def disk_empty():
                current = self.native()
                status = current.get('status', {}).get('diskStatus') or {}
                return (current['spec'].get('disks') == disabled and all(
                    name in status and status[name] is not None and status[name].get('storageScheduled') == 0
                    for name in disabled))
            lab.wait('node25-disk-disabled-empty', 300, disk_empty)
        self.disable_default_disk()
        self.native_patch(disks={}, evictionRequested=False)
        self.config(False); supervise(lab, self.pid, 'longhorn-setup')
        lab.wait('node25-empty-native-removal-guard', 300, self.empty)
        row = self.row(); row['phase'] = 'deleting'; self.save(row)
        supervise(lab, self.pid, 'del-node')
        lab.wait('node25-kubernetes-retired', 180, lambda: not lab.get('node', NAME))
        self.finish_retirement()

    def finish_retirement(self):
        lab = self.lab; row = self.row()
        if lab.get('node', NAME): raise ValueError('receiving Kubernetes Node still exists')
        native = lab.get('nodes.longhorn.io', NAME, NS)
        if native:
            self.native()
            if native['spec'].get('allowScheduling', True) or native['spec'].get('disks'):
                raise ValueError('receiving native node is not safely empty after product retirement')
            def gone():
                current = lab.get('nodes.longhorn.io', NAME, NS)
                if not current: return True  # KubernetesNodeController may delete it first.
                self.native()
                return any(c.get('type') == 'Ready' and c.get('status') == 'False' and c.get('reason') == 'KubernetesNodeGone'
                           for c in current.get('status', {}).get('conditions', []))
            lab.wait('node25-native-kubernetes-gone', 180, gone)
            if lab.get('nodes.longhorn.io', NAME, NS): lab.remove(self.native())
            lab.wait('node25-native-retired', 180, lambda: not lab.get('nodes.longhorn.io', NAME, NS))
        if (lab.DIRECTORY/'hosts').read_text() != row['inventory']: raise ValueError('product inventory did not return to original bytes')
        row['phase'] = 'retired'; self.save(row)
        self.config(False)
        (lab.DIRECTORY/'config.yml').write_text(row['config'])
        supervise(lab, self.pid, 'longhorn-setup'); lab.core_identity(); lab.installation_health()
        print('LONGHORN_NODE_NATIVE_EVACUATION_RETIRED host='+HOST, flush=True)

    def clean(self):
        lab = self.lab; row = self.row(); self.retire(); row = self.row()
        self.host_identity()
        self.workload.cleanup()
        disk = row['disk']
        # Reuse hardware/UUID checks, never the shared original-worker cleanup.
        probe = json.loads(lab.remote(HOST, ['lsblk', '-J', '-b', '-O', disk['device']]).stdout)['blockdevices']
        if len(probe) != 1: raise ValueError('ambiguous retired receiving disk')
        lab.verify_device(disk, probe[0])
        if probe[0].get('uuid') not in (None, '', disk['uuid']): raise ValueError('receiving filesystem UUID changed')
        if [m for m in probe[0].get('mountpoints', []) or [] if m] not in ([], [lab.DATA]):
            raise ValueError('receiving disk has foreign consumers')
        lab.remote(HOST, ['python3', '-c', lab.disk_parent_script(), 'check'], stdin=json.dumps(disk))
        lab.remote(HOST, ['python3', '-c', lab.disk_cleanup_script(), 'check'], stdin=json.dumps(disk))
        lab.remote(HOST, ['python3', '-c', lab.disk_cleanup_script(), 'clean'], stdin=json.dumps(disk))
        lab.remote(HOST, ['python3', '-c', host_script(), 'clean'], stdin=json.dumps(disk))
        current = lab.remote(HOST, ['blkid', '-p', '-s', 'UUID', '-o', 'value', disk['device']], check=False)
        if current.returncode == 0:
            if current.stdout.strip() != disk['uuid']: raise ValueError('refuse wiping foreign filesystem')
            lab.remote(HOST, ['wipefs', '--all', disk['device']])
        elif current.returncode != 2: raise ValueError('cannot verify retired raw disk')
        lab.remote(HOST, ['python3', '-c', lab.disk_parent_script(), 'clean'], stdin=json.dumps(disk))
        lab.remote(HOST, ['python3', '-c', lab.host_state_script()], stdin=json.dumps(
            {'action': 'restore', 'expected': row['host_expected'], 'before': row['host_before']}))
        entry = row['multipath']
        if entry['phase'] not in ('planned', 'stopped', 'restored'):
            raise ValueError('ambiguous receiving multipath write')
        entry['current'] = load('longhorn-v2-install.py').multipath(lab, HOST, 'restore',
            expected=entry.get('current', entry['original']), original=entry['original'])
        entry['phase'] = 'restored'; self.save(row)
        # prepare owns this hostname change; restore only the recorded host
        # after del-node has removed its consumers and original disk cleanup.
        lab.remote(HOST, ['python3', '-c', 'import pathlib,socket,subprocess,sys\n'
            'assert pathlib.Path("/etc/machine-id").read_text().strip()==sys.argv[1]\n'
            'assert socket.gethostname() in (sys.argv[2],sys.argv[3])\n'
            'subprocess.run(["hostnamectl","set-hostname",sys.argv[3]],check=True,timeout=30)\n',
            row['disk']['machine_id'], NAME, row['hostname']])
        if row.get('node_uid'):
            certificates = worker_certificates(lab, NAME)
            if row.setdefault('worker_certificates', certificates) != certificates:
                raise ValueError('original worker certificate source changed')
            self.save(row)
            retire_worker_certificates(lab, HOST, NAME, row['disk']['machine_id'],
                                       row['multipath']['original']['boot_id'], certificates)
        # Fixed wipe verifier checks removal of Kubernetes/runtime consumers;
        # no wipe is performed by this command and no other host is targeted.
        lab.execute(['bash', lab.BASE/'tests/helpers/lab-wipe-nodes.sh', '--verify', '--rocky-only', HOST], timeout=180)
        disk_plan(row['release_proof'], load('longhorn-v2-pool.py').collect(HOST), row['run_id'])
        (lab.DIRECTORY/'config.yml').write_text(row['config'])
        lab.core_identity(); lab.installation_health()
        row['phase'] = 'clean'; self.save(row)
        print('LONGHORN_NODE_LIFECYCLE_SCOPED_CLEAN_PASS', flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('admit', 'business', 'resume-business', 'clean', 'recover-partial-join', 'verify-core', 'relay-ensure', 'relay-maintain', 'relay-clean', 'finalize'))
    parser.add_argument('--relay-pid', type=int)
    parser.add_argument('--released-v2-proof')
    args = parser.parse_args(); lab = fixture(); lab.require_lease(); lab.owner()
    if args.action == 'verify-core': lab.core_identity(); return
    if args.action.startswith('relay-'):
        if not (lab.DIRECTORY/RECORD).exists(): raise ValueError('receiving transport requires admitted journal')
        journal(lab)
        {'relay-ensure': lab.relay_ensure, 'relay-maintain': lab.relay_maintain, 'relay-clean': lab.relay_cleanup}[args.action]()
        return
    scenario = Lifecycle(lab, args.relay_pid)
    if args.action == 'recover-partial-join': scenario.recover_partial_join()
    elif args.action == 'admit':
        if not args.released_v2_proof: parser.error('admit requires --released-v2-proof')
        scenario.admit(args.released_v2_proof)
    elif args.action == 'finalize':
        row = journal(lab)
        if row['phase'] != 'clean' or lab.owner().get('relays'): raise ValueError('node lifecycle cleanup/transport incomplete')
        lab.core_identity(); (lab.DIRECTORY/RECORD).unlink()
        print('LONGHORN_NODE_LIFECYCLE_FINALIZED')
    else:
        if not args.relay_pid: parser.error('business/clean require --relay-pid')
        if args.action == 'resume-business':
            if not args.released_v2_proof: parser.error('resume requires the original release proof')
            scenario.resume_business(args.released_v2_proof)
        else:
            {'business': scenario.business, 'clean': scenario.clean}[args.action]()


if __name__ == '__main__':
    try: main()
    except (ValueError, KeyError, TypeError, OSError, TimeoutError, subprocess.SubprocessError) as error:
        print('LONGHORN_NODE_LIFECYCLE_FAIL reason='+str(error), file=sys.stderr, flush=True)
        sys.exit(1)
