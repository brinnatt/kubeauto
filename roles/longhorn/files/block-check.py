#!/usr/bin/env python3
"""Read-only V2 AIO host admission and exact post-uninstall receipt release.

Never partitions, formats, wipes, binds PCI devices or allocates hugepages.
The declaration contains public device identities, not credentials.
"""
import argparse
import base64
import json
import os
from pathlib import Path
import platform
import re
import stat
import subprocess
import sys


def output(argv):
    result = subprocess.run(argv, text=True, capture_output=True, timeout=30)
    if result.returncode:
        raise ValueError('Longhorn V2 prerequisite failed: ' + argv[0])
    return result.stdout.strip()


def online_cpu_mask():
    """Linux sysfs cpulist uses CPU IDs, which need not be contiguous."""
    text = Path('/sys/devices/system/cpu/online').read_text().strip()
    if not re.fullmatch(r'\d+(?:-\d+)?(?:,\d+(?:-\d+)?)*', text):
        raise ValueError('invalid Longhorn V2 online CPU inventory')
    mask = 0
    for span in text.split(','):
        ends = list(map(int, span.split('-')))
        first, last = ends[0], ends[-1]
        if first > last:
            raise ValueError('invalid Longhorn V2 online CPU range')
        mask |= ((1 << (last - first + 1)) - 1) << first
    return mask


def cpu_policy(kubelet_root):
    """Use the same running-kubelet checkpoint as the native policy monitor."""
    root = Path(kubelet_root)
    if not root.is_absolute() or '..' in root.parts:
        raise ValueError('invalid kubelet root for Longhorn CPU policy check')
    state = json.loads((root/'cpu_manager_state').read_text())
    if not isinstance(state, dict) or state.get('policyName') != 'static':
        raise ValueError('Longhorn exclusive CPU cores require kubelet static CPU policy')
    return {'cpu_policy': 'static'}


def platform_receipt(c, loaded=False):
    kernel = platform.release()
    match = re.match(r'(\d+)\.(\d+)', kernel)
    if not match or tuple(map(int, match.groups())) < (6, 7):
        raise ValueError('Longhorn V2 NVMe/TCP requires Linux kernel >=6.7')
    machine = platform.machine()
    if machine not in ('x86_64', 'aarch64'):
        raise ValueError('unsupported Longhorn V2 architecture')
    if machine == 'x86_64':
        flags = re.findall(r'^flags\s*:\s*(.*)$', Path('/proc/cpuinfo').read_text(), re.M)
        if not flags or any('sse4_2' not in row.split() for row in flags):
            raise ValueError('Longhorn V2 requires SSE4.2 on every AMD64 CPU')
    mask = int(c['cpu_mask'], 16)
    cores = int(c.get('cpu_cores', 0))
    online = online_cpu_mask()
    if cores < 0 or (cores > 0 and cores > online.bit_count()):
        raise ValueError('Longhorn V2 exclusive CPU count exceeds online host CPUs')
    if not cores and (not mask or mask & online != mask):
        raise ValueError('Longhorn V2 CPU mask exceeds online host CPUs')
    for module in ('vfio_pci', 'uio_pci_generic', 'nvme_tcp'):
        if loaded:
            if not (Path('/sys/module')/module).exists():
                raise ValueError('Longhorn V2 kernel module not loaded: ' + module)
        else:
            output(['modprobe', '--dry-run', module])
    memory_mib = int(c['memory_mib'])
    memory = memory_mib * 1024**2
    info = dict((key, int(value)*1024) for key, value in
                re.findall(r'^(MemTotal|MemAvailable):\s+(\d+) kB$', Path('/proc/meminfo').read_text(), re.M))
    if info.get('MemTotal', 0) < memory + 1024**3:
        raise ValueError('Longhorn V2 host memory is below SPDK plus manager headroom')
    if c['hugepages']:
        # Total (not free) allows a healthy existing IM to retain its allocation.
        pages = int(Path('/sys/kernel/mm/hugepages/hugepages-2048kB/nr_hugepages').read_text())
        if pages * 2 < memory_mib:
            raise ValueError('Longhorn V2 requires administrator-preallocated 2Mi hugepages')
    return {'kernel': kernel, 'architecture': machine, 'cpu_mask': c['cpu_mask'],
            'cpu_cores': cores, 'memory_mib': memory_mib, 'hugepages': c['hugepages']}


def inspect_disk(disk, minimum_gib, *, initial):
    path = disk['path']
    if disk['disk_driver'] != 'aio' or not re.fullmatch(r'/dev/disk/by-id/[A-Za-z0-9_.:+-]+', path):
        raise ValueError('Longhorn managed V2 disk requires stable by-id and explicit aio')
    device = Path(path)
    real = device.resolve(strict=True)
    status = real.stat()
    if not stat.S_ISBLK(status.st_mode):
        raise ValueError('Longhorn V2 path is not a block device')
    sysdisk = Path('/sys/class/block')/real.name
    if (sysdisk/'partition').exists() or list(sysdisk.glob('*/partition')):
        raise ValueError('Longhorn V2 refuses partitions and partitioned devices')
    if '/virtual/' in str(sysdisk.resolve()) or list((sysdisk/'holders').iterdir()):
        raise ValueError('Longhorn V2 refuses virtual/nested devices and active holders')
    number = str(os.major(status.st_rdev)) + ':' + str(os.minor(status.st_rdev))
    for line in Path('/proc/self/mountinfo').read_text().splitlines():
        if line.split()[2] == number:
            raise ValueError('Longhorn V2 refuses a mounted device, including root/boot')
    for line in Path('/proc/swaps').read_text().splitlines()[1:]:
        if Path(line.split()[0]).resolve() == real:
            raise ValueError('Longhorn V2 refuses active swap')
    rows = json.loads(output(['lsblk', '--json', '--bytes', '--nodeps', '--output',
                             'PATH,TYPE,SIZE,RO,WWN,SERIAL,MAJ:MIN', str(real)]))['blockdevices']
    if len(rows) != 1 or rows[0]['type'] != 'disk' or rows[0]['ro'] not in (False, 0, '0'):
        raise ValueError('Longhorn V2 requires one writable whole disk')
    row = rows[0]
    if row['maj:min'] != number or row['path'] != str(real):
        raise ValueError('Longhorn V2 device identity changed during admission')
    if not disk['identity'] or disk['identity'] not in (row.get('wwn'), row.get('serial')):
        raise ValueError('Longhorn V2 declared hardware identity does not match current WWN/serial')
    size = int(row['size'])
    if size < int(minimum_gib) * 1024**3 or int(disk['storage_reserved']) >= size * 3 // 4:
        raise ValueError('Longhorn V2 raw disk capacity/reservation leaves insufficient space')
    if initial:
        signatures = json.loads(output(['wipefs', '--no-act', '--json', '--output', 'TYPE,UUID', str(real)]))
        if signatures.get('signatures'):
            raise ValueError('Longhorn V2 refuses existing signatures; no implicit wipe is permitted')
    # Recheck symlink target and device number after external probes.
    if device.resolve(strict=True) != real or real.stat().st_rdev != status.st_rdev:
        raise ValueError('Longhorn V2 device changed during inspection')
    return {'path': path, 'disk_driver': 'aio', 'identity': disk['identity'],
            'wwn': row.get('wwn'), 'serial': row.get('serial'), 'size_bytes': size,
            'device_number': number}


def receipt(c, marker, *, loaded=False, release=False):
    if marker.is_symlink() or (marker.exists() and not marker.is_file()):
        raise ValueError('Longhorn V2 refuses non-file or symlink receipts')
    for key in ('cluster_uid', 'node_uid'):
        if not re.fullmatch(r'[A-Za-z0-9-]+', c[key]):
            raise ValueError('Longhorn V2 requires live cluster and node identities')
    old = json.loads(marker.read_text()) if marker.exists() else None
    if old and any(old.get(key) != c[key] for key in ('cluster_uid', 'node_uid')):
        raise ValueError('Longhorn V2 receipt belongs to another cluster/node')
    if release and not old:
        return None
    declared = {disk['name'] for disk in c['disks']}
    if old and not set(old['disks']) <= declared:
        raise ValueError('Longhorn V2 disk retirement requires explicit evacuation and receipt release')
    disks = {}
    numbers = set()
    for disk in c['disks']:
        previous = old['disks'].get(disk['name']) if old else None
        current = inspect_disk(disk, c['minimum_gib'], initial=previous is None)
        if previous and any(previous.get(key) != current[key] for key in
                            ('path', 'disk_driver', 'identity', 'wwn', 'serial', 'size_bytes')):
            raise ValueError('Longhorn V2 raw disk identity changed; replacement must be explicit')
        if disk['name'] in disks or current['device_number'] in numbers:
            raise ValueError('Longhorn V2 refuses duplicate aliases of a raw device')
        numbers.add(current['device_number']); disks[disk['name']] = current
    return {'cluster_uid': c['cluster_uid'], 'node_uid': c['node_uid'], 'disks': disks,
            'platform': {} if release else platform_receipt(c, loaded)}


def retire_receipt(c, marker, *, apply=False):
    """Retire only approved empty AIO identities; never write their media."""
    import hashlib
    import tempfile
    operation=c['id']
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,63}',operation):raise ValueError('invalid block retirement ID')
    archive=marker.with_name('longhorn-block-retirement-'+operation+'.json')
    if marker.is_symlink() or archive.is_symlink():raise ValueError('symlink host block retirement state')
    targets=c['targets'];names={t['name'] for t in targets}
    if (not targets or len(names)!=len(targets) or any(set(t)!={'node','name','disk_uuid'} or t['node']!=c['node'] or
            not isinstance(t['disk_uuid'],str) or not t['disk_uuid'] for t in targets)):
        raise ValueError('invalid exact host disk retirement approvals')
    def digest(data):return hashlib.sha256(data).hexdigest() if data is not None else None
    def current():return marker.read_bytes() if marker.exists() else None
    def private(path,data):
        with tempfile.NamedTemporaryFile('wb',dir=path.parent,delete=False) as stream:
            stream.write(data);stream.flush();os.fsync(stream.fileno());temporary=Path(stream.name)
        temporary.chmod(0o600);temporary.replace(path)
    saved=json.loads(archive.read_text()) if archive.exists() else None
    identity={k:c[k] for k in ('id','cluster_uid','node_uid','node','targets')}
    if saved and saved['identity']!=identity:raise ValueError('host retirement archive belongs to another approval')
    original=base64.b64decode(saved['before'],validate=True) if saved else current()
    if original is None:raise ValueError('host retirement requires original raw-disk ownership receipt')
    old=json.loads(original)
    if any(old.get(k)!=c[k] for k in ('cluster_uid','node_uid')):raise ValueError('host retirement cluster/node UID differs')
    declared={d['name']:d for d in c['disks']}
    if set(old['disks'])!=set(declared) or not names<=set(declared):raise ValueError('host retirement declarations differ from original receipt')
    numbers=set()
    for name,decl in declared.items():
        previous=old['disks'][name];now=inspect_disk(decl,c['minimum_gib'],initial=False)
        if name in names:numbers.add(now['device_number'])
        if any(previous.get(key)!=now[key] for key in ('path','disk_driver','identity','wwn','serial','size_bytes')):
            raise ValueError('host retirement stable raw identity changed')
    after=dict(old,disks={k:v for k,v in old['disks'].items() if k not in names})
    after_bytes=(json.dumps(after,sort_keys=True)+'\n').encode() if after['disks'] else None
    if saved and saved['after_sha256']!=digest(after_bytes):raise ValueError('host retirement retained peer receipts changed')
    observed=current()
    if digest(observed) not in (digest(original),digest(after_bytes)):
        raise ValueError('host block receipt changed outside precise retirement')
    if not saved and observed!=original:raise ValueError('host block receipt changed during inspection')
    before_hash=digest(original)
    result=dict(identity,marker_sha256=before_hash,original=old)
    if not apply:return result
    proof=c.get('proof',{})
    if (proof.get('id')!=operation or proof.get('cluster_uid')!=c['cluster_uid'] or proof.get('node_uid')!=c['node_uid'] or
            proof.get('targets')!=targets or proof.get('before_sha256')!=before_hash or proof.get('native_absent') is not True):
        raise ValueError('host receipt release requires exact control proof of native SPDK absence')
    for process in Path('/proc').iterdir():
        if not process.name.isdigit():continue
        try:
            for fd in (process/'fd').iterdir():
                try:info=fd.stat()
                except FileNotFoundError:continue
                if stat.S_ISBLK(info.st_mode) and str(os.major(info.st_rdev))+':'+str(os.minor(info.st_rdev)) in numbers:
                    raise ValueError('retired raw disk is still open by a host process')
        except (FileNotFoundError,ProcessLookupError):continue
    if not saved:
        saved={'identity':identity,'before':base64.b64encode(original).decode(),'after_sha256':digest(after_bytes),'phase':'release-intent'}
        private(archive,(json.dumps(saved,sort_keys=True)+'\n').encode())
    if digest(current())!=digest(after_bytes):
        if current()!=original:raise ValueError('host receipt changed before release write')
        if after_bytes is None:marker.unlink()
        else:private(marker,after_bytes)
    if digest(current())!=digest(after_bytes):raise ValueError('host receipt release did not converge')
    saved['phase']='complete';private(archive,(json.dumps(saved,sort_keys=True)+'\n').encode())
    return dict(identity,before_sha256=before_hash,after_sha256=digest(after_bytes),data_preserved=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--declaration', required=True)
    parser.add_argument('--marker', default='/etc/kubeauto/longhorn-block-disks.json')
    parser.add_argument('--phase', choices=('preflight', 'verify', 'release-check', 'release', 'cpu-policy', 'retire-check', 'retire'), default='preflight')
    args = parser.parse_args()
    c = json.loads(base64.b64decode(args.declaration, validate=True))
    if args.phase == 'cpu-policy':
        print(json.dumps(cpu_policy(c['kubelet_root']), sort_keys=True))
        return
    marker = Path(args.marker)
    if args.phase in ('retire-check', 'retire'):
        print(json.dumps(retire_receipt(c, marker, apply=args.phase == 'retire'), sort_keys=True)); return
    original = marker.read_bytes() if marker.is_file() and not marker.is_symlink() else None
    result = receipt(c, marker, loaded=args.phase == 'verify', release=args.phase.startswith('release'))
    if args.phase == 'release':
        if result is not None:
            if marker.is_symlink() or marker.read_bytes() != original:
                raise ValueError('Longhorn V2 receipt changed during release')
            marker.unlink()
        print(json.dumps({'changed': result is not None, 'data_preserved': True}))
    else:
        print(json.dumps(result, sort_keys=True))


if __name__ == '__main__':
    try:
        main()
    except (ValueError, KeyError, TypeError, OSError, subprocess.TimeoutExpired) as error:
        print('LONGHORN_V2_HOST_FAIL reason=' + str(error), file=sys.stderr)
        sys.exit(1)
