"""Actual file allocation, native-action identity and snapshot retention contracts."""
import copy
import ast
from contextlib import redirect_stdout
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('trim_probe', ROOT / 'tests/helpers/longhorn-trim.py')
trim = importlib.util.module_from_spec(spec); spec.loader.exec_module(trim)


class TrimContracts(unittest.TestCase):
    def fixture(self, directory):
        topology = {'replica-' + str(n): {'uid': 'r' + str(n), 'node': 'longhorn-node-' + str(n)}
                    for n in (212, 165, 238)}
        volume = {'metadata': {'uid': 'v'}, 'spec': {'dataEngine': 'v1', 'accessMode': 'rwo',
                  'encrypted': False, 'unmapMarkSnapChainRemoved': 'ignored'}, 'status': {'currentNodeID': 'longhorn-node-212'}}
        engine = {'spec': {'volumeName': 'volume', 'nodeID': 'longhorn-node-212'},
                  'status': {'currentState': 'running', 'snapshots': {}, 'purgeStatus': {}}}
        replicas = [{'metadata': {'name': name, 'uid': row['uid']}, 'spec': {'nodeID': row['node'],
                     'diskPath': '/data/test', 'dataDirectoryName': name}}
                    for name, row in topology.items()]
        native = {'metadata': {'uid': 'snapshot-uid'}}
        manager = {'metadata': {'name': 'manager', 'uid': 'manager-uid', 'labels': {'app': 'longhorn-manager'},
                   'ownerReferences': [{'kind': 'DaemonSet', 'uid': 'ds-uid', 'controller': True}]},
                   'spec': {'nodeName': 'longhorn-node-212'},
                   'status': {'podIP': '10.244.1.2', 'phase': 'Running',
                              'conditions': [{'type': 'Ready', 'status': 'True'}]}}
        def get(kind, *_args, **_kwargs):
            return copy.deepcopy({'volumes.longhorn.io': volume, 'engines.longhorn.io': {'items': [engine]},
                                  'replicas.longhorn.io': {'items': replicas}, 'snapshots.longhorn.io': native,
                                  'namespace': {'metadata': {'uid': 'ns-uid'}},
                                  'daemonset': {'metadata': {'uid': 'ds-uid'}},
                                  'pods': {'items': [manager]}, 'pod': manager}[kind])
        def wait(_name, _timeout, check):
            value = check()
            if not value: raise TimeoutError('condition did not converge')
            return value
        lab = SimpleNamespace(DIRECTORY=Path(directory), DATA='/data/test',
            WORKERS=('192.168.122.212', '192.168.122.165', '192.168.122.238'),
            get=Mock(side_effect=get), replica_topology=Mock(return_value=copy.deepcopy(topology)),
            kubectl=Mock(return_value=SimpleNamespace(stdout='{"id":"volume"}')),
            remote=Mock(return_value=SimpleNamespace(stdout='123456')), wait=Mock(side_effect=wait),
            write_private=Mock(), run_requests=Mock(), bind_snapshots=Mock(), remove=Mock(), payload_program=Mock(return_value='read-original'))
        lab.owner = Mock(return_value={'namespace_uid': 'ns-uid', 'disks': {ip: {'path': lab.DATA, 'phase': 'mounted', 'uuid': 'fs-uuid'}
                                                for ip in lab.WORKERS}})
        journal = {'volume': 'volume', 'volume_uid': 'v', 'namespace': 'owned'}
        return trim.Probe(lab, journal, topology), lab, volume, engine, replicas, native

    def test_actual_filler_fsync_readback_unlink_and_foreign_file_refusals(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(trim, 'SIZE', 2 * 1024**2):
            path = Path(directory)/'filler'
            row = trim.filler(str(path), 'create')
            self.assertEqual(row['size'], trim.SIZE)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            with self.assertRaises(FileExistsError): trim.filler(str(path), 'create')
            row_bad = dict(row, inode=row['inode']+1)
            with self.assertRaisesRegex(ValueError, 'identity'): trim.filler(str(path), 'remove', row_bad)
            raw = path.read_bytes(); path.write_bytes(b'foreign' + raw[7:])
            with self.assertRaisesRegex(ValueError, 'bytes'): trim.filler(str(path), 'remove', row)
            path.write_bytes(raw)
            result = trim.filler(str(path), 'remove', row)
            self.assertGreater(result['free_bytes'], 0)
            self.assertFalse(path.exists())
            path.symlink_to(Path(directory)/'foreign')
            with self.assertRaises(OSError): trim.filler(str(path), 'remove', row)
            with self.assertRaises(ValueError): trim.filler(str(path), 'unsafe')

    def test_host_allocation_counts_real_blocks_and_rejects_links_and_wrong_boundaries(self):
        original_stat = Path.stat
        def disk_stat(path, *args, **kwargs):
            return SimpleNamespace(st_dev=-1) if path == Path('/') else original_stat(path, *args, **kwargs)
        with tempfile.TemporaryDirectory() as directory, patch.object(Path, 'stat', disk_stat):
            base = Path(directory)/'data'; replica = base/'replicas/r1'; replica.mkdir(parents=True)
            (replica/'volume-head.img').write_bytes(os.urandom(8192))
            self.assertEqual(trim.allocated(str(replica), str(base)), os.lstat(replica/'volume-head.img').st_blocks * 512)
            (replica/'link').symlink_to(replica/'volume-head.img')
            with self.assertRaisesRegex(ValueError, 'non-file'): trim.allocated(str(replica), str(base))
            (replica/'link').unlink()
            with self.assertRaisesRegex(ValueError, 'outside'): trim.allocated(str(base), str(base))
            link = base/'replicas/alias'; link.symlink_to(replica)
            with self.assertRaisesRegex(ValueError, 'outside'): trim.allocated(str(link), str(base))

    def test_native_action_requires_original_volume_replicas_snapshot_and_no_purge_error(self):
        for defect in ('none', 'uid', 'encrypted', 'engine', 'topology', 'snapshot', 'purge', 'response'):
            with self.subTest(defect=defect), tempfile.TemporaryDirectory() as directory:
                probe, lab, volume, engine, _replicas, _native = self.fixture(directory)
                if defect == 'uid': volume['metadata']['uid'] = 'foreign'
                if defect == 'encrypted': volume['spec']['encrypted'] = True
                if defect == 'engine': engine['status']['currentState'] = 'stopped'
                if defect == 'topology': lab.replica_topology.return_value = None
                if defect == 'snapshot': engine['status']['snapshots']['foreign'] = {'usercreated': True}
                if defect == 'purge': engine['status']['purgeStatus']['r1'] = {'error': 'bad'}
                if defect == 'response': lab.kubectl.return_value.stdout = '{"id":"foreign"}'
                if defect == 'none':
                    probe.action('trimFilesystem')
                    args = lab.kubectl.call_args.args
                    self.assertEqual(args[:7], ('--request-timeout=170s', '-n', 'longhorn-system', 'exec', '-i', 'manager', '-c'))
                    self.assertEqual(args[-1], 'http://10.244.1.2:9500/v1/volumes/volume?action=trimFilesystem')
                    self.assertIn('--max-time', args)
                    self.assertNotIn('--raw', args)
                    self.assertEqual(lab.kubectl.call_args.kwargs, {'stdin': '{}', 'timeout': 180})
                else:
                    with self.assertRaises(ValueError): probe.action('trimFilesystem')
                    if defect != 'response': lab.kubectl.assert_not_called()
        with self.assertRaises(ValueError): probe.action('deleteEverything')

    def test_native_action_refuses_foreign_unready_or_replaced_manager_before_post(self):
        for defect in ('namespace', 'daemonset', 'node', 'unready', 'duplicate', 'missing', 'replaced', 'ip'):
            with self.subTest(defect=defect), tempfile.TemporaryDirectory() as directory:
                probe, lab, *_rest = self.fixture(directory)
                original_get = lab.get.side_effect
                def get(kind, *args, **kwargs):
                    obj = original_get(kind, *args, **kwargs)
                    if defect == 'namespace' and kind == 'namespace': obj['metadata']['uid'] = 'foreign'
                    if kind == 'pods':
                        pod = obj['items'][0]
                        if defect == 'daemonset': pod['metadata']['ownerReferences'][0]['uid'] = 'foreign'
                        if defect == 'node': pod['spec']['nodeName'] = 'foreign'
                        if defect == 'unready': pod['status']['conditions'][0]['status'] = 'False'
                        if defect == 'ip': pod['status']['podIP'] = 'host/unsafe'
                        if defect == 'missing': obj['items'] = []
                        if defect == 'duplicate': obj['items'].append(copy.deepcopy(pod))
                    if defect == 'replaced' and kind == 'pod': obj['metadata']['uid'] = 'foreign'
                    return obj
                lab.get.side_effect = get
                with self.assertRaises(ValueError): probe.action('trimFilesystem')
                lab.kubectl.assert_not_called()

    def test_host_filesystem_uuid_type_and_mount_must_match_the_owned_receipt(self):
        for defect in ('none', 'uuid', 'type', 'mount', 'empty'):
            with self.subTest(defect=defect):
                rows = [{'target': '/data/test', 'uuid': 'fs-uuid', 'fstype': 'ext4'}]
                if defect == 'uuid': rows[0]['uuid'] = 'foreign'
                if defect == 'type': rows[0]['fstype'] = 'tmpfs'
                if defect == 'mount': rows[0]['target'] = '/'
                if defect == 'empty': rows = []
                with patch.object(trim.subprocess, 'run', return_value=SimpleNamespace(stdout=json.dumps({'filesystems': rows}))), \
                        patch.object(trim, 'allocated', return_value=123) as allocated:
                    if defect == 'none': self.assertEqual(trim.mounted_allocation('/data/test/replicas/r1', '/data/test', 'fs-uuid'), 123)
                    else:
                        with self.assertRaises(ValueError): trim.mounted_allocation('/data/test/replicas/r1', '/data/test', 'fs-uuid')
                        allocated.assert_not_called()

    def test_host_measurement_rejects_replaced_replica_foreign_node_disk_or_directory(self):
        for defect in ('none', 'uid', 'node', 'disk', 'directory', 'symlink-component', 'receipt'):
            with self.subTest(defect=defect), tempfile.TemporaryDirectory() as directory:
                probe, lab, _volume, _engine, replicas, _native = self.fixture(directory)
                if defect == 'uid': replicas[0]['metadata']['uid'] = 'foreign'
                if defect == 'node': replicas[0]['spec']['nodeID'] = 'foreign'
                if defect == 'disk': replicas[0]['spec']['diskPath'] = '/'
                if defect == 'directory': replicas[0]['spec']['dataDirectoryName'] = '../foreign'
                if defect == 'symlink-component': replicas[0]['spec']['dataDirectoryName'] = '..'
                if defect == 'receipt': lab.owner.return_value['disks'] = {}
                with redirect_stdout(io.StringIO()):
                    if defect == 'none':
                        self.assertEqual(len(probe.sample('baseline')), 3)
                        for call in lab.remote.call_args_list:
                            self.assertIn(call.args[0], lab.WORKERS)
                            self.assertNotIn('rm ', call.args[1][-1])
                    else:
                        with self.assertRaises(ValueError): probe.sample('baseline')
                        lab.remote.assert_not_called()

    def test_prepare_proves_guest_free_and_snapshot_retention_before_native_purge(self):
        for defect in ('none', 'unallocated', 'guest-full', 'snapshot-lost', 'replaced-snapshot', 'purge-error'):
            with self.subTest(defect=defect), tempfile.TemporaryDirectory() as directory:
                probe, lab, _volume, engine, _replicas, native = self.fixture(directory)
                baseline = {name: 4096 for name in probe.topology}
                retained = {name: value + trim.SIZE for name, value in baseline.items()}
                samples = [baseline, baseline if defect == 'unallocated' else retained,
                           baseline if defect == 'snapshot-lost' else retained]
                identity = {'free_bytes': 100, 'sha256': 'filler-hash'}
                removed = {'free_bytes': 100 + (0 if defect == 'guest-full' else trim.SIZE)}
                probe.sample = Mock(side_effect=samples); probe.file = Mock(side_effect=[identity, removed])
                actions = []
                def action(value):
                    actions.append(value)
                    if value == 'snapshotPurge' and defect == 'purge-error':
                        engine['status']['purgeStatus']['r1'] = {'error': 'purge failure'}
                probe.action = Mock(side_effect=action)
                def bind(journal):
                    journal['native_snapshots'][probe.snapshot]['uid'] = 'snapshot-uid'
                    engine['status']['snapshots'][probe.snapshot] = {'usercreated': True, 'removed': True}
                lab.bind_snapshots.side_effect = bind
                if defect == 'replaced-snapshot': native['metadata']['uid'] = 'foreign'
                if defect == 'none':
                    proof = probe.prepare(123)
                    self.assertEqual(proof['guest_freed_bytes'], trim.SIZE)
                    self.assertEqual(proof['snapshot'], {'name': probe.snapshot, 'uid': 'snapshot-uid'})
                    self.assertEqual(actions, ['trimFilesystem', 'trimFilesystem', 'snapshotPurge'])
                    lab.run_requests.assert_called_once_with(123, [{'kind': 'snapshot', 'name': probe.snapshot, 'volume': 'volume'}])
                    lab.remove.assert_called_once()
                    patch_ops = json.loads(lab.kubectl.call_args.kwargs['stdin'])
                    self.assertEqual(patch_ops[0], {'op': 'test', 'path': '/metadata/uid', 'value': 'v'})
                    self.assertEqual(patch_ops[-1]['value'], 'disabled')
                else:
                    with self.assertRaises(ValueError): probe.prepare(123)

    def test_post_remount_requires_actual_reclaim_on_every_replica_and_original_bytes(self):
        for defect in ('none', 'one-unreclaimed', 'corruption'):
            with self.subTest(defect=defect), tempfile.TemporaryDirectory() as directory:
                probe, lab, _volume, _engine, _replicas, native = self.fixture(directory)
                retained = {name: trim.SIZE+4096 for name in probe.topology}
                after = {name: 4096 for name in probe.topology}
                if defect == 'one-unreclaimed': after[next(iter(after))] += trim.SIZE
                probe.sample = Mock(return_value=after); probe.action = Mock()
                lab.kubectl.return_value.stdout = 'foreign' if defect == 'corruption' else 'original'
                request = {'kind': 'snapshot', 'name': 'owned', 'volume': 'volume'}
                probe.journal['native_snapshots'] = {'owned': {'uid': 'snapshot-uid', 'request': request}}
                native['metadata']['deletionTimestamp'] = '2026-10-19T06:08:09Z'
                proof = {'replicas': probe.topology, 'retained': retained,
                         'snapshot': {'name': 'owned', 'uid': 'snapshot-uid'}}
                with redirect_stdout(io.StringIO()) as output:
                    if defect == 'none': probe.finish(proof, 'payload', 'original')
                    else:
                        with self.assertRaises((ValueError, TimeoutError)): probe.finish(proof, 'payload', 'original')
                self.assertEqual('LONGHORN_TRIM_RESULT' in output.getvalue(), defect == 'none')

    def test_finish_carries_original_removed_v1_head_parent_and_refuses_foreign_or_live_snapshot(self):
        for defect in ('none', 'cr-gone', 'uid', 'request', 'proof', 'live', 'not-deleting', 'foreign'):
            with self.subTest(defect=defect), tempfile.TemporaryDirectory() as directory:
                probe, lab, _volume, engine, _replicas, native = self.fixture(directory)
                request = {'kind': 'snapshot', 'name': 'owned', 'volume': 'volume'}
                probe.journal['native_snapshots'] = {'owned': {'uid': 'snapshot-uid', 'request': request}}
                native['metadata']['deletionTimestamp'] = '2026-10-19T06:08:09Z'
                engine['status']['snapshots']['owned'] = {'usercreated': True, 'removed': True}
                retained = {name: trim.SIZE + 4096 for name in probe.topology}
                proof = {'replicas': probe.topology, 'retained': retained,
                         'snapshot': {'name': 'owned', 'uid': 'snapshot-uid'}}
                if defect == 'uid': native['metadata']['uid'] = 'replacement'
                if defect == 'request': request['volume'] = 'foreign'
                if defect == 'proof': proof['snapshot']['name'] = 'foreign'
                if defect == 'live': engine['status']['snapshots']['owned']['removed'] = False
                if defect == 'not-deleting': del native['metadata']['deletionTimestamp']
                if defect == 'foreign': engine['status']['snapshots']['foreign'] = {'usercreated': True, 'removed': True}
                if defect == 'cr-gone':
                    original = lab.get.side_effect
                    lab.get.side_effect = lambda kind, *args, **kwargs: None if kind == 'snapshots.longhorn.io' else original(kind, *args, **kwargs)
                probe.sample = Mock(return_value={name: 4096 for name in probe.topology})
                lab.kubectl.side_effect = [SimpleNamespace(stdout='{"id":"volume"}'), SimpleNamespace(stdout='original')]
                with redirect_stdout(io.StringIO()) as output:
                    if defect in ('none', 'cr-gone'):
                        probe.finish(proof, 'payload', 'original')
                        self.assertIn('LONGHORN_TRIM_RESULT', output.getvalue())
                    else:
                        with self.assertRaises(ValueError): probe.finish(proof, 'payload', 'original')
                        lab.kubectl.assert_not_called()

    def test_business_remount_hooks_preserve_replica_ledger_and_isolate_trim(self):
        tree = ast.parse((ROOT/'tests/helpers/longhorn-lab.py').read_text())
        branch = next(node for node in ast.walk(tree) if isinstance(node, ast.If) and
                      any(isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute) and
                          call.func.attr == 'verify_client_ledger' for call in ast.walk(ast.Module(body=node.body, type_ignores=[]))) and
                      isinstance(node.test, ast.Compare) and isinstance(node.test.left, ast.Name) and
                      node.test.left.id == 'scenario')
        # Select the smallest final hook rather than an enclosing setup branch.
        branches = [node for node in ast.walk(branch) if isinstance(node, ast.If) and
                    any(isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call) and
                        isinstance(stmt.value.func, ast.Attribute) and stmt.value.func.attr == 'verify_client_ledger'
                        for stmt in node.body)]
        self.assertEqual(len(branches), 1)
        code = compile(ast.Module(body=branches, type_ignores=[]), 'actual-business-remount', 'exec')
        for scenario in ('replica_failure', 'trim'):
            faults = Mock(); helper = Mock()
            import sys
            values = dict(scenario=scenario, faults=faults, trim=helper, sys=sys, __name__=__name__, journal={},
                          fault_proof={'summary': {'confirmed': 4}}, trim_proof={}, payload='p', digest='d', json=json)
            with redirect_stdout(io.StringIO()) as output: exec(code, values)
            self.assertEqual(faults.verify_client_ledger.called, scenario == 'replica_failure')
            self.assertEqual(helper.finish.called, scenario == 'trim')
            self.assertEqual('LONGHORN_REPLICA_FAULT_REMOUNT_LEDGER_VERIFIED' in output.getvalue(), scenario == 'replica_failure')


if __name__ == '__main__':
    unittest.main()
