"""V1 real monitoring profile and bounded runner contracts, without a live lab."""
import contextlib
import hashlib
import copy
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import yaml

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location('v1_observability_contract', ROOT/'tests/helpers/longhorn-v1-observability.py')
GATE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(GATE)
NODES = ('192.168.122.135', '192.168.122.40', '192.168.122.72', '192.168.122.212', '192.168.122.165', '192.168.122.238')
WORKERS = NODES[3:]
GiB = 1024**3


def filesystem_nodes():
    rows = []
    for i, address in enumerate(WORKERS):
        disk = {'data': {'diskType': 'filesystem', 'path': '/data/longhorn', 'allowScheduling': True, 'storageReserved': GiB}}
        status = {'data': {'diskUUID': 'disk-'+str(i), 'storageMaximum': 200*GiB,
                          'storageAvailable': 190*GiB, 'storageScheduled': 0}}
        rows.append({'metadata': {'name': 'longhorn-node-'+address.rsplit('.', 1)[1], 'uid': 'node-'+str(i)},
                     'spec': {'disks': disk}, 'status': {'diskStatus': status}})
    return rows


def lab_disks(rows=None):
    rows = filesystem_nodes() if rows is None else rows
    return SimpleNamespace(NODES=NODES, WORKERS=WORKERS, DATA='/data/longhorn', DIRECTORY=Path('/unused'),
        write_private=Mock(), get=Mock(side_effect=lambda kind, *a, **kw:
            {'value': '25'} if kind == 'settings.longhorn.io' else {'items': rows}))


def manager_pods():
    return [{'metadata': {'name': 'manager-'+str(i), 'uid': 'pod-'+str(i), 'labels': {'app': 'longhorn-manager'},
                         'ownerReferences': [{'kind': 'DaemonSet', 'uid': 'ds', 'controller': True}]},
             'spec': {'nodeName': 'longhorn-node-'+a.rsplit('.', 1)[1]},
             'status': {'podIP': '10.20.0.'+str(i+1), 'phase': 'Running',
                        'conditions': [{'type': 'Ready', 'status': 'True'}]}} for i, a in enumerate(WORKERS)]


def scrape_fixture():
    pods = manager_pods()
    targets = [{'scrapeUrl': 'http://'+p['status']['podIP']+':9500/metrics', 'health': 'up', 'lastError': '',
                'labels': {'namespace': 'longhorn-system'}} for p in pods]
    ready = [{'labels': {'node': p['spec']['nodeName']}, 'value': 1} for p in pods]
    disks = [{'labels': {'node': p['spec']['nodeName']}, 'value': 200*GiB} for p in pods]
    lab = SimpleNamespace(NODES=NODES, WORKERS=WORKERS, get=Mock(side_effect=lambda kind, *a, **kw:
        {'metadata': {'uid': 'ds'}} if kind == 'daemonset' else {'items': pods}))
    prom = SimpleNamespace(api=Mock(return_value={'status': 'success', 'data': {'activeTargets': targets}}),
        query=Mock(side_effect=lambda expression, **kw: ready if 'node_status' in expression else disks))
    return lab, prom, pods, targets, ready, disks


def full_log():
    lab, prom, *_ = scrape_fixture()
    output = io.StringIO()
    with contextlib.redirect_stdout(output): GATE.scrape_health(lab, prom)
    lines = output.getvalue().splitlines()
    def emit(marker, row): lines.append(marker+' '+json.dumps(row, sort_keys=True))
    names = sorted(GATE.node_names(WORKERS)); digest = 'a'*64
    emit('LONGHORN_V1_OBSERVABILITY_SOURCE_VERIFIED', {'volume_uid': 'volume', 'bytes': 1048576, 'sha256': digest})
    emit('LONGHORN_V1_REAL_PROMETHEUS_RULES', {'volume_uid': 'volume', 'sample_probe': 'probe',
        'bounded_space_threshold_bytes': 1, 'alert_prefix': 'Owned'})
    for suffix in ('Degraded', 'Space', 'Backup', 'Missing'):
        for i, active in enumerate((False, True, False)):
            emit('LONGHORN_V1_REAL_ALERT', {'alert': 'Owned'+suffix, 'active': active, 'volume_uid': 'volume', 'observed_at': i})
    emit('LONGHORN_V1_NATIVE_DEGRADED_RECOVERED', {'volume_uid': 'volume', 'fault_restored': True,
        'degraded': {'volume_robustness': 'degraded'}, 'replicas': {str(i): {'node': n} for i, n in enumerate(names)}})
    emit('LONGHORN_V1_BOUNDED_CAPACITY', {'filler_bytes': 512*1024**2, 'filler_sha256': 'b'*64,
        'before': {n: 0 for n in names}, 'peak': {n: 512*1024**2 for n in names}, 'after': {n: 1 for n in names}})
    emit('LONGHORN_V1_BACKUP_ALERT_RECOVERED', {'volume_uid': 'volume', 'backup_uid': 'backup', 'object_receipt_sha256': 'c'*64})
    emit('LONGHORN_V1_SUPPORT_BUNDLE_BOUNDARY', {'source_uid': 'bundle', 'crc_verified': True,
        'extracted': False, 'published': False, 'compressed_bytes': 100, 'expanded_bytes': 1000, 'members': 1, 'sha256': 'd'*64})
    lines += ['LONGHORN_FSYNC_REMOUNT_VERIFIED bytes=1048576 sha256='+digest+' replica_nodes='+','.join(names),
              'LONGHORN_BUSINESS_FIXTURES_CLEAN_PASS', 'LONGHORN_V1_OBSERVABILITY_BUSINESS_PASS']
    return '\n'.join(lines)


class V1ObservabilityContracts(unittest.TestCase):
    def test_private_v1_profiles_leave_loaded_v2_defaults_unchanged(self):
        v2 = GATE.load('longhorn-v2-monitoring.py'); v1 = GATE.monitoring()
        self.assertEqual(('v2', 'longhorn-v2-install.py', 'LONGHORN_V2_MONITORING'),
                         (v2.DATA_ENGINE, v2.RELAY_HELPER, v2.MARKER))
        self.assertEqual(('v1', 'longhorn-lab.py', 'LONGHORN_V1_MONITORING'),
                         (v1.DATA_ENGINE, v1.RELAY_HELPER, v1.MARKER))
        config = yaml.safe_load((ROOT/'conf/config.yml').read_text()); config['longhorn_data_engines'] = ['v1']
        desired = v1.desired(yaml.safe_dump(config), 'owned')
        self.assertEqual('v1', desired['longhorn_storage_classes'][-1]['data_engine'])
        with self.assertRaises(ValueError): v2.desired(yaml.safe_dump(config), 'owned')
        config['longhorn_data_engines'] = ['v2']
        self.assertEqual('v2', v2.desired(yaml.safe_dump(config), 'owned')['longhorn_storage_classes'][-1]['data_engine'])
        self.assertEqual('v2', GATE.load('longhorn-v2-observability.py').Probe.DATA_ENGINE)
        self.assertEqual('v2_observability', GATE.load('longhorn-v2-observability.py').KEY)
        self.assertEqual('v1', GATE.Probe.DATA_ENGINE)
        self.assertEqual(GATE.KEY, GATE.SHARED.KEY)

    def test_v1_inventory_preserves_three_control_and_three_storage_roles(self):
        lab = lab_disks(); groups = GATE.inventory_groups(lab)
        self.assertEqual(list(NODES[:3]), groups['kube_master']); self.assertEqual(list(WORKERS), groups['kube_node'])
        self.assertEqual(list(NODES[:3]), groups['etcd'])
        self.assertEqual(list(NODES), GATE.load('longhorn-v2-monitoring.py').inventory_groups(lab)['kube_node'])
        lab.WORKERS = NODES[:3]
        with self.assertRaises(ValueError): GATE.inventory_groups(lab)

    def test_actual_monitoring_template_renders_control_endpoints_separately_from_six_proxy_nodes(self):
        module = GATE.monitoring()
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            template = base/'roles/cluster-addon/templates/prometheus/values.yaml.j2'
            template.parent.mkdir(parents=True)
            shutil.copyfile(ROOT/'roles/cluster-addon/templates/prometheus/values.yaml.j2', template)
            chart = base/'roles/cluster-addon/files/kube-prometheus-stack-88.0.0.tgz'
            chart.parent.mkdir(parents=True); chart.write_bytes(b'unit-chart-identity')
            config = yaml.safe_load((ROOT/'conf/config.yml').read_text())
            config.update(K8S_VER='1.33.6', CLUSTER_NAME='longhorn-gate', longhorn_data_engines=['v1'])
            config = module.desired(yaml.safe_dump(config), 'owned')
            lab = lab_disks(); lab.BASE = base; lab.execute = Mock(return_value=SimpleNamespace(stdout='kind: ConfigMap\nmetadata: {name: fixture}\n'))
            documents, raw, digest = module.render(lab, config)
            values = yaml.safe_load(raw)
            for component in ('kubeControllerManager', 'kubeEtcd', 'kubeScheduler'):
                self.assertEqual(list(NODES[:3]), values[component]['endpoints'])
            self.assertEqual(list(NODES), values['kubeProxy']['endpoints'])
            self.assertEqual(module.CLASS, values['prometheus']['prometheusSpec']['storageSpec']['volumeClaimTemplate']['spec']['storageClassName'])
            self.assertEqual('owned', lab.execute.call_args.args[0][5])

    def test_support_bundle_manager_requires_three_owned_client_managers_for_v1(self):
        lab, _, pods, *_ = scrape_fixture()
        probe = object.__new__(GATE.Probe); probe.lab = lab
        pod, endpoint = probe.manager()
        self.assertEqual('http://10.20.0.1:9500', endpoint)
        pods.pop()
        with self.assertRaisesRegex(ValueError, 'all owned product managers'): probe.manager()

    def test_native_profile_follows_actual_product_client_labels_and_pinned_chart(self):
        # Reach the actual bootstrap + official chart selector. A six-host
        # Kubernetes inventory does not imply six Longhorn Manager clients.
        spec = importlib.util.spec_from_file_location('topology_product', ROOT/'roles/longhorn/files/longhorn.py')
        product = importlib.util.module_from_spec(spec); spec.loader.exec_module(product)
        config = yaml.safe_load((ROOT/'roles/longhorn/defaults/main.yml').read_text())
        config.update(longhorn_inventory_nodes=list(NODES), longhorn_client_nodes=list(WORKERS),
                      longhorn_storage_nodes=list(WORKERS))
        selected = []
        for address in NODES:
            node = {'metadata': {'name': 'longhorn-node-'+address.rsplit('.', 1)[1],
                                  'uid': address, 'resourceVersion': '1'}}
            api = SimpleNamespace(get=Mock(return_value=node), patch=Mock())
            product.bootstrap_node(config, api, address, node, {'total_bytes': 200*GiB})
            labels = api.patch.call_args.args[2]['metadata']['labels']
            if labels['kubeauto.io/longhorn-client'] == 'true': selected.append(node['metadata']['name'])
        helm = shutil.which('helm')
        self.assertIsNotNone(helm, 'Pinned Helm is required by the topology render contract')
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'values.json'; path.write_text(json.dumps(product.chart_values(config)))
            raw = subprocess.check_output([helm, 'template', 'longhorn',
                str(ROOT/'roles/longhorn/files/longhorn-1.12.1.tgz'), '-n', 'longhorn-system', '-f', str(path)], text=True)
        ds = next(o for o in yaml.safe_load_all(raw) if o and o['kind'] == 'DaemonSet' and o['metadata']['name'] == 'longhorn-manager')
        self.assertEqual({'kubeauto.io/longhorn-client': 'true'}, ds['spec']['template']['spec']['nodeSelector'])
        self.assertEqual(set(selected), set(GATE.filesystem_rows(lab_disks())))
        lab, prom, *_ = scrape_fixture()
        result = GATE.scrape_health(lab, prom)
        self.assertIsNotNone(result)
        self.assertEqual(set(selected), set(result['ready_nodes']))
        v1 = object.__new__(GATE.Probe); v1.lab = lab
        self.assertIn(v1.manager()[0]['spec']['nodeName'], selected)
        shared = object.__new__(GATE.SHARED.Probe); shared.lab = lab
        # The shared V2 default still expects all of its explicitly supplied
        # nodes. The V1 correction must not relax that independent boundary.
        with self.assertRaisesRegex(ValueError, 'all owned product managers'): shared.manager()
        shared.lab = SimpleNamespace(NODES=WORKERS, get=lab.get)
        self.assertIn(shared.manager()[0]['spec']['nodeName'], selected)

    def test_filesystem_budget_uses_native_counters_and_actual_scheduling_watermark(self):
        lab = lab_disks(); rows = GATE.storage_budget(lab)
        self.assertEqual(GATE.node_names(WORKERS), set(rows))
        self.assertTrue(all(r['schedulable_bytes'] == 140*GiB for r in rows.values()))
        nodes = filesystem_nodes(); nodes[0]['status']['diskStatus']['data']['storageAvailable'] = 80*GiB
        with self.assertRaisesRegex(ValueError, '54-GiB'): GATE.storage_budget(lab_disks(nodes))

    def test_filesystem_profile_refuses_raw_missing_control_disks_and_low_space(self):
        for mutation in ('block', 'control', 'uuid', 'path', 'count', 'capacity', 'bool', 'foreign'):
            rows = filesystem_nodes()
            if mutation == 'block': rows[0]['spec']['disks']['data']['diskType'] = 'block'
            if mutation == 'control':
                extra = copy.deepcopy(rows[0]); extra['metadata']['name'] = 'longhorn-node-135'; rows.append(extra)
            if mutation == 'uuid': rows[0]['status']['diskStatus']['data']['diskUUID'] = ''
            if mutation == 'path': rows[0]['spec']['disks']['data']['path'] = '/foreign'
            if mutation == 'count': rows.pop()
            if mutation == 'capacity': rows[0]['status']['diskStatus']['data']['storageAvailable'] = 3*GiB
            if mutation == 'bool': rows[0]['status']['diskStatus']['data']['storageMaximum'] = True
            if mutation == 'foreign': rows[0]['metadata']['name'] = 'foreign'
            with self.subTest(mutation=mutation), self.assertRaises(ValueError): GATE.filesystem_rows(lab_disks(rows))

    def test_capacity_observation_freezes_original_native_disk_identity(self):
        rows = filesystem_nodes(); probe = object.__new__(GATE.Probe)
        probe.lab = lab_disks(rows); probe.journal = {GATE.KEY: {}}
        self.assertEqual(3, len(probe.disk_used())); self.assertEqual(1, probe.lab.write_private.call_count)
        rows[0]['status']['diskStatus']['data']['storageAvailable'] -= GiB
        self.assertEqual(11*GiB, probe.disk_used()[rows[0]['metadata']['name']])
        rows[0]['status']['diskStatus']['data']['diskUUID'] = 'replacement'
        with self.assertRaisesRegex(ValueError, 'identity changed'): probe.disk_used()

    def test_real_scrape_proves_three_client_managers_and_three_storage_series(self):
        lab, prom, *_ = scrape_fixture()
        result = GATE.scrape_health(lab, prom)
        self.assertEqual(3, len(result['manager_targets'])); self.assertEqual(3, len(result['filesystem_nodes']))
        prom.api.assert_called_once_with('/api/v1/targets', {'state': 'active'})

    def test_scrape_rejects_missing_down_foreign_and_wrong_node_or_disk_samples(self):
        for mutation in ('missing', 'down', 'owner', 'notready', 'endpoint', 'namespace', 'nodestatus', 'disknode'):
            lab, prom, pods, targets, ready, disks = scrape_fixture()
            if mutation == 'missing': targets.pop()
            if mutation == 'down': targets[0]['health'] = 'down'
            if mutation == 'owner': pods[0]['metadata']['ownerReferences'][0]['uid'] = 'foreign'
            if mutation == 'notready': pods[0]['status']['conditions'][0]['status'] = 'False'
            if mutation == 'endpoint': targets[0]['scrapeUrl'] = 'http://10.20.0.1:9500/foreign'
            if mutation == 'namespace': targets[0]['labels']['namespace'] = 'foreign'
            if mutation == 'nodestatus': ready[0]['value'] = 0
            if mutation == 'disknode': disks[0]['labels']['node'] = 'longhorn-node-135'
            with self.subTest(mutation=mutation): self.assertIsNone(GATE.scrape_health(lab, prom))

    def test_source_native_volume_and_engine_require_v1_identity(self):
        volume = {'metadata': {'uid': 'v'}, 'spec': {'dataEngine': 'v1'}, 'status': {'currentNodeID': 'n'}}
        engine = {'metadata': {'ownerReferences': [{'kind': 'Volume', 'uid': 'v'}]},
                  'spec': {'dataEngine': 'v1', 'nodeID': 'n', 'volumeName': 'source'}, 'status': {'currentState': 'running'}}
        probe = object.__new__(GATE.Probe); probe.journal = {'volume': 'source', 'volume_uid': 'v'}
        probe.lab = SimpleNamespace(get=Mock(side_effect=lambda kind, *a, **kw: volume if kind == 'volumes.longhorn.io' else {'items': [engine]}))
        self.assertIs(engine, probe.engine())
        engine['spec']['dataEngine'] = 'v2'
        with self.assertRaises(ValueError): probe.engine()
        volume['spec']['dataEngine'] = 'v2'
        with self.assertRaises(ValueError): probe.volume()

    def test_bounded_run_uses_alert_actions_once_without_sustained_or_performance_work(self):
        probe = object.__new__(GATE.Probe); order = []
        for method in ('rules', 'network_fault', 'pressure', 'backup', 'missing', 'retire_rules', 'support_bundle'):
            setattr(probe, method, Mock(side_effect=lambda name=method: order.append(name)))
        probe.start = Mock(side_effect=AssertionError('no repeated longload')); probe.queue = Mock(side_effect=AssertionError('no repeated benchmark'))
        probe.emit = Mock(); probe.lab = SimpleNamespace(DIRECTORY=Path('/unused'), write_private=Mock(),
            owner=lambda: {'run_id': 'run'}, payload_program=lambda *a, **kw: 'read',
            kubectl=Mock(return_value=SimpleNamespace(stdout='digest')), replica_topology=Mock(return_value={'r': {}}))
        probe.run({'volume_uid': 'v', 'namespace': 'ns'}, 'data', 'digest')
        self.assertEqual(['rules', 'network_fault', 'pressure', 'backup', 'missing', 'retire_rules', 'support_bundle'], order)
        probe.start.assert_not_called(); probe.queue.assert_not_called()
        self.assertEqual('LONGHORN_V1_OBSERVABILITY_SOURCE_VERIFIED', probe.emit.call_args.args[0])

    def test_network_fault_drives_real_confirmed_writer_handoff_and_cleans_failures(self):
        # Execute the real control.verify composition, including started/stop,
        # native recovery, post-recovery writes and independent ledger checks.
        for failure in ('', 'native', 'alert', 'ledger'):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory:
                control = GATE.load('longhorn-control-faults.py')
                faults = GATE.load('longhorn-faults.py')
                events = []; state = {'started': False, 'stopped': False, 'count': 0}
                token = 'run:instance-manager'
                def records(path):
                    state['count'] += 4
                    return [{'type': 'ack', 'sequence': i,
                             'sha256': hashlib.sha256(faults.ledger_block(token, i)).hexdigest()}
                            for i in range(state['count'])] + ([{'type': 'complete'}] if state['stopped'] else [])
                def popen(*args, **kwargs):
                    self.assertIn('-i', args[0]); self.assertIn('writer', args[0])
                    self.assertIn('stop=ledger_stop_requested', args[0][-1])
                    self.assertIn('os.fsync(descriptor)', args[0][-1])
                    state['started'] = True; events.append('writer-start')
                    return process
                def stop(message):
                    self.assertEqual('stop\n', message); state['stopped'] = True; events.append('writer-stop')
                process = SimpleNamespace(returncode=0, stdin=SimpleNamespace(write=stop, flush=Mock(), close=Mock()),
                    poll=lambda: 0 if state['stopped'] else None,
                    terminate=lambda: state.update(stopped=True), wait=Mock())
                allowed = {'source': 'v', 'monitor': 'm'}
                proof = {'replica_uid': 'r', 'fault_restored': False, 'degraded': {'volume_robustness': 'degraded'}}
                probe = object.__new__(GATE.Probe); probe.journal = {'volume_uid': 'v', 'namespace': 'owned'}
                def inject(lab, journal, case, allowed_consumers=None):
                    self.assertTrue(state['started'], 'network fault requires fsync I/O already running')
                    self.assertEqual(allowed, allowed_consumers); events.append('inject')
                    if failure == 'native': raise TimeoutError('native degraded')
                    journal['v1_storage_fault'] = proof
                    return proof
                def restore(*args):
                    proof['fault_restored'] = True; events.append('restore')
                def recovered(*args, **kwargs):
                    self.assertTrue(proof['fault_restored'])
                    proof['recovered'] = {'replicas': {'r': {}}}; events.append('native-recovered')
                    return True
                native = SimpleNamespace(KEY='v1_storage_fault', inject=Mock(side_effect=inject),
                    restore_fault=Mock(side_effect=restore), recovered=Mock(side_effect=recovered))
                def alert(name, active):
                    self.assertEqual('Degraded', name); events.append('firing' if active else 'resolved')
                    if active and failure == 'alert': raise TimeoutError('alert firing')
                def readback(*args):
                    events.append('ledger-readback')
                    if failure == 'ledger': raise ValueError('acknowledged ledger changed')
                faults.verify_client_ledger = Mock(side_effect=readback)
                fresh = []
                # Fresh-write CLI output must satisfy the actual confirmation checker.
                fresh_token = token+':recovered'
                for i in range(4):
                    fresh.append({'type': 'ack', 'sequence': i,
                        'sha256': hashlib.sha256(faults.ledger_block(fresh_token, i)).hexdigest()})
                probe.lab = SimpleNamespace(BASE=Path(directory), DIRECTORY=Path(directory),
                    owner=lambda: {'run_id': 'run'}, load_helper=lambda name: faults,
                    wait=lambda name, timeout, fn: fn(), kubectl=Mock(return_value=SimpleNamespace(
                        stdout='\n'.join(json.dumps(r) for r in fresh))))
                probe.owned_consumers = Mock(return_value=allowed); probe.alert = alert; probe.emit = Mock()
                original_load = GATE.load
                def load(name):
                    return native if name == 'longhorn-v1-storage-faults.py' else control if name == 'longhorn-control-faults.py' else original_load(name)
                with patch.object(GATE, 'load', side_effect=load), patch.object(control.subprocess, 'Popen', side_effect=popen), \
                     patch.object(control, 'ledger_rows', side_effect=records), contextlib.redirect_stdout(io.StringIO()):
                    if failure:
                        with self.assertRaises((ValueError, TimeoutError)): probe.network_fault()
                        self.assertNotIn('resolved', events); probe.emit.assert_not_called()
                    else:
                        probe.network_fault()
                        self.assertLess(events.index('writer-start'), events.index('inject'))
                        self.assertLess(events.index('firing'), events.index('restore'))
                        self.assertLess(events.index('native-recovered'), events.index('writer-stop'))
                        self.assertLess(events.index('ledger-readback'), events.index('resolved'))
                        self.assertEqual(1, faults.verify_client_ledger.call_count)
                        self.assertIn('/data/control-recovered-ledger', probe.lab.kubectl.call_args.args[-1])
                        self.assertEqual('LONGHORN_V1_NATIVE_DEGRADED_RECOVERED', probe.emit.call_args.args[0])
                self.assertTrue(state['stopped']); process.stdin.close.assert_called_once()

    def test_business_restores_callback_and_cleanup_even_when_product_path_raises(self):
        for fail in (False, True):
            events = []; original_hook = Mock(); original_cleanup = Mock()
            store = SimpleNamespace(cleanup=Mock(side_effect=lambda: events.append('store-clean')), prepare=Mock(), qualify=Mock(), product_configuration=lambda: {})
            @contextlib.contextmanager
            def scoped(fn):
                try: yield
                finally: fn()
            probe = SimpleNamespace(run=Mock(), restore=Mock(side_effect=lambda: events.append('restore')))
            lab = SimpleNamespace(backup_store=lambda: store, scoped_cleanup=scoped, verify_performance=original_hook, cleanup_business=original_cleanup)
            def product(scenario, **kwargs):
                self.assertEqual(('performance', 'longhorn-delete'), (scenario, kwargs['storage_class']))
                self.assertIs(probe.run, lab.verify_performance)
                lab.cleanup_business('record', relay_pid=3)
                if fail: raise ValueError('product failure')
            lab.business = Mock(side_effect=product)
            with patch.object(GATE, 'monitoring', return_value=SimpleNamespace(verify=Mock())), patch.object(GATE, 'Probe', return_value=probe):
                if fail:
                    with self.assertRaisesRegex(ValueError, 'product failure'): GATE.business(lab, 3)
                else: GATE.business(lab, 3)
            self.assertIs(original_hook, lab.verify_performance); self.assertIs(original_cleanup, lab.cleanup_business)
            self.assertEqual(['restore', 'store-clean'], events); original_hook.assert_not_called()
            original_cleanup.assert_called_once_with('record', relay_pid=3)

    def test_cleanup_restores_fault_before_deleting_business_and_external_store(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/GATE.RECORD
            path.write_text(json.dumps({'volume_uid': 'v', GATE.KEY: {'run_id': 'run', 'volume_uid': 'v'}}))
            order = []; store = SimpleNamespace(cleanup=lambda: order.append('store'))
            lab = SimpleNamespace(DIRECTORY=Path(directory), owner=lambda: {'run_id': 'run'},
                check_business_graph=lambda j: order.append('graph'), backup_store=lambda: store,
                cleanup_business=lambda **kw: (order.append('delete'), path.unlink()))
            with patch.object(GATE, 'Probe', return_value=SimpleNamespace(restore=lambda: order.append('restore'))): GATE.cleanup(lab, 3)
            self.assertEqual(['graph', 'restore', 'delete', 'store'], order)

    def test_cleanup_rejects_foreign_or_symlink_receipts_before_deletion(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/GATE.RECORD; path.write_text('{}')
            lab = SimpleNamespace(DIRECTORY=Path(directory), owner=lambda: {'run_id': 'run'}, cleanup_business=Mock())
            with self.assertRaises(ValueError): GATE.cleanup(lab, 3)
            path.unlink(); path.symlink_to(Path(directory)/'missing')
            with self.assertRaises(ValueError): GATE.cleanup(lab, 3)
            lab.cleanup_business.assert_not_called()

    def test_complete_raw_log_proves_all_four_alert_lifecycles_and_business_readback(self):
        GATE.verify_log(full_log())

    def test_marker_only_wrong_identity_unresolved_and_incomplete_records_do_not_pass(self):
        for mutation in ('marker', 'resolve', 'uid', 'source', 'replica', 'capacity', 'backup', 'bundle', 'remount', 'scrape', 'failure'):
            lines = full_log().splitlines()
            if mutation == 'marker': lines = ['LONGHORN_V1_OBSERVABILITY_BUSINESS_PASS']
            if mutation == 'resolve': lines = [l for l in lines if not (l.startswith('LONGHORN_V1_REAL_ALERT ') and 'OwnedMissing' in l and '"active": false' in l)]
            if mutation == 'uid': lines = [l.replace('"volume_uid": "volume"', '"volume_uid": "foreign"') if l.startswith('LONGHORN_V1_BACKUP_ALERT') else l for l in lines]
            if mutation == 'source': lines = [l.replace('"bytes": 1048576', '"bytes": 0') for l in lines]
            if mutation == 'replica': lines = [l.replace('"node": "longhorn-node-212"', '"node": "foreign"') if l.startswith('LONGHORN_V1_NATIVE_') else l for l in lines]
            if mutation == 'capacity': lines = [l.replace('"after": {', '"after": {"foreign": 0,') for l in lines]
            if mutation == 'backup': lines = [l.replace('"backup_uid": "backup"', '"backup_uid": ""') for l in lines]
            if mutation == 'bundle': lines = [l.replace('"published": false', '"published": true') for l in lines]
            if mutation == 'remount': lines = [l for l in lines if not l.startswith('LONGHORN_FSYNC_REMOUNT_VERIFIED')]
            if mutation == 'scrape': lines = [l for l in lines if not l.startswith('LONGHORN_V1_REAL_SCRAPE ')]
            if mutation == 'failure': lines.append('LONGHORN_FIRST_FAILURE phase=unit')
            with self.subTest(mutation=mutation), self.assertRaises(ValueError): GATE.verify_log('\n'.join(lines))

    def run_shell(self, mode='observability', fail='', terminate=False):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            for name in ('.venv/bin', 'kube-bin', 'tests/helpers'): (base/name).mkdir(parents=True)
            script = (ROOT/'tests/helpers/longhorn-v1-observability.sh').read_text().replace('/usr/local/kubeauto', str(base))
            script = script.replace('/var/tmp/kubeauto-longhorn-v1-observability.', str(base/'run.'))
            (base/'runner.sh').write_text(script)
            shutil.copyfile(ROOT/'tests/helpers/run-durable-gate.sh', base/'tests/helpers/run-durable-gate.sh')
            fake = base/'.venv/bin/python'
            fake.write_text('''#!/usr/bin/env bash
set -eu
echo "${1##*/} ${2:-}" >> "$OBS_TRACE"
if [[ "${2:-}" == relay-maintain ]]; then
 exec "$OBS_REAL_PY" -c 'import os,signal,time; from pathlib import Path; signal.signal(signal.SIGTERM,lambda *a:exit(0)); Path(os.environ["OBS_RELAY_READY"]).touch(); time.sleep(60)'
fi
if [[ "${2:-}" == cleanup ]]; then
 "$OBS_REAL_PY" -c 'import os,time; from pathlib import Path; p=Path(os.environ["OBS_RELAY_READY"]); start=time.monotonic()
while not p.exists():
 assert time.monotonic()-start<3
 time.sleep(.01)'
fi
if [[ "${2:-}" == "$OBS_FAIL" ]]; then exit 17; fi
if [[ "${2:-}" == business && "$OBS_TERMINATE" == yes ]]; then
 touch "$OBS_READY"
 exec "$OBS_REAL_PY" -c 'import time; time.sleep(2)'
fi
if [[ "${2:-}" == setup ]]; then echo LONGHORN_V1_MONITORING_SETUP_PASS; fi
if [[ "${2:-}" == business ]]; then echo LONGHORN_V1_OBSERVABILITY_BUSINESS_PASS; fi
if [[ "${2:-}" == support-business ]]; then echo LONGHORN_V1_SUPPORT_BUSINESS_PASS; fi
''')
            fake.chmod(0o755)
            kubectl = base/'kube-bin/kubectl'; kubectl.write_text('#!/usr/bin/env bash\nexit 0\n'); kubectl.chmod(0o755)
            env = dict(os.environ, LONGHORN_PARENT_LEASE_HELD='yes', OBS_TRACE=str(base/'trace'), OBS_FAIL=fail,
                       OBS_REAL_PY=sys.executable, OBS_READY=str(base/'ready'), OBS_RELAY_READY=str(base/'relay-ready'),
                       OBS_TERMINATE='yes' if terminate else 'no')
            process = subprocess.Popen(['bash', str(base/'runner.sh'), mode], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            if terminate:
                deadline = time.monotonic()+5
                while not (base/'ready').exists() and process.poll() is None:
                    if time.monotonic() >= deadline: process.kill(); self.fail('shell did not reach bounded business')
                    time.sleep(.01)
                process.send_signal(signal.SIGTERM)
            stdout, stderr = process.communicate(timeout=10)
            work = list(base.glob('run.*'))
            durable = {p.name: p.read_text().strip() for p in work[0].iterdir() if p.name in ('exit', 'finalized')} if work else {}
            trace = (base/'trace').read_text() if (base/'trace').exists() else ''
            return process.returncode, stdout, stderr, trace, durable

    def test_wrapper_full_has_verified_business_before_dependent_cleanup_and_terminal(self):
        result = self.run_shell(); rc, stdout, stderr, trace, durable = result
        self.assertEqual(0, rc, stdout+stderr)
        self.assertEqual({'exit': '0', 'finalized': '0'}, durable)
        self.assertLess(trace.index('longhorn-v1-observability.py cleanup'), trace.index('longhorn-v1-observability.py monitor-cleanup'))
        self.assertIn('longhorn-v1-observability.py verify', trace)
        self.assertEqual(1, stdout.splitlines().count('LONGHORN_OBSERVABILITY_PASS'))

    def test_wrapper_preserves_first_failure_and_blocks_monitor_deletion_when_business_cleanup_fails(self):
        for failure in ('setup', 'monitor-verify', 'business', 'verify', 'cleanup', 'monitor-cleanup'):
            rc, stdout, stderr, trace, durable = self.run_shell(fail=failure)
            with self.subTest(failure=failure):
                self.assertEqual(17, rc, stdout+stderr); self.assertEqual('17', durable['exit'])
                self.assertNotIn('LONGHORN_OBSERVABILITY_PASS', stdout)
                if failure == 'cleanup':
                    self.assertNotIn('longhorn-v1-observability.py monitor-cleanup', trace)
                    self.assertEqual('17', durable['finalized'])
                else: self.assertIn('LONGHORN_FIRST_FAILURE', stderr) if failure not in ('monitor-cleanup',) else None

    def test_wrapper_focus_and_cleanup_have_distinct_terminals_without_full_pass(self):
        for mode, terminal in (('observability-business', 'LONGHORN_OBSERVABILITY_BUSINESS_BASELINE_RETAINED'),
                               ('observability-cleanup', 'LONGHORN_OBSERVABILITY_CLEANUP_PASS')):
            rc, stdout, stderr, trace, durable = self.run_shell(mode)
            with self.subTest(mode=mode):
                self.assertEqual(0, rc, stdout+stderr); self.assertIn(terminal, stdout)
                self.assertNotIn('LONGHORN_OBSERVABILITY_PASS', stdout)
                self.assertNotIn('longhorn-v1-observability.py setup', trace)
                if mode == 'observability-business': self.assertNotIn('longhorn-v1-observability.py monitor-cleanup', trace)
                else: self.assertNotIn('longhorn-v1-observability.py business', trace)

    def test_wrapper_sigterm_has_durable_143_and_runs_scoped_cleanup(self):
        rc, stdout, stderr, trace, durable = self.run_shell(terminate=True)
        self.assertEqual(143, rc, stdout+stderr); self.assertEqual({'exit': '143', 'finalized': '0'}, durable)
        self.assertIn('longhorn-v1-observability.py cleanup', trace)
        self.assertIn('longhorn-v1-observability.py monitor-cleanup', trace)
        self.assertNotIn('LONGHORN_OBSERVABILITY_PASS', stdout)

    def test_unknown_mode_refuses_before_any_remote_operation(self):
        rc, stdout, stderr, trace, durable = self.run_shell('unknown')
        self.assertEqual(2, rc); self.assertFalse(trace); self.assertFalse(durable)


if __name__ == '__main__': unittest.main()
