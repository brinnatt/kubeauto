"""V2 integration contracts; upstream GA does not constitute a live gate PASS."""
import copy
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import yaml

from tests.unit.test_longhorn_delivery import configuration, product, FakeAPI, native, module

block = module('block-check.py')


def v2_configuration(coexist=False):
    c = configuration()
    clients = list(c['longhorn_client_nodes'])
    c.update(longhorn_data_engines=['v1', 'v2'] if coexist else ['v2'],
             longhorn_allow_engine_coexistence=coexist,
             longhorn_storage_nodes=clients if coexist else [],
             longhorn_v2_nodes=clients,
             longhorn_block_disks={address: [{'name': 'data-block',
                 'path': '/dev/disk/by-id/wwn-unit-disk', 'disk_driver': 'aio',
                 'identity': 'unit-disk', 'storage_reserved': 1073741824}]
                 for address in clients})
    c['longhorn_storage_classes'][0]['data_engine'] = 'v2'
    return c


class EngineConfiguration(unittest.TestCase):
    def test_rebuild_qos_is_optional_nonnegative_and_v2_only(self):
        for c in (configuration(), v2_configuration()):
            product.validate(c)
            self.assertNotIn('replicaRebuildingBandwidthLimit', product.chart_values(c)['defaultSettings'])
        for version in ('1.12.0', '1.12.1'):
            c = v2_configuration(); c['longhorn_version'] = version
            for value in (0, 100, '00100'):
                c['longhorn_v2_rebuild_bandwidth_limit'] = value
                product.validate(c)
                self.assertEqual(product.chart_values(c)['defaultSettings']['replicaRebuildingBandwidthLimit'],
                                 {'v2': str(int(value))})
            for value in (-1, True, 1.5, '100 MB/s', 2**63):
                c['longhorn_v2_rebuild_bandwidth_limit'] = value
                with self.subTest(version=version, value=value), self.assertRaises(ValueError):
                    product.validate(c)
        c = configuration(); c['longhorn_v2_rebuild_bandwidth_limit'] = 0
        with self.assertRaisesRegex(ValueError, 'requires enabled V2'):
            product.validate(c)

    def test_official_charts_render_rebuild_qos_and_preserve_unmanaged_default(self):
        helm = shutil.which('helm')
        if not helm:
            self.skipTest('pinned Helm required for render')
        for version in ('1.12.0', '1.12.1'):
            for value in (None, 0, 100):
                c = v2_configuration()
                c.update(longhorn_version=version, longhorn_v2_rebuild_bandwidth_limit=value)
                with self.subTest(version=version, value=value), tempfile.TemporaryDirectory() as directory:
                    values = Path(directory)/'values.json'
                    values.write_text(json.dumps(product.chart_values(c)))
                    rendered = subprocess.check_output([helm, 'template', 'longhorn',
                        str(product.FILES/('longhorn-' + version + '.tgz')), '-n', product.NAMESPACE,
                        '-f', str(values)], text=True)
                    cm = next(obj for obj in yaml.safe_load_all(rendered) if obj and
                              obj['kind'] == 'ConfigMap' and obj['metadata']['name'] == 'longhorn-default-setting')
                    settings = yaml.safe_load(cm['data']['default-setting.yaml'])
                    if value is None:
                        self.assertNotIn('replica-rebuilding-bandwidth-limit', settings)
                    else:
                        self.assertEqual(json.loads(settings['replica-rebuilding-bandwidth-limit']), {'v2': str(value)})

    def test_rebuild_qos_waits_for_native_setting_without_fabricating_rebuild_status(self):
        c = v2_configuration(); api = FakeAPI()
        self.assertTrue(product.rebuild_qos_ready(c, api))
        self.assertFalse(api.reads)
        c['longhorn_v2_rebuild_bandwidth_limit'] = 100
        self.assertFalse(product.rebuild_qos_ready(c, api))
        obj = native('Setting', 'replica-rebuilding-bandwidth-limit')
        for value in ('{"v2":"0"}', '{"v2":"100"}', '{"v1":"100"}'):
            obj['value'] = value
            api.put('settings.longhorn.io', obj, product.NAMESPACE)
            with self.subTest(value=value):
                self.assertEqual(product.rebuild_qos_ready(c, api), value == '{"v2":"100"}')
        c['longhorn_v2_rebuild_bandwidth_limit'] = None
        reads = list(api.reads)
        self.assertTrue(product.rebuild_qos_ready(c, api))
        self.assertEqual(api.reads, reads)
        self.assertFalse(api.writes)

    def test_iobuf_pools_are_versioned_and_share_the_spdk_memory_budget(self):
        c = v2_configuration()
        c.update(longhorn_v2_iobuf_large_pool_size=2048, longhorn_v2_iobuf_small_pool_size=16384)
        product.validate(c)
        settings = product.chart_values(c)['defaultSettings']
        self.assertEqual(settings['dataEngineIobufLargePoolSize'], {'v2': '2048'})
        self.assertEqual(settings['dataEngineIobufSmallPoolSize'], {'v2': '16384'})
        for change in ({'longhorn_v2_iobuf_large_pool_size': 1023},
                       {'longhorn_v2_iobuf_small_pool_size': 8191},
                       {'longhorn_v2_iobuf_small_pool_size': True},
                       {'longhorn_v2_iobuf_large_pool_size': 16384},
                       {'longhorn_version': '1.12.0'}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                product.validate(dict(c, **change))
        legacy = v2_configuration(); legacy['longhorn_version'] = '1.12.0'
        product.validate(legacy)
        self.assertNotIn('dataEngineIobufLargePoolSize', product.chart_values(legacy)['defaultSettings'])
        self.assertNotIn('dataEngineNumberOfCPUCores', product.chart_values(legacy)['defaultSettings'])

    def test_disk_maintenance_flags_are_paired_and_eviction_requires_disabled_scheduling(self):
        c = v2_configuration()
        disk = c['longhorn_block_disks'][c['longhorn_v2_nodes'][0]][0]
        disk.update(allow_scheduling=False, eviction_requested=True)
        product.validate(c)
        declared = product.block_declarations(c, c['longhorn_v2_nodes'][0])['data-block']
        self.assertFalse(declared['allowScheduling'])
        self.assertTrue(declared['evictionRequested'])
        for change in ({'allow_scheduling': True}, {'eviction_requested': 'invalid'}):
            original = dict(disk)
            disk.update(change)
            with self.subTest(change=change), self.assertRaises(ValueError):
                product.validate(c)
            disk.clear()
            disk.update(original)
        disk.pop('allow_scheduling')
        with self.assertRaisesRegex(ValueError, 'paired'):
            product.validate(c)

    def test_exclusive_cores_are_versioned_and_override_mask_budget(self):
        c = v2_configuration()
        c.update(longhorn_v2_cpu_cores='2', longhorn_v2_cpu_mask='0xff')
        product.validate(c)
        self.assertEqual(product.chart_values(c)['defaultSettings']['dataEngineNumberOfCPUCores'], {'v2': '2'})
        node = native('Node', 'worker')
        node['status'] = {'allocatable': {'cpu': '4', 'memory': '16Gi', 'hugepages-2Mi': '2Gi'}}
        # Upstream raises the effective request to the exclusive core count.
        product.check_v2_resources(c, node)
        node['status']['allocatable']['cpu'] = '1'
        with self.assertRaisesRegex(ValueError, 'CPU'):
            product.check_v2_resources(c, node)
        for change in ({'longhorn_version': '1.12.0'}, {'longhorn_v2_cpu_cores': -1},
                       {'longhorn_v2_cpu_cores': True}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                product.validate(dict(c, **change))
        v1 = configuration(); v1['longhorn_v2_cpu_cores'] = 2
        with self.assertRaisesRegex(ValueError, 'V2'):
            product.validate(v1)

    def test_quoted_numeric_configuration_keeps_native_types_and_receipt_matching(self):
        c = v2_configuration()
        c['longhorn_v2_memory_mib'] = '2048'
        c['longhorn_v2_cpu_reservation_percentage'] = '25'
        c['longhorn_min_disk_gib'] = '20'
        context = {'cluster_uid': 'cluster', 'node_uids': {}}
        c['longhorn_block_receipts'] = {}
        for address, disks in c['longhorn_block_disks'].items():
            disks[0]['storage_reserved'] = '1073741824'
            context['node_uids'][address] = 'node-' + address
            c['longhorn_block_receipts'][address] = {
                'cluster_uid': 'cluster', 'node_uid': context['node_uids'][address],
                'disks': {'data-block': dict(disks[0], size_bytes=100 * 1024**3)},
                'platform': {'cpu_mask': '0x3', 'memory_mib': 2048, 'hugepages': True}}
        product.validate(c)
        for address in c['longhorn_block_disks']:
            declaration = product.block_declarations(c, address)['data-block']
            self.assertEqual(declaration['storageReserved'], 1073741824)
            self.assertIsInstance(declaration['storageReserved'], int)
        product.check_block_receipts(c, context)

    def test_v1_compatibility_and_explicit_v2_class(self):
        c = configuration()
        product.validate(c)
        self.assertEqual(product.storage_class(c['longhorn_storage_classes'][0])['parameters']['dataEngine'], 'v1')
        c = v2_configuration()
        product.validate(c)
        self.assertEqual(product.storage_class(c['longhorn_storage_classes'][0])['parameters']['dataEngine'], 'v2')
        settings = product.chart_values(c)['defaultSettings']
        self.assertFalse(settings['v1DataEngine'])
        self.assertTrue(settings['v2DataEngine'])
        self.assertEqual(settings['dataEngineCPUMask'], {'v2': '0x3'})
        self.assertEqual(settings['dataEngineMemorySize'], {'v2': '2048'})
        self.assertEqual(settings['guaranteedInstanceManagerCPU']['v2'], '25')

    def test_invalid_engine_topology_is_rejected(self):
        changes = [
            {'longhorn_data_engines': []}, {'longhorn_data_engines': ['v3']},
            {'longhorn_data_engines': ['v2', 'v2']},
            {'longhorn_data_engines': ['v1', 'v2'], 'longhorn_allow_engine_coexistence': False},
            {'longhorn_storage_nodes': ['192.168.122.212']},
            {'longhorn_v2_nodes': ['outside-inventory']},
            {'longhorn_block_disks': {}},
            {'longhorn_v2_cpu_mask': '0x0'}, {'longhorn_v2_cpu_mask': '3; reboot'},
            {'longhorn_v2_cpu_reservation_percentage': 41},
            {'longhorn_v2_memory_mib': 0},
        ]
        for change in changes:
            c = v2_configuration(); c.update(change)
            with self.subTest(change=change), self.assertRaises(ValueError):
                product.validate(c)

    def test_v1_rejects_ignored_v2_disks_and_class_engine_mismatch(self):
        c = v2_configuration(); c['longhorn_storage_classes'][0].pop('data_engine')
        with self.assertRaisesRegex(ValueError, 'engine'):
            product.validate(c)
        c = v2_configuration(); c['longhorn_data_engines'] = ['v1']
        with self.assertRaises(ValueError):
            product.validate(c)

    def test_raw_disk_input_cannot_select_unstable_paths_or_implicit_driver(self):
        for field, value in [('path', '/dev/sdb'), ('path', '/dev/longhorn/volume'),
                             ('disk_driver', 'auto'), ('identity', ''),
                             ('name', 'kubeauto-data'), ('storage_reserved', -1)]:
            c = v2_configuration()
            c['longhorn_block_disks'][c['longhorn_v2_nodes'][0]][0][field] = value
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                product.validate(c)
        c = v2_configuration()
        disks = c['longhorn_block_disks'][c['longhorn_v2_nodes'][0]]
        disks.append(copy.deepcopy(disks[0]))
        with self.assertRaisesRegex(ValueError, 'duplicate'):
            product.validate(c)

    def test_coexistence_is_explicit_and_retains_independent_classes(self):
        c = v2_configuration(coexist=True)
        old = configuration()['longhorn_storage_classes'][0]
        old['name'] = 'longhorn-v1'
        c['longhorn_storage_classes'].append(old)
        product.validate(c)
        self.assertEqual([product.storage_class(sc)['parameters']['dataEngine']
                          for sc in c['longhorn_storage_classes']], ['v2', 'v1'])

    def test_linked_clone_is_explicit_v2_and_replicated_layout_is_pinned(self):
        c = v2_configuration()
        c['longhorn_storage_classes'][0]['clone_mode'] = 'linked-clone'
        product.validate(c)
        parameters = product.storage_class(c['longhorn_storage_classes'][0])['parameters']
        self.assertEqual(parameters['cloneMode'], 'linked-clone')
        self.assertEqual(parameters['dataLayout.type'], 'replicated')
        self.assertEqual(parameters['frontend'], 'blockdev')
        c = configuration(); c['longhorn_storage_classes'][0]['clone_mode'] = 'linked-clone'
        with self.assertRaisesRegex(ValueError, 'linked-clone'):
            product.validate(c)

    def test_cpu_reservation_and_memory_are_checked_against_actual_node(self):
        c = v2_configuration()
        node = native('Node', 'worker')
        node['status'] = {'allocatable': {'cpu': '8', 'memory': '16Gi', 'hugepages-2Mi': '2Gi'}}
        product.check_v2_resources(c, node)
        for field, value in [('cpu', '7999m'), ('memory', '2Gi'), ('hugepages-2Mi', '1Gi')]:
            changed = copy.deepcopy(node); changed['status']['allocatable'][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                product.check_v2_resources(c, changed)
        c['longhorn_v2_hugepages'] = False
        node['status']['allocatable'].pop('hugepages-2Mi')
        product.check_v2_resources(c, node)

    def test_sparse_cpu_ids_use_host_membership_and_api_capacity_independently(self):
        c = v2_configuration()
        # Eight online CPUs may be numbered 0-3,8-11. Kubernetes reports a
        # quantity, not the maximum CPU ID; the host checker verifies IDs.
        c['longhorn_v2_cpu_mask'] = '0x300'
        node = native('Node', 'worker')
        node['status'] = {'allocatable': {'cpu': '8', 'memory': '16Gi', 'hugepages-2Mi': '2Gi'}}
        product.check_v2_resources(c, node)
        node['status']['allocatable']['cpu'] = '7999m'
        with self.assertRaisesRegex(ValueError, 'reservation'):
            product.check_v2_resources(c, node)

    def test_actual_official_chart_serializes_v2_engine_settings(self):
        helm = shutil.which('helm')
        if not helm:
            self.skipTest('pinned Helm required for render')
        c = v2_configuration()
        with tempfile.TemporaryDirectory() as directory:
            values = Path(directory)/'values.json'
            values.write_text(json.dumps(product.chart_values(c)))
            rendered = subprocess.check_output([helm, 'template', 'longhorn',
                str(product.FILES/'longhorn-1.12.1.tgz'), '-n', product.NAMESPACE,
                '-f', str(values)], text=True)
        cm = next(obj for obj in yaml.safe_load_all(rendered) if obj and
                  obj['kind'] == 'ConfigMap' and obj['metadata']['name'] == 'longhorn-default-setting')
        settings = yaml.safe_load(cm['data']['default-setting.yaml'])
        self.assertEqual(json.loads(settings['data-engine-cpu-mask']), {'v2': '0x3'})
        self.assertEqual(json.loads(settings['guaranteed-instance-manager-cpu'])['v2'], '25')
        self.assertTrue(settings['v2-data-engine'])
        self.assertFalse(settings['v1-data-engine'])
        c.update(longhorn_v2_cpu_cores=2, longhorn_v2_iobuf_large_pool_size=2048,
                 longhorn_v2_iobuf_small_pool_size=16384)
        with tempfile.TemporaryDirectory() as directory:
            values = Path(directory)/'values.json'
            values.write_text(json.dumps(product.chart_values(c)))
            rendered = subprocess.check_output([helm, 'template', 'longhorn',
                str(product.FILES/'longhorn-1.12.1.tgz'), '-n', product.NAMESPACE,
                '-f', str(values)], text=True)
        cm = next(obj for obj in yaml.safe_load_all(rendered) if obj and
                  obj['kind'] == 'ConfigMap' and obj['metadata']['name'] == 'longhorn-default-setting')
        self.assertEqual(json.loads(yaml.safe_load(cm['data']['default-setting.yaml'])[
            'data-engine-number-of-cpu-cores']), {'v2': '2'})
        settings = yaml.safe_load(cm['data']['default-setting.yaml'])
        self.assertEqual(json.loads(settings['data-engine-iobuf-large-pool-size']), {'v2': '2048'})
        self.assertEqual(json.loads(settings['data-engine-iobuf-small-pool-size']), {'v2': '16384'})


class NativeReconciliation(unittest.TestCase):
    def setUp(self):
        self.c = v2_configuration()
        self.api = FakeAPI()
        self.nodes = {address: native('Node', 'worker-' + str(index))
                      for index, address in enumerate(self.c['longhorn_v2_nodes'])}

    def test_selective_activation_does_not_create_a_filesystem_default(self):
        for address in list(self.nodes) + ['control-address']:
            node = self.nodes.get(address, native('Node', 'control'))
            self.api.put('node', node)
            product.bootstrap_node(self.c, self.api, address, node, None)
            metadata = self.api.writes[-1][1][2]['metadata']
            self.assertEqual(metadata['labels']['node.longhorn.io/create-default-disk'], 'false')
            self.assertEqual(metadata['labels']['node.longhorn.io/disable-v2-data-engine'],
                             str(address not in self.nodes).lower())
            self.assertNotIn('annotations', metadata)

    def test_native_additions_preserve_existing_fields_and_reject_name_or_path_collisions(self):
        self.api.put('crd', native('CustomResourceDefinition', 'nodes.longhorn.io'))
        for address, node in self.nodes.items():
            current = native('Node', node['metadata']['name'], {'disks': {}})
            self.api.put('nodes.longhorn.io', current, product.NAMESPACE)
        self.assertTrue(product.reconcile_block_disks(self.c, self.api, self.nodes))
        self.assertEqual(len(self.api.writes), 3)
        desired = self.api.writes[0][1][2]
        self.assertEqual(desired['metadata']['uid'], 'uid-worker-0')
        self.assertEqual(desired['spec']['disks']['data-block']['diskType'], 'block')
        self.assertEqual(desired['spec']['disks']['data-block']['diskDriver'], 'aio')
        self.api.writes.clear()
        first = self.api.objects['nodes.longhorn.io', 'worker-0', product.NAMESPACE]
        existing = copy.deepcopy(desired['spec']['disks'])
        existing['data-block'].update(allowScheduling=False, evictionRequested=True, tags=['native'])
        first['spec']['disks'] = existing
        product.reconcile_block_disks(self.c, self.api, self.nodes)
        self.assertFalse(any(write[1][0:2] == ('nodes.longhorn.io', 'worker-0') for write in self.api.writes))
        first['spec']['disks']['data-block']['path'] = '/dev/disk/by-id/replacement'
        with self.assertRaisesRegex(ValueError, 'identity changed'):
            product.reconcile_block_disks(self.c, self.api, self.nodes)
        first['spec']['disks'] = {'other': desired['spec']['disks']['data-block']}
        with self.assertRaisesRegex(ValueError, 'another native disk'):
            product.reconcile_block_disks(self.c, self.api, self.nodes)

    def test_explicit_disk_eviction_only_changes_flags_and_preserves_administrator_fields(self):
        address = next(iter(self.nodes))
        requested = self.c['longhorn_block_disks'][address][0]
        requested.update(allow_scheduling=False, eviction_requested=True)
        for ip, node in self.nodes.items():
            disks = product.block_declarations(self.c, ip)
            for disk in disks.values():
                disk.update(allowScheduling=True, evictionRequested=False, storageReserved=123, tags=['administrator'])
            self.api.put('nodes.longhorn.io', native('Node', node['metadata']['name'], {'disks': disks}), product.NAMESPACE)
        self.assertTrue(product.reconcile_block_disks(self.c, self.api, self.nodes))
        self.assertEqual(len(self.api.writes), 1)
        change = self.api.writes[0][1][2]
        self.assertEqual(change['spec'], {'disks': {'data-block': {'allowScheduling': False, 'evictionRequested': True}}})
        self.assertEqual(change['metadata']['uid'], 'uid-worker-0')
        requested.pop('allow_scheduling')
        requested.pop('eviction_requested')
        self.api.writes.clear()
        current = self.api.objects['nodes.longhorn.io', 'worker-0', product.NAMESPACE]
        current['spec']['disks']['data-block'].update(allowScheduling=False, evictionRequested=True)
        product.reconcile_block_disks(self.c, self.api, self.nodes)
        self.assertFalse(self.api.writes)  # Omitted flags retain native maintenance.

    def test_disk_scheduling_cannot_be_enabled_until_eviction_is_cancelled(self):
        address = next(iter(self.nodes))
        requested = self.c['longhorn_block_disks'][address][0]
        requested.update(allow_scheduling=True, eviction_requested=False)
        disks = product.block_declarations(self.c, address)
        disks['data-block'].update(allowScheduling=False, evictionRequested=True)
        self.api.put('nodes.longhorn.io', native('Node', 'worker-0', {'disks': disks}), product.NAMESPACE)
        with self.assertRaisesRegex(ValueError, 'cancel'):
            product.reconcile_block_disks(self.c, self.api, self.nodes)
        self.assertFalse(self.api.writes)

    def test_eviction_waits_for_native_accounting_and_all_disk_replica_objects(self):
        state = {'diskUUID': 'disk-uuid', 'storageScheduled': 0, 'scheduledReplica': {}, 'scheduledBackingImage': {}}
        self.assertTrue(product.block_disk_drained(self.api, state))
        self.assertFalse(product.block_disk_drained(self.api, dict(state, diskUUID='')))
        conditions = {'conditions': [{'type': 'Ready', 'status': 'True'},
                                     {'type': 'Schedulable', 'status': 'False'}]}
        self.assertTrue(product.block_disk_ready({'allowScheduling': False}, conditions))
        self.assertFalse(product.block_disk_ready({'allowScheduling': True}, conditions))
        self.assertFalse(product.block_disk_ready({'allowScheduling': False}, {'conditions': []}))
        for field, value in [('storageScheduled', 1), ('scheduledReplica', {'replica': 0}),
                             ('scheduledBackingImage', {'image': 0})]:
            with self.subTest(field=field):
                self.assertFalse(product.block_disk_drained(self.api, dict(state, **{field: value})))
        replica = native('Replica', 'still-deleting', {'diskID': 'disk-uuid', 'nodeID': 'worker-0'})
        replica['metadata']['deletionTimestamp'] = '2026-10-20T00:00:00Z'
        self.api.listing('replicas.longhorn.io', [replica], product.NAMESPACE)
        self.assertFalse(product.block_disk_drained(self.api, state))
        replica['spec']['diskID'] = 'another-disk'
        self.api.listing('replicas.longhorn.io', [replica], product.NAMESPACE)
        self.assertTrue(product.block_disk_drained(self.api, state))
        self.assertFalse(self.api.writes)

    def test_attached_v2_blocks_runtime_changes_and_engine_removal(self):
        self.api.put('crd', native('CustomResourceDefinition', 'volumes.longhorn.io'))
        volume = native('Volume', 'v2-volume', {'dataEngine': 'v2', 'nodeID': 'worker-0'})
        volume['status'] = {'state': 'attached'}
        self.api.listing('volumes.longhorn.io', [volume], product.NAMESPACE)
        keys = {'dataEngineCPUMask': 'data-engine-cpu-mask',
                'dataEngineNumberOfCPUCores': 'data-engine-number-of-cpu-cores',
                'dataEngineIobufLargePoolSize': 'data-engine-iobuf-large-pool-size',
                'dataEngineIobufSmallPoolSize': 'data-engine-iobuf-small-pool-size',
                'guaranteedInstanceManagerCPU': 'guaranteed-instance-manager-cpu',
                'dataEngineMemorySize': 'data-engine-memory-size',
                'dataEngineHugepageEnabled': 'data-engine-hugepage-enabled',
                'dataEngineInterruptModeEnabled': 'data-engine-interrupt-mode-enabled'}
        for key, setting in keys.items():
            obj = native('Setting', setting)
            obj['value'] = json.dumps(product.chart_values(self.c)['defaultSettings'][key])
            self.api.put('settings.longhorn.io', obj, product.NAMESPACE)
        product.check_engine_changes(self.c, self.api, self.nodes)
        # Native rebuild QoS is dynamically applied; changing it must not use
        # the detached-only guard for SPDK startup resource settings.
        product.check_engine_changes(dict(self.c, longhorn_v2_rebuild_bandwidth_limit=100),
                                     self.api, self.nodes)
        for field, value in [('longhorn_v2_iobuf_large_pool_size', 2048),
                             ('longhorn_v2_iobuf_small_pool_size', 16384)]:
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, 'detached'):
                product.check_engine_changes(dict(self.c, **{field: value}), self.api, self.nodes)
        self.c['longhorn_v2_cpu_cores'] = 2
        with self.assertRaisesRegex(ValueError, 'detached'):
            product.check_engine_changes(self.c, self.api, self.nodes)
        self.c['longhorn_v2_cpu_cores'] = 0
        self.c['longhorn_v2_memory_mib'] = 4096
        with self.assertRaisesRegex(ValueError, 'detached'):
            product.check_engine_changes(self.c, self.api, self.nodes)
        self.c['longhorn_data_engines'] = ['v1']
        with self.assertRaisesRegex(ValueError, 'disable an engine'):
            product.check_engine_changes(self.c, self.api, self.nodes)

    def test_upgrade_source_reentry_uses_only_its_native_settings(self):
        self.c['longhorn_version'] = '1.12.0'
        self.api.put('crd', native('CustomResourceDefinition', 'volumes.longhorn.io'))
        volume = native('Volume', 'legacy-v2', {'dataEngine': 'v2', 'nodeID': 'worker-0'})
        volume['status'] = {'state': 'attached'}
        self.api.listing('volumes.longhorn.io', [volume], product.NAMESPACE)
        values = {'data-engine-cpu-mask': '0x3', 'guaranteed-instance-manager-cpu': '25',
                  'data-engine-memory-size': '2048', 'data-engine-hugepage-enabled': 'true',
                  'data-engine-interrupt-mode-enabled': 'false'}
        for setting, value in values.items():
            obj = native('Setting', setting); obj['value'] = json.dumps({'v2': value})
            self.api.put('settings.longhorn.io', obj, product.NAMESPACE)
        product.check_engine_changes(self.c, self.api, self.nodes)
        self.assertFalse(self.api.writes)
        observed = {name for kind, name, _ in self.api.reads if kind == 'settings.longhorn.io'}
        self.assertEqual(observed, set(values))

    def test_disk_conflict_refresh_never_adopts_a_replaced_native_node(self):
        original = native('Node', 'worker-0', {'disks': {}})
        replacement = copy.deepcopy(original); replacement['metadata']['uid'] = 'replaced-node'
        with patch.object(self.api, 'get', side_effect=[original, replacement]), \
                patch.object(self.api, 'patch', side_effect=product.CommandError('kubectl', 1, 'Error from server (Conflict): stale')) as apply:
            with self.assertRaisesRegex(ValueError, 'native Node identity changed'):
                product.reconcile_block_disks(self.c, self.api, self.nodes)
        self.assertEqual(apply.call_count, 1)

    def test_node_and_instance_cpu_overrides_cannot_undermine_qualified_budget(self):
        self.api.put('crd', native('CustomResourceDefinition', 'volumes.longhorn.io'))
        node = native('Node', 'worker-0', {'instanceManagerCPURequest': 1000})
        self.api.listing('nodes.longhorn.io', [node], product.NAMESPACE)
        with self.assertRaisesRegex(ValueError, 'CPU override'):
            product.check_engine_changes(self.c, self.api, self.nodes)
        node['spec']['instanceManagerCPURequest'] = 2000
        self.api.listing('nodes.longhorn.io', [node], product.NAMESPACE)
        im = native('InstanceManager', 'v2-im', {'nodeID': 'worker-0', 'dataEngine': 'v2',
                    'dataEngineSpec': {'v2': {'cpuMask': '0xf'}}})
        self.api.listing('instancemanagers.longhorn.io', [im], product.NAMESPACE)
        with self.assertRaisesRegex(ValueError, 'per-instance CPU mask'):
            product.check_engine_changes(self.c, self.api, self.nodes)

    def test_exclusive_cores_require_every_native_node_static_and_budget_rounded_requests(self):
        self.c['longhorn_v2_cpu_cores'] = 2
        self.api.put('crd', native('CustomResourceDefinition', 'volumes.longhorn.io'))
        for node in self.nodes.values():
            node['status'] = {'allocatable': {'cpu': '8', 'memory': '16Gi', 'hugepages-2Mi': '2Gi'}}
        node = native('Node', 'worker-0', {'instanceManagerCPURequest': 9000})
        node['status'] = {'cpuPolicy': 'static'}
        self.api.listing('nodes.longhorn.io', [node], product.NAMESPACE)
        with self.assertRaisesRegex(ValueError, 'CPU'):
            product.check_engine_changes(self.c, self.api, self.nodes)
        node['spec']['instanceManagerCPURequest'] = 1000
        self.api.listing('nodes.longhorn.io', [node], product.NAMESPACE)
        # A smaller override is raised by upstream; a per-IM mask is ignored.
        im = native('InstanceManager', 'v2-im', {'nodeID': 'worker-0', 'dataEngine': 'v2',
                    'dataEngineSpec': {'v2': {'cpuMask': '0xff'}}})
        self.api.listing('instancemanagers.longhorn.io', [im], product.NAMESPACE)
        product.check_engine_changes(self.c, self.api, self.nodes)
        foreign_node = native('Node', 'non-v2-worker')
        for policy in ('none', 'unknown', ''):
            foreign_node['status'] = {'cpuPolicy': policy}
            self.api.listing('nodes.longhorn.io', [node, foreign_node], product.NAMESPACE)
            with self.subTest(policy=policy), self.assertRaisesRegex(ValueError, 'static'):
                product.check_engine_changes(self.c, self.api, self.nodes)
        self.assertFalse(self.api.writes)

    def test_exclusive_pod_requires_native_core_count_and_guaranteed_integer_resources(self):
        self.c['longhorn_v2_cpu_cores'] = 2
        im = native('InstanceManager', 'v2-im', {'nodeID': 'worker-0', 'dataEngine': 'v2', 'image': 'pinned-im'})
        im['status'] = {'dataEngineStatus': {'v2': {'cpuCoreNumber': 2}}}
        pod = native('Pod', 'v2-im', {'nodeName': 'worker-0', 'containers': [{'name': 'instance-manager',
            'image': 'pinned-im', 'args': ['start-spdk-tgt', '--spdk-cpumask', '', '--spdk-core-number', '2',
                '--spdk-memory-size', '2048'], 'resources': {
                'requests': {'cpu': '3', 'memory': '128Mi'},
                'limits': {'cpu': '3', 'memory': '128Mi', 'hugepages-2Mi': '2Gi'}}}]})
        pod['metadata']['ownerReferences'] = [{'kind': 'InstanceManager', 'uid': im['metadata']['uid']}]
        pod['status'] = {'qosClass': 'Guaranteed'}
        self.api.put('pod', pod, product.NAMESPACE)
        self.assertTrue(product.v2_manager_ready(self.c, self.api, im))
        for field, value in [('cpu', '2500m'), ('memory', '256Mi')]:
            changed = copy.deepcopy(pod)
            changed['spec']['containers'][0]['resources']['limits'][field] = value
            self.api.put('pod', changed, product.NAMESPACE)
            self.assertFalse(product.v2_manager_ready(self.c, self.api, im))
        changed = copy.deepcopy(pod)
        for section in ('requests', 'limits'):
            changed['spec']['containers'][0]['resources'][section]['cpu'] = '2500m'
        self.api.put('pod', changed, product.NAMESPACE)
        self.assertFalse(product.v2_manager_ready(self.c, self.api, im))
        self.api.put('pod', pod, product.NAMESPACE)
        im['status']['dataEngineStatus']['v2']['cpuCoreNumber'] = 1
        self.assertFalse(product.v2_manager_ready(self.c, self.api, im))

    def test_actual_instance_manager_pod_must_have_owner_placement_and_cpu_budget(self):
        im = native('InstanceManager', 'v2-im', {'nodeID': 'worker-0', 'dataEngine': 'v2', 'image': 'pinned-im'})
        pod = native('Pod', 'v2-im', {'nodeName': 'worker-0', 'containers': [{'name': 'instance-manager',
            'image': 'pinned-im', 'args': ['start-spdk-tgt', '--spdk-cpumask', '0x3', '--spdk-core-number', '0',
                '--spdk-memory-size', '2048'],
            'resources': {'requests': {'cpu': '2000m'}, 'limits': {'hugepages-2Mi': '2Gi'}}}]})
        pod['metadata']['ownerReferences'] = [{'kind': 'InstanceManager', 'uid': im['metadata']['uid']}]
        self.api.put('pod', pod, product.NAMESPACE)
        self.assertTrue(product.v2_manager_ready(self.c, self.api, im))
        original = copy.deepcopy(pod)
        self.c.update(longhorn_v2_iobuf_large_pool_size=2048, longhorn_v2_iobuf_small_pool_size=16384)
        self.assertFalse(product.v2_manager_ready(self.c, self.api, im))
        pod['spec']['containers'][0]['args'] += [
            '--spdk-iobuf-large-pool-size', '2048', '--spdk-iobuf-small-pool-size', '16384']
        self.api.put('pod', pod, product.NAMESPACE)
        self.assertTrue(product.v2_manager_ready(self.c, self.api, im))
        for key in ('--spdk-iobuf-large-pool-size', '--spdk-iobuf-small-pool-size'):
            wrong = copy.deepcopy(pod)
            args = wrong['spec']['containers'][0]['args']; args[args.index(key) + 1] = '99999'
            self.api.put('pod', wrong, product.NAMESPACE)
            self.assertFalse(product.v2_manager_ready(self.c, self.api, im))
        self.c.update(longhorn_v2_iobuf_large_pool_size=1024, longhorn_v2_iobuf_small_pool_size=8192)
        pod = copy.deepcopy(original)
        self.api.put('pod', pod, product.NAMESPACE)
        legacy = copy.deepcopy(pod)
        legacy['spec']['containers'][0]['args'] = ['start-spdk-tgt', '--spdk-cpumask', '0x03', '--spdk-memory-size', '2048']
        self.api.put('pod', legacy, product.NAMESPACE)
        self.c['longhorn_version'] = '1.12.0'
        self.assertTrue(product.v2_manager_ready(self.c, self.api, im))
        self.c['longhorn_version'] = '1.12.1'
        self.assertFalse(product.v2_manager_ready(self.c, self.api, im))
        for args in (['start-spdk-tgt'], ['start-spdk-tgt', '--spdk-cpumask'],
                     original['spec']['containers'][0]['args'] + ['--spdk-no-hugepage'],
                     original['spec']['containers'][0]['args'] + ['--spdk-interrupt-mode']):
            pod['spec']['containers'][0]['args'] = args
            self.api.put('pod', pod, product.NAMESPACE)
            self.assertFalse(product.v2_manager_ready(self.c, self.api, im))
        pod = copy.deepcopy(original)
        pod['spec']['containers'][0]['resources']['requests']['cpu'] = '1999m'
        self.api.put('pod', pod, product.NAMESPACE)
        self.assertFalse(product.v2_manager_ready(self.c, self.api, im))
        pod['metadata']['ownerReferences'][0]['uid'] = 'replacement'
        self.api.put('pod', pod, product.NAMESPACE)
        with self.assertRaisesRegex(ValueError, 'ownership'):
            product.v2_manager_ready(self.c, self.api, im)

    def test_host_receipt_cannot_be_reused_by_another_node_or_for_different_settings(self):
        context = {'cluster_uid': 'cluster', 'node_uids': {a: n['metadata']['uid'] for a, n in self.nodes.items()}}
        receipts = {a: {'cluster_uid': 'cluster', 'node_uid': n['metadata']['uid'],
            'disks': {'data-block': dict(self.c['longhorn_block_disks'][a][0], size_bytes=100*1024**3)},
            'platform': {'cpu_mask': '0x3', 'memory_mib': 2048, 'hugepages': True}}
            for a, n in self.nodes.items()}
        self.c['longhorn_block_receipts'] = receipts
        product.check_block_receipts(self.c, context)
        address = next(iter(receipts))
        for field, value in [('node_uid', 'replacement'), ('cluster_uid', 'other')]:
            original = receipts[address][field]; receipts[address][field] = value
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, 'another cluster/node'):
                product.check_block_receipts(self.c, context)
            receipts[address][field] = original
        receipts[address]['platform']['memory_mib'] = 4096
        with self.assertRaisesRegex(ValueError, 'resource settings'):
            product.check_block_receipts(self.c, context)

    def test_v2_preflight_precedes_all_host_mutation_and_release_checks_precede_removal(self):
        root = product.FILES.parents[2]
        tasks = yaml.safe_load((root/'roles/longhorn/tasks/host.yml').read_text())
        probe = next(i for i, t in enumerate(tasks) if 'block-check.py --phase preflight' in
                     t.get('ansible.builtin.script', {}).get('cmd', ''))
        policy = next(i for i, t in enumerate(tasks) if 'block-check.py --phase cpu-policy' in
                      t.get('ansible.builtin.script', {}).get('cmd', ''))
        self.assertIn("inventory_hostname in hostvars['localhost'].longhorn_client_nodes", tasks[policy]['when'])
        for index, task in enumerate(tasks):
            if task['name'].startswith(('Install native', 'Load ', 'Persist ')):
                self.assertLess(probe, index)
                self.assertLess(policy, index)
        plays = yaml.safe_load((root/'playbooks/09.longhorn.yml').read_text())
        tasks = plays[-1]['tasks']
        v2_check = next(i for i, t in enumerate(tasks) if '--phase release-check' in
                        t.get('ansible.builtin.script', {}).get('cmd', ''))
        for index, task in enumerate(tasks):
            cmd = task.get('ansible.builtin.script', {}).get('cmd', '')
            if '--release apply' in cmd or '--phase release --' in cmd:
                self.assertLess(v2_check, index)


class RawHostAdmission(unittest.TestCase):
    def test_cpu_policy_cli_is_read_only_and_refuses_nonstatic_checkpoint(self):
        import base64
        state = self.root/'kubelet/cpu_manager_state'
        state.parent.mkdir()
        declaration = base64.b64encode(json.dumps({'kubelet_root': str(state.parent)}).encode()).decode()
        command = [sys.executable, str(product.FILES/'block-check.py'), '--phase', 'cpu-policy',
                   '--declaration', declaration]
        for policy, expected in [('static', 0), ('none', 1)]:
            raw = json.dumps({'policyName': policy})
            state.write_text(raw)
            result = subprocess.run(command, text=True, capture_output=True, timeout=10)
            self.assertEqual(result.returncode, expected, result.stderr)
            self.assertEqual(state.read_text(), raw)
            self.assertFalse(self.marker.exists())
            if not expected:
                self.assertEqual(json.loads(result.stdout), {'cpu_policy': 'static'})

    def test_exclusive_mode_checks_count_instead_of_ignored_mask(self):
        self.c.update(cpu_cores=2, cpu_mask='0x300')
        self.assertEqual(block.platform_receipt(self.c)['cpu_cores'], 2)
        (self.root/'sys/devices/system/cpu/online').write_text('0')
        with self.assertRaisesRegex(ValueError, 'CPU'):
            block.platform_receipt(self.c)

    def test_cpu_policy_reads_the_actual_kubelet_checkpoint_without_changes(self):
        state = self.root/'var/lib/kubelet/cpu_manager_state'
        state.parent.mkdir(parents=True)
        for content in ('{"policyName":"none"}', '{}', 'invalid'):
            state.write_text(content)
            with self.subTest(content=content), self.assertRaises(ValueError):
                block.cpu_policy('/var/lib/kubelet')
            self.assertEqual(state.read_text(), content)
        state.write_text('{"policyName":"static"}')
        self.assertEqual(block.cpu_policy('/var/lib/kubelet'), {'cpu_policy': 'static'})
        state.unlink()
        with self.assertRaises(OSError):
            block.cpu_policy('/var/lib/kubelet')

    def test_quoted_resource_and_disk_numbers_match_integer_receipt(self):
        expected = block.receipt(self.c, self.marker)
        self.c.update(memory_mib='2048', minimum_gib='20')
        self.disk['storage_reserved'] = '0'
        self.assertEqual(block.receipt(self.c, self.marker), expected)
        self.c['memory_mib'] = '4096'
        with self.assertRaisesRegex(ValueError, 'hugepages'):
            block.receipt(self.c, self.marker)
        self.c['memory_mib'] = '2048'
        self.disk['storage_reserved'] = str(100 * 1024**3)
        with self.assertRaisesRegex(ValueError, 'capacity/reservation'):
            block.receipt(self.c, self.marker)

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.path = '/dev/disk/by-id/wwn-unit-disk'
        self.real = self.root/'dev/sdb'
        self.real.parent.mkdir(); self.real.touch()
        link = self.root/self.path.lstrip('/')
        link.parent.mkdir(parents=True); link.symlink_to(self.real)
        for filename, content in {
            'proc/self/mountinfo': '', 'proc/swaps': 'Filename Type Size Used Priority\n',
            'proc/cpuinfo': 'flags : sse4_2\n',
            'proc/meminfo': 'MemTotal: 16777216 kB\nMemAvailable: 8388608 kB\n',
            'sys/kernel/mm/hugepages/hugepages-2048kB/nr_hugepages': '1024',
            'sys/devices/system/cpu/online': '0-7',
        }.items():
            target = self.root/filename
            target.parent.mkdir(parents=True, exist_ok=True); target.write_text(content)
        (self.root/'sys/class/block/sdb/holders').mkdir(parents=True)
        self.disk = {'name': 'raw', 'path': self.path, 'disk_driver': 'aio',
                     'identity': 'unit-disk', 'storage_reserved': 0}
        self.row = {'path': str(self.real), 'type': 'disk', 'size': 100*1024**3,
                    'ro': False, 'wwn': 'unit-disk', 'serial': 'serial', 'maj:min': '0:0'}
        self.signatures = []
        self.commands = []

        def paths(value):
            value = str(value)
            return self.root/value.lstrip('/') if value.startswith(('/dev/', '/sys/', '/proc/', '/var/')) else Path(value)

        def output(argv):
            self.commands.append(argv)
            if argv[0] == 'lsblk': return json.dumps({'blockdevices': [self.row]})
            if argv[0] == 'wipefs': return json.dumps({'signatures': self.signatures})
            if argv[0] == 'modprobe' and argv[1] == '--dry-run': return ''
            raise AssertionError(argv)

        for mocked in (patch.object(block, 'Path', side_effect=paths),
                       patch.object(block.stat, 'S_ISBLK', return_value=True),
                       patch.object(block, 'output', side_effect=output),
                       patch.object(block.platform, 'release', return_value='6.8.0-test'),
                       patch.object(block.platform, 'machine', return_value='x86_64'),
                       patch.object(block.os, 'cpu_count', return_value=8)):
            mocked.start(); self.addCleanup(mocked.stop)
        self.c = {'cluster_uid': 'cluster', 'node_uid': 'node', 'disks': [self.disk],
                  'cpu_mask': '0x3', 'memory_mib': 2048, 'hugepages': True, 'minimum_gib': 20}
        self.marker = self.root/'receipt.json'

    def test_first_use_checks_signatures_and_reentry_binds_hardware_not_kernel_number(self):
        original = block.receipt(self.c, self.marker)
        self.assertTrue(any(command[:2] == ['wipefs', '--no-act'] for command in self.commands))
        self.marker.write_text(json.dumps(original))
        self.signatures = [{'type': 'owned-SPDK-label'}]
        self.commands.clear()
        self.assertEqual(block.receipt(self.c, self.marker), original)
        self.assertFalse(any(command[0] == 'wipefs' for command in self.commands))
        self.row['serial'] = 'replacement'
        with self.assertRaisesRegex(ValueError, 'identity changed'):
            block.receipt(self.c, self.marker)

    def test_foreign_receipt_and_disk_retirement_are_rejected(self):
        original = block.receipt(self.c, self.marker)
        self.marker.write_text(json.dumps(original))
        for change in ({'cluster_uid': 'other'}, {'node_uid': 'other'}, {'disks': []}):
            c = dict(self.c, **change)
            with self.subTest(change=change), self.assertRaises(ValueError):
                block.receipt(c, self.marker)

    def test_signatures_root_mount_swap_partition_and_holders_block_admission(self):
        self.signatures = [{'type': 'ext4'}]
        with self.assertRaisesRegex(ValueError, 'signatures'):
            block.receipt(self.c, self.marker)
        self.signatures.clear()
        files = [('proc/self/mountinfo', '1 0 0:0 / / rw - ext4 /dev/sdb rw\n', 'mounted'),
                 ('proc/swaps', 'Filename Type Size Used Priority\n/dev/sdb partition 1 0 -2\n', 'swap'),
                 ('sys/class/block/sdb/sdb1/partition', '1', 'partition'),
                 ('sys/class/block/sdb/holders/dm-0', '', 'holders')]
        for filename, content, message in files:
            target = self.root/filename
            previous = target.read_text() if target.exists() else None
            target.parent.mkdir(parents=True, exist_ok=True); target.write_text(content)
            with self.subTest(filename=filename), self.assertRaisesRegex(ValueError, message):
                block.receipt(self.c, self.marker)
            if previous is None: target.unlink()
            else: target.write_text(previous)

    def test_wrong_identity_readonly_small_disk_and_aliases_are_rejected(self):
        original = copy.deepcopy(self.row)
        for key, value in [('wwn', 'replacement'), ('ro', True), ('size', 1024), ('type', 'lvm')]:
            self.row = dict(original, **{key: value})
            with self.subTest(field=key), self.assertRaises(ValueError):
                block.receipt(self.c, self.marker)
        self.row = original
        self.c['disks'].append(dict(self.disk, name='alias'))
        with self.assertRaisesRegex(ValueError, 'duplicate aliases'):
            block.receipt(self.c, self.marker)

    def test_platform_rejects_old_kernel_missing_flags_and_unallocated_hugepages(self):
        with patch.object(block.platform, 'release', return_value='5.14.0-570.el9'):
            with self.assertRaisesRegex(ValueError, 'kernel'):
                block.platform_receipt(self.c)
        (self.root/'proc/cpuinfo').write_text('flags : sse4_2\nflags : sse\n')
        with self.assertRaisesRegex(ValueError, 'SSE4.2'):
            block.platform_receipt(self.c)
        (self.root/'proc/cpuinfo').write_text('flags : sse4_2\n')
        pages = self.root/'sys/kernel/mm/hugepages/hugepages-2048kB/nr_hugepages'
        pages.write_text('512')
        with self.assertRaisesRegex(ValueError, 'hugepages'):
            block.platform_receipt(self.c)
        self.c['hugepages'] = False
        self.assertFalse(block.platform_receipt(self.c)['hugepages'])

    def test_release_preserves_media_and_skips_runtime_platform_checks(self):
        original = block.receipt(self.c, self.marker)
        self.marker.write_text(json.dumps(original))
        with patch.object(block, 'platform_receipt', side_effect=AssertionError('runtime probe')):
            result = block.receipt(self.c, self.marker, release=True)
        self.assertEqual(result['disks'], original['disks'])
        self.assertEqual(self.real.read_bytes(), b'')

    def test_cpu_mask_uses_actual_online_ids_including_sparse_and_hotplugged_cpus(self):
        online = self.root/'sys/devices/system/cpu/online'
        for cpus, mask, accepted in [('0-1,3', '0x4', False), ('0-1,3', '0x8', True),
                                     ('8-9', '0x300', True), ('8-9', '0x3', False)]:
            with self.subTest(cpus=cpus, mask=mask):
                online.write_text(cpus)
                self.c['cpu_mask'] = mask
                if accepted:
                    self.assertEqual(block.platform_receipt(self.c)['cpu_mask'], mask)
                else:
                    with self.assertRaisesRegex(ValueError, 'online host CPUs'):
                        block.platform_receipt(self.c)

    def test_missing_or_invalid_online_cpu_inventory_fails_closed(self):
        online = self.root/'sys/devices/system/cpu/online'
        for cpus in ('', '0-3,5-2', '0,x', '-1', '0-1,,3'):
            with self.subTest(cpus=cpus):
                online.write_text(cpus)
                with self.assertRaisesRegex(ValueError, 'online CPU'):
                    block.platform_receipt(self.c)
        online.unlink()
        with self.assertRaises(OSError):
            block.platform_receipt(self.c)


class NativeIdentityContracts(unittest.TestCase):

    def fixture(self, directory):
        values = Path(directory) / 'values.json'
        values.with_name('installation.json').write_text(json.dumps({'cluster_uid': 'cluster', 'namespace_uid': 'namespace'}))
        c = v2_configuration()
        api = FakeAPI()
        nodes = {}
        for i, address in enumerate(c['longhorn_block_disks']):
            node = native('Node', 'worker-' + str(i))
            nodes[address] = node
            obj = copy.deepcopy(node)
            obj['metadata']['uid'] = 'longhorn-' + str(i)
            obj['spec'] = {'disks': product.block_declarations(c, address)}
            obj['status'] = {'diskStatus': {'data-block': {
                'diskType': 'block', 'diskDriver': 'aio', 'diskUUID': 'disk-' + str(i),
                'conditions': [{'type': kind, 'status': 'True'} for kind in ('Ready', 'Schedulable')]}}}
            api.put('nodes.longhorn.io', obj, product.NAMESPACE)
        return (c, api, nodes, values)

    def test_initial_pin_is_private_idempotent_and_allows_native_maintenance(self):
        with tempfile.TemporaryDirectory() as directory:
            c, api, nodes, values = self.fixture(directory)
            path = values.with_name('block-disks.json')
            product.check_block_identity(c, api, nodes, values)
            self.assertFalse(path.exists())
            product.check_block_identity(c, api, nodes, values, record=True)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            before = path.read_bytes()
            stamp = path.stat().st_mtime_ns
            for node in nodes.values():
                obj = api.objects['nodes.longhorn.io', node['metadata']['name'], product.NAMESPACE]
                obj['spec']['disks']['data-block'].update(allowScheduling=False, evictionRequested=True, tags=['admin'])
            product.check_block_identity(c, api, nodes, values)
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(path.stat().st_mtime_ns, stamp)

    def test_replacement_and_retirement_are_rejected_without_overwriting_receipt(self):
        for fault in ('kubernetes-node', 'native-node', 'disk-uuid', 'disk-path', 'disk-driver', 'hardware',
                      'missing-node', 'missing-disk', 'missing-status', 'null-status', 'null-disk',
                      'retire-node', 'retire-disk', 'installation'):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as directory:
                c, api, nodes, values = self.fixture(directory)
                product.check_block_identity(c, api, nodes, values, record=True)
                path = values.with_name('block-disks.json')
                before = path.read_bytes()
                address = next(iter(nodes))
                node = nodes[address]
                obj = api.objects['nodes.longhorn.io', node['metadata']['name'], product.NAMESPACE]
                if fault == 'kubernetes-node':
                    node['metadata']['uid'] = 'replacement'
                elif fault == 'native-node':
                    obj['metadata']['uid'] = 'replacement'
                elif fault == 'disk-uuid':
                    obj['status']['diskStatus']['data-block']['diskUUID'] = 'replacement'
                elif fault == 'disk-path':
                    obj['spec']['disks']['data-block']['path'] = '/dev/disk/by-id/other'
                elif fault == 'disk-driver':
                    obj['spec']['disks']['data-block']['diskDriver'] = 'nvme'
                elif fault == 'hardware':
                    c['longhorn_block_disks'][address][0]['identity'] = 'replacement'
                elif fault == 'missing-node':
                    api.objects.pop(('nodes.longhorn.io', node['metadata']['name'], product.NAMESPACE))
                elif fault == 'missing-disk':
                    obj['spec']['disks'] = {}
                elif fault == 'missing-status':
                    obj['status']['diskStatus'] = {}
                elif fault == 'null-status':
                    obj['status']['diskStatus'] = None
                elif fault == 'null-disk':
                    obj['status']['diskStatus']['data-block'] = None
                elif fault == 'retire-node':
                    c['longhorn_block_disks'].pop(address)
                elif fault == 'retire-disk':
                    c['longhorn_block_disks'][address] = []
                elif fault == 'installation':
                    values.with_name('installation.json').write_text(json.dumps({'cluster_uid': 'other', 'namespace_uid': 'namespace'}))
                with self.assertRaises(ValueError):
                    product.check_block_identity(c, api, nodes, values, record=True)
                self.assertEqual(path.read_bytes(), before)

    def test_addition_requires_new_ready_identity_and_preserves_accepted_disks(self):
        with tempfile.TemporaryDirectory() as directory:
            c, api, nodes, values = self.fixture(directory)
            product.check_block_identity(c, api, nodes, values, record=True)
            address = next(iter(nodes))
            new = copy.deepcopy(c['longhorn_block_disks'][address][0])
            new.update(name='new-block', path='/dev/disk/by-id/new', identity='new')
            c['longhorn_block_disks'][address].append(new)
            product.check_block_identity(c, api, nodes, values)
            with self.assertRaises(ValueError):
                product.check_block_identity(c, api, nodes, values, record=True)
            obj = api.objects['nodes.longhorn.io', nodes[address]['metadata']['name'], product.NAMESPACE]
            obj['spec']['disks'].update(product.block_declarations(c, address))
            obj['status']['diskStatus']['new-block'] = copy.deepcopy(obj['status']['diskStatus']['data-block'])
            obj['status']['diskStatus']['new-block']['diskUUID'] = 'new-uuid'
            product.check_block_identity(c, api, nodes, values, record=True)
            record = json.loads(values.with_name('block-disks.json').read_text())
            self.assertEqual(record['nodes'][address]['disks']['data-block']['disk_uuid'], 'disk-0')
            self.assertEqual(record['nodes'][address]['disks']['new-block']['disk_uuid'], 'new-uuid')

    def test_initial_pin_rejects_duplicate_uuid_and_unready_disk(self):
        for fault in ('duplicate', 'unready'):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as directory:
                c, api, nodes, values = self.fixture(directory)
                obj = api.objects['nodes.longhorn.io', 'worker-1', product.NAMESPACE]
                if fault == 'duplicate':
                    obj['status']['diskStatus']['data-block']['diskUUID'] = 'disk-0'
                else:
                    obj['status']['diskStatus']['data-block']['conditions'][0]['status'] = 'False'
                with self.assertRaises(ValueError):
                    product.check_block_identity(c, api, nodes, values, record=True)
                self.assertFalse(values.with_name('block-disks.json').exists())

    def test_v1_does_not_create_receipt_and_symlink_cannot_grant_ownership(self):
        with tempfile.TemporaryDirectory() as directory:
            c, api, nodes, values = self.fixture(directory)
            c['longhorn_block_disks'] = {}
            product.check_block_identity(c, api, nodes, values, record=True)
            path = values.with_name('block-disks.json')
            self.assertFalse(path.exists())
            path.symlink_to(values.with_name('installation.json'))
            with self.assertRaises(ValueError):
                product.check_block_identity(c, api, nodes, values)

    def test_uninstall_archives_only_original_installation_and_keeps_private_content(self):
        for fault in ('none', 'foreign', 'symlink', 'archive'):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as directory:
                c, api, nodes, values = self.fixture(directory)
                product.check_block_identity(c, api, nodes, values, record=True)
                path = values.with_name('block-disks.json')
                original = path.read_bytes()
                archive = values.with_name('block-disks-uninstalled-namespace.json')
                if fault == 'foreign':
                    row = json.loads(original)
                    row['cluster_uid'] = 'foreign'
                    path.write_text(json.dumps(row))
                elif fault == 'symlink':
                    path.unlink()
                    path.symlink_to(values.with_name('installation.json'))
                elif fault == 'archive':
                    archive.write_text('foreign')
                if fault == 'none':
                    product.retire_block_identity(values)
                    self.assertFalse(path.exists())
                    self.assertEqual(archive.read_bytes(), original)
                    self.assertEqual(archive.stat().st_mode & 0o777, 0o600)
                    product.retire_block_identity(values)
                else:
                    with self.assertRaises(ValueError):
                        product.retire_block_identity(values)
                    self.assertTrue(path.exists())

    def test_atomic_write_failure_keeps_original_receipt_and_removes_temporary_file(self):
        with tempfile.TemporaryDirectory() as directory:
            c, api, nodes, values = self.fixture(directory)
            with patch.object(Path, 'replace', side_effect=OSError('atomic replacement failed')):
                with self.assertRaisesRegex(OSError, 'atomic replacement failed'):
                    product.check_block_identity(c, api, nodes, values, record=True)
            self.assertEqual([path.name for path in Path(directory).iterdir()], ['installation.json'])

    def test_reconcile_refuses_reinitialized_disk_before_any_mutation(self):
        with tempfile.TemporaryDirectory() as directory:
            c, api, nodes, values = self.fixture(directory)
            product.check_block_identity(c, api, nodes, values, record=True)
            api.objects['nodes.longhorn.io', 'worker-0', product.NAMESPACE][
                'status']['diskStatus']['data-block']['diskUUID'] = 'reinitialized'
            with patch.object(product, 'snapshot_preflight', return_value=[]), \
                    patch.object(product, 'host_context', return_value={}), \
                    patch.object(product, 'check_block_receipts'):
                with self.assertRaisesRegex(ValueError, 'UUID or hardware declaration changed'):
                    product.reconcile(c, api, 'helm', values, nodes, None)
            self.assertEqual(api.writes, [])

    def test_uninstall_guard_failure_preserves_native_identity_receipt(self):
        with tempfile.TemporaryDirectory() as directory:
            c, api, nodes, values = self.fixture(directory)
            product.check_block_identity(c, api, nodes, values, record=True)
            c.update(longhorn_action='uninstall', longhorn_uninstall_id='retire')
            path = values.with_name('block-disks.json')
            original = path.read_bytes()
            with patch.object(product, 'guard', side_effect=ValueError('data remains')):
                with self.assertRaisesRegex(ValueError, 'data remains'):
                    product.reconcile(c, api, 'helm', values, nodes, None)
            self.assertEqual(path.read_bytes(), original)
            self.assertFalse(values.with_name('block-disks-uninstalled-namespace.json').exists())
