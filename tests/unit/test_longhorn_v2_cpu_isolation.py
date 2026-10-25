"""Host policy restoration uses real private files; never the test host /proc."""
import copy
import hashlib
import secrets
import shutil
from types import SimpleNamespace
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

HELPER = Path(__file__).resolve().parents[1]/'helpers/longhorn-v2-cpu-isolation.py'
spec = importlib.util.spec_from_file_location('cpu_isolation', HELPER)
cpu = importlib.util.module_from_spec(spec); spec.loader.exec_module(cpu)


class HostPolicy(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name); self.service = 'active'; self.commands = []
        self.request = dict(id='12345678-1234-1234-1234-123456789abc', machine_id='machine', boot_id='boot', spdk_mask='0x03')
        for name, data in {'etc/machine-id':'machine', 'proc/sys/kernel/random/boot_id':'boot',
                'sys/devices/system/cpu/online':'0-7', 'proc/irq/default_smp_affinity':'f0\n',
                'proc/irq/12/smp_affinity':'aa\n', 'proc/irq/12/actions':'virtio0',
                'proc/irq/12/chip_name':'PCI-MSI', 'proc/irq/12/hwirq':'1',
                'sys/devices/virtual/workqueue/cpumask':'f8\n',
                'sys/devices/virtual/workqueue/writeback/cpumask':'c0\n',
                'sys/devices/pci0/net/eth0/queues/rx-0/rps_cpus':'80\n'}.items():
            path = self.root/name; path.parent.mkdir(parents=True, exist_ok=True); path.write_text(data)
        (self.root/'var/lib').mkdir(parents=True)
        link = self.root/'sys/class/net/eth0'; link.parent.mkdir(parents=True)
        link.symlink_to('../../devices/pci0/net/eth0')
        self.patcher = patch.object(subprocess, 'run', side_effect=self.systemctl); self.patcher.start(); self.addCleanup(self.patcher.stop)
        self.original = self.act('capture')

    def systemctl(self, args, **kwargs):
        self.commands.append(args)
        if args[1] == 'stop': self.service = 'inactive'
        if args[1] == 'start': self.service = 'active'
        return subprocess.CompletedProcess(args, 0,
            'LoadState=loaded\nActiveState='+self.service+'\nUnitFileState=enabled\n', '')

    def act(self, action): return cpu.host_masks(self.request, action, root=self.root)

    def native(self, enabled, managed=False):
        value = 'fc\n' if enabled else 'ff\n'
        for path in self.original['original']['masks']:
            if managed and path == '/proc/irq/12/smp_affinity': continue
            (self.root/path.lstrip('/')).write_text(value)
        marker = self.root/'var/lib/longhorn/instance-manager/v2/spdk_cpu_mask'
        if enabled:
            marker.parent.mkdir(parents=True, exist_ok=True); marker.write_text('0x03\n')
        elif marker.exists(): marker.unlink()

    def test_each_custom_mask_restores_after_native_all_online_clear(self):
        self.native(True); self.act('enabled'); self.native(False); self.act('cleared')
        restored = self.act('restore')
        self.assertTrue(restored['original_custom_masks_restored'])
        self.assertEqual(self.service, 'active')
        self.assertEqual([a[1] for a in self.commands if a[1] != 'show'], ['stop','start'])
        for path, row in self.original['original']['masks'].items():
            self.assertEqual((self.root/path.lstrip('/')).read_text(), row['raw'])
        self.assertTrue(next((self.root/'var/lib').glob('*-completed.json')).is_file())

    def test_managed_irq_may_stay_original_but_required_rps_must_converge(self):
        self.native(True, managed=True); self.act('enabled')
        (self.root/'sys/class/net/eth0/queues/rx-0/rps_cpus').write_text('80\n')
        with self.assertRaisesRegex(ValueError, 'RPS'): self.act('enabled')

    def test_foreign_third_mask_blocks_restore_without_writes(self):
        self.native(True); self.act('enabled'); self.native(False); self.act('cleared')
        path = self.root/'proc/irq/12/smp_affinity'; path.write_text('04\n')
        with self.assertRaisesRegex(ValueError, 'foreign CPU mask'): self.act('restore')
        self.assertEqual((self.root/'proc/irq/default_smp_affinity').read_text(), 'ff\n')
        self.assertEqual(self.service, 'inactive')

    def test_replaced_irq_identity_blocks_restore(self):
        self.native(True); self.act('enabled'); self.native(False); self.act('cleared')
        path = self.root/'proc/irq/12/smp_affinity'; path.rename(path.with_name('old'))
        path.write_text('ff\n')
        with self.assertRaisesRegex(ValueError, 'identity changed'): self.act('restore')

    def test_stale_native_marker_blocks_original_policy_restore(self):
        self.native(True); self.act('enabled')
        with self.assertRaisesRegex(ValueError, 'before native clear'): self.act('restore')

    def test_new_topology_rejects_without_rewriting_custom_policy(self):
        path = self.root/'proc/irq/13/smp_affinity'; path.parent.mkdir(); path.write_text('ff\n')
        with self.assertRaisesRegex(ValueError, 'topology changed'): self.act('restore')

    def test_changed_boot_id_blocks_every_mutation(self):
        (self.root/'proc/sys/kernel/random/boot_id').write_text('newboot')
        with self.assertRaisesRegex(ValueError, 'topology changed'): self.act('restore')

    def test_completed_host_reuse_reads_exact_original_policy_without_writes(self):
        self.native(True); self.act('enabled'); self.native(False); self.act('cleared'); self.act('restore')
        path = next((self.root/'var/lib').glob('*-completed.json'))
        request = {'path': '/var/lib/'+path.name, 'sha256': hashlib.sha256(path.read_bytes()).hexdigest(), 'machine_id': 'machine'}
        before = path.read_bytes(); commands = len(self.commands)
        result = cpu.completed_host(request, root=self.root)
        self.assertTrue(result['original_custom_masks_restored']); self.assertEqual(before, path.read_bytes())
        self.assertEqual(['show'], [a[1] for a in self.commands[commands:]])
        (self.root/'proc/irq/default_smp_affinity').write_text('ff\n')
        with self.assertRaisesRegex(ValueError, 'mask differs'): cpu.completed_host(request, root=self.root)


class ReusePolicy(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name); self.stage = Path('/var/tmp/kubeauto-longhorn-v2-observability.'+secrets.token_hex(8))
        self.stage.mkdir(); self.addCleanup(shutil.rmtree, self.stage)
        self.proof = json.loads((HELPER.parents[1]/'evidence/longhorn-v2-cpu-isolation-compatible-20261022.json').read_text())
        node = '192.168.47.146'; self.config = self.directory/'config.yml'; self.config.write_text('original config\n')
        self.owner = {name: copy.deepcopy(value) for name, value in self.proof['owner'].items()}
        self.manager = {'metadata': {'name': 'im', 'uid': 'uid'}, 'spec': {'nodeID': 'longhorn-node-146', 'image': 'pin'}, 'status': {'currentState': 'running'}}
        self.pod = {'metadata': {'ownerReferences': [{'kind': 'InstanceManager', 'uid': 'uid', 'controller': True}]},
            'spec': {'nodeName': 'longhorn-node-146', 'containers': [{'args': ['--longhorn-control-path', '/var/lib/longhorn']}]},
            'status': {'conditions': [{'type': 'Ready', 'status': 'True'}]}}
        names = ['override-false-over-global-true', 'empty-inherits-global-true', 'global-disabled-native-clear',
                 'override-true-over-global-false', 'empty-inherits-global-false', 'cleanup-disabled']
        final = {'im': {'uid': 'uid', 'node': 'longhorn-node-146', 'args': self.pod['spec']['containers'][0]['args']}}
        observations = [{'stage': name, 'managers': final} for name in names]
        self.done = {'config_sha256': hashlib.sha256(self.config.read_bytes()).hexdigest(), 'observations': observations}
        self.completed = self.directory/self.proof['completed']['name']; self.completed.write_text(json.dumps(self.done))
        self.proof['completed']['sha256'] = hashlib.sha256(self.completed.read_bytes()).hexdigest()
        lines = ['LONGHORN_V2_CPU_ISOLATION_OBSERVED '+json.dumps(row) for row in observations]
        lines += ['LONGHORN_V2_CPU_ISOLATION_PASS', 'LONGHORN_V2_CPU_ISOLATION_SCOPED_CLEAN_PASS', 'LONGHORN_V2_OBSERVABILITY_STAGE_EXIT rc=0']
        for name, text in {'cpu-isolation.log': '\n'.join(lines)+'\n', 'cpu-isolation.exit': '0\n', 'cpu-isolation.finalized': '0\n'}.items():
            (self.stage/name).write_text(text)
        self.proof['remote_stage'] = {'directory': str(self.stage), 'files': {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in self.stage.iterdir()}}
        self.proof['hosts'] = {node: self.proof['hosts'][node]}
        self.evidence = self.directory/'evidence.json'; self.save()
        self.lab = SimpleNamespace(BASE=HELPER.parents[2], DIRECTORY=self.directory, NODES=(node,), owner=Mock(return_value=self.owner),
            get=Mock(side_effect=lambda kind,*a: {'value': '{"v2":"false"}'} if kind == 'settings.longhorn.io' else self.pod),
            installation_health=Mock(), remote=Mock(return_value=SimpleNamespace(stdout='{"original_custom_masks_restored":true}')))
        self.patcher = patch.object(cpu, 'empty', return_value=[self.manager]); self.patcher.start(); self.addCleanup(self.patcher.stop)

    def save(self): self.evidence.write_text(json.dumps(self.proof))
    def verify(self): cpu.verify_completed(self.lab, 123, evidence=self.evidence)

    def test_bound_original_scope_and_current_installation_are_reused_without_business(self):
        self.verify(); self.lab.installation_health.assert_called_once(); self.lab.remote.assert_called_once()
        self.assertIn('completed_host', self.lab.remote.call_args.args[1][-1])
        self.assertNotIn('host_masks(', self.lab.remote.call_args.args[1][-1])

    def test_missing_or_symlink_evidence_refuses_before_host_reads(self):
        self.evidence.unlink()
        with self.assertRaisesRegex(ValueError, 'missing fixed'): self.verify()
        self.evidence.symlink_to(self.config)
        with self.assertRaisesRegex(ValueError, 'missing fixed'): self.verify()
        self.lab.remote.assert_not_called()

    def test_nullable_source_reuse_refuses_any_other_product_change(self):
        name = 'roles/longhorn/files/longhorn.py'
        product = self.directory/name; product.parent.mkdir(parents=True)
        product.write_bytes((self.lab.BASE/name).read_bytes())
        self.lab.BASE = self.directory
        self.verify()
        self.lab.remote.reset_mock()
        product.write_text(product.read_text()+'\nUNREVIEWED_CHANGE = True\n')
        with self.assertRaisesRegex(ValueError, 'owning product changed'): self.verify()
        self.lab.remote.assert_not_called()

    def test_default_selects_current_boot_evidence_and_still_checks_its_scope(self):
        helper=self.directory/'helpers/longhorn-v2-cpu-isolation.py';helper.parent.mkdir()
        evidence=self.directory/'evidence/longhorn-v2-cpu-isolation-complete-20261023.json';evidence.parent.mkdir()
        evidence.write_text(json.dumps({'status':'pending'}))
        with patch.object(cpu,'__file__',str(helper)):
            with self.assertRaisesRegex(ValueError,'invalid fixed CPU reuse evidence scope'):cpu.verify_completed(self.lab,1)
        self.lab.remote.assert_not_called()

    def test_foreign_owner_and_changed_owning_function_refuse(self):
        original = copy.deepcopy(self.proof)
        for change in ('owner', 'function'):
            self.proof = copy.deepcopy(original)
            if change == 'owner': self.proof['owner']['cluster_uid'] = 'foreign'
            else: self.proof['source']['functions']['host_masks'] = '0'*64
            self.save()
            with self.assertRaisesRegex(ValueError, 'owner differs|function changed'): self.verify()
        self.lab.remote.assert_not_called()

    def test_corrupt_durable_receipt_and_active_fixture_residue_refuse(self):
        (self.stage/'cpu-isolation.finalized').write_text('1\n')
        with self.assertRaisesRegex(ValueError, 'durable evidence differs'): self.verify()
        (self.stage/'cpu-isolation.finalized').write_text('0\n')
        (self.directory/cpu.RECORD).write_text('{}')
        with self.assertRaisesRegex(ValueError, 'active fixture'): self.verify()
        self.lab.remote.assert_not_called()

    def test_changed_IM_native_policy_or_configuration_refuses(self):
        for change in ('im', 'args', 'config'):
            with self.subTest(change=change):
                if change == 'im': self.manager['metadata']['uid'] = 'foreign'
                if change == 'args': self.pod['spec']['containers'][0]['args'].append('--enable-rps')
                if change == 'config': self.config.write_text('foreign config')
                with self.assertRaisesRegex(ValueError, 'identity|arguments|configuration'): self.verify()
                self.manager['metadata']['uid'] = 'uid'; self.pod['spec']['containers'][0]['args'] = ['--longhorn-control-path', '/var/lib/longhorn']
        self.lab.remote.assert_not_called()



class NativePolicy(unittest.TestCase):
    def test_override_has_precedence_and_empty_inherits(self):
        for global_value in (True, False):
            self.assertEqual(cpu.effective(global_value, ''), global_value)
            self.assertTrue(cpu.effective(global_value, 'true'))
            self.assertFalse(cpu.effective(global_value, 'false'))
        with self.assertRaises(ValueError): cpu.effective(True, 'TRUE')
        with self.assertRaises(ValueError): cpu.effective('false', '')

    def test_native_missing_empty_field_is_only_normalized_difference(self):
        from types import SimpleNamespace
        manager={'metadata':{'name':'im','uid':'uid'},'spec':{'nodeID':'longhorn-node-146','image':'pin','dataEngine':'v2'},
            'status':{'currentState':'running'}}
        pod={'metadata':{'uid':'pod','ownerReferences':[{'kind':'InstanceManager','uid':'uid','controller':True}]},
            'spec':{'nodeName':'longhorn-node-146','containers':[{'image':'pin','args':['--longhorn-control-path','/var/lib/longhorn']}]},
            'status':{'conditions':[{'type':'Ready','status':'True'}]}}
        lab=SimpleNamespace(get=lambda *a:pod)
        row={'managers':{'im':{'uid':'uid','spec':copy.deepcopy(manager['spec'])}},'expected_overrides':{'im':''}}
        with patch.object(cpu,'empty',return_value=[manager]):
            self.assertFalse(cpu.managers(lab,row,False)['im']['enabled'])
            manager['spec']['unrelated']='change'
            with self.assertRaisesRegex(ValueError,'unrelated'): cpu.managers(lab,row,False)

    def test_all_three_flags_required_and_duplicate_rejected(self):
        self.assertEqual(len(cpu.FLAGS),3)
        source=HELPER.read_text()
        self.assertIn('args.count(flag) != int(enabled)',source)
        self.assertIn('host.get(\'restored\')',source)


if __name__ == '__main__': unittest.main()

class FilesystemImpactReuse(unittest.TestCase):
 def test_compatible_cpu_record_retains_original_scope_and_exact_source_delta(self):
  import ast
  root=HELPER.parents[2]
  current=json.loads((root/'tests/evidence/longhorn-v2-cpu-isolation-compatible-20261022.json').read_text())
  prior_ref=current['source']['prior_evidence'];prior_path=root/prior_ref['path']
  self.assertEqual(prior_ref['sha256'],hashlib.sha256(prior_path.read_bytes()).hexdigest())
  prior=json.loads(prior_path.read_text());review_ref=current['source']['compatibility_review'];review_path=root/review_ref['path']
  self.assertEqual(review_ref['sha256'],hashlib.sha256(review_path.read_bytes()).hexdigest())
  source=(root/'roles/longhorn/files/longhorn.py').read_text()
  for variable,key,count in (('status','name',1),('state','disk_name',2)):
   current_line=variable+" = (current.get('status', {}).get('diskStatus') or {}).get("+key+") or {}"
   previous_line=variable+" = current.get('status', {}).get('diskStatus', {}).get("+key+", {})"
   self.assertEqual(count,source.count(current_line));source=source.replace(current_line,previous_line)
  func=next(n for n in ast.parse(source).body if isinstance(n,ast.FunctionDef) and n.name=='reconcile_filesystem_disks')
  original=source.replace(ast.get_source_segment(source,func)+'\n\n\n','').replace(
    'return reconcile_filesystem_disks(c, api, nodes) and reconcile_block_disks(c, api, nodes) and healthy(c, api, nodes)',
    'return reconcile_block_disks(c, api, nodes) and healthy(c, api, nodes)')
  self.assertEqual(prior['source']['files']['roles/longhorn/files/longhorn.py'],hashlib.sha256(original.encode()).hexdigest())
  self.assertEqual(current['source']['files']['roles/longhorn/files/longhorn.py'],hashlib.sha256(source.encode()).hexdigest())
  expected=copy.deepcopy(current)
  expected['source'].pop('prior_evidence');expected['source'].pop('compatibility_review');expected['source']['files']=prior['source']['files']
  self.assertEqual(expected,prior)
  namespace={};exec(ast.get_source_segment(source,func),namespace)
  forbidden=Mock();forbidden.get.side_effect=AssertionError('V2-only path must not touch native disks')
  self.assertTrue(namespace['reconcile_filesystem_disks']({'longhorn_storage_nodes':[]},forbidden,{}));forbidden.get.assert_not_called()
