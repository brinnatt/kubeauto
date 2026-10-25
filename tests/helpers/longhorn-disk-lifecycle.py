#!/usr/bin/env python3
"""LONGHORN-26: native V1 extra-disk lifecycle around config+kubecli.

Official 1.12.1 multidisk/eviction/backing-image APIs; native administrator
managed extra disks are deliberately preserved by the product reconciler.
The parent runner owns the six-host lease, transport and baseline checks.
"""
import argparse
import copy
import importlib.util
import json
from pathlib import Path
import sys
import uuid

NS = 'longhorn-system'
RECORD = 'longhorn-disk-lifecycle.json'
EXTRA = '/data/kubeauto-longhorn-extra26'
UNIT = '/etc/systemd/system/kubelet.service.d/kubeauto-longhorn-extra26.conf'


def load(filename):
    path = Path(__file__).with_name(filename)
    spec = importlib.util.spec_from_file_location('disk26_'+filename.replace('-', '_'), path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def plan(lab, raw):
    """Select the two unused physical filesystems from the qualified 18 disks."""
    state = lab.owner(); lab.disk_plan(raw, state['run_id'])  # validate full qualification shape
    node = lab.WORKERS[0]; primary = state['disks'][node]
    rows = [line.split('|') for line in raw.splitlines() if line]
    selected = sorted((r for r in rows if r[0] == node and r[2] != primary['device']), key=lambda r: r[2])
    if len(selected) != 2 or len({r[2] for r in selected}) != 2:
        raise ValueError('extra disk lifecycle requires two independent unused qualified disks')
    output = []
    for i, row in enumerate(selected):
        if not row[2].startswith('/dev/disk/by-') or not (row[5].startswith('path:pci-') or row[3] or row[4]):
            raise ValueError('extra disk lacks stable qualification identity')
        if int(row[6]) < 20*1024**3: raise ValueError('extra disk is too small')
        disk_uuid = str(uuid.uuid5(uuid.UUID(state['run_id']), 'disk26:'+node+':'+str(i)))
        output.append({'node': node, 'kernel': row[1], 'device': row[2], 'identity': row[5],
            'size': int(row[6]), 'uuid': disk_uuid, 'path': EXTRA,
            'name': 'kubeauto-extra26-'+str(i), 'phase': 'planned',
            'fstab_line': 'UUID='+disk_uuid+' '+EXTRA+' ext4 defaults 0 2 # kubeauto-longhorn-extra26:'+state['run_id']})
    if len({r['device'] for r in output} | {primary['device']}) != 3:
        raise ValueError('extra disk aliases the existing default disk')
    return output


def host_script():
    """Bounded host fixture, independent of the product default-disk receipt."""
    return r'''import hashlib,json,os,pathlib,subprocess,sys
r=json.load(sys.stdin);action=sys.argv[1];p=pathlib.Path(r['path'])
assert str(p)=='/data/kubeauto-longhorn-extra26', 'foreign extra mount'
u=pathlib.Path('/etc/systemd/system/kubelet.service.d/kubeauto-longhorn-extra26.conf')
f=pathlib.Path('/etc/fstab');m=p/'.kubeauto-extra26-owner'
assert not any(x.is_symlink() for x in (p,p.parent,u,f,m)), 'symlink extra fixture'
unit='[Unit]\nRequiresMountsFor='+str(p)+'\n[Service]\nExecStartPre=/usr/bin/mountpoint -q -- '+str(p)+'\n'
def run(*a,check=True):return subprocess.run(a,text=True,capture_output=True,check=check)
def mounted():
 x=run('findmnt','-J','--mountpoint',str(p),'-o','TARGET,UUID,FSTYPE',check=False)
 if x.returncode==1:return False
 assert x.returncode==0 and json.loads(x.stdout)['filesystems']==[{'target':str(p),'uuid':r['uuid'],'fstype':'ext4'}], 'extra mount identity changed'
 return True
def underlay():
 assert p.is_dir() and not m.is_symlink() and m.read_text()==r['uuid']+'\n', 'extra underlay ownership changed'
 assert {x.name for x in p.iterdir()}=={m.name}, 'foreign extra underlay file'
def configs(require=False):
 lines=f.read_text().splitlines(True);owned=r['fstab_line']+'\n'
 assert sum(x==owned for x in lines)<=1 and not any(str(p) in x and not x.lstrip().startswith('#') and x!=owned for x in lines), 'foreign extra fstab entry'
 assert not u.exists() or u.read_text()==unit, 'foreign extra kubelet guard'
 if require:assert u.exists() and owned in lines, 'missing extra boot protection'
 return lines,owned
if action=='blank':
 assert not p.exists() and not u.exists(), 'extra path or unit already exists'
 configs();assert not mounted();sys.exit(0)
if action=='mount':
 assert not p.exists() and not u.exists(), 'extra fixture appeared before mount'
 lines,owned=configs();assert owned not in lines
 p.mkdir(mode=0o755);m.write_text(r['uuid']+'\n')
 with f.open('a') as stream:stream.write(owned);stream.flush();os.fsync(stream.fileno())
 u.write_text(unit);run('systemctl','daemon-reload');run('mount',str(p));assert mounted()
elif action=='check':
 assert mounted();configs(True)
elif action=='clean':
 lines,owned=configs()
 if mounted():
  run('fuser','-m',str(p),check=False)
  run('umount',str(p))  # busy mounts must fail, never lazy/force unmount
 if p.exists():underlay()
 if u.exists():u.unlink()
 if owned in lines:
  t=f.with_name('.fstab.kubeauto-extra26.part')
  with t.open('x') as stream:
   stream.writelines(x for x in lines if x!=owned);stream.flush();os.fsync(stream.fileno())
  os.chmod(t,f.stat().st_mode & 0o777);os.chown(t,f.stat().st_uid,f.stat().st_gid);t.replace(f)
 if p.exists():m.unlink();p.rmdir()
 run('systemctl','daemon-reload')
 assert not p.exists() and not u.exists() and owned not in f.read_text().splitlines(True)
else:raise ValueError('unknown extra fixture action')
'''


def check_device(lab, row, *, blank=False):
    rows = json.loads(lab.remote(row['node'], ['lsblk', '-J', '-b', '-O', row['device']]).stdout)['blockdevices']
    if len(rows) != 1: raise ValueError('ambiguous extra disk device')
    probe = rows[0]; lab.verify_device(row, probe)
    if row['identity'].startswith('path:'):
        props = lab.remote(row['node'], ['udevadm', 'info', '--query=property', '--name='+row['device']]).stdout
        if 'ID_PATH='+row['identity'][5:] not in props.splitlines(): raise ValueError('extra disk PCI identity changed')
    mounts = [m for m in probe.get('mountpoints', []) or [] if m]
    if blank:
        if probe.get('fstype') or mounts: raise ValueError('extra disk is not blank')
        signatures = json.loads(lab.remote(row['node'], ['wipefs', '--no-act', '--json', row['device']]).stdout)
        if signatures.get('signatures'): raise ValueError('extra disk has a foreign signature')
    elif probe.get('uuid') not in ('', None, row['uuid']) or mounts not in ([], [EXTRA]):
        raise ValueError('extra disk filesystem identity or consumers changed')
    return probe


def patch(lab, obj, spec, *, dry=False):
    args = ['-n', NS, 'patch', obj['kind'].lower()+'.longhorn.io', obj['metadata']['name'],
        '--type=json', '--patch-file=/dev/stdin', '-o', 'json']
    if dry: args.append('--dry-run=server')
    body = [{'op': 'test', 'path': '/metadata/uid', 'value': obj['metadata']['uid']},
            {'op': 'test', 'path': '/metadata/resourceVersion', 'value': obj['metadata']['resourceVersion']},
            {'op': 'test', 'path': '/spec', 'value': obj['spec']},
            {'op': 'replace', 'path': '/spec', 'value': spec}]
    return lab.kubectl(*args, stdin=json.dumps(body), check=not dry)


def condition(status, kind):
    return next((c for c in status.get('conditions', []) if c.get('type') == kind), {})


class Lifecycle:
    def __init__(self, lab, workload, pid):
        self.lab = lab; self.workload = workload; self.pid = pid
        self.path = lab.DIRECTORY/RECORD; self.row = None

    def save(self): self.lab.write_private(self.path, self.row)

    def node(self):
        obj = self.lab.get('nodes.longhorn.io', self.row['node'], NS)
        if not obj or obj['metadata']['uid'] != self.row['node_uid']:
            raise ValueError('disk lifecycle native Node identity changed')
        return obj

    def disks(self, transform):
        obj = self.node(); spec = copy.deepcopy(obj['spec']); transform(spec['disks'])
        patch(self.lab, obj, spec)

    def original(self):
        obj = self.node(); expected = self.row['original_disks']
        for key, saved in expected.items():
            if obj['spec']['disks'].get(key) != saved: raise ValueError('original disk declaration changed')

    def status(self, disk):
        obj = self.node(); spec = obj['spec']['disks'].get(disk['name'])
        if spec != self.declaration(disk): raise ValueError('owned extra disk declaration changed')
        status = obj.get('status', {}).get('diskStatus', {}).get(disk['name'], {})
        if condition(status, 'Ready').get('status') != 'True' or not status.get('diskUUID'): return None
        if disk.get('disk_uuid') and status['diskUUID'] != disk['disk_uuid']:
            raise ValueError('owned Longhorn disk UUID changed')
        return status

    @staticmethod
    def declaration(disk):
        return {'allowScheduling': disk.get('allow', True), 'evictionRequested': disk.get('evict', False),
                'diskType': 'filesystem', 'diskDriver': '', 'path': disk['path'],
                'storageReserved': 1024**3, 'tags': ['kubeauto-extra26']}

    def flags(self, name, allow, evict):
        def edit(disks):
            if name not in disks: raise ValueError('missing disk before maintenance flags')
            disks[name].update(allowScheduling=allow, evictionRequested=evict)
        self.disks(edit)

    def restore_main(self):
        obj = self.node(); name = self.row['main']; saved = self.row['original_disks'][name]
        current = obj['spec']['disks'].get(name)
        if not current or {k:v for k,v in current.items() if k not in ('allowScheduling','evictionRequested')} != {
                k:v for k,v in saved.items() if k not in ('allowScheduling','evictionRequested')}:
            raise ValueError('original main disk identity changed during maintenance')
        if current == saved:
            self.original(); return
        self.flags(name, False, False)
        self.flags(name, saved['allowScheduling'], saved['evictionRequested'])
        self.original()

    def ready_placement(self):
        row = self.workload.journal(); lab = self.lab
        topology = lab.replica_topology(row, 3)
        if not topology: return None
        rows = {name: lab.get('replicas.longhorn.io', name, NS) for name in topology}
        return {name: {'uid': obj['metadata']['uid'], 'node': obj['spec']['nodeID'], 'disk': obj['spec']['diskID']}
                for name, obj in rows.items()}

    def prepare_disk(self, disk):
        lab = self.lab; check_device(lab, disk, blank=True)
        lab.remote(disk['node'], ['python3', '-c', host_script(), 'blank'], stdin=json.dumps(disk))
        disk['phase'] = 'formatting'; self.save()
        lab.remote(disk['node'], ['mkfs.ext4', '-F', '-U', disk['uuid'], '-L', 'kca-extra26', disk['device']], timeout=180)
        disk['phase'] = 'formatted'; self.save()
        lab.remote(disk['node'], ['python3', '-c', host_script(), 'mount'], stdin=json.dumps(disk))
        disk['phase'] = 'mounted'; self.save()
        self.disks(lambda disks: disks.update({disk['name']: self.declaration(disk)}))
        disk['phase'] = 'declared'; self.save()
        status = lab.wait('extra-disk-ready', 600, lambda: self.status(disk))
        disk['disk_uuid'] = status['diskUUID']; self.save()
        lab.remote(disk['node'], ['python3', '-c', host_script(), 'check'], stdin=json.dumps(disk))
        before = copy.deepcopy(self.node()['spec']['disks'])
        # Product reconciliation must preserve the official administrator API.
        lab.run_requests(self.pid, [])
        if self.node()['spec']['disks'] != before: raise ValueError('product reconciliation rewrote native extra disks')
        self.original()

    def duplicate(self, disk):
        lab = self.lab; name = 'kubeauto-extra26-duplicate'; path = EXTRA+'/duplicate'
        lab.remote(disk['node'], ['mkdir', '-m', '0755', path])
        declaration = self.declaration(disk); declaration.update(path=path, allowScheduling=False)
        self.row['duplicate'] = {'name': name, 'declaration': declaration}; self.save()
        self.disks(lambda disks: disks.update({name: declaration}))
        def rejected():
            obj = self.node()
            if obj['spec']['disks'].get(name) != declaration: raise ValueError('duplicate disk declaration replaced')
            status = obj.get('status', {}).get('diskStatus', {}).get(name, {})
            ready = condition(status, 'Ready')
            if (ready.get('status') == 'False' and ready.get('reason') == 'DiskFilesystemChanged' and
                    'same file system ID' in ready.get('message', '') and not status.get('storageScheduled')):
                if any(r['spec'].get('diskPath') == path for r in lab.get('replicas.longhorn.io', namespace=NS)['items']):
                    raise ValueError('duplicate filesystem unexpectedly received a replica')
                return ready
        result = lab.wait('duplicate-filesystem-native-refusal', 180, rejected)
        self.row['duplicate']['refusal'] = result; self.save()
        self.remove_duplicate()
        print('LONGHORN_DISK_DUPLICATE_FILESYSTEM_REFUSAL '+json.dumps(result, sort_keys=True), flush=True)

    def remove_duplicate(self):
        duplicate = self.row.get('duplicate')
        if not duplicate: return
        obj = self.node(); name = duplicate['name']; current = obj['spec']['disks'].get(name)
        if current:
            if current != duplicate['declaration']: raise ValueError('duplicate filesystem declaration changed')
            self.disks(lambda disks: disks.pop(name))
        self.lab.wait('duplicate-disk-status-retired', 180, lambda:
                      name not in self.node().get('status', {}).get('diskStatus', {}))
        # Controller may have created disk.cfg on this owned filesystem. It is
        # never recursively removed; the whole independently owned FS retires.
        duplicate['retired'] = True; self.save()

    def backing(self, disk):
        lab = self.lab; source = self.workload.journal(); name = 'disk26-image-'+self.row['id'][:12]
        self.row['backing'] = {'name': name, 'uid': None}; self.save()
        obj = {'apiVersion': 'longhorn.io/v1beta2', 'kind': 'BackingImage',
            'metadata': {'name': name, 'namespace': NS}, 'spec': {'dataEngine': 'v1',
                'sourceType': 'export-from-volume', 'sourceParameters': {'volume-name': source['volume'], 'export-type': 'raw'},
                'minNumberOfCopies': 1, 'diskSelector': ['kubeauto-extra26'], 'nodeSelector': [],
                'diskFileSpecMap': {disk['disk_uuid']: {'dataEngine': 'v1', 'evictionRequested': False}}}}
        created = json.loads(lab.kubectl('create', '-f', '-', '-o', 'json', stdin=json.dumps(obj)).stdout)
        self.row['backing']['uid'] = created['metadata']['uid']; self.save()
        def ready():
            image = self.image(); status = image.get('status', {})
            on_disk = status.get('diskFileStatusMap', {}).get(disk['disk_uuid'], {})
            checksum = status.get('checksum', '')
            return (status if on_disk.get('state') == 'ready' and on_disk.get('progress') == 100 and
                    len(checksum) == 128 and all(c in '0123456789abcdef' for c in checksum) and status.get('size', 0) > 0 else None)
        self.row['backing']['ready'] = lab.wait('extra-backing-image-source-ready', 1200, ready); self.save()
        obj = self.image(); spec = copy.deepcopy(obj['spec']); spec['diskSelector'] = []
        patch(lab, obj, spec)

    def image(self):
        row = self.row['backing']; obj = self.lab.get('backingimages.longhorn.io', row['name'], NS)
        if not obj or not row['uid'] or obj['metadata']['uid'] != row['uid']:
            raise ValueError('backing image creation or original identity is unconfirmed')
        return obj

    def remove_image(self):
        row = self.row.get('backing')
        if not row: return
        if not row['uid']: raise ValueError('backing image creation identity is unconfirmed')
        obj = self.lab.get('backingimages.longhorn.io', row['name'], NS)
        if obj:
            obj = self.image()
            if any(v['spec'].get('backingImage') == row['name'] for v in self.lab.get('volumes.longhorn.io', namespace=NS)['items']):
                raise ValueError('backing image acquired a foreign volume consumer')
            self.lab.remove(obj)
        self.lab.wait('extra-backing-image-retired', 600, lambda:
                      self.lab.get('backingimages.longhorn.io', row['name'], NS) is None)
        for kind in ('backingimagemanagers.longhorn.io', 'backingimagedatasources.longhorn.io'):
            self.lab.wait('extra-backing-image-process-retired', 600, lambda kind=kind:
                not any(row['name'] in obj.get('spec', {}).get('backingImages', {}) or
                        obj['metadata']['name'] == row['name'] for obj in self.lab.get(kind, namespace=NS)['items']))
        row['retired'] = True; self.save()

    def transferred_source(self, obj):
        """A completed BIDS keeps its historical disk after its Pod is gone.

        Only recognize our own original export. Physical retirement still
        removes this owned image and its BIDS before the strict empty check.
        """
        image = self.row.get('backing', {})
        references = obj.get('metadata', {}).get('ownerReferences', [])
        if (not image.get('uid') or obj.get('metadata', {}).get('name') != image['name'] or
                not any(r.get('kind') == 'BackingImage' and r.get('uid') == image['uid']
                        for r in references) or obj.get('spec', {}).get('fileTransferred') is not True or
                obj['spec'].get('sourceType') != 'export-from-volume' or
                obj['spec'].get('parameters', {}).get('volume-name') != self.workload.journal()['volume']):
            return False
        return not any(any(r.get('uid') == obj['metadata']['uid'] for r in p.get('metadata', {}).get('ownerReferences', []))
                       for p in self.lab.get('pods', namespace=NS)['items'])

    def empty(self, disk, *, transferred_source=False):
        obj = self.node(); status = obj.get('status', {}).get('diskStatus', {}).get(disk['name'], {})
        if disk['name'] in obj['spec']['disks']:
            declaration = obj['spec']['disks'][disk['name']]
            if declaration.get('allowScheduling') or not declaration.get('evictionRequested'): return False
        if any(status.get(k) for k in ('storageScheduled', 'scheduledReplica', 'scheduledBackingImage')): return False
        lab = self.lab; disk_id = disk.get('disk_uuid')
        for kind, key in (('replicas.longhorn.io', 'diskID'), ('backingimagemanagers.longhorn.io', 'diskUUID'),
                          ('backingimagedatasources.longhorn.io', 'diskUUID')):
            for obj in lab.get(kind, namespace=NS)['items']:
                if ((disk_id and obj.get('spec', {}).get(key) == disk_id) or
                        (obj.get('spec', {}).get('nodeID') == self.row['node'] and
                         obj.get('spec', {}).get('diskPath') == disk['path'])):
                    if (kind == 'backingimagedatasources.longhorn.io' and transferred_source and
                            self.transferred_source(obj)): continue
                    return False
        if disk_id and any(disk_id in o.get('spec', {}).get('diskFileSpecMap', {}) or
                           disk_id in o.get('status', {}).get('diskFileStatusMap', {})
               for o in lab.get('backingimages.longhorn.io', namespace=NS)['items']): return False
        return True

    def retire_disk(self, disk):
        lab = self.lab; obj = self.node()
        if disk['name'] in obj['spec']['disks']:
            current = obj['spec']['disks'][disk['name']]
            expected = self.declaration(disk)
            stable = lambda values: {key:value for key,value in values.items()
                                    if key not in ('allowScheduling', 'evictionRequested')}
            if (stable(current) != stable(expected) or
                    (current.get('allowScheduling'), current.get('evictionRequested')) not in
                    ((True, False), (False, False), (False, True))):
                raise ValueError('extra disk replaced before retirement')
            status = obj.get('status', {}).get('diskStatus', {}).get(disk['name'], {})
            if status.get('diskUUID'):
                if disk.get('disk_uuid') and disk['disk_uuid'] != status['diskUUID']:
                    raise ValueError('extra Longhorn disk identity changed before retirement')
                disk['disk_uuid'] = status['diskUUID']; self.save()
            self.flags(disk['name'], False, True); disk.update(allow=False, evict=True); self.save()
            lab.wait('extra-disk-native-evacuation', 1800, lambda: self.empty(disk))
            self.disks(lambda disks: disks.pop(disk['name']))
        lab.wait('extra-disk-native-status-retired', 180, lambda:
                    disk['name'] not in self.node().get('status', {}).get('diskStatus', {}))
        if not self.empty(disk): raise ValueError('disk acquired consumers after native retirement')
        check_device(lab, disk)
        lab.remote(disk['node'], ['python3', '-c', host_script(), 'clean'], stdin=json.dumps(disk))
        probe = check_device(lab, disk)
        if probe.get('uuid'):
            if probe['uuid'] != disk['uuid']: raise ValueError('extra filesystem replaced immediately before wipe')
            lab.remote(disk['node'], ['wipefs', '--all', '--force', disk['device']])
        check_device(lab, disk, blank=True)
        disk['phase'] = 'retired'; self.save()

    def business(self, allowlist):
        lab = self.lab; lab.installation_health()
        if self.path.exists() or self.path.is_symlink(): raise ValueError('prior disk lifecycle requires scoped recovery')
        for kind in ('volumes.longhorn.io', 'backingimages.longhorn.io'):
            if lab.get(kind, namespace=NS)['items']: raise ValueError('disk lifecycle requires an empty owned storage baseline')
        disks = plan(lab, Path(allowlist).read_text()); name = 'longhorn-node-'+disks[0]['node'].rsplit('.', 1)[1]
        node = lab.get('nodes.longhorn.io', name, NS)
        mains = [key for key, value in node['spec']['disks'].items() if value['path'] == lab.DATA]
        if len(mains) != 1: raise ValueError('disk lifecycle requires its unique original default disk')
        for disk in disks: check_device(lab, disk, blank=True)
        self.row = {'id': uuid.uuid4().hex, 'run_id': lab.owner()['run_id'], 'node': name,
            'node_uid': node['metadata']['uid'], 'main': mains[0], 'original_disks': copy.deepcopy(node['spec']['disks']),
            'disks': disks, 'payload': lab.owner()['run_id']+':disk26'}
        self.save()
        with lab.scoped_cleanup(self.clean):
            self.workload.prepare(); self.workload.data(self.row['payload'])
            before = lab.wait('disk-lifecycle-source-healthy', 600, self.ready_placement)
            if any(p['disk'] == disks[0].get('disk_uuid') for p in before.values()):
                raise ValueError('source unexpectedly used a not-yet-admitted extra disk')
            self.row['before'] = before; self.save()
            self.prepare_disk(disks[0]); self.duplicate(disks[0])
            self.flags(self.row['main'], False, True)
            def moved():
                placement = self.ready_placement()
                if not placement: return None
                retained = {name:row for name,row in before.items() if row['node'] != self.row['node']}
                local = {name:row for name,row in placement.items() if row['node'] == self.row['node']}
                return placement if (all(placement.get(name) == row for name,row in retained.items()) and
                    len(local) == 1 and all(name not in before and row['disk'] == disks[0]['disk_uuid']
                                           for name,row in local.items())) else None
            self.row['on_extra'] = lab.wait('default-disk-native-evacuation-to-extra', 1200, moved); self.save()
            self.restore_main(); self.workload.data(self.row['payload'], read=True)
            self.backing(disks[0])
            self.flags(disks[0]['name'], False, True); disks[0].update(allow=False, evict=True); self.save()
            def evacuated():
                placement = self.ready_placement()
                if not placement or any(p['disk'] == disks[0]['disk_uuid'] for p in placement.values()): return None
                image = self.image(); ready = self.row['backing']['ready']; status = image.get('status', {})
                copies = status.get('diskFileStatusMap', {})
                return {'replicas': placement, 'backing': status} if (
                    status.get('checksum') == ready['checksum'] and status.get('size') == ready['size'] and
                    any(k != disks[0]['disk_uuid'] and v.get('state') == 'ready' for k,v in copies.items()) and
                    self.empty(disks[0], transferred_source=True)) else None
            proof = lab.wait('extra-replica-and-backing-image-evacuated', 1800, evacuated)
            self.row['evacuation'] = proof; self.save(); self.workload.data(self.row['payload'], read=True)
            print('LONGHORN_DISK_NATIVE_EVACUATION_VERIFIED '+json.dumps(proof, sort_keys=True), flush=True)
            self.remove_image()  # historical completed BIDS must retire before host unmount
            self.retire_disk(disks[0]); self.prepare_disk(disks[1])
            if disks[1]['disk_uuid'] == disks[0]['disk_uuid']: raise ValueError('replacement reused the old Longhorn disk identity')
            self.row['replacement'] = {'old_uuid': disks[0]['disk_uuid'], 'new_uuid': disks[1]['disk_uuid']}; self.save()
            self.workload.detach(); self.workload.mount('disk-remount', 1); self.workload.data(self.row['payload'], read=True)
            print('LONGHORN_DISK_REPLACEMENT_VERIFIED '+json.dumps(self.row['replacement'], sort_keys=True), flush=True)
        print('LONGHORN_DISK_LIFECYCLE_PASS', flush=True)

    def clean(self):
        if self.row is None:
            if not self.path.exists() or self.path.is_symlink(): raise ValueError('missing real disk lifecycle cleanup journal')
            self.row = json.loads(self.path.read_text())
        if self.row['run_id'] != self.lab.owner()['run_id']: raise ValueError('foreign disk lifecycle journal')
        expected_node = 'longhorn-node-'+self.lab.WORKERS[0].rsplit('.', 1)[1]
        if self.row['node'] != expected_node: raise ValueError('disk lifecycle node is outside its lease')
        if len(self.row['disks']) != 2 or len({d['device'] for d in self.row['disks']}) != 2:
            raise ValueError('disk lifecycle cleanup requires its two independent disk identities')
        for index, disk in enumerate(self.row['disks']):
            expected_uuid = str(uuid.uuid5(uuid.UUID(self.row['run_id']), 'disk26:'+self.lab.WORKERS[0]+':'+str(index)))
            if (disk['node'] != self.lab.WORKERS[0] or disk['path'] != EXTRA or disk['uuid'] != expected_uuid or
                    disk['name'] != 'kubeauto-extra26-'+str(index) or not disk['device'].startswith('/dev/disk/by-')):
                raise ValueError('foreign extra disk cleanup journal')
        self.restore_main(); self.remove_duplicate(); self.remove_image()
        for disk in self.row['disks']:
            if disk['phase'] not in ('planned', 'retired'): self.retire_disk(disk)
        self.workload.cleanup(); self.original(); self.lab.installation_health()
        self.path.unlink()
        print('LONGHORN_DISK_LIFECYCLE_SCOPED_CLEAN_PASS', flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('business', 'clean'))
    parser.add_argument('--relay-pid', type=int, required=True)
    parser.add_argument('--allowlist', type=Path)
    args = parser.parse_args()
    lab = load('longhorn-lab.py'); lab.require_lease(); lab.owner()
    shared = load('longhorn-upgrade-lifecycle.py')
    workload = shared.Workload(lab, 'disk26')
    workload.path = lab.DIRECTORY/'longhorn-disk-lifecycle-business.json'
    scenario = Lifecycle(lab, workload, args.relay_pid)
    if args.action == 'business':
        if not args.allowlist: parser.error('business requires the fixed qualified --allowlist')
        scenario.business(args.allowlist)
    else: scenario.clean()


if __name__ == '__main__': main()
