"""Exercise the actual read-only preflight CLI; kubectl is an offline fixture."""
import copy
import hashlib
import json
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest


CLI = Path(__file__).resolve().parents[1] / 'product/kubernetes_preflight.py'
FAKE = '''#!/usr/bin/env python3
import json
from pathlib import Path
import sys
root = Path(__file__).parent
log = root / 'calls.jsonl'
calls = log.read_text().splitlines() if log.exists() else []
with log.open('a') as stream:
    stream.write(json.dumps(sys.argv[1:]) + '\\n')
responses = json.loads((root / 'responses.json').read_text())
response = responses[len(calls)]
if response.get('create_receipt'):
    (root / 'receipt.json').write_text('concurrent evidence\\n')
print(response.get('stdout', ''))
print(response.get('stderr', ''), file=sys.stderr)
raise SystemExit(response.get('rc', 0))
'''


class SourcePreflightContracts(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.binary = self.root / 'kubectl'
        self.binary.write_text(FAKE)
        self.binary.chmod(0o700)
        self.config = self.root / 'config'
        self.config.write_text('offline fixture only\n')
        self.output = self.root / 'receipt.json'
        self.call_log = self.root / 'calls.jsonl'
        self.nodes = {'items': [self.node('master-1'), self.node('worker-1')]}
        self.namespace = {'metadata': {'name': 'kube-system', 'uid': 'source-cluster-uid'},
                          'status': {'phase': 'Active'}}
        self.responses = [
            {'stdout': json.dumps({'clientVersion': {'gitVersion': 'v1.34.13'}})},
            {'stdout': json.dumps({'gitVersion': 'v1.33.6'})},
            {'stdout': 'health body is deliberately not parsed'},
            {'stdout': json.dumps(self.namespace)},
            {'stdout': json.dumps({'gitVersion': 'v1.33.6'})},
            {'stdout': 'ok'},
            {'stdout': json.dumps(self.namespace)},
            {'stdout': json.dumps(self.nodes)},
        ]

    @staticmethod
    def node(name):
        return {'metadata': {'name': name, 'uid': name + '-uid'}, 'spec': {}, 'status': {
            'nodeInfo': {'kubeletVersion': 'v1.33.6'},
            'conditions': [{'type': 'Ready', 'status': 'True'}]}}

    def invoke(self, **changes):
        self.call_log.unlink(missing_ok=True)
        (self.root / 'responses.json').write_text(json.dumps(self.responses))
        args = {
            'source': ['v1.33.6'], 'target': ['v1.34.13'],
            'kubectl': [str(self.binary)],
            'kubectl-sha256': [hashlib.sha256(self.binary.read_bytes()).hexdigest()],
            'kubeconfig': [str(self.config)],
            'api-server': ['https://192.168.122.217:6443', 'https://192.168.122.243:6443'],
            'node': ['master-1', 'worker-1'], 'output': [str(self.output)],
        }
        args.update(changes)
        command = [sys.executable, '-B', str(CLI)]
        for key, values in args.items():
            for value in values:
                command.extend(['--' + key, value])
        return subprocess.run(command, capture_output=True, text=True, timeout=10)

    def calls(self):
        return [json.loads(line) for line in self.call_log.read_text().splitlines()] if self.call_log.exists() else []

    def refused(self, result, calls):
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn('UPGRADE_SOURCE_PREFLIGHT_REFUSED:', result.stderr)
        self.assertNotIn('Traceback', result.stderr)
        self.assertNotIn('OBSERVED_SUCCESS', result.stdout)
        self.assertFalse(self.output.exists())
        self.assertEqual(len(self.calls()), calls)

    def test_success_is_read_only_and_receipt_does_not_sign_parent(self):
        result = self.invoke()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('UPGRADE_SOURCE_PREFLIGHT_OBSERVED_SUCCESS', result.stdout)
        receipt = json.loads(self.output.read_text())
        self.assertEqual(receipt['mutation'], 'none')
        self.assertFalse(receipt['parent_acceptance'])
        self.assertEqual(len(receipt['api_backends']), 2)
        self.assertEqual([n['name'] for n in receipt['nodes']], ['master-1', 'worker-1'])
        self.assertEqual(receipt['cluster_identity'],
                         {'namespace': 'kube-system', 'uid': 'source-cluster-uid'})
        self.assertEqual([n['uid'] for n in receipt['nodes']], ['master-1-uid', 'worker-1-uid'])
        self.assertTrue(all(n['originally_schedulable'] for n in receipt['nodes']))
        self.assertEqual(stat.S_IMODE(self.output.stat().st_mode), 0o600)
        tails = [['version', '--client', '-o', 'json'], ['get', '--raw=/version'],
                 ['get', '--raw=/readyz'], ['get', 'namespace', 'kube-system', '-o', 'json'],
                 ['get', '--raw=/version'], ['get', '--raw=/readyz'],
                 ['get', 'namespace', 'kube-system', '-o', 'json'],
                 ['get', 'nodes', '-o', 'json']]
        servers = ['217', '217', '217', '217', '243', '243', '243', '217']
        for call, tail, server in zip(self.calls(), tails, servers):
            self.assertEqual(call[0:2], ['--kubeconfig', str(self.config)])
            self.assertEqual(call[2:4], ['--server', f'https://192.168.122.{server}:6443'])
            self.assertEqual(call[4:6], ['--insecure-skip-tls-verify=false', '--request-timeout=10s'])
            self.assertEqual(call[6:], tail)
        self.assertEqual(len(self.calls()), len(tails))
        self.assertEqual(sorted(p.name for p in self.root.iterdir()),
                         ['calls.jsonl', 'config', 'kubectl', 'receipt.json', 'responses.json'])

    def test_refuses_skips_downgrades_and_unpinned_versions_before_query(self):
        for target in ['v1.33.7', 'v1.32.9', 'v1.35.1', 'v2.34.1', 'stable',
                       'v1.34.13-rc.1', 'v1.034.13']:
            with self.subTest(target=target):
                self.refused(self.invoke(target=[target]), 0)

    def test_invalid_or_credential_endpoints_never_reach_kubectl(self):
        for endpoint in ['http://host:6443', 'https://host', 'https://host:0',
                         'https://host:65536', 'https://host:not-port', 'https://[bad',
                         'https://user:secret@host:6443', 'https://host:6443/',
                         'https://host:6443?secret', 'https://host:6443#fragment',
                         'https://host:6443\n']:
            with self.subTest(endpoint=endpoint):
                self.refused(self.invoke(**{'api-server': [endpoint]}), 0)

    def test_duplicate_inventory_and_wrong_checksum_refuse_before_query(self):
        for changes in [
            {'api-server': ['https://HOST:6443', 'https://host:6443']},
            {'node': ['master-1', 'master-1']}, {'node': ['unsafe name']},
            {'kubectl-sha256': ['0' * 64]},
        ]:
            with self.subTest(changes=changes):
                self.refused(self.invoke(**changes), 0)

    def test_failure_at_each_probe_stops_without_leaking_stderr(self):
        original = copy.deepcopy(self.responses)
        for index in range(8):
            with self.subTest(index=index):
                self.responses = copy.deepcopy(original)
                self.responses[index] = {'rc': 1, 'stdout': 'SENSITIVE_OUTPUT', 'stderr': 'SENSITIVE_SECRET'}
                result = self.invoke()
                self.refused(result, index + 1)
                self.assertNotIn('SENSITIVE', result.stdout + result.stderr)

    def test_mixed_backend_and_wrong_client_stop_at_first_failure(self):
        for index, value, count in [
            (0, {'clientVersion': {'gitVersion': 'v1.33.6'}}, 1),
            (1, {'gitVersion': 'v1.34.13'}, 2), (4, {'gitVersion': 'v1.33.5'}, 5),
        ]:
            with self.subTest(index=index):
                original = self.responses[index]
                self.responses[index] = {'stdout': json.dumps(value)}
                self.refused(self.invoke(), count)
                self.responses[index] = original

    def test_missing_extra_duplicate_and_unhealthy_nodes_refuse_receipt(self):
        cases = [[], [self.node('master-1')],
                 [self.node('master-1'), self.node('worker-1'), self.node('foreign')],
                 [self.node('master-1'), self.node('master-1')]]
        for kind in ['not-ready', 'duplicate-ready', 'deleting', 'wrong-kubelet',
                     'missing-uid', 'duplicate-uid', 'cordoned']:
            nodes = copy.deepcopy(self.nodes['items'])
            if kind == 'not-ready':
                nodes[0]['status']['conditions'][0]['status'] = 'False'
            elif kind == 'duplicate-ready':
                nodes[0]['status']['conditions'] *= 2
            elif kind == 'deleting':
                nodes[0]['metadata']['deletionTimestamp'] = '2026-11-15T00:00:00Z'
            elif kind == 'wrong-kubelet':
                nodes[0]['status']['nodeInfo']['kubeletVersion'] = 'v1.34.13'
            elif kind == 'missing-uid':
                del nodes[0]['metadata']['uid']
            elif kind == 'duplicate-uid':
                nodes[1]['metadata']['uid'] = nodes[0]['metadata']['uid']
            else:
                nodes[0]['spec']['unschedulable'] = True
            cases.append(nodes)
        for nodes in cases:
            with self.subTest(nodes=nodes):
                self.responses[-1] = {'stdout': json.dumps({'items': nodes})}
                self.refused(self.invoke(), 8)

    def test_foreign_inactive_missing_or_deleting_cluster_refuses_before_node_query(self):
        original = copy.deepcopy(self.responses)
        for kind in ['foreign', 'missing-uid', 'deleting', 'inactive', 'wrong-name']:
            with self.subTest(kind=kind):
                namespace = copy.deepcopy(self.namespace)
                if kind == 'foreign':
                    namespace['metadata']['uid'] = 'foreign-cluster'
                elif kind == 'missing-uid':
                    del namespace['metadata']['uid']
                elif kind == 'deleting':
                    namespace['metadata']['deletionTimestamp'] = '2026-11-15T00:00:00Z'
                elif kind == 'inactive':
                    namespace['status']['phase'] = 'Terminating'
                else:
                    namespace['metadata']['name'] = 'default'
                self.responses = copy.deepcopy(original)
                self.responses[6] = {'stdout': json.dumps(namespace)}
                self.refused(self.invoke(), 7)

    def test_malformed_api_objects_fail_closed_without_response_echo(self):
        for index, raw in [(0, 'SENSITIVE_BAD_JSON'), (0, '[]'),
                           (0, '{"clientVersion":null}'), (1, 'null'),
                           (3, '{"metadata":null}'), (6, '{"status":null}'),
                           (7, '{"items":{}}'), (7, '{"items":[null]}'),
                           (7, '{"items":[{"metadata":[],"status":{}}]}'),
                           (7, '{"items":[{"status":{"conditions":[null]}}]}')]:
            with self.subTest(index=index, raw=raw):
                original = self.responses[index]
                self.responses[index] = {'stdout': raw}
                result = self.invoke()
                self.refused(result, index + 1)
                self.assertNotIn('SENSITIVE', result.stdout + result.stderr)
                self.responses[index] = original

    def test_existing_receipt_and_symlink_are_preserved_before_any_query(self):
        for symlink in [False, True]:
            with self.subTest(symlink=symlink):
                original = self.root / 'original.json'
                original.write_text('previous evidence\n')
                if symlink:
                    self.output.symlink_to(original)
                else:
                    self.output.write_bytes(original.read_bytes())
                result = self.invoke()
                self.assertEqual(result.returncode, 1)
                self.assertEqual(self.calls(), [])
                self.assertEqual(self.output.read_text(), 'previous evidence\n')
                self.assertEqual(original.read_text(), 'previous evidence\n')
                self.output.unlink()

    def test_concurrent_receipt_is_preserved_and_temporary_file_removed(self):
        self.responses[-1]['create_receipt'] = True
        result = self.invoke()
        self.assertEqual(result.returncode, 1)
        self.assertIn('UPGRADE_SOURCE_PREFLIGHT_REFUSED:', result.stderr)
        self.assertNotIn('OBSERVED_SUCCESS', result.stdout)
        self.assertEqual(self.output.read_text(), 'concurrent evidence\n')
        self.assertEqual(len(self.calls()), 8)
        self.assertEqual(sorted(p.name for p in self.root.iterdir()),
                         ['calls.jsonl', 'config', 'kubectl', 'receipt.json', 'responses.json'])


if __name__ == '__main__':
    unittest.main()
