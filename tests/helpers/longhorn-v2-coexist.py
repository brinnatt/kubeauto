#!/usr/bin/env python3
"""LONGHORN-46: explicit dual-engine integration on three failure domains.

The V1 filesystem fixtures are bounded loop files on the three approved test
hosts. They prove engine/class integration, not independent physical-media
performance. Production host admission and all reconciliation remain unchanged.
"""
import argparse
import base64
import copy
import hashlib
import importlib.util
import inspect
import json
from pathlib import Path
import secrets
import signal
import sys
import uuid

import yaml

NS = 'longhorn-system'
RECORD = 'longhorn-v2-coexist.json'
MOUNT = '/data/kubeauto-longhorn-coexist46'
SIZE = 24 * 1024**3


def host_filesystem(request, action, root='/'):
    """Own the file inode, loop association, mount UUID and exact fstab line."""
    import hashlib
    import json
    import os
    from pathlib import Path
    import shutil
    import stat
    import subprocess
    import uuid

    root = Path(root)
    token = str(uuid.UUID(request['id']))
    fsid = str(uuid.UUID(request['uuid']))
    base = root/'var/lib/kubeauto-longhorn-coexist46'
    directory = base/token
    image = directory/'disk.img'
    record = directory/'owner.json'
    mount = root/'data/kubeauto-longhorn-coexist46'
    fstab = root/'etc/fstab'
    line = str(image)+' '+str(mount)+' ext4 loop,defaults 0 0 # kubeauto-longhorn-coexist46:'+token+'\n'
    if action not in ('prepare', 'check', 'disk-identity', 'remove'):
        raise ValueError('unknown coexistence filesystem action')
    for path in (base, directory, image, record, mount.parent, mount, fstab):
        if path.is_symlink(): raise ValueError('symlink coexistence fixture')
    machine = (root/'etc/machine-id').read_text().strip()
    if machine != request['machine_id'] or request['size'] != 24*1024**3:
        raise ValueError('coexistence host or capacity changed')

    def run(argv, *, check=True):
        return subprocess.run(argv, check=check, text=True, capture_output=True, timeout=120)

    def identity(path):
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid():
            raise ValueError('foreign coexistence file')
        return {'device': info.st_dev, 'inode': info.st_ino, 'size': info.st_size}

    def save(row):
        temporary = directory/'owner.new'
        fd = os.open(temporary, os.O_CREAT|os.O_EXCL|os.O_WRONLY|os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'w') as stream:
            json.dump(row, stream, sort_keys=True); stream.flush(); os.fsync(stream.fileno())
        temporary.replace(record)

    def config():
        lines = fstab.read_text().splitlines(True)
        if lines.count(line) > 1 or any(str(mount) in s and s != line and not s.lstrip().startswith('#') for s in lines):
            raise ValueError('foreign or duplicate coexistence fstab entry')
        return lines

    def filesystem():
        result = run(['findmnt', '-J', '-M', str(mount), '-o', 'TARGET,SOURCE,FSTYPE,UUID'], check=False)
        if result.returncode == 1: return None
        if result.returncode: raise ValueError('cannot inspect coexistence mount')
        rows = json.loads(result.stdout)['filesystems']
        if len(rows) != 1 or any(rows[0].get(k) != v for k, v in (
                ('target', str(mount)), ('fstype', 'ext4'), ('uuid', fsid))):
            raise ValueError('coexistence mount identity changed')
        loops = json.loads(run(['losetup', '-J', '-l', '-O', 'NAME,BACK-FILE', rows[0]['source']]).stdout)['loopdevices']
        if len(loops) != 1 or loops[0].get('back-file') != str(image):
            raise ValueError('coexistence loop backing file changed')
        return rows[0]

    if action == 'prepare' and not directory.exists():
        if mount.exists() or line in config(): raise ValueError('coexistence path already exists')
        for path in (root/'etc/kubeauto/longhorn-disk.json',
                     root/'etc/systemd/system/kubelet.service.d/kubeauto-longhorn.conf'):
            if path.exists() or path.is_symlink(): raise ValueError('pre-existing V1 host ownership')
        if shutil.disk_usage(root/'var/lib').free < 8*1024**3:
            raise ValueError('coexistence requires 8 GiB actual backing-filesystem headroom')
        base.mkdir(mode=0o700, exist_ok=True); directory.mkdir(mode=0o700)
        save({'id': token, 'uuid': fsid, 'machine_id': machine, 'phase': 'intent'})
    if not record.exists():
        if (action == 'remove' and not directory.exists() and not mount.exists()
                and line not in config() and not filesystem()):
            return {'removed': True, 'never_created': True, 'machine_id': machine, 'uuid': fsid}
        raise ValueError('missing coexistence filesystem ownership')
    row = json.loads(record.read_text())
    if any(row.get(k) != v for k, v in (('id', token), ('uuid', fsid), ('machine_id', machine))):
        raise ValueError('foreign coexistence journal')
    parent = row.get('created_parent')
    if parent:
        info = mount.parent.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or
                parent != {'device': info.st_dev, 'inode': info.st_ino}):
            raise ValueError('coexistence parent directory changed')
    elif row.get('parent_intent') and mount.parent.exists():
        raise ValueError('unconfirmed coexistence parent creation')
    if action == 'prepare':
        if row['phase'] == 'intent':
            # A lost creation response leaves the exact inode in this host journal.
            fd = os.open(image, os.O_CREAT|os.O_EXCL|os.O_RDWR|os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, 'wb') as stream: stream.truncate(request['size']); os.fsync(stream.fileno())
            row.update(file=identity(image), phase='file'); save(row)
        if identity(image) != row['file']: raise ValueError('coexistence file inode changed')
        if row['phase'] == 'file':
            run(['mkfs.ext4', '-F', '-U', fsid, str(image)])
            row['phase'] = 'formatted'; save(row)
        if row['phase'] == 'formatted':
            if mount.exists(): raise ValueError('coexistence mount path appeared')
            if not mount.parent.exists():
                row['parent_intent'] = True; save(row)
                mount.parent.mkdir(mode=0o755)
                info = mount.parent.lstat()
                row['created_parent'] = {'device': info.st_dev, 'inode': info.st_ino}; save(row)
            mount.mkdir(mode=0o755)
            (mount/'.coexist-owner').write_text(token+'\n')
            lines = config()
            if line not in lines:
                with fstab.open('a') as stream: stream.write(line); stream.flush(); os.fsync(stream.fileno())
            row['phase'] = 'mount-intent'; save(row)
        if row['phase'] == 'mount-intent':
            if not filesystem(): run(['mount', str(mount)])
            row['phase'] = 'mounted'; save(row)
    if image.exists() and identity(image) != row.get('file'):
        raise ValueError('coexistence backing file replaced')
    if action in ('check', 'prepare', 'disk-identity'):
        current = filesystem()
        if not current or line not in config() or row['phase'] != 'mounted':
            raise ValueError('coexistence filesystem not persistently mounted')
        result = {'uuid': fsid, 'file': row['file'], 'mount': current, 'kind': 'loop-test-fixture'}
        if action == 'disk-identity':
            filename = mount/'longhorn-disk.cfg'
            if filename.is_symlink() or not filename.is_file():
                raise ValueError('missing or foreign native filesystem identity')
            native = json.loads(filename.read_text())
            result['disk_uuid'] = str(uuid.UUID(native['diskUUID']))
        return result
    # Product host receipts must have been released, and native references must
    # have been proved absent by the controller before this local phase.
    for path in (root/'etc/kubeauto/longhorn-disk.json',
                 root/'etc/systemd/system/kubelet.service.d/kubeauto-longhorn.conf'):
        if path.exists() or path.is_symlink(): raise ValueError('coexistence V1 host receipt remains')
    if filesystem(): run(['umount', str(mount)])
    loops = json.loads(run(['losetup', '-J', '-l', '-j', str(image)]).stdout)['loopdevices'] if image.exists() else []
    if loops: raise ValueError('coexistence backing file retains a loop consumer')
    if mount.exists():
        owner = mount/'.coexist-owner'
        if {p.name for p in mount.iterdir()} != {owner.name} or owner.read_text() != token+'\n':
            raise ValueError('coexistence underlay has foreign data')
        owner.unlink(); mount.rmdir()
    lines = config()
    if line in lines:
        temporary = fstab.with_name('.fstab.coexist46-'+token)
        info = fstab.stat()
        with temporary.open('x') as stream:
            stream.writelines(s for s in lines if s != line); stream.flush(); os.fsync(stream.fileno())
        os.chmod(temporary, stat.S_IMODE(info.st_mode)); os.chown(temporary, info.st_uid, info.st_gid)
        if fstab.read_text().splitlines(True) != lines: raise ValueError('fstab changed during coexistence cleanup')
        temporary.replace(fstab)
    if image.exists(): image.unlink()
    if {p.name for p in directory.iterdir()} != {record.name}:
        raise ValueError('foreign coexistence fixture file remains')
    record.unlink(); directory.rmdir()
    if row.get('created_parent') and not any(mount.parent.iterdir()):
        mount.parent.rmdir()
    return {'removed': True, 'machine_id': machine, 'uuid': fsid}


def desired(original, workers):
    config = yaml.safe_load(original)
    if config.get('longhorn_data_engines') != ['v2'] or config['longhorn_storage_nodes'] or config['longhorn_requests']:
        raise ValueError('coexistence requires the unused V2-only product baseline')
    config.update(longhorn_data_engines=['v1', 'v2'], longhorn_allow_engine_coexistence=True,
                  longhorn_storage_nodes=list(workers), longhorn_data_path=MOUNT)
    storage = copy.deepcopy(config['longhorn_storage_classes'][0])
    storage.update(name='longhorn-coexist-v1', data_engine='v1', replicas=3, reclaim_policy='Retain',
                   default=False, encrypted=False, encryption_secret='', recurring_jobs=[])
    if any(s['name'] == storage['name'] for s in config['longhorn_storage_classes']):
        raise ValueError('coexistence fixture class already declared')
    config['longhorn_storage_classes'].append(storage)
    return config


def load(name):
    spec = importlib.util.spec_from_file_location('coexist46_'+name.replace('-', '_'), Path(__file__).with_name(name))
    module = importlib.util.module_from_spec(spec);sys.modules[spec.name] = module;spec.loader.exec_module(module)
    return module


def host(lab, row, address, action):
    request = row['hosts'][address]
    program = inspect.getsource(host_filesystem)+'\nimport json,sys\nprint(json.dumps(host_filesystem(json.load(sys.stdin),sys.argv[1]),sort_keys=True))'
    result = lab.remote(address, ['python3', '-c', program, action], stdin=json.dumps(request))
    return json.loads(result.stdout)


def native_disks(lab):
    return {n['metadata']['name']: {'uid': n['metadata']['uid'], 'disks': copy.deepcopy(n['spec'].get('disks', {}))}
            for n in lab.get('nodes.longhorn.io', namespace=NS)['items']}


def record(lab):
    path = lab.DIRECTORY/RECORD
    if path.is_symlink(): raise ValueError('symlink coexistence journal')
    row = json.loads(path.read_text())
    if row['run_id'] != lab.owner()['run_id']: raise ValueError('foreign coexistence journal')
    return row


def persist(lab, row):
    lab.write_private(lab.DIRECTORY/RECORD, row)


def capture(lab, row, *, retiring=False):
    """Recover only disks created on this run's independently owned filesystem."""
    for address in row['host_intents']:
        name = 'longhorn-node-'+address.rsplit('.', 1)[1]
        node = lab.get('nodes.longhorn.io', name, NS)
        original = row['native_disks'][name]
        if not node or node['metadata']['uid'] != original['uid']:
            raise ValueError('coexistence original native Node changed')
        current = node['spec'].get('disks', {})
        if any(current.get(k) != v for k, v in original['disks'].items()):
            raise ValueError('coexistence original raw declaration changed')
        added = set(current)-set(original['disks'])
        if added - {'kubeauto-data'}:
            raise ValueError('coexistence found an unowned disk declaration')
        if not added:
            continue
        disk = current['kubeauto-data']
        previous = row['v1_disks'].get(name)
        if (retiring and previous and
                disk == dict(previous['spec'], allowScheduling=False)):
            disk = previous['spec']
        if (disk.get('path') != MOUNT or disk.get('diskType', 'filesystem') != 'filesystem'
                or disk.get('allowScheduling') is not True or disk.get('evictionRequested', False)):
            raise ValueError('coexistence V1 declaration differs from its fixture')
        hardware = host(lab, row, address, 'disk-identity')
        native = node.get('status', {}).get('diskStatus', {}).get('kubeauto-data')
        if not native or native.get('diskUUID') != hardware['disk_uuid']:
            raise ValueError('coexistence native disk UUID differs from its owned filesystem')
        saved = {'node_uid': node['metadata']['uid'], 'name': 'kubeauto-data',
                 'spec': copy.deepcopy(disk), 'uuid': hardware['disk_uuid']}
        if name in row['v1_disks'] and row['v1_disks'][name] != saved:
            raise ValueError('coexistence V1 disk receipt changed')
        row['v1_disks'][name] = saved
        row['disk_uuids']['v1'][address] = saved['uuid']
        persist(lab, row)
    storage = lab.get('storageclass', 'longhorn-coexist-v1')
    if storage:
        if (storage.get('provisioner') != 'driver.longhorn.io'
                or storage['metadata'].get('labels', {}).get('kubeauto.io/component') != 'longhorn'
                or storage.get('parameters', {}).get('dataEngine') != 'v1'
                or storage.get('parameters', {}).get('numberOfReplicas') != '3'
                or storage.get('reclaimPolicy') != 'Retain'):
            raise ValueError('coexistence fixture StorageClass ownership differs')
        uid = storage['metadata']['uid']
        if row.get('class_uid', uid) != uid:
            raise ValueError('coexistence fixture StorageClass was replaced')
        row['class_uid'] = uid
        persist(lab, row)


def apply(lab, row, config, pid):
    path = lab.DIRECTORY/'config.yml'
    current = hashlib.sha256(path.read_bytes()).hexdigest()
    if current not in (row['original_sha256'], row.get('applied_sha256')):
        raise ValueError('coexistence product input changed outside this module')
    content = yaml.safe_dump(config, sort_keys=False).encode()
    row['applied_sha256'] = hashlib.sha256(content).hexdigest();persist(lab, row)
    path.write_bytes(content)
    lab.supervise_core(pid, 'longhorn-setup')


def refusals(lab, row, config, pid):
    """Reject invalid declarations through the same customer setup entry point."""
    cases = []
    invalid = copy.deepcopy(config); invalid['longhorn_allow_engine_coexistence'] = False
    cases.append(('budget', invalid, 'Longhorn dual-engine operation requires explicit resource-budget approval'))
    invalid = yaml.safe_load(base64.b64decode(row['original_b64']))
    invalid['longhorn_storage_classes'].append(copy.deepcopy(config['longhorn_storage_classes'][-1]))
    cases.append(('class-engine', invalid, 'StorageClass data engine is not enabled'))
    invalid = yaml.safe_load(base64.b64decode(row['original_b64']))
    invalid['longhorn_storage_nodes'] = list(lab.WORKERS)
    cases.append(('disabled-engine', invalid, 'disabled V1 engine cannot own filesystem storage nodes'))
    invalid = yaml.safe_load(base64.b64decode(row['original_b64']))
    invalid['longhorn_v2_nodes'] = list(lab.WORKERS[:-1])
    invalid['longhorn_block_disks'].pop(lab.WORKERS[-1])
    cases.append(('failure-domains', invalid, 'Longhorn engine requires independent storage nodes for its replica count: v2'))
    path = lab.DIRECTORY/'config.yml'
    original = base64.b64decode(row['original_b64'], validate=True)
    for label, invalid, reason in cases:
        before = identities(lab)
        content = yaml.safe_dump(invalid, sort_keys=False).encode()
        row['applied_sha256'] = hashlib.sha256(content).hexdigest(); persist(lab, row)
        path.write_bytes(content)
        try:
            lab.supervise_core(pid, 'longhorn-setup', expected_refusal=reason)
        finally:
            if path.read_bytes() != content:
                raise ValueError('coexistence refused input was changed externally')
            path.write_bytes(original)
            row['applied_sha256'] = row['original_sha256']; persist(lab, row)
        if identities(lab) != before:
            raise ValueError('coexistence refusal mutated the installation')
        print('LONGHORN_V2_COEXIST_REFUSAL_VERIFIED case='+label, flush=True)


def components(lab):
    expected = {'longhorn-node-'+a.rsplit('.', 1)[1] for a in lab.WORKERS}
    managers = lab.get('instancemanagers.longhorn.io', namespace=NS)['items']
    for engine in ('v1', 'v2'):
        rows = [m for m in managers if m['spec'].get('dataEngine', 'v1') == engine]
        if len(rows) != 3 or {m['spec']['nodeID'] for m in rows} != expected:
            return None
        for manager in rows:
            if manager.get('status', {}).get('currentState') != 'running': return None
            pod = lab.get('pod', manager['metadata']['name'], NS)
            if not ready(pod): return None
            owners = pod['metadata'].get('ownerReferences', [])
            if (not any(o.get('uid') == manager['metadata']['uid'] and o.get('controller') for o in owners)
                    or pod['spec'].get('nodeName') != manager['spec']['nodeID']
                    or pod['spec'].get('containers', [{}])[0].get('image') != manager['spec']['image']):
                raise ValueError('coexistence Instance Manager Pod ownership, image or placement differs')
    return {m['metadata']['name']: {'uid': m['metadata']['uid'], 'engine': m['spec'].get('dataEngine', 'v1')}
            for m in managers}


def ready(pod):
    return bool(pod and not pod['metadata'].get('deletionTimestamp') and any(
        c.get('type') == 'Ready' and c.get('status') == 'True' for c in pod.get('status', {}).get('conditions', [])))


def volume(lab, row, engine):
    """One independent application and durable object graph per engine."""
    path = lab.DIRECTORY/('longhorn-coexist-business-'+engine+'.json')
    if path.exists() or path.is_symlink(): raise ValueError('previous coexistence business remains')
    namespace = 'longhorn-business-'+secrets.token_hex(8)
    token = secrets.token_hex(16)
    row['business'][engine] = {'namespace': namespace, 'token': token};persist(lab, row)
    ns = json.loads(lab.kubectl('create', '-f', '-', '-o', 'json', stdin=json.dumps({
        'apiVersion': 'v1', 'kind': 'Namespace', 'metadata': {'name': namespace,
        'annotations': {'kubeauto.io/coexist46-token': token}}})).stdout)
    storage = 'longhorn-coexist-v1' if engine == 'v1' else row['v2_class']
    business = {'namespace': namespace, 'namespace_uid': ns['metadata']['uid'], 'objects': {},
                'storage_class': storage, 'reclaim_policy': 'Retain'}
    lab.write_private(path, business)
    business['defaults'] = lab.wait('coexist-namespace-defaults', 60, lambda: lab.namespace_defaults(namespace))
    lab.write_private(path, business)
    lab.create_business_object(business, path, {'apiVersion': 'v1', 'kind': 'PersistentVolumeClaim',
        'metadata': {'name': 'data', 'namespace': namespace}, 'spec': {'accessModes': ['ReadWriteOnce'],
            'storageClassName': storage, 'resources': {'requests': {'storage': '2Gi'}}}})
    def bound():
        claim = lab.get('pvc', 'data', namespace)
        if not claim or claim.get('status', {}).get('phase') != 'Bound': return None
        lab.bind_business_volume(business, path)
        return business if business.get('volume_uid') else None
    lab.wait('coexist-'+engine+'-bound', 600, bound)
    native = lab.get('volumes.longhorn.io', business['volume'], NS)
    if native['spec'].get('dataEngine', 'v1') != engine:
        raise ValueError('coexistence class provisioned the wrong engine')
    client = 'writer'
    lab.create_business_object(business, path, lab.client_pod(namespace, client, 'data',
        'longhorn-node-'+lab.WORKERS[0].rsplit('.', 1)[1]))
    lab.wait('coexist-'+engine+'-writer', 600, lambda: ready(lab.get('pod', client, namespace)))
    payload = row['run_id']+':'+engine
    digest = lab.kubectl('-n', namespace, 'exec', client, '--', 'python3', '-c',
                         lab.payload_program(payload)).stdout.strip()
    if digest != hashlib.sha256(payload.encode()*262144).hexdigest():
        raise ValueError('coexistence fsync acknowledgement differs')
    topology = lab.wait('coexist-'+engine+'-three-replicas', 600, lambda: lab.replica_topology(business, 3))
    disk_ids = {r['spec']['diskID'] for r in lab.get('replicas.longhorn.io', namespace=NS)['items']
                if r['spec'].get('volumeName') == business['volume']}
    expected = row['disk_uuids'][engine]
    if disk_ids != set(expected.values()): raise ValueError('coexistence replicas use an unexpected disk type or UUID')
    row['business'][engine].update(volume_uid=business['volume_uid'], payload=payload, digest=digest,
                                   topology=topology, client=client)
    persist(lab, row)
    return business


def readback(lab, row, engine, *, remount=False):
    saved = row['business'][engine]
    path = lab.DIRECTORY/('longhorn-coexist-business-'+engine+'.json')
    business = json.loads(path.read_text())
    lab.check_business_graph(business)
    native = lab.get('volumes.longhorn.io', business['volume'], NS)
    if native['metadata']['uid'] != saved['volume_uid'] or native['spec'].get('dataEngine', 'v1') != engine:
        raise ValueError('coexistence volume identity or engine changed')
    if remount:
        old = lab.get('pod', saved['client'], business['namespace'])
        if old['metadata']['uid'] != business['objects']['Pod/'+saved['client']]:
            raise ValueError('coexistence client was replaced')
        lab.remove(old)
        lab.wait('coexist-client-retired', 180, lambda: lab.get('pod', saved['client'], business['namespace']) is None)
        lab.wait('coexist-volume-detached', 600, lambda:
            lab.get('volumes.longhorn.io', business['volume'], NS)['status'].get('state') == 'detached')
        saved['client'] = 'reader';persist(lab, row)
        lab.create_business_object(business, path, lab.client_pod(business['namespace'], 'reader', 'data',
            'longhorn-node-'+lab.WORKERS[1].rsplit('.', 1)[1]))
        lab.wait('coexist-reader-ready', 600, lambda: ready(lab.get('pod', 'reader', business['namespace'])))
    digest = lab.kubectl('-n', business['namespace'], 'exec', saved['client'], '--', 'python3', '-c',
        lab.payload_program(saved['payload'], read_only=True)).stdout.strip()
    if digest != saved['digest']: raise ValueError('coexistence original acknowledged data changed')
    if not lab.wait('coexist-original-replicas', 600, lambda: lab.replica_topology(business, 3)):
        raise ValueError('coexistence replica health did not recover')
    return {'engine': engine, 'volume_uid': saved['volume_uid'], 'sha256': digest, 'cross_node': remount}


def identities(lab):
    release = json.loads(lab.execute([str(lab.BASE/'extra-bin/helm'), 'list', '-n', NS, '-o', 'json']).stdout)
    pods = lab.get('pods', namespace=NS)['items']
    return {'helm': release, 'pods': {p['metadata']['name']: p['metadata']['uid'] for p in pods},
            'classes': lab.class_identity(), 'native_disks': native_disks(lab), 'managers': components(lab)}


def restore_business(lab, row, pid):
    for engine in ('v1', 'v2'):
        saved = row['business'].get(engine)
        if not saved: continue
        path = lab.DIRECTORY/('longhorn-coexist-business-'+engine+'.json')
        if not path.exists():
            namespace = lab.get('namespace', saved['namespace'])
            if namespace:
                if namespace['metadata'].get('annotations', {}).get('kubeauto.io/coexist46-token') != saved['token']:
                    raise ValueError('coexistence namespace creation intent differs')
                lab.write_private(path, {'namespace': saved['namespace'], 'namespace_uid': namespace['metadata']['uid'],
                    'objects': {}, 'defaults': lab.wait('coexist-recovery-defaults', 60,
                        lambda: lab.namespace_defaults(saved['namespace']))})
        if path.exists():
            # No component/host fault is injected in this module. Restore the
            # exact consumer before deleting fixtures if its data was confirmed.
            if saved.get('digest'):
                business = json.loads(path.read_text())
                client = lab.get('pod', saved['client'], saved['namespace'])
                if not client:
                    lab.create_business_object(business, path, lab.client_pod(saved['namespace'], saved['client'], 'data',
                        'longhorn-node-'+lab.WORKERS[1].rsplit('.', 1)[1]))
                lab.wait('coexist-recovery-client-ready', 600, lambda: ready(lab.get('pod', saved['client'], saved['namespace'])))
                readback(lab, row, engine)
            lab.cleanup_business(path, relay_pid=pid)


def retire_empty_disk(lab, name, saved):
    """The native validator requires a separate disable before disk removal."""
    disk_name = saved.get('name', 'kubeauto-data')
    disabled = dict(saved['spec'], allowScheduling=False)
    for remove in (False, True):
        for attempt in range(3):
            node = lab.get('nodes.longhorn.io', name, NS)
            if not node or node['metadata']['uid'] != saved['node_uid']:
                raise ValueError('recovery native Node replaced before disk retirement')
            disk = node['spec'].get('disks', {}).get(disk_name)
            if disk is None: break
            status = node.get('status', {}).get('diskStatus', {}).get(disk_name, {})
            if (disk not in (saved['spec'], disabled) or status.get('diskUUID') != saved['uuid'] or
                    any(status.get(k) for k in ('storageScheduled', 'scheduledReplica', 'scheduledBackingImage'))):
                raise ValueError('recovery filesystem disk changed or still has scheduled data')
            if any(r['spec'].get('diskID') == saved['uuid'] for r in lab.get('replicas.longhorn.io', namespace=NS)['items']):
                raise ValueError('recovery filesystem disk still has replica references')
            if not remove and disk == disabled: break
            patch = [{'op': 'test', 'path': '/metadata/uid', 'value': saved['node_uid']},
                {'op': 'test', 'path': '/metadata/resourceVersion', 'value': node['metadata']['resourceVersion']},
                {'op': 'test', 'path': '/spec/disks/'+disk_name, 'value': disk}]
            if remove:
                if disk != disabled: raise ValueError('recovery disk scheduling was re-enabled')
                patch.append({'op': 'remove', 'path': '/spec/disks/'+disk_name})
            else:
                patch.append({'op': 'replace', 'path': '/spec/disks/'+disk_name+'/allowScheduling', 'value': False})
            try:
                lab.kubectl('-n', NS, 'patch', 'nodes.longhorn.io', name, '--type=json',
                            '--patch-file=/dev/stdin', stdin=json.dumps(patch))
                break
            except ValueError as error:
                # Status reconciliation also advances resourceVersion. Retry
                # only a proven stale read, then repeat every ownership/data guard.
                if not str(error).startswith('fixture command failed: kubectl rc='):
                    raise
                current = lab.get('nodes.longhorn.io', name, NS)
                if (not current or attempt == 2 or current['metadata']['resourceVersion'] ==
                        node['metadata']['resourceVersion']):
                    raise


def clean(lab, pid):
    path = lab.DIRECTORY/RECORD
    if not path.exists(): return
    row = record(lab)
    restore_business(lab, row, pid)
    if lab.get('volumes.longhorn.io', namespace=NS)['items']:
        raise ValueError('coexistence cleanup found another volume consumer')
    capture(lab, row, retiring=True)
    for manager in lab.get('instancemanagers.longhorn.io', namespace=NS)['items']:
        if manager['spec'].get('dataEngine', 'v1') == 'v1' and any(
                manager.get('status', {}).get(key) for key in ('instances', 'instanceEngines', 'instanceReplicas')):
            raise ValueError('coexistence V1 Instance Manager still has native consumers')
    for name, saved in row.get('v1_disks', {}).items():
        node = lab.get('nodes.longhorn.io', name, NS)
        if not node or node['metadata']['uid'] != saved['node_uid']:
            raise ValueError('coexistence native Node changed')
        disk = node['spec'].get('disks', {}).get(saved['name'])
        status = node.get('status', {}).get('diskStatus', {}).get(saved['name'])
        if disk:
            if disk not in (saved['spec'], dict(saved['spec'], allowScheduling=False)) or not status or status.get('diskUUID') != saved['uuid']:
                raise ValueError('coexistence V1 disk identity changed before cleanup')
            if any(r['spec'].get('diskID') == saved['uuid'] for r in lab.get('replicas.longhorn.io', namespace=NS)['items']):
                raise ValueError('coexistence V1 replica reference remains')
            if any(status.get(k) for k in ('storageScheduled', 'scheduledReplica', 'scheduledBackingImage')):
                raise ValueError('coexistence V1 disk still has scheduled data')
            retire_empty_disk(lab, name, saved)
        lab.wait('coexist-native-v1-disk-retired', 180, lambda:
            saved['name'] not in lab.get('nodes.longhorn.io', name, NS).get('status', {}).get('diskStatus', {}))
    original = base64.b64decode(row['original_b64'], validate=True)
    if hashlib.sha256(original).hexdigest() != row['original_sha256']:
        raise ValueError('coexistence original input checksum differs')
    if row.get('applied_sha256'):
        # Native empty V1 disk removal precedes disabling that engine.
        apply(lab, row, yaml.safe_load(original), pid)
        (lab.DIRECTORY/'config.yml').write_bytes(original)
        row['applied_sha256'] = row['original_sha256'];persist(lab, row)
    storage = lab.get('storageclass', 'longhorn-coexist-v1')
    if storage:
        if storage['metadata']['uid'] != row.get('class_uid'):
            raise ValueError('coexistence fixture StorageClass changed')
        lab.remove(storage)
    state = lab.owner()
    for phase in ('check', 'apply'):
        for address in row.get('host_intents', []):
            declaration = ['--release', phase, '--data-path', MOUNT, '--cluster-uid', state['cluster_uid'],
                '--node-uid', state['node_uids']['longhorn-node-'+address.rsplit('.', 1)[1]]]
            lab.remote(address, ['python3', '/usr/local/sbin/kubeauto-longhorn-host-check', *declaration])
    for address in row.get('host_intents', []): host(lab, row, address, 'remove')
    if native_disks(lab) != row['native_disks']:
        raise ValueError('coexistence changed original raw disk declarations')
    load('longhorn-v2-install.py').health(lab)
    row['clean'] = True;persist(lab, row)
    print('LONGHORN_V2_COEXIST_FIXTURES_CLEAN_PASS', flush=True)


def business(lab, pid):
    install = load('longhorn-v2-install.py');install.volume_baseline(lab)
    canonical_before=load('longhorn-v2-platform-install.py').snapshot(lab)
    path = lab.DIRECTORY/RECORD
    if path.exists() or path.is_symlink(): raise ValueError('previous coexistence requires recovery')
    original = (lab.DIRECTORY/'config.yml').read_bytes()
    config = desired(original, lab.WORKERS)
    if lab.get('storageclass', 'longhorn-coexist-v1'):
        raise ValueError('coexistence fixture StorageClass already exists')
    state = lab.owner()
    classes = [s['name'] for s in yaml.safe_load(original)['longhorn_storage_classes']
               if s.get('data_engine') == 'v2' and s['replicas'] == 3 and s['reclaim_policy'] == 'Retain'
               and not s.get('encrypted')]
    if not classes: raise ValueError('coexistence has no original three-replica V2 class')
    block = json.loads((lab.DIRECTORY/'yml/longhorn/block-disks.json').read_text())
    v2_uuids = {a: next(iter(v['disks'].values()))['disk_uuid'] for a, v in block['nodes'].items()}
    row = {'run_id': state['run_id'], 'original_b64': base64.b64encode(original).decode(),
           'original_sha256': hashlib.sha256(original).hexdigest(), 'native_disks': native_disks(lab),
           'hosts': {a: {'id': str(uuid.uuid4()), 'uuid': str(uuid.uuid4()),
                         'machine_id': state['raw_disks'][a]['machine_id'], 'size': SIZE} for a in lab.WORKERS},
           'host_intents': [], 'business': {}, 'v2_class': classes[0], 'disk_uuids': {'v2': v2_uuids, 'v1': {}},
           'v1_disks': {}, 'passed': False, 'clean': False}
    persist(lab, row)
    with lab.scoped_cleanup(lambda: clean(lab, pid)):
        refusals(lab, row, config, pid)
        for address in lab.WORKERS:
            row['host_intents'].append(address);persist(lab, row)
            host(lab, row, address, 'prepare')
        apply(lab, row, config, pid)
        managers = lab.wait('coexist-six-instance-managers', 600, lambda: components(lab))
        capture(lab, row)
        if len(row['v1_disks']) != 3 or not row.get('class_uid'):
            raise ValueError('coexistence product did not converge its three V1 disks and class')
        for engine in ('v1', 'v2'): volume(lab, row, engine)
        before = identities(lab)
        lab.supervise_core(pid, 'longhorn-setup')
        after = identities(lab)
        if after != before: raise ValueError('dual-engine idempotence changed Helm, Pods, classes or disk identities')
        results = [readback(lab, row, engine, remount=True) for engine in ('v1', 'v2')]
        row['passed'] = True;persist(lab, row)
    final = record(lab)
    if not final['clean'] or not final['passed']: raise ValueError('coexistence business or cleanup incomplete')
    canonical_after=load('longhorn-v2-platform-install.py').snapshot(lab)
    if canonical_before!=canonical_after:raise ValueError('coexistence canonical baseline changed after cleanup')
    print('LONGHORN_V2_COEXIST_RESULT '+json.dumps({'managers': managers,
            'disk_uuids': row['disk_uuids'], 'business': results, 'idempotent': True,
            'v1_media': 'bounded-loop-files-on-three-independent-hosts',
            'baseline_before': canonical_before, 'baseline_after': canonical_after}, sort_keys=True), flush=True)
    path.unlink()
    print('LONGHORN_V2_COEXIST_BUSINESS_PASS', flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('business', 'clean'))
    parser.add_argument('--relay-pid', type=int, required=True)
    args = parser.parse_args()
    install = load('longhorn-v2-install.py');_, lab = install.fixture();lab.require_lease()
    if args.action == 'business': business(lab, args.relay_pid)
    else:
        clean(lab, args.relay_pid)
        (lab.DIRECTORY/RECORD).unlink()


if __name__ == '__main__':
    def interrupted(signum, frame): raise SystemExit(128+signum)
    signal.signal(signal.SIGINT, interrupted);signal.signal(signal.SIGTERM, interrupted)
    main()
