"""Real filesystem publication/retirement and early boot allocation contracts."""
import copy
import hashlib
import importlib.util
import inspect
import json
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch
import uuid

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location('boot_fixture', ROOT/'tests/helpers/longhorn-v2-boot-fixture.py')
B = importlib.util.module_from_spec(SPEC); SPEC.loader.exec_module(B)


class BootFixtureContracts(unittest.TestCase):
    def fixture(self, directory):
        base = Path(directory); identifier = str(uuid.uuid4())
        for name in ('etc', 'proc/sys/kernel/random', 'sys/kernel/mm/hugepages/hugepages-2048kB', 'var/lib', 'etc/systemd/system'):
            (base/name).mkdir(parents=True, exist_ok=True)
        (base/'etc/machine-id').write_text('owned-machine\n')
        (base/'proc/sys/kernel/random/boot_id').write_text('old-boot\n')
        (base/'proc/meminfo').write_text('MemAvailable: 6291456 kB\nHugePages_Rsvd: 0\n')
        (base/'sys/kernel/mm/hugepages/hugepages-2048kB/nr_hugepages').write_text('0\n')
        (base/'sys/kernel/mm/hugepages/hugepages-2048kB/free_hugepages').write_text('0\n')
        units = {name: {'ActiveState': 'inactive', 'LoadState': 'loaded', 'UnitFileState': 'enabled'}
                 for name in ('multipathd.service', 'multipathd.socket')}
        request = {'id': identifier, 'run_id': str(uuid.uuid4()), 'cluster_uid': 'cluster-uid', 'volume_uid': 'volume-uid',
                   'node': B.NODES[0], 'machine_id': 'owned-machine', 'old_boot_id': 'old-boot',
                   'directory': '/var/lib/kubeauto-longhorn-v2-boot-'+identifier,
                   'multipath': {'current': {'units': units}}}
        request['script'] = inspect.getsource(B.allocate)+'\nallocate('+repr({k: v for k, v in request.items() if k != 'multipath'})+')\n'
        request['unit'] = B.unit(request)
        return base, request

    def systemctl(self, base, request, *, kubelet='inactive'):
        def run(args, **kwargs):
            if args[:2] == ['systemctl', 'show']:
                name = args[2]
                if name == 'kubelet.service':
                    dependency = base/'etc/systemd/system/kubelet.service.d'/('50-kubeauto-longhorn-'+request['id']+'.conf')
                    requires = 'kubeauto-longhorn-v2-pages-'+request['id']+'.service' if dependency.exists() else ''
                    return SimpleNamespace(stdout='ActiveState='+kubelet+'\nRequires='+requires)
                mask = base/'etc/systemd/system'/name
                state = {'LoadState': 'masked', 'ActiveState': 'inactive', 'UnitFileState': 'masked'} if mask.is_symlink() else request['multipath']['current']['units'][name]
                return SimpleNamespace(stdout='\n'.join(k+'='+v for k, v in state.items()))
            if args[:3] == ['systemctl', 'is-active', 'kubelet']: return SimpleNamespace(stdout=kubelet, returncode=0)
            self.assertTrue(args[:2] in (['systemctl', 'daemon-reload'], ['systemctl', 'stop']))
            return SimpleNamespace(stdout='', returncode=0)
        return run

    def kernel_write(self, base, pages=1024):
        original = Path.write_text
        def write(path, data, *a, **kw):
            result = original(path, data, *a, **kw)
            if str(path).endswith('/nr_hugepages'):
                original(path, str(pages)+'\n')
                original(path.with_name('free_hugepages'), str(pages)+'\n')
            return result
        return write

    def test_arm_and_cancel_use_inode_bound_persistent_masks_and_restore_exact_files(self):
        with tempfile.TemporaryDirectory() as directory:
            base, request = self.fixture(directory)
            with patch.object(subprocess, 'run', side_effect=self.systemctl(base, request)):
                armed = B.host_fixture(request, 'arm', root=base)
                self.assertEqual(armed['manifest']['phase'], 'armed')
                self.assertIsNone(armed['allocation'])
                files = armed['manifest']['files']
                for name in ('multipathd.service', 'multipathd.socket'):
                    mask = base/'etc/systemd/system'/name
                    self.assertEqual(mask.readlink(), Path('/dev/null'))
                    self.assertIn(str(mask.relative_to(base)).join(('/', '')), files)
                dependency = next(n for n in files if 'kubelet.service.d/' in n)
                content = (base/dependency.lstrip('/')).read_text()
                self.assertIn('Requires=kubeauto-longhorn-v2-pages-', content)
                removed = B.host_fixture(request, 'remove', root=base)
                self.assertTrue(removed['removed'])
                self.assertFalse((base/request['directory'].lstrip('/')).exists())
                self.assertFalse(list((base/'etc/systemd/system').iterdir()))

    def test_retirement_reloads_own_dependency_before_stop_and_preserves_active_kubelet(self):
        with tempfile.TemporaryDirectory() as directory:
            base, request = self.fixture(directory)
            original = self.systemctl(base, request, kubelet='active')
            name='kubeauto-longhorn-v2-pages-'+request['id']+'.service'
            dependency=base/'etc/systemd/system/kubelet.service.d'/('50-kubeauto-longhorn-'+request['id']+'.conf')
            state={'requires':False,'active':'active'};events=[]
            def systemctl(argv,**kwargs):
                events.append(tuple(argv))
                if argv[:2]==['systemctl','daemon-reload']:
                    state['requires']=dependency.exists()
                if argv[:3]==['systemctl','show','kubelet.service']:
                    return SimpleNamespace(stdout='ActiveState='+state['active']+'\nRequires='+
                        (name if state['requires'] else ''))
                if argv[:2]==['systemctl','stop'] and state['requires']:
                    state['active']='inactive'
                return original(argv,**kwargs)
            with patch.object(subprocess,'run',side_effect=systemctl):
                B.host_fixture(request,'arm',root=base)
                result=B.host_fixture(request,'remove',root=base)
            self.assertEqual(state['active'],'active')
            self.assertEqual(result['kubelet_retirement']['before']['ActiveState'],'active')
            self.assertEqual(result['kubelet_retirement']['after']['ActiveState'],'active')
            stop=events.index(('systemctl','stop',name))
            self.assertEqual(events[stop-2],('systemctl','daemon-reload'))
            self.assertFalse((base/request['directory'].lstrip('/')).exists())

    def test_foreign_mask_or_existing_unit_is_never_replaced(self):
        for filename in ('multipathd.service', 'own-unit'):
            with self.subTest(filename=filename), tempfile.TemporaryDirectory() as directory:
                base, request = self.fixture(directory)
                name = filename if filename != 'own-unit' else 'kubeauto-longhorn-v2-pages-'+request['id']+'.service'
                foreign = base/'etc/systemd/system'/name; foreign.symlink_to('/dev/null')
                inode = foreign.lstat().st_ino
                with patch.object(subprocess, 'run', side_effect=self.systemctl(base, request)):
                    with self.assertRaises(ValueError): B.host_fixture(request, 'arm', root=base)
                self.assertEqual(foreign.lstat().st_ino, inode)

    def test_replaced_mask_prevents_all_cleanup_and_preserves_foreign_inode(self):
        with tempfile.TemporaryDirectory() as directory:
            base, request = self.fixture(directory)
            with patch.object(subprocess, 'run', side_effect=self.systemctl(base, request)):
                B.host_fixture(request, 'arm', root=base)
                mask = base/'etc/systemd/system/multipathd.service'; mask.unlink(); mask.symlink_to('/dev/null')
                inode = mask.lstat().st_ino
                with self.assertRaisesRegex(ValueError, 'artifact identity'):
                    B.host_fixture(request, 'remove', root=base)
                self.assertEqual(mask.lstat().st_ino, inode)
                self.assertTrue((base/request['directory'].lstrip('/')/'allocate.py').exists())

    def test_actual_boot_program_records_zero_then_complete_free_allocation_before_kubelet(self):
        with tempfile.TemporaryDirectory() as directory:
            base, request = self.fixture(directory)
            with patch.object(subprocess, 'run', side_effect=self.systemctl(base, request)):
                B.host_fixture(request, 'arm', root=base)
                (base/'proc/sys/kernel/random/boot_id').write_text('new-boot\n')
                with patch.object(Path, 'write_text', new=self.kernel_write(base)):
                    B.allocate(request, root=base)
                observed = B.host_fixture(request, 'inspect', root=base)
                receipt = observed['allocation']
                self.assertEqual(receipt['phase'], 'ready'); self.assertEqual(receipt['original']['total'], 0)
                self.assertEqual(receipt['current']['total'], 1024); self.assertEqual(receipt['current']['free'], 1024)
                self.assertEqual(receipt['current']['boot_id'], 'new-boot')
                with self.assertRaisesRegex(ValueError, 'previous boot allocation intent'):
                    B.allocate(request, root=base)
                self.assertTrue(B.host_fixture(request, 'remove', root=base)['removed'])

    def test_wrong_machine_same_boot_active_kubelet_or_memory_shortage_refuses_before_allocation(self):
        for fault in ('machine', 'same-boot', 'kubelet', 'memory'):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as directory:
                base, request = self.fixture(directory)
                (base/request['directory'].lstrip('/')).mkdir()
                if fault != 'same-boot': (base/'proc/sys/kernel/random/boot_id').write_text('new-boot\n')
                if fault == 'machine': (base/'etc/machine-id').write_text('foreign\n')
                if fault == 'memory': (base/'proc/meminfo').write_text('MemAvailable: 1 kB\nHugePages_Rsvd: 0\n')
                with patch.object(subprocess, 'run', side_effect=self.systemctl(base, request, kubelet='active' if fault == 'kubelet' else 'inactive')):
                    with self.assertRaises(ValueError): B.allocate(request, root=base)
                self.assertEqual((base/'sys/kernel/mm/hugepages/hugepages-2048kB/nr_hugepages').read_text(), '0\n')
                self.assertFalse((base/request['directory'].lstrip('/')/'boot.json').exists())

    def test_partial_allocation_retains_measured_failure_and_never_claims_ready(self):
        with tempfile.TemporaryDirectory() as directory:
            base, request = self.fixture(directory); (base/request['directory'].lstrip('/')).mkdir()
            (base/'proc/sys/kernel/random/boot_id').write_text('new-boot\n')
            with patch.object(subprocess, 'run', side_effect=self.systemctl(base, request)), \
                 patch.object(Path, 'write_text', new=self.kernel_write(base, pages=512)):
                with self.assertRaisesRegex(ValueError, '1024 completely available'): B.allocate(request, root=base)
            receipt = json.loads((base/request['directory'].lstrip('/')/'boot.json').read_text())
            self.assertEqual(receipt['phase'], 'allocation-confirmed'); self.assertEqual(receipt['current']['total'], 512)

    def test_foreign_private_file_blocks_retirement(self):
        with tempfile.TemporaryDirectory() as directory:
            base, request = self.fixture(directory)
            with patch.object(subprocess, 'run', side_effect=self.systemctl(base, request)):
                B.host_fixture(request, 'arm', root=base)
                (base/request['directory'].lstrip('/')/'foreign').write_text('preserve')
                with self.assertRaisesRegex(ValueError, 'foreign file'): B.host_fixture(request, 'remove', root=base)
                self.assertTrue((base/'etc/systemd/system/multipathd.service').is_symlink())

    def test_boot_unit_has_real_dependency_and_systemd_static_syntax(self):
        with tempfile.TemporaryDirectory() as directory:
            base, request = self.fixture(directory); text = B.unit(request)
            self.assertIn('DefaultDependencies=no', text)
            self.assertIn('After=local-fs.target systemd-modules-load.service systemd-sysctl.service', text)
            self.assertIn('Before=sysinit.target kubelet.service docker.service containerd.service cri-docker.service', text)
            unit = base/'fixture.service'; unit.write_text(text)
            result = subprocess.run(['systemd-analyze', 'verify', str(unit)], capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr)
        compile(inspect.getsource(B.allocate), '<boot-allocation>', 'exec')
        compile(inspect.getsource(B.host_fixture), '<boot-host-fixture>', 'exec')

    def controller_fixture(self, directory):
        base, request = self.fixture(directory); node = request['node']
        state = {'run_id': request['run_id'], 'host_state': {'nodes': {node: {'multipath': {
            'original': {'boot_id': 'old-boot'}, 'current': {'boot_id': 'old-boot'}}}}}}
        state_path = base/'state.json'; state_path.write_text(json.dumps(state, indent=3))
        resource_path = base/'resources.json'; resource_path.write_text(json.dumps({'hosts': {node: {'phase': 'ready'}}}, indent=3))
        original = dict(machine_id=request['machine_id'], boot_id='new-boot', total=0, free=0, reserved=0, available_kib=6291456)
        allocated = dict(original, total=1024, free=1024)
        allocation = {'phase': 'ready', 'original': original, 'current': allocated}
        current = dict(allocated, free=0, reserved=0, kubelet_active='active')
        observed = {'boot_id': 'new-boot', 'allocation': allocation}
        row = {'run_id': request['run_id'], 'request': request, 'phase': 'armed',
               'resource_sha256': hashlib.sha256(resource_path.read_bytes()).hexdigest(),
               'state_sha256': hashlib.sha256(state_path.read_bytes()).hexdigest()}
        (base/B.RECORD).write_text(json.dumps(row))
        lab = SimpleNamespace(DIRECTORY=base, STATE=state_path, owner=lambda: json.loads(state_path.read_text()),
            write_private=lambda p, data: p.write_text(json.dumps(data)), get=MagicMock(return_value={'node': 'actual'}))
        resources = SimpleNamespace(record_path=lambda lab: resource_path, read_record=lambda lab: json.loads(resource_path.read_text()),
            probe=MagicMock(return_value=current), product=lambda: SimpleNamespace(check_v2_resources=MagicMock()))
        spec = importlib.util.spec_from_file_location('boot_archive', ROOT/'tests/helpers/longhorn-v2-resources-reboot.py')
        archive = importlib.util.module_from_spec(spec); spec.loader.exec_module(archive)
        common = SimpleNamespace(multipath=MagicMock())
        modules = {'longhorn-v2-resources.py': resources, 'longhorn-v2-resources-reboot.py': archive, 'longhorn-v2-install.py': common}
        journal = {'volume_uid': request['volume_uid']}; proof = {'address': node, 'node': 'longhorn-node-146'}
        return lab, resources, modules, journal, proof, observed

    def test_complete_preserves_boot_and_runtime_measurements_and_retires_only_after_ledger_update(self):
        with tempfile.TemporaryDirectory() as directory:
            lab, resources, modules, journal, proof, observed = self.controller_fixture(directory)
            resource_raw = resources.record_path(lab).read_bytes(); state_raw = lab.STATE.read_bytes(); calls = []
            def remote(lab, request, action):
                calls.append(action)
                if action == 'inspect': return copy.deepcopy(observed)
                self.assertEqual(json.loads((lab.DIRECTORY/B.RECORD).read_text())['phase'], 'remove-intent')
                self.assertEqual(lab.owner()['host_state']['nodes'][proof['address']]['multipath']['current']['boot_id'], 'new-boot')
                return {'removed': True}
            with patch.object(B, 'bound', side_effect=lambda lab, *a: lab.owner()), \
                 patch.object(B, 'load', side_effect=lambda name: modules[name]), patch.object(B, 'remote', side_effect=remote):
                self.assertTrue(B.complete(lab, journal, proof)); self.assertTrue(B.complete(lab, journal, proof))
                self.assertEqual(calls, ['inspect', 'remove'])
                entry = resources.read_record(lab)['hosts'][proof['address']]
                self.assertEqual(entry['original']['total'], 0)
                self.assertEqual(entry['current']['free'], 1024)
                self.assertEqual(entry['reboot_fixture']['runtime_observation']['free'], 0)
                self.assertEqual(entry['reboot_fixture']['boot_allocation']['current']['free'], 1024)
                self.assertEqual(entry['current']['kubelet_active'], 'active')
                root = lab.DIRECTORY; resource_archive = next(root.glob('longhorn-v2-boot-*-resources.json'))
                state_archive = next(root.glob('longhorn-v2-boot-*-host-state.json'))
                self.assertEqual(resource_archive.read_bytes(), resource_raw); self.assertEqual(state_archive.read_bytes(), state_raw)
                B.clean(lab, journal, proof)
                self.assertFalse((root/B.RECORD).exists())
                self.assertEqual(len(list(root.glob('longhorn-v2-boot-fixture-completed-*.json'))), 1)

    def test_incomplete_allocation_or_changed_journal_prevents_mutation_and_retirement(self):
        for fault in ('allocation', 'resource-journal', 'state-journal', 'runtime-pages'):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as directory:
                lab, resources, modules, journal, proof, observed = self.controller_fixture(directory)
                if fault == 'allocation': observed['allocation']['phase'] = 'allocation-confirmed'
                elif fault == 'resource-journal': resources.record_path(lab).write_text('{"changed":true}')
                elif fault == 'state-journal':
                    changed = lab.owner(); changed['changed'] = True; lab.STATE.write_text(json.dumps(changed))
                else: resources.probe.return_value['total'] = 0
                state_raw = lab.STATE.read_bytes(); resource_raw = resources.record_path(lab).read_bytes()
                with patch.object(B, 'bound', side_effect=lambda lab, *a: lab.owner()), \
                     patch.object(B, 'load', side_effect=lambda name: modules[name]), \
                     patch.object(B, 'remote', return_value=observed) as remote:
                    with self.assertRaises(ValueError): B.complete(lab, journal, proof)
                self.assertEqual(remote.call_count, 1)
                self.assertEqual(lab.STATE.read_bytes(), state_raw); self.assertEqual(resources.record_path(lab).read_bytes(), resource_raw)

    def test_remove_lost_response_keeps_durable_intent_and_never_replays(self):
        with tempfile.TemporaryDirectory() as directory:
            lab, resources, modules, journal, proof, observed = self.controller_fixture(directory)
            def remote(lab, request, action):
                if action == 'inspect': return observed
                raise TimeoutError('response lost after remove')
            with patch.object(B, 'bound', side_effect=lambda lab, *a: lab.owner()), \
                 patch.object(B, 'load', side_effect=lambda name: modules[name]), patch.object(B, 'remote', side_effect=remote):
                with self.assertRaises(TimeoutError): B.complete(lab, journal, proof)
                self.assertEqual(json.loads((lab.DIRECTORY/B.RECORD).read_text())['phase'], 'remove-intent')
                with self.assertRaisesRegex(ValueError, 'ambiguous'): B.complete(lab, journal, proof)
                with self.assertRaisesRegex(ValueError, 'ambiguous'): B.clean(lab, journal, proof)

    def test_cancel_before_reboot_removes_fixture_without_changing_resource_or_host_ledger(self):
        with tempfile.TemporaryDirectory() as directory:
            lab, resources, modules, journal, proof, observed = self.controller_fixture(directory)
            state_raw = lab.STATE.read_bytes(); resource_raw = resources.record_path(lab).read_bytes()
            with patch.object(B, 'bound', side_effect=lambda lab, *a: lab.owner()), \
                 patch.object(B, 'remote', side_effect=[{'boot_id': 'old-boot'}, {'removed': True}]):
                B.clean(lab, journal, proof)
            self.assertEqual(lab.STATE.read_bytes(), state_raw); self.assertEqual(resources.record_path(lab).read_bytes(), resource_raw)
            self.assertFalse((lab.DIRECTORY/B.RECORD).exists())

    def test_completed_legacy_fixture_restores_only_clean_owned_kubelet_stop(self):
        for defect in ('none','active','machine','boot','phase','new-retirement','artifact','pages','busy',
                       'reserved','failed','dependency','dropin-proof','multipath'):
            with self.subTest(defect=defect),tempfile.TemporaryDirectory() as directory:
                base,request=self.fixture(directory)
                with patch.object(subprocess,'run',side_effect=self.systemctl(base,request,kubelet='active')):
                    B.host_fixture(request,'arm',root=base)
                    (base/'proc/sys/kernel/random/boot_id').write_text('new-boot\n')
                    removed=B.host_fixture(request,'remove',root=base)
                removed.pop('kubelet_retirement')
                completed={'phase':'complete','run_id':request['run_id'],'request':request,
                    'removed':removed,'current':{'machine_id':request['machine_id'],'boot_id':'new-boot',
                    'kubelet_active':'active'}}
                pages=base/'sys/kernel/mm/hugepages/hugepages-2048kB'
                (pages/'nr_hugepages').write_text('1024\n');(pages/'free_hugepages').write_text('1024\n')
                state={'LoadState':'loaded','ActiveState':'inactive','SubState':'dead','Result':'success','Requires':''}
                if defect=='active':state['ActiveState']='active'
                if defect=='machine':(base/'etc/machine-id').write_text('foreign\n')
                if defect=='boot':(base/'proc/sys/kernel/random/boot_id').write_text('another-boot\n')
                if defect=='phase':completed['phase']='remove-intent'
                if defect=='new-retirement':removed['kubelet_retirement']={}
                if defect=='artifact':(base/'etc/systemd/system/multipathd.service').symlink_to('/dev/null')
                if defect=='pages':(pages/'nr_hugepages').write_text('0\n')
                if defect=='busy':(pages/'free_hugepages').write_text('0\n')
                if defect=='reserved':(base/'proc/meminfo').write_text('HugePages_Rsvd: 1\n')
                if defect=='failed':state.update(ActiveState='failed',Result='exit-code')
                if defect=='dependency':state['Requires']='kubeauto-longhorn-v2-pages-'+request['id']+'.service'
                if defect=='dropin-proof':
                    key=next(n for n in removed['manifest']['files'] if 'kubelet.service.d' in n)
                    removed['manifest']['files'][key]['sha256']='foreign'
                calls=[]
                def systemctl(argv,**kwargs):
                    calls.append(tuple(argv))
                    if argv[:3]==['systemctl','show','kubelet.service']:
                        return SimpleNamespace(stdout='\n'.join(k+'='+v for k,v in state.items()))
                    if argv[:2]==['systemctl','show']:
                        value=copy.deepcopy(request['multipath']['current']['units'][argv[2]])
                        if defect=='multipath':value['ActiveState']='active'
                        return SimpleNamespace(stdout='\n'.join(k+'='+v for k,v in value.items()))
                    self.assertEqual(argv,['systemctl','start','kubelet.service'])
                    state.update(ActiveState='active',SubState='running');return SimpleNamespace(stdout='')
                with patch.object(subprocess,'run',side_effect=systemctl):
                    if defect in ('none','active'):
                        result=B.restore_completed_kubelet(request,completed,root=base)
                        self.assertEqual(result['started'],defect=='none')
                        self.assertEqual(result['after']['ActiveState'],'active')
                    else:
                        with self.assertRaises(ValueError):B.restore_completed_kubelet(request,completed,root=base)
                starts=[a for a in calls if a[:2]==('systemctl','start')]
                self.assertEqual(len(starts),int(defect=='none'))

    def test_scoped_recovery_journals_intent_and_never_starts_new_or_foreign_fixture(self):
        for phase in ('legacy','new','armed','foreign'):
            with self.subTest(phase=phase),tempfile.TemporaryDirectory() as directory:
                lab,resources,modules,journal,proof,observed=self.controller_fixture(directory)
                state=lab.owner();state['cluster_uid']='cluster-uid';lab.STATE.write_text(json.dumps(state))
                proof['original_host']={'machine_id':'owned-machine'}
                path=lab.DIRECTORY/B.RECORD;row=json.loads(path.read_text())
                row.update(phase='armed' if phase=='armed' else 'complete',current={'boot_id':'new-boot'},removed={})
                if phase=='new':row['removed']['kubelet_retirement']={}
                if phase=='foreign':row['request']['volume_uid']='foreign'
                path.write_text(json.dumps(row))
                def remote(*args,**kwargs):
                    current=json.loads(path.read_text())
                    self.assertEqual(current['kubelet_recovery_intent'],{'boot_id':'new-boot'})
                    compile(args[1][-1],'<completed-kubelet-recovery>','exec')
                    return SimpleNamespace(stdout=json.dumps({'started':True,'boot_id':'new-boot'}))
                lab.remote=MagicMock(side_effect=remote)
                with patch.object(B,'bound',side_effect=lambda *a:lab.owner()):
                    if phase=='foreign':
                        with self.assertRaisesRegex(ValueError,'foreign journal'):B.recover_kubelet(lab,journal,proof)
                    else:B.recover_kubelet(lab,journal,proof)
                self.assertEqual(lab.remote.call_count,int(phase=='legacy'))
                if phase=='legacy':self.assertTrue(json.loads(path.read_text())['kubelet_recovery']['started'])

    def test_inactive_kubelet_after_complete_fails_before_native_wait_but_armed_boot_may_wait(self):
        with tempfile.TemporaryDirectory() as directory:
            lab,resources,modules,journal,proof,observed=self.controller_fixture(directory)
            path=lab.DIRECTORY/B.RECORD
            with patch.object(B,'bound',return_value=lab.owner()):
                B.check_inactive_kubelet(lab,journal,proof)
                row=json.loads(path.read_text());row['phase']='complete';path.write_text(json.dumps(row))
                with self.assertRaisesRegex(ValueError,'inactive after completed boot fixture'):
                    B.check_inactive_kubelet(lab,journal,proof)

    def test_binding_requires_durable_proof_and_exact_source_worker_pool(self):
        proof = {'case': 'worker-reboot', 'address': B.NODES[0]}; journal = {'v2_runtime_fault': proof}
        lab = SimpleNamespace(require_lease=MagicMock(), owner=lambda: {'lease': 'owned'}, NODES=B.NODES)
        runtime = SimpleNamespace(validate_intent=MagicMock())
        with patch.object(B, 'load', return_value=runtime):
            self.assertEqual(B.bound(lab, journal, proof), {'lease': 'owned'})
            runtime.validate_intent.assert_called_with(lab, journal, proof)
            with self.assertRaisesRegex(ValueError, 'durable'): B.bound(lab, {}, proof)
            lab.NODES = ('192.168.47.149',)
            with self.assertRaisesRegex(ValueError, 'exact source-pool'): B.bound(lab, journal, proof)


if __name__ == '__main__': unittest.main()
