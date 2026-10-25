"""Bounded allocation, inode retirement and native recovery contracts."""
import copy
import errno
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import uuid

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location('capacity', ROOT/'tests/helpers/longhorn-capacity.py')
C = importlib.util.module_from_spec(SPEC); SPEC.loader.exec_module(C)


class CapacityContracts(unittest.TestCase):
    def roomy(self):
        return patch.object(os, 'statvfs', return_value=SimpleNamespace(
            f_frsize=4096, f_blocks=4*1024**2, f_bavail=4*1024**2, f_bfree=4*1024**2))

    def fixture(self, directory):
        base = Path(directory); identifier = str(uuid.uuid4())
        request = {'base': str(base), 'id': identifier, 'uuid': 'parent-uuid', 'limit': C.LIMIT}
        state = {'mounted': False}; calls = []
        original_stat = Path.stat
        def pathstat(path, *a, **kw):
            return SimpleNamespace(st_dev=-1) if path == Path('/') else original_stat(path, *a, **kw)
        def run(argv, **kw):
            calls.append(argv)
            if argv[0] == 'findmnt':
                is_base = argv[3] == str(base)
                rows = [{'target': str(base), 'uuid': 'parent-uuid', 'fstype': 'ext4'}] if is_base else ([{
                    'target': str(base/('capacity-'+identifier)/'mount'), 'uuid': identifier,
                    'fstype': 'ext4', 'source': '/dev/loop77'}] if state['mounted'] else [])
                return SimpleNamespace(stdout=json.dumps({'filesystems': rows}))
            if argv[0] == 'mount': state['mounted'] = True
            elif argv[0] == 'umount': state['mounted'] = False
            elif argv[0] == 'losetup':
                return SimpleNamespace(stdout=json.dumps({'loopdevices': [{'name': '/dev/loop77',
                    'back-file': str(base/('capacity-'+identifier)/'disk.img')}] if state['mounted'] else []}))
            elif argv[0] != 'mkfs.ext4': raise AssertionError(argv)
            return SimpleNamespace(stdout='')
        return request, calls, pathstat, run

    def test_private_image_creation_and_removal_keep_parent_and_foreign_files(self):
        with tempfile.TemporaryDirectory() as d:
            request, calls, pathstat, run = self.fixture(d)
            marker = Path(d)/'foreign'; marker.write_text('preserve')
            with self.roomy(), patch.object(Path, 'stat', pathstat), patch.object(subprocess, 'run', side_effect=run):
                observed = C.host_fixture(request, 'create')
                image = Path(observed['mount']).parent/'disk.img'
                self.assertEqual(C.LIMIT, image.stat().st_size)
                self.assertLess(image.stat().st_blocks*512, 1024**2)
                self.assertTrue(C.host_fixture(request, 'remove')['removed'])
            self.assertEqual('preserve', marker.read_text())
            self.assertEqual(['foreign'], [p.name for p in Path(d).iterdir()])
            mkfs = next(a for a in calls if a[0] == 'mkfs.ext4')
            self.assertIn(str(image), mkfs)
            self.assertNotIn('/dev/loop77', mkfs)

    def test_real_inode_substitution_or_foreign_consumer_prevents_unmount_and_unlink(self):
        for defect in ('image', 'filler', 'consumer'):
            with self.subTest(defect=defect), tempfile.TemporaryDirectory() as d:
                request, calls, pathstat, run = self.fixture(d)
                with self.roomy(), patch.object(Path, 'stat', pathstat), patch.object(subprocess, 'run', side_effect=run):
                    observed = C.host_fixture(request, 'create'); mount = Path(observed['mount'])
                    if defect == 'image':
                        image = mount.parent/'disk.img'; image.rename(image.with_suffix('.old')); image.touch()
                    elif defect == 'filler': (mount/'kubeauto-capacity-filler').write_text('foreign')
                    else:
                        (mount/'replicas').mkdir(); (mount/'replicas'/'foreign').mkdir()
                    with self.assertRaises(ValueError): C.host_fixture(request, 'remove')
                self.assertFalse(any(a[0] == 'umount' for a in calls))

    def test_low_and_full_allocations_are_bounded_and_exact_filler_is_released(self):
        with tempfile.TemporaryDirectory() as d:
            request, calls, pathstat, run = self.fixture(d)
            with self.roomy(), patch.object(Path, 'stat', pathstat), patch.object(subprocess, 'run', side_effect=run):
                observed = C.host_fixture(request, 'create'); mount = Path(observed['mount'])
                filler = mount/'kubeauto-capacity-filler'
                def space(*args):
                    size = filler.stat().st_size if filler.exists() else 0
                    return SimpleNamespace(f_frsize=4096, f_blocks=C.LIMIT//4096,
                        f_bavail=(C.LIMIT-size)//4096, f_bfree=(C.LIMIT-size)//4096)
                def allocate(fd, offset, amount):
                    if offset+amount > C.LIMIT:
                        raise OSError(errno.ENOSPC, 'bounded filesystem has no allocatable blocks')
                    os.ftruncate(fd, offset+amount)
                # Actual files and inodes; only the kernel space allocator is
                # simulated, so no test can fill the development filesystem.
                with patch.object(os, 'fstatvfs', side_effect=space), patch.object(os, 'statvfs', side_effect=space), \
                        patch.object(os, 'posix_fallocate', side_effect=allocate):
                    low = C.host_fixture(request, 'low')
                    self.assertLessEqual(low['available'], C.LIMIT//10+4096)
                    full = C.host_fixture(request, 'full')
                    self.assertEqual(0, full['free']); self.assertEqual(C.LIMIT, full['filler_bytes'])
                    self.assertEqual(C.LIMIT, C.host_fixture(request, 'release')['available'])
                    self.assertFalse(filler.exists())
                C.host_fixture(request, 'remove')

    def test_system_filesystem_or_changed_uuid_blocks_before_mutation(self):
        with tempfile.TemporaryDirectory() as d:
            request, calls, pathstat, run = self.fixture(d)
            with self.assertRaises(ValueError): C.host_fixture(dict(request, base='/'), 'create')
            request['uuid'] = 'foreign'
            with patch.object(Path, 'stat', pathstat), patch.object(subprocess, 'run', side_effect=run):
                with self.assertRaises(ValueError): C.host_fixture(request, 'create')
            self.assertEqual([], list(Path(d).iterdir()))

    def test_full_records_block_enospc_while_ext4_retains_internal_reserved_blocks(self):
        with tempfile.TemporaryDirectory() as d:
            request, calls, pathstat, run = self.fixture(d)
            with self.roomy(), patch.object(Path, 'stat', pathstat), patch.object(subprocess, 'run', side_effect=run):
                observed = C.host_fixture(request, 'create'); mount = Path(observed['mount'])
                filler = mount/'kubeauto-capacity-filler'; reserved = 16*1024**2
                allocations = []
                def space(*args):
                    size = filler.stat().st_size if filler.exists() else 0
                    return SimpleNamespace(f_frsize=4096, f_blocks=C.LIMIT//4096,
                        f_bavail=max(C.LIMIT-reserved-size, 0)//4096,
                        f_bfree=(C.LIMIT-size)//4096)
                def allocate(fd, offset, amount):
                    allocations.append((offset, amount))
                    if offset+amount > C.LIMIT-reserved:
                        raise OSError(errno.ENOSPC, 'ext4 reserves metadata clusters')
                    os.ftruncate(fd, offset+amount)
                with patch.object(os, 'fstatvfs', side_effect=space), patch.object(os, 'statvfs', side_effect=space), \
                        patch.object(os, 'posix_fallocate', side_effect=allocate):
                    low = C.host_fixture(request, 'low')
                    self.assertLessEqual(low['available'], C.LIMIT//10+4096)
                    full = C.host_fixture(request, 'full')
                    self.assertEqual(0, full['available'])
                    self.assertEqual(reserved, full['free'])
                    self.assertEqual(errno.ENOSPC, (full.get('enospc') or {}).get('errno'))
                    self.assertEqual((C.LIMIT-reserved, 4096), allocations[-1])
                    self.assertEqual({'errno': errno.ENOSPC, 'offset': C.LIMIT-reserved, 'length': 4096},
                                     full['enospc'])
                    self.assertEqual(4096, full['block_size'])
                    self.assertEqual(C.LIMIT-reserved, full['filler_bytes'])
                    C.host_fixture(request, 'release')
                    self.assertFalse(filler.exists())
                C.host_fixture(request, 'remove')

    def test_full_observation_is_saved_before_enospc_validation(self):
        for defect in ('none', 'no-probe', 'wrong-error', 'large-probe'):
            with self.subTest(defect=defect):
                full = {'free': 16*1024**2, 'available': 0, 'block_size': 4096,
                        'enospc': {'errno': errno.ENOSPC, 'offset': C.LIMIT-16*1024**2, 'length': 4096}}
                if defect == 'no-probe': full['enospc'] = None
                if defect == 'wrong-error': full['enospc']['errno'] = errno.EIO
                if defect == 'large-probe': full['enospc']['length'] = 16*1024**2
                row = {}; lab = SimpleNamespace()
                with patch.object(C, 'host', return_value=full), patch.object(C, 'save') as save:
                    if defect == 'none': self.assertEqual(full, C.full_disk(lab, row))
                    else:
                        with self.assertRaisesRegex(ValueError, 'block-sized ENOSPC'):
                            C.full_disk(lab, row)
                    self.assertEqual(full, row['full'])
                    save.assert_called_once_with(lab, row)

    def test_removed_host_probe_is_read_only_and_refuses_any_owned_residue(self):
        for defect in ('none', 'directory', 'mount', 'loop', 'loop-deleted'):
            with self.subTest(defect=defect), tempfile.TemporaryDirectory() as d:
                request, calls, pathstat, run = self.fixture(d)
                directory = Path(d)/('capacity-'+request['id'])
                if defect == 'directory': directory.mkdir()
                def observed(argv, **kw):
                    if argv[:3] == ['findmnt', '-J', '--list'] and defect == 'mount':
                        return SimpleNamespace(stdout=json.dumps({'filesystems': [{
                            'target': '/another/path', 'uuid': request['id']}]}))
                    if argv[0] == 'losetup' and defect in ('loop', 'loop-deleted'):
                        return SimpleNamespace(stdout=json.dumps({'loopdevices': [{
                            'name': '/dev/loop77', 'back-file': str(directory/'disk.img')+
                                (' (deleted)' if defect == 'loop-deleted' else '')}]}))
                    return run(argv, **kw)
                with patch.object(Path, 'stat', pathstat), patch.object(subprocess, 'run', side_effect=observed):
                    if defect == 'none':
                        self.assertEqual({'uuid': request['id'], 'parent_uuid': request['uuid'],
                            'directory_absent': True, 'mount_absent': True, 'loop_absent': True},
                            C.host_fixture(request, 'verify-removed'))
                    else:
                        with self.assertRaisesRegex(ValueError, 'remains after cleanup'):
                            C.host_fixture(request, 'verify-removed')
                self.assertTrue(all(a[0] in ('findmnt', 'losetup') for a in calls))

    def test_clean_failed_receipt_archives_only_after_complete_same_owner_verification(self):
        for defect in ('none', 'owner', 'cluster', 'disk-uuid', 'node', 'flags', 'disk-status',
                       'native', 'namespace', 'settings', 'host', 'business', 'archive', 'passed',
                       'shared', 'input'):
            with self.subTest(defect=defect), tempfile.TemporaryDirectory() as d:
                base = Path(d); (base/'config.yml').write_text('unchanged input')
                lab, row, node = self.native()
                address = '192.168.122.212'; identifier = str(uuid.uuid4())
                row.update(run_id='run', cluster_uid='cluster', address=address, clean=True,
                    host={'id': identifier, 'uuid': 'parent-uuid'},
                    namespace_intent={'name': 'owned-ns', 'token': 'owned-token'},
                    settings={'setting': {'uid': 'setting-uid', 'value': '25'}}, shared={'unchanged': True},
                    config_sha256=hashlib.sha256((base/'config.yml').read_bytes()).hexdigest())
                if defect == 'input': (base/'config.yml').write_text('changed input')
                if defect == 'passed': row['passed'] = True
                record = base/C.RECORD; record.write_text(json.dumps(row))
                archived = record.with_name('longhorn-capacity-recovered-'+identifier+'.json')
                if defect == 'node': node['metadata']['uid'] = 'replacement'
                if defect == 'flags': node['spec']['disks']['default']['allowScheduling'] = False
                if defect == 'disk-status': node['status'] = {'diskStatus': {row['disk']: {}}}
                if defect == 'business': (base/C.BUSINESS).write_text('preserve')
                if defect == 'archive': archived.write_text('preserve')
                setting = {'metadata': {'uid': 'setting-uid'}, 'value': '26' if defect == 'settings' else '25'}
                def get(kind, name=None, namespace=None):
                    if kind == 'nodes.longhorn.io': return node
                    if kind == 'namespace': return {'metadata': {'uid': 'foreign'}} if defect == 'namespace' else None
                    if kind == 'settings.longhorn.io': return setting
                    raise AssertionError((kind, name, namespace))
                platform = SimpleNamespace(empty=Mock(side_effect=ValueError('native residue') if defect == 'native' else None))
                lab.DIRECTORY=base; lab.WORKERS=(address,); lab.get=Mock(side_effect=get)
                lab.owner=Mock(return_value={'run_id': 'foreign' if defect == 'owner' else 'run',
                    'cluster_uid': 'replacement' if defect == 'cluster' else 'cluster',
                    'disks': {address: {'uuid': 'replacement' if defect == 'disk-uuid' else 'parent-uuid'}}})
                lab.load_helper=Mock(return_value=platform); lab.installation_health=Mock()
                lab.shared_snapshot_identity=Mock(return_value={'unchanged': defect != 'shared'})
                lab.write_private=lambda p, value: p.write_text(json.dumps(value))
                removed = {'uuid': 'replacement' if defect == 'host' else identifier, 'parent_uuid': 'parent-uuid',
                           'directory_absent': True, 'mount_absent': True, 'loop_absent': True}
                with patch.object(C, 'host', return_value=removed) as host:
                    if defect == 'none':
                        C.clean(lab, 0, failed=True)
                        self.assertFalse(record.exists()); self.assertTrue(archived.exists())
                        accepted = json.loads(archived.read_text())
                        self.assertTrue(accepted['clean']); self.assertNotIn('passed', accepted)
                        self.assertEqual(removed, accepted['recovery_verified']['host'])
                        host.assert_called_once_with(lab, accepted, 'verify-removed')
                    else:
                        with self.assertRaises(ValueError): C.clean(lab, 0, failed=True)
                        self.assertTrue(record.exists())
                        self.assertNotIn('recovery_verified', json.loads(record.read_text()))
                        if defect == 'archive': self.assertEqual('preserve', archived.read_text())
                        else: self.assertFalse(archived.exists())

    def native(self):
        row = {'node': 'worker', 'node_uid': 'node-uid', 'disk': 'extra',
            'original_disks': {'default': {'path': '/data/test', 'allowScheduling': True}},
            'extra': {'path': '/data/extra', 'allowScheduling': True}}
        node = {'metadata': {'uid': 'node-uid'}, 'spec': {'disks': copy.deepcopy(row['original_disks'])}}
        lab = SimpleNamespace(get=Mock(return_value=node), kubectl=Mock())
        return lab, row, node

    def test_native_disk_flags_restore_exact_original_configuration_with_cas(self):
        lab, row, node = self.native(); C.disk_flags(lab, row)
        change = json.loads(lab.kubectl.call_args.kwargs['stdin'])
        self.assertEqual('test', change[0]['op']); self.assertEqual(node['spec']['disks'], change[1]['value'])
        desired = change[2]['value']; self.assertFalse(desired['default']['allowScheduling'])
        self.assertTrue(desired['extra']['allowScheduling'])
        node['spec']['disks'] = desired
        C.disk_flags(lab, row, restore=True, isolate=True, remove=True)
        self.assertEqual(row['original_disks'], json.loads(lab.kubectl.call_args.kwargs['stdin'])[2]['value'])

    def test_foreign_native_disk_identity_or_configuration_refuses_all_changes(self):
        for defect in ('node', 'path', 'foreign'):
            with self.subTest(defect=defect):
                lab, row, node = self.native()
                if defect == 'node': node['metadata']['uid'] = 'replacement'
                if defect == 'path': node['spec']['disks']['default']['path'] = '/foreign'
                if defect == 'foreign': node['spec']['disks']['foreign'] = {'path': '/foreign'}
                with self.assertRaises(ValueError): C.disk_flags(lab, row, restore=True)
                lab.kubectl.assert_not_called()

    def test_failed_replica_retirement_requires_two_distinct_running_rw_peers(self):
        failed = {'metadata': {'name': 'failed', 'uid': 'failed-uid'},
            'spec': {'volumeName': 'data', 'failedAt': 'now'}, 'status': {'currentState': 'error'}}
        peers = [{'metadata': {'name': 'peer'+str(i), 'uid': str(i)}, 'spec': {
            'volumeName': 'data', 'nodeID': 'node'+str(i), 'healthyAt': 'before'},
            'status': {'currentState': 'running'}} for i in range(2)]
        for defect in ('none', 'same-node', 'not-rw', 'not-failed', 'replacement'):
            with self.subTest(defect=defect):
                current = copy.deepcopy(failed); rows = copy.deepcopy(peers)
                mode = {p['metadata']['name']: 'RW' for p in rows}
                if defect == 'same-node': rows[1]['spec']['nodeID'] = rows[0]['spec']['nodeID']
                if defect == 'not-rw': mode['peer1'] = 'ERR'
                if defect == 'not-failed': current['spec'].pop('failedAt'); current['status']['currentState'] = 'running'
                if defect == 'replacement': current['metadata']['uid'] = 'new'
                engine = {'spec': {'volumeName': 'data'}, 'status': {'currentState': 'running', 'replicaModeMap': mode}}
                def get(kind, name=None, namespace=None):
                    if name: return current
                    return {'items': rows+[current] if kind == 'replicas.longhorn.io' else [engine]}
                lab = SimpleNamespace(get=Mock(side_effect=get), remove=Mock())
                if defect == 'none':
                    C.retire_failed(lab, {'volume': 'data'}, failed); lab.remove.assert_called_once_with(current)
                else:
                    with self.assertRaises(ValueError): C.retire_failed(lab, {'volume': 'data'}, failed)
                    lab.remove.assert_not_called()

    def test_failure_cleanup_releases_filler_and_restores_flags_before_business_cleanup(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d); (base/C.RECORD).touch(); (base/C.BUSINESS).write_text(json.dumps({'objects': {}}))
            row = {'host_ready': True, 'flags_intent': True, 'original_disks': {}, 'disk': 'extra'}
            calls = []
            lab = SimpleNamespace(DIRECTORY=base, cleanup_business=Mock(side_effect=lambda *a, **k: calls.append('business')),
                installation_health=Mock(), write_private=Mock(), wait=Mock(return_value=True))
            with patch.object(C, 'receipt', return_value=row), patch.object(C, 'host', side_effect=lambda *a: calls.append(a[-1])), \
                    patch.object(C, 'disk_flags', side_effect=lambda *a, **k: calls.append('flags-remove' if k.get('remove') else 'flags-restore')), \
                    patch.object(C, 'native_node', return_value={'spec': {'disks': {}}, 'status': {}}):
                C.clean(lab, 123)
            self.assertEqual(['release', 'flags-restore', 'business', 'flags-remove', 'remove'], calls)


if __name__ == '__main__': unittest.main()
