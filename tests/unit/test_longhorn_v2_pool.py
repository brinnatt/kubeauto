"""Contracts for the new pool's read-only input boundary, not V2 acceptance."""
import copy
import importlib.util
import io
import json
from pathlib import Path
import shlex
import subprocess
from types import SimpleNamespace
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location('longhorn_v2_pool', ROOT/'tests/helpers/longhorn-v2-pool.py')
POOL = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(POOL)


def inventory():
    return {
        'kernel_ge_6_7': True, 'architecture': 'x86_64', 'sse42_all_cpus': True,
        'memory_kib': {'MemTotal': 6 * 1024**2},
        'modules': {name: {'loaded': False, 'available': {'rc': 0}} for name in
                    ('vfio_pci', 'uio_pci_generic', 'nvme_tcp')},
        'services': {'kubelet': {'rc': 3}}, 'existing_paths': {}, 'ceph_files': [],
        'disks': {'blockdevices': [
            {'name': 'sda', 'type': 'disk', 'size': 120 * 1024**3, 'ro': False,
             'children': [{'name': 'sda1', 'mountpoints': ['/']}]},
            {'name': 'sdb', 'type': 'disk', 'size': 100 * 1024**3, 'ro': False,
             'fstype': None, 'mountpoints': [None], 'wwn': '0x6000abc', 'serial': None}]},
        'disk_details': {'sdb': {'signatures': {'signatures': []}, 'holders': [],
                                 'sysfs_path': '/sys/devices/pci/block/sdb'}},
        'by_id': {'/dev/disk/by-id/wwn-0x6000abc': '/dev/sdb'}}


class PoolInputs(unittest.TestCase):
    def test_fixed_pool_is_separate_from_v1_and_never_accepts_arbitrary_hosts(self):
        self.assertEqual(POOL.HOSTS, ('192.168.47.146', '192.168.47.147', '192.168.47.148'))
        with patch.object(POOL.subprocess, 'run') as run:
            for host in ('192.168.47.129', '192.168.47.145', '192.168.122.1', '192.168.122.135'):
                with self.assertRaisesRegex(ValueError, 'unauthorized'):
                    POOL.collect(host)
            run.assert_not_called()

    def test_inventory_success_still_requires_leased_product_admission(self):
        result = POOL.assess(inventory())
        self.assertEqual(result['blockers'], [])
        self.assertTrue(result['requires_leased_product_admission'])

    def test_kernel_name_or_fabricated_alias_cannot_replace_hardware_identity(self):
        for aliases, identity in (({}, '0x6000abc'), ({'/dev/disk/by-id/fake': '/dev/sdb'}, None)):
            row = inventory()
            row['by_id'] = aliases
            row['disks']['blockdevices'][1]['wwn'] = identity
            self.assertIn('raw-disk-missing-stable-hardware-identity', POOL.assess(row)['blockers'])

    def test_dirty_partitioned_mounted_readonly_nested_or_held_disk_is_not_candidate(self):
        for change in ({'fstype': 'ext4'}, {'children': [{}]}, {'mountpoints': ['/data']}, {'ro': True}):
            with self.subTest(change=change):
                row = inventory()
                row['disks']['blockdevices'][1].update(change)
                self.assertIn('expected-one-unused-100GiB-disk', POOL.assess(row)['blockers'])
        for change in ({'signatures': {'signatures': [{'type': 'LVM2_member'}]}},
                       {'holders': ['dm-1']}, {'sysfs_path': '/sys/devices/virtual/block/dm-0'}):
            row = inventory()
            row['disk_details']['sdb'].update(change)
            self.assertIn('expected-one-unused-100GiB-disk', POOL.assess(row)['blockers'])

    def test_multiple_unused_disks_are_ambiguous(self):
        row = inventory()
        disk = copy.deepcopy(row['disks']['blockdevices'][1]); disk['name'] = 'sdc'
        row['disks']['blockdevices'].append(disk)
        row['disk_details']['sdc'] = copy.deepcopy(row['disk_details']['sdb'])
        self.assertIn('expected-one-unused-100GiB-disk', POOL.assess(row)['blockers'])

    def test_host_requirements_and_foreign_state_are_reported(self):
        row = inventory()
        row.update(kernel_ge_6_7=False, sse42_all_cpus=False, ceph_files=['/etc/ceph/ceph.conf'])
        row['modules']['nvme_tcp']['available']['rc'] = 1
        row['memory_kib']['MemTotal'] = 2 * 1024**2
        self.assertEqual(set(POOL.assess(row)['blockers']), {
            'kernel-below-6.7', 'missing-sse4.2', 'required-module-unavailable',
            'insufficient-memory', 'existing-host-state-requires-ownership-check'})

    def test_remote_commands_are_read_only_and_preserve_errno_for_absent_wwid(self):
        commands = []
        class ProbePath:
            def __init__(self, *parts): self.path = '/'.join(map(str, parts))
            def __truediv__(self, part): return ProbePath(self.path, part)
            def read_text(self):
                if self.path.endswith('/wwid'): raise OSError(6, 'No such device or address')
                return {'/proc/meminfo': 'MemTotal: 6291456 kB\nMemAvailable: 5242880 kB\n',
                        '/proc/cpuinfo': 'flags : sse4_2\n',
                        '/sys/devices/system/cpu/online': '0-1',
                        '/sys/kernel/mm/hugepages/hugepages-2048kB/nr_hugepages': '0',
                        '/etc/machine-id': 'public-machine-id'}[self.path]
            def exists(self): return False
            def resolve(self): return '/sys/devices/pci/block/sdb'
        def run(argv, **kwargs):
            commands.append(argv)
            if argv[0] == 'lsblk': out = json.dumps(inventory()['disks'])
            elif argv[0] == 'wipefs':
                self.assertEqual(argv[1:3], ['--no-act', '--json'])
                out = '{"signatures":[]}'
            else: out = ''
            return SimpleNamespace(returncode=0, stdout=out)
        # Execute the actual transmitted probe; an unreadable sysfs WWID must
        # remain an observed gap instead of aborting the entire inventory.
        with patch('pathlib.Path', ProbePath), patch('subprocess.run', side_effect=run), \
                patch('glob.glob', return_value=[]), patch('platform.release', return_value='7.0.0'), \
                patch('platform.machine', return_value='x86_64'), \
                patch('platform.freedesktop_os_release', return_value={'PRETTY_NAME': 'fixture'}), \
                patch('sys.stdout', new_callable=io.StringIO) as output:
            exec(compile(POOL.PROBE, 'remote-read-only-probe', 'exec'), {})
        row = json.loads(output.getvalue())
        self.assertEqual(row['disk_details']['sdb']['wwid'], {'errno': 6})
        self.assertTrue(row['kernel_ge_6_7'])
        self.assertTrue(row['sse42_all_cpus'])
        self.assertEqual({a[0] for a in commands}, {'lsblk', 'wipefs', 'udevadm', 'modinfo', 'systemctl'})
        self.assertTrue(all(a[1] == 'is-active' for a in commands if a[0] == 'systemctl'))
        for forbidden in ('write_text', 'unlink', 'modprobe', 'sudo', 'mount(', 'kubectl', 'read_bytes'):
            self.assertNotIn(forbidden, POOL.PROBE)

    def test_collection_uses_batchmode_exact_probe_and_hides_remote_error_output(self):
        with patch.object(POOL.subprocess, 'run', return_value=SimpleNamespace(
                returncode=0, stdout=json.dumps(inventory()))) as run:
            row = POOL.collect(POOL.HOSTS[0])
        argv = run.call_args.args[0]
        self.assertIn('BatchMode=yes', argv)
        self.assertEqual(argv[-2], 'root@192.168.47.146')
        self.assertEqual(shlex.split(argv[-1]), ['python3', '-c', POOL.PROBE])
        self.assertEqual(row['assessment']['blockers'], [])
        with patch.object(POOL.subprocess, 'run', return_value=SimpleNamespace(
                returncode=1, stdout='PRIVATE', stderr='PRIVATE')):
            with self.assertRaisesRegex(ValueError, '^host inventory failed: 192.168.47.146 rc=1$'):
                POOL.collect(POOL.HOSTS[0])

    def test_fixed_runner_archives_only_input_success(self):
        script = 'source "$1"\nlonghorn_local_gate() { printf "%s %s" "$1" "$2"; }\nlonghorn_dispatch --longhorn-v2-pool-inputs\n'
        result = subprocess.run(['bash', '-c', script, 'contract', str(ROOT/'tests/helpers/longhorn-runner.sh')],
                                text=True, capture_output=True, check=True)
        self.assertEqual(result.stdout, 'v2-pool-inputs LONGHORN_V2_POOL_INPUTS_PASS')


if __name__ == '__main__':
    unittest.main()
