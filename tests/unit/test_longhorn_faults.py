"""Real ledger I/O and identity/refusal contracts for scoped replica faults."""
import copy
from contextlib import redirect_stdout
import errno
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import select
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('longhorn_faults', ROOT / 'tests/helpers/longhorn-faults.py')
faults = importlib.util.module_from_spec(spec); spec.loader.exec_module(faults)


class LonghornFaultContracts(unittest.TestCase):
    def records(self, count, token='contract'):
        return [{'type': 'ack', 'sequence': n, 'sha256': hashlib.sha256(faults.ledger_block(token, n)).hexdigest()}
                for n in range(count)]

    def test_exported_client_stops_on_explicit_input_and_rejects_eof_or_invalid_input(self):
        for command in ('stop\n', '', 'continue\n'):
            with self.subTest(command=command), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / 'ledger'
                code = faults.ledger_program('contract', duration=5).replace('/data/fault-ledger', str(path))
                process = subprocess.Popen([sys.executable, '-u', '-c', code], stdin=subprocess.PIPE,
                                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                try:
                    prefix = ''
                    if command == 'stop\n':
                        self.assertTrue(select.select([process.stdout], [], [], 5)[0])
                        prefix = process.stdout.readline()
                        self.assertEqual(json.loads(prefix)['type'], 'ack')
                    stdout, _stderr = process.communicate(command, timeout=5)
                    rows = [json.loads(line) for line in (prefix + stdout).splitlines()]
                    complete = [row for row in rows if row['type'] == 'complete']
                    self.assertEqual(process.returncode == 0, command == 'stop\n')
                    self.assertEqual(len(complete), 1 if command == 'stop\n' else 0)
                    if complete:
                        records = [row for row in rows if row['type'] == 'ack']
                        self.assertEqual(complete[0]['confirmed'], len(records))
                        self.assertEqual(faults.verify_ledger(path, 'contract', records)['sha256'], complete[0]['sha256'])
                finally:
                    if process.poll() is None: process.kill(); process.wait(timeout=5)
                    for stream in (process.stdin, process.stdout, process.stderr): stream.close()

    def test_every_fsynced_slot_is_verified_and_corruption_or_wrong_ledger_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'ledger'
            clock = [0]
            with patch.object(faults.time, 'monotonic', side_effect=lambda: clock[0]), \
                    patch.object(faults.time, 'sleep', side_effect=lambda seconds: clock.__setitem__(0, clock[0] + seconds)), \
                    redirect_stdout(io.StringIO()) as output:
                result = faults.write_ledger(str(path), 'contract', duration=5, interval=1)
            rows = [json.loads(line) for line in output.getvalue().splitlines()]
            records = [r for r in rows if r['type'] == 'ack']
            self.assertGreaterEqual(len(records), 2)
            self.assertEqual(result['confirmed'], len(records))
            self.assertEqual(result['verified_bytes'], len(records) * 65536)
            self.assertEqual(result['retries'], 0)
            self.assertEqual(rows[-1], result)
            self.assertEqual(faults.verify_ledger(path, 'contract', records)['sha256'], result['sha256'])
            for changed in (list(reversed(records)), records + records[:1]):
                with self.assertRaises(ValueError): faults.verify_ledger(path, 'contract', changed)
            with self.assertRaises(ValueError): faults.verify_ledger(path, 'different-volume', records)
            with path.open('r+b') as stream:
                stream.seek(65536); stream.write(b'corrupt')
            with self.assertRaisesRegex(ValueError, 'changed'): faults.verify_ledger(path, 'contract', records)

    def test_retry_never_acknowledges_failed_fsync_and_does_not_skip_a_slot(self):
        for failure in ('write', 'fsync'):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / 'ledger'
                method = 'pwrite' if failure == 'write' else 'fsync'
                original = getattr(faults.os, method); calls = 0
                def interrupted(*args):
                    nonlocal calls
                    calls += 1
                    # fsync call one persists the directory, call two is data.
                    if calls == (1 if failure == 'write' else 2):
                        raise OSError(errno.EIO, 'injected I/O fault')
                    return original(*args)
                clock = [0]
                with patch.object(faults.os, method, side_effect=interrupted), \
                        patch.object(faults.time, 'monotonic', side_effect=lambda: clock[0]), \
                        patch.object(faults.time, 'sleep', side_effect=lambda seconds: clock.__setitem__(0, clock[0] + seconds)), \
                        redirect_stdout(io.StringIO()) as output:
                    result = faults.write_ledger(str(path), 'contract', duration=5, interval=1)
                rows = [json.loads(line) for line in output.getvalue().splitlines()]
                self.assertEqual(rows[0], {'type': 'retry', 'sequence': 0, 'errno': 'EIO'})
                records = [r for r in rows if r['type'] == 'ack']
                self.assertGreaterEqual(len(records), 2)
                self.assertEqual(records, self.records(len(records)))
                self.assertEqual(result['errors'], {'EIO': 1})
                self.assertEqual(result['retries'], 1)

    def test_foreign_file_symlink_short_write_and_unexpected_os_error_stop(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'ledger'; path.write_bytes(b'foreign')
            with self.assertRaises(FileExistsError): faults.write_ledger(str(path), 'contract', duration=0.01)
            link = Path(directory) / 'link'; link.symlink_to(path)
            with self.assertRaises(OSError): faults.write_ledger(str(link), 'contract', duration=0.01)
            with self.assertRaises(OSError): faults.verify_ledger(link, 'contract', [])
            self.assertEqual(path.read_bytes(), b'foreign')
            for error in (0, OSError(errno.ENOSPC, 'capacity prerequisite failed')):
                target = Path(directory) / ('short' if error == 0 else 'full')
                with patch.object(faults.os, 'pwrite', side_effect=error if isinstance(error, OSError) else None,
                                  return_value=0), redirect_stdout(io.StringIO()) as output:
                    with self.assertRaises((ValueError, OSError)):
                        faults.write_ledger(str(target), 'contract', duration=0.01)
                self.assertNotIn('"type": "ack"', output.getvalue())

    def fixture(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        base = Path(directory.name)
        catalog = base/'roles/longhorn/files/images.json'
        catalog.parent.mkdir(parents=True)
        catalog.write_text(json.dumps({'1.12.1': {'longhorn-instance-manager': 'v1.12.1'}}))
        journal = {'volume': 'volume', 'volume_uid': 'volume-uid', 'namespace': 'owned-business'}
        replica = {'metadata': {'name': 'volume-r-1', 'uid': 'replica-uid', 'ownerReferences': [
                       {'kind': 'Volume', 'name': 'volume', 'uid': 'volume-uid'}]},
                   'spec': {'volumeName': 'volume', 'nodeID': 'longhorn-node-165', 'diskPath': '/owned-data',
                            'dataDirectoryName': 'volume-data', 'failedAt': ''},
                   'status': {'currentState': 'running', 'instanceManagerName': 'instance-manager-owned'}}
        manager = {'metadata': {'uid': 'manager-uid'},
                   'spec': {'nodeID': 'longhorn-node-165', 'dataEngine': 'v1'}, 'status': {'currentState': 'running'}}
        pod = {'metadata': {'uid': 'pod-uid', 'ownerReferences': [
                    {'kind': 'InstanceManager', 'uid': 'manager-uid', 'controller': True}]},
               'spec': {'nodeName': 'longhorn-node-165', 'containers': [
                    {'name': 'instance-manager', 'image': 'fixed-registry/longhorn-instance-manager:v1.12.1'}],
                    'volumes': [{'secret': {'secretName': 'longhorn-grpc-tls'}}]},
               'status': {'conditions': [{'type': 'Ready', 'status': 'True'}]}}
        objects = {'volumes.longhorn.io': {'metadata': {'uid': 'volume-uid'}}, 'replicas.longhorn.io': copy.deepcopy(replica),
                   'instancemanagers.longhorn.io': manager, 'pod': pod}
        native = {'spec': {'name': 'volume-r-1', 'binary': '/engine/longhorn',
                           'args': ['replica', '/host/owned-data/replicas/volume-data']},
                  'status': {'state': 'running', 'uuid': 'fa6c8981-5a25-4d2e-a060-5e0667af24c7'}}
        lab = SimpleNamespace(BASE=base, DATA='/owned-data', WORKERS=('192.168.122.212', '192.168.122.165', '192.168.122.238'),
                              get=Mock(side_effect=lambda kind, *_args, **_kwargs: objects[kind]),
                              kubectl=Mock(side_effect=lambda *_args, **_kwargs: SimpleNamespace(stdout=json.dumps(native))))
        return lab, journal, replica, objects, native

    def test_replica_process_requires_current_catalog_pin_and_refuses_retired_tag(self):
        lab, journal, replica, objects, _native = self.fixture()
        (lab.BASE/'roles/longhorn/files/images.json').write_text(json.dumps(
            {'1.12.1': {'longhorn-instance-manager': 'v1.12.1-kubeauto.1'}}))
        container = objects['pod']['spec']['containers'][0]
        container['image'] += '-kubeauto.1'
        faults.replica_process(lab, journal, replica)
        container['image'] = 'registry/longhorn-instance-manager:v1.12.1'
        with self.assertRaisesRegex(ValueError, 'pinned'): faults.replica_process(lab, journal, replica)

    def test_native_replica_path_uses_the_fixed_manager_container_host_prefix(self):
        # Manager 1.12.1 getBinaryAndArgsForReplicaProcessCreation calls
        # types.GetReplicaMountedDataPath, which maps host DATA beneath /host.
        lab, journal, replica, _objects, native = self.fixture()
        self.assertEqual(faults.replica_process(lab, journal, replica)['name'], replica['metadata']['name'])
        for path in ('/owned-data/replicas/volume-data', '/host/foreign-data/replicas/volume-data',
                     '/host/owned-data/replicas/foreign-volume', '/hostile/owned-data/replicas/volume-data'):
            with self.subTest(path=path):
                native['spec']['args'][1] = path
                with self.assertRaisesRegex(ValueError, 'original data identity'):
                    faults.replica_process(lab, journal, replica)
        self.assertTrue(all('delete' not in call.args for call in lab.kubectl.call_args_list))

    def test_process_lookup_refuses_replaced_foreign_unready_or_unpinned_sources(self):
        for fault in ('none', 'volume', 'replica', 'foreign-volume', 'replica-owner', 'node', 'disk', 'directory', 'failed', 'manager',
                      'owner', 'pod-node', 'image', 'tls', 'process-name', 'process-path', 'uuid', 'process-state'):
            with self.subTest(fault=fault):
                lab, journal, replica, objects, native = self.fixture()
                if fault == 'volume': objects['volumes.longhorn.io']['metadata']['uid'] = 'replacement'
                if fault == 'replica': objects['replicas.longhorn.io']['metadata']['uid'] = 'replacement'
                if fault == 'foreign-volume': objects['replicas.longhorn.io']['spec']['volumeName'] = 'foreign'
                if fault == 'replica-owner': objects['replicas.longhorn.io']['metadata']['ownerReferences'][0]['uid'] = 'foreign'
                if fault == 'node': objects['replicas.longhorn.io']['spec']['nodeID'] = 'foreign-node'
                if fault == 'disk': objects['replicas.longhorn.io']['spec']['diskPath'] = '/foreign-data'
                if fault == 'directory': objects['replicas.longhorn.io']['spec']['dataDirectoryName'] = '../foreign'
                if fault == 'failed': objects['replicas.longhorn.io']['spec']['failedAt'] = 'already-failed'
                if fault == 'manager': objects['instancemanagers.longhorn.io']['status']['currentState'] = 'stopped'
                if fault == 'owner': objects['pod']['metadata']['ownerReferences'][0]['uid'] = 'foreign'
                if fault == 'pod-node': objects['pod']['spec']['nodeName'] = 'foreign-node'
                if fault == 'image': objects['pod']['spec']['containers'][0]['image'] = 'image:latest'
                if fault == 'tls': objects['pod']['spec']['volumes'] = []
                if fault == 'process-name': native['spec']['name'] = 'foreign'
                if fault == 'process-path': native['spec']['args'][-1] = '/foreign-data'
                if fault == 'uuid': native['status']['uuid'] = ''
                if fault == 'process-state': native['status']['state'] = 'stopped'
                if fault == 'none':
                    actual = faults.replica_process(lab, journal, replica)
                    self.assertEqual(actual['uuid'], native['status']['uuid'])
                else:
                    with self.assertRaises(ValueError): faults.replica_process(lab, journal, replica)
                for call in lab.kubectl.call_args_list:
                    self.assertNotIn('delete', call.args)

    def test_native_delete_carries_uuid_and_never_retries_a_replacement(self):
        for fault in ('none', 'raced', 'wrong-response', 'not-deleted'):
            with self.subTest(fault=fault):
                lab, journal, replica, _objects, native = self.fixture(); calls = []
                def command(*args, **_kwargs):
                    calls.append(args); row = copy.deepcopy(native)
                    if len(calls) == 2 and fault == 'raced': row['status']['uuid'] = '6763b59f-3891-49c9-9d69-f9dfebba3971'
                    if 'delete' in args:
                        row['deleted'] = fault != 'not-deleted'
                        if fault == 'wrong-response': row['status']['uuid'] = '6763b59f-3891-49c9-9d69-f9dfebba3971'
                    return SimpleNamespace(stdout=json.dumps(row))
                lab.kubectl.side_effect = command
                with redirect_stdout(io.StringIO()):
                    if fault == 'none': faults.stop_replica_process(lab, journal, replica)
                    else:
                        with self.assertRaises(ValueError): faults.stop_replica_process(lab, journal, replica)
                deletes = [args for args in calls if 'delete' in args]
                self.assertEqual(len(deletes), 0 if fault == 'raced' else 1)
                for args in deletes:
                    self.assertEqual(args[-4:], ('--name', replica['metadata']['name'], '--uuid', native['status']['uuid']))
                    self.assertIn('/tls-files/', args)

    def test_remote_ledger_verification_streams_input_and_detects_corruption(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'ledger'; records = self.records(3)
            path.write_bytes(b''.join(faults.ledger_block('contract', n) for n in range(3)))
            proof = {'token': 'contract', 'records': records, 'summary': faults.verify_ledger(path, 'contract', records)}
            def command(*args, **kwargs):
                self.assertIn('-i', args)
                code = args[-1].replace('/data/fault-ledger', str(path))
                return subprocess.run([sys.executable, '-c', code], input=kwargs['stdin'], text=True,
                                      capture_output=True, timeout=5, check=True)
            lab = SimpleNamespace(kubectl=Mock(side_effect=command))
            faults.verify_client_ledger(lab, {'namespace': 'owned'}, 'writer', proof)
            with path.open('r+b') as stream: stream.write(b'corrupt')
            with self.assertRaises(subprocess.CalledProcessError):
                faults.verify_client_ledger(lab, {'namespace': 'owned'}, 'remount-reader', proof)

    def test_fault_orchestration_requires_writes_after_rebuild_and_keeps_first_diagnostics(self):
        for outcome in ('none', 'no-resume', 'survivor-replaced', 'client-failed', 'corrupt-ledger'):
            with self.subTest(outcome=outcome), tempfile.TemporaryDirectory() as directory:
                root = Path(directory); observations = root / 'observations'; observations.mkdir()
                data = root / 'fault-ledger'; token = 'owned-run:replica-failure'
                journal = {'volume': 'volume', 'volume_uid': 'volume-uid', 'namespace': 'owned'}
                before = {name: {'uid': name + '-uid', 'node': 'longhorn-node-' + node}
                          for name, node in (('r165', '165'), ('r212', '212'), ('r238', '238'))}
                replicas = [{'metadata': {'name': name, 'uid': row['uid']}, 'spec': {'nodeID': row['node']}}
                            for name, row in before.items()]
                contract = self
                class Client:
                    returncode = None
                    terminated = False
                    def __init__(self, stream):
                        self.stream = stream; self.records = []; self.stdin = io.StringIO()
                        self.produce(4)
                        if outcome == 'client-failed': self.returncode = 17
                    def produce(self, count):
                        records = contract.records(len(self.records) + count, token)
                        for row in records[len(self.records):]:
                            self.stream.write(json.dumps(row) + '\n')
                        self.stream.flush(); self.records = records
                        data.write_bytes(b''.join(faults.ledger_block(token, n) for n in range(len(records))))
                    def complete(self):
                        contract.assertEqual(self.stdin.getvalue(), 'stop\n')
                        row = dict(faults.verify_ledger(data, token, self.records), type='complete',
                                   errors={}, retries=0, max_latency_s=0.1)
                        self.stream.write(json.dumps(row) + '\n'); self.stream.flush(); self.returncode = 0
                        if outcome == 'corrupt-ledger': data.write_bytes(b'corrupted')
                    def poll(self): return self.returncode
                    def terminate(self): self.terminated = True; self.returncode = -15
                    def wait(self, timeout=None): return self.returncode
                client = None
                def launch(*_args, **kwargs):
                    nonlocal client
                    client = Client(kwargs['stdout']); return client
                def wait(stage, _timeout, predicate):
                    if stage == 'replica-fault-post-rebuild-writes':
                        if outcome == 'no-resume': client.returncode = 0
                        else: client.produce(4)
                    if stage == 'replica-fault-ledger-complete': client.complete()
                    value = predicate()
                    if not value: raise TimeoutError(stage)
                    return value
                topology = copy.deepcopy(before)
                if outcome == 'survivor-replaced': topology['r212']['uid'] = 'foreign-uid'
                lab = SimpleNamespace(BASE=root, DIRECTORY=root, WORKERS=('192.168.122.212', '192.168.122.165', '192.168.122.238'),
                    wait=Mock(side_effect=wait), owner=Mock(return_value={'run_id': 'owned-run'}),
                    replica_topology=Mock(side_effect=[before, topology]),
                    get=Mock(side_effect=lambda kind, *_args, **_kwargs: {'items': replicas} if not _args else
                             {'metadata': {'uid': 'r165-uid'}, 'spec': {'failedAt': 'observed'}}))
                def verify(*args, **kwargs):
                    code = args[-1].replace('/data/fault-ledger', str(data))
                    with patch.object(sys, 'stdin', io.StringIO(kwargs['stdin'])), redirect_stdout(io.StringIO()) as output:
                        exec(code, {})
                    return SimpleNamespace(stdout=output.getvalue())
                lab.kubectl = Mock(side_effect=verify)
                with patch.object(faults.tempfile, 'mkdtemp', return_value=str(observations)), \
                        patch.object(faults.subprocess, 'Popen', side_effect=launch), \
                        patch.object(faults, 'stop_replica_process', return_value={'name': 'r165', 'replica_uid': 'r165-uid'}) as stop, \
                        redirect_stdout(io.StringIO()) as output:
                    if outcome == 'none':
                        proof = faults.verify_replica_failure(lab, journal)
                        self.assertEqual(proof['summary']['confirmed'], 8)
                        self.assertFalse(observations.exists())
                        self.assertIn('LONGHORN_REPLICA_FAULT_ACKNOWLEDGEMENTS', output.getvalue())
                    else:
                        with self.assertRaises((ValueError, TimeoutError)):
                            faults.verify_replica_failure(lab, journal)
                        self.assertTrue(observations.is_dir())
                        self.assertIn('LONGHORN_PRIVATE_DIAGNOSTIC', output.getvalue())
                        self.assertNotIn('LONGHORN_REPLICA_FAULT_LEDGER_VERIFIED', output.getvalue())
                    self.assertEqual(stop.call_count, 0 if outcome == 'client-failed' else 1)
                    if outcome == 'survivor-replaced': self.assertTrue(client.terminated)


if __name__ == '__main__':
    unittest.main()
