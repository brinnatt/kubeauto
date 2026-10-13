"""Cluster rollback is confined to qualified pins and real ready runtime containers."""
from copy import deepcopy
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

import yaml

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('redis_cluster_upgrade_contract', ROOT/'tests/helpers/redis-cluster-upgrade-gate.py')
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)


class ClusterUpgradeTests(unittest.TestCase):
    def test_interrupted_repair_refuses_changed_input_or_fixture_before_writing_config(self):
        for changed in ('input', 'fixture-uid', 'symlink'):
            with self.subTest(changed=changed), tempfile.TemporaryDirectory() as tmp:
                base = Path(tmp); source_dir = base / 'clusters/redis-gate'; source_dir.mkdir(parents=True)
                target = base / 'clusters/redis-cluster-gate'; target.mkdir()
                config = {'redis_namespace': 'redis-gate', 'redis_mode': 'cluster',
                          'redis_cluster_shards': 3, 'redis_cluster_replicas': 2,
                          'redis_operator_image_ver': '0.27.0-kubeauto.2',
                          'redis_cluster_image_ver': '8.10.2-kubeauto.1'}
                original = yaml.safe_dump(config, sort_keys=False).encode()
                digest = hashlib.sha256(original).hexdigest()
                state = {'run_id': 'a' * 32, 'cr_uid': 'owned-cr', 'original_config_sha256': digest}
                fixture = {'healthy': True, 'cluster_uid': 'ours', 'cr_uid': 'owned-cr',
                           'config_sha256': digest, 'run_id': 'b' * 32}
                if changed == 'input': config['redis_tls_secret'] = 'concurrent-change'
                if changed == 'fixture-uid': fixture['cr_uid'] = 'foreign'
                (target / 'config.yml').write_text(yaml.safe_dump(config, sort_keys=False))
                (target / 'redis-fixture-owner.json').write_text(json.dumps({'run_id': 'b' * 32}))
                (source_dir / 'redis-cluster-upgrade-fault.json').write_text(json.dumps(state))
                (source_dir / 'redis-cluster-fixture.json').write_text(json.dumps(fixture))
                (source_dir / 'redis-lab-owner.json').write_text(json.dumps({'cluster_uid': 'ours'}))
                if changed == 'symlink':
                    target.rename(base / 'foreign')
                    target.symlink_to(base / 'foreign', target_is_directory=True)
                source = Mock(cluster=source_dir); inputs = Mock()
                with self.assertRaises(ValueError):
                    gate.repair_interrupted(source, Mock(base=base), Mock(), Mock(), inputs, base)
                inputs.replace_config.assert_not_called()
                source.run.assert_not_called()

    def rollback_fixture(self):
        image = 'current-image'
        name = 'redis-gate-cluster-data'
        resource = Mock(ns='redis-gate', journal={'cr_uid': 'owned-cr'})
        objects = {('rediscluster', name): {'metadata': {'uid': 'owned-cr', 'name': name,
                   'namespace': 'redis-gate', 'labels': {'kubeauto.io/component': 'redis',
                                                        'kubeauto.io/managed-by': 'kubeauto'}},
                   'spec': {'kubernetesConfig': {'image': image}}}}
        groups = {}
        for role, count in (('leader', 3), ('follower', 6)):
            app = name + '-' + role
            objects[('statefulset', app)] = {
                'metadata': {'name': app, 'namespace': 'redis-gate', 'uid': role + '-sts',
                    'ownerReferences': [{'controller': True, 'kind': 'RedisCluster',
                                         'name': name, 'uid': 'owned-cr'}]},
                'spec': {'template': {'spec': {'containers': [{'name': app, 'image': image}]}}}}
            groups[role] = [{'metadata': {'name': app + '-' + str(i), 'namespace': 'redis-gate',
                'uid': role + str(i), 'resourceVersion': '7',
                'labels': {'app': app, 'kubeauto.io/component': 'redis', 'kubeauto.io/managed-by': 'kubeauto'},
                'ownerReferences': [{'controller': True, 'kind': 'StatefulSet', 'name': app,
                                     'uid': role + '-sts'}]},
                'spec': {'containers': [{'name': app, 'image': image}]},
                'status': {'conditions': [{'type': 'Ready', 'status': 'True'}]}}
                for i in range(count)]
        groups['leader'][2]['status']['conditions'][0]['status'] = 'False'
        groups['leader'][2]['spec']['containers'][0]['image'] = 'bad-old-image'
        # A healthy old Pod must finish its ordinary rollout without deletion.
        groups['follower'][0]['spec']['containers'][0]['image'] = 'old-image'
        resource.get.side_effect = lambda kind, name: objects[kind, name]
        resource.members.return_value = groups
        return resource, objects, groups

    def test_forced_rollback_requires_restored_templates_and_exact_failed_old_pod_identity(self):
        resource, objects, groups = self.rollback_fixture()
        plan = gate.rollback_deletions(resource, 'current-image')
        self.assertEqual(len(plan), 1)
        self.assertEqual(plan[0][0], 'redis-gate-cluster-data-leader-2')
        self.assertEqual(plan[0][1]['preconditions'], {'uid': 'leader2', 'resourceVersion': '7'})
        resource.k.assert_not_called()
        objects['statefulset', 'redis-gate-cluster-data-follower']['spec']['template']['spec']['containers'][0]['image'] = 'old-image'
        self.assertIsNone(gate.rollback_deletions(resource, 'current-image'))
        resource.k.assert_not_called()
        groups['follower'][5]['metadata']['ownerReferences'][0]['uid'] = 'foreign'
        with self.assertRaisesRegex(ValueError, 'foreign Pod'):
            gate.rollback_deletions(resource, 'current-image')
        resource.k.assert_not_called()

    def test_forced_rollback_rejects_foreign_cr_sts_and_partial_boundary(self):
        for boundary in ('cr', 'sts', 'member-count'):
            with self.subTest(boundary=boundary):
                resource, objects, groups = self.rollback_fixture()
                if boundary == 'cr': objects['rediscluster', 'redis-gate-cluster-data']['metadata']['uid'] = 'foreign'
                elif boundary == 'sts': objects['statefulset', 'redis-gate-cluster-data-follower']['metadata']['ownerReferences'][0]['uid'] = 'foreign'
                else: groups['follower'].pop()
                with self.assertRaises(ValueError): gate.rollback_deletions(resource, 'current-image')
                resource.k.assert_not_called()

    def test_repair_keeps_product_entry_and_reaps_it_when_identity_probe_fails(self):
        source = Mock(base=Path('/owned'))
        process = Mock(returncode=0)
        process.poll.return_value = None
        with patch.object(gate.subprocess, 'Popen', return_value=process) as launch, \
                patch.object(gate, 'rollback_deletions', side_effect=ValueError('foreign identity')):
            with self.assertRaisesRegex(ValueError, 'foreign identity'):
                gate.reconcile_version(source, Mock(), 'current', repair=True)
        self.assertEqual(launch.call_args.args[0], ['/owned/.venv/bin/python', '/owned/kubecli.py',
                          'setup', 'redis-cluster-gate', '07', '--tags', 'redis'])
        self.assertEqual(launch.call_args.kwargs['stdin'], subprocess.DEVNULL)
        process.terminate.assert_called_once()
        process.wait.assert_called_once_with(timeout=15)

    def test_qualified_cluster_input_preserves_controller_topology_and_customer_credentials(self):
        config = {'redis_namespace':'redis-gate', 'redis_mode':'cluster', 'redis_cluster_shards':3,
                  'redis_cluster_replicas':2, 'redis_operator_image_ver':'0.27.0-kubeauto.2',
                  'redis_tls_secret':'owned-tls', 'redis_cluster_image_ver':'8.10.2-kubeauto.1'}
        old = yaml.safe_load(gate.version_input(yaml.safe_dump(config), '8.10.1'))
        self.assertEqual(old, dict(config, redis_cluster_image_ver='8.10.1'))
        for update in ({'redis_namespace':'foreign'}, {'redis_cluster_shards':4},
                       {'redis_operator_image_ver':'0.26.0'}, {'redis_mode':'sentinel'}):
            with self.assertRaises(ValueError):
                gate.version_input(yaml.safe_dump(dict(config, **update)), '8.10.1')
        with self.assertRaises(ValueError):
            gate.version_input(yaml.safe_dump(config), 'latest')

    def test_role_named_container_and_ready_image_are_required_not_an_exporter_or_old_pod(self):
        image = 'registry.talkschool.cn:5000/brinnatt/redis:8.10.2-kubeauto.1'
        groups = {role:[{'metadata':{'name':role+str(i), 'labels':{'app':'data-'+role}},
                        'spec':{'containers':[{'name':'data-'+role, 'image':image}]},
                        'status':{'conditions':[{'type':'Ready', 'status':'True'}]}}
                       for i in range(count)] for role,count in [('leader',3),('follower',6)]}
        self.assertTrue(gate.members_ready(groups, image))
        for mutate in (lambda g:g['leader'].pop(),
                       lambda g:g['leader'][0]['spec']['containers'][0].update(name='exporter'),
                       lambda g:g['leader'][0]['spec']['containers'][0].update(image='old'),
                       lambda g:g['leader'][0]['metadata'].update(deletionTimestamp='now'),
                       lambda g:g['leader'][0]['status']['conditions'][0].update(status='False')):
            bad = deepcopy(groups)
            mutate(bad)
            self.assertFalse(gate.members_ready(bad, image))
