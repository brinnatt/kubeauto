"""Last-replica refusal, bounded native faults and protected recovery material."""
import ast
from contextlib import redirect_stdout
import copy
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location('last23', ROOT/'tests/helpers/longhorn-last-replica.py')
GATE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(GATE)


class LastReplicaContracts(unittest.TestCase):
    def fixture(self, base):
        addresses = tuple('192.168.122.'+str(i) for i in (212, 165, 238))
        nodes = {'longhorn-node-'+a.rsplit('.', 1)[1]: {'metadata': {'name': 'longhorn-node-'+a.rsplit('.', 1)[1],
            'uid': 'node-'+a, 'resourceVersion': '7'}, 'spec': {'allowScheduling': True}} for a in addresses}
        rows = [{'metadata': {'name': 'replica-'+str(i), 'uid': 'replica-uid-'+str(i), 'resourceVersion': '12'},
            'spec': {'volumeName': 'volume', 'nodeID': name, 'healthyAt': 'healthy', 'failedAt': ''},
            'status': {'currentState': 'running'}} for i, name in enumerate(nodes)]
        objects = {'nodes.longhorn.io': nodes, 'replicas.longhorn.io': rows,
            'settings.longhorn.io': {'metadata': {'uid': 'policy'}, 'value': 'false'},
            'backups.longhorn.io': {'metadata': {'uid': 'backup-uid'}},
            'volumes.longhorn.io': {'metadata': {'uid': 'volume-uid'}, 'spec': {'dataEngine': 'v1'},
                                  'status': {'robustness': 'faulted'}},
            'engines.longhorn.io': [{'metadata': {'uid': 'engine-uid'}, 'spec': {'volumeName': 'volume'}, 'status': {}}]}
        def get(kind, name=None, namespace=None):
            value = objects[kind]
            if kind == 'nodes.longhorn.io': return copy.deepcopy(value[name])
            if kind in ('replicas.longhorn.io', 'engines.longhorn.io'):
                return {'items': copy.deepcopy(value)} if name is None else next(
                    (copy.deepcopy(r) for r in value if r['metadata']['name'] == name), None)
            return copy.deepcopy(value)
        def kubectl(*args, **kwargs):
            if 'patch' in args:
                name = args[args.index('nodes.longhorn.io')+1]
                values = json.loads(kwargs['stdin'])
                self.assertEqual(objects['nodes.longhorn.io'][name]['metadata']['uid'], values[0]['value'])
                self.assertEqual('7', values[1]['value'])
                self.assertEqual(objects['nodes.longhorn.io'][name]['spec']['allowScheduling'], values[2]['value'])
                objects['nodes.longhorn.io'][name]['spec']['allowScheduling'] = values[3]['value']
            return SimpleNamespace(stdout='', stderr='', returncode=0)
        lab = SimpleNamespace(DIRECTORY=base, WORKERS=addresses, get=Mock(side_effect=get), kubectl=Mock(side_effect=kubectl),
            owner=lambda: {'run_id': 'run', 'cluster_uid': 'cluster', 'namespace_uid': 'namespace'},
            write_private=Mock(), check_business_graph=Mock(), installation_health=Mock(),
            cleanup_business=Mock(), restore_request_input=Mock(), backup_store=Mock(return_value=SimpleNamespace(cleanup=Mock())))
        lab.wait = Mock(side_effect=lambda label, timeout, callback: callback())
        lab.write_private.side_effect = lambda path, row: path.write_text(json.dumps(row))
        identity = {'run_id': 'run', 'cluster_uid': 'cluster', 'token': 'a'*32}
        (base/GATE.INTENT).write_text(json.dumps(dict(identity, stage='store-prepare-intent')))
        journal = {'volume': 'volume', 'volume_uid': 'volume-uid', 'namespace': 'business', GATE.OWNER: identity}
        receipt = {'volume': 'volume', 'backup': 'backup', 'objects': {'key': {'sha256': 'a'*64}}}
        journal['backups'] = {'backup': {'object_receipt': receipt}}
        faults = GATE.load('longhorn-faults.py')
        control = GATE.load('longhorn-control-faults.py')
        token = 'run:all-replicas'
        records = [{'type': 'ack', 'sequence': i, 'sha256': hashlib.sha256(faults.ledger_block(token, i)).hexdigest()} for i in range(4)]
        checkpoint = dict(control.confirmed(records, token, faults), path='/data/fault-ledger', retries=[])
        point = {'source_volume_uid': 'volume-uid', 'preflight_restore_verified': True, 'preflight_volume_uid': 'preflight',
            'backup': 'backup', 'backup_uid': 'backup-uid', 'checkpoint': checkpoint,
            'object_receipt_sha256': hashlib.sha256(json.dumps(receipt, sort_keys=True).encode()).hexdigest()}
        native = {r['metadata']['name']: {'name': r['metadata']['name'], 'replica_uid': r['metadata']['uid'],
            'uuid': 'uuid-'+str(i), 'node': r['spec']['nodeID']} for i, r in enumerate(rows)}
        common = SimpleNamespace(source=Mock(return_value={'uid': 'engine-uid'}))
        process = SimpleNamespace(replica_process=Mock(side_effect=lambda l,j,r: native[r['metadata']['name']]),
            stop_replica_process=Mock(side_effect=lambda l,j,r: native[r['metadata']['name']]))
        original_load = GATE.load
        def load(name):
            if name == 'longhorn-v1-stability.py': return common
            if name == 'longhorn-faults.py':
                process.ledger_block = faults.ledger_block
                process.BLOCK = faults.BLOCK
                return process
            return original_load(name)
        return lab, journal, point, objects, native, process, load

    def injected(self, lab, journal, point, loader):
        with patch.object(GATE, 'load', side_effect=loader), redirect_stdout(io.StringIO()):
            return GATE.inject(lab, journal, point)

    def test_last_replica_uses_native_dry_run_uid_and_rv_and_requires_exact_webhook_refusal(self):
        for response in ('refused', 'accepted', 'different-error'):
            with self.subTest(response=response), tempfile.TemporaryDirectory() as d:
                lab, journal, _, objects, _, _, _ = self.fixture(Path(d))
                objects['replicas.longhorn.io'] = objects['replicas.longhorn.io'][:1]
                message = 'no other healthy replica available for volume volume, cannot delete replica replica-0 since it may still contain data for recovery'
                lab.kubectl = Mock(return_value=SimpleNamespace(returncode=0 if response == 'accepted' else 1,
                    stdout='', stderr=message if response == 'refused' else 'forbidden'))
                maintenance = SimpleNamespace(scale=Mock(), temporary_replica_balance=Mock(),
                                              restore_replica_balance=Mock())
                with patch.object(GATE, 'load', return_value=maintenance), redirect_stdout(io.StringIO()):
                    if response == 'refused': GATE.last_replica_guard(lab, journal)
                    else:
                        with self.assertRaises(ValueError): GATE.last_replica_guard(lab, journal)
                args, kwargs = lab.kubectl.call_args
                self.assertEqual('delete', args[0]); self.assertIn('--raw', args)
                options = json.loads(kwargs['stdin'])
                self.assertEqual(['All'], options['dryRun'])
                self.assertEqual({'uid': 'replica-uid-0', 'resourceVersion': '12'}, options['preconditions'])
                self.assertEqual([1, 3] if response == 'refused' else [1], [c.args[-1] for c in maintenance.scale.call_args_list])

    def test_backup_identity_or_invalid_ack_stops_before_any_mutation(self):
        for defect in ('preflight', 'same-source', 'source', 'backup', 'receipt', 'ack'):
            with self.subTest(defect=defect), tempfile.TemporaryDirectory() as d:
                lab, journal, point, objects, _, process, loader = self.fixture(Path(d))
                if defect == 'preflight': point['preflight_restore_verified'] = False
                if defect == 'same-source': point['preflight_volume_uid'] = journal['volume_uid']
                if defect == 'source': point['source_volume_uid'] = 'foreign'
                if defect == 'backup': objects['backups.longhorn.io']['metadata']['uid'] = 'foreign'
                if defect == 'receipt': point['object_receipt_sha256'] = 'b'*64
                if defect == 'ack': point['checkpoint']['records'][0]['sha256'] = 'b'*64
                with self.assertRaises(ValueError): self.injected(lab, journal, point, loader)
                lab.kubectl.assert_not_called(); process.replica_process.assert_not_called()
                self.assertNotIn(GATE.KEY, journal)

    def test_scope_is_double_read_before_parallel_deletes_and_each_intent_is_durable(self):
        with tempfile.TemporaryDirectory() as d:
            lab, journal, point, objects, native, process, loader = self.fixture(Path(d))
            barrier = threading.Barrier(3)
            def stop(l, j, row):
                self.assertEqual(6, process.replica_process.call_count)
                self.assertEqual(set(native), set(j[GATE.KEY]['process_intents']))
                self.assertTrue(all(not r['spec']['allowScheduling'] for r in objects['nodes.longhorn.io'].values()))
                self.assertTrue(lab.write_private.called)
                barrier.wait(timeout=2)
                return native[row['metadata']['name']]
            process.stop_replica_process.side_effect = stop
            proof = self.injected(lab, journal, point, loader)
            self.assertEqual(native, proof['results']); self.assertEqual(3, process.stop_replica_process.call_count)
            with self.assertRaises(ValueError): self.injected(lab, journal, point, loader)
            self.assertEqual(3, process.stop_replica_process.call_count)

    def test_changed_scope_prevents_all_mutations(self):
        with tempfile.TemporaryDirectory() as d:
            lab, journal, point, _, native, process, loader = self.fixture(Path(d))
            calls = 0
            def changed(l, j, row):
                nonlocal calls
                calls += 1
                value = dict(native[row['metadata']['name']])
                if calls == 6: value['uuid'] = 'replaced'
                return value
            process.replica_process.side_effect = changed
            with self.assertRaises(ValueError): self.injected(lab, journal, point, loader)
            lab.kubectl.assert_not_called(); process.stop_replica_process.assert_not_called()
            self.assertNotIn(GATE.KEY, journal)

    def test_partial_parallel_failure_collects_results_then_restores_exact_scheduling_and_retains_data(self):
        with tempfile.TemporaryDirectory() as d:
            lab, journal, point, objects, native, process, loader = self.fixture(Path(d))
            first = next(iter(objects['nodes.longhorn.io'].values()))
            first['spec']['allowScheduling'] = False
            def stop(l, j, row):
                if row['metadata']['name'] == 'replica-1': raise TimeoutError('ambiguous RPC')
                return native[row['metadata']['name']]
            process.stop_replica_process.side_effect = stop
            with self.assertRaises(TimeoutError): self.injected(lab, journal, point, loader)
            self.assertEqual(3, process.stop_replica_process.call_count)
            self.assertEqual(2, len(journal[GATE.KEY]['results']))
            self.assertEqual({'replica-1': 'TimeoutError'}, journal[GATE.KEY]['process_errors'])
            (Path(d)/GATE.RECORD).write_text(json.dumps(journal))
            with redirect_stdout(io.StringIO()), self.assertRaises(ValueError): GATE.cleanup(lab, 19)
            self.assertEqual(journal[GATE.KEY]['nodes'], GATE.node_state(lab))
            self.assertFalse(first['spec']['allowScheduling'])
            lab.cleanup_business.assert_not_called(); lab.backup_store.assert_not_called()

    def test_scheduling_restore_rejects_changed_uid_and_patches_with_uid_resource_version(self):
        with tempfile.TemporaryDirectory() as d:
            lab, journal, point, objects, _, _, loader = self.fixture(Path(d))
            self.injected(lab, journal, point, loader)
            item = next(iter(objects['nodes.longhorn.io'].values()))
            item['metadata']['uid'] = 'foreign'
            with self.assertRaises(ValueError): GATE.restore_scheduling(lab, journal)
            self.assertTrue(all(not r['spec']['allowScheduling'] for r in objects['nodes.longhorn.io'].values()))

    def test_refusal_requires_all_original_failed_native_identities_and_no_salvage(self):
        for defect in ('none', 'healthy', 'no-failed', 'no-healthy', 'replica-uid', 'engine-uid', 'requested', 'executed', 'policy'):
            with self.subTest(defect=defect), tempfile.TemporaryDirectory() as d:
                lab, journal, point, objects, _, _, loader = self.fixture(Path(d))
                self.injected(lab, journal, point, loader)
                for row in objects['replicas.longhorn.io']: row['spec']['failedAt'] = 'failed'
                if defect == 'healthy': objects['volumes.longhorn.io']['status']['robustness'] = 'healthy'
                if defect == 'no-failed': objects['replicas.longhorn.io'][0]['spec']['failedAt'] = ''
                if defect == 'no-healthy': objects['replicas.longhorn.io'][0]['spec']['healthyAt'] = ''
                if defect == 'replica-uid': objects['replicas.longhorn.io'][0]['metadata']['uid'] = 'foreign'
                if defect == 'engine-uid': objects['engines.longhorn.io'][0]['metadata']['uid'] = 'foreign'
                if defect == 'requested': objects['engines.longhorn.io'][0]['spec']['salvageRequested'] = True
                if defect == 'executed': objects['engines.longhorn.io'][0]['status']['salvageExecuted'] = True
                if defect == 'policy': objects['settings.longhorn.io']['value'] = 'true'
                if defect == 'none': self.assertEqual('faulted', GATE.faulted(lab, journal)['robustness'])
                elif defect in ('healthy', 'no-failed', 'no-healthy'): self.assertIsNone(GATE.faulted(lab, journal))
                else:
                    with self.assertRaises(ValueError): GATE.faulted(lab, journal)

    def test_io_refusal_requires_same_writer_explicit_errno_and_no_ack_or_timeout(self):
        for defect in ('none', 'ack', 'generic-error', 'missing-begin', 'timeout', 'new-pod'):
            with self.subTest(defect=defect), tempfile.TemporaryDirectory() as d:
                lab, journal, _, _, _, _, _ = self.fixture(Path(d))
                lab.get = Mock(return_value={'metadata': {'uid': 'writer'}})
                def command(*args, **kw):
                    code = args[-1]
                    token = next(line for line in code.splitlines() if line.startswith('token='))[7:-1]
                    begin = {'token': token, 'phase': 'begin'}
                    end = {'token': token, 'phase': 'refused', 'errno': 5}
                    if defect == 'ack': end = {'token': token, 'phase': 'ack'}
                    if defect == 'generic-error': end['errno'] = 13
                    if defect == 'timeout': raise TimeoutError('exec timeout')
                    if defect == 'new-pod': lab.get.return_value = {'metadata': {'uid': 'new'}}
                    return SimpleNamespace(stdout='\n'.join(json.dumps(r) for r in ([end] if defect == 'missing-begin' else [begin,end])))
                lab.kubectl = Mock(side_effect=command)
                if defect == 'none': self.assertEqual(5, GATE.io_refusal(lab, journal, 'writer')['errno'])
                else:
                    with self.assertRaises((ValueError, TimeoutError)): GATE.io_refusal(lab, journal, 'writer')

    def test_actual_probe_reports_oserror_and_never_fabricates_ack(self):
        ast.parse(GATE.refusal_program('token'))
        for failure in (False, True):
            with self.subTest(failure=failure):
                stream = io.StringIO()
                with patch('os.open', side_effect=OSError(5, 'I/O error') if failure else None, return_value=42), \
                        patch('os.write', return_value=19), patch('os.fsync'), patch('os.close'), redirect_stdout(stream):
                    exec(GATE.refusal_program('token'), {})
                rows = [json.loads(s) for s in stream.getvalue().splitlines()]
                self.assertEqual('begin', rows[0]['phase'])
                self.assertEqual('refused' if failure else 'ack', rows[1]['phase'])

    def test_verified_cleanup_restores_scheduling_before_owned_business_and_external_store(self):
        with tempfile.TemporaryDirectory() as d:
            lab, journal, point, _, _, _, loader = self.fixture(Path(d))
            self.injected(lab, journal, point, loader)
            journal[GATE.KEY]['verified'] = True
            (Path(d)/GATE.RECORD).write_text(json.dumps(journal))
            order = []
            lab.installation_health.side_effect = lambda: order.append('health')
            lab.cleanup_business.side_effect = lambda **kw: order.append('owned-delete')
            lab.backup_store.return_value.cleanup.side_effect = lambda: order.append('store')
            def saved(path, row):
                if row[GATE.KEY].get('scheduling_restored'): order.append('scheduling')
                path.write_text(json.dumps(row))
            lab.write_private.side_effect = saved
            def remove_owned(**kw):
                order.append('owned-delete'); (Path(d)/GATE.RECORD).unlink()
            lab.cleanup_business.side_effect = remove_owned
            original_write = lab.write_private.side_effect
            lab.write_private.side_effect = lambda path,row: original_write(path,row) if GATE.KEY in row else path.write_text(json.dumps(row))
            with redirect_stdout(io.StringIO()): GATE.cleanup(lab, 19)
            self.assertEqual(['scheduling', 'health', 'owned-delete', 'store'], order)

    def test_partial_setup_cleanup_uses_own_intent_and_never_generic_business_deletion(self):
        with tempfile.TemporaryDirectory() as d:
            lab, _, _, _, _, _, _ = self.fixture(Path(d))
            with redirect_stdout(io.StringIO()): GATE.cleanup(lab, 19)
            lab.restore_request_input.assert_not_called()
            lab.backup_store.return_value.cleanup.assert_called_once()
            lab.cleanup_business.assert_not_called()

    def test_cleanup_refuses_foreign_business_store_or_control_receipts_before_any_mutation(self):
        for receipt in (GATE.RECORD, GATE.STORE[0], GATE.FOREIGN[0]):
            for has_intent in (False, True):
                with self.subTest(receipt=receipt, has_intent=has_intent), tempfile.TemporaryDirectory() as d:
                    lab, _, _, _, _, _, _ = self.fixture(Path(d))
                    if not has_intent: (Path(d)/GATE.INTENT).unlink()
                    (Path(d)/receipt).write_text(json.dumps({GATE.OWNER: {'token': 'foreign'}}))
                    with self.assertRaises(ValueError): GATE.cleanup(lab, 19)
                    lab.check_business_graph.assert_not_called(); lab.kubectl.assert_not_called()
                    lab.cleanup_business.assert_not_called(); lab.backup_store.assert_not_called()
                    lab.restore_request_input.assert_not_called()

    def test_begin_refuses_prior_shared_material_and_native_consumers_before_store_creation(self):
        for defect in ('none', 'business', 'store', 'operation', 'native'):
            with self.subTest(defect=defect), tempfile.TemporaryDirectory() as d:
                lab, _, _, _, _, _, _ = self.fixture(Path(d))
                (Path(d)/GATE.INTENT).unlink()
                lab.get = Mock(return_value={'items': []})
                if defect == 'business': (Path(d)/GATE.RECORD).write_text('{}')
                if defect == 'store': (Path(d)/GATE.STORE[1]).mkdir()
                if defect == 'operation': (Path(d)/GATE.FOREIGN[-1]).write_text('{}')
                if defect == 'native': lab.get.return_value = {'items': [{'spec': {}}]}
                if defect == 'none':
                    owner = GATE.begin(lab)
                    self.assertEqual(owner, GATE.read_owner(lab, Path(d)/GATE.INTENT))
                else:
                    with self.assertRaises(ValueError): GATE.begin(lab)
                    self.assertFalse((Path(d)/GATE.INTENT).exists())
                lab.backup_store.assert_not_called(); lab.kubectl.assert_not_called()

    def test_first_store_receipt_is_tagged_and_partial_setup_cleans_only_this_store(self):
        with tempfile.TemporaryDirectory() as d:
            lab, _, _, _, _, _, _ = self.fixture(Path(d))
            identity = GATE.owner_id(GATE.read_owner(lab, Path(d)/GATE.INTENT))
            private = GATE.StoreLab(lab, identity)
            record = Path(d)/GATE.STORE[0]
            first = {'run_id': 'run', 'cluster_uid': 'cluster'}
            private.write_private(record, first)
            self.assertEqual(identity, json.loads(record.read_text())[GATE.OWNER])
            (Path(d)/GATE.STORE[1]).mkdir()
            def clean():
                record.unlink(); (Path(d)/GATE.STORE[1]).rmdir()
            lab.backup_store.return_value.cleanup.side_effect = clean
            with redirect_stdout(io.StringIO()): GATE.cleanup(lab, 19)
            self.assertFalse((Path(d)/GATE.INTENT).exists())
            self.assertEqual('complete', GATE.read_owner(lab, Path(d)/GATE.COMPLETE)['stage'])
            lab.cleanup_business.assert_not_called(); lab.check_business_graph.assert_not_called()

    def test_repeat_cleanup_requires_retired_own_receipt_and_all_shared_material_absent(self):
        with tempfile.TemporaryDirectory() as d:
            lab, _, _, _, _, _, _ = self.fixture(Path(d))
            with redirect_stdout(io.StringIO()): GATE.cleanup(lab, 19)
            lab.backup_store.reset_mock()
            with redirect_stdout(io.StringIO()): GATE.cleanup(lab, 19)
            lab.backup_store.assert_not_called(); lab.cleanup_business.assert_not_called()
            (Path(d)/GATE.STORE[0]).write_text('{}')
            with self.assertRaises(ValueError): GATE.cleanup(lab, 19)
            lab.backup_store.assert_not_called()

    def test_product_policy_reassertion_uses_kubecli_and_preserves_setting_uid(self):
        with tempfile.TemporaryDirectory() as d:
            lab, _, _, objects, _, _, _ = self.fixture(Path(d))
            lab.run_requests = Mock()
            # Invalid empty baseline stops before fixture mutation, after the
            # product-owned configuration and its exact Setting identity check.
            lab.get.side_effect = lambda kind,*a,**kw: objects[kind] if kind == 'settings.longhorn.io' else {'items': [1]}
            with self.assertRaises(ValueError): GATE.scenario(lab, 19, Mock(), {'fixture': 'configuration'})
            lab.run_requests.assert_called_once_with(19, [], backup_configuration={'fixture': 'configuration'})
            lab.kubectl.assert_not_called()
            objects['settings.longhorn.io']['value'] = 'true'
            lab.run_requests.reset_mock()
            with self.assertRaises(ValueError): GATE.scenario(lab, 19, Mock(), {})
            lab.run_requests.assert_not_called()

    def test_recovery_consumes_same_restore_tested_backup_and_retains_material_if_receipt_changed(self):
        for corrupt_receipt in (False, True):
            with self.subTest(corrupt_receipt=corrupt_receipt), tempfile.TemporaryDirectory() as d:
                base = Path(d)
                _, _, point, _, _, _, _ = self.fixture(base)
                pods = {}; order = []; stored = {'volume': 'volume', 'backup': 'backup', 'objects': {'key': 'hash'}}
                digest = hashlib.sha256(b'run'*262144).hexdigest()
                policy = {'uid': 'policy', 'value': 'false'}
                def get(kind, name=None, namespace=None):
                    if kind == 'storageclass': return {'provisioner': 'driver.longhorn.io', 'reclaimPolicy': 'Delete',
                        'parameters': {'dataEngine': 'v1'}, 'metadata': {'labels': {'kubeauto.io/component': 'longhorn'}}}
                    if kind == 'volumes.longhorn.io': return {'items': []}
                    if kind == 'pod': return pods.get(name)
                    raise AssertionError(kind)
                def command(*args, **kwargs):
                    if args[0] == 'create': return SimpleNamespace(stdout=json.dumps({'metadata': {'uid': 'business-ns'}}))
                    return SimpleNamespace(stdout=digest)
                def write(path, value): path.write_text(json.dumps(value))
                lab = SimpleNamespace(DIRECTORY=base, WORKERS=('192.168.122.212', '192.168.122.165', '192.168.122.238'),
                    run_requests=Mock(), installation_health=Mock(), get=Mock(side_effect=get), kubectl=Mock(side_effect=command),
                    write_private=write, owner=lambda: {'run_id': 'run', 'cluster_uid': 'cluster'}, namespace_defaults=lambda namespace: {'fixture': 'defaults'},
                    create_business_object=Mock(), payload_program=lambda *a,**kw: 'payload',
                    remove=lambda row: pods.pop(row['metadata']['name']))
                lab.wait = lambda label, timeout, callback: callback()
                (base/GATE.INTENT).write_text(json.dumps({'run_id': 'run', 'cluster_uid': 'cluster', 'token': 'a'*32, 'stage': 'store-prepare-intent'}))
                def claim(l, journal, create, name):
                    journal.update(volume='volume', volume_uid='volume-uid')
                def client(l, journal, create, name, claim, worker):
                    row = {'metadata': {'uid': name+'-uid', 'name': name}}
                    pods[name] = row; journal['objects']['Pod/'+name] = row['metadata']['uid']
                    return row
                def backup(l, journal, pid, store, config, checkpoint):
                    order.append('backup')
                    journal['backups'] = {'backup': {'object_receipt': stored}}
                    return copy.deepcopy(point)
                def restore(l, journal, create, checkpoint, name, payload, payload_digest):
                    order.append(name)
                    self.assertEqual('backup', checkpoint['backup'])
                    if name == 'postfault-restore': self.assertIn('fault', order)
                    client(l, journal, create, name, name, l.WORKERS[1])
                    return {'volume_uid': name+'-volume'}
                def inject(l, journal, checkpoint):
                    self.assertTrue(checkpoint['preflight_restore_verified'])
                    self.assertEqual('preflight-restore-volume', checkpoint['preflight_volume_uid'])
                    self.assertLess(order.index('preflight-restore'), len(order))
                    order.append('fault'); journal[GATE.KEY] = {'verified': False, 'policy': policy}
                    return journal[GATE.KEY]
                shared = SimpleNamespace(policy=Mock(return_value=policy), create_claim=claim, client=client,
                    ledger=Mock(side_effect=lambda l,j,c,t,p: dict(point['checkpoint'], token=t, path=p)), backup=backup,
                    restore=Mock(side_effect=restore), retire_preflight=lambda *a: order.append('retired-preflight'), verify_ledger=Mock())
                store = SimpleNamespace(backup_receipt=Mock(return_value={'changed': True} if corrupt_receipt else stored))
                def cleaned(l, pid):
                    saved = json.loads((base/GATE.RECORD).read_text())
                    self.assertTrue(saved[GATE.KEY]['verified'])
                    self.assertEqual('postfault-restore-volume', saved[GATE.KEY]['restored_volume_uid'])
                    order.append('cleanup')
                with patch.object(GATE, 'load', return_value=shared), patch.object(GATE, 'last_replica_guard'), \
                        patch.object(GATE, 'inject', side_effect=inject), patch.object(GATE, 'io_refusal', return_value={'phase': 'refused', 'errno': 5}), \
                        patch.object(GATE, 'faulted', return_value={'volume_uid': 'volume-uid', 'robustness': 'faulted'}), \
                        patch.object(GATE, 'restore_scheduling'), patch.object(GATE.time, 'sleep'), \
                        patch.object(GATE, 'cleanup', side_effect=cleaned), redirect_stdout(io.StringIO()):
                    if corrupt_receipt:
                        with self.assertRaises(ValueError): GATE.scenario(lab, 19, store, {})
                        self.assertNotIn('cleanup', order)
                        saved = json.loads((base/GATE.RECORD).read_text())
                        self.assertFalse(saved[GATE.KEY]['verified'])
                    else:
                        GATE.scenario(lab, 19, store, {})
                        self.assertEqual('cleanup', order[-1])
                self.assertEqual(2, shared.restore.call_count)
                self.assertEqual(3, shared.verify_ledger.call_count)
                self.assertLess(order.index('retired-preflight'), order.index('fault'))


class RealScaleContracts(unittest.TestCase):
    """Use the real scale callee and its pinned native cleanup prerequisite."""

    def fixture(self, base, policy='ignored', refuse=True):
        volume = {'metadata': {'uid': 'volume-uid'}, 'spec': {
            'numberOfReplicas': 3, 'replicaAutoBalance': policy, 'dataEngine': 'v1'}}
        selected, calls = [], []
        journal = {'volume': 'volume', 'volume_uid': 'volume-uid'}

        def replicas(count):
            selected[:] = [{'metadata': {'name': 'replica-'+str(i), 'uid': 'replica-uid-'+str(i),
                'resourceVersion': '7'}, 'spec': {'volumeName': 'volume', 'healthyAt': 'yes', 'failedAt': ''},
                'status': {'currentState': 'running'}} for i in range(count)]

        replicas(3)

        def get(kind, *args, **kwargs):
            return copy.deepcopy(volume if kind == 'volumes.longhorn.io' else {'items': selected})

        def kubectl(*args, stdin, **kwargs):
            if args[0] == 'delete':
                calls.append('delete-dryrun')
                self.assertEqual(1, len(selected))
                self.assertEqual(['All'], json.loads(stdin)['dryRun'])
                return SimpleNamespace(returncode=1 if refuse else 0, stdout='', stderr=(
                    'no other healthy replica available for volume volume, cannot delete replica replica-0 '
                    'since it may still contain data for recovery'))
            for operation in json.loads(stdin):
                key = operation['path'].rsplit('/', 1)[1]
                target = volume['metadata'] if key == 'uid' else volume['spec']
                if operation['op'] == 'test':
                    self.assertEqual(target[key], operation['value'])
                else:
                    if key == 'replicaAutoBalance':
                        self.assertIn('replica_balance', json.loads((base/GATE.RECORD).read_text()))
                    target[key] = operation['value']
                    calls.append(key+'='+str(operation['value']))

        def topology(journal, count):
            if count < len(selected) and volume['spec']['replicaAutoBalance'] in ('ignored', 'disabled'):
                return None  # Global disabled: native cleanupAutoBalancedReplicas skips reduction.
            replicas(count)
            return {row['metadata']['name']: {'uid': row['metadata']['uid']} for row in selected}

        def wait(label, timeout, check):
            result = check()
            if not result:
                raise RuntimeError('native disabled-balance scale-down cannot converge: '+label)
            return result

        lab = SimpleNamespace(DIRECTORY=base, get=get, kubectl=kubectl, replica_topology=topology,
            wait=wait, write_private=lambda path, row: path.write_text(json.dumps(row)))
        return lab, journal, volume, calls

    def test_real_scale_down_guard_up_and_original_policy_restoration(self):
        for policy in ('ignored', 'disabled', 'least-effort', 'best-effort'):
            with self.subTest(policy=policy), tempfile.TemporaryDirectory() as directory:
                lab, journal, volume, calls = self.fixture(Path(directory), policy)
                with redirect_stdout(io.StringIO()):
                    GATE.last_replica_guard(lab, journal)
                self.assertEqual(3, volume['spec']['numberOfReplicas'])
                self.assertEqual(policy, volume['spec']['replicaAutoBalance'])
                self.assertLess(calls.index('numberOfReplicas=1'), calls.index('delete-dryrun'))
                self.assertLess(calls.index('delete-dryrun'), calls.index('numberOfReplicas=3'))
                self.assertFalse((Path(directory)/'longhorn-maintenance.json').exists())

    def test_rejected_guard_retains_original_balance_intent_for_scoped_recovery(self):
        with tempfile.TemporaryDirectory() as directory:
            lab, journal, volume, _ = self.fixture(Path(directory), refuse=False)
            with self.assertRaisesRegex(ValueError, 'explicitly refuse'):
                GATE.last_replica_guard(lab, journal)
            self.assertEqual('least-effort', volume['spec']['replicaAutoBalance'])
            GATE.load('longhorn-maintenance.py').restore_replica_balance(lab, journal, journal)
            self.assertEqual('ignored', volume['spec']['replicaAutoBalance'])

    def test_unknown_policy_or_volume_replacement_blocks_before_any_patch(self):
        for defect in ('policy', 'volume'):
            with self.subTest(defect=defect), tempfile.TemporaryDirectory() as directory:
                lab, journal, volume, calls = self.fixture(Path(directory))
                if defect == 'policy':
                    volume['spec']['replicaAutoBalance'] = 'foreign'
                else:
                    volume['metadata']['uid'] = 'foreign'
                with self.assertRaises(ValueError):
                    GATE.last_replica_guard(lab, journal)
                self.assertEqual([], calls)

    def test_cleanup_refuses_changed_policy_before_deleting_recovery_material(self):
        with tempfile.TemporaryDirectory() as directory:
            lab, journal, _, objects, _, _, _ = LastReplicaContracts().fixture(Path(directory))
            journal['replica_balance'] = {'volume': 'volume', 'uid': 'volume-uid',
                                         'original': 'ignored', 'temporary': 'least-effort'}
            objects['volumes.longhorn.io']['spec']['replicaAutoBalance'] = 'foreign'
            (Path(directory)/GATE.RECORD).write_text(json.dumps(journal))
            with self.assertRaisesRegex(ValueError, 'ownership or policy'):
                GATE.cleanup(lab, 19)
            lab.cleanup_business.assert_not_called()
            lab.backup_store.assert_not_called()


if __name__ == '__main__':
    unittest.main()
