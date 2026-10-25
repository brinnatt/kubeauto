"""V1 DR engine/identity, asynchronous restore and planned handoff contracts."""
import copy
from contextlib import contextmanager, redirect_stdout
import hashlib
import importlib.util
from io import StringIO
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[2]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec); sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


gate = load('v1_disaster_contract', ROOT / 'tests/helpers/longhorn-v1-disaster.py')


def native_volume(name='standby', uid='standby-uid'):
    return {'metadata': {'name': name, 'uid': uid, 'resourceVersion': '2'},
        'spec': {'dataEngine': 'v1', 'size': str(2 * 1024**3), 'numberOfReplicas': 1,
                 'Standby': True, 'frontend': '', 'fromBackup': 's3://bucket/initial'},
        'status': {'state': 'attached', 'robustness': 'healthy', 'isStandby': True,
                   'lastBackup': 'second', 'restoreRequired': True}}


def standby_fixture():
    volume = native_volume()
    engine = {'metadata': {'name': 'engine', 'uid': 'engine-uid'},
        'spec': {'volumeName': 'standby', 'dataEngine': 'v1', 'frontend': '', 'requestedBackupRestore': 'second'},
        'status': {'currentState': 'running', 'lastRestoredBackup': 'second', 'endpoint': '',
                   'restoreStatus': {'replica': {'isRestoring': False, 'error': ''}}}}
    replica = {'metadata': {'name': 'replica', 'uid': 'replica-uid'},
        'spec': {'volumeName': 'standby', 'dataEngine': 'v1', 'nodeID': 'longhorn-node-149', 'diskID': 'filesystem-uuid',
                 'healthyAt': 'now', 'failedAt': ''}, 'status': {'currentState': 'running'}}
    objects = {'volumes.longhorn.io': volume, 'engines.longhorn.io': [engine], 'replicas.longhorn.io': [replica], 'pv': []}
    def get(kind, name='', namespace=''):
        return copy.deepcopy(objects[kind]) if kind == 'volumes.longhorn.io' else {'items': copy.deepcopy(objects[kind])}
    return SimpleNamespace(get=get), objects


class StandbyContracts(unittest.TestCase):
    def check(self, lab):
        return gate.standby_restored(lab, 'standby', 'standby-uid', 'second', 'filesystem-uuid')

    def test_last_actually_restored_point_and_filesystem_identity_required(self):
        lab, objects = standby_fixture(); result = self.check(lab)
        self.assertEqual(result['last_restored_backup'], 'second'); self.assertEqual(result['disk_uuid'], 'filesystem-uuid')
        for field, value in (('lastRestoredBackup', 'first'), ('currentState', 'starting')):
            old = copy.deepcopy(objects); objects['engines.longhorn.io'][0]['status'][field] = value
            self.assertIsNone(self.check(lab)); objects.update(old)
        objects['engines.longhorn.io'][0]['status']['restoreStatus']['replica']['isRestoring'] = True
        self.assertIsNone(self.check(lab))

    def test_v2_replica_foreign_disk_pv_and_frontend_are_refused(self):
        for fault in ('v2', 'disk', 'node', 'pv', 'endpoint', 'frontend', 'restore-error'):
            with self.subTest(fault=fault):
                lab, objects = standby_fixture()
                if fault == 'v2': objects['replicas.longhorn.io'][0]['spec']['dataEngine'] = 'v2'
                elif fault == 'disk': objects['replicas.longhorn.io'][0]['spec']['diskID'] = 'raw-uuid'
                elif fault == 'node': objects['replicas.longhorn.io'][0]['spec']['nodeID'] = 'foreign'
                elif fault == 'pv': objects['pv'] = [{'spec': {'csi': {'volumeHandle': 'standby'}}}]
                elif fault == 'endpoint': objects['engines.longhorn.io'][0]['status']['endpoint'] = '/dev/longhorn/standby'
                elif fault == 'frontend': objects['engines.longhorn.io'][0]['spec']['frontend'] = 'blockdev'
                else: objects['engines.longhorn.io'][0]['status']['restoreStatus']['replica']['error'] = 'missing backup block'
                with self.assertRaises(ValueError): self.check(lab)

    def test_unscheduled_replica_waits_without_relaxing_disk_allowlist(self):
        lab, objects = standby_fixture(); objects['replicas.longhorn.io'][0]['spec'].update(nodeID='', diskID='')
        self.assertIsNone(self.check(lab))
        objects['replicas.longhorn.io'][0]['spec']['diskID'] = 'foreign'
        with self.assertRaisesRegex(ValueError, 'filesystem'): self.check(lab)

    def test_manifest_explicitly_selects_v1_and_keeps_original_backup(self):
        lab = SimpleNamespace(get=lambda *a: {'value': 'pinned-engine'})
        manifest = gate.standby_manifest(lab, 'approved', 's3://bucket/point')
        self.assertEqual(manifest['spec']['dataEngine'], 'v1'); self.assertEqual(manifest['spec']['numberOfReplicas'], 1)
        self.assertTrue(manifest['spec']['Standby']); self.assertEqual(manifest['spec']['fromBackup'], 's3://bucket/point')

    def test_two_points_cannot_silently_replace_original_standby_engine(self):
        with tempfile.TemporaryDirectory() as directory:
            dr = gate.Standby.__new__(gate.Standby)
            dr.target_fixture = SimpleNamespace(ready=lambda: {'v1_disk_uuid': 'filesystem-uuid'})
            lab, objects = standby_fixture(); dr.target = lab
            lab.wait = lambda label, timeout, check: check()
            row = {'points': {'first': {'restored': {'volume_uid': 'standby-uid', 'engine_uid': 'old-engine',
                'replica_uid': 'replica-uid', 'disk_uuid': 'filesystem-uuid'}}, 'second': {}}}
            dr.receipt = lambda: row; dr.save = Mock()
            with self.assertRaisesRegex(ValueError, 'identity changed'): dr.restored('standby', 'standby-uid', {'name': 'second'})
            dr.save.assert_not_called()


class FencingContracts(unittest.TestCase):
    def fixture(self):
        volume = {'metadata': {'uid': 'volume-uid', 'name': 'source', 'resourceVersion': '1'},
            'spec': {'dataEngine': 'v1', 'frontend': 'blockdev', 'disableFrontend': True, 'nodeID': ''},
            'status': {'state': 'detached', 'currentNodeID': ''}}
        journal = {'namespace': 'longhorn-business-source', 'volume': 'source', 'volume_uid': 'volume-uid',
            'pv': 'pv', 'pv_uid': 'pv-uid', 'objects': {'Pod/writer': 'writer-uid', 'PersistentVolumeClaim/data': 'claim-uid'}}
        objects = {'volumes.longhorn.io': volume, 'pod': None, 'pvc': None, 'pv': None,
            'volumeattachments.longhorn.io': [], 'engines.longhorn.io': [{'metadata': {'name': 'engine', 'uid': 'engine-uid'},
            'spec': {'volumeName': 'source', 'dataEngine': 'v1', 'desireState': 'stopped'},
            'status': {'currentState': 'stopped', 'endpoint': ''}}]}
        def get(kind, name='', namespace=''):
            if kind in ('pv', 'pod', 'pvc'):
                return copy.deepcopy(objects[kind]) if name else {'items': [copy.deepcopy(objects[kind])] if objects[kind] else []}
            if kind == 'volumes.longhorn.io': return copy.deepcopy(objects[kind])
            return {'items': copy.deepcopy(objects[kind])}
        lab = SimpleNamespace(get=get, owner=lambda: {'cluster_uid': 'source-cluster'},
            check_business_graph=Mock(), remove=Mock(), kubectl=Mock(), wait=lambda label, timeout, check: check())
        return lab, journal, objects

    def test_unreachable_source_is_never_a_fence_and_stale_proof_cannot_activate(self):
        lab, journal, objects = self.fixture(); target, _ = standby_fixture()
        with patch.object(gate.native, 'guarded_patch') as update:
            with self.assertRaisesRegex(ValueError, 'current source fencing'):
                gate.activate(lab, journal, target, 'standby', 'standby-uid', {'name': 'second'}, 'filesystem-uuid', None, 's3://bucket/initial')
            proof = gate.fenced(lab, journal); objects['volumes.longhorn.io']['spec']['disableFrontend'] = False
            with self.assertRaisesRegex(ValueError, 'frontend disabled'):
                gate.activate(lab, journal, target, 'standby', 'standby-uid', {'name': 'second'}, 'filesystem-uuid', proof, 's3://bucket/initial')
            update.assert_not_called()
        lab.get = Mock(side_effect=TimeoutError('source unreachable'))
        with self.assertRaises(TimeoutError): gate.fenced(lab, journal)

    def test_pv_tickets_running_or_exposed_engine_prevent_fence(self):
        for fault in ('pv', 'tickets', 'engine', 'endpoint', 'missing-engine'):
            with self.subTest(fault=fault):
                lab, journal, objects = self.fixture()
                if fault == 'pv': objects['pv'] = {'spec': {'csi': {'volumeHandle': 'source'}}}
                elif fault == 'tickets': objects['volumeattachments.longhorn.io'] = [{'spec': {'volume': 'source', 'attachmentTickets': {'writer': {}}}}]
                elif fault == 'engine': objects['engines.longhorn.io'][0]['status']['currentState'] = 'running'
                elif fault == 'endpoint': objects['engines.longhorn.io'][0]['status']['endpoint'] = '/dev/longhorn/source'
                else: objects['engines.longhorn.io'] = []
                with self.assertRaises(ValueError): gate.fenced(lab, journal)
                lab.remove.assert_not_called(); lab.kubectl.assert_not_called()

    def test_activate_latest_restored_point_only_and_uid_spec_guarded(self):
        source, journal, _ = self.fixture(); target, objects = standby_fixture(); proof = gate.fenced(source, journal)
        with patch.object(gate.native, 'guarded_patch') as update:
            gate.activate(source, journal, target, 'standby', 'standby-uid', {'name': 'second'}, 'filesystem-uuid', proof, 's3://bucket/initial')
            self.assertEqual(update.call_args.args[-1], {'Standby': False, 'frontend': 'blockdev'})
            self.assertEqual(update.call_args.args[2]['metadata']['uid'], 'standby-uid')
            update.reset_mock(); objects['engines.longhorn.io'][0]['status']['lastRestoredBackup'] = 'first'
            with self.assertRaisesRegex(ValueError, 'completed approved'):
                gate.activate(source, journal, target, 'standby', 'standby-uid', {'name': 'second'}, 'filesystem-uuid', proof, 's3://bucket/initial')
            update.assert_not_called()

    def test_fence_removes_only_original_consumers_and_never_force_detaches(self):
        lab, journal, objects = self.fixture()
        for kind, name, uid in (('pod', 'writer', 'writer-uid'), ('pvc', 'data', 'claim-uid'), ('pv', 'pv', 'pv-uid')):
            objects[kind] = {'metadata': {'name': name, 'uid': uid}, 'spec': {}}
        objects['pv']['spec'] = {'persistentVolumeReclaimPolicy': 'Retain', 'claimRef': {'uid': 'claim-uid'}, 'csi': {'volumeHandle': 'source'}}
        calls = []
        def remove(obj):
            kind = next(k for k in ('pod', 'pvc', 'pv') if objects[k] and objects[k]['metadata']['uid'] == obj['metadata']['uid'])
            calls.append(kind); objects[kind] = None
        lab.remove.side_effect = remove
        proof = gate.fence(lab, journal)
        self.assertEqual(calls, ['pod', 'pvc', 'pv']); self.assertEqual(proof['volume_uid'], 'volume-uid')
        lab.kubectl.assert_not_called()


class PointJournalContracts(unittest.TestCase):
    def test_secondary_backup_preserves_original_journal_and_reuses_binding(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); original = root / 'longhorn-install-business.json'; original.write_text('original-source')
            path = root / 'longhorn-dr-failback-business.json'; journal = {'volume': 'returned', 'namespace': 'longhorn-business-return'}
            probe = gate.Disaster.__new__(gate.Disaster); records = []; row = {'points': {}}
            backup = {'metadata': {'uid': 'backup-uid'}, 'status': {}}
            def run(pid, requests, **kwargs):
                name = requests[-1]['name']; backup['status'] = {'state': 'Completed', 'progress': 100,
                    'url': 's3://bucket/path?volume=returned&backup=' + name}
            def get(kind, name='', namespace=''):
                return backup if kind == 'backups.longhorn.io' else {'metadata': {'uid': 'snapshot-uid'}}
            product = SimpleNamespace(check_request=Mock(), request_object=lambda x: x)
            lab = SimpleNamespace(write_private=lambda p, j: records.append(str(p)), run_requests=run, get=get,
                load_helper=lambda name: product, bind_snapshots=Mock(side_effect=AssertionError('default journal binder')),
                bind_backups=Mock(side_effect=AssertionError('default journal binder')))
            probe.source = lab; probe.dr = SimpleNamespace(receipt=lambda: row, save=Mock())
            probe.store = SimpleNamespace(backup_receipt=lambda *a: {'incremental': False, 'newly_uploaded_bytes': 8})
            probe.data = lambda *a: 'confirmed-sha'
            holder = SimpleNamespace(receipt=lambda: {'product_binding': {'fixture_spec': {'backupTargetURL': 's3://bucket/path'},
                'secret_name': 'exact-secret'}}, product_configuration=Mock(side_effect=AssertionError('binding must not repeat')))
            result = probe.point('source', journal, path, 'payload', 'full', 12, holder)
            self.assertEqual(original.read_text(), 'original-source'); self.assertTrue(all(p == str(path) for p in records))
            self.assertEqual(result['source_backup_uid'], 'backup-uid'); self.assertEqual(result['sha256'], 'confirmed-sha')
            holder.product_configuration.assert_not_called()

    def test_standby_cannot_enter_static_pv_claim_mount(self):
        probe = gate.Disaster.__new__(gate.Disaster); lab, _ = standby_fixture()
        probe.context_lab = lambda role: lab
        with tempfile.TemporaryDirectory() as directory:
            probe.context_path = lambda role: Path(directory) / 'context.json'
            with self.assertRaisesRegex(ValueError, 'cannot bind'): probe.mount('target', 'standby', 'standby-uid', 1, 'longhorn-node-149')


class WorkflowContracts(unittest.TestCase):
    def test_two_points_then_fenced_activation_reverse_handoff_and_new_standby(self):
        events = []; row = {'points': {}}; probe = gate.Disaster.__new__(gate.Disaster)
        def side(lab): return 'source' if lab is probe.source else 'target'
        source = SimpleNamespace(owner=lambda: {'cluster_uid': 'source-cluster', 'run_id': 'run'}, WORKERS=('192.168.122.212', '192.168.122.165', '192.168.122.238'))
        target = SimpleNamespace(owner=lambda: {'cluster_uid': 'target-cluster'}, wait=lambda label, timeout, check: check())
        probe.source, probe.target, probe.source_pid, probe.target_pid = source, target, 3, 4
        probe.upgrade = SimpleNamespace(retained_identity=lambda *a: {'baseline': True})
        probe.target_fixture = SimpleNamespace(path=Path('/var/tmp/never-present-v1-dr-contract-'+str(id(probe))),
            prepare=lambda pid: events.append('prepare-target'), ready=lambda: {'v1_disk_uuid': 'fs'}, read=lambda: {'host': {'id': 'owned-filesystem'}})
        probe.store = SimpleNamespace(prepare=lambda: events.append('prepare-store'), qualify=lambda: None,
            product_configuration=Mock(return_value={}))
        probe.consumer = SimpleNamespace(initialize=lambda: None, qualify=lambda pid: None)
        original = {'volume': 'original', 'volume_uid': 'original-uid'}
        recovered = {'volume': 'recovered', 'volume_uid': 'recovered-uid'}
        returned = {'volume': 'returned', 'volume_uid': 'returned-uid'}
        names = iter((('standby', 'recovered-uid'), ('renewed-standby', 'renewed-uid')))
        def create(point):
            result = next(names); events.append(('create-standby', point['name'], result[1])); return result
        probe.dr = SimpleNamespace(backups=None, initialize=lambda: events.append('initialize'), receipt=lambda: row, save=lambda r: None,
            source_workload=lambda: original, business_path=Path('/source/original.json'),
            create_standby=create, restored=lambda name, uid, point: events.append(('restored', point['name'], uid)),
            discover=lambda point: events.append(('discover', point['name'])))
        def data(lab, journal, payload, read):
            events.append(('data', side(lab), payload, read)); return hashlib.sha256(payload.encode()).hexdigest()
        probe.data = data
        def point(side, journal, path, payload, mode, pid, holder):
            point = {'name': 'point-' + str(1 + sum(isinstance(e, tuple) and e[0] == 'backup' for e in events)),
                'url': 's3://exact', 'sha256': hashlib.sha256(payload.encode()).hexdigest()}
            events.append(('backup', side, mode, point['name'])); return point
        probe.point = point
        probe.mount = lambda role, *args: (events.append(('mount', role)) or (recovered if role == 'target' else returned))
        probe.restore_return = lambda point: ('returned', 'returned-uid')
        probe.context_path = lambda role: Path('/source/' + role + '.json')
        def proof(lab, journal): return {'proof': side(lab)}
        def fence(lab, journal): events.append(('fence', side(lab))); return proof(lab, journal)
        def activate(*args):
            if args[-2] is None: raise ValueError('V1 DR activation requires current source fencing proof')
            events.append(('activate', args[3]))
        with patch.object(gate, 'load', return_value=SimpleNamespace(healthy=lambda *a: None, empty=lambda *a: None)), \
                patch.object(gate, 'fenced', side_effect=proof), patch.object(gate, 'fence', side_effect=fence), \
                patch.object(gate, 'activate', side_effect=activate), patch.object(gate, 'activated', return_value=True), redirect_stdout(StringIO()):
            probe.qualify()
        backups = [e for e in events if isinstance(e, tuple) and e[0] == 'backup']
        self.assertEqual([(e[1], e[2]) for e in backups], [('source', 'full'), ('source', 'incremental'), ('target', 'full'), ('source', 'full')])
        self.assertLess(events.index(('fence', 'source')), events.index(('activate', 'standby')))
        self.assertLess(events.index(('fence', 'target')), events.index(('mount', 'failback')))
        self.assertEqual(row['verified']['renewed_standby_uid'], 'renewed-uid')
        self.assertIn(('restored', 'point-4', 'renewed-uid'), events)
        target_read = next(e for e in events if isinstance(e, tuple) and e[:2] == ('data', 'target') and e[3])
        self.assertEqual(target_read[2], 'run-second-confirmed-point')
        self.assertNotEqual(row['rpo']['unbacked_tail_sha256'], row['rpo']['restored_sha256'])
        probe.store.product_configuration.assert_called_once()

    def test_no_dr_journal_never_cleans_another_scenarios_target_filesystem(self):
        with tempfile.TemporaryDirectory() as directory:
            probe = gate.Disaster.__new__(gate.Disaster)
            probe.dr = SimpleNamespace(path=Path(directory) / 'absent.json')
            probe.target_fixture = SimpleNamespace(clean=Mock())
            probe.cleanup()
            probe.target_fixture.clean.assert_not_called()

    def test_secondary_cleanup_refuses_replaced_request_before_losing_ownership(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); path = root / 'dr.json'; path.write_text('{}')
            probe = gate.Disaster.__new__(gate.Disaster)
            journal = {'native_snapshots': {'snap': {'uid': 'accepted'}}, 'backups': {}}
            source = SimpleNamespace(restore_request_input=lambda: None,
                get=lambda *a: {'metadata': {'uid': 'replacement'}}, cleanup_business=Mock(),
                wait=lambda label, timeout, check: check())
            target = SimpleNamespace(restore_request_input=lambda: None)
            probe.source, probe.target = source, target
            probe.dr = SimpleNamespace(path=path, validate_intents=lambda: None, receipt=lambda: {'volumes': {}}, cleanup=Mock())
            holder = SimpleNamespace(record=root / 'missing.json')
            probe.consumer = probe.store = holder
            probe.retire_context = Mock(); probe.recover_context = lambda role: journal if role == 'failback' else None
            probe.context_lab = lambda role: source; probe.save_context = Mock()
            with self.assertRaisesRegex(ValueError, 'replaced during cleanup'): probe.cleanup()
            probe.save_context.assert_not_called(); source.cleanup_business.assert_not_called(); probe.dr.cleanup.assert_not_called()
            self.assertTrue(path.exists()); self.assertIn('native_snapshots', journal)


class CliContracts(unittest.TestCase):
    def test_terminal_pass_requires_both_leases_and_completed_cleanup(self):
        events = []
        @contextmanager
        def clean(callback):
            events.append('enter')
            yield
            callback(); events.append('cleaned')
        source = SimpleNamespace(require_lease=lambda: events.append('source-lease'), scoped_cleanup=clean)
        target = SimpleNamespace(require_lease=lambda: events.append('target-lease'))
        probe = SimpleNamespace(qualify=lambda: events.append('business'), cleanup=lambda: events.append('cleanup'))
        with patch.object(gate, 'load', return_value=SimpleNamespace(fixture=lambda: (source, target, None, None))), \
                patch.object(gate, 'Disaster', return_value=probe), \
                patch.object(sys, 'argv', ['gate', 'qualify', '--source-relay-pid', '3', '--target-relay-pid', '4']), redirect_stdout(StringIO()) as out:
            gate.main()
        self.assertEqual(events, ['source-lease', 'target-lease', 'enter', 'business', 'cleanup', 'cleaned'])
        self.assertEqual(out.getvalue().splitlines(), ['LONGHORN_V1_DR_BUSINESS_PASS'])


    def test_first_failure_diagnostics_precede_cleanup_and_keep_original_error(self):
        for diagnostic_failure in (False, True):
            with self.subTest(diagnostic_failure=diagnostic_failure):
                events = []; original = ValueError('first causal failure')
                @contextmanager
                def clean(callback):
                    try: yield
                    finally: callback()
                source = SimpleNamespace(require_lease=lambda: None, scoped_cleanup=clean)
                target = SimpleNamespace(require_lease=lambda: None)
                def qualify():
                    events.append('qualify'); raise original
                def diagnostics(*args):
                    self.assertIs(args[-1], original); events.append('diagnostics')
                    if diagnostic_failure: raise OSError('diagnostic collection unavailable')
                probe = SimpleNamespace(qualify=qualify, cleanup=lambda: events.append('cleanup'))
                with patch.object(gate, 'load', return_value=SimpleNamespace(fixture=lambda: (source, target, None, None))), \
                        patch.object(gate, 'Disaster', return_value=probe), patch.object(gate, 'capture_failure', side_effect=diagnostics), \
                        patch.object(sys, 'argv', ['gate', 'qualify', '--source-relay-pid', '3', '--target-relay-pid', '4']), \
                        redirect_stdout(StringIO()) as out:
                    with self.assertRaises(ValueError) as caught: gate.main()
                self.assertIs(caught.exception, original)
                self.assertEqual(events, ['qualify', 'diagnostics', 'cleanup'])
                self.assertNotIn('BUSINESS_PASS', out.getvalue())

    def test_diagnostics_are_private_and_exclude_original_config_and_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); work = root / 'diagnostics'; work.mkdir(mode=0o700)
            inputs = root / 'yml/longhorn'; inputs.mkdir(parents=True)
            (inputs / 'input.json').write_text('{"longhorn_version":"1.12.1"}')
            (root / 'longhorn-v1-dr.json').write_text('{"id":"owned-dr"}')
            (root / 'longhorn-v1-recovery-target.json').write_text('{"original_b64":"must-not-copy"}')
            (root / 'config.yml').write_text('password: must-not-copy')
            records = {}
            def save(path, data): records[path.name] = data
            result = SimpleNamespace(returncode=0, stdout='state', stderr='')
            lab = SimpleNamespace(DIRECTORY=root, record_failure=Mock(), write_private=save, kubectl=Mock(return_value=result))
            with patch.object(gate.tempfile, 'mkdtemp', return_value=str(work)), redirect_stdout(StringIO()):
                gate.capture_failure(lab, lab, ValueError('cause'))
            self.assertEqual(len(records), 14)
            self.assertTrue(any('pod-descriptions' in name for name in records))
            self.assertTrue(any('manager-logs' in name for name in records))
            self.assertFalse(any('recovery-target' in name or 'config.yml' in name for name in records))
            self.assertNotIn('must-not-copy', json.dumps(records))
            for call in lab.kubectl.call_args_list:
                self.assertEqual(call.kwargs, {'check': False, 'timeout': 15})
                self.assertEqual(call.args[0], '--request-timeout=5s')

    def test_wrapper_requires_product_result_then_owned_cleanup_before_relays_stop(self):
        text = (ROOT / 'tests/helpers/longhorn-v1-disaster.sh').read_text()
        self.assertIn('set -Eeuo pipefail', text)
        self.assertIn('LONGHORN_PARENT_LEASE_HELD:-no', text)
        self.assertLess(text.index('"$DRIVER" cleanup'), text.index('kill -TERM'))
        self.assertLess(text.index('"$SOURCE" verify-core'), text.index('echo LONGHORN_DISASTER_RECOVERY_PASS'))
        for marker in ('LONGHORN_V1_DR_BUSINESS_PASS', 'LONGHORN_V1_DR_FAILBACK_RENEWED',
                       'LONGHORN_V1_DR_ACTIVATED_BUSINESS', 'LONGHORN_V1_DR_SCOPED_CLEAN_PASS'):
            self.assertIn(marker, text)
        self.assertIn('> "$work/status.tmp"', text)
        self.assertIn('mv "$work/status.tmp" "$work/status"', text)


if __name__ == '__main__': unittest.main()
