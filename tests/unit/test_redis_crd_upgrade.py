"""CRD schema upgrades must fail before mutation on shared identity or dry-run failure."""
from copy import deepcopy
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import yaml

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('redis_crd_contract', ROOT / 'roles/cluster-addon/files/redis-crd.py')
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)


def definition(name='redisclusters'):
    return {'apiVersion': 'apiextensions.k8s.io/v1', 'kind': 'CustomResourceDefinition',
            'metadata': {'name': name + '.' + gate.GROUP},
            'spec': {'group': gate.GROUP, 'scope': 'Namespaced',
                     'names': {'plural': name, 'kind': 'RedisCluster'},
                     'versions': [{'name': 'v1beta2', 'served': True, 'storage': True}]}}


class RedisCrdUpgradeTests(unittest.TestCase):
    def test_official_kubectl_printer_stream_accepts_all_objects_and_rejects_partial_or_junk(self):
        rows = [definition(name.split('.')[0]) for name in sorted(gate.NAMES)]
        stream = '\n'.join(json.dumps(row, indent=4) for row in rows) + '\n'
        with self.assertRaises(json.JSONDecodeError):
            json.loads(stream)
        self.assertEqual(gate.printed_definitions(stream), rows)
        self.assertEqual(gate.printed_definitions(json.dumps({'kind': 'List', 'items': rows})), rows)
        for bad in ['\n'.join(json.dumps(row) for row in rows[:-1]), stream + 'not-json', stream + 'null']:
            with self.assertRaises(ValueError):
                gate.printed_definitions(bad)

    def test_narrow_product_gate_requests_through_config_and_cannot_sign_off_version_upgrade(self):
        spec = importlib.util.spec_from_file_location('redis_crd_narrow_contract',
               ROOT / 'tests/helpers/redis-crd-upgrade-gate.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        original = yaml.safe_dump({'redis_mode': 'sentinel', 'redis_namespace': 'redis-gate',
                                   'redis_crd_upgrade_requested': False, 'redis_password_secret': 'owned'})
        updated = yaml.safe_load(module.requested_config(original))
        self.assertIs(updated['redis_crd_upgrade_requested'], True)
        self.assertEqual(updated['redis_password_secret'], 'owned')
        with self.assertRaises(ValueError):
            module.requested_config(original.replace('redis-gate', 'foreign'))
        runner = (ROOT/'tests/run_enterprise_regression.sh').read_text()
        shell = (ROOT/'tests/helpers/redis-focused-gate.sh').read_text()
        self.assertIn('"$MODE" == --redis-crd-upgrade', runner)
        self.assertIn('script=redis-crd-upgrade-gate.py', shell)
        source = (ROOT/'tests/helpers/redis-crd-upgrade-gate.py').read_text()
        self.assertIn('acceptance=partial-no-version-migration', source)
        self.assertIn("'setup', 'redis-gate', '07', '--tags', 'redis'", source)
        self.assertLess(source.index('capacity.replace_config(config, [original, requested], original)'),
                        source.index('fault.unlink()'))

    def test_shared_consumers_and_stored_versions_are_refused_before_upgrade(self):
        target = definition()
        current = deepcopy(target)
        current['status'] = {'storedVersions': ['v1beta2']}
        consumer = {'metadata': {'namespace': 'redis-owned', 'labels': {
                    'kubeauto.io/component': 'redis', 'kubeauto.io/managed-by': 'kubeauto'}}}
        gate.validate_existing(target, current, [consumer], 'redis-owned')
        for edit in [lambda c: c['metadata'].update(namespace='another-tenant'),
                     lambda c: c['metadata']['labels'].update({'kubeauto.io/managed-by': 'foreign'})]:
            bad = deepcopy(consumer)
            edit(bad)
            with self.assertRaises(ValueError):
                gate.validate_existing(target, current, [bad], 'redis-owned')
        for edit in [lambda c: c['status'].update(storedVersions=['removed-version']),
                     lambda c: c['spec'].update(conversion={'strategy': 'Webhook'}),
                     lambda c: c['metadata'].update(annotations={'kubeauto.io/redis-owner-namespace': 'foreign'}),
                     lambda c: c['spec']['names'].update(kind='Foreign')]:
            bad = deepcopy(current)
            edit(bad)
            with self.assertRaises(ValueError):
                gate.validate_existing(target, bad, [], 'redis-owned')

    def test_bundle_must_contain_exactly_four_official_names(self):
        rows = [definition(name.split('.')[0]) for name in sorted(gate.NAMES)]
        self.assertEqual(gate.definitions({'kind': 'List', 'items': rows}), rows)
        with self.assertRaises(ValueError):
            gate.definitions({'kind': 'List', 'items': rows[:-1]})
        bad = deepcopy(rows)
        bad[0]['metadata']['name'] = 'foreign.redis.example'
        with self.assertRaises(ValueError):
            gate.definitions({'kind': 'List', 'items': bad})

    def test_checksum_failure_and_server_dry_run_failure_do_not_mutate_or_create_backup(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            chart = root / 'redis-operator-0.27.0.tgz'
            chart.write_bytes(b'pinned-fixture')
            checksum = root / 'chart.sha256'
            args = SimpleNamespace(chart=chart, chart_sha256_file=checksum, helm=Path('helm'),
                                   kubeconfig=root/'kubeconfig', namespace='owned', backup_parent=root/'backups')
            checksum.write_text('0' * 64 + '  ' + chart.name)
            with patch.object(gate, 'execute') as execute:
                with self.assertRaises(ValueError): gate.upgrade(args)
                execute.assert_not_called()
            checksum.write_text(hashlib.sha256(chart.read_bytes()).hexdigest() + '  ' + chart.name)
            rows = [definition(name.split('.')[0]) for name in sorted(gate.NAMES)]
            current = deepcopy(rows)
            for i, row in enumerate(current):
                row['metadata'].update(uid='uid-' + str(i), resourceVersion='1')
                row['status'] = {'storedVersions': ['v1beta2']}
            replies = ['official-crds-yaml', json.dumps({'kind': 'List', 'items': rows})]
            for row in current: replies += [json.dumps(row), '{"items": []}']
            replies += [RuntimeError('server dry-run refused schema ownership')]
            with patch.object(gate, 'execute', side_effect=replies) as execute:
                with self.assertRaises(RuntimeError): gate.upgrade(args)
                commands = [call.args[0] for call in execute.call_args_list]
                apply = [cmd for cmd in commands if 'apply' in cmd]
                self.assertEqual(len(apply), 1)
                self.assertIn('--dry-run=server', apply[0])
                self.assertFalse(args.backup_parent.exists())
                planned = json.loads(execute.call_args_list[-1].kwargs['stdin'])['items']
                self.assertTrue(all(row['metadata']['resourceVersion'] == '1' for row in planned))
                self.assertTrue(all(row['metadata']['uid'].startswith('uid-') for row in planned))

    def test_product_request_is_explicit_and_precedes_operator_upgrade(self):
        config = yaml.safe_load((ROOT/'conf/config.yml').read_text())
        self.assertIs(config['redis_crd_upgrade_requested'], False)
        tasks = yaml.safe_load((ROOT/'roles/cluster-addon/tasks/redis.yml').read_text())
        upgrade = next(t for t in tasks if t['name'] == '显式受控更新已有 Redis CRD')
        self.assertEqual(upgrade['when'], 'redis_crd_upgrade_requested | default(false) | bool')
        self.assertLess(tasks.index(upgrade), next(i for i,t in enumerate(tasks) if t['name'] == '安装或升级 Redis Operator'))
        source = (ROOT/'roles/cluster-addon/files/redis-crd.py').read_text()
        self.assertNotIn('--force-conflicts', source)
        self.assertIn('os.fsync(stream.fileno())', source)


if __name__ == '__main__':
    unittest.main()
