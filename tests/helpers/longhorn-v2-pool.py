#!/usr/bin/env python3
"""Read-only inputs for the explicitly authorized three-node V2 pool.

Inventory success is not platform admission, product acceptance or a disk lease.
Never discovers arbitrary targets or changes modules, pages, disks or services.
"""
import json
import shlex
import subprocess
import sys


HOSTS = ('192.168.47.146', '192.168.47.147', '192.168.47.148')
PROBE = r'''
import glob,json,os,platform,re,subprocess
from pathlib import Path

def command(argv):
    result = subprocess.run(argv, text=True, capture_output=True, timeout=15)
    return {'rc': result.returncode, 'stdout': result.stdout.strip()}

def read(path):
    try:
        return Path(path).read_text().strip()
    except OSError as error:
        return {'errno': error.errno}

def required(argv):
    result = command(argv)
    if result['rc']:
        raise ValueError('required inventory failed: ' + argv[0])
    return result['stdout']

disks = json.loads(required(['lsblk', '--json', '--bytes', '--output',
    'NAME,TYPE,SIZE,RO,FSTYPE,MOUNTPOINTS,WWN,SERIAL,MODEL,PKNAME']))
details = {}
for disk in disks['blockdevices']:
    # Restrict device diagnostics to whole disks; never inspect mounted data.
    if disk['type'] != 'disk' or int(disk['size']) < 1024**3:
        continue
    name = disk['name']
    if not re.fullmatch(r'[A-Za-z0-9_-]+', name):
        raise ValueError('unexpected whole-disk name')
    path = '/dev/' + name
    details[name] = {
        'signatures': json.loads(required(['wipefs', '--no-act', '--json', path])),
        'holders': sorted(glob.glob('/sys/class/block/' + name + '/holders/*')),
        'sysfs_path': str((Path('/sys/class/block')/name).resolve()),
        'wwid': read('/sys/class/block/' + name + '/device/wwid'),
        'udev': command(['udevadm', 'info', '--query=property', '--name=' + path])}
memory = read('/proc/meminfo')
kernel = platform.release()
version = re.match(r'([0-9]+)\.([0-9]+)', kernel)
flags = re.findall(r'^flags\s*:\s*(.*)$', read('/proc/cpuinfo'), re.M)
print(json.dumps({
    'os': platform.freedesktop_os_release(), 'kernel': kernel,
    'kernel_ge_6_7': bool(version and tuple(map(int, version.groups())) >= (6, 7)),
    'architecture': platform.machine(), 'hostname': platform.node(),
    'sse42_all_cpus': bool(flags) and all('sse4_2' in row.split() for row in flags),
    'online_cpus': read('/sys/devices/system/cpu/online'),
    'memory_kib': {key: int(value) for key, value in
        re.findall(r'^(MemTotal|MemAvailable):\s+(\d+) kB$', memory, re.M)},
    'hugepages_2mi': read('/sys/kernel/mm/hugepages/hugepages-2048kB/nr_hugepages'),
    'modules': {name: {'loaded': Path('/sys/module', name).exists(),
                      'available': command(['modinfo', '-F', 'filename', name])}
                for name in ('vfio_pci', 'uio_pci_generic', 'nvme_tcp')},
    'disks': disks, 'disk_details': details,
    'by_id': {path: os.path.realpath(path) for path in sorted(glob.glob('/dev/disk/by-id/*'))},
    'services': {name: command(['systemctl', 'is-active', name]) for name in
                 ('kubelet', 'containerd', 'docker', 'ceph.target')},
    'existing_paths': {path: Path(path).exists() for path in
                      ('/etc/kubernetes', '/var/lib/kubelet', '/usr/local/kubeauto')},
    'ceph_files': sorted(glob.glob('/etc/ceph/*') + glob.glob('/var/lib/ceph/*')),
    'machine_id': read('/etc/machine-id')}, sort_keys=True))
'''


def assess(inventory):
    """Report initial fixture prerequisites; never authorize disk consumption."""
    blockers = []
    if not inventory['kernel_ge_6_7']:
        blockers.append('kernel-below-6.7')
    architecture = inventory['architecture']
    if architecture not in ('x86_64', 'aarch64'):
        blockers.append('unsupported-architecture')
    if architecture == 'x86_64' and not inventory['sse42_all_cpus']:
        blockers.append('missing-sse4.2')
    if any(not row['loaded'] and row['available']['rc'] for row in inventory['modules'].values()):
        blockers.append('required-module-unavailable')
    if inventory['memory_kib'].get('MemTotal', 0) < 3 * 1024**2:
        blockers.append('insufficient-memory')
    if (any(row['rc'] == 0 for row in inventory['services'].values()) or
            any(inventory['existing_paths'].values()) or inventory['ceph_files']):
        blockers.append('existing-host-state-requires-ownership-check')
    candidates = []
    for disk in inventory['disks']['blockdevices']:
        name = disk['name']
        detail = inventory['disk_details'].get(name)
        if (disk['type'] != 'disk' or int(disk['size']) != 100 * 1024**3 or
                disk['ro'] or disk.get('fstype') or disk.get('children') or
                any(disk.get('mountpoints', [])) or not detail or
                detail['signatures'].get('signatures') or detail['holders'] or
                '/virtual/' in detail['sysfs_path']):
            continue
        aliases = sorted(path for path, target in inventory['by_id'].items() if target == '/dev/' + name)
        identity = disk.get('wwn') or disk.get('serial')
        candidates.append({'name': name, 'size_bytes': int(disk['size']),
                           'by_id': aliases, 'identity': identity})
    if len(candidates) != 1:
        blockers.append('expected-one-unused-100GiB-disk')
    elif not candidates[0]['by_id'] or not candidates[0]['identity']:
        blockers.append('raw-disk-missing-stable-hardware-identity')
    return {'blockers': blockers, 'candidates': candidates,
            'requires_leased_product_admission': True}


def collect(host):
    if host not in HOSTS:
        raise ValueError('unauthorized V2 fixture host')
    result = subprocess.run(
        ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10',
         '-o', 'ServerAliveInterval=5', '-o', 'ServerAliveCountMax=2',
         'root@' + host, shlex.join(['python3', '-c', PROBE])],
        text=True, capture_output=True, timeout=90)
    if result.returncode:
        raise ValueError('host inventory failed: ' + host + ' rc=' + str(result.returncode))
    inventory = json.loads(result.stdout)
    return {'host': host, 'inventory': inventory, 'assessment': assess(inventory)}


def main():
    for host in HOSTS:
        print(json.dumps(collect(host), sort_keys=True), flush=True)
    print('LONGHORN_V2_POOL_INPUTS_PASS scope=read-only-not-platform-admission')


if __name__ == '__main__':
    try:
        main()
    except (ValueError, KeyError, TypeError, OSError, subprocess.TimeoutExpired) as error:
        print('LONGHORN_V2_POOL_INPUTS_FAIL reason=' + str(error), file=sys.stderr)
        sys.exit(1)
