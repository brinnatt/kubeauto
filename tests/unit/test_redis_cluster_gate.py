"""Independent Cluster inputs and certificate names before live product installation."""
import copy
from contextlib import redirect_stdout
import io
import importlib.util
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch
import json

ROOT = Path(__file__).resolve().parents[2]
def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

cluster = load('redis_cluster_contract', ROOT / 'tests/helpers/redis-cluster-gate.py')
fixtures = load('redis_cluster_credentials', ROOT / 'tests/helpers/redis-lab-config.py')


class RedisClusterTests(unittest.TestCase):
    def test_seed_recovery_rejects_foreign_or_injected_fault_before_cluster_mutation(self):
        with tempfile.TemporaryDirectory() as tmp:
            gate = Mock()
            gate.cluster = Path(tmp)
            state = {'run_id': 'a' * 32, 'cluster_uid': 'ours', 'cr_uid': 'cr', 'stage': 'seed'}
            path = gate.cluster / 'redis-stability-fault.json'
            (gate.cluster / 'redis-cluster-fixture.json').write_text(json.dumps(state))
            (gate.cluster / 'redis-lab-owner.json').write_text(json.dumps({'cluster_uid': 'ours'}))
            gate.get.side_effect = lambda kind, *args: (
                {'metadata': {'uid': 'cr'}} if kind == 'rediscluster' else {'items': []})
            path.write_text(json.dumps(state))
            self.assertEqual(cluster.seed_failure_state(gate), state)
            for field, value in [('stage', 'pod'), ('run_id', '../foreign'),
                                 ('cluster_uid', 'foreign'), ('cr_uid', 'foreign')]:
                path.write_text(json.dumps({**state, field: value}))
                with self.assertRaisesRegex(ValueError, 'seed-only'):
                    cluster.seed_failure_state(gate)
                gate.k.assert_not_called()
            path.write_text(json.dumps(state))
            fault = gate.cluster / 'redis-cluster-process-fault-owned.json'
            fault.write_text('{}')
            with self.assertRaisesRegex(ValueError, 'seed-only'):
                cluster.seed_failure_state(gate)
            fault.unlink()
            gate.get.side_effect = lambda kind, *args: (
                {'metadata': {'uid': 'cr'}} if kind == 'rediscluster' else {'items': [{'foreign': True}]})
            with self.assertRaisesRegex(ValueError, 'seed-only'):
                cluster.seed_failure_state(gate)
            gate.k.assert_not_called()

    def test_seed_cleanup_removes_only_failed_uuid_key_after_source_retention_verified(self):
        with tempfile.TemporaryDirectory() as tmp:
            gate = Mock()
            gate.base = gate.cluster = Path(tmp)
            gate.ns, gate.name, gate.key, gate.value = 'redis-gate', 'source', 'new-probe', b'retained'
            state = {'cluster_uid': 'ours', 'name': cluster.NAME, 'namespace': gate.ns}
            seed = {'run_id': 'a' * 32, 'cluster_uid': 'ours', 'cr_uid': 'cr', 'stage': 'seed'}
            path = gate.cluster / 'redis-stability-fault.json'
            path.write_text(json.dumps(seed))
            (gate.cluster / 'redis-cluster-fixture.json').write_text(json.dumps(state))
            (gate.cluster / 'redis-lab-owner.json').write_text(json.dumps({'cluster_uid': 'ours'}))
            gate.get.return_value = {'items': []}
            client = Mock()
            client.command.side_effect = lambda command, *args: b'retained' if command == 'GET' else 0
            gate.client.return_value = client
            gate.seeded_primary.return_value = ('primary', Mock())
            with patch.object(cluster, 'seed_failure_state', return_value=seed), \
                    patch.object(cluster, 'cleanup_fixture'), redirect_stdout(io.StringIO()) as output:
                cluster.clean_owned_fixture(gate)
            client.command.assert_any_call('DEL', 'kubeauto:redis:focused:' + seed['run_id'])
            self.assertFalse(path.exists())
            self.assertIn('REDIS_STABILITY_SEED_RECOVERY_PASS', output.getvalue())
            gate.cleanup_keys.assert_called_once()

    def test_install_download_failure_blocks_fixture_and_source_mutation(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            source = base / 'clusters/redis-gate'
            source.mkdir(parents=True)
            gate = Mock()
            gate.base, gate.cluster, gate.ns = base, source, 'redis-gate'
            gate.k.return_value = ''
            gate.get.return_value = {'items': []}
            gate.run.side_effect = ValueError('download unavailable')
            args = Mock(base=base)
            with self.assertRaisesRegex(ValueError, 'download unavailable'):
                cluster.install_fixture(gate, Mock(), args, Mock(), base, Mock())
            gate.run.assert_called_once_with(
                [str(base / '.venv/bin/python'), str(base / 'kubecli.py'),
                 'download', '-E', 'redis'], timeout=800, capture=False)
            gate.seeded_primary.assert_not_called()
            self.assertFalse((source / 'redis-cluster-fixture.json').exists())
            self.assertFalse((base / 'clusters' / cluster.FIXTURE).exists())

    def test_new_cluster_fixture_resolves_its_runtime_without_altering_source_sentinel(self):
        source = {'redis_namespace': 'redis-gate', 'redis_mode': 'sentinel', 'redis_ver': '8.10.2'}
        before = copy.deepcopy(source)
        target = cluster.cluster_config(source)
        self.assertEqual(source, before)
        self.assertEqual(target['redis_ver'], '8.10.2')
        self.assertEqual(target['redis_cluster_image_ver'], '8.10.2-kubeauto.1')
        self.assertEqual(target['redis_mode'], 'cluster')

    def test_controller_diagnostics_precede_potentially_blocking_member_cli(self):
        probe = (ROOT / 'tests/helpers/redis-cluster-probe.py').read_text()
        self.assertLess(probe.index("'logs', 'deployment/redis-operator'"), probe.index('for pod in pods:'))
        self.assertLess(probe.index("owner.get('cluster_uid')"), probe.index("'logs', 'deployment/redis-operator'"))

    def test_config_wait_requires_new_ready_members_before_assertion_and_cleanup(self):
        groups = {role: [{'metadata': {'name': role + str(i)}, 'spec': {'volumes': []},
                          'status': {'podIP': '10.0.0.1', 'conditions': [{'type': 'Ready', 'status': 'True'}]}}
                         for i in range(count)] for role, count in (('leader', 3), ('follower', 6))}
        self.assertFalse(cluster.startup_members_ready(groups), 'old Ready Pods must not unlock new-mount assertions')
        current = copy.deepcopy(groups)
        for pods in current.values():
            for pod in pods:
                pod['spec']['volumes'] = [{'name': 'external-config', 'configMap': {'name': cluster.NAME + '-config'}}]
        self.assertTrue(cluster.startup_members_ready(current))
        image = 'registry.talkschool.cn:5000/brinnatt/redis-operator:0.27.0-kubeauto.2'
        self.assertFalse(cluster.startup_members_ready(current, image), 'correct mounts do not prove the new agent image')
        for pods in current.values():
            for pod in pods:
                pod['spec']['initContainers'] = [{'name': 'init-config', 'image': image}]
        self.assertTrue(cluster.startup_members_ready(current, image))
        old_agent = copy.deepcopy(current)
        old_agent['leader'][0]['spec']['initContainers'][0]['image'] = image.replace('-kubeauto.2', '-kubeauto.1')
        self.assertFalse(cluster.startup_members_ready(old_agent, image))
        for field, value in (('podIP', None), ('conditions', [])):
            invalid = copy.deepcopy(current)
            invalid['leader'][0]['status'][field] = value
            self.assertFalse(cluster.startup_members_ready(invalid))
        invalid = copy.deepcopy(current)
        invalid['leader'][0]['metadata']['deletionTimestamp'] = 'now'
        self.assertFalse(cluster.startup_members_ready(invalid))
        fixture = Mock()
        fixture.members.side_effect = [groups, current]
        fixture.source.forwards = {'old-port': 1234}
        with patch.object(cluster.time, 'sleep') as pause:
            cluster.wait_startup_rollout(fixture)
        pause.assert_called_once_with(5)
        fixture.health.assert_called_once()
        self.assertEqual(fixture.source.forwards, {})

    def test_startup_rejects_missing_role_mount_before_reading_sensitive_config(self):
        pod = {'metadata': {'name': 'member'}, 'spec': {
            'volumes': [{'name': 'external-config', 'configMap': {'name': cluster.NAME + '-config'}}],
            'containers': [{'name': 'redis', 'volumeMounts': [
                {'name': 'external-config', 'mountPath': '/etc/redis/external.conf.d'}]}],
            'initContainers': [{'name': 'init-config', 'volumeMounts': [
                {'name': 'external-config', 'mountPath': '/etc/redis/external.conf.d'}]}]}}
        config = {'redis_cluster_replicas': 2, 'redis_appendfsync': 'everysec',
                  'redis_min_replicas_to_write': 1, 'redis_min_replicas_max_lag': 10}
        values = {'cluster-migration-barrier': '2', 'appendonly': 'yes', 'appendfsync': 'everysec',
                  'min-replicas-to-write': '1', 'min-replicas-max-lag': '10',
                  'cluster-require-full-coverage': 'yes', 'tls-auth-clients': 'yes'}
        gate, client = Mock(), Mock()
        gate.ns = 'redis-gate'
        client.command.side_effect = lambda command, operation, key: [key.encode(), values[key].encode()]
        cluster.verify_startup_config(gate, pod, client, config)
        self.assertIn('>/dev/null', gate.k.call_args.args[-3])
        self.assertNotIn('cat', gate.k.call_args.args)
        for kind in ('containers', 'initContainers'):
            invalid = copy.deepcopy(pod)
            invalid['spec'][kind][0]['volumeMounts'] = []
            gate.k.reset_mock()
            with self.assertRaisesRegex(ValueError, 'mount'):
                cluster.verify_startup_config(gate, invalid, client, config)
            gate.k.assert_not_called()
        invalid = copy.deepcopy(pod)
        invalid['spec']['volumes'][0]['configMap']['name'] = 'foreign-config'
        with self.assertRaisesRegex(ValueError, 'volume'):
            cluster.verify_startup_config(gate, invalid, client, config)
        values['cluster-migration-barrier'] = '1'
        with self.assertRaisesRegex(ValueError, 'effective'):
            cluster.verify_startup_config(gate, pod, client, config)

    def test_failed_cluster_clean_retains_first_failure_and_always_removes_source_probe(self):
        with tempfile.TemporaryDirectory() as tmp:
            gate = Mock()
            gate.base = gate.cluster = Path(tmp)
            gate.ns, gate.name, gate.value = 'redis-gate', 'source', b'retained'
            state = {'cluster_uid': 'ours', 'name': cluster.NAME, 'namespace': gate.ns}
            (gate.cluster / 'redis-cluster-fixture.json').write_text(json.dumps(state))
            (gate.cluster / 'redis-lab-owner.json').write_text(json.dumps({'cluster_uid': 'ours'}))
            gate.get.return_value = {'items': []}
            gate.seeded_primary.return_value = ('primary', Mock())
            first = ValueError('original cleanup boundary failure')
            with patch.object(cluster, 'cleanup_fixture', side_effect=first):
                with self.assertRaises(ValueError) as caught: cluster.clean_owned_fixture(gate)
            self.assertIs(caught.exception, first)
            gate.cleanup_keys.assert_called_once()
            gate.health.assert_not_called()
            gate.cleanup_keys.side_effect = ValueError('secondary source probe cleanup error')
            with patch.object(cluster, 'cleanup_fixture', side_effect=first), redirect_stdout(io.StringIO()):
                with self.assertRaises(ValueError) as caught: cluster.clean_owned_fixture(gate)
            self.assertIs(caught.exception, first)

    def test_clean_rejects_foreign_identity_and_stale_process_before_mutation(self):
        with tempfile.TemporaryDirectory() as tmp:
            gate = Mock()
            gate.base = gate.cluster = Path(tmp)
            gate.ns = 'redis-gate'
            state = {'cluster_uid': 'ours', 'name': cluster.NAME, 'namespace': gate.ns,
                     'cr_uid': 'cr', 'config_sha256': 'config'}
            journal = gate.cluster / 'redis-cluster-fixture.json'
            journal.write_text(json.dumps(state))
            (gate.cluster / 'redis-lab-owner.json').write_text(json.dumps({'cluster_uid': 'foreign'}))
            with patch.object(cluster, 'cleanup_fixture') as cleanup:
                with self.assertRaisesRegex(ValueError, 'identity'): cluster.clean_owned_fixture(gate)
                cleanup.assert_not_called()
                gate.seeded_primary.assert_not_called()
                (gate.cluster / 'redis-lab-owner.json').write_text(json.dumps({'cluster_uid': 'ours'}))
                (gate.cluster / 'redis-cluster-process-fault-owned.json').write_text('{}')
                with self.assertRaisesRegex(ValueError, 'process'): cluster.clean_owned_fixture(gate)
                cleanup.assert_not_called()
                gate.seeded_primary.assert_not_called()

    def test_clean_dispatch_is_allowlisted_and_never_claims_install(self):
        helper = (ROOT / 'tests/helpers/redis-focused-gate.sh').read_text()
        allowlist = helper.split('case "$module" in ', 1)[1].split(') ;;', 1)[0].split('|')
        self.assertIn('cluster-clean', allowlist)
        self.assertIn('cluster-install|cluster-clean|cluster-config|cluster-config-repair) script=redis-cluster-gate.py', helper)
        self.assertIn('"$MODE" == --redis-cluster-clean',
                      (ROOT / 'tests/run_enterprise_regression.sh').read_text())
        self.assertNotIn("'cluster-clean':", (ROOT / 'tests/helpers/redis-evidence.py').read_text())
        self.assertIn('cluster-config', allowlist)
        self.assertIn('"$MODE" == --redis-cluster-config',
                      (ROOT / 'tests/run_enterprise_regression.sh').read_text())

    def test_cleanup_attempts_each_owned_scope_without_masking_first_failure(self):
        first = RuntimeError('first failure')
        cleanup = Mock(side_effect=ValueError('cleanup failure'))
        keys = Mock()
        with redirect_stdout(io.StringIO()) as output:
            cluster.cleanup_actions([cleanup, keys], first)
        self.assertIn('REDIS_CLUSTER_CLEANUP_FAIL', output.getvalue())
        keys.assert_called_once()
        with self.assertRaisesRegex(ValueError, 'cleanup failure'):
            cluster.cleanup_actions([cleanup, keys], None)
        self.assertEqual(keys.call_count, 2)

    def test_cleanup_refuses_foreign_identity_and_deletes_with_uid_resource_version(self):
        obj = {'metadata': {'name': 'owned-pvc', 'namespace': 'redis-gate', 'uid': 'uid-a',
               'resourceVersion': '7', 'labels': {'kubeauto.io/component': 'redis',
               'kubeauto.io/managed-by': 'kubeauto', 'app': cluster.NAME + '-leader'}}}
        self.assertEqual(cluster.owned_metadata(obj, 'redis-gate', uid='uid-a'), obj['metadata'])
        for values in ({'namespace': 'foreign'}, {'uid': 'uid-b'}, {'app': 'another-app'}):
            with self.assertRaises(ValueError):
                cluster.owned_metadata(obj, values.get('namespace', 'redis-gate'),
                                       uid=values.get('uid', 'uid-a'), app=values.get('app'))
        gate = Mock()
        gate.ns = 'redis-gate'
        cluster.delete_exact(gate, obj, 'persistentvolumeclaims')
        import json
        payload = json.loads(gate.k.call_args.kwargs['input_text'])
        self.assertEqual(payload['preconditions'], {'uid': 'uid-a', 'resourceVersion': '7'})
        self.assertEqual(gate.k.call_args.args[2], '/api/v1/namespaces/redis-gate/persistentvolumeclaims/owned-pvc')

    def test_independent_fixture_really_renders_cluster_cr_and_exact_data_namespace(self):
        from tests.unit.test_redis_delivery import render
        resource, = render('cluster', redis_name=cluster.NAME, redis_namespace='redis-gate',
                           redis_tls_secret=cluster.TLS_SECRET, redis_password_secret='redis-gate-auth',
                           redis_acl_secret='redis-gate-acl', redis_pvc_size='2Gi')
        self.assertEqual(resource['kind'], 'RedisCluster')
        self.assertEqual(resource['metadata']['name'], cluster.NAME)
        self.assertEqual(resource['spec']['redisLeader']['replicas'], 3)
        self.assertEqual(resource['spec']['redisFollower']['replicas'], 6)
        self.assertEqual(resource['spec']['TLS']['secret']['secretName'], cluster.TLS_SECRET)

    def test_separate_cluster_configuration_preserves_source_and_operator_watch_boundary(self):
        source = {'redis_namespace': 'redis-gate', 'redis_mode': 'sentinel',
                  'redis_operator_namespace': 'redis-gate-operator', 'redis_password_secret': 'redis-gate-auth',
                  'unrelated': {'keep': True}, 'redis_pvc_size': '3Gi'}
        original = copy.deepcopy(source)
        result = cluster.cluster_config(source)
        self.assertEqual(source, original)
        self.assertEqual(result['redis_operator_namespace'], source['redis_operator_namespace'])
        self.assertEqual(result['redis_password_secret'], source['redis_password_secret'])
        self.assertEqual(result['unrelated'], source['unrelated'])
        self.assertEqual((result['redis_cluster_shards'], result['redis_cluster_replicas']), (3, 2))
        self.assertNotEqual(result['redis_mode'], source['redis_mode'])
        self.assertEqual(result['redis_pvc_size'], '2Gi')
        for bad in ({'redis_namespace': 'foreign', 'redis_mode': 'sentinel'},
                    {'redis_namespace': 'redis-gate', 'redis_mode': 'cluster'}):
            with self.assertRaises(ValueError): cluster.cluster_config(bad)

    def test_inventory_changes_only_independent_deployment_directory(self):
        original = fixtures.inventory()
        rendered = cluster.inventory_for_cluster(original)
        self.assertEqual(rendered.replace('clusters/redis-cluster-gate', 'clusters/redis-gate'), original)
        for invalid in ('foreign', original + original):
            with self.assertRaises(ValueError): cluster.inventory_for_cluster(invalid)

    def test_cluster_certificate_covers_actual_members_without_reusing_sentinel_leaf(self):
        tls = fixtures.credentials(cluster.NAME)[2]['stringData']
        with tempfile.TemporaryDirectory() as tmp:
            cert = Path(tmp) / 'leaf.crt'
            cert.write_text(tls['tls.crt'])
            for role in ('leader', 'follower'):
                name = f'{cluster.NAME}-{role}-0.{cluster.NAME}-{role}-headless.redis-gate.svc.cluster.local'
                result = subprocess.run(['openssl', 'x509', '-in', str(cert), '-noout', '-checkhost', name],
                                        capture_output=True, text=True, check=True)
                self.assertIn('does match certificate', result.stdout)
        for invalid in ('bad\nname', '-invalid', 'x' * 64):
            with self.assertRaises(ValueError): fixtures.credentials(invalid)


if __name__ == '__main__':
    unittest.main()
