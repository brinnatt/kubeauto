"""Redis middleware static and template contracts."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import tarfile
import tempfile
import subprocess
import unittest
from unittest.mock import patch
import io
import os
import sys
from contextlib import contextmanager
from pathlib import Path

import yaml
from jinja2 import Environment, StrictUndefined

from common.constants import KubeConstant

ROOT = Path(__file__).resolve().parents[2]
ROLE = ROOT / "roles" / "cluster-addon"
CONFIG = (ROOT / "conf" / "config.yml").read_text(encoding="utf-8")
TASKS = (ROLE / "tasks" / "redis.yml").read_text(encoding="utf-8")
TEMPLATE = (ROLE / "templates" / "redis" / "instances.yaml.j2").read_text(encoding="utf-8")
VALUES = (ROLE / "templates" / "redis" / "operator-values.yaml.j2").read_text(encoding="utf-8")
MATRIX = ROOT / "tests" / "redis-test-matrix.yaml"
RUNNER = ROOT / "tests" / "helpers" / "redis-regression.sh"
CLEANUP = ROOT / "tests" / "helpers" / "redis-cleanup.sh"
CHART = ROLE / "files" / "redis-operator-0.27.0.tgz"
EXT = ROOT.parent / "kubeauto-ext-images-dockerfile"


@contextmanager
def pending_qualification_fixture():
    """Run real preflight code against fixed inputs, with no live fallback."""
    with tempfile.TemporaryDirectory() as tmp:
        fixture = Path(tmp)
        (fixture / 'tests').mkdir()
        (fixture / 'tests/redis-test-matrix.yaml').write_text(yaml.safe_dump({
            'cases': [{'id': f'REDIS-{i:02d}', 'status': 'pending' if i == 1 else 'pass'}
                      for i in range(1, 25)]}))
        calls = fixture / 'unexpected-calls'
        python = fixture / 'python'
        python.write_text(f'''#!{sys.executable}
import os,sys
from pathlib import Path
if sys.argv[1:] != ['-', {str(ROOT)!r}]:
    Path({str(calls)!r}).write_text('unexpected Python invocation')
    raise SystemExit(99)
os.execv({sys.executable!r}, [{sys.executable!r}, '-', {str(fixture)!r}])
''')
        python.chmod(0o755)
        for command in ('ssh', 'rsync', 'kubectl', 'kubecli'):
            stub = fixture / command
            stub.write_text('#!/bin/sh\nprintf called >> "$REDIS_TEST_REMOTE_CALLS"\nexit 99\n')
            stub.chmod(0o755)
        yield {**os.environ, 'REDIS_PYTHON': str(python),
               'PATH': tmp + ':' + os.environ['PATH'],
               'REDIS_TEST_REMOTE_CALLS': str(calls)}, calls


def render(mode: str, **overrides) -> list[dict]:
    values = {
        "redis_mode": mode, "redis_ver": "8.10.2", "redis_cluster_image_ver": "8.10.2-kubeauto.1", "redis_exporter_ver": "v1.93.0",
        "redis_operator_ver": "0.27.0", "redis_name": "redis-prod", "redis_namespace": "redis",
        "redis_storage_class": "csi-block", "redis_pvc_size": "20Gi", "redis_replication_replicas": 3,
        "redis_sentinel_replicas": 3, "redis_cluster_shards": 3, "redis_cluster_replicas": 2,
        "redis_password_secret": "", "redis_password_secret_key": "password", "redis_tls_secret": "",
        "redis_acl_secret": "", "redis_exporter_enabled": True,
        "redis_exporter_cpu_request": "100m", "redis_exporter_memory_request": "128Mi",
        "redis_cpu_request": "500m", "redis_memory_request": "1Gi", "redis_cpu_limit": "2",
        "redis_memory_limit": "2Gi", "redis_maxmemory_percent": 70, "redis_min_replicas_to_write": 1,
        "redis_min_replicas_max_lag": 10, "redis_appendfsync": "everysec", "redis_topology_key": "kubernetes.io/hostname",
    }
    values.update(overrides)
    env = Environment(undefined=StrictUndefined)
    env.filters["quote"] = lambda value: '"' + str(value).replace('"', '\\"') + '"'
    env.filters["bool"] = bool
    return [doc for doc in yaml.safe_load_all(env.from_string(TEMPLATE).render(**values)) if doc]


class RedisDeliveryTests(unittest.TestCase):
    def test_cluster_bus_protection_is_rendered_only_for_supported_runtime_versions(self):
        template = (ROLE / 'templates/redis/config.yaml.j2').read_text()
        for version, supported in (('8.10.1', False), ('8.10.2', True),
                                   ('8.10.2-kubeauto.1', True), ('8.10.10', True)):
            config = yaml.safe_load(Environment(undefined=StrictUndefined).from_string(template).render(
                redis_name='redis-prod', redis_namespace='redis', redis_mode='cluster',
                redis_cluster_image_ver=version, redis_appendfsync='everysec',
                redis_min_replicas_to_write=1, redis_min_replicas_max_lag=10,
                redis_tls_secret='tls', redis_cluster_replicas=2))
            text = config['data']['redis-additional.conf']
            self.assertEqual('cluster-bus-port-protected-mode yes\n' in text, supported)
            self.assertIn('tls-auth-clients yes\n', text)
            self.assertIn('min-replicas-to-write 1\n', text)
            self.assertIn('cluster-migration-barrier 2\n', text)

    def test_persistence_filename_is_explicit_across_initialization_agent_versions(self):
        template = (ROLE / 'templates/redis/config.yaml.j2').read_text()
        for mode in ('sentinel', 'cluster'):
            config = yaml.safe_load(Environment(undefined=StrictUndefined).from_string(template).render(
                redis_name='redis-prod', redis_namespace='redis', redis_mode=mode,
                redis_appendfsync='everysec', redis_min_replicas_to_write=1,
                redis_min_replicas_max_lag=10, redis_tls_secret='tls', redis_cluster_replicas=2,
                redis_cluster_image_ver='8.10.2-kubeauto.1'))
            self.assertIn('appendfilename "appendonly.aof"\n', config['data']['redis-additional.conf'])

    def test_cluster_startup_config_is_bound_to_both_upstream_role_fields(self):
        resource, = render('cluster')
        for role in ('redisLeader', 'redisFollower'):
            self.assertEqual(resource['spec'][role]['redisConfig']['additionalRedisConfig'],
                             'redis-prod-config')
        self.assertIn('cluster-migration-barrier 2', resource['spec']['redisConfig']['dynamicConfig'])

    def test_cluster_migration_cannot_donate_below_declared_replica_budget(self):
        env = Environment(undefined=StrictUndefined)
        for replicas in (1, 2, 3):
            resource, = render('cluster', redis_cluster_replicas=replicas)
            self.assertIn('cluster-migration-barrier ' + str(replicas),
                          resource['spec']['redisConfig']['dynamicConfig'])
            config = yaml.safe_load(env.from_string((ROLE / 'templates/redis/config.yaml.j2').read_text()).render(
                redis_name='redis-prod', redis_namespace='redis', redis_mode='cluster',
                redis_appendfsync='everysec', redis_min_replicas_to_write=1,
                redis_min_replicas_max_lag=10, redis_tls_secret='tls', redis_cluster_replicas=replicas,
                redis_cluster_image_ver='8.10.2-kubeauto.1'))
            self.assertIn('cluster-migration-barrier ' + str(replicas) + '\n',
                          config['data']['redis-additional.conf'])
        for resource in render('sentinel'):
            field = 'redisSentinelConfig' if resource['kind'] == 'RedisSentinel' else 'redisConfig'
            self.assertFalse(any(line.startswith('cluster-migration-barrier ')
                                 for line in resource['spec'].get(field, {}).get('dynamicConfig', [])))

    @classmethod
    def setUpClass(cls):
        cls.kc = KubeConstant()

    def test_versions_and_images_are_pinned(self):
        self.assertEqual(self.kc.v_redis_operator, "0.27.0")
        self.assertEqual(self.kc.v_redis_operator_image, "0.27.0-kubeauto.2")
        self.assertEqual(self.kc.v_redis, "8.10.2")
        self.assertEqual(self.kc.v_redis_exporter, "v1.93.0")
        self.assertEqual(self.kc.component_images["redis"], [
            "brinnatt/redis-operator:0.27.0-kubeauto.2", "brinnatt/redis:8.10.2",
            "brinnatt/redis:8.10.2-kubeauto.1", "brinnatt/redis-exporter:v1.93.0"
        ])

    def test_cluster_runtime_revision_is_isolated_from_sentinel(self):
        self.assertEqual(self.kc.v_redis_cluster_image, '8.10.2-kubeauto.1')
        for resource in render('sentinel'):
            self.assertEqual(resource['spec']['kubernetesConfig']['image'],
                             'registry.talkschool.cn:5000/brinnatt/redis:8.10.2')
        cluster, = render('cluster')
        self.assertEqual(cluster['spec']['kubernetesConfig']['image'],
                         'registry.talkschool.cn:5000/brinnatt/redis:8.10.2-kubeauto.1')
        override, = render('cluster', redis_cluster_image_ver='8.10.1')
        self.assertEqual(override['spec']['kubernetesConfig']['image'],
                         'registry.talkschool.cn:5000/brinnatt/redis:8.10.1')

    def test_integer_cpu_quantities_render_as_strings_for_controller_roundtrip(self):
        for mode in ('sentinel', 'cluster'):
            document = render(mode, redis_cpu_request='1', redis_cpu_limit='2',
                              redis_exporter_cpu_request='1')[0]
            resources = document['spec']['kubernetesConfig']['resources']
            self.assertEqual(resources['requests']['cpu'], '1')
            self.assertEqual(resources['limits']['cpu'], '2')
            self.assertEqual(document['spec']['redisExporter']['resources']['requests']['cpu'], '1')

    def test_operator_cr_apply_uses_official_client_side_convergence_without_force(self):
        tasks = yaml.safe_load(TASKS)
        task = next(t for t in tasks if t['name'] == '应用 Redis 实例声明')
        argv = task['ansible.builtin.command']['argv']
        self.assertIn('apply', argv)
        self.assertNotIn('--server-side', argv)
        self.assertNotIn('--force-conflicts', argv)
        self.assertIn('--field-manager=kubeauto-redis', argv)

    def test_sentinel_uses_upstream_stable_dns_announcement_and_rejects_stale_ip(self):
        document = render('sentinel')[1]
        self.assertEqual(document['spec']['redisSentinelConfig']['resolveHostnames'], 'yes')
        self.assertEqual(document['spec']['redisSentinelConfig']['announceHostnames'], 'yes')
        self.assertEqual(document['metadata']['labels']['kubeauto.io/resource'], 'sentinel')
        self.assertEqual(render('sentinel')[0]['metadata']['annotations']['kubeauto.io/sentinel-addressing'], 'pod-dns')
        tasks = yaml.safe_load(TASKS)
        publish = next(i for i, task in enumerate(tasks) if task['name'] == '先声明 Sentinel DNS 与复制关联')
        apply = next(i for i, task in enumerate(tasks) if task['name'] == '应用 Redis 实例声明')
        self.assertLess(publish, apply)
        self.assertIn('--selector=kubeauto.io/resource=sentinel', tasks[publish]['ansible.builtin.command']['argv'])
        self.assertEqual(tasks[publish]['when'], "redis_mode == 'sentinel'")
        spec = importlib.util.spec_from_file_location('redis_health_dns', ROLE / 'files/redis-health.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        primary = {'metadata': {'name': 'data-1'}, 'spec': {'hostname': 'data-1', 'subdomain': 'data-headless'},
                   'status': {'podIP': '10.1.1.20'}}
        self.assertTrue(module.sentinel_endpoint_matches(
            ['data-1.data-headless.redis-gate.svc.cluster.local', '6379'], primary, 'redis-gate', 'cluster.local'))
        self.assertTrue(module.sentinel_endpoint_matches(['10.1.1.20', '6379'], primary, 'redis-gate', 'cluster.local'))
        for reply in (['10.1.1.19', '6379'], ['foreign.data-headless.redis-gate.svc.cluster.local', '6379'],
                      ['data-1.data-headless.other.svc.cluster.local', '6379'], ['10.1.1.20', '26379'], ['10.1.1.20']):
            self.assertFalse(module.sentinel_endpoint_matches(reply, primary, 'redis-gate', 'cluster.local'))

    def test_chart_is_checksum_bound_and_contains_crds(self):
        digest = hashlib.sha256(CHART.read_bytes()).hexdigest()
        self.assertEqual(digest, self.kc.v_redis_operator_chart_sha256)
        with tarfile.open(CHART) as archive:
            names = set(archive.getnames())
        self.assertIn("redis-operator/crds/crds.yaml", names)
        self.assertIn("redis-operator/templates/operator-deployment.yaml", names)

    def test_chart_is_the_complete_unmodified_official_release(self):
        provenance = json.loads(CHART.with_name('redis-operator-0.27.0.provenance.json').read_text())
        self.assertEqual(provenance['asset_id'], 624041570)
        self.assertEqual(provenance['sha256'], hashlib.sha256(CHART.read_bytes()).hexdigest())
        with tarfile.open(CHART) as archive:
            files = [entry for entry in archive.getmembers() if entry.isfile()]
            self.assertEqual(len(files), provenance['expected_files'])
            self.assertIn('redis-operator/templates/aggregate-roles.yaml', archive.getnames())
            self.assertEqual(yaml.safe_load(archive.extractfile('redis-operator/Chart.yaml'))['version'], '0.27.0')
        self.assertIn('aggregate: false', (ROLE / 'templates/redis/operator-values.yaml.j2').read_text())

    def test_chart_checksum_command_works_in_the_product_working_directory(self):
        result = subprocess.run(['sha256sum', '--check', CHART.name + '.sha256'],
                                cwd=ROLE / 'files', capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_config_is_opt_in_and_secret_free(self):
        for phrase in ('redis_install: "no"', 'redis_mode: "sentinel"', 'redis_storage_class: ""',
                       'redis_sentinel_replicas: 3', 'redis_cluster_shards: 3', 'redis_cluster_replicas: 2',
                       'redis_min_replicas_to_write: 1', 'redis_appendfsync: "everysec"'):
            self.assertIn(phrase, CONFIG)
        self.assertNotRegex(CONFIG, r"(?m)^redis_.*password:\s*[^\"\n]+")

    def test_task_has_ownership_checksum_and_product_wait_contracts(self):
        for phrase in ("sha256sum", "--server-side", "--field-manager=kubeauto-redis",
                       "redis-health.py", "not (redis_backup_enabled | bool)"):
            self.assertIn(phrase, TASKS)
        self.assertIn("keepAfterDelete", TEMPLATE)

    def test_product_secret_validation_handles_dotted_keys_and_api_failures(self):
        import os
        task = next(t for t in yaml.safe_load(TASKS) if t['name']=='校验 Redis 外部 Secret 已存在且包含所需键')
        env = Environment(undefined=StrictUndefined)
        env.filters['quote'] = lambda value: '"' + str(value) + '"'
        script = env.from_string(task['ansible.builtin.shell']).render(
            cluster_dir='/tmp/redis-gate', redis_namespace='redis-gate', redis_password_secret='auth',
            redis_password_secret_key='password', redis_tls_secret='tls', redis_acl_secret='acl')
        with tempfile.TemporaryDirectory() as tmp:
            kubectl = Path(tmp)/'kubectl'
            kubectl.write_text('''#!/usr/bin/env python3
import json,os,sys
if os.environ.get('REDIS_TEST_API_FAIL'): raise SystemExit(42)
data={k:'c2VjcmV0' for k in ['password','tls.crt','tls.key','ca.crt','user.acl']}
data.pop(os.environ.get('REDIS_TEST_MISSING_KEY',''),None)
print(json.dumps({'data':data}))
''')
            kubectl.chmod(0o755)
            child_env = {**os.environ,'PATH':tmp+':'+os.environ['PATH']}
            result = subprocess.run(['bash','-c',script],env=child_env,capture_output=True,text=True)
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertNotIn('c2VjcmV0',result.stdout+result.stderr)
            for flag,value in [('REDIS_TEST_MISSING_KEY','tls.crt'),('REDIS_TEST_API_FAIL','yes')]:
                result = subprocess.run(['bash','-c',script],env={**child_env,flag:value},capture_output=True,text=True)
                self.assertNotEqual(result.returncode,0)
                self.assertNotIn('c2VjcmV0',result.stdout+result.stderr)

    def test_sentinel_template_has_replication_and_sentinel(self):
        docs = render("sentinel")
        self.assertEqual([d["kind"] for d in docs], ["RedisReplication", "RedisSentinel"])
        self.assertEqual(docs[0]["spec"]["clusterSize"], 3)
        self.assertEqual(docs[1]["spec"]["redisSentinelConfig"]["quorum"], "2")
        self.assertTrue(docs[1]["spec"]["redisExporter"]["enabled"])
        self.assertEqual(docs[0]["spec"]["storage"]["keepAfterDelete"], True)

    def test_cluster_template_has_shards_replicas_and_slot_protection(self):
        doc = render("cluster")[0]
        self.assertEqual(doc["kind"], "RedisCluster")
        self.assertEqual(doc["spec"]["clusterVersion"], "v8")
        self.assertEqual(doc["spec"]["redisLeader"]["replicas"], 3)
        self.assertEqual(doc["spec"]["redisFollower"]["replicas"], 6)
        dynamic = doc["spec"]["redisConfig"]["dynamicConfig"]
        self.assertIn("cluster-require-full-coverage yes", dynamic)

    def test_security_secret_references_render_for_both_modes(self):
        env = Environment(undefined=StrictUndefined)
        env.filters["quote"] = lambda value: '"' + str(value).replace('"', '\\"') + '"'
        env.filters["bool"] = bool
        values = {
            "redis_mode": "sentinel", "redis_ver": "8.10.2", "redis_exporter_ver": "v1.93.0",
            "redis_name": "redis-prod", "redis_namespace": "redis", "redis_storage_class": "csi-block",
            "redis_pvc_size": "20Gi", "redis_replication_replicas": 3, "redis_sentinel_replicas": 3,
            "redis_cluster_shards": 3, "redis_cluster_replicas": 2, "redis_password_secret": "redis-auth",
            "redis_password_secret_key": "password", "redis_tls_secret": "redis-tls", "redis_acl_secret": "redis-acl",
            "redis_exporter_enabled": True, "redis_exporter_cpu_request": "100m", "redis_exporter_memory_request": "128Mi",
            "redis_cpu_request": "500m", "redis_memory_request": "1Gi", "redis_cpu_limit": "2", "redis_memory_limit": "2Gi",
            "redis_maxmemory_percent": 70, "redis_min_replicas_to_write": 1, "redis_min_replicas_max_lag": 10,
            "redis_appendfsync": "everysec", "redis_topology_key": "kubernetes.io/hostname",
        }
        docs = [doc for doc in yaml.safe_load_all(env.from_string(TEMPLATE).render(**values)) if doc]
        replication, sentinel = docs
        self.assertEqual(replication["spec"]["TLS"]["secret"]["secretName"], "redis-tls")
        self.assertEqual(replication["spec"]["TLS"]["ca"], "ca.crt")
        self.assertEqual(replication["spec"]["acl"]["secret"]["secretName"], "redis-acl")
        self.assertEqual(replication["spec"]["acl"]["secret"]["items"][0]["path"], "user.acl")
        self.assertEqual(sentinel["spec"]["TLS"]["secret"]["secretName"], "redis-tls")
        self.assertEqual(sentinel["spec"]["redisSentinelConfig"]["redisReplicationPassword"]["secretKeyRef"]["key"], "password")

    def test_operator_values_enable_init_gate_for_maxmemory_or_acl(self):
        env = Environment(undefined=StrictUndefined)
        values = {"redis_operator_ver": "0.27.0", "redis_namespace": "redis", "redis_operator_replicas": 2,
                  "redis_acl_secret": "", "redis_maxmemory_percent": 70}
        rendered = env.from_string(VALUES).render(**values)
        self.assertIn("GenerateConfigInInitContainer: true", rendered)
        self.assertIn('watchNamespace: "redis"', rendered)

    def test_operator_gate_is_at_official_chart_root(self):
        env = Environment(undefined=StrictUndefined)
        rendered = yaml.safe_load(env.from_string(VALUES).render(
            redis_operator_ver="0.27.0", redis_namespace="redis", redis_operator_replicas=2,
            redis_acl_secret="", redis_maxmemory_percent=70))
        self.assertEqual(rendered.get("featureGates"), {"GenerateConfigInInitContainer": True})
        self.assertNotIn("featureGates", rendered["redisOperator"])

    def test_legacy_chart_removes_the_flag_unregistered_in_its_official_binary(self):
        rendered = yaml.safe_load(Environment(undefined=StrictUndefined).from_string(VALUES).render(
            redis_operator_ver='0.26.0', redis_operator_image_ver='0.26.0', redis_namespace='redis',
            redis_operator_replicas=2, redis_acl_secret='', redis_maxmemory_percent=70))
        self.assertIn('AvoidCommandLinePassword', rendered['featureGates'])
        self.assertIsNone(rendered['featureGates']['AvoidCommandLinePassword'])
        self.assertTrue(rendered['featureGates']['GenerateConfigInInitContainer'])

    def test_official_runtime_keeps_config_generation_without_memory_override(self):
        rendered = yaml.safe_load(Environment(undefined=StrictUndefined).from_string(VALUES).render(
            redis_operator_ver="0.27.0", redis_namespace="redis", redis_operator_replicas=2,
            redis_acl_secret="", redis_maxmemory_percent=0))
        self.assertTrue(rendered["featureGates"]["GenerateConfigInInitContainer"])

    def test_sentinel_authentication_and_independent_failure_domains(self):
        replication, sentinel = render("sentinel", redis_password_secret="redis-auth")
        self.assertEqual(sentinel["spec"]["kubernetesConfig"]["redisSecret"],
                         replication["spec"]["kubernetesConfig"]["redisSecret"])
        for doc, role in ((replication, "replication"), (sentinel, "sentinel")):
            spec = doc["spec"]
            self.assertEqual(spec["pdb"], {"enabled": True, "minAvailable": 2})
            spread = spec["topologySpreadConstraints"][0]
            self.assertEqual(spread["labelSelector"]["matchLabels"]["role"], role)
            self.assertEqual(spread["minDomains"], 3)

    def test_cluster_spread_excludes_untolerated_nodes_for_multiple_replicas(self):
        resource, = render('cluster')
        with tarfile.open(CHART) as archive:
            crd = next(doc for doc in yaml.safe_load_all(archive.extractfile('redis-operator/crds/crds.yaml'))
                       if doc['spec']['names']['kind'] == 'RedisCluster')
        schema = next(version for version in crd['spec']['versions']
                      if version['name'] == resource['apiVersion'].split('/')[1])['schema']['openAPIV3Schema']
        properties = schema['properties']['spec']['properties']
        for role in ('redisLeader', 'redisFollower'):
            spread = resource['spec'][role]['topologySpreadConstraints'][0]
            self.assertEqual(spread['nodeTaintsPolicy'], 'Honor')
            self.assertEqual((spread['minDomains'], spread['maxSkew']),
                             (3, 1 if role == 'redisLeader' else 2))
            fields = properties[role]['properties']['topologySpreadConstraints']['items']['properties']
            self.assertIn('nodeTaintsPolicy', fields)
        for sentinel_resource in render('sentinel'):
            self.assertNotIn('nodeTaintsPolicy', sentinel_resource['spec']['topologySpreadConstraints'][0])

    def test_cluster_same_index_peer_anti_affinity_separates_declared_pods(self):
        import itertools
        for shards in (3, 4):
            resource, = render('cluster', redis_cluster_shards=shards)
            for role, peer in (('redisLeader', 'follower'), ('redisFollower', 'leader')):
                term, = resource['spec'][role]['affinity']['podAntiAffinity']['requiredDuringSchedulingIgnoredDuringExecution']
                self.assertEqual(term['topologyKey'], 'kubernetes.io/hostname')
                self.assertEqual(term['matchLabelKeys'], ['apps.kubernetes.io/pod-index'])
                self.assertEqual(term['labelSelector']['matchLabels'], {'app': 'redis-prod-' + peer})
            # This proves declared same-index Pod separation only. Upstream
            # passes a modulo contact seed; redis-cli actually selects the
            # master with least replicas. Live health must verify that graph.
            leaders = tuple(i % 3 for i in range(shards))
            accepted = 0
            for followers in itertools.product(range(3), repeat=shards):
                if any(followers[i] == leaders[i] for i in range(shards)):
                    continue
                accepted += 1
                for i in range(shards):
                    self.assertGreaterEqual(len({leaders[i], followers[i]}), 2)
            self.assertGreater(accepted, 0)

    def test_cluster_spread_can_finish_every_allowed_ordered_replica_prefix(self):
        def candidates(counts, forbidden, skew):
            return [domain for domain in range(3) if domain != forbidden and
                    counts[domain] + 1 - min(counts) <= skew]
        # Observed failure: leader domains C,A,B; followers A,C; follower-2
        # cannot use empty B due to anti-affinity and cannot use A/C at skew=1.
        self.assertEqual(candidates((1, 0, 1), 1, 1), [])
        self.assertEqual(candidates((1, 0, 1), 1, 2), [0, 2])
        for shards in (3, 4):
            resource, = render('cluster', redis_cluster_shards=shards)
            spread = resource['spec']['redisFollower']['topologySpreadConstraints'][0]
            leaders = tuple(i % 3 for i in range(shards))
            prefixes = {(0, 0, 0)}
            for index in range(shards * 2):
                following = set()
                for counts in prefixes:
                    allowed = candidates(counts, leaders[index] if index < shards else None, spread['maxSkew'])
                    self.assertTrue(allowed, (shards, index, counts))
                    for domain in allowed:
                        next_counts = list(counts)
                        next_counts[domain] += 1
                        following.add(tuple(next_counts))
                prefixes = following
            self.assertTrue(all(min(counts) > 0 for counts in prefixes))

    def test_restart_keeps_durability_and_security_configuration(self):
        env = Environment(undefined=StrictUndefined)
        for mode in ("sentinel", "cluster"):
            cm = yaml.safe_load(env.from_string((ROLE / "templates/redis/config.yaml.j2").read_text()).render(
                redis_name="redis-prod", redis_namespace="redis", redis_mode=mode,
                redis_appendfsync="everysec", redis_min_replicas_to_write=1,
                redis_min_replicas_max_lag=10, redis_tls_secret="redis-tls", redis_cluster_replicas=2,
                redis_cluster_image_ver="8.10.2-kubeauto.1"))
            config = cm["data"]["redis-additional.conf"]
            self.assertIn("appendfsync everysec", config)
            self.assertIn("maxmemory-policy noeviction", config)
            self.assertIn("tls-auth-clients yes", config)
            self.assertEqual("cluster-require-full-coverage yes" in config, mode == "cluster")
            for doc in render(mode):
                key = "redisSentinelConfig" if doc["kind"] == "RedisSentinel" else "redisConfig"
                field = "additionalSentinelConfig" if key == "redisSentinelConfig" else "additionalRedisConfig"
                self.assertEqual(doc["spec"][key][field], "redis-prod-config")

    def test_health_rejects_partial_membership_unbound_storage_and_missing_domains(self):
        path = ROLE / "files/redis-health.py"
        spec = importlib.util.spec_from_file_location("redis_health", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        pods = [{"metadata": {"name": f"redis-{i}", "labels": {"role": "replication"}},
                 "spec": {"nodeName": f"node-{i}", "volumes": [
                     {"persistentVolumeClaim": {"claimName": f"data-{i}"}}]},
                 "status": {"phase": "Running", "conditions": [{"type": "Ready", "status": "True"}]}}
                for i in range(3)]
        claims = {f"data-{i}": "Bound" for i in range(3)}
        domains = {f"node-{i}": f"domain-{i}" for i in range(3)}
        self.assertEqual(len(module.check_resources(pods, claims, {"replication": 3}, domains)["replication"]), 3)
        for members, storage, topology in ((pods[:2], claims, domains),
                                          (pods, {"data-0": "Bound"}, domains),
                                          (pods, claims, {f"node-{i}": "same" for i in range(3)})):
            with self.assertRaises(ValueError):
                module.check_resources(members, storage, {"replication": 3}, topology)
        pods[0]['spec']['volumes'] = [{'emptyDir': {}}]
        with self.assertRaisesRegex(ValueError, 'no persistent data claim'):
            module.check_resources(pods, claims, {'replication': 3}, domains)

    def test_cluster_health_validates_real_replica_graph_and_slot_ownership(self):
        spec = importlib.util.spec_from_file_location('redis_health', ROLE / 'files/redis-health.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        pods, rows = [], []
        domains = {f'node-{i}': f'domain-{i}' for i in range(3)}
        for i in range(9):
            ip = f'10.0.0.{i + 1}'
            pods.append({'metadata': {'name': f'pod-{i}'},
                         'spec': {'nodeName': f'node-{i // 3}'}, 'status': {'podIP': ip}})
            primary = i < 3
            parent = '-' if primary else f'{i % 3 + 1:040x}'
            slots = f'{i * 5461}-{(i + 1) * 5461 - 1 if i < 2 else 16383}' if primary else ''
            rows.append(f'{i + 1:040x} {ip}:6379@16379 {"master" if primary else "slave"} '
                        f'{parent} 0 0 1 connected {slots}')
        self.assertEqual(len(module.cluster_members('\n'.join(rows), pods, domains, 3, 2)), 9)
        for invalid in ('\n'.join(rows[:-1]), '\n'.join(rows).replace('0-5460', '0-5461'),
                        '\n'.join(rows).replace('connected', 'disconnected', 1),
                        '\n'.join(rows) + '\n' + rows[0]):
            with self.assertRaises(ValueError):
                module.cluster_members(invalid, pods, domains, 3, 2)
        for pod in pods:
            pod['spec']['nodeName'] = 'node-0'
        with self.assertRaisesRegex(ValueError, 'share a failure domain'):
            module.cluster_members('\n'.join(rows), pods, domains, 3, 2)

    def test_health_accepts_the_upstream_tls_replication_dns_identity(self):
        spec = importlib.util.spec_from_file_location('redis_health', ROLE / 'files/redis-health.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        primary = {'metadata': {'name':'redis-prod-0'}, 'status': {'podIP':'10.0.0.1'},
                   'spec': {'hostname':'redis-prod-0','subdomain':'redis-prod-headless'}}
        addresses = module.primary_addresses(primary,'redis','cluster.local')
        self.assertEqual(addresses, {'redis-prod-0','10.0.0.1',
                                    'redis-prod-0.redis-prod-headless.redis.svc.cluster.local'})
        self.assertNotIn('redis-prod-1.redis-prod-headless.redis.svc.cluster.local',addresses)

    def test_redis_tag_does_not_select_other_addons(self):
        imports = yaml.safe_load((ROLE / "tasks/main.yml").read_text())
        tagged = [task.get("import_tasks") for task in imports if "redis" in task.get("tags", [])]
        self.assertEqual(tagged, ["redis.yml"])

    def test_cr_fields_are_accepted_by_pinned_official_schema(self):
        with tarfile.open(CHART) as archive:
            crds = list(yaml.safe_load_all(archive.extractfile("redis-operator/crds/crds.yaml")))
        schemas = {crd["spec"]["names"]["kind"]: version["schema"]["openAPIV3Schema"]
                   for crd in crds for version in crd["spec"]["versions"]
                   if version["name"] == "v1beta2"}

        def check(value, schema, path):
            if isinstance(value, dict):
                for key, item in value.items():
                    child = schema.get("properties", {}).get(key)
                    if child is None:
                        additional = schema.get("additionalProperties")
                        if additional is True or schema.get("x-kubernetes-preserve-unknown-fields"):
                            continue
                        self.assertIsInstance(additional, dict, f"unknown field {path}.{key}")
                        child = additional
                    check(item, child, f"{path}.{key}")
            elif isinstance(value, list):
                for item in value:
                    check(item, schema.get("items", {}), path + "[]")
        for mode in ("sentinel", "cluster"):
            for options in ({}, {"redis_password_secret": "redis-auth", "redis_tls_secret": "redis-tls",
                                  "redis_acl_secret": "redis-acl"}):
                for doc in render(mode, **options):
                    # Kubernetes ObjectMeta is not expanded in the upstream CRD.
                    check(doc["spec"], schemas[doc["kind"]]["properties"]["spec"], doc["kind"] + ".spec")

    def test_lab_inventory_config_and_credentials_are_isolated(self):
        spec = importlib.util.spec_from_file_location('redis_lab_config', ROOT / 'tests/helpers/redis-lab-config.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        hosts = module.inventory()
        addresses = set(re.findall(r'(?m)^(192\.[0-9.]+)', hosts))
        self.assertEqual(addresses, set(module.NODES))
        self.assertNotIn('192.168.122.', hosts)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'config.yml'
            path.write_text(yaml.safe_dump({'redis_ver': '__redis__'}))
            with self.assertRaises(ValueError):
                module.configure(path, 'sentinel')
            path.write_text(yaml.safe_dump({'redis_ver': '8.10.2', 'ceph_install': 'no',
                                           'unrelated_setting': 'preserve'}))
            module.configure(path, 'cluster')
            data = yaml.safe_load(path.read_text())
            self.assertEqual(data['unrelated_setting'], 'preserve')
            self.assertEqual(data['ceph_install'], 'no')
            self.assertEqual(data['K8S_VER'], '1.33.6')
            self.assertEqual(data['redis_mode'], 'cluster')
            self.assertEqual(data['KUBE_RESERVED_ENABLED'], 'no')
            self.assertEqual(data['SYS_RESERVED_ENABLED'], 'no')
            self.assertEqual(data['redis_namespace'], 'redis-gate')
        credentials = module.credentials()
        auth, acl, tls = [c['stringData'] for c in credentials]
        self.assertIn('#' + hashlib.sha256(auth['password'].encode()).hexdigest(), acl['user.acl'])
        self.assertIn('~kubeauto:redis:*', acl['user.acl'])
        self.assertNotIn(auth['password'], acl['user.acl'])
        self.assertEqual(set(tls), {'ca.crt', 'tls.crt', 'tls.key'})
        self.assertTrue(all(c['metadata']['namespace'] == 'redis-gate' for c in credentials))

    def test_storage_fixture_rejects_foreign_hosts_versions_and_resources_before_mutation(self):
        import copy
        from types import SimpleNamespace
        from unittest.mock import patch
        spec = importlib.util.spec_from_file_location('redis_storage', ROOT / 'tests/helpers/redis-lab-storage.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        nodes = [{'metadata': {'name': f'node-{i}'}, 'status': {
            'addresses': [{'type': 'InternalIP', 'address': f'192.168.47.{i}'}],
            'nodeInfo': {'kubeletVersion': 'v1.33.6'},
            'conditions': [{'type': 'Ready', 'status': 'True'}]}} for i in range(131, 137)]
        self.assertEqual(set(module.node_addresses(nodes).values()), module.NODES)
        for mutate in (lambda n: n[0]['status']['addresses'][0].update(address='192.168.122.1'),
                       lambda n: n[0]['status']['nodeInfo'].update(kubeletVersion='v1.37.1'),
                       lambda n: n.pop(),
                       lambda n: n[0]['status']['conditions'][0].update(status='False')):
            bad = copy.deepcopy(nodes)
            mutate(bad)
            with self.assertRaises(ValueError):
                module.node_addresses(bad)
        with tempfile.TemporaryDirectory() as tmp:
            args = SimpleNamespace(base=Path(tmp), kubeconfig=Path(tmp) / 'config', helm=Path(tmp) / 'helm')
            fixture = module.Fixture(args)

            def get(kind, name=None):
                if kind == 'nodes': return {'items': nodes}
                if name == 'kube-system': return {'metadata': {'uid': 'dedicated-cluster'}}
                return {'metadata': {'name': name, 'labels': {'owner': 'foreign'}}}

            with patch.object(fixture, 'get', side_effect=get), patch.object(fixture, 'run') as run:
                with self.assertRaisesRegex(ValueError, 'pre-existing'):
                    fixture.prepare()
                run.assert_not_called()
            self.assertFalse(fixture.state_file.exists())

    def test_storage_chart_images_use_only_exact_published_pins(self):
        import copy
        spec = importlib.util.spec_from_file_location('redis_storage', ROOT / 'tests/helpers/redis-lab-storage.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        evidence = json.loads((ROOT / 'tests/evidence/redis-storage-images-20261012.json').read_text())
        chart = ROOT / 'tests/fixtures/longhorn-1.10.1.tgz'
        values, images = module.image_plan(chart, evidence)
        self.assertEqual(len(images), 13)
        self.assertEqual(values['global']['imageRegistry'], 'registry.talkschool.cn:5000')
        self.assertFalse(values['persistence']['defaultClass'])
        self.assertEqual(values['defaultSettings']['defaultDataPath'], '/var/lib/kubeauto-redis-longhorn')
        for mutate in (lambda e: e['images'].pop(), lambda e: e.update(durable_rc=1)):
            bad = copy.deepcopy(evidence)
            mutate(bad)
            with self.assertRaises(ValueError):
                module.image_plan(chart, bad)

    def test_install_gate_orders_core_storage_and_product_and_preserves_control_context(self):
        import os
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            helpers = base / 'tests/helpers'
            helpers.mkdir(parents=True)
            (base / '.venv').symlink_to(ROOT / '.venv', target_is_directory=True)
            (helpers / 'redis-lab-config.py').symlink_to(ROOT / 'tests/helpers/redis-lab-config.py')
            (helpers / 'lab-wipe-nodes.sh').write_text('#!/bin/sh\necho LAB_CLEAN_VERIFY_PASS\n')
            (helpers / 'redis-lab-storage.py').write_text('''import yaml,sys
from pathlib import Path
base=Path(sys.argv[sys.argv.index('--base')+1])
assert yaml.safe_load((base/'clusters/redis-gate/config.yml').read_text())['redis_install']=='no'
print('REDIS_STORAGE_LAB_PASS')
''')
            (helpers / 'redis-regression.sh').write_text('''#!/bin/bash
set -eu
"$KUBEAUTO_BASE/.venv/bin/python" - <<'PY'
import os,yaml
from pathlib import Path
data=yaml.safe_load((Path(os.environ['KUBEAUTO_BASE'])/'clusters/redis-gate/config.yml').read_text())
assert data['redis_install']=='yes'
assert data['redis_storage_class']=='redis-longhorn'
PY
echo REDIS_INSTALL_SMOKE_PASS
''')
            kubecli = base / 'kubecli.py'
            kubecli.write_text(f'''#!{ROOT / '.venv/bin/python'}
import os,sys,yaml
from pathlib import Path
base=Path(os.environ['KUBEAUTO_BASE']);config=base/'clusters/redis-gate/config.yml'
with (base/'commands').open('a') as log:log.write(' '.join(sys.argv[1:])+'\\n')
if sys.argv[1]=='new':
 config.parent.mkdir(parents=True);config.write_text(yaml.safe_dump({{'redis_ver':'8.10.2'}}))
elif sys.argv[1]=='setup':
 assert sys.argv[2:]==['redis-gate','90']
 assert yaml.safe_load(config.read_text())['redis_install']=='no'
 (base/'control-config').write_text('temporary product context')
''')
            kubectl = base / 'kubectl'
            kubectl.write_text('''#!/usr/bin/env python3
import json,sys
if 'kube-system' in sys.argv: print(json.dumps({'metadata':{'uid':'redis-dedicated-uid'}}))
''')
            kubecli.chmod(0o755)
            kubectl.chmod(0o755)
            control = base / 'control-config'
            control.write_text('preserved existing cluster context')
            env = {**os.environ, 'PATH': tmp + ':' + os.environ['PATH'], 'KUBEAUTO_BASE': tmp,
                   'REDIS_LAB_LEASE_FILE': str(base / 'lease'), 'REDIS_CONTROL_KUBECONFIG': str(control)}
            gate = ROOT / 'tests/helpers/redis-install-gate.sh'
            result = subprocess.run(['bash', str(gate)], env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(control.read_text(), 'preserved existing cluster context')
            owner_path = base / 'clusters/redis-gate/redis-lab-owner.json'
            owner = json.loads(owner_path.read_text())
            self.assertTrue(owner['healthy'])
            self.assertEqual(owner['cluster_uid'], 'redis-dedicated-uid')
            self.assertIn('REDIS_INSTALL_SCOPED_CLEAN_PASS', result.stdout)
            self.assertIn('REDIS_INSTALL_MODULE_PASS', result.stdout)
            self.assertNotIn('REDIS_DELIVERY_BRANCH_PASS', result.stdout)
            (base / 'commands').unlink()
            # Refusing a second installation must preserve the validated baseline.
            result = subprocess.run(['bash', str(gate)], env=env, capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse((base / 'commands').exists())
            self.assertEqual(json.loads(owner_path.read_text()), owner)
            self.assertEqual(control.read_text(), 'preserved existing cluster context')

    def test_mode_and_image_contracts_are_explicit(self):
        self.assertIn("redis_mode in ['sentinel', 'cluster']", TASKS)
        self.assertIn("watchNamespace: \"{{ redis_namespace }}\"", VALUES)
        for image in self.kc.component_images["redis"]:
            self.assertTrue(image.startswith("brinnatt/"))

    def test_matrix_tracks_all_acceptance_items(self):
        matrix = yaml.safe_load(MATRIX.read_text(encoding="utf-8"))
        self.assertEqual(len(matrix["cases"]), 24)
        self.assertEqual({c['id'] for c in matrix['cases']},
                         {f'REDIS-{n:02d}' for n in range(1, 25)})

    def test_matrix_validator_rejects_pending_evidence_and_accepts_current_matrix(self):
        spec = importlib.util.spec_from_file_location("redis_matrix_validator", ROOT / "tests/helpers/validate_redis_matrix.py")
        module = importlib.util.module_from_spec(spec)
        assert spec and spec.loader
        spec.loader.exec_module(module)
        self.assertEqual(module.validate(MATRIX), [])
        bad = {"meta": {"result": "PENDING"}, "cases": [{"id": "X", "group": "g", "desc": "d", "status": "pending", "evidence": "bad"}]}
        with tempfile.NamedTemporaryFile("w", suffix=".yaml") as temp:
            temp.write(yaml.safe_dump(bad))
            temp.flush()
            self.assertTrue(module.validate(Path(temp.name)))

    def test_matrix_supports_completed_modules_without_premature_delivery_pass(self):
        spec = importlib.util.spec_from_file_location("redis_validator", ROOT / "tests/helpers/validate_redis_matrix.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        cases = [{"id": "A", "group": "g", "desc": "d", "status": "pass", "evidence": "run-1 rc=0 scoped-clean"},
                 {"id": "B", "group": "g", "desc": "d", "status": "pending", "evidence": ""}]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "matrix.yaml"
            path.write_text(yaml.safe_dump({"meta": {"result": "PENDING"}, "cases": cases}))
            self.assertEqual(module.validate(path), [])
            path.write_text(yaml.safe_dump({"meta": {"result": "PASS"}, "cases": cases}))
            self.assertTrue(module.validate(path))

    def test_cleanup_rbac_verification_is_scoped_to_exact_helm_release_namespace(self):
        import os
        with tempfile.TemporaryDirectory() as tmp:
            kubectl = Path(tmp) / 'kubectl'
            kubectl.write_text("#!/usr/bin/env python3\nimport json,os,sys\nargs=sys.argv[1:]\nif any('clusterrole' in arg for arg in args):\n row={'metadata':{'name':'redis-operator', 'annotations':{\n  'meta.helm.sh/release-name':'redis-operator',\n  'meta.helm.sh/release-namespace':os.environ['REDIS_TEST_RBAC_NAMESPACE']}}}\n print(json.dumps({'items':[row]}) if 'json' in args else 'clusterrole/redis-operator')\n")
            kubectl.chmod(0o755)
            env = {**os.environ, 'PATH':tmp+':'+os.environ['PATH'],
                   'REDIS_NAMESPACE':'redis-clean-fixture', 'REDIS_OPERATOR_NAMESPACE':'redis-clean-operator',
                   'REDIS_CRD_OWNERSHIP_FILE':str(Path(tmp)/'absent-journal')}
            for namespace, expected in [('redis-gate-operator', 0), ('redis-clean-operator', 1)]:
                result = subprocess.run(['bash', str(CLEANUP), '--verify'], capture_output=True, text=True,
                                        env={**env, 'REDIS_TEST_RBAC_NAMESPACE':namespace})
                self.assertEqual(result.returncode == 0, expected == 0, result.stdout+result.stderr)
                self.assertEqual('REDIS_CLEAN_VERIFY_PASS' in result.stdout, expected == 0)

    def test_cleanup_does_not_treat_api_failure_as_clean(self):
        import os
        with tempfile.TemporaryDirectory() as tmp:
            kubectl = Path(tmp) / "kubectl"
            kubectl.write_text("#!/bin/sh\nexit 42\n")
            kubectl.chmod(0o755)
            result = subprocess.run(["bash", str(CLEANUP), "--verify"], capture_output=True, text=True,
                                    env={**os.environ, "PATH": tmp + ":" + os.environ["PATH"]})
            self.assertNotEqual(result.returncode, 0)
            self.assertNotIn("REDIS_CLEAN_VERIFY_PASS", result.stdout)

    def test_cleanup_accepts_exact_pinned_crd_names_and_partial_helm_install(self):
        import os
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            state = directory / 'operator-namespace'
            state.touch()
            journal = directory / 'crds'
            with tarfile.open(CHART) as archive:
                journal.write_text(''.join(c['metadata']['name'] + '\n' for c in
                                          yaml.safe_load_all(archive.extractfile('redis-operator/crds/crds.yaml'))))
            kubectl = directory / 'kubectl'
            kubectl.write_text('''#!/usr/bin/env python3
import os,sys
from pathlib import Path
args=sys.argv[1:]
state=Path(os.environ['REDIS_TEST_NS_STATE'])
if 'namespace' in args and 'redis-operator' in args and 'get' in args and state.exists():
 print('redis:kubeauto' if any('jsonpath=' in a for a in args) else 'namespace/redis-operator')
elif any('clusterrole' in arg for arg in args):
 print('{"items": []}')
elif 'delete' in args and 'namespace' in args:
 state.unlink()
''')
            helm = directory / 'helm'
            helm.write_text('#!/bin/sh\n[ "$1" = list ] || exit 88\nprintf "[]\\n"\n')
            kubectl.chmod(0o755)
            helm.chmod(0o755)
            env = {**os.environ, 'PATH': tmp + ':' + os.environ['PATH'],
                   'REDIS_HELM': str(helm), 'REDIS_CRD_OWNERSHIP_FILE': str(journal),
                   'REDIS_TEST_NS_STATE': str(state)}
            result = subprocess.run(['bash', str(CLEANUP)], env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn('REDIS_CLEAN_VERIFY_PASS', result.stdout)
            self.assertFalse(journal.exists())
            self.assertFalse(state.exists())
            # A malformed later journal entry must stop before any deletion.
            state.touch()
            journal.write_text('redis.redis.redis.opstreelabs.in\nforeign.example.org\n')
            result = subprocess.run(['bash', str(CLEANUP)], env=env, capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertTrue(state.exists())
            self.assertNotIn('REDIS_CLEAN_VERIFY_PASS', result.stdout)

    def test_artifact_identity_requires_both_architectures(self):
        spec = importlib.util.spec_from_file_location("redis_artifacts", ROOT / "tests/helpers/redis-artifacts.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        rows = [{"platform": {"os": "linux", "architecture": arch}, "digest": "sha256:" + arch}
                for arch in ("amd64", "arm64")]
        raw = json.dumps({"manifests": rows}).encode()
        digest, platforms = module.identity(raw)
        self.assertEqual(digest, "sha256:" + hashlib.sha256(raw).hexdigest())
        self.assertEqual(set(platforms), {"amd64", "arm64"})
        with self.assertRaises(ValueError):
            module.identity(json.dumps({"manifests": rows[:1]}).encode())

    def test_actions_evidence_requires_full_coverage_and_current_source_identity(self):
        import copy
        from unittest.mock import patch
        spec = importlib.util.spec_from_file_location('redis_artifacts', ROOT / 'tests/helpers/redis-artifacts.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        evidence = json.loads((ROOT / 'tests/evidence/redis-images-20261012.json').read_text())
        published = module.publication_records(evidence)
        record = published['brinnatt/redis:8.10.2']
        platforms = {key.split('/')[1]: value for key, value in record['platforms'].items()}
        from contextlib import redirect_stdout
        import io
        with redirect_stdout(io.StringIO()), patch.object(module, 'inspect', return_value=(record['digest'], platforms)) as readback:
            self.assertTrue(module.verify(('redis', '8.10.2'), published))
            readback.assert_called_once_with('hub.talkedu.cn/kubeauto/redis:8.10.2')
        with redirect_stdout(io.StringIO()), patch.object(module, 'inspect', return_value=('sha256:' + '0' * 64, platforms)):
            self.assertFalse(module.verify(('redis', '8.10.2'), published))
        for mutate in (lambda e: e.update(durable_rc=1),
                       lambda e: e['images'].pop(),
                       lambda e: e['images'].append(e['images'][0]),
                       lambda e: e['images'][0]['platforms'].pop('linux/arm64'),
                       lambda e: e['images'][0].update(talkedu_ref='foreign/redis:8.10.2')):
            bad = copy.deepcopy(evidence)
            mutate(bad)
            with self.assertRaises(ValueError):
                module.publication_records(bad)

    def test_operator_patch_publication_keeps_its_own_run_and_commit_identity(self):
        import copy
        spec = importlib.util.spec_from_file_location('redis_artifacts_protected', ROOT / 'tests/helpers/redis-artifacts.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        original = json.loads((ROOT / 'tests/evidence/redis-images-20261012.json').read_text())
        with self.assertRaises(ValueError):
            module.publication_records(original, module.PROTECTED_IMAGES)
        protected = copy.deepcopy(original)
        protected.update(head='a' * 40, run_id=1,
                         run_url='https://github.com/brinnatt/kubeauto-ext-images-dockerfile/actions/runs/1')
        record = next(record for record in protected['images'] if record['key'] == 'brinnatt/redis-operator:0.27.0')
        record.update(key='brinnatt/redis-operator:0.27.0-kubeauto.2', head=protected['head'],
                      dockerhub_ref='brinnatt/redis-operator:0.27.0-kubeauto.2',
                      talkedu_ref='hub.talkedu.cn/kubeauto/redis-operator:0.27.0-kubeauto.2')
        protected['images'] = [record]
        self.assertEqual(set(module.publication_records(protected, module.PROTECTED_IMAGES)), {record['key']})
        record['head'] = original['head']
        with self.assertRaises(ValueError):
            module.publication_records(protected, module.PROTECTED_IMAGES)

    def test_snapshot_cli_accepts_the_product_cluster_image_before_directory_preflight(self):
        spec = importlib.util.spec_from_file_location('redis_snapshot_cli', ROLE / 'files/redis-snapshot.py')
        module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
        for image, expected in (
                ('registry.talkschool.cn:5000/brinnatt/redis:8.10.2-kubeauto.1', 'new absolute request directory'),
                ('registry.talkschool.cn:5000/brinnatt/redis:8.10.2', 'new absolute request directory'),
                ('registry.talkschool.cn:5000/brinnatt/redis:8.10.2-kubeauto.0', 'pinned product Redis image'),
                ('foreign/redis:8.10.2-kubeauto.1', 'pinned product Redis image')):
            argv = ['snapshot', '--kubeconfig', '/unused', '--namespace', 'redis', '--name', 'redis-prod',
                    '--mode', 'cluster', '--image', image, '--password-secret', 'auth', '--tls-secret', 'tls',
                    '--directory', 'relative']
            with self.subTest(image=image), patch.object(module.sys, 'argv', argv), \
                    patch.object(module.sys, 'stderr', new_callable=io.StringIO) as stderr, \
                    patch.object(module.subprocess, 'run') as run:
                with self.assertRaises(SystemExit) as exc:
                    module.main()
                self.assertEqual(exc.exception.code, 2)
                self.assertIn(expected, stderr.getvalue())
                run.assert_not_called()

    def test_snapshot_transfer_publishes_only_complete_verified_artifacts(self):
        spec = importlib.util.spec_from_file_location("redis_snapshot", ROLE / "files/redis-snapshot.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.assertEqual(module.seed_service('redis-prod', 'sentinel'), 'redis-prod-master')
        self.assertEqual(module.seed_service('redis-prod', 'cluster'), 'redis-prod-leader')
        payload = b"REDIS0012" + b"controlled-test-payload" * 100
        digest = hashlib.sha256(payload).hexdigest()
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            with self.assertRaises(ValueError):
                module.publish_file(directory, "bad.rdb", lambda out: out.write(payload), "wrong")
            self.assertEqual(list(directory.iterdir()), [])

            def interrupted(out):
                out.write(payload[:10])
                raise ValueError("transfer interrupted")

            with self.assertRaises(ValueError):
                module.publish_file(directory, "interrupted.rdb", interrupted, digest)
            self.assertEqual(list(directory.iterdir()), [])
            record = module.publish_file(directory, "snapshot.rdb", lambda out: out.write(payload), digest)
            self.assertEqual(record["sha256"], digest)
            self.assertEqual((directory / "snapshot.rdb").read_bytes(), payload)
            with self.assertRaises(ValueError):
                module.publish_file(directory, "snapshot.rdb", lambda out: out.write(payload), digest)

    def test_product_cli_auth_matches_upstream_crlf_normalization_without_argv_secret(self):
        import os
        with tempfile.TemporaryDirectory() as tmp:
            cli = Path(tmp) / 'redis-cli'
            cli.write_text('''#!/usr/bin/env python3
import os,sys
assert os.environ['REDISCLI_AUTH']=='test-password'
assert sys.argv[1:]==['PING']
print('PONG')
''')
            cli.chmod(0o755)
            for name in ('redis-health.py', 'redis-snapshot.py'):
                spec = importlib.util.spec_from_file_location(name, ROLE / 'files' / name)
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                argv = [*module.AUTH_CLI_PREFIX, 'PING']
                self.assertNotIn('test-password', ' '.join(argv))
                result = subprocess.run(argv, capture_output=True, text=True, env={**os.environ,
                    'PATH': tmp + ':' + os.environ['PATH'], 'REDISCLI_AUTH': 'test-password\r\n'})
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout.strip(), 'PONG')

    def test_product_cli_auth_accepts_legacy_agent_environment_and_prefers_current(self):
        import os
        with tempfile.TemporaryDirectory() as tmp:
            cli = Path(tmp) / 'redis-cli'
            cli.write_text('''#!/usr/bin/env python3
import os,sys
assert os.environ['REDISCLI_AUTH']=='test-password'
assert sys.argv[1:]==['PING']
print('PONG')
''')
            cli.chmod(0o755)
            for name in ('redis-health.py', 'redis-snapshot.py'):
                spec = importlib.util.spec_from_file_location(name, ROLE / 'files' / name)
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                for auth in ({'REDIS_PASSWORD': 'test-password\r\n'},
                             {'REDISCLI_AUTH': '', 'REDIS_PASSWORD': 'test-password\r\n'},
                             {'REDISCLI_AUTH': 'test-password\r\n', 'REDIS_PASSWORD': 'wrong-password'}):
                    with self.subTest(helper=name, keys=sorted(auth)):
                        environ = {k:v for k,v in os.environ.items() if k not in ('REDISCLI_AUTH', 'REDIS_PASSWORD')}
                        environ.update(auth, PATH=tmp + ':' + os.environ['PATH'])
                        result = subprocess.run([*module.AUTH_CLI_PREFIX, 'PING'],
                                                capture_output=True, text=True, env=environ)
                        self.assertEqual(result.returncode, 0, result.stderr)
                        self.assertEqual(result.stdout.strip(), 'PONG')

    def test_runner_and_cleanup_have_product_and_ownership_contracts(self):
        runner = RUNNER.read_text(encoding="utf-8")
        cleanup = CLEANUP.read_text(encoding="utf-8")
        for phrase in ("kubecli download -E redis", "kubecli setup", "REDIS_BUSINESS_DATA_PASS", "KUBEAUTO_CLUSTER",
                       "REDIS_DATA_DIGEST_PASS", "REDIS_SENTINEL_DISCOVERY_PASS", "REDIS_CLUSTER_SLOT_PASS",
                       "secretKeyRef", "redis-cli"):
            self.assertIn(phrase, runner)
        self.assertIn("namespace_owned", cleanup)
        self.assertIn("refusing foreign Redis namespace", cleanup)

    def test_runner_uses_operator_service_names(self):
        runner = RUNNER.read_text(encoding="utf-8")
        self.assertIn('service="$REDIS_NAME-master"', runner)
        self.assertIn('service="$REDIS_NAME-sentinel-sentinel"', runner)
        self.assertNotIn('service="$REDIS_NAME"', runner)
        self.assertIn('REDIS_NS="${REDIS_NAMESPACE:-$(config_value redis_namespace)}"', runner)
        self.assertIn('REDIS_NAME="${REDIS_NAME:-$(config_value redis_name)}"', runner)

    def test_runner_reads_quoted_product_config_and_preserves_name(self):
        import os
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.yml"
            data = {"redis_install": "yes", "redis_mode": "sentinel", "redis_namespace": "custom-redis",
                    "redis_operator_namespace": "custom-operator", "redis_name": "custom-data", "redis_ver": "8.10.2"}
            path.write_text(yaml.safe_dump(data))
            env = {**os.environ, "KUBEAUTO_CONFIG": str(path)}
            # The shell entry point must accept quoted scalars, without touching a cluster.
            result = subprocess.run(["bash", str(RUNNER), "--config-check"], env=env,
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("name=custom-data", result.stdout)
            self.assertIn("namespace=custom-redis", result.stdout)
            data["redis_ver"] = "__redis__"
            path.write_text(yaml.safe_dump(data))
            result = subprocess.run(["bash", str(RUNNER), "--config-check"], env=env,
                                    capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertNotIn("REDIS_CONFIG_PASS", result.stdout)

    def test_basic_smoke_cannot_emit_a_ga_delivery_marker(self):
        with pending_qualification_fixture() as (env, calls):
            result = subprocess.run(["bash", str(RUNNER), "--full"], env=env,
                                    capture_output=True, text=True, timeout=15)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('focused-qualification-pending cases=REDIS-01', result.stderr)
            self.assertNotIn("REDIS_DELIVERY_BRANCH_PASS", result.stdout)
            self.assertFalse(calls.exists(), result.stdout + result.stderr)

    def test_full_launcher_rejects_pending_qualification_before_remote_calls(self):
        with pending_qualification_fixture() as (env, calls):
            result = subprocess.run(
                ['bash', str(ROOT / 'tests/run_enterprise_regression.sh'), '--redis-only'],
                capture_output=True, text=True, env=env, timeout=15)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('focused-qualification-pending cases=REDIS-01', result.stderr)
            self.assertFalse(calls.exists(), result.stdout + result.stderr)

    def test_redis_launchers_reject_foreign_control_host_before_ssh(self):
        import os
        with tempfile.TemporaryDirectory() as tmp:
            calls = Path(tmp) / 'calls'
            ssh = Path(tmp) / 'ssh'
            ssh.write_text('#!/bin/sh\nprintf called >> "$REDIS_TEST_REMOTE_CALLS"\nexit 99\n')
            ssh.chmod(0o755)
            for mode in ('--redis-probe', '--redis-artifacts', '--redis-storage-artifacts', '--redis-install-only'):
                result = subprocess.run(['bash', str(ROOT / 'tests/run_enterprise_regression.sh'), mode],
                    capture_output=True, text=True, env={**os.environ, 'PATH': tmp + ':' + os.environ['PATH'],
                        'REDIS_TEST_HOST': 'root@192.168.122.1', 'REDIS_TEST_REMOTE_CALLS': str(calls)})
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(calls.exists(), result.stdout + result.stderr)

    def test_install_supervision_dispatch_reaches_defined_functions_without_relaunch(self):
        import os
        with tempfile.TemporaryDirectory() as tmp:
            calls = Path(tmp) / 'calls'
            ssh = Path(tmp) / 'ssh'
            ssh.write_text('''#!/bin/sh
printf '%s\\n' "$*" >> "$REDIS_TEST_REMOTE_CALLS"
printf '%s\\n' "$REDIS_TEST_JOB_STATE"
''')
            ssh.chmod(0o755)
            env = {**os.environ, 'PATH': tmp + ':' + os.environ['PATH'],
                   'REDIS_TEST_HOST': 'root@192.168.47.130', 'REDIS_TEST_REMOTE_CALLS': str(calls),
                   'REDIS_TEST_JOB_STATE': 'state=exited pid=123 rc=0 bytes=100 pass_markers=1 failure_markers=0 latest=pass'}
            result = subprocess.run(['bash', str(ROOT / 'tests/run_enterprise_regression.sh'), '--redis-install-watch'],
                                    capture_output=True, text=True, env=env, timeout=20)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn('supervised job redis rc=0', result.stdout)
            self.assertNotIn('command not found', result.stderr)
            self.assertNotIn('nohup', calls.read_text())
            self.assertNotIn('rsync', calls.read_text())
            env['REDIS_TEST_JOB_STATE'] = 'state=lost pid=123 rc=missing bytes=0 pass_markers=0 failure_markers=0 latest=missing'
            result = subprocess.run(['bash', str(ROOT / 'tests/run_enterprise_regression.sh'), '--redis-install-watch'],
                                    capture_output=True, text=True, env=env, timeout=20)
            self.assertNotEqual(result.returncode, 0)
            self.assertNotIn('[PASS] supervised job', result.stdout)
        dispatcher = (ROOT / 'tests/run_enterprise_regression.sh').read_text()
        self.assertLess(dispatcher.index('monitor_remote_job() {'),
                        dispatcher.index('if [[ "$MODE" == "--redis-install-only"'))

    def test_core_repair_requires_exact_healthy_dependency_boundary(self):
        import copy
        spec = importlib.util.spec_from_file_location('redis_lab_config', ROOT / 'tests/helpers/redis-lab-config.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        owner = {'cluster': 'redis-gate', 'nodes': list(module.NODES), 'cluster_uid': 'owned-core'}
        namespaces = [{'metadata': {'name': name, 'uid': 'owned-core' if name == 'kube-system' else name}}
                      for name in ('default', 'kube-system', 'kube-public', 'kube-node-lease')]
        nodes = [{'metadata': {'name': f'redis-node-{i}'}, 'status': {
            'addresses': [{'type': 'InternalIP', 'address': f'192.168.47.{i}'}],
            'nodeInfo': {'kubeletVersion': 'v1.33.6'},
            'conditions': [{'type': 'Ready', 'status': 'True'}]}} for i in (131, 132, 134, 135, 136)]
        self.assertEqual(module.verify_core_baseline(nodes, namespaces, owner, True), 'owned-core')
        owned_storage = {'metadata': {'name': 'redis-lab-storage', 'labels': {
            'kubeauto.io/component': 'redis-test-storage', 'kubeauto.io/managed-by': 'kubeauto'}}}
        complete = nodes + [{'metadata': {'name': 'redis-node-133'}, 'status': {
            'addresses': [{'type': 'InternalIP', 'address': '192.168.47.133'}],
            'nodeInfo': {'kubeletVersion': 'v1.33.6'},
            'conditions': [{'type': 'Ready', 'status': 'True'}]}}]
        self.assertEqual(module.verify_core_baseline(complete, namespaces + [owned_storage], owner, True), 'owned-core')
        self.assertEqual(module.verify_core_baseline(complete, namespaces + [owned_storage], owner, False), 'owned-core')
        with self.assertRaises(ValueError):
            module.verify_core_baseline(complete, namespaces + [{'metadata': {'name': 'redis-lab-storage'}}], owner, True)
        with self.assertRaises(ValueError):
            module.verify_core_baseline(nodes, namespaces, owner, False)
        for mutate in (lambda n: n[0]['metadata'].update(name='foreign-node'),
                       lambda n: n[0]['status']['conditions'][0].update(status='False'),
                       lambda n: n.pop()):
            bad = copy.deepcopy(nodes)
            mutate(bad)
            with self.assertRaises(ValueError):
                module.verify_core_baseline(bad, namespaces, owner, True)
        with self.assertRaises(ValueError):
            module.verify_core_baseline(nodes, namespaces + [{'metadata': {'name': 'foreign-business'}}], owner, True)
        with self.assertRaises(ValueError):
            module.verify_core_baseline(nodes, namespaces, {**owner, 'cluster_uid': 'another-cluster'}, True)

    def test_storage_reuse_requires_identity_health_and_ownership_without_mutation(self):
        from types import SimpleNamespace
        from unittest.mock import patch
        from contextlib import redirect_stdout
        import io
        spec = importlib.util.spec_from_file_location('redis_storage', ROOT / 'tests/helpers/redis-lab-storage.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        nodes = [{'metadata': {'name': f'redis-node-{i}'}, 'status': {
            'addresses': [{'type': 'InternalIP', 'address': f'192.168.47.{i}'}],
            'nodeInfo': {'kubeletVersion': 'v1.33.6'},
            'conditions': [{'type': 'Ready', 'status': 'True'}]}} for i in range(131,137)]
        with tempfile.TemporaryDirectory() as tmp:
            args = SimpleNamespace(base=Path(tmp), kubeconfig=Path(tmp)/'config', helm=Path(tmp)/'helm')
            fixture = module.Fixture(args)
            module.write_state(fixture.state_file, {'cluster_uid': 'owned-uid', 'namespace': module.NAMESPACE,
                                                     'nodes': module.node_addresses(nodes)})
            def get(kind, name=None):
                if kind == 'nodes': return {'items': nodes}
                if name == 'kube-system': return {'metadata': {'uid': 'owned-uid'}}
                obj = {'metadata': {'labels': module.LABELS}}
                if kind == 'storageclass':
                    obj.update(provisioner='driver.longhorn.io', allowVolumeExpansion=True,
                               volumeBindingMode='WaitForFirstConsumer')
                return obj
            def command(*argv):
                if 'daemonset' in argv:
                    return json.dumps({'status': {'desiredNumberScheduled':3, 'numberReady':3}})
                if 'deployment' in argv:
                    return json.dumps({'spec': {'replicas':1}, 'status': {'readyReplicas':1}})
                if 'nodes.longhorn.io' in argv:
                    return json.dumps({'items':[{'status': {'conditions': [{'type':'Ready','status':'True'}]}}]*3})
                if 'pods' in argv:
                    return json.dumps({'items': [{'status': {'phase':'Running','conditions':[{'type':'Ready','status':'True'}]}}]})
                raise AssertionError('unexpected command/mutation: '+str(argv))
            with patch.object(fixture,'get',side_effect=get), patch.object(fixture,'k',side_effect=command), redirect_stdout(io.StringIO()):
                fixture.health()
                state = json.loads(fixture.state_file.read_text())
                state['cluster_uid']='foreign-uid'
                module.write_state(fixture.state_file,state)
                with self.assertRaisesRegex(ValueError,'identity changed'):
                    fixture.health()

    def test_matrix_malformed_entries_are_failures_without_traceback(self):
        spec = importlib.util.spec_from_file_location(
            'redis_validator', ROOT / 'tests/helpers/validate_redis_matrix.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'matrix.yaml'
            for meta in ('invalid', None, {'result': 'PASS'}):
                path.write_text(yaml.safe_dump({'meta': meta, 'cases': ['invalid']}))
                self.assertTrue(module.validate(path, require_pass=True))

    def test_mode_switch_is_rejected_before_apply(self):
        self.assertIn("拒绝 Redis 模式切换造成的遗留资源", TASKS)
        self.assertIn("get rediscluster {{ redis_name | quote }}", TASKS)
        self.assertIn("get redissentinel {{ (redis_name ~ '-sentinel') | quote }}", TASKS)

    def test_ext_images_catalog_has_redis_entries(self):
        catalog = json.loads((EXT / "catalog.json").read_text(encoding="utf-8"))
        pins = {(row["image"], row["tag"]) for row in catalog}
        self.assertIn(("brinnatt/redis", "8.10.2"), pins)
        self.assertIn(("brinnatt/redis-operator", "0.27.0"), pins)
        self.assertIn(("brinnatt/redis-operator", "0.27.0-kubeauto.2"), pins)
        self.assertIn(("brinnatt/redis-exporter", "v1.93.0"), pins)

    def test_protected_cluster_lifecycle_is_isolated_from_sentinel(self):
        cluster = render('cluster')[0]
        self.assertEqual(cluster['metadata']['annotations']['kubeauto.io/protected-cluster-lifecycle'], 'v1')
        for resource in render('sentinel'):
            self.assertNotIn('kubeauto.io/protected-cluster-lifecycle', resource['metadata'].get('annotations', {}))

    def test_chart_and_operator_image_versions_are_independent(self):
        template = Environment(undefined=StrictUndefined).from_string(VALUES)
        values = dict(redis_operator_ver='0.27.0', redis_namespace='redis', redis_operator_replicas=2,
                      redis_acl_secret='', redis_maxmemory_percent=0)
        rendered = yaml.safe_load(template.render(**values))
        self.assertEqual(rendered['redisOperator']['imageTag'], self.kc.v_redis_operator_image)
        self.assertEqual(rendered['redisOperator']['initContainerImageTag'], self.kc.v_redis_operator_image)
        override = yaml.safe_load(template.render(**values, redis_operator_image_ver='0.26.0'))
        self.assertEqual(override['redisOperator']['imageTag'], '0.26.0')
        self.assertIn('redis-operator-{{ redis_operator_ver }}.tgz', TASKS)


if __name__ == "__main__":
    unittest.main()
