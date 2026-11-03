"""Small deterministic contracts before the owned, real 30-minute V2 gate."""
import ast
import copy
import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import zipfile

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location('v2_observability', ROOT/'tests/helpers/longhorn-v2-observability.py')
GATE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(GATE)


def ledger_rows():
    token = 'owned:observability-30min'
    faults = GATE.load('longhorn-faults.py')
    records = [{'type': 'ack', 'sequence': i,
                'sha256': hashlib.sha256(faults.ledger_block(token, i)).hexdigest()}
               for i in range(4)]
    proof = GATE.load('longhorn-control-faults.py').confirmed(records, token, faults)
    rows = [{'type': 'started', 'token': token, 'pid': 123, 'start_tick': '456'}]
    rows += [dict(r, observed_elapsed_s=t) for r, t in zip(records, (0., 600., 1200., 1799.))]
    rows.append(dict(proof['summary'], type='complete', observed_elapsed_s=1800., errors={}, retries=0, max_latency_s=.1))
    return token, rows


def full_log():
    token, rows = ledger_rows()
    proof = GATE.confirmed_load(rows, token)
    result = []
    def emit(marker, row): result.append(marker+' '+json.dumps(row, sort_keys=True))
    emit('LONGHORN_V2_REAL_PROMETHEUS_RULES', {'volume_uid': 'volume', 'alert_prefix': 'Owned', 'bounded_space_threshold_bytes': 1})
    for suffix, states in {'Degraded': [False, True, False], 'Space': [False, True, False],
                           'Backup': [True, False], 'Missing': [False, True, False]}.items():
        for i, active in enumerate(states):
            emit('LONGHORN_V2_REAL_ALERT', {'alert': 'Owned'+suffix, 'volume_uid': 'volume', 'active': active, 'observed_at': i})
    emit('LONGHORN_V2_SUSTAINED_ACKNOWLEDGEMENTS', {'token': token, 'rows': rows})
    emit('LONGHORN_V2_SUSTAINED_RESULT', {k: proof[k] for k in ('summary', 'timeline', 'errors', 'retries', 'max_latency_s')})
    emit('LONGHORN_V2_SUSTAINED_REMOUNT', proof['summary'])
    emit('LONGHORN_V2_SUSTAINED_FAULT', {'engine_uid': 'engine', 'replica_uid': 'replica', 'before_acks': 4,
         'after_acks': 5, 'native_degraded_and_restored': True})
    # The fixture needs post-fault acknowledgements, so use before=4 and a
    # fifth real hash in the ledger rather than pretending four prove five.
    faults = GATE.load('longhorn-faults.py')
    fifth = {'type': 'ack', 'sequence': 4, 'sha256': hashlib.sha256(faults.ledger_block(token, 4)).hexdigest(),
             'observed_elapsed_s': 1799.5}
    rows.insert(-1, fifth)
    complete = GATE.load('longhorn-control-faults.py').confirmed(
        [{k: v for k, v in r.items() if k != 'observed_elapsed_s'} for r in rows if r['type'] == 'ack'], token, faults)
    rows[-1].update(complete['summary'])
    proof = GATE.confirmed_load(rows, token)
    for i, line in enumerate(result):
        if line.startswith('LONGHORN_V2_SUSTAINED_ACKNOWLEDGEMENTS '):
            result[i] = 'LONGHORN_V2_SUSTAINED_ACKNOWLEDGEMENTS '+json.dumps({'token': token, 'rows': rows})
        if line.startswith('LONGHORN_V2_SUSTAINED_RESULT '):
            result[i] = 'LONGHORN_V2_SUSTAINED_RESULT '+json.dumps({k: proof[k] for k in ('summary', 'timeline', 'errors', 'retries', 'max_latency_s')})
        if line.startswith('LONGHORN_V2_SUSTAINED_REMOUNT '):
            result[i] = 'LONGHORN_V2_SUSTAINED_REMOUNT '+json.dumps(proof['summary'])
    emit('LONGHORN_V2_BOUNDED_CAPACITY', {'filler_bytes': GATE.FILLER_BYTES, 'filler_sha256': 'a'*64,
         'before': {n: 0 for n in 'abc'}, 'peak': {n: GATE.FILLER_BYTES for n in 'abc'}, 'after': {n: 1 for n in 'abc'}})
    emit('LONGHORN_V2_BACKUP_ALERT_RECOVERED', {'volume_uid': 'volume', 'backup_uid': 'backup', 'object_receipt_sha256': 'b'*64})
    emit('LONGHORN_PERFORMANCE_RESULT', {'errors': 0, 'verified_blocks': 16384, 'dataset_bytes': 64*1024**2,
         'write_fsync_per_operation': True, 'buffered_io': True, 'concurrency': 1,
         'results': [{'mode': mode, 'operations': 256 if mode.startswith('sequential') else 4096,
                      'latency_ms': {str(p): 1 for p in (50, 95, 99)}}
                     for mode in ('sequential-write', 'random-write', 'sequential-read', 'random-read')]})
    emit('LONGHORN_PERFORMANCE_RESOURCE_PROFILE', {'before': {'pod': {'uid': 'pod'}}, 'after': {'pod': {'uid': 'pod'}}})
    native = [{'method': 'iobuf_set_options', 'params': {'small_bufsize': 8192, 'large_bufsize': 132*1024,
               'large_pool_count': 1024, 'small_pool_count': 8192}}]
    runtime = {'pid': 1, 'start_time': '1', 'argv': ['spdk_tgt'], 'memory_events': {}}
    deltas = {'module': {pool: {k: 0 for k in ('cache', 'main', 'retry')} for pool in ('small_pool', 'large_pool')}}
    for size in (4096, 128*1024):
        for depth in (1, 8, 32):
            emit('LONGHORN_V2_IOBUF_WORKLOAD', {'workload': {'size_bytes': size, 'requested_depth': depth,
                 'errors': 0, 'operations': 128, 'verified_operations': 128, 'fsync_per_write': True,
                 'measured_peak_inflight': 1, 'latency_ms': {str(p): 1 for p in (50, 95, 99)}},
                 'allocation': {n: native for n in 'abc'}, 'resource_before': {n: runtime for n in 'abc'},
                 'resource_after': {n: runtime for n in 'abc'}, 'iobuf_counter_deltas': {n: deltas for n in 'abc'},
                 'exhaustion_retry_observed': False, 'tuning_benefit_claimed': False})
    emit('LONGHORN_V2_BOUNDED_ATTACH_QUEUE', {'max_concurrent': 4, 'mount_generations': 2,
         'volumes': {str(i): {'uid': str(i)} for i in range(4)}, 'confirmed_sha256': {str(i): 'c'*64 for i in range(4)},
         'samples': [{'stage': stage, 'backlog': 0} for stage in ('attach-0', 'detach-0', 'attach-1', 'detach-1')]})
    emit('LONGHORN_V2_SUPPORT_BUNDLE_BOUNDARY', {'compressed_bytes': 100, 'expanded_bytes': 1000, 'members': 1,
         'crc_verified': True, 'extracted': False, 'published': False, 'source_uid': 'bundle', 'sha256': 'd'*64})
    return '\n'.join(result+['LONGHORN_BUSINESS_FIXTURES_CLEAN_PASS', 'LONGHORN_V2_OBSERVABILITY_BUSINESS_PASS'])


class ObservabilityContracts(unittest.TestCase):
    def test_actual_pod_budget_covers_pressure_ledger_cache_and_benchmark_without_changing_other_clients(self):
        native=GATE.load('longhorn-lab.py')
        program=ast.parse(GATE.sustained_program('unit:observability-30min'))
        call=next(n for n in ast.walk(program) if isinstance(n,ast.Call) and isinstance(n.func,ast.Name) and n.func.id=='write_ledger')
        interval=next(ast.literal_eval(k.value) for k in call.keywords if k.arg=='interval')
        ledger_bytes=int(GATE.LOAD_SECONDS/interval+1)*65536
        minimum=GATE.FILLER_BYTES+ledger_bytes+64*1024**2+256*1024**2
        for name in ('writer','remount-reader','concurrent-0-client-0'):
            expected=native.client_pod('owned',name,'data','longhorn-node-146',block=True)
            pod=GATE.client_pod(native.client_pod,'owned',name,'data','longhorn-node-146',block=True)
            if name.startswith('concurrent'):
                self.assertEqual(expected,pod);continue
            resources=pod['spec']['containers'][0]['resources']
            quantity=GATE.load('longhorn-v2-monitoring.py').quantity
            self.assertGreaterEqual(quantity(resources['limits']['memory']),minimum)
            self.assertEqual(resources['limits']['memory'],resources['requests']['memory'])
            for kind in ('requests','limits'):expected['spec']['containers'][0]['resources'][kind]['memory']=resources[kind]['memory']
            self.assertEqual(expected,pod)
        self.assertEqual('256Mi',native.client_pod('owned','writer','data','longhorn-node-146')['spec']['containers'][0]['resources']['limits']['memory'])

    def test_business_installs_the_budget_before_pod_creation_and_restores_factory_after_failure(self):
        factory=GATE.load('longhorn-lab.py').client_pod
        store=SimpleNamespace(prepare=Mock(),qualify=Mock(),cleanup=Mock(),product_configuration=lambda:{})
        @contextlib.contextmanager
        def scoped_cleanup(clean):
            try:yield
            finally:clean()
        lab=SimpleNamespace(client_pod=factory,verify_performance=Mock(),cleanup_business=Mock(),kubectl=Mock(),
            backup_store=lambda:store,scoped_cleanup=scoped_cleanup)
        def business(*args,**kwargs):
            self.assertEqual(('performance',),args);self.assertEqual('longhorn-delete',kwargs['storage_class'])
            pod=lab.client_pod('owned','writer','data','longhorn-node-146')
            self.assertEqual('1536Mi',pod['spec']['containers'][0]['resources']['limits']['memory'])
            raise ValueError('owning scene failure')
        lab.business=business;originals={k:getattr(lab,k) for k in ('client_pod','verify_performance','cleanup_business','kubectl')}
        monitoring=SimpleNamespace(verify=Mock());probe=SimpleNamespace(process=None)
        with patch.object(GATE,'load',return_value=monitoring),patch.object(GATE,'Probe',return_value=probe):
            with self.assertRaisesRegex(ValueError,'owning scene failure'):GATE.business(lab,1)
        for name,original in originals.items():self.assertIs(original,getattr(lab,name))
        store.cleanup.assert_called_once()

    def initial_sample_fixture(self, payloads, *, changed_identity=False):
        monitoring = GATE.load('longhorn-v2-monitoring.py')
        real_lab = GATE.load('longhorn-lab.py')
        probe = object.__new__(GATE.Probe)
        clock = [0.0]
        actual_queries = []
        volume = {'metadata': {'uid': 'volume-uid'}, 'spec': {'dataEngine': 'v2'}}
        def get(*args):
            result = copy.deepcopy(volume)
            if changed_identity and actual_queries:
                result['metadata']['uid'] = 'replacement'
            return result
        lab = SimpleNamespace(DIRECTORY=Path('/unused'), owner=lambda: {'run_id': 'owned-run'},
            get=Mock(side_effect=get), wait=real_lab.wait, write_private=Mock())
        probe.lab = lab
        probe.journal = {'namespace': 'business', 'volume': 'owned-volume', 'volume_uid': 'volume-uid', GATE.KEY: {}}
        probe.create = Mock(side_effect=lambda obj: dict(copy.deepcopy(obj),
            metadata=dict(obj['metadata'], uid='created-uid')))
        probe.alert = Mock(); probe.emit = Mock(); probe.start = Mock()
        prom = object.__new__(monitoring.Prometheus)
        def api(endpoint, params):
            self.assertEqual('/api/v1/query', endpoint)
            expression = params['query']
            if expression.startswith('longhorn_volume_actual_size_bytes'):
                # No rule or fault may be created before the first actual sample.
                self.assertEqual(0, probe.create.call_count)
                actual_queries.append(expression)
                return copy.deepcopy(payloads[min(len(actual_queries)-1, len(payloads)-1)])
            return self.initial_sample_payload('1')
        prom.api = Mock(side_effect=api)
        probe.prom = prom
        def sleep(seconds): clock[0] += seconds
        def run():
            with patch.object(real_lab.time, 'monotonic', side_effect=lambda: clock[0]), \
                    patch.object(real_lab.time, 'sleep', side_effect=sleep), \
                    patch.object(monitoring.time, 'time', return_value=1000), \
                    patch('sys.stdout', new=io.StringIO()):
                probe.rules()
        return SimpleNamespace(probe=probe, lab=lab, prom=prom, run=run,
            clock=clock, actual_queries=actual_queries)

    @staticmethod
    def initial_sample_payload(value=None, *, stamp=1000):
        rows = [] if value is None else [{'metric': {'volume': 'owned-volume'}, 'value': [stamp, value]}]
        return {'status': 'success', 'data': {'resultType': 'vector', 'result': rows}}

    def test_rules_wait_for_first_actual_sample_and_preserve_real_zero(self):
        for value in ('0', '4096'):
            with self.subTest(value=value):
                fixture = self.initial_sample_fixture([self.initial_sample_payload(),
                    self.initial_sample_payload(), self.initial_sample_payload(value)])
                fixture.run()
                self.assertEqual(3, len(fixture.actual_queries))
                self.assertEqual(3, fixture.lab.get.call_count)
                self.assertEqual(10, fixture.clock[0])
                self.assertEqual(['longhorn_volume_actual_size_bytes{volume="owned-volume"}']*3,
                    fixture.actual_queries)
                self.assertEqual(float(value)+GATE.FILLER_BYTES//2,
                    fixture.probe.journal[GATE.KEY]['space_threshold_bytes'])
                self.assertEqual(['ServiceMonitor', 'PrometheusRule'],
                    [call.args[0]['kind'] for call in fixture.probe.create.call_args_list])
                fixture.probe.start.assert_not_called()

    def test_rules_missing_first_actual_sample_has_bounded_timeout(self):
        fixture = self.initial_sample_fixture([self.initial_sample_payload()])
        with self.assertRaisesRegex(TimeoutError, 'owned-volume-actual-size-scraped'):
            fixture.run()
        self.assertEqual(180, fixture.clock[0])
        self.assertEqual(37, len(fixture.actual_queries))
        fixture.probe.create.assert_not_called()
        fixture.lab.write_private.assert_not_called()
        fixture.probe.start.assert_not_called()

    def test_rules_first_actual_sample_invalid_response_is_not_retried(self):
        bad = [self.initial_sample_payload('NaN'), self.initial_sample_payload('Inf'),
            self.initial_sample_payload('1', stamp=909), self.initial_sample_payload('1', stamp=1001),
            {'status': 'error', 'data': {'resultType': 'vector', 'result': []}}]
        for payload in bad:
            with self.subTest(payload=payload):
                fixture = self.initial_sample_fixture([payload, self.initial_sample_payload('1')])
                with self.assertRaises(ValueError): fixture.run()
                self.assertEqual(1, len(fixture.actual_queries))
                self.assertEqual(0, fixture.clock[0])
                fixture.probe.create.assert_not_called()
                fixture.probe.start.assert_not_called()

    def test_rules_wait_rejects_changed_volume_before_next_query(self):
        fixture = self.initial_sample_fixture([self.initial_sample_payload(), self.initial_sample_payload('1')],
            changed_identity=True)
        with self.assertRaisesRegex(ValueError, 'volume identity changed'): fixture.run()
        self.assertEqual(1, len(fixture.actual_queries))
        self.assertEqual(2, fixture.lab.get.call_count)
        fixture.probe.create.assert_not_called()
        fixture.probe.start.assert_not_called()

    def test_metric_default_query_still_rejects_missing_samples(self):
        fixture = self.initial_sample_fixture([self.initial_sample_payload()])
        with self.assertRaisesRegex(ValueError, 'absence is not zero'):
            fixture.probe.metric('longhorn_volume_actual_size_bytes')
        self.assertEqual(1, len(fixture.actual_queries))
        fixture.probe.create.assert_not_called()

    def test_short_generated_ledger_executes_real_fsync_and_independent_readback(self):
        with tempfile.TemporaryDirectory() as directory:
            code = GATE.sustained_program('unit', 1.05).replace('"/data/fault-ledger"', repr(str(Path(directory)/'ledger')))
            ast.parse(code)
            result = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True, check=True, timeout=10)
            rows = [json.loads(line) for line in result.stdout.splitlines()]
            proof = GATE.confirmed_load(rows, 'unit', minimum_seconds=1)
            self.assertGreaterEqual(proof['summary']['confirmed'], 4)
            self.assertEqual(proof['summary'], GATE.load('longhorn-faults.py').verify_ledger(
                str(Path(directory)/'ledger'), 'unit', proof['records']))

    def test_duration_hash_timeline_and_normal_completion_are_required(self):
        token, rows = ledger_rows()
        GATE.confirmed_load(rows, token)
        for mutation in ('duration', 'hash', 'order', 'nan', 'complete', 'pid', 'retry'):
            row = copy.deepcopy(rows)
            if mutation == 'duration': row[-1]['observed_elapsed_s'] = 1799
            if mutation == 'hash': row[1]['sha256'] = '0'*64
            if mutation == 'order': row[2]['observed_elapsed_s'] = -1
            if mutation == 'nan': row[-1]['observed_elapsed_s'] = float('nan')
            if mutation == 'complete': row.pop()
            if mutation == 'pid': row[0]['pid'] = 0
            if mutation == 'retry': row[-1]['errors'] = {'EIO': 1}; row[-1]['retries'] = 1
            with self.subTest(mutation=mutation), self.assertRaises(ValueError): GATE.confirmed_load(row, token)

    def test_buffered_queue_fsync_readback_is_bounded_and_inode_cleanup_is_exact(self):
        with tempfile.TemporaryDirectory() as directory:
            result = GATE.queue_workload(directory, 'unit', 4096, 8, operations=16)
            self.assertEqual(16, result['verified_operations'])
            self.assertLessEqual(result['measured_peak_inflight'], 8)
            self.assertEqual([], list(Path(directory).iterdir()))
            path = Path(directory)/'queue-4096-8'; path.write_bytes(b'foreign')
            with self.assertRaises(FileExistsError): GATE.queue_workload(directory, 'unit', 4096, 8)
            self.assertEqual(b'foreign', path.read_bytes())
        for size, depth in ((8192, 1), (4096, 33)):
            with self.assertRaises(ValueError): GATE.queue_workload('/unused', 'unit', size, depth)

    def test_iobuf_native_module_and_unsigned_counter_reset_contract(self):
        first = [{'module': 'bdev', **{p: {'cache': 1, 'main': 2, 'retry': 0} for p in ('small_pool', 'large_pool')}}]
        last = copy.deepcopy(first); last[0]['small_pool']['retry'] = 2
        self.assertEqual(2, GATE.iobuf_delta(first, last)['bdev']['small_pool']['retry'])
        for mutation in ('reset', 'module', 'bool', 'empty'):
            row = copy.deepcopy(last)
            if mutation == 'reset': row[0]['small_pool']['cache'] = 0
            if mutation == 'module': row[0]['module'] = 'other'
            if mutation == 'bool': row[0]['small_pool']['main'] = True
            if mutation == 'empty': row = []
            with self.subTest(mutation=mutation), self.assertRaises(ValueError): GATE.iobuf_delta(first, row)
        ast.parse(GATE.rpc_program())

    def test_support_bundle_zip_crc_and_private_metadata_only_boundary(self):
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, 'w') as archive: archive.writestr('support/log.txt', b'private diagnostic')
        row = GATE.support_archive(stream.getvalue())
        self.assertEqual(1, row['members']); self.assertFalse(row['published']); self.assertFalse(row['extracted'])
        for path in ('../escape', '/absolute', 'bad\\name'):
            stream = io.BytesIO()
            with zipfile.ZipFile(stream, 'w') as archive: archive.writestr(path, b'bad')
            with self.subTest(path=path), self.assertRaises(ValueError): GATE.support_archive(stream.getvalue())
        stream = io.BytesIO()
        info = zipfile.ZipInfo('link'); info.external_attr = (stat.S_IFLNK|0o777) << 16
        with zipfile.ZipFile(stream, 'w') as archive: archive.writestr(info, b'target')
        with self.assertRaises(ValueError): GATE.support_archive(stream.getvalue())

    def test_support_bundle_accepts_measured_size_and_keeps_owner_and_size_refusals(self):
        for size, owner, allowed in ((539961501, 'longhorn-node-146', True),
                                     (1024**3+1, 'longhorn-node-146', False),
                                     (539961501, 'foreign-node', False)):
            with self.subTest(size=size, owner=owner):
                state = {'created': False, 'retired': False}
                obj = {'metadata': {'name': 'support-bundle-owned', 'uid': 'bundle-uid'},
                       'spec': {'description': 'kubeauto-observability-run', 'issueURL': ''},
                       'status': {'state': 'ReadyForDownload', 'ownerID': owner, 'filesize': size}}
                def get(kind, name=None, namespace=None):
                    if name: return None if state['retired'] else obj
                    return {'items': [obj] if state['created'] else []}
                lab = SimpleNamespace(NODES=('192.168.47.146',), DIRECTORY=Path('/unused'),
                    owner=lambda: {'run_id': 'run'}, get=get, write_private=Mock(),
                    kubectl=Mock(side_effect=lambda *a, **k: state.update(created=True)),
                    wait=lambda name, timeout, condition: condition())
                probe = GATE.Probe(lab, 1, None, None, observe=False)
                probe.journal = {GATE.KEY: {}}
                probe.manager = Mock(return_value=({'metadata': {'name': 'manager'}}, 'http://manager'))
                probe.bundle_dependents = Mock(return_value=[])
                def download(*args):
                    state['retired'] = True
                    return {'compressed_bytes': size, 'crc_verified': True}
                with patch.object(GATE, 'download_support', side_effect=download) as transfer:
                    if allowed:
                        probe.support_bundle()
                        self.assertEqual(transfer.call_args.args[2], size)
                        self.assertTrue(probe.journal[GATE.KEY]['support_bundle']['retired_after_download'])
                    else:
                        with self.assertRaisesRegex(ValueError, 'owner or private download size'):
                            probe.support_bundle()
                        transfer.assert_not_called()

    def test_support_download_reserves_headroom_and_refuses_oversized_input_before_transport(self):
        size = 539961501
        with patch.object(GATE.shutil, 'disk_usage', return_value=SimpleNamespace(free=size+1024**3-1)), \
                patch.object(GATE.subprocess, 'Popen') as process:
            with self.assertRaisesRegex(ValueError, 'reserved disk headroom'):
                GATE.download_support(None, '/unused', size)
            process.assert_not_called()
        with patch.object(GATE.shutil, 'disk_usage') as disk, patch.object(GATE.subprocess, 'Popen') as process:
            with self.assertRaisesRegex(ValueError, 'private fixture budget'):
                GATE.download_support(None, '/unused', 1024**3+1)
            disk.assert_not_called(); process.assert_not_called()
        stream = Mock()
        stream.tell.return_value = 1024**3+1
        with self.assertRaisesRegex(ValueError, 'private fixture budget'): GATE.support_archive(stream)
        stream.read.assert_not_called()

    def test_missing_monitor_requires_uid_and_current_spec_before_journaled_patch(self):
        journal = {'namespace': 'business', GATE.KEY: {'metric_probe': 'owned', 'sample_monitor': {
            'name': 'monitor', 'uid': 'uid', 'original_spec': {'selector': {'matchLabels': {'app': 'longhorn-manager'}}},
            'expected_spec': {'selector': {'matchLabels': {'app': 'longhorn-manager'}}}}}}
        obj = {'metadata': {'uid': 'uid', 'resourceVersion': 'rv'}, 'spec': copy.deepcopy(journal[GATE.KEY]['sample_monitor']['original_spec'])}
        order = []
        lab = SimpleNamespace(DIRECTORY=Path('/unused'), get=Mock(return_value=obj),
            write_private=Mock(side_effect=lambda *a: order.append('intent')),
            kubectl=Mock(side_effect=lambda *a, **kw: order.append('patch')))
        probe = object.__new__(GATE.Probe); probe.lab = lab; probe.journal = journal
        probe.sample_monitor(True)
        self.assertEqual(['intent', 'patch'], order)
        patch_rows = json.loads(lab.kubectl.call_args.kwargs['stdin'])
        self.assertEqual('/metadata/uid', patch_rows[0]['path'])
        self.assertEqual('/metadata/resourceVersion', patch_rows[1]['path'])
        obj['metadata']['uid'] = 'replacement'
        with self.assertRaises(ValueError): probe.sample_monitor(False)
        self.assertEqual(1, lab.kubectl.call_count)

    def test_fresh_recovery_fences_remote_writer_without_local_popen_handle(self):
        pod = {'metadata': {'uid': 'pod'}}
        journal = {'namespace': 'business', 'objects': {'Pod/writer': 'pod'}, GATE.KEY: {'run_id': 'run', 'token': 'token'}}
        lab = SimpleNamespace(owner=lambda: {'run_id': 'run'}, get=Mock(return_value=pod), remove=Mock(), wait=Mock())
        probe = object.__new__(GATE.Probe)
        probe.lab = lab; probe.journal = journal; probe.process = None; probe.directory = None
        probe.restore_network = Mock()
        probe.restore()
        lab.remove.assert_called_once_with(pod)
        pod['metadata']['uid'] = 'foreign'
        with self.assertRaises(ValueError): probe.restore()

    def test_transport_mutation_records_exact_intent_before_ambiguous_add_failure(self):
        order = []
        engine = {'metadata': {'name': 'engine', 'uid': 'engine'}, 'spec': {'nodeID': 'longhorn-node-146'},
                  'status': {'instanceManagerName': 'im-146', 'ip': '172.31.1.1',
                    'currentReplicaAddressMap': {'replica-147': 'tcp://172.31.1.2:12345'}}}
        replicas = [{'metadata': {'name': 'replica-'+n, 'uid': n, 'ownerReferences': [{'kind': 'Volume', 'uid': 'volume'}]},
            'spec': {'volumeName': 'source', 'nodeID': 'longhorn-node-'+n, 'dataEngine': 'v2'},
            'status': {'currentState': 'running', 'port': 12345,
                'instanceManagerName': 'im-'+n, 'ip': '172.31.1.'+str(int(n)-145)}} for n in ('147', '148')]
        def get(kind, name=None, namespace=None):
            if kind == 'replicas.longhorn.io': return {'items': replicas}
            if kind == 'events': return {'items': []}
            n = name.split('-')[1]
            if kind == 'instancemanagers.longhorn.io':
                return {'metadata': {'uid': 'parent-'+n}, 'spec': {'nodeID': 'longhorn-node-'+n},
                    'status': {'currentState': 'running'}}
            if kind == 'pod':
                return {'metadata': {'uid': 'pod-'+n, 'ownerReferences': [
                    {'kind': 'InstanceManager', 'uid': 'parent-'+n, 'controller': True}]},
                    'spec': {'nodeName': 'longhorn-node-'+n}, 'status': {'phase': 'Running',
                        'podIP': '172.31.1.'+str(int(n)-145), 'conditions': [{'type': 'Ready', 'status': 'True'}]}}
            self.fail(kind)
        lab = SimpleNamespace(WORKERS=['192.168.47.146', '192.168.47.147', '192.168.47.148'],
            DIRECTORY=Path('/unused'), healthy_replicas=lambda n: True, get=get,
            kubectl=Mock(return_value=SimpleNamespace(stdout='sl local_address rem_address st\n0: 01011FAC:8000 02011FAC:3039 01\n')),
            owner=lambda: {'run_id': 'owned'}, write_private=lambda *a: order.append('intent'))
        def call(lab, address, request):
            order.append(request['operation'])
            self.assertEqual('192.168.47.146', address)
            self.assertEqual('172.31.1.1', request['source'])
            self.assertEqual('172.31.1.2', request['destination'])
            self.assertFalse(request['host_network'])
            self.assertEqual(12345, request['port'])
            if request['operation'] == 'add': raise RuntimeError('ambiguous connection')
            return SimpleNamespace(stdout=json.dumps({'present': [False, False]}))
        fault = GATE.load('longhorn-v2-runtime-faults.py')
        probe = object.__new__(GATE.Probe); probe.lab = lab
        probe.journal = {'volume': 'source', 'volume_uid': 'volume', GATE.KEY: {}}
        probe.owned_consumers = Mock(); probe.engine = lambda: engine; probe.ack_count = lambda: 4
        with patch.object(GATE, 'load', return_value=fault), patch.object(fault, 'host_call', side_effect=call), \
                patch.object(fault, 'probe', return_value={'machine_id': 'machine', 'boot_id': 'boot'}), \
                self.assertRaisesRegex(RuntimeError, 'ambiguous'):
            probe.network_fault()
        self.assertEqual(['probe', 'intent', 'add'], order)
        self.assertTrue(probe.journal[GATE.KEY]['network']['intent'])
        self.assertEqual('147', probe.journal[GATE.KEY]['network']['replica_uid'])
        with patch.object(GATE, 'load', return_value=fault), patch.object(fault, 'host_call', side_effect=call):
            probe.restore_network()
        self.assertTrue(probe.journal[GATE.KEY]['network']['restored'])
        for defect in ('pod', 'destination', 'mode', 'comment'):
            proof = probe.journal[GATE.KEY]['network']; original = copy.deepcopy(proof)
            if defect == 'pod': proof['endpoints']['source']['pod_uid'] = 'replacement'
            if defect == 'destination': proof['destination'] = '192.168.47.149'
            if defect == 'mode': proof['host_network'] = True
            if defect == 'comment': proof['comment'] = 'kubeauto-longhorn-v2-'+'0'*24
            with patch.object(GATE, 'load', return_value=fault), patch.object(fault, 'host_call') as mutation:
                with self.assertRaises(ValueError): probe.restore_network()
                mutation.assert_not_called()
            probe.journal[GATE.KEY]['network'] = original

    def test_native_degraded_accepts_separate_reconciles_but_refuses_unrelated_events(self):
        # Execute the actual nested assertion, including the original Engine
        # identity guard. The transport contract above covers its owning setup.
        tree = ast.parse((ROOT/'tests/helpers/longhorn-v2-observability.py').read_text())
        method = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == 'network_fault')
        assertion = next(n for n in method.body if isinstance(n, ast.FunctionDef) and n.name == 'degraded')
        engine = {'metadata': {'uid': 'engine'}, 'status': {'replicaModeMap': {}}}
        volume = {'status': {'robustness': 'degraded'}}
        event = {'metadata': {'uid': 'new-event'}, 'involvedObject': {'kind': 'Engine', 'uid': 'engine'},
                 'reason': 'Faulted', 'type': 'Warning',
                 'message': 'Detected replica owned-replica (172.31.1.2:12345) in error'}
        events = [event]
        lab = SimpleNamespace(get=Mock(side_effect=lambda *a, **kw: {'items': events}))
        probe = SimpleNamespace(lab=lab, engine=lambda: engine, volume=lambda: volume)
        scope = {'self': probe, 'proof': {'engine_uid': 'engine', 'prior_events': ['old-event']},
                 'name': 'owned-replica', 'parsed': SimpleNamespace(hostname='172.31.1.2', port=12345), 'NS': GATE.NS}
        exec(compile(ast.Module(body=[assertion], type_ignores=[]), '<actual-degraded-assertion>', 'exec'), scope)
        check = scope['degraded']
        self.assertTrue(check())  # ERR has already left the map.
        for key, value in (('uid', 'old-event'), ('engine_uid', 'replacement'), ('kind', 'Volume'),
                           ('reason', 'Rebuilt'), ('type', 'Normal'), ('message', 'other replica/endpoint')):
            with self.subTest(defect=key):
                events = [copy.deepcopy(event)]
                if key == 'uid': events[0]['metadata']['uid'] = value
                elif key in ('engine_uid', 'kind'): events[0]['involvedObject']['uid' if key == 'engine_uid' else key] = value
                else: events[0][key] = value
                self.assertFalse(check())
        events = []; self.assertFalse(check())
        engine['status']['replicaModeMap']['owned-replica'] = 'ERR'
        self.assertTrue(check())
        volume['status']['robustness'] = 'healthy'; self.assertFalse(check())
        engine['metadata']['uid'] = 'replacement'
        with self.assertRaisesRegex(ValueError, 'engine changed'): check()

    def test_completed_threshold_rule_retirement_refuses_replacement(self):
        lab = SimpleNamespace(get=Mock(return_value={'metadata': {'uid': 'replacement'}}), remove=Mock(), wait=Mock())
        probe = object.__new__(GATE.Probe); probe.lab = lab
        probe.journal = {'namespace': 'owned', 'objects': {'PrometheusRule/observability-rules': 'original'}}
        with self.assertRaises(ValueError): probe.retire_rules()
        lab.remove.assert_not_called()

    def test_bundle_garbage_collection_follows_uid_owner_closure_and_keeps_history(self):
        rows = [{'metadata': {'uid': uid, 'ownerReferences': [{'uid': owner}]}}
                for uid, owner in [('deployment', 'bundle'), ('rs', 'deployment'), ('pod', 'rs'), ('foreign', 'other')]]
        lab = SimpleNamespace(DIRECTORY=Path('/unused'), write_private=Mock(),
                              get=lambda kind, **kw: {'items': rows if kind == 'pods' else []})
        probe = object.__new__(GATE.Probe); probe.lab = lab; probe.journal = {}
        proof = {'uid': 'bundle'}
        self.assertEqual({'deployment', 'rs', 'pod'}, {r['metadata']['uid'] for r in probe.bundle_dependents(proof)})
        rows[:] = [r for r in rows if r['metadata']['uid'] in ('pod', 'foreign')]
        self.assertEqual(['pod'], [r['metadata']['uid'] for r in probe.bundle_dependents(proof)])
        rows.clear()
        self.assertEqual([], probe.bundle_dependents(proof))

    def test_cleanup_restores_fault_before_business_delete_and_store_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            order = []
            path = Path(directory)/GATE.RECORD
            path.write_text(json.dumps({'volume_uid': 'volume', GATE.KEY: {'run_id': 'run', 'volume_uid': 'volume'}}))
            store = SimpleNamespace(cleanup=lambda: order.append('store'))
            lab = SimpleNamespace(DIRECTORY=Path(directory), owner=lambda: {'run_id': 'run'},
                check_business_graph=lambda j: order.append('graph'), backup_store=lambda: store,
                cleanup_business=lambda **kw: (order.append('delete'), path.unlink()))
            monitoring = SimpleNamespace(RECORD='monitor.json', verify=Mock())
            with patch.object(GATE, 'Probe', return_value=SimpleNamespace(restore=lambda: order.append('restore'))), \
                    patch.object(GATE, 'load', return_value=monitoring):
                GATE.cleanup(lab, 123)
            self.assertEqual(['graph', 'restore', 'delete', 'store'], order)
            self.assertFalse(monitoring.verify.called)

    def test_foreign_or_symlink_journal_is_not_cleanup_authority(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/GATE.RECORD; path.write_text('{}')
            lab = SimpleNamespace(DIRECTORY=Path(directory), owner=lambda: {'run_id': 'run'}, cleanup_business=Mock())
            with self.assertRaises(ValueError): GATE.cleanup(lab, 1)
            path.unlink(); path.symlink_to(Path(directory)/'missing')
            with self.assertRaises(ValueError): GATE.cleanup(lab, 1)
            lab.cleanup_business.assert_not_called()

    def test_complete_log_has_raw_ledger_four_real_alerts_and_all_bounded_observations(self):
        GATE.verify_log(full_log())

    def test_marker_only_partial_missing_samples_or_false_exhaustion_never_pass(self):
        log = full_log()
        for mutation in ('partial', 'alert', 'remount', 'allocation', 'retry', 'failure', 'bundle'):
            lines = log.splitlines()
            if mutation == 'partial': lines = ['LONGHORN_V2_OBSERVABILITY_BUSINESS_PASS']
            if mutation == 'alert': lines = [l for l in lines if not l.startswith('LONGHORN_V2_REAL_ALERT ') or 'OwnedMissing' not in l]
            if mutation == 'remount': lines = [l for l in lines if not l.startswith('LONGHORN_V2_SUSTAINED_REMOUNT ')]
            if mutation == 'allocation': lines = [l.replace('"large_pool_count": 1024', '"large_pool_count": 0') for l in lines]
            if mutation == 'retry': lines = [l.replace('"exhaustion_retry_observed": false', '"exhaustion_retry_observed": true') for l in lines]
            if mutation == 'failure': lines.append('LONGHORN_FIRST_FAILURE phase=unit')
            if mutation == 'bundle': lines = [l.replace('"published": false', '"published": true') for l in lines]
            with self.subTest(mutation=mutation), self.assertRaises(ValueError): GATE.verify_log('\n'.join(lines))

    def test_monitoring_continuation_requires_cpu_proof_and_owns_every_cleanup_exit(self):
        fake_python = r"""#!/usr/bin/env bash
set -eu
helper="${1##*/}"; action="$2"
printf '%s %s\n' "$helper" "$action" >> "$TRACE"
case "$helper $action" in
  'longhorn-v2-install.py relay-maintain') trap 'exit 0' TERM; while :; do sleep 0.05; done;;
  'longhorn-v2-cpu-isolation.py business') exit 99;;
  'longhorn-v2-cpu-isolation.py verify-completed')
    [[ "$CASE" != proof-fail ]] || exit 7
    if [[ "$CASE" == proof-marker-missing ]]; then
      "$REAL_PY" "$CPU_REUSE_FIXTURE" > /dev/null
    else
      "$REAL_PY" "$CPU_REUSE_FIXTURE"
    fi;;
  'longhorn-v2-monitoring.py setup') [[ "$CASE" != monitoring-fail ]] || exit 8; echo LONGHORN_V2_MONITORING_SETUP_PASS;;
  'longhorn-v2-monitoring.py cleanup')
    if [[ -e "$RECEIPT" || -L "$RECEIPT" ]]; then
      [[ ! -L "$RECEIPT" && "$CASE" != prior-cleanup-fail ]] || exit 17
      [[ "$CASE" != prior-cleanup-marker-missing ]] || exit 0
      if [[ "$CASE" != prior-journal-retained ]]; then rm -- "$RECEIPT"; fi
      printf 'original config\n' > "${RECEIPT%/*}/config.yml"
      if [[ "$CASE" == prior-foreign-config ]]; then printf 'foreign config\n' > "${RECEIPT%/*}/config.yml"; fi
    fi
    echo LONGHORN_V2_MONITORING_SCOPED_CLEAN_PASS;;
  'longhorn-v2-observability.py business') [[ "$CASE" != business-fail ]] || exit 9; echo LONGHORN_V2_OBSERVABILITY_BUSINESS_PASS;;
esac
"""
        prior_cases = ('prior-success', 'prior-symlink', 'prior-dangling-symlink', 'prior-cleanup-fail',
                       'prior-cleanup-marker-missing', 'prior-cleanup-exit-missing', 'prior-cleanup-finalized-missing',
                       'prior-proof-fail', 'prior-proof-marker-missing', 'prior-foreign-config', 'prior-journal-retained')
        for case in ('success', 'proof-fail', 'proof-marker-missing', 'monitoring-fail', 'business-fail') + prior_cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as d:
                base = Path(d); helpers = base/'tests/helpers'; helpers.mkdir(parents=True)
                binary = base/'.venv/bin/python'; binary.parent.mkdir(parents=True)
                binary.write_text(fake_python); binary.chmod(0o700)
                durable = (ROOT/'tests/helpers/run-durable-gate.sh').read_text()
                # Corrupt only this stage's completed durable fixture to prove
                # rc=0 and a marker alone cannot unlock the new product setup.
                durable = durable.replace('exit "$rc"', '''if [[ "$STATE_PREFIX" == */prior-monitoring-cleanup ]]; then
  if [[ "$CASE" == prior-cleanup-exit-missing ]]; then rm -- "${STATE_PREFIX}.exit"; fi
  if [[ "$CASE" == prior-cleanup-finalized-missing ]]; then rm -- "${STATE_PREFIX}.finalized"; fi
fi
exit "$rc"''')
                (helpers/'run-durable-gate.sh').write_text(durable)
                source = (ROOT/'tests/helpers/longhorn-v2-observability.sh').read_text().replace('/usr/local/kubeauto', str(base))
                source = source.replace('/var/tmp/kubeauto-longhorn-v2-observability.', str(base/'durable.'))
                wrapper = helpers/'wrapper.sh'; wrapper.write_text(source)
                fakebin = base/'kube-bin'; fakebin.mkdir(); command = fakebin/'kubectl'
                command.write_text('#!/usr/bin/env bash\nexit 0\n'); command.chmod(0o700)
                receipt = base/'clusters/longhorn-v2-gate/longhorn-v2-monitoring.json'
                receipt.parent.mkdir(parents=True)
                (receipt.parent/'config.yml').write_text('monitoring config\n' if case in prior_cases else 'original config\n')
                if case in prior_cases:
                    if case in ('prior-symlink', 'prior-dangling-symlink'):
                        target = base/'foreign-receipt'
                        if case == 'prior-symlink': target.write_text('foreign')
                        receipt.symlink_to(target)
                    else: receipt.write_text('original-owned-receipt')
                fixture_case = case[len('prior-'):] if case.startswith('prior-proof-') else case
                # Reuse the existing CPU fixture and the real, unchanged
                # verify_completed function, including its exact config hash
                # and active-journal refusals. Host probes remain mocked.
                cpu_fixture = base/'cpu-reuse.py'
                cpu_fixture.write_text('''import os,sys
from pathlib import Path
sys.path.insert(0, os.environ['CONTRACT_ROOT'])
from tests.unit.test_longhorn_v2_cpu_isolation import ReusePolicy
fixture = ReusePolicy('test_bound_original_scope_and_current_installation_are_reused_without_business')
try:
    fixture.setUp()
    directory = Path(os.environ['RECEIPT']).parent
    fixture.completed.rename(directory/fixture.completed.name)
    fixture.lab.DIRECTORY = directory
    fixture.verify()
finally:
    fixture.doCleanups()
''')
                trace = base/'trace'; env = dict(os.environ, LONGHORN_PARENT_LEASE_HELD='yes', TRACE=str(trace),
                                               CASE=fixture_case, RECEIPT=str(receipt), REAL_PY=sys.executable,
                                               CPU_REUSE_FIXTURE=str(cpu_fixture), CONTRACT_ROOT=str(ROOT))
                result = subprocess.run(['bash', str(wrapper), 'v2-observability-monitoring'], env=env,
                                        capture_output=True, text=True, timeout=15)
                actions = trace.read_text().splitlines() if trace.exists() else []
                self.assertNotIn('longhorn-v2-cpu-isolation.py business', actions)
                self.assertNotIn('longhorn-v2-cpu-isolation.py clean', actions)
                cleanup_refusal = case in prior_cases and case not in ('prior-success', 'prior-proof-fail',
                    'prior-proof-marker-missing', 'prior-foreign-config', 'prior-journal-retained')
                if cleanup_refusal:
                    self.assertNotIn('longhorn-v2-cpu-isolation.py verify-completed', actions)
                else:
                    self.assertIn('longhorn-v2-cpu-isolation.py verify-completed', actions)
                work = next(base.glob('durable.*'))
                self.assertEqual('0', (work/'finalized').read_text().strip())
                self.assertEqual(str(result.returncode), (work/'exit').read_text().strip())
                if case in ('success', 'prior-success'):
                    self.assertEqual(0, result.returncode, result.stdout+result.stderr)
                    self.assertEqual('0', (work/'cpu-evidence-reuse.exit').read_text().strip())
                    self.assertEqual('0', (work/'cpu-evidence-reuse.finalized').read_text().strip())
                    self.assertIn('LONGHORN_V2_OBSERVABILITY_MONITORING_PASS', result.stdout)
                    self.assertIn('LONGHORN_V2_OBSERVABILITY_MONITORING_SCOPED_CLEAN_PASS', result.stdout)
                    self.assertNotIn('LONGHORN_V2_OBSERVABILITY_PERFORMANCE_PASS', result.stdout)
                    self.assertLess(actions.index('longhorn-v2-cpu-isolation.py verify-completed'), actions.index('longhorn-v2-monitoring.py setup'))
                    if case == 'prior-success':
                        self.assertLess(actions.index('longhorn-v2-monitoring.py cleanup'), actions.index('longhorn-v2-cpu-isolation.py verify-completed'))
                        self.assertLess(actions.index('longhorn-v2-monitoring.py cleanup'), actions.index('longhorn-v2-install.py volume-baseline'))
                        self.assertLess(actions.index('longhorn-v2-install.py volume-baseline'), actions.index('longhorn-v2-monitoring.py setup'))
                        self.assertEqual(2, actions.count('longhorn-v2-monitoring.py cleanup'))
                        self.assertFalse(receipt.exists())
                        for suffix in ('exit', 'finalized'):
                            self.assertEqual('0', (work/('prior-monitoring-cleanup.'+suffix)).read_text().strip())
                    else:
                        self.assertFalse((work/'prior-monitoring-cleanup.exit').exists())
                else:
                    self.assertNotEqual(0, result.returncode)
                    self.assertNotIn('LONGHORN_V2_OBSERVABILITY_MONITORING_PASS', result.stdout)
                if case in ('proof-fail', 'proof-marker-missing') or (case in prior_cases and case != 'prior-success'):
                    self.assertNotIn('longhorn-v2-monitoring.py setup', actions)
                    self.assertNotIn('longhorn-v2-observability.py business', actions)
                    self.assertNotIn('longhorn-v2-install.py volume-baseline', actions)
                else:
                    self.assertIn('longhorn-v2-monitoring.py cleanup', actions)
                if case == 'business-fail':
                    self.assertLess(actions.index('longhorn-v2-observability.py cleanup'), actions.index('longhorn-v2-monitoring.py cleanup'))
                if case.startswith('prior-proof-'):
                    self.assertEqual(1, actions.count('longhorn-v2-monitoring.py cleanup'))
                    self.assertFalse(receipt.exists())
                elif case in prior_cases and case != 'prior-success':
                    self.assertEqual(1, actions.count('longhorn-v2-monitoring.py cleanup'))
                    if case in ('prior-cleanup-fail', 'prior-symlink', 'prior-dangling-symlink'):
                        self.assertEqual(17, result.returncode)
                    if case in ('prior-cleanup-fail', 'prior-cleanup-marker-missing'):
                        self.assertEqual('original-owned-receipt', receipt.read_text())
                    if case in ('prior-symlink', 'prior-dangling-symlink'):
                        self.assertTrue(receipt.is_symlink())
                        if case == 'prior-symlink': self.assertEqual('foreign', target.read_text())
                    if case == 'prior-foreign-config':
                        self.assertIn('CPU reuse product configuration differs', result.stdout+result.stderr)
                    if case == 'prior-journal-retained':
                        self.assertIn('CPU reuse refuses active fixture residue', result.stdout+result.stderr)

    def test_full_wrapper_has_one_relay_cpu_before_monitor_and_dependent_cleanup_order(self):
        path = ROOT/'tests/helpers/longhorn-v2-observability.sh'
        subprocess.run(['bash', '-n', str(path)], check=True)
        source = path.read_text()
        self.assertEqual(1, source.count('"$INSTALL" relay-maintain &'))
        self.assertLess(source.index('stage cpu-isolation'), source.index('stage monitoring-product-setup'))
        self.assertLess(source.index('"$DRIVER" cleanup'), source.index('"$MONITOR" cleanup'))
        self.assertIn('"$cleanup_rc" == 0 && "$mode" != v2-observability-business', source)
        self.assertIn('LONGHORN_V2_OBSERVABILITY_PERFORMANCE_PASS', source)
        self.assertIn('"$DRIVER" verify --log', source)


if __name__ == '__main__': unittest.main()
