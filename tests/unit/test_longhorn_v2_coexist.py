"""Bounded filesystem ownership and dual-engine recovery contracts; no lab IO."""
import copy
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import uuid
import yaml

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location('coexist_contract', ROOT/'tests/helpers/longhorn-v2-coexist.py')
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class FilesystemContracts(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(); self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        for name in ('etc', 'var/lib', 'data'): (self.root/name).mkdir(parents=True)
        (self.root/'etc/machine-id').write_text('machine')
        (self.root/'etc/fstab').write_text('# original\n')
        self.request = {'id': str(uuid.uuid4()), 'uuid': str(uuid.uuid4()), 'machine_id': 'machine', 'size': MODULE.SIZE}
        self.mount = self.root/MODULE.MOUNT.lstrip('/')
        self.image = self.root/'var/lib/kubeauto-longhorn-coexist46'/self.request['id']/'disk.img'
        self.active = False
        self.commands = []
        self.runner = patch('subprocess.run', side_effect=self.command).start(); self.addCleanup(patch.stopall)
        patch('shutil.disk_usage', return_value=SimpleNamespace(free=16*1024**3)).start()

    def command(self, argv, **kwargs):
        self.commands.append(argv)
        data = ''; rc = 0
        if argv[0] == 'findmnt':
            rc = 0 if self.active else 1
            data = json.dumps({'filesystems': [{'target': str(self.mount), 'source': '/dev/loop46', 'fstype': 'ext4', 'uuid': self.request['uuid']}]})
        elif argv[0] == 'losetup':
            data = json.dumps({'loopdevices': [{'name': '/dev/loop46', 'back-file': str(self.image)}] if self.active else []})
        elif argv[0] == 'mount': self.active = True
        elif argv[0] == 'umount':
            self.active = False
            (self.mount/'longhorn-disk.cfg').unlink(missing_ok=True)
        elif argv[0] != 'mkfs.ext4': raise AssertionError(argv)
        return subprocess.CompletedProcess(argv, rc, data, '')

    def call(self, action): return MODULE.host_filesystem(self.request, action, self.root)

    def test_prepare_check_native_identity_and_exact_cleanup_preserve_fstab(self):
        saved = self.call('prepare'); self.assertEqual('loop-test-fixture', saved['kind'])
        native = str(uuid.uuid4()); (self.mount/'longhorn-disk.cfg').write_text(json.dumps({'diskUUID': native}))
        self.assertEqual(native, self.call('disk-identity')['disk_uuid'])
        self.assertEqual(saved, self.call('check'))
        self.assertTrue(self.call('remove')['removed']); self.assertFalse(self.image.exists())
        self.assertEqual('# original\n', (self.root/'etc/fstab').read_text())
        self.assertEqual(1, sum(c[0] == 'mkfs.ext4' for c in self.commands))

    def test_unassociated_loop_json_requires_explicit_list_mode(self):
        self.call('prepare')
        original=self.command
        def native_semantics(argv,**kwargs):
            if argv[0]=='losetup' and '-j' in argv and '-l' not in argv:
                return subprocess.CompletedProcess(argv,0,'','')
            return original(argv,**kwargs)
        with patch('subprocess.run',side_effect=native_semantics):
            self.assertTrue(self.call('remove')['removed'])
        self.assertFalse(self.image.exists())

    def test_missing_data_parent_is_owned_and_removed_after_real_directory_creation(self):
        (self.root/'data').rmdir()
        self.call('prepare')
        self.assertTrue(self.mount.is_dir())
        self.call('remove')
        self.assertFalse((self.root/'data').exists())

    def test_created_parent_preserves_new_foreign_contents_and_preexisting_parent(self):
        (self.root/'data').rmdir();self.call('prepare')
        foreign=self.root/'data/foreign';foreign.write_text('preserve')
        self.call('remove');self.assertEqual('preserve',foreign.read_text())
        self.assertTrue((self.root/'data').is_dir())

    def test_created_parent_replacement_refuses_before_unmount_or_deletion(self):
        (self.root/'data').rmdir();self.call('prepare')
        original=self.root/'data';original.rename(self.root/'old-data');original.mkdir()
        before=list(self.commands)
        with self.assertRaisesRegex(ValueError,'parent directory changed'):self.call('remove')
        self.assertEqual(before,self.commands);self.assertTrue(self.image.exists())

    def test_unknown_parent_creation_response_never_adopts_or_deletes_directory(self):
        self.call('prepare')
        record=self.image.with_name('owner.json');row=json.loads(record.read_text());row['parent_intent']=True;record.write_text(json.dumps(row))
        before=list(self.commands)
        with self.assertRaisesRegex(ValueError,'unconfirmed'):self.call('remove')
        self.assertEqual(before,self.commands);self.assertTrue(self.image.exists())

    def test_never_created_intent_is_safe_noop_but_unknown_mount_is_rejected(self):
        self.assertTrue(self.call('remove')['never_created'])
        self.mount.mkdir()
        with self.assertRaisesRegex(ValueError, 'missing.*ownership'): self.call('remove')

    def test_foreign_fstab_or_receipt_prevents_first_mutation(self):
        (self.root/'etc/fstab').write_text('/foreign '+str(self.mount)+' ext4 defaults 0 0\n')
        with self.assertRaisesRegex(ValueError, 'foreign'): self.call('prepare')
        self.assertEqual([], self.commands)

    def test_machine_replacement_rejected_before_subprocess(self):
        self.request['machine_id'] = 'other'
        with self.assertRaisesRegex(ValueError, 'host or capacity'): self.call('prepare')
        self.assertEqual([], self.commands)

    def test_image_inode_replacement_blocks_cleanup(self):
        self.call('prepare')
        original = self.image.with_name('original'); self.image.rename(original)
        self.image.write_bytes(b'foreign')
        with self.assertRaisesRegex(ValueError, 'replaced'): self.call('remove')
        self.assertEqual(b'foreign', self.image.read_bytes()); self.assertTrue(original.exists())

    def test_product_receipt_blocks_host_release(self):
        self.call('prepare'); p = self.root/'etc/kubeauto'; p.mkdir()
        (p/'longhorn-disk.json').write_text('{}')
        with self.assertRaisesRegex(ValueError, 'receipt remains'): self.call('remove')
        self.assertTrue(self.active); self.assertTrue(self.image.exists())

    def test_prepare_failure_before_image_creation_can_be_cleaned(self):
        with patch('os.open', side_effect=OSError('injected')):
            with self.assertRaises(OSError): self.call('prepare')
        # Directory without an ownership record is retained, not guessed owned.
        with self.assertRaisesRegex(ValueError, 'missing.*ownership'): self.call('remove')

    def test_fstab_change_keeps_unrelated_admin_entry(self):
        self.call('prepare')
        with (self.root/'etc/fstab').open('a') as stream: stream.write('/admin /srv ext4 defaults 0 0\n')
        self.call('remove')
        self.assertIn('/admin /srv', (self.root/'etc/fstab').read_text())


class NativeRecoveryContracts(unittest.TestCase):
    def setUp(self):
        self.disk = {'path': MODULE.MOUNT, 'diskType': 'filesystem', 'allowScheduling': True}
        self.node = {'metadata': {'uid': 'node', 'name': 'longhorn-node-146'},
                     'spec': {'disks': {'raw': {'path': '/dev/disk/by-id/original'}, 'kubeauto-data': self.disk}},
                     'status': {'diskStatus': {'kubeauto-data': {'diskUUID': 'native'}}}}
        self.row = {'host_intents': ['192.168.47.146'], 'native_disks': {'longhorn-node-146':
                    {'uid': 'node', 'disks': {'raw': {'path': '/dev/disk/by-id/original'}}}},
                    'v1_disks': {}, 'disk_uuids': {'v1': {}}}
        self.lab = SimpleNamespace(get=Mock(side_effect=lambda kind, *a: self.node if kind == 'nodes.longhorn.io' else None))

    def test_partial_product_apply_recovers_native_disk_using_independent_host_uuid(self):
        with patch.object(MODULE, 'host', return_value={'disk_uuid': 'native'}), patch.object(MODULE, 'persist'):
            MODULE.capture(self.lab, self.row)
        self.assertEqual('native', self.row['v1_disks']['longhorn-node-146']['uuid'])
        self.assertEqual('node', self.row['v1_disks']['longhorn-node-146']['node_uid'])

    def test_replaced_node_or_native_uuid_never_adopted(self):
        for field in ('node', 'disk'):
            with self.subTest(field=field):
                node = copy.deepcopy(self.node)
                if field == 'node': node['metadata']['uid'] = 'replacement'
                self.lab.get.side_effect = lambda kind, *a: node if kind == 'nodes.longhorn.io' else None
                with patch.object(MODULE, 'host', return_value={'disk_uuid': 'other'}), patch.object(MODULE, 'persist'):
                    with self.assertRaises(ValueError): MODULE.capture(self.lab, self.row)
                self.assertEqual({}, self.row['v1_disks'])

    def test_foreign_raw_change_blocks_recovery(self):
        self.node['spec']['disks']['raw']['path'] = '/foreign'
        with patch.object(MODULE, 'host') as host:
            with self.assertRaisesRegex(ValueError, 'raw declaration'): MODULE.capture(self.lab, self.row)
            host.assert_not_called()

    def test_cleanup_capture_accepts_only_original_owned_disk_after_scheduling_disable(self):
        with patch.object(MODULE,'host',return_value={'disk_uuid':'native'}),patch.object(MODULE,'persist'):
            MODULE.capture(self.lab,self.row)
            original=copy.deepcopy(self.row['v1_disks'])
            self.node['spec']['disks']['kubeauto-data']['allowScheduling']=False
            with self.assertRaises(ValueError):MODULE.capture(self.lab,self.row)
            MODULE.capture(self.lab,self.row,retiring=True)
            self.assertEqual(original,self.row['v1_disks'])
            self.node['spec']['disks']['kubeauto-data']['storageReserved']=1
            with self.assertRaises(ValueError):MODULE.capture(self.lab,self.row,retiring=True)

    def test_cleanup_capture_does_not_adopt_an_unknown_disabled_disk(self):
        self.node['spec']['disks']['kubeauto-data']['allowScheduling']=False
        with patch.object(MODULE,'host') as host,patch.object(MODULE,'persist'):
            with self.assertRaises(ValueError):MODULE.capture(self.lab,self.row,retiring=True)
            host.assert_not_called()


class RefusalContracts(unittest.TestCase):
    def test_declared_refusals_hit_the_exact_product_validation_and_restore_input(self):
        from tests.unit.test_longhorn_v2 import v2_configuration, product
        original = v2_configuration()
        workers = tuple(original['longhorn_v2_nodes'])
        encoded = yaml.safe_dump(original).encode()
        config = MODULE.desired(encoded, workers)
        product.validate(config)
        with tempfile.TemporaryDirectory() as directory:
            import base64, hashlib
            path = Path(directory)/'config.yml'; path.write_bytes(encoded)
            row = {'original_b64': base64.b64encode(encoded).decode(),
                   'original_sha256': hashlib.sha256(encoded).hexdigest()}
            seen = []
            def product_step(pid, step, expected_refusal):
                self.assertEqual((123, 'longhorn-setup'), (pid, step))
                with self.assertRaises(ValueError) as caught: product.validate(yaml.safe_load(path.read_bytes()))
                self.assertEqual(expected_refusal, str(caught.exception))
                seen.append(expected_refusal)
            lab = SimpleNamespace(DIRECTORY=Path(directory), WORKERS=workers, supervise_core=product_step)
            with patch.object(MODULE, 'persist'), patch.object(MODULE, 'identities', return_value={'original': True}):
                MODULE.refusals(lab, row, config, 123)
            self.assertEqual(4, len(seen)); self.assertEqual(encoded, path.read_bytes())


if __name__ == '__main__': unittest.main()


class DiskRetirementConcurrencyContracts(unittest.TestCase):
    def setUp(self):
        self.saved = {'node_uid': 'node', 'name': 'kubeauto-data', 'uuid': 'disk',
                      'spec': {'path': MODULE.MOUNT, 'allowScheduling': True}}
        self.node = {'metadata': {'uid': 'node', 'resourceVersion': '1'},
                     'spec': {'disks': {'kubeauto-data': copy.deepcopy(self.saved['spec'])}},
                     'status': {'diskStatus': {'kubeauto-data': {'diskUUID': 'disk'}}}}
        self.replicas = []; self.calls = []; self.fail = None
        def get(kind, *args, **kwargs):
            return copy.deepcopy(self.node) if kind == 'nodes.longhorn.io' else {'items': copy.deepcopy(self.replicas)}
        def apply(*args, **kwargs):
            operations = json.loads(kwargs['stdin']); self.calls.append(operations)
            action = operations[-1]['op']
            if self.fail and self.fail(action):
                raise ValueError('fixture command failed: kubectl rc=1')
            for op in operations:
                if op['op'] != 'test': continue
                current = self.node
                for key in op['path'].strip('/').split('/'): current = current[key]
                if current != op['value']: raise ValueError('fixture command failed: kubectl rc=1')
            if action == 'replace': self.node['spec']['disks']['kubeauto-data']['allowScheduling'] = False
            else: self.node['spec']['disks'].pop('kubeauto-data')
            self.bump()
        self.lab = SimpleNamespace(get=Mock(side_effect=get), kubectl=Mock(side_effect=apply))
    def bump(self):
        self.node['metadata']['resourceVersion'] = str(int(self.node['metadata']['resourceVersion']) + 1)
    def retire(self): MODULE.retire_empty_disk(self.lab, 'name', self.saved)
    def test_status_resource_version_race_revalidates_disable_and_remove(self):
        for action in ('replace', 'remove'):
            with self.subTest(action=action):
                self.setUp(); failed = []
                def race(current):
                    if current == action and not failed:
                        failed.append(True); self.bump(); return True
                    return False
                self.fail = race; self.retire()
                self.assertNotIn('kubeauto-data', self.node['spec']['disks'])
                self.assertEqual(3, len(self.calls))
                self.assertTrue(self.saved['spec']['allowScheduling'])
    def test_unchanged_version_rejection_is_not_retried(self):
        self.fail = lambda action: True
        with self.assertRaisesRegex(ValueError, 'fixture command failed'): self.retire()
        self.assertEqual(1, len(self.calls)); self.assertTrue(self.node['spec']['disks']['kubeauto-data']['allowScheduling'])
    def test_changed_disk_or_node_is_refused_before_retry_mutation(self):
        for key in ('uid', 'path', 'uuid', 'scheduled', 'replica'):
            with self.subTest(key=key):
                self.setUp()
                def race(action):
                    self.bump()
                    if key == 'uid': self.node['metadata']['uid'] = 'foreign'
                    elif key == 'path': self.node['spec']['disks']['kubeauto-data']['path'] = '/foreign'
                    elif key == 'uuid': self.node['status']['diskStatus']['kubeauto-data']['diskUUID'] = 'foreign'
                    elif key == 'scheduled': self.node['status']['diskStatus']['kubeauto-data']['scheduledReplica'] = {'foreign': 1}
                    else: self.replicas.append({'spec': {'diskID': 'disk'}})
                    return True
                self.fail = race
                with self.assertRaises(ValueError): self.retire()
                self.assertEqual(1, len(self.calls)); self.assertIn('kubeauto-data', self.node['spec']['disks'])
    def test_continuous_resource_version_churn_is_bounded(self):
        def race(action): self.bump(); return True
        self.fail = race
        with self.assertRaises(ValueError): self.retire()
        self.assertEqual(3, len(self.calls)); self.assertIn('kubeauto-data', self.node['spec']['disks'])
    def test_lost_disable_response_resumes_only_the_original_disabled_disk(self):
        once = []
        def race(action):
            if once: return False
            once.append(True); self.node['spec']['disks']['kubeauto-data']['allowScheduling'] = False
            self.bump(); return True
        self.fail = race; self.retire()
        self.assertEqual(2, len(self.calls)); self.assertNotIn('kubeauto-data', self.node['spec']['disks'])


class CleanupCliContracts(unittest.TestCase):
    def test_removed_host_fixture_is_reloaded_before_health_and_cleanup_receipt(self):
        import base64, hashlib
        for fail in (False, True):
            with self.subTest(fail=fail), tempfile.TemporaryDirectory() as value:
                root=Path(value); (root/MODULE.RECORD).write_text('{}')
                raw=b'original'; events=[]
                row={'original_b64':base64.b64encode(raw).decode(), 'original_sha256':hashlib.sha256(raw).hexdigest(),
                     'native_disks':{}, 'host_intents':['192.168.47.146']}
                def remote(node,argv):
                    if argv==['systemctl','daemon-reload']:
                        events.append('reload')
                        if fail:raise OSError('reload failed')
                lab=SimpleNamespace(DIRECTORY=root,get=lambda *a,**kw:None if a[0]=='storageclass' else {'items':[]},
                    owner=lambda:{'cluster_uid':'cluster','node_uids':{'longhorn-node-146':'node'}},remote=remote)
                install=SimpleNamespace(health=lambda lab:events.append('healthy'))
                with patch.object(MODULE,'record',return_value=row),patch.object(MODULE,'restore_business'), \
                        patch.object(MODULE,'capture'),patch.object(MODULE,'native_disks',return_value={}), \
                        patch.object(MODULE,'host',side_effect=lambda *a:events.append('remove')), \
                        patch.object(MODULE,'load',return_value=install),patch.object(MODULE,'persist') as persist:
                    if fail:
                        with self.assertRaisesRegex(OSError,'reload failed'):MODULE.clean(lab,42)
                        persist.assert_not_called();self.assertNotIn('healthy',events)
                    else:
                        MODULE.clean(lab,42);self.assertTrue(row['clean'])
                        self.assertEqual(['remove','reload','healthy'],events)

    def test_cleanup_cli_retires_journal_only_after_owning_cleanup_succeeds(self):
        for fail in (False, True):
            with self.subTest(fail=fail), tempfile.TemporaryDirectory() as value:
                directory = Path(value); path = directory/MODULE.RECORD; path.write_text('original-receipt')
                lab = SimpleNamespace(DIRECTORY=directory, require_lease=Mock())
                install = SimpleNamespace(fixture=Mock(return_value=(None, lab)))
                def clean(actual, pid):
                    self.assertIs(lab, actual); self.assertEqual(42, pid)
                    self.assertEqual('original-receipt', path.read_text())
                    if fail: raise ValueError('unrecovered owned state')
                with patch.object(MODULE, 'load', return_value=install), patch.object(MODULE, 'clean', side_effect=clean), patch('sys.argv', ['driver', 'clean', '--relay-pid', '42']):
                    if fail:
                        with self.assertRaisesRegex(ValueError, 'unrecovered'): MODULE.main()
                        self.assertTrue(path.exists())
                    else:
                        MODULE.main(); self.assertFalse(path.exists())
                lab.require_lease.assert_called_once()
