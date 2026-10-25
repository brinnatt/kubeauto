"""Deterministic contracts for the isolated same-subnet LONGHORN-25 topology."""
import copy
import importlib.util
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from tests.unit.test_longhorn_v2_pool import inventory

ROOT = Path(__file__).resolve().parents[2]
CANDIDATE = ROOT/'tests/helpers'
RUN = '11111111-1111-1111-1111-111111111111'


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module


class SameSubnetContracts(unittest.TestCase):
    def setUp(self):
        self.module = load(CANDIDATE/'longhorn-node-local.py', 'local25_contract')

    def test_topology_is_three_data_control_nodes_plus_same_subnet_receiver(self):
        lab = self.module.base_fixture()
        self.assertEqual(self.module.NODES, lab.NODES)
        self.assertEqual(lab.NODES, lab.WORKERS)
        self.assertEqual('longhorn-node-gate', lab.CLUSTER)
        self.assertEqual('/usr/local/kubeauto/clusters/longhorn-node-gate', str(lab.DIRECTORY))
        self.assertNotIn('192.168.122.', lab.inventory())
        self.assertNotIn('clusters/longhorn-v2-gate', lab.inventory())
        self.assertIn('clusters/longhorn-node-gate', lab.inventory())
        self.assertNotIn(self.module.RECEIVER, lab.inventory())
        extended = self.module.lifecycle_module().fixture()
        self.assertEqual((*self.module.NODES, self.module.RECEIVER), extended.NODES)
        original = load(ROOT/'tests/helpers/longhorn-lab.py', 'unchanged_v1')
        self.assertEqual(6, len(original.NODES))
        self.assertTrue(all(ip.startswith('192.168.122.') for ip in original.NODES))

    def released(self):
        address = self.module.NODES[0]
        proof = {'host': address, 'original_raw_disk': {'machine_id': 'a'*32,
            'identity': 'wwid', 'size_bytes': 100*1024**3, 'path': '/dev/disk/by-id/wwn-stable'}}
        collected = {'host': address, 'inventory': {'machine_id': 'a'*32, 'hugepages_2mi': '0'},
            'assessment': {'blockers': [], 'candidates': [{'name': 'sdb', 'identity': 'wwid',
                'size_bytes': 100*1024**3, 'by_id': ['/dev/disk/by-id/wwn-stable']}]}}
        return address, proof, collected

    def test_release_and_actual_raw_identity_are_both_required(self):
        address, proof, collected = self.released()
        result = self.module.admit_released_disk(proof, collected, address, RUN)
        self.assertEqual('/dev/disk/by-id/wwn-stable', result['device'])
        for defect in ('host', 'proof-host', 'machine', 'pages', 'blocker', 'duplicate', 'wwid', 'alias', 'size'):
            p, c = copy.deepcopy(proof), copy.deepcopy(collected)
            if defect == 'host': c['host'] = self.module.RECEIVER
            elif defect == 'proof-host': p['host'] = self.module.RECEIVER
            elif defect == 'machine': c['inventory']['machine_id'] = 'b'*32
            elif defect == 'pages': c['inventory']['hugepages_2mi'] = '1024'
            elif defect == 'blocker': c['assessment']['blockers'] = ['foreign-state']
            elif defect == 'duplicate': c['assessment']['candidates'] *= 2
            else:
                candidate = c['assessment']['candidates'][0]
                candidate[{'wwid': 'identity', 'alias': 'by_id', 'size': 'size_bytes'}[defect]] = {
                    'wwid': 'foreign', 'alias': [], 'size': 1}[defect]
            with self.subTest(defect=defect), self.assertRaises(ValueError):
                self.module.admit_released_disk(p, c, address, RUN)

    def test_real_private_owner_and_config_handoff_accept_three_control_data_nodes(self):
        with tempfile.TemporaryDirectory() as directory:
            module = self.module.lifecycle_module(); lab = module.fixture()
            lab.DIRECTORY = Path(directory); lab.STATE = lab.DIRECTORY/'longhorn-lab-owner.json'
            original_inventory = self.module.base_fixture().inventory()
            state = {'cluster': lab.CLUSTER, 'nodes': list(self.module.NODES), 'run_id': RUN,
                'clean_start_verified': True, 'storage_nodes': list(self.module.NODES)}
            lab.STATE.write_text(json.dumps(state)); (lab.DIRECTORY/'hosts').write_text(original_inventory)
            self.assertEqual(state, lab.owner())
            scenario = module.Lifecycle(lab, 123)
            config = 'longhorn_storage_nodes:\n'+''.join('- '+ip+'\n' for ip in self.module.NODES)
            (lab.DIRECTORY/'config.yml').write_text(config)
            scenario.save({'run_id': RUN, 'host': self.module.RECEIVER, 'name': module.NAME, 'config': config})
            scenario.config(True)
            expanded = module.yaml.safe_load((lab.DIRECTORY/'config.yml').read_text())
            self.assertEqual([*self.module.NODES, self.module.RECEIVER], expanded['longhorn_storage_nodes'])
            scenario.config(False)
            self.assertEqual(list(self.module.NODES), module.yaml.safe_load((lab.DIRECTORY/'config.yml').read_text())['longhorn_storage_nodes'])
            (lab.DIRECTORY/'hosts').write_text(module.extended_inventory(original_inventory))
            self.assertEqual(state, lab.owner()); self.assertEqual(4, len(lab.WORKERS))
            self.assertEqual(state, json.loads(lab.STATE.read_text()))

    def test_disk_preparation_consumes_only_the_admitted_plan_and_run(self):
        lab = SimpleNamespace(owner=lambda: {'run_id': RUN, 'planned_disks': {'host': {'uuid': 'owned'}}})
        original = self.module.checked_disk_plan(lab, '{"host":{"uuid":"owned"}}', RUN)
        original['host']['uuid'] = 'changed'
        self.assertEqual('owned', lab.owner()['planned_disks']['host']['uuid'])
        for raw, run in (('{}', RUN), ('{"host":{"uuid":"owned"}}', 'other')):
            with self.assertRaises(ValueError): self.module.checked_disk_plan(lab, raw, run)

    def test_configuration_reads_all_four_releases_and_hosts_before_creating_owner(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            lab = SimpleNamespace(DIRECTORY=root, STATE=root/'owner.json', require_lease=Mock())
            state = {'run_id': RUN}
            lab.owner = lambda: state
            lab.write_private = lambda path, data: path.write_text(json.dumps(data))
            addresses = []
            def probe(command, **kwargs):
                address = next(part[5:] for part in command if part.startswith('root@'))
                addresses.append(address)
                row = inventory()
                row.update(machine_id='a'*32, hugepages_2mi='0', hostname='original-host')
                return SimpleNamespace(returncode=0, stdout=json.dumps(row))
            def proof(address):
                _, row, _ = self.released(); row['host'] = address
                row['original_raw_disk'].update(identity='0x6000abc', path='/dev/disk/by-id/wwn-0x6000abc')
                return row
            def initialize():
                self.assertEqual([*self.module.NODES, self.module.RECEIVER], addresses)
                (root/'config.yml').write_text('longhorn_storage_nodes: []\n')
            lab.configure = Mock(side_effect=initialize)
            source = Mock(side_effect=lambda path, address: proof(address))
            pool = self.module.load('longhorn-v2-pool.py')
            target = SimpleNamespace(release_proof=Mock(return_value=proof(self.module.RECEIVER)))
            with patch.object(self.module, 'BASE', root), patch.object(self.module, 'base_fixture', return_value=lab), \
                    patch.object(self.module, 'read_source_proof', source), \
                    patch.object(pool.subprocess, 'run', side_effect=probe), \
                    patch.object(self.module, 'load', side_effect=lambda name: pool if name=='longhorn-v2-pool.py' else target):
                self.module.configure('/original/source', '/original/target.json')
            self.assertEqual(list(self.module.NODES), [call.args[1] for call in source.call_args_list])
            target.release_proof.assert_called_once_with('/original/target.json')
            self.assertEqual(set(self.module.NODES), set(state['planned_disks']))
            self.assertEqual(list(self.module.NODES), state['storage_nodes'])
            self.assertEqual(['v1'], self.module.yaml.safe_load((root/'config.yml').read_text())['longhorn_data_engines'])

    def test_existing_sources_verify_their_own_host_and_target_keeps_target_schema(self):
        modules = []
        def dependency(name):
            module = SimpleNamespace(release_proof=Mock(return_value='verified'))
            modules.append((name, module)); return module
        self.module.load = dependency
        self.assertEqual('verified', self.module.read_source_proof('/original/receipt', self.module.NODES[1]))
        self.assertEqual(self.module.NODES[1], modules[0][1].HOST)
        modules[0][1].release_proof.assert_called_once_with('/original/receipt')

    def test_lifecycle_admission_reaches_disk_plan_through_real_receiver_probe(self):
        module = self.module.lifecycle_module()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            scenario = object.__new__(module.Lifecycle)
            scenario.lab = SimpleNamespace(require_lease=Mock(), core_identity=Mock(),
                installation_health=Mock(), owner=lambda: {'run_id': RUN})
            scenario.path = root/'journal.json'
            scenario.workload = SimpleNamespace(path=root/'business.json')
            result = SimpleNamespace(returncode=0, stdout=json.dumps(inventory()))
            with patch.object(module, 'release_proof', return_value='original-proof'), \
                    patch('subprocess.run', return_value=result) as probe, \
                    patch.object(module, 'disk_plan', side_effect=ValueError('reached-original-disk-plan')) as plan:
                with self.assertRaisesRegex(ValueError, '^reached-original-disk-plan$'):
                    scenario.admit('/original/target.json')
            self.assertIn('root@'+self.module.RECEIVER, probe.call_args.args[0])
            self.assertEqual(self.module.RECEIVER, plan.call_args.args[1]['host'])
            # Both admission and post-clean verification load this same scoped
            # dependency. No other host may reach SSH through that adapter.
            pool = module.load('longhorn-v2-pool.py')
            with patch.object(pool.subprocess, 'run') as remote:
                for address in (*self.module.NODES, '192.168.47.129'):
                    with self.assertRaisesRegex(ValueError, 'unauthorized'):
                        pool.collect(address)
                remote.assert_not_called()


if __name__ == '__main__': unittest.main()
