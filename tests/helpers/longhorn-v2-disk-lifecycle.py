#!/usr/bin/env python3
"""LONGHORN-45/51: extend V2 with the released DR host, evacuate and retire AIO.

Original core, disk and volume UIDs remain authoritative. The private adapter
admits only .149 after target54's hashed release, and never changes the shared
three-node fixture. All declarations, eviction and retirement use config+kubecli.
The pinned SPDK DiskCreate imports an existing matching lvstore; DiskDelete
only removes its AIO bdev. Re-admission must retain the original disk UUID.
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
import signal
import subprocess
import sys
import tempfile
import time
import uuid

import yaml

HOST = '192.168.47.149'
NAME = 'longhorn-node-149'
SOURCE = ('192.168.47.146', '192.168.47.147', '192.168.47.148')
RECORD = 'longhorn-v2-disk-lifecycle.json'
BUSINESS = 'longhorn-v2-disk-lifecycle-business.json'
NS = 'longhorn-system'
DISK = 'kubeauto-block'
RELEASE_MARKER = 'LONGHORN_V2_TARGET_HOST_RELEASE_PASS'
REMOVAL_REFUSAL = 'Longhorn node must be unschedulable with all disks evacuated and removed'
BUSY_REFUSAL = 'retirement requires the approved native disk already evacuated with scheduling disabled'

# Read-only attribution of pre-fix bridge sandboxes. No host files are removed.
JOIN_NETWORK_PROBE = r'''
import hashlib,ipaddress,json,pathlib,subprocess
root=pathlib.Path('/etc/cni/net.d')
configs=sorted(p for p in root.iterdir() if p.suffix in ('.conf','.conflist','.json'))
assert [p.name for p in configs]==['10-calico.conflist'], 'real Calico must be the only CNI'
data=configs[0].read_bytes(); conf=json.loads(data)
assert conf['plugins'][0]['type']=='calico' and conf['plugins'][0]['ipam']['type']=='calico-ipam'
sandboxes=json.loads(subprocess.check_output(['crictl','pods','-o','json'],timeout=30))['items']
allocations=[]
for p in pathlib.Path('/var/lib/cni/networks/mynet').glob('*'):
    try: ipaddress.ip_address(p.name)
    except ValueError: continue
    assert not p.is_symlink()
    fields=p.read_text().split(); assert len(fields)==2 and fields[1]=='eth0'
    for sandbox in sandboxes:
        if sandbox['id']==fields[0] and sandbox['state']=='SANDBOX_READY':
            allocations.append(dict(sandbox['metadata'],ip=p.name,sandbox=sandbox['id']))
bridge=pathlib.Path('/sys/class/net/mynet0')
identity={key:(bridge/key).read_text().strip() for key in ('address','ifindex')} if bridge.exists() else None
assert not allocations or (identity and (bridge/'bridge').is_dir()), 'sandbox bridge identity missing'
print(json.dumps({'machine_id':pathlib.Path('/etc/machine-id').read_text().strip(),
 'boot_id':pathlib.Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
 'cni_sha256':hashlib.sha256(data).hexdigest(),'allocations':allocations,'bridge':identity}))
'''


def release_join_bridge(expected, root='/'):
    """After product del-node, remove only the original fixture's empty bridge."""
    import pathlib
    import subprocess
    root = pathlib.Path(root)
    if ((root/'etc/machine-id').read_text().strip() != expected['machine_id'] or
            (root/'proc/sys/kernel/random/boot_id').read_text().strip() != expected['boot_id']):
        raise ValueError('receiving bridge host identity changed')
    bridge = root/'sys/class/net/mynet0'
    if not bridge.exists(): return
    if (not (bridge/'bridge').is_dir() or
            {key:(bridge/key).read_text().strip() for key in ('address','ifindex')} != expected['bridge'] or
            list((bridge/'brif').iterdir())):
        raise ValueError('receiving bridge changed or still has consumers')
    subprocess.run(['ip','link','delete','dev','mynet0','type','bridge'], check=True, timeout=30)


def load(name):
    spec = importlib.util.spec_from_file_location('disk45_'+name.replace('-', '_'), Path(__file__).with_name(name))
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def extended_inventory(original):
    if original.count('[kube_node]\n') != 1 or HOST in original:
        raise ValueError('V2 extension needs the exact original inventory')
    lines = original.splitlines(); start = lines.index('[kube_node]')
    end = next((i for i in range(start+1, len(lines)) if lines[i].startswith('[')), len(lines))
    lines.insert(end, HOST+" k8s_nodename='"+NAME+"'")
    return '\n'.join(lines)+'\n'


def release_proof(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file(): raise ValueError('missing regular target release proof')
    row = json.loads(path.read_text())
    if (row.get('status') != 'pass' or row.get('signed_scope') != 'v2-target-host-release' or
            row.get('host') != HOST or row.get('durable') != {'exit': 0, 'finalized': 0} or
            row.get('cleanup') != {'host_clean_verified': True, 'raw_disk_clean_verified': True,
                                   'page_pool_restored': True} or RELEASE_MARKER not in row.get('markers', [])):
        raise ValueError('target release is incomplete or outside the receiving scope')
    uuid.UUID(row['owner_run_id']); uuid.UUID(row['target_cluster_uid'])
    if datetime.fromisoformat(row['released_at'].replace('Z', '+00:00')).tzinfo is None:
        raise ValueError('target release timestamp requires timezone')
    raw = row['original_raw_disk']
    if (not re.fullmatch('[0-9a-f]{32}', raw.get('machine_id', '')) or not raw.get('identity') or
            not raw.get('path', '').startswith('/dev/disk/by-id/') or raw.get('size_bytes') != 100*1024**3):
        raise ValueError('target release lacks stable raw identity')
    found = False
    for item in row.get('raw_logs', []):
        log = Path(item['path'])
        if (not log.is_absolute() or log.is_symlink() or not log.is_file() or
                hashlib.sha256(log.read_bytes()).hexdigest() != item['sha256']):
            raise ValueError('target release raw log identity changed')
        lines = log.read_text().splitlines()
        if any(line.startswith(('LONGHORN_FIRST_FAILURE', 'LONGHORN_CLEANUP_FAIL')) for line in lines):
            raise ValueError('failed raw log cannot release target host')
        found |= RELEASE_MARKER+' host='+HOST in lines
    if not found: raise ValueError('target release marker absent from hashed logs')
    return row


def disk_identity(proof, collected):
    raw = proof['original_raw_disk']; inv = collected['inventory']; assessment = collected['assessment']
    if collected['host'] != HOST or assessment['blockers'] or len(assessment['candidates']) != 1:
        raise ValueError('receiving host is not exclusively clean')
    disk = assessment['candidates'][0]
    if (inv['machine_id'] != raw['machine_id'] or disk['identity'] != raw['identity'] or
            disk['size_bytes'] != raw['size_bytes'] or raw['path'] not in disk['by_id'] or
            str(inv['hugepages_2mi']) != '0'):
        raise ValueError('receiving hardware identity or page pool differs from target release')
    return copy.deepcopy(raw)


def accepted_source():
    """Consume the one strict, durable post-52 transition before extension."""
    _, source = load('longhorn-v2-install.py').fixture()
    return load('longhorn-v2-upgrade-baseline.py').accepted(source)


def journal(lab):
    path = lab.DIRECTORY/RECORD
    if path.is_symlink(): raise ValueError('symlink V2 disk lifecycle journal')
    row = json.loads(path.read_text())
    state = json.loads(lab.STATE.read_text())
    if (row['run_id'] != state['run_id'] or row['cluster_uid'] != state['cluster_uid'] or
            row['host'] != HOST or tuple(state['nodes']) != SOURCE):
        raise ValueError('foreign V2 disk lifecycle journal')
    return row


def fixture():
    install = load('longhorn-v2-install.py'); _, lab = install.fixture()
    original = lab.inventory(); expanded = extended_inventory(original)
    lab.NODES = SOURCE+(HOST,)
    # relay_ensure's owning V2 profile is reached through its closure. Use the
    # existing generic transport primitives with this adapter's four hosts.
    profile = load('longhorn-v2-lab.py'); profile.NODES = lab.NODES; profile.DIRECTORY = lab.DIRECTORY
    lab.relay_ensure = lambda force=False: profile.relay_ensure(lab, force=force)
    lab.relay_probe = lambda node: profile.relay_probe(lab, node)

    def owner():
        if lab.STATE.is_symlink() or lab.DIRECTORY.is_symlink(): raise ValueError('symlink source owner')
        state = json.loads(lab.STATE.read_text()); uuid.UUID(state['run_id'])
        actual = (lab.DIRECTORY/'hosts').read_text()
        if (state['cluster'] != lab.CLUSTER or tuple(state['nodes']) != SOURCE or
                state['clean_start_verified'] is not True or actual not in (original, expanded)):
            raise ValueError('V2 extension cannot adopt another source owner/inventory')
        lab.WORKERS = SOURCE+(HOST,) if actual == expanded else SOURCE
        return state

    def core():
        state = owner(); nodes = lab.get('nodes')['items']; expected = dict(state['node_uids'])
        row = journal(lab) if (lab.DIRECTORY/RECORD).exists() else None
        if any(n['metadata']['name'] == NAME for n in nodes):
            if not row or not row.get('node_uid'): raise ValueError('unconfirmed receiving Kubernetes Node UID')
            expected[NAME] = row['node_uid']
        if (lab.get('namespace', 'kube-system')['metadata']['uid'] != state['cluster_uid'] or
                {n['metadata']['name'] for n in nodes} != set(expected)):
            raise ValueError('original V2 core or receiving node set changed')
        for obj in nodes:
            name = obj['metadata']['name']; address = '192.168.47.'+name.rsplit('-', 1)[1]
            if (obj['metadata']['uid'] != expected[name] or
                    [a['address'] for a in obj['status']['addresses'] if a['type'] == 'InternalIP'] != [address] or
                    not any(c['type'] == 'Ready' and c['status'] == 'True' for c in obj['status']['conditions'])):
                raise ValueError('V2 lifecycle node identity or readiness changed')
        print('LONGHORN_V2_DISK_CORE_IDENTITY_PASS nodes='+str(len(nodes)), flush=True)

    lab.owner, lab.core_identity = owner, core
    lab.supervise_core = lambda pid, step, expected_refusal=None: supervise(lab, pid, step, expected_refusal)
    return lab


def supervise(lab, pid, step, refusal=None):
    lab.owner(); identity = lab.process_identity(pid)
    if not identity or identity[0][-2:] != [str(Path(__file__).resolve()).encode(), b'relay-maintain']:
        raise ValueError('V2 lifecycle requires its exact live Registry supervisor')
    argv = [sys.executable, str(lab.BASE/'kubecli.py'), *{
        'longhorn-setup': ['setup', lab.CLUSTER, '09'], 'add-node': ['add-node', lab.CLUSTER, HOST, NAME],
        'del-node': ['del-node', lab.CLUSTER, HOST]}[step]]
    with tempfile.TemporaryFile(mode='w+') as output:
        process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, start_new_session=True,
            stdout=output if refusal else None, stderr=subprocess.STDOUT if refusal else None)
        started = heartbeat = time.monotonic()
        with lab.scoped_cleanup(lambda: lab.stop_product(process, argv)):
            while process.poll() is None:
                if lab.process_identity(pid) != identity: raise ValueError('V2 lifecycle Registry supervisor exited')
                now = time.monotonic()
                if now-started >= 7200: raise TimeoutError('V2 lifecycle product deadline exceeded')
                if now-heartbeat >= 15:
                    print('LONGHORN_V2_DISK_PRODUCT_HEARTBEAT step='+step+' elapsed='+str(int(now-started)), flush=True)
                    heartbeat = now
                time.sleep(1)
            if refusal:
                output.seek(0); text = output.read()
                if not process.returncode or 'LONGHORN_PRODUCT_FAIL reason='+refusal not in text:
                    lab.write_private(lab.DIRECTORY/('disk45-unexpected-refusal-'+uuid.uuid4().hex+'.json'),
                                      {'rc': process.returncode, 'output': text})
                    raise ValueError('V2 lifecycle did not observe the owning product refusal')
                print('LONGHORN_V2_DISK_EXPECTED_REFUSAL step='+step, flush=True)
            elif process.returncode: raise ValueError('V2 lifecycle product failed rc='+str(process.returncode))


def compact_receiving_pages(expected, root='/'):
    """Compact once on the disabled receiving client; never resize or persist VM settings."""
    import pathlib
    import re
    import subprocess
    root = pathlib.Path(root)
    pages = root/'sys/kernel/mm/hugepages/hugepages-2048kB'
    actual = {key:(root/path).read_text().strip() for key,path in (
        ('machine_id', 'etc/machine-id'), ('boot_id', 'proc/sys/kernel/random/boot_id'))}
    actual.update({key:int((pages/path).read_text()) for key,path in (
        ('total', 'nr_hugepages'), ('free', 'free_hugepages'))})
    info = dict((key,int(value)) for key,value in re.findall(
        r'^(MemAvailable|HugePages_Rsvd):\s+(\d+)', (root/'proc/meminfo').read_text(), re.M))
    actual['reserved'] = info['HugePages_Rsvd']
    actual['kubelet_active'] = subprocess.check_output(
        ['systemctl', 'is-active', 'kubelet'], text=True, timeout=15).strip()
    if any(actual[key] != expected[key] for key in actual):
        raise ValueError('receiving compaction input changed')
    if (not 0 <= actual['total'] < 1024 or actual['free'] != actual['total'] or
            actual['reserved'] or actual['kubelet_active'] != 'active' or
            info['MemAvailable'] < (1024-actual['total'])*2048+1024**2):
        raise ValueError('receiving compaction requires an unused pool and system headroom')
    (root/'proc/sys/vm/compact_memory').write_text('1\n')


class Lifecycle:
    def __init__(self, lab, pid):
        self.lab, self.pid = lab, pid
        self.path = lab.DIRECTORY/RECORD
        self.workload = load('longhorn-upgrade-lifecycle.py').Workload(lab, 'v2-disk45')
        self.workload.path = lab.DIRECTORY/BUSINESS

    def row(self): return journal(self.lab)
    def save(self, row): self.lab.write_private(self.path, row)

    def config(self, config, *, refusal=None):
        row = self.row(); path = self.lab.DIRECTORY/'config.yml'
        if path.is_symlink() or path.read_text() not in row['allowed_inputs']:
            raise ValueError('foreign V2 lifecycle product input')
        desired = yaml.safe_dump(config, sort_keys=False)
        if desired not in row['allowed_inputs']: row['allowed_inputs'].append(desired)
        self.save(row)
        load('longhorn-upgrade-lifecycle.py').atomic_config(self.lab, config)
        supervise(self.lab, self.pid, 'longhorn-setup', refusal)

    def current(self):
        row = self.row(); path = self.lab.DIRECTORY/'config.yml'
        if path.is_symlink() or path.read_text() not in row['allowed_inputs']:
            raise ValueError('foreign V2 lifecycle configuration')
        return yaml.safe_load(path.read_text())

    def native(self, address):
        row = self.row(); name = 'longhorn-node-'+address.rsplit('.', 1)[1]
        obj = self.lab.get('nodes.longhorn.io', name, NS)
        if not obj or obj['metadata']['uid'] != row['native_uids'][address]:
            raise ValueError('V2 lifecycle native Node identity changed')
        return obj

    def disk(self, address, *, absent=False):
        row = self.row(); node = self.native(address)
        spec = node['spec'].get('disks', {}).get(DISK)
        status = node.get('status', {}).get('diskStatus', {}).get(DISK, {})
        if absent:
            if spec or status: raise ValueError('retired V2 disk remains in native Node')
        elif (not spec or spec.get('diskType') != 'block' or spec.get('diskDriver') != 'aio' or
                spec.get('path') != row['raw_disks'][address]['path'] or
                status.get('diskUUID') != row['disk_uuids'][address]):
            raise ValueError('V2 lifecycle native disk UUID/driver/stable path changed')
        return node

    def topology(self, *, excluded=None):
        business = self.workload.journal(); lab = self.lab
        result = lab.replica_topology(business, 3)
        if not result or (excluded and any(r['node'] == 'longhorn-node-'+excluded.rsplit('.', 1)[1] for r in result.values())):
            return None
        mapping = {self.native(a)['metadata']['name']: a for a in self.row()['disk_uuids']}
        for name, identity in result.items():
            replica = lab.get('replicas.longhorn.io', name, NS); address = mapping[identity['node']]
            self.disk(address)
            if (replica['metadata']['uid'] != identity['uid'] or replica['spec'].get('dataEngine') != 'v2' or
                    replica['spec'].get('diskID') != self.row()['disk_uuids'][address]):
                raise ValueError('V2 lifecycle replica is outside its exact approved raw disk')
        return result

    def data(self):
        identities = self.workload.identities()
        if identities != self.row()['binding']: raise ValueError('V2 lifecycle retained CSI identities changed')
        actual = self.workload.data(self.row()['payload'], read=True)
        row = self.row()
        print('LONGHORN_V2_DISK_DATA_READBACK '+json.dumps({
            'sha256': actual, 'payload_bytes': len(row['payload'].encode())*262144,
            'binding': identities,
            'disk_uuids': row['disk_uuids']}, sort_keys=True), flush=True)
        return actual

    def admit(self, proof_path):
        lab = self.lab; lab.require_lease()
        transition = accepted_source()
        lab.core_identity(); lab.installation_health()
        if self.path.exists() or self.path.is_symlink() or self.workload.path.exists():
            raise ValueError('V2 disk lifecycle needs a new owned journal')
        proof = release_proof(proof_path); pool = load('longhorn-v2-pool.py'); pool.HOSTS = (HOST,)
        collected = pool.collect(HOST); raw = disk_identity(proof, collected)
        lab.execute(['bash', lab.BASE/'tests/helpers/lab-wipe-nodes.sh', '--verify', '--rocky-only', HOST], timeout=180)
        config = (lab.DIRECTORY/'config.yml').read_text(); parsed = yaml.safe_load(config)
        if (parsed['longhorn_data_engines'] != ['v2'] or parsed['longhorn_requests'] or
                parsed['longhorn_v2_nodes'] != list(SOURCE) or set(parsed['longhorn_block_disks']) != set(SOURCE)):
            raise ValueError('V2 disk lifecycle needs its idle three-source AIO baseline')
        if proof['target_cluster_uid'] == lab.owner()['cluster_uid']:
            raise ValueError('receiving release must belong to the independent target cluster')
        install = load('longhorn-v2-install.py')
        expected = {name: hashlib.sha256(data).hexdigest() for name, data in install.host_files(lab).items()}
        host = json.loads(lab.remote(HOST, ['python3', '-c', lab.host_state_script()],
            stdin=json.dumps({'action': 'capture', 'expected': expected})).stdout)
        multipath = install.multipath(lab, HOST, 'probe')
        natives = {a: lab.get('nodes.longhorn.io', 'longhorn-node-'+a.rsplit('.', 1)[1], NS) for a in SOURCE}
        row = {'run_id': lab.owner()['run_id'], 'cluster_uid': lab.owner()['cluster_uid'], 'host': HOST,
            'release': proof, 'release_sha256': hashlib.sha256(Path(proof_path).read_bytes()).hexdigest(),
            'accepted_upgrade_baseline': transition,
            'config': config, 'allowed_inputs': [config], 'inventory': (lab.DIRECTORY/'hosts').read_text(),
            'raw_disks': dict(lab.owner()['raw_disks'], **{HOST: raw}),
            'native_uids': {a: n['metadata']['uid'] for a, n in natives.items()},
            'disk_uuids': {a: n['status']['diskStatus'][DISK]['diskUUID'] for a, n in natives.items()},
            'hostname': collected['inventory']['hostname'], 'host_before': host, 'host_expected': expected,
            'multipath': {'original': multipath, 'current': multipath, 'phase': 'planned'},
            'canaries': {}, 'id': str(uuid.uuid4()), 'retirements': {}, 'phase': 'admitted', 'payload': secrets.token_hex(16)}
        self.save(row)
        # Existing cleanup probe creates only one inode-bound canary file.
        cleanup = load('longhorn-v2-cleanup.py')
        row['canaries'][HOST] = cleanup.remote(lab, row, HOST, 'create', True); self.save(row)
        print('LONGHORN_V2_DISK_RECEIVING_HOST_ADMITTED host='+HOST, flush=True)

    def join(self):
        """Prepare before add-node's embedded Longhorn play; recover its own partial join."""
        lab = self.lab; row = self.row()
        if row['phase'] not in ('admitted', 'joining'):
            raise ValueError('receiving join requires its original admission intent')
        inventory = (lab.DIRECTORY/'hosts').read_text()
        if inventory not in (row['inventory'], extended_inventory(row['inventory'])):
            raise ValueError('receiving join inventory changed outside lifecycle')
        node = lab.get('node', NAME)
        if node:
            info = node.get('status', {}).get('nodeInfo', {})
            addresses = [a['address'] for a in node.get('status', {}).get('addresses', []) if a['type'] == 'InternalIP']
            if (row['phase'] != 'joining' or addresses != [HOST] or
                    info.get('machineID') != row['raw_disks'][HOST]['machine_id'] or
                    info.get('bootID') != row['multipath']['original']['boot_id'] or
                    (row.get('node_uid') and node['metadata']['uid'] != row['node_uid'])):
                raise ValueError('unconfirmed/replaced receiving Node; retain lifecycle journal')
            native = lab.get('nodes.longhorn.io', NAME, NS)
            if native and (native['spec'].get('disks') or (row['native_uids'].get(HOST) and
                    native['metadata']['uid'] != row['native_uids'][HOST])):
                raise ValueError('partial receiving join has foreign disks or native identity')
            for kind in ('instancemanagers.longhorn.io', 'replicas.longhorn.io', 'engines.longhorn.io', 'enginefrontends.longhorn.io'):
                if any(obj['spec'].get('nodeID') == NAME for obj in lab.get(kind, namespace=NS)['items']):
                    raise ValueError('partial receiving join has native consumers')
            row['node_uid'] = node['metadata']['uid']
            if native: row['native_uids'][HOST] = native['metadata']['uid']
        elif inventory != row['inventory'] or row.get('node_uid'):
            raise ValueError('receiving join lost its recorded Node')
        install = load('longhorn-v2-install.py'); entry = row['multipath']
        if entry['phase'] == 'planned':
            entry['phase'] = 'stop-intent'; self.save(row)
            entry['current'] = install.multipath(lab, HOST, 'stop', expected=entry['current'])
            entry['phase'] = 'stopped'
        elif entry['phase'] != 'stopped':
            raise ValueError('ambiguous receiving multipath write')
        row['phase'] = 'joining'; self.save(row)
        if node: self.recover_join_network()
        if inventory == row['inventory']: supervise(lab, self.pid, 'add-node')
        node = lab.get('node', NAME)
        if not node or (row.get('node_uid') and node['metadata']['uid'] != row['node_uid']):
            raise ValueError('product add-node has not retained receiving Node')
        row = self.row(); row['node_uid'] = node['metadata']['uid']; row['phase'] = 'joined'; self.save(row)
        lab.core_identity()

    def recover_join_network(self):
        """Retire only proven pre-fix bridge Pods on this empty receiving node.

        This repairs the failed fixture, not customer workloads. Fresh joins
        use the corrected product CNI path and have no bridge allocations.
        """
        lab = self.lab; row = self.row(); lab.core_identity()
        if row['phase'] != 'joining' or not row.get('node_uid'):
            raise ValueError('network recovery requires the original partial join')
        probe = json.loads(lab.remote(HOST, ['python3', '-c', JOIN_NETWORK_PROBE]).stdout)
        if (probe['machine_id'] != row['raw_disks'][HOST]['machine_id'] or
                probe['boot_id'] != row['multipath']['original']['boot_id']):
            raise ValueError('receiving network host identity changed')
        pods = lab.get('pods', namespace=NS)['items']
        def ready(pod):
            return not pod['metadata'].get('deletionTimestamp') and any(
                c['type'] == 'Ready' and c['status'] == 'True' for c in pod.get('status', {}).get('conditions', []))
        # An unavailable source is not permission to repair a fourth node.
        for address in SOURCE:
            name = 'longhorn-node-'+address.rsplit('.',1)[1]
            for app in ('longhorn-manager','longhorn-csi-plugin'):
                matches = [p for p in pods if p['spec'].get('nodeName') == name and
                           p['metadata'].get('labels',{}).get('app') == app]
                if len(matches) != 1 or not ready(matches[0]):
                    raise ValueError('original source component is not healthy')
        calico = [p for p in lab.get('pods', namespace='kube-system')['items']
                  if p['spec'].get('nodeName') == NAME and p['metadata'].get('labels',{}).get('k8s-app') == 'calico-node']
        if len(calico) != 1 or not ready(calico[0]): raise ValueError('receiving Calico is not ready')
        repairs = row.setdefault('join_network_repairs', {})
        if probe['allocations'] and not row.get('join_network_probe'):
            row['join_network_probe'] = probe
        for allocation in probe['allocations']:
            matches = [p for p in pods if p['metadata']['uid'] == allocation['uid']]
            if len(matches) != 1: raise ValueError('bridge sandbox has no unique owning API Pod')
            pod = matches[0]; meta = pod['metadata']; owners = meta.get('ownerReferences', [])
            if (allocation['namespace'] != NS or meta['name'] != allocation['name'] or
                    pod['spec'].get('nodeName') != NAME or pod['spec'].get('hostNetwork',False) or
                    pod['status'].get('podIP') != allocation['ip'] or len(owners) != 1 or
                    owners[0].get('kind') != 'DaemonSet' or owners[0].get('controller') is not True or
                    not (owners[0]['name'] == 'longhorn-csi-plugin' or
                         re.fullmatch(r'engine-image-ei-[0-9a-f]+',owners[0]['name']))):
                raise ValueError('bridge Pod is outside the empty-node recovery scope')
            ds = lab.get('daemonset', owners[0]['name'], NS)
            if not ds or ds['metadata']['uid'] != owners[0]['uid']:
                raise ValueError('bridge Pod controller identity changed')
            # Persist exact UID and resourceVersion before the native guarded DELETE.
            repair = repairs.get(meta['uid'])
            if repair and (repair['pod']['metadata']['uid'] != meta['uid'] or
                           repair['sandbox'] != allocation['sandbox'] or repair['cni_sha256'] != probe['cni_sha256']):
                raise ValueError('recorded bridge recovery changed')
            repairs[meta['uid']] = {'pod':pod,'sandbox':allocation['sandbox'],
                'cni_sha256':probe['cni_sha256'],'phase':'delete-intent'}
        self.save(row)
        for uid, repair in repairs.items():
            if repair['phase'] == 'ready': continue
            old = repair['pod']; owner = old['metadata']['ownerReferences'][0]
            current = lab.get('pod', old['metadata']['name'], NS)
            if current:
                if current['metadata']['uid'] != uid:
                    raise ValueError('recorded bridge Pod was replaced under its old name')
                lab.remove(current)
            def replacement():
                candidates = [p for p in lab.get('pods', namespace=NS)['items']
                    if p['spec'].get('nodeName') == NAME and p['metadata']['uid'] != uid and
                    p['metadata'].get('ownerReferences') == old['metadata']['ownerReferences']]
                return candidates[0] if len(candidates) == 1 and ready(candidates[0]) else None
            new = lab.wait('disk45-real-cni-'+owner['name'], 300, replacement)
            repair['replacement_uid'] = new['metadata']['uid']; repair['phase'] = 'ready'; self.save(row)
        after = json.loads(lab.remote(HOST, ['python3', '-c', JOIN_NETWORK_PROBE]).stdout)
        if (after['machine_id'],after['boot_id'],after['cni_sha256']) != (
                probe['machine_id'],probe['boot_id'],probe['cni_sha256']) or after['allocations']:
            raise ValueError('receiving bridge consumers or CNI drift remain')
        print('LONGHORN_V2_DISK_PARTIAL_JOIN_NETWORK_RECOVERED pods='+str(len(repairs)), flush=True)

    def enable(self):
        self.join()
        lab = self.lab; row = self.row()
        # Qualify the new client with V2 selectively disabled, before pages or disk.
        self.config(self.current())
        node = lab.get('node', NAME); native = lab.get('nodes.longhorn.io', NAME, NS)
        if (node['metadata'].get('labels', {}).get('node.longhorn.io/disable-v2-data-engine') != 'true' or
                not native or native['spec'].get('disks') or any(im['spec'].get('nodeID') == NAME and
                im['spec'].get('dataEngine') == 'v2' for im in lab.get('instancemanagers.longhorn.io', namespace=NS)['items'])):
            raise ValueError('new client was not selectively disabled without a V2 disk/IM')
        row = self.row(); row['native_uids'][HOST] = native['metadata']['uid']; self.save(row)
        resources = load('longhorn-v2-resources.py'); before = resources.probe(lab, HOST)
        if before['total'] or before['free'] or before['reserved'] or before['machine_id'] != row['raw_disks'][HOST]['machine_id']:
            raise ValueError('receiving hugepage pool differs from release')
        row['pages'] = {'original': before, 'current': before, 'phase': 'write-intent'}; self.save(row)
        row['pages']['current'] = resources.probe(lab, HOST, target=1024, expected=before)
        row['pages']['phase'] = 'allocated'; self.save(row)
        if row['pages']['current']['total'] != 1024:
            self.retry_receiving_pages()
            row = self.row()
        if row['pages']['current']['total'] != 1024: raise ValueError('receiving page allocation incomplete')
        lab.remote(HOST, ['systemctl', 'restart', 'kubelet'])
        product = resources.product()
        lab.wait('disk45-client-hugepage-refresh', 180, lambda: lab.get('node', NAME)['metadata']['uid'] == row['node_uid'] and
            product.quantity(lab.get('node', NAME)['status']['allocatable'].get('hugepages-2Mi', 0)) == 2*1024**3)
        config = self.current(); config['longhorn_v2_nodes'].append(HOST)
        config['longhorn_block_disks'][HOST] = [{'name': DISK, 'path': row['raw_disks'][HOST]['path'],
            'identity': row['raw_disks'][HOST]['identity'], 'disk_driver': 'aio', 'storage_reserved': 1024**3}]
        row['phase'] = 'enabling'; self.save(row); self.config(config)
        native = self.native(HOST); row = self.row()
        row['disk_uuids'][HOST] = native['status']['diskStatus'][DISK]['diskUUID']; row['phase'] = 'enabled'; self.save(row)
        self.disk(HOST); lab.installation_health(); self.data()
        print('LONGHORN_V2_DISK_CLIENT_EXPANSION_PASS host='+HOST+' selective_disable=true raw_disks=4', flush=True)

    def maintenance(self, address, allow, evict):
        config = self.current(); config.update(longhorn_action='present', longhorn_retire_block_disks=[])
        disk = config['longhorn_block_disks'][address][0]
        disk.update(allow_scheduling=allow, eviction_requested=evict)
        self.config(config)

    def retire(self, address):
        row = self.row()
        operation = row['retirements'].get(address)
        if operation is None:
            self.disk(address)
            operation = 'disk45-'+address.rsplit('.', 1)[1]+'-'+uuid.uuid4().hex[:12]
            row['retirements'][address] = operation; self.save(row)
        config = self.current(); config.update(longhorn_action='retire-block-disks',
            longhorn_block_retirement_id=operation, longhorn_retire_block_disks=[
                {'node': address, 'name': DISK, 'disk_uuid': row['disk_uuids'][address]}])
        self.config(config)
        self.disk(address, absent=True)
        archive = self.lab.DIRECTORY/'yml/longhorn'/('block-retirement-'+operation+'.json')
        proof = json.loads(archive.read_text())
        if (proof['phase'] != 'complete' or proof['cluster_uid'] != row['cluster_uid'] or
                address in json.loads((archive.parent/'block-disks.json').read_text())['nodes']):
            raise ValueError('product retirement has not released the precise native/host receipts')
        released = self.lab.remote(address, ['test', '!', '-e', '/etc/kubeauto/longhorn-block-disks.json'], check=False)
        if released.returncode: raise ValueError('retired host disk receipt remains')
        row = self.row()
        if address not in row.setdefault('retired', []): row['retired'].append(address)
        self.save(row)
        print('LONGHORN_V2_DISK_NATIVE_RETIREMENT_PASS host='+address+' disk_uuid='+row['disk_uuids'][address], flush=True)

    def business(self):
        lab = self.lab; row = self.row()
        if row['phase'] != 'admitted': raise ValueError('V2 disk business needs fresh admission')
        config = self.current(); candidates = [c['name'] for c in config['longhorn_storage_classes']
            if c.get('data_engine') == 'v2' and c['replicas'] == 3 and c['reclaim_policy'] == 'Retain'
            and not c['encrypted'] and c.get('clone_mode', 'full-copy') == 'full-copy']
        if not candidates: raise ValueError('V2 disk lifecycle needs an explicit three-replica Retain class')
        self.workload.prepare(candidates[0]); self.workload.data(row['payload'])
        row = self.row(); row['binding'] = self.workload.identities(); self.save(row)
        before = lab.wait('disk45-original-three-raw-replicas', 600, self.topology)
        row['original_replicas'] = before; self.save(row)
        source = SOURCE[-1]
        # Exact product guard refuses active source media before any native deletion.
        trial = copy.deepcopy(config); trial['longhorn_block_disks'][source][0].update(
            allow_scheduling=False, eviction_requested=True)
        trial.update(longhorn_action='retire-block-disks', longhorn_block_retirement_id='busy-'+uuid.uuid4().hex[:12],
            longhorn_retire_block_disks=[{'node': source, 'name': DISK, 'disk_uuid': row['disk_uuids'][source]}])
        self.config(trial, refusal=BUSY_REFUSAL)
        if self.topology() != before: raise ValueError('refused busy retirement changed source replicas')
        self.config(config); self.data(); self.enable()
        # Safe node deletion refuses while the new raw disk is still declared.
        supervise(lab, self.pid, 'del-node', REMOVAL_REFUSAL)
        self.maintenance(source, False, True)
        after = lab.wait('disk45-original-source-evacuated', 900, lambda: self.topology(excluded=source))
        kept = {n: r for n, r in before.items() if r['node'] != 'longhorn-node-148'}
        if (not all(after.get(n) == r for n, r in kept.items()) or
                sum(r['node'] == NAME for r in after.values()) != 1):
            raise ValueError('eviction did not preserve healthy peer replicas and rebuild on fourth raw disk')
        print('LONGHORN_V2_DISK_REPLICA_RELOCATION '+json.dumps({
            'source': source, 'target': HOST, 'before': before, 'after': after}, sort_keys=True), flush=True)
        self.data(); self.retire(source)
        # No wipe or format. DiskCreate must import the same original lvstore UUID.
        self.maintenance(source, False, False); self.disk(source)
        self.maintenance(source, True, False); self.data()
        print('LONGHORN_V2_DISK_REPLACEMENT_REIMPORT_PASS source='+source+' replica_domain='+HOST, flush=True)
        self.maintenance(HOST, False, True)
        restored = lab.wait('disk45-original-three-restored', 900, lambda: self.topology(excluded=HOST))
        print('LONGHORN_V2_DISK_ORIGINAL_TOPOLOGY_RESTORED '+json.dumps(restored, sort_keys=True), flush=True)
        self.data(); self.retire(HOST)
        self.workload.detach(); self.workload.mount('reader', 1); self.data()
        row = self.row(); row['business_passed'] = True; self.save(row)
        print('LONGHORN_V2_DISK_LIFECYCLE_BUSINESS_PASS', flush=True)

    def cleanup(self):
        lab = self.lab; row = self.row(); config = self.current()
        # A failed add-node may register kubelet before committing inventory.
        # Complete only that admitted product operation before using del-node.
        if row['phase'] == 'joining':
            self.join(); row = self.row()
            native = lab.get('nodes.longhorn.io', NAME, NS)
            if native:
                if native['spec'].get('disks'): raise ValueError('partial join unexpectedly acquired storage')
                row['native_uids'][HOST] = native['metadata']['uid']; self.save(row)
        # First remove only this module's exact CSI objects. Never force a disk
        # release while they or another consumer remain.
        self.workload.cleanup()
        node = lab.get('node', NAME)
        if node:
            if not row.get('node_uid') or node['metadata']['uid'] != row['node_uid']:
                raise ValueError('unconfirmed/replaced receiving Node; retain lifecycle journal')
            source = SOURCE[-1]
            if HOST not in row.get('retired', []):
                if source in row.get('retirements', {}) and source not in row.get('retired', []):
                    self.retire(source)
                # Restore the source before retiring the fourth failure domain.
                self.maintenance(source, False, False); self.maintenance(source, True, False); self.disk(source)
                native = self.native(HOST)
                if DISK in native['spec'].get('disks', {}):
                    self.maintenance(HOST, False, True); self.retire(HOST)
                elif HOST in row.get('retirements', {}):
                    self.retire(HOST)
            config = self.current(); config.update(longhorn_action='present', longhorn_retire_block_disks=[])
            config['longhorn_block_disks'].pop(HOST, None)
            config['longhorn_v2_nodes'] = list(SOURCE)
            config['longhorn_block_disks'][source][0].update(allow_scheduling=True, eviction_requested=False)
            self.config(config)
            lab.wait('disk45-receiving-im-retired', 300, lambda: not any(im['spec'].get('nodeID') == NAME and
                im['spec'].get('dataEngine') == 'v2' for im in lab.get('instancemanagers.longhorn.io', namespace=NS)['items']))
            # Restore the administrator's page pool while the qualified kubelet
            # is still active. The existing resource fixture owns every write.
            self.restore_pages()
            native = self.native(HOST)
            spec = copy.deepcopy(native['spec']); spec.update(allowScheduling=False, evictionRequested=False)
            load('longhorn-disk-lifecycle.py').patch(lab, native, spec)
            # The customer's removal command owns core/runtime cleanup.
            row = self.row(); row['phase'] = 'deleting'; self.save(row)
            supervise(lab, self.pid, 'del-node')
            lab.wait('disk45-kubernetes-node-retired', 180, lambda: not lab.get('node', NAME))
        elif row.get('node_uid') and (lab.DIRECTORY/'hosts').read_text() != row['inventory']:
            raise ValueError('receiving Node vanished before product inventory retirement')
        native = lab.get('nodes.longhorn.io', NAME, NS)
        if native:
            self.native(HOST)
            if native['spec'].get('allowScheduling', True) or native['spec'].get('disks'):
                raise ValueError('receiving native Node is not empty/disabled after product removal')
            lab.remove(native); lab.wait('disk45-native-node-retired', 180, lambda: not lab.get('nodes.longhorn.io', NAME, NS))
        row = self.row()
        if (lab.DIRECTORY/'hosts').read_text() != row['inventory']: raise ValueError('original V2 inventory bytes not restored')
        cleanup = load('longhorn-v2-cleanup.py')
        if row.get('canaries'):
            after = cleanup.remote(lab, row, HOST, 'check', True)
            if after['driver'] != row['canaries'][HOST]['driver']: raise ValueError('AIO kernel driver binding changed')
        if row.get('pages') and load('longhorn-v2-resources.py').probe(lab, HOST)['total']:
            raise ValueError('receiving page pool not restored before product node removal')
        install = load('longhorn-v2-install.py'); installed = {n: hashlib.sha256(data).hexdigest() for n, data in install.host_files(lab).items()}
        lab.remote(HOST, ['python3', '-c', lab.host_state_script()], stdin=json.dumps(
            {'action': 'restore', 'expected': row['host_expected'], 'installed': installed, 'before': row['host_before']}))
        entry = row['multipath']
        if entry['phase'] not in ('planned', 'stopped', 'restored'): raise ValueError('ambiguous receiving multipath write')
        entry['current'] = install.multipath(lab, HOST, 'restore', expected=entry['current'], original=entry['original'])
        entry['phase'] = 'restored'; self.save(row)
        if row.get('join_network_probe'):
            if not row.get('join_network_repairs') or any(r['phase'] != 'ready' for r in row['join_network_repairs'].values()):
                raise ValueError('receiving bridge recovery has not completed')
            lab.remote(HOST, ['python3', '-c', inspect.getsource(release_join_bridge)+
                '\nimport json,sys\nrelease_join_bridge(json.load(sys.stdin))\n'], stdin=json.dumps(row['join_network_probe']))
        certificates = load('longhorn-node-lifecycle.py')
        expected = certificates.worker_certificates(lab, NAME)
        if row.setdefault('worker_certificates', expected) != expected:
            raise ValueError('original worker certificate source changed')
        self.save(row)
        certificates.retire_worker_certificates(lab, HOST, NAME, row['raw_disks'][HOST]['machine_id'],
                                               row['multipath']['original']['boot_id'], expected)
        if row.get('phase') == 'deleting' and row.get('business_passed'):
            # Reconcile the already removed, still leased node using the same
            # product cleanup task. No node/disk/volume is recreated for repair.
            tasks = yaml.safe_load((lab.BASE/'roles/clean/tasks/clean_node.yml').read_text())[0]['block']
            task = next(t for t in tasks if t['name'] == 'Stop Kubernetes pod slices before removing node state')
            lab.remote(HOST, ['bash', '-c', task['ansible.builtin.shell']], timeout=180)
        lab.execute(['bash', lab.BASE/'tests/helpers/lab-wipe-nodes.sh', '--verify', '--rocky-only', HOST], timeout=180)
        # Product hostname changes are restored only after core removal.
        lab.remote(HOST, ['python3', '-c', 'import pathlib,socket,subprocess,sys\n'
            'assert pathlib.Path("/etc/machine-id").read_text().strip()==sys.argv[1]\n'
            'assert socket.gethostname() in (sys.argv[2],sys.argv[3])\n'
            'subprocess.run(["hostnamectl","set-hostname",sys.argv[3]],check=True,timeout=30)\n',
            row['raw_disks'][HOST]['machine_id'], NAME, row['hostname']])
        if row.get('canaries'):
            cleanup.remote(lab, row, HOST, 'remove', True); row['canaries'] = {}; self.save(row)
        pool = load('longhorn-v2-pool.py'); pool.HOSTS = (HOST,)
        disk_identity(row['release'], pool.collect(HOST))
        load('longhorn-upgrade-lifecycle.py').atomic_config(lab, yaml.safe_load(row['config']))
        (lab.DIRECTORY/'config.yml').write_text(row['config'])
        supervise(lab, self.pid, 'longhorn-setup'); lab.core_identity(); lab.installation_health()
        row = self.row(); row['phase'] = 'clean'; self.save(row)
        print('LONGHORN_V2_DISK_LIFECYCLE_SCOPED_CLEAN_PASS', flush=True)

    def retry_receiving_pages(self):
        """A single bounded compaction/retry after the first recorded short allocation."""
        row = self.row(); pages = row['pages']; current = pages['current']
        if (row['phase'] != 'joined' or pages['phase'] != 'allocated' or
                pages.get('compaction') or current['machine_id'] != row['raw_disks'][HOST]['machine_id'] or
                current['boot_id'] != pages['original']['boot_id']):
            raise ValueError('receiving page retry is not a fresh owned short allocation')
        node = self.lab.get('node', NAME); native = self.native(HOST)
        if (node['metadata']['uid'] != row['node_uid'] or
                node['metadata'].get('labels', {}).get('node.longhorn.io/disable-v2-data-engine') != 'true' or
                native['spec'].get('disks') or any(im['spec'].get('nodeID') == NAME and
                im['spec'].get('dataEngine') == 'v2' for im in self.lab.get(
                    'instancemanagers.longhorn.io', namespace=NS)['items'])):
            raise ValueError('receiving compaction requires the original disabled diskless client')
        pages['compaction'] = 'intent'; self.save(row)
        self.lab.remote(HOST, ['python3', '-c', inspect.getsource(compact_receiving_pages)+
            '\nimport json,sys\ncompact_receiving_pages(json.load(sys.stdin))\n'],
            stdin=json.dumps(current), timeout=120)
        pages['compaction'] = 'complete'; pages['phase'] = 'write-intent'; self.save(row)
        pages['current'] = load('longhorn-v2-resources.py').probe(self.lab, HOST, target=1024, expected=current)
        pages['phase'] = 'allocated'; self.save(row)
        print('LONGHORN_V2_DISK_RECEIVING_PAGE_RETRY '+json.dumps({
            'before':current['total'], 'after':pages['current']['total'], 'compactions':1}), flush=True)

    def restore_pages(self):
        row = self.row()
        if not row.get('pages'): return
        pages = row['pages']; resources = load('longhorn-v2-resources.py')
        if pages['phase'] in ('write-intent', 'restore-intent'):
            raise ValueError('ambiguous receiving page write; retain exact journal')
        def free():
            actual = resources.probe(self.lab, HOST)
            for key in ('machine_id', 'boot_id', 'total'):
                if actual[key] != pages['current'][key]: raise ValueError('receiving page pool changed outside lifecycle')
            return actual if actual['free'] == actual['total'] and not actual['reserved'] else None
        actual = self.lab.wait('disk45-receiving-page-pool-free', 180, free)
        if actual['total']:
            pages['phase'] = 'restore-intent'; self.save(row)
            pages['current'] = resources.probe(self.lab, HOST, target=0, expected=actual)
            pages['phase'] = 'restored'; self.save(row)
        if pages['current']['total'] or pages['current']['free'] or pages['current']['reserved']:
            raise ValueError('receiving page pool is not fully released')


def verify_log(log, completed):
    """Independently check original readbacks and the finalized owning journal."""
    if re.search(r'^(LONGHORN_[A-Z0-9_]*(?:FAIL|FIRST_FAILURE)(?: |$)|FAILED|ERROR[:!]|FAIL:)', log, re.M):
        raise ValueError('V2 disk log contains its first failure')
    lines = log.splitlines()
    def rows(prefix):
        return [json.loads(line[len(prefix):]) for line in lines if line.startswith(prefix)]
    row = completed
    if (row.get('phase') != 'clean' or row.get('business_passed') is not True or row.get('host') != HOST or
            set(row.get('raw_disks', {})) != set(SOURCE+(HOST,)) or
            set(row.get('disk_uuids', {})) != set(SOURCE+(HOST,)) or
            not {SOURCE[-1], HOST} <= set(row.get('retired', [])) or row.get('canaries') or
            row.get('multipath', {}).get('phase') != 'restored'):
        raise ValueError('V2 disk journal lacks completed source/target restoration')
    for raw in row['raw_disks'].values():
        if not raw.get('identity') or not raw.get('path', '').startswith('/dev/disk/by-id/'):
            raise ValueError('V2 disk journal lacks stable hardware identity')
    data = rows('LONGHORN_V2_DISK_DATA_READBACK ')
    expected = hashlib.sha256(row['payload'].encode()*262144).hexdigest()
    if len(data) < 6:
        raise ValueError('V2 disk lacks the actual original, moved and remounted readbacks')
    for readback in data:
        if (readback.get('sha256') != expected or readback.get('payload_bytes') != len(row['payload'].encode())*262144 or
                readback.get('binding') != row['binding'] or
                not set(SOURCE) <= set(readback.get('disk_uuids',{})) <= set(SOURCE+(HOST,)) or
                any(row['disk_uuids'].get(address) != value for address, value in readback.get('disk_uuids', {}).items())):
            raise ValueError('V2 disk original payload, CSI identities or native replicas differ')
    moved = rows('LONGHORN_V2_DISK_REPLICA_RELOCATION ')
    if len(moved) != 1:
        raise ValueError('V2 disk lacks its unique actual replica relocation')
    restored = rows('LONGHORN_V2_DISK_ORIGINAL_TOPOLOGY_RESTORED ')
    if len(restored) != 1 or len(restored[0]) != 3:
        raise ValueError('V2 disk lacks its original three-node restored topology')
    move = moved[0]; before = move['before']; after = move['after']
    for topology in (before,after,restored[0]):
        if len(topology) != 3 or len({r.get('uid') for r in topology.values() if r.get('uid')}) != 3:
            raise ValueError('V2 disk topology lacks three independent original replica identities')
    kept = {n:r for n,r in before.items() if r['node'] != 'longhorn-node-148'}
    if (move.get('source') != SOURCE[-1] or move.get('target') != HOST or before != row['original_replicas'] or
            {r['node'] for r in before.values()} != {'longhorn-node-'+a.rsplit('.',1)[1] for a in SOURCE} or
            len(kept) != 2 or not all(after.get(n) == r for n,r in kept.items()) or
            {r['uid'] for r in before.values()} >= {r['uid'] for r in after.values()} or
            sum(r.get('node') == NAME for r in after.values()) != 1 or
            any(r.get('node') == 'longhorn-node-148' for r in after.values()) or
            {r['node'] for r in restored[0].values()} != {'longhorn-node-'+a.rsplit('.',1)[1] for a in SOURCE}):
        raise ValueError('V2 disk relocation/remount lacks the original independent failure domains')
    for marker in ('LONGHORN_V2_DISK_CLIENT_EXPANSION_PASS host='+HOST+' selective_disable=true raw_disks=4',
                   'LONGHORN_V2_DISK_REPLACEMENT_REIMPORT_PASS source='+SOURCE[-1]+' replica_domain='+HOST,
                   'LONGHORN_V2_DISK_LIFECYCLE_BUSINESS_PASS', 'LONGHORN_V2_DISK_LIFECYCLE_SCOPED_CLEAN_PASS',
                   'LONGHORN_V2_DISK_LIFECYCLE_FINALIZED',
                   'LONGHORN_V2_DISK_EXPECTED_REFUSAL step=longhorn-setup',
                   'LONGHORN_V2_DISK_EXPECTED_REFUSAL step=del-node'):
        if lines.count(marker) != 1:
            raise ValueError('V2 disk lacks its unique product expansion, retirement or cleanup result')
    return {'data_engine': 'v2', 'payload_sha256': expected, 'binding': row['binding'],
            'relocation': move, 'readbacks': len(data), 'cluster_uid': row['cluster_uid'], 'run_id': row['run_id']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('admit', 'business', 'clean', 'finalize', 'verify-core',
                                        'relay-ensure', 'relay-maintain', 'relay-clean'))
    parser.add_argument('--released-target-proof'); parser.add_argument('--relay-pid', type=int, default=0)
    args = parser.parse_args(); lab = fixture(); lab.require_lease(); lab.owner()
    if args.action == 'verify-core': lab.core_identity(); return
    if args.action.startswith('relay-'):
        journal(lab)
        {'relay-ensure': lab.relay_ensure, 'relay-maintain': lab.relay_maintain, 'relay-clean': lab.relay_cleanup}[args.action]()
        return
    scenario = Lifecycle(lab, args.relay_pid)
    if args.action == 'admit':
        if not args.released_target_proof: parser.error('admit needs --released-target-proof')
        scenario.admit(args.released_target_proof)
    elif args.action == 'finalize':
        row = scenario.row()
        if row['phase'] != 'clean' or lab.owner().get('relays'): raise ValueError('V2 lifecycle cleanup/transport incomplete')
        lab.core_identity()
        archive = lab.DIRECTORY/('longhorn-v2-disk-lifecycle-completed-'+row['id']+'.json')
        if archive.is_symlink() or (archive.exists() and json.loads(archive.read_text()) != row):
            raise ValueError('completed V2 lifecycle archive changed')
        if not archive.exists(): lab.write_private(archive, row)
        scenario.path.unlink()
        print('LONGHORN_V2_DISK_LIFECYCLE_COMPLETED path='+str(archive)+' sha256='+hashlib.sha256(archive.read_bytes()).hexdigest(), flush=True)
        print('LONGHORN_V2_DISK_LIFECYCLE_FINALIZED', flush=True)
    else:
        if not args.relay_pid: parser.error('business/clean need --relay-pid')
        {'business': scenario.business, 'clean': scenario.cleanup}[args.action]()


if __name__ == '__main__':
    def interrupted(signum, frame): raise SystemExit(128+signum)
    signal.signal(signal.SIGINT, interrupted); signal.signal(signal.SIGTERM, interrupted)
    try: main()
    except (ValueError, KeyError, TypeError, OSError, TimeoutError, subprocess.SubprocessError) as error:
        print('LONGHORN_V2_DISK_LIFECYCLE_FAIL reason='+str(error), file=sys.stderr, flush=True)
        sys.exit(1)
