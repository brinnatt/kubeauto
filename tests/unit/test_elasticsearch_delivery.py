"""Guard the actual ES declarations, ECK handoff and irreversible boundaries."""
import copy
from contextlib import redirect_stdout
import importlib.util
from io import StringIO
import json
from pathlib import Path
import tempfile
import unittest
import sys
from unittest.mock import patch

from jsonschema import Draft7Validator
import yaml

from common.constants import KubeConstant


ROOT = Path(__file__).resolve().parents[2]
FILES = ROOT / "roles/cluster-addon/files"


def load(name):
    spec = importlib.util.spec_from_file_location(name, FILES / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


es, eck = load("elasticsearch"), load("eck")


def configuration(topology="standard"):
    c = {k: v for k, v in yaml.safe_load((ROOT / "conf/config.yml").read_text()).items() if k.startswith("elasticsearch_")}
    c.update(elasticsearch_ver="9.5.6", elasticsearch_storage_class="longhorn", elasticsearch_topology=topology,
             elasticsearch_data_nodes=["data-a", "data-b", "data-c"],
             elasticsearch_master_nodes=["master-a", "master-b", "master-c"] if topology == "standard" else [],
             elasticsearch_exporter_secret="search-metrics")
    return c


class ECKAPI:
    def __init__(self, existing=True):
        self.applied = []
        self.source = eck.manifests()
        self.operator = copy.deepcopy(next(o for o in self.source["operator"] if o["kind"] == "StatefulSet")) if existing else None
        self.config = copy.deepcopy(next(o for o in self.source["operator"] if o["kind"] == "ConfigMap"))
        self.resources = []
        self.changes = {}
        if self.operator:
            self.operator["metadata"].update(uid="owned-controller", managedFields=[{"manager": "kubeauto-eck-operator"}])
            self.operator["spec"]["template"]["spec"]["containers"][0]["image"] = "registry.talkschool.cn:5000/brinnatt/eck-operator:3.5.0"

    def get(self, kind, name="", namespace=""):
        if kind == "deployments,statefulsets":
            return {"items": [self.operator] if self.operator else []}
        if kind == "statefulset":
            return self.operator
        if kind == "namespace":
            return {"metadata": {"labels": eck.LABELS}} if self.operator else None
        if kind == "configmap":
            return self.config if name == "elastic-operator" else self.changes.get(name)
        if kind == "crd":
            objects = copy.deepcopy(self.source["crds"])
            for obj in objects:
                obj["metadata"]["managedFields"] = [{"manager": "kubeauto-eck-crd"}]
            return {"items": objects}
        if kind.endswith(".k8s.elastic.co"):
            return {"items": self.resources if kind.startswith("elasticsearches.") else []}
        if kind in ("elasticsearch", "kibana"):
            return next((obj for obj in self.resources if obj['metadata']['name'] == name and
                         obj['metadata']['namespace'] == namespace and obj['kind'].lower() == kind), None)
        raise AssertionError(kind)

    def apply(self, objects, manager):
        self.applied.append((objects, manager))

    def run(self, *args, **kwargs):
        return ""


class ECKUpgradeAPI(ECKAPI):
    def __init__(self):
        super().__init__()
        self.target = self.operator['spec']['template']['spec']['containers'][0]['image']
        self.operator['spec']['template']['spec']['containers'][0]['image'] = self.target.replace('3.5.0', '3.4.1')
        self.resources = [{'kind': 'Elasticsearch', 'metadata': {'name': 'orders', 'namespace': 'search', 'uid': 'consumer-uid',
            'labels': dict(es.LABELS), 'generation': 1}, 'spec': {'version': '9.5.5', 'nodeSets': [{'count': 3}]},
            'status': {'health': 'green', 'phase': 'Ready', 'availableNodes': 3, 'observedGeneration': 1}}]
        self.failure = ''
        self.pod = {'metadata': {'name': 'elastic-operator-0', 'uid': 'target-pod',
                                'ownerReferences': [{'uid': 'owned-controller'}]},
                    'spec': {'containers': [{'name': 'manager', 'image': self.target}]},
                    'status': {'containerStatuses': [{'name': 'manager', 'ready': True,
                                                      'containerID': 'containerd://target', 'restartCount': 0}]}}
        self.logs = '\n'.join(json.dumps({'service.version': '3.5.0+386c7b14',
            'log.logger': 'elasticsearch-controller', 'namespace': 'search', 'es_name': 'orders',
            'iteration': '1', 'message': message}) for message in
            ('Starting reconciliation run', 'Ending reconciliation run'))

    def get(self, kind, name='', namespace=''):
        if kind == 'pod': return copy.deepcopy(self.pod)
        return super().get(kind, name, namespace)

    def run(self, *args, **kwargs):
        if 'logs' in args: return self.logs
        return super().run(*args, **kwargs)

    def apply(self, objects, manager):
        super().apply(copy.deepcopy(objects), manager)
        for obj in objects:
            if obj['kind'] == 'ConfigMap' and obj['metadata']['name'].startswith('kubeauto-eck-change-'):
                self.changes[obj['metadata']['name']] = copy.deepcopy(obj)
            elif obj['kind'] == 'StatefulSet':
                self.operator['spec'] = copy.deepcopy(obj['spec'])
                if self.failure == 'operator': raise ValueError('interrupted after target Operator apply')
        if manager == 'kubeauto-eck-crd' and self.failure == 'crds': raise ValueError('interrupted after CRD apply')


class AtomicNodeSetContracts(unittest.TestCase):
    def test_eck_atomic_list_updates_validate_first_preserve_identity_and_refuse_foreign_or_stale_inputs(self):
        for defect in ('none', 'dry-run', 'foreign-owner', 'foreign-other-owner', 'foreign-resource', 'ssa-validation', 'patch-validation', 'stale'):
            with self.subTest(defect=defect), tempfile.TemporaryDirectory() as temporary:
                c = configuration() | {'elasticsearch_data_nodes':['a','b','c','d'], 'elasticsearch_data_count':4,
                                       'elasticsearch_s3_ca_configmap':'private-ca'}
                desired = {'apiVersion':'v1','kind':'List','items':es.resources(c, 'ca-hash')}
                live = copy.deepcopy(next(obj for obj in desired['items'] if obj['kind']=='Elasticsearch'))
                live['spec']['nodeSets'][-1]['count'] = 3
                live['spec']['version'] = '9.5.5'
                live['spec']['monitoring'] = {'logs':{}}
                live['metadata'].update(uid='original-cr', resourceVersion='7', annotations={'eck-hint':'preserved'},
                    managedFields=[{'manager':'foreign' if defect=='foreign-owner' else 'elastic-operator',
                                    'operation':'Update','fieldsV1':{'f:spec':{'f:nodeSets':{}}}}])
                live['spec']['secureSettings'] = [{'secretName':'previous-secret'}]
                live['metadata']['managedFields'].append({'manager':'kubeauto-elasticsearch','operation':'Apply',
                    'fieldsV1':{'f:spec':{'f:secureSettings':{}}}})
                if defect=='foreign-other-owner':
                    live['metadata']['managedFields'].append({'manager':'foreign','fieldsV1':{'f:spec':{'f:version':{}}}})
                if defect=='foreign-resource':live['metadata']['labels']['kubeauto.io/component']='other'
                before = copy.deepcopy(live)
                events = []
                class Client:
                    def run(inner, *args, body=None):
                        self.assertNotIn('--force-conflicts', args)
                        if 'get' in args:
                            self.assertIn('--show-managed-fields',args)
                            return json.dumps(live)
                        dry = '--dry-run=server' in args
                        if args[0]=='apply':
                            objects=json.loads(body)['items']; cluster=next(obj for obj in objects if obj['kind']=='Elasticsearch')
                            self.assertEqual(live['spec']['nodeSets'],cluster['spec']['nodeSets'])
                            events.append('ssa-check' if dry else 'ssa-write')
                            if defect=='ssa-validation':raise ValueError('other field has a foreign SSA owner')
                            if not dry:self.assertIn('--dry-run=none',args)
                        else:
                            self.assertIn('--type=json',args)
                            patch=json.loads(args[args.index('-p')+1])
                            self.assertEqual([{'op':'test','path':'/metadata/uid','value':'original-cr'},
                                              {'op':'test','path':'/metadata/resourceVersion','value':'7'},
                                              {'op':'test','path':'/spec','value':before['spec']}],patch[:3])
                            self.assertEqual('/spec',patch[-1]['path'])
                            self.assertEqual('9.5.6',patch[-1]['value']['version'])
                            self.assertEqual({'logs':{}},patch[-1]['value']['monitoring'])
                            self.assertNotIn('secureSettings',patch[-1]['value'])
                            self.assertEqual({'replace'}, {item['op'] for item in patch if item['op']!='test'})
                            events.append('patch-check' if dry else 'patch-write')
                            if defect=='patch-validation' or defect=='stale' and not dry:raise ValueError('server refused patch')
                            if not dry:live['spec']=patch[-1]['value']
                        return ''
                path=Path(temporary)/'cluster.json';path.write_text(json.dumps(desired))
                if defect in ('none','dry-run'):
                    es.converge(c,Client(),path,dry_run=defect=='dry-run')
                    self.assertEqual(['ssa-check','patch-check']+(['patch-write','ssa-write'] if defect=='none' else []),events)
                    self.assertEqual(before['metadata'],live['metadata'])
                    self.assertEqual(4 if defect=='none' else 3,live['spec']['nodeSets'][-1]['count'])
                else:
                    with self.assertRaises(ValueError):es.converge(c,Client(),path,dry_run=False)
                    self.assertNotIn('ssa-write',events)
                    self.assertEqual(before,live)

    def test_s3_init_resource_shape_matches_eck_typed_round_trip(self):
        for topology in ('standard','compact'):
            c=configuration(topology) | {'elasticsearch_s3_ca_configmap':'private-ca'}
            cluster=next(obj for obj in es.resources(c) if obj['kind']=='Elasticsearch')
            for node in cluster['spec']['nodeSets']:
                self.assertEqual({},node['podTemplate']['spec']['initContainers'][0]['resources'])


class ECKUpgradeContracts(unittest.TestCase):
    def test_old_green_health_cannot_complete_before_target_controller_reconciles(self):
        api = ECKUpgradeAPI()
        api.logs = ''  # Ready target Pod is still waiting for leader election.
        with patch.object(eck.time, 'monotonic', side_effect=[0, 0, 1801]), patch.object(eck.time, 'sleep'):
            with self.assertRaisesRegex(ValueError, 'did not recover'):
                eck.ensure(api, 'elastic-system', api.target, True, 'platform-change')
        self.assertEqual('running', api.changes['kubeauto-eck-change-platform-change']['data']['phase'])

    def test_platform_reconciliation_fence_rejects_wrong_process_resource_and_incomplete_runs(self):
        for defect in ('old-image', 'old-version', 'foreign-owner', 'wrong-resource', 'start-only',
                       'error', 'mixed-iteration', 'restarted', 'kibana-pending', 'valid'):
            api = ECKUpgradeAPI()
            rows = eck.upgrade_consumers(api)
            entries = [json.loads(line) for line in api.logs.splitlines()]
            if defect == 'old-image': api.pod['spec']['containers'][0]['image'] = api.target.replace('3.5.0', '3.4.1')
            elif defect == 'foreign-owner': api.pod['metadata']['ownerReferences'][0]['uid'] = 'foreign'
            elif defect == 'old-version':
                for row in entries: row['service.version'] = '3.4.1+old'
            elif defect == 'wrong-resource':
                for row in entries: row['namespace'] = 'foreign'
            elif defect == 'start-only': entries.pop()
            elif defect == 'error': entries.insert(1, dict(entries[0], message='reconcile error', **{'log.level':'error'}))
            elif defect == 'mixed-iteration': entries[-1]['iteration'] = '2'
            elif defect == 'kibana-pending': rows.append({'kind':'Kibana','namespace':'search','name':'ui','uid':'ui-uid'})
            api.logs = '\n'.join(json.dumps(row) for row in entries)
            original = copy.deepcopy(api.pod)
            def read(*args, **kwargs):
                if 'logs' in args:
                    if defect == 'restarted': api.pod['status']['containerStatuses'][0]['containerID'] = 'replacement'
                    return api.logs
                return ''
            with self.subTest(defect=defect), patch.object(api, 'run', side_effect=read):
                accepted = eck.reconciled_consumers(api, rows, 'owned-controller', api.target)
                self.assertEqual({'consumer-uid'} if defect in ('valid','kibana-pending') else set(), accepted)
                self.assertEqual([], api.applied)
                if defect != 'restarted': self.assertEqual(original, api.pod)

    def test_platform_waits_for_every_new_controller_run_and_then_current_health(self):
        api = ECKUpgradeAPI()
        kibana = {'kind':'Kibana','metadata':{'name':'ui','namespace':'search','uid':'ui-uid','generation':1,
                  'labels':dict(es.LABELS)},'spec':{'version':'9.5.5','count':2},
                  'status':{'health':'green','observedGeneration':1}}
        api.resources.append(kibana)
        complete = api.logs + '\n' + '\n'.join(json.dumps({'service.version':'3.5.0+386c7b14',
            'log.logger':'kibana-controller','namespace':'search','kibana_name':'ui','iteration':'1','message':message})
            for message in ('Starting reconciliation run','Ending reconciliation run'))
        api.logs = ''
        waits = []
        def advance(_):
            self.assertEqual('running', api.changes['kubeauto-eck-change-platform-change']['data']['phase'])
            waits.append(True)
            api.logs = complete
            api.resources[0]['status']['phase'] = 'ApplyingChanges' if len(waits) == 1 else 'Ready'
        # Enumerate both kinds through the real discovery path.
        native_get = api.get
        def get(kind, name='', namespace=''):
            if kind.startswith('elasticsearches.'): return {'items':[api.resources[0]]}
            if kind.startswith('kibanas.'): return {'items':[kibana]}
            return native_get(kind, name, namespace)
        with patch.object(api, 'get', side_effect=get), patch.object(eck.time, 'sleep', side_effect=advance):
            eck.ensure(api, 'elastic-system', api.target, True, 'platform-change')
        self.assertEqual(2, len(waits))
        self.assertEqual('complete', api.changes['kubeauto-eck-change-platform-change']['data']['phase'])

    def test_paused_consumers_refuse_before_any_platform_mutation(self):
        for name, value in (('eck.k8s.elastic.co/managed','false'), ('common.k8s.elastic.co/pause','true'),
                            ('eck.k8s.elastic.co/pause-orchestration','true')):
            api = ECKUpgradeAPI()
            api.resources[0]['metadata']['annotations'] = {name:value}
            with self.subTest(annotation=name), self.assertRaisesRegex(ValueError, 'every consumer to be healthy'):
                eck.ensure(api, 'elastic-system', api.target, True, 'platform-change')
            self.assertEqual([], api.applied)

    def test_platform_tasks_are_excluded_from_default_and_business_tag_selection(self):
        import os
        import subprocess
        executable = Path(sys.executable).with_name('ansible-playbook')
        environment = dict(os.environ, ANSIBLE_ROLES_PATH=str(ROOT/'roles'))
        for tags, included in (('all', False), ('elasticsearch', False), ('eck-platform', True)):
            result = subprocess.run([str(executable), '--list-tasks', str(ROOT/'playbooks/07.cluster-addon.yml'),
                                     '-i', 'localhost,', '--tags', tags], env=environment, capture_output=True, text=True, timeout=30)
            with self.subTest(tags=tags):
                self.assertEqual(0, result.returncode, result.stderr)
                self.assertEqual(included, '校验平台 ECK 维护输入' in result.stdout)
                if included:
                    self.assertNotIn('创建 Elasticsearch 私有渲染目录', result.stdout)
                    self.assertNotIn('Validate logging route', result.stdout)

    def test_platform_receipt_cannot_complete_with_unrecovered_consumers(self):
        api = ECKUpgradeAPI()
        api.failure = 'crds'
        with self.assertRaisesRegex(ValueError, 'interrupted'):
            eck.ensure(api, 'elastic-system', api.target, True, 'platform-change')
        api.failure = ''
        api.resources[0]['status']['health'] = 'red'
        with patch.object(eck.time, 'monotonic', side_effect=[0, 1801]):
            with self.assertRaisesRegex(ValueError, 'did not recover'):
                eck.ensure(api, 'elastic-system', api.target, True, 'platform-change')
        self.assertEqual('running', api.changes['kubeauto-eck-change-platform-change']['data']['phase'])

    def test_new_platform_upgrade_refuses_unhealthy_consumers_before_any_mutation(self):
        for defect in ('red', 'stale', 'missing-node'):
            api = ECKUpgradeAPI()
            status = api.resources[0]['status']
            if defect == 'red': status['health'] = 'red'
            elif defect == 'stale': status['observedGeneration'] = 0
            else: status['availableNodes'] = 0
            with self.subTest(defect=defect), self.assertRaisesRegex(ValueError, 'every consumer to be healthy'):
                eck.ensure(api, 'elastic-system', api.target, True, 'platform-change')
            self.assertEqual([], api.applied)

    def test_platform_upgrade_checks_all_consumer_watches_instead_of_operator_namespace(self):
        for watched, allowed in ((['search'], True), (['logging'], False)):
            api = ECKUpgradeAPI()
            api.config['data']['eck.yaml'] += '\nnamespaces: '+json.dumps(watched)+'\n'
            with self.subTest(watched=watched), redirect_stdout(StringIO()):
                if allowed: eck.ensure(api, 'elastic-system', api.target, True, 'platform-change')
                else:
                    with self.assertRaisesRegex(ValueError, 'does not watch'):
                        eck.ensure(api, 'elastic-system', api.target, True, 'platform-change')
                    self.assertEqual([], api.applied)

    def test_consumer_cli_cannot_trigger_platform_upgrade(self):
        for action in ('preflight', 'ensure'):
            argv = ['eck.py', action, '--namespace', 'search', '--kubeconfig', 'unused',
                    '--upgrade-from', '3.4.1', '--change-id', 'mistake']
            with self.subTest(action=action), patch.object(sys, 'argv', argv), patch.object(eck, 'API') as client:
                with self.assertRaises(SystemExit): eck.main()
                client.assert_not_called()
        for key in ('elasticsearch_eck_upgrade_from', 'elasticsearch_eck_upgrade_change_id'):
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, 'platform maintenance'):
                es.validate(configuration() | {key: '3.4.1' if key.endswith('from') else 'change'})

    def test_unknown_owner_kind_version_and_changed_spec_refuse_before_shared_mutation(self):
        for defect in ('owner', 'kind', 'version', 'deleting'):
            api = ECKUpgradeAPI()
            if defect == 'owner': api.resources[0]['metadata']['labels']['kubeauto.io/component'] = 'foreign'
            elif defect == 'kind': api.resources[0]['kind'] = 'Agent'
            elif defect == 'version': api.resources[0]['spec']['version'] = '8.0.0'
            else: api.resources[0]['metadata']['deletionTimestamp'] = 'now'
            with self.subTest(defect=defect), self.assertRaisesRegex(ValueError, 'unqualified'):
                eck.ensure(api, 'elastic-system', api.target, True, 'platform-change')
            self.assertEqual([], api.applied)
        api = ECKUpgradeAPI(); api.failure = 'crds'
        with self.assertRaises(ValueError): eck.ensure(api, 'elastic-system', api.target, True, 'platform-change')
        api.resources[0]['spec']['version'] = '9.5.6'
        before = len(api.applied)
        with self.assertRaisesRegex(ValueError, 'different input'):
            eck.ensure(api, 'elastic-system', api.target, True, 'platform-change')
        self.assertEqual(before, len(api.applied))

    def test_crd_and_operator_interruptions_resume_bound_change_and_only_then_complete(self):
        for boundary in ('crds', 'operator'):
            api = ECKUpgradeAPI(); api.failure = boundary
            with self.subTest(boundary=boundary), redirect_stdout(StringIO()):
                with self.assertRaisesRegex(ValueError, 'interrupted'): eck.ensure(api, 'search', api.target, True, 'upgrade-1')
                self.assertEqual('running', api.changes['kubeauto-eck-change-upgrade-1']['data']['phase'])
                api.failure = ''
                eck.ensure(api, 'search', api.target, True, 'upgrade-1')
            row = api.changes['kubeauto-eck-change-upgrade-1']['data']
            self.assertEqual('complete', row['phase'])
            self.assertEqual('owned-controller', json.loads(row['inputs'])['operator_uid'])
            self.assertEqual('consumer-uid', json.loads(row['inputs'])['consumers'][0]['uid'])
            self.assertEqual(api.target, api.operator['spec']['template']['spec']['containers'][0]['image'])
            before = len(api.applied)
            with redirect_stdout(StringIO()): eck.ensure(api, 'search', api.target, True, 'upgrade-1')
            self.assertEqual(before + 1, len(api.applied))  # Receipt only; no repeated CRD/Operator rollout.

    def test_changed_consumer_or_controller_refuses_before_resuming_mutations(self):
        for identity in ('consumer', 'operator'):
            api = ECKUpgradeAPI(); api.failure = 'crds'
            with self.assertRaises(ValueError): eck.ensure(api, 'search', api.target, True, 'upgrade-1')
            if identity == 'consumer': api.resources[0]['metadata']['uid'] = 'replacement'
            else: api.operator['metadata']['uid'] = 'replacement'
            before = len(api.applied)
            with self.assertRaisesRegex(ValueError, 'different input'): eck.ensure(api, 'search', api.target, True, 'upgrade-1')
            self.assertEqual(before, len(api.applied))

    def test_new_upgrade_cannot_claim_source_version_when_already_at_target(self):
        api = ECKUpgradeAPI(); api.operator['spec']['template']['spec']['containers'][0]['image'] = api.target
        with self.assertRaisesRegex(ValueError, 'declared 3.4.1'): eck.ensure(api, 'search', api.target, True, 'upgrade-1')
        self.assertEqual([], api.applied)


class BusinessAPI:
    def __init__(self):
        self.config = configuration("compact") | {"elasticsearch_exporter_enabled": False, "elasticsearch_exporter_secret": ""}
        c = self.config
        self.cluster = next(o for o in es.resources(c) if o["kind"] == "Elasticsearch")
        self.cluster["metadata"].update(uid="es-uid", generation=1)
        self.namespace = {"metadata": {"labels": dict(es.LABELS)}}
        self.ledger = None
        self.claim = {"metadata": {"name": "elasticsearch-data-search-es-data-0", "uid": "pvc-uid", "labels": {"elasticsearch.k8s.elastic.co/cluster-name": "search"}}, "spec": {"volumeName": "volume-a"}}
        self.statefulsets = [{"metadata": {"uid": "sts-uid", "ownerReferences": [{"uid": "es-uid"}]}}]
        self.pods = [{"metadata": {"namespace": "elasticsearch", "ownerReferences": [{"uid": "sts-uid"}], "labels": {"elasticsearch.k8s.elastic.co/cluster-name": "search"}}, "spec": {"volumes": [{"persistentVolumeClaim": {"claimName": self.claim["metadata"]["name"]}}]}}]

    def get(self, kind, name="", namespace=""):
        if kind == "namespace": return self.namespace
        if kind == "elasticsearch": return self.cluster
        if kind == "configmap": return self.ledger if name == "search-identity" else None
        if kind == "crd": return {"metadata": {"name": name}}
        if kind == "statefulset": return {"items": self.statefulsets}
        if kind == "pods": return {"items": self.pods}
        if kind == "pvc": return {"items": [self.claim]}
        if kind == "pv": return {"spec": {"claimRef": {"uid": "pvc-uid", "name": self.claim["metadata"]["name"], "namespace": "elasticsearch"}}}
        if kind == "storageclass": return {"provisioner": "driver.longhorn.io", "allowVolumeExpansion": True}
        if kind == "node": return {"spec": {}, "status": {"conditions": [{"type": "Ready", "status": "True"}], "allocatable": {"cpu": "8", "memory": "16Gi"}}}
        return None


class ElasticsearchDeliveryTests(unittest.TestCase):
    def test_minio_analysis_compatibility_is_explicit_and_keeps_other_repository_checks(self):
        option = 'elasticsearch_repository_analysis_copy_during_write_contention'
        native = 'repositories.blobstore.testkit.analyze.copy_during_write_contention'
        for topology in ('standard', 'compact'):
            c = configuration(topology)
            original = es.resources(c)
            self.assertEqual(original, es.resources(c | {option: True}))
            compatible = es.resources(es.validate(c | {option: False}))
            cluster = next(obj for obj in compatible if obj['kind'] == 'Elasticsearch')
            for nodes in cluster['spec']['nodeSets']:
                self.assertIs(nodes['config'].pop(native), False)
            self.assertEqual(original, compatible)
        for invalid in (None, 0, 1, [], 'automatic'):
            with self.subTest(invalid=invalid), self.assertRaisesRegex(ValueError, 'expected boolean'):
                es.validate(configuration() | {option: invalid})

        c = configuration() | {option: False}
        req = {'id':'minio-check', 'type':'repository', 'name':'archive',
               'endpoint':'https://s3.example.com', 'bucket':'backups',
               'base_path':'customer/search', 'region':'us-east-1'}
        events = []
        def request(method, path, body=None):
            events.append((method, path))
            if path == '/': return {'cluster_uuid':'cluster'}
            if method == 'PUT': return {'acknowledged':True}
            if path.endswith('/_verify'): return {'nodes':{'data-a':{}}}
            raise ValueError('repository analysis still fails closed')
        with patch.object(es, 'reserve_repository'), self.assertRaisesRegex(ValueError, 'still fails closed'):
            es.operation(c, None, request, req)
        self.assertEqual(('POST', '/_snapshot/archive/_verify'), events[-2])
        self.assertTrue(events[-1][1].startswith('/_snapshot/archive/_analyze?blob_count=10&'))

    def test_efk_repository_tree_and_ancestors_are_not_business_backup_targets(self):
        c = configuration() | {'elasticsearch_s3_secret': 'business-s3'}
        request = {'id': 'archive', 'type': 'repository', 'name': 'archive', 'endpoint': 'https://s3.example.com',
                   'bucket': 'backups', 'base_path': 'elasticsearch/logging', 'region': 'us-east-1'}
        for path in ('elasticsearch', 'elasticsearch/logging', 'elasticsearch/logging/daily'):
            for role in ('writer', 'reader'):
                with self.subTest(path=path, role=role), self.assertRaisesRegex(ValueError, 'reserved EFK'):
                    es.validate_request(c | {'elasticsearch_repository_role': role}, request | {'base_path': path})
        for path in ('orders/prod', 'elasticsearch/logging-other', 'elasticsearch/business'):
            es.validate_request(c, request | {'base_path': path})

    def test_cross_product_namespace_and_s3_identity_are_checked_in_both_directions(self):
        for component, other_component in (('elasticsearch', 'logging-efk'), ('logging-efk', 'elasticsearch')):
            expected = 'logging' if component == 'logging-efk' else 'elasticsearch'
            for defect in ('none', 'namespace', 'same-access-key', 'missing-other-secret', 'changed-consumer'):
                calls = []
                other = {'kind': 'Elasticsearch', 'metadata': {'namespace': 'other', 'name': 'other', 'uid': 'other-uid',
                         'labels': {'kubeauto.io/component': other_component}},
                         'spec': {'secureSettings': [{'secretName': 'different-secret-name'}]}}
                class Client:
                    def get(inner, kind, name='', namespace=''):
                        calls.append((kind, name, namespace))
                        if kind == 'namespace':
                            return {'metadata': {'labels': {'app.kubernetes.io/managed-by': 'kubeauto',
                                'kubeauto.io/component': 'foreign' if defect == 'namespace' else expected}}}
                        if kind == 'elasticsearch':
                            return None if defect == 'changed-consumer' else other
                        if kind == 'secret':
                            if namespace == 'other' and defect == 'missing-other-secret': return None
                            same = namespace != 'other' or defect == 'same-access-key'
                            return {'data': {'s3.client.default.access_key': 'YQ==' if same else 'Yg==',
                                             's3.client.default.secret_key': 'different-password-' + namespace}}
                        raise AssertionError(kind)
                with self.subTest(component=component, defect=defect), patch.object(eck, 'consumers', return_value=[
                        {'kind': 'Elasticsearch', 'namespace': 'other', 'name': 'other', 'uid': 'other-uid'}]):
                    if defect == 'none': eck.consumer_preflight(Client(), 'owned', component, 's3')
                    else:
                        with self.assertRaises(ValueError): eck.consumer_preflight(Client(), 'owned', component, 's3')
                self.assertTrue(calls)

    def test_s3_truststore_script_imports_into_readonly_defaults_as_nonroot_and_restarts_cleanly(self):
        import os
        import subprocess
        import sys
        c=configuration('compact') | {'elasticsearch_s3_ca_configmap':'customer-ca'}
        cluster=next(o for o in es.resources(c) if o['kind']=='Elasticsearch')
        init=cluster['spec']['nodeSets'][0]['podTemplate']['spec']['initContainers'][0]
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);jdk=root/'jdk';trust=root/'trust';ca=root/'ca'
            for directory in (jdk/'lib/security',jdk/'bin',trust,ca):directory.mkdir(parents=True)
            defaults=jdk/'lib/security/cacerts';defaults.write_text('distribution-default-roots\n');defaults.chmod(0o444)
            bundle='-----BEGIN CERTIFICATE-----\nfirst-ca\n-----END CERTIFICATE-----\n-----BEGIN CERTIFICATE-----\nsecond-ca\n-----END CERTIFICATE-----\n'
            (ca/'ca.crt').write_text(bundle)
            keytool=jdk/'bin/keytool'
            keytool.write_text('#!'+sys.executable+'\nimport sys\nfrom pathlib import Path\na=sys.argv\nwith Path(a[a.index("-keystore")+1]).open("a") as store:store.write(Path(a[a.index("-file")+1]).read_text())\n')
            keytool.chmod(0o755)
            script=init['command'][2].replace('/usr/share/elasticsearch/jdk',str(jdk)).replace('/trust',str(trust)).replace('/ca/ca.crt',str(ca/'ca.crt'))
            preexec=None
            if os.geteuid()==0:
                # A root CI process would hide this production permission bug.
                for path in (root,*root.rglob('*')):os.chown(path,65534,65534)
                def preexec():
                    os.setgroups([]);os.setgid(65534);os.setuid(65534)
            for stale in (False,True):
                if stale:
                    target=trust/'cacerts';target.write_text('discarded-previous-attempt\n');target.chmod(0o444)
                    if os.geteuid()==0:os.chown(target,65534,65534)
                result=subprocess.run(init['command'][:2]+[script],text=True,capture_output=True,timeout=10,preexec_fn=preexec)
                self.assertEqual(0,result.returncode,result.stdout+result.stderr)
                self.assertEqual('distribution-default-roots\n'+bundle,(trust/'cacerts').read_text())
                self.assertEqual(0o644,(trust/'cacerts').stat().st_mode & 0o777)
                self.assertEqual('distribution-default-roots\n',defaults.read_text())
                self.assertEqual(0o444,defaults.stat().st_mode & 0o777)

    def test_server_validation_failure_stops_before_real_apply_in_actual_ansible(self):
        import os
        import subprocess
        import sys
        tasks = yaml.safe_load((ROOT/'roles/cluster-addon/tasks/elasticsearch.yml').read_text())
        applies = [task for task in tasks if 'apply' in task.get('ansible.builtin.command', {}).get('argv', [])]
        self.assertEqual(2, len(applies))
        self.assertTrue(all('loop' not in task and not task.get('ignore_errors') for task in applies))
        for fail in (True, False):
            with self.subTest(dry_run_fails=fail), tempfile.TemporaryDirectory() as temporary:
                directory = Path(temporary); binary = directory/'kubectl'; log = directory/'calls'
                target = directory/'yml/elasticsearch'; target.mkdir(parents=True)
                c = configuration('compact')
                (target/'input.json').write_text(json.dumps(c))
                (target/'cluster.json').write_text(json.dumps({'apiVersion':'v1','kind':'List','items':es.resources(c)}))
                binary.write_text('#!'+sys.executable+'\nimport os,sys\nfrom pathlib import Path\np=Path(os.environ["APPLY_CALLS"])\nwith p.open("a") as f:f.write(" ".join(sys.argv[1:])+"\\n")\nsys.exit(7 if "--dry-run=server" in sys.argv and os.environ["DRY_RUN_FAIL"]=="yes" else 0)\n')
                binary.chmod(0o700)
                playbook = directory/'play.yml'
                playbook.write_text(yaml.safe_dump([{'hosts':'localhost','gather_facts':False,
                    'vars':{'base_dir':str(ROOT),'cluster_dir':str(directory),'elasticsearch_state':'present','ansible_python_interpreter':sys.executable},
                    'tasks':applies}],allow_unicode=True))
                environment = dict(os.environ, PATH=str(directory)+':'+os.environ['PATH'],
                    APPLY_CALLS=str(log), DRY_RUN_FAIL='yes' if fail else 'no',
                    ANSIBLE_LOCAL_TEMP=str(directory/'local'), ANSIBLE_REMOTE_TEMP=str(directory/'remote'))
                result = subprocess.run([str(ROOT/'.venv/bin/ansible-playbook'),'-i','localhost,','-c','local',str(playbook)],
                    text=True,capture_output=True,env=environment,timeout=30)
                self.assertEqual(fail,result.returncode!=0,result.stdout+result.stderr)
                calls=log.read_text().splitlines()
                self.assertEqual(2 if fail else 5,len(calls))
                self.assertIn('--dry-run=server',calls[1])
                if fail:self.assertFalse(any('--dry-run=none' in call for call in calls))
                else:self.assertIn('--dry-run=none',calls[-1])

    def test_default_http_certificate_remains_an_object_after_custom_certificate_removal(self):
        c = configuration('compact')
        def tls(config):
            return next(o for o in es.resources(config) if o['kind'] == 'Elasticsearch')['spec']['http']['tls']
        self.assertEqual({'secretName': 'customer-http'}, tls(c | {'elasticsearch_http_tls_secret': 'customer-http'})['certificate'])
        restored = tls(c | {'elasticsearch_http_tls_secret': ''})
        # An empty granular object is removed by SSA while relinquishing the
        # previously owned secretName. An explicit empty string keeps the
        # non-nullable object and selects ECK's default certificate branch.
        self.assertEqual({'secretName': ''}, restored['certificate'])
        self.assertTrue(restored['selfSignedCertificate']['subjectAltNames'])

    def test_shared_eck_get_requests_the_managed_fields_used_to_prove_crd_ownership(self):
        native=ECKAPI()
        api=eck.API('qualification-context')
        calls=[]
        def run(*args,**kwargs):
            calls.append(args)
            data=copy.deepcopy(native.get('crd'))
            if '--show-managed-fields=true' not in args:
                for obj in data['items']:obj['metadata'].pop('managedFields',None)
            return json.dumps(data)
        with patch.object(api,'run',side_effect=run):
            eck.verify_crds(api)
        self.assertTrue(calls)
        self.assertTrue(all('--show-managed-fields=true' in args for args in calls))

    def test_rolling_budget_allows_one_healthy_restart_and_blocks_a_second(self):
        # ECK 3.5.0, commit 386c7b14, stateful/upgrade_pods_deletion.go:
        # allowedDeletions = maxUnavailable - unhealthyPods. Its matching
        # predicate refuses a healthy candidate when that budget is reached.
        # Existing nodeSets restart their stable Pod identities; maxSurge
        # alone cannot unblock them, even when a spare host is available.
        for topology in ("standard", "compact"):
            with self.subTest(topology=topology):
                cluster = next(o for o in es.resources(configuration(topology)) if o["kind"] == "Elasticsearch")
                budget = cluster["spec"]["updateStrategy"]["changeBudget"]
                self.assertGreater(budget["maxUnavailable"] - 0, 0, "healthy rolling changes cannot start")
                self.assertLessEqual(budget["maxUnavailable"] - 1, 0, "a recovering member must block a second restart")
                self.assertEqual(1, budget["maxSurge"])
                self.assertNotIn("eck.k8s.elastic.co/disable-upgrade-predicates", cluster["metadata"].get("annotations", {}))

    def test_api_failure_diagnosis_reports_causal_types_without_credential_bearing_reasons(self):
        raw = json.dumps({'error': {'type': 'repository_verification_exception', 'reason': 'password=do-not-log',
            'root_cause': [{'type': 'repository_verification_exception', 'reason': 'Authorization secret'}],
            'caused_by': {'type': 'amazon_s3_exception', 'reason': 'access-key-do-not-log'}}})
        self.assertEqual(' types=repository_verification_exception,amazon_s3_exception', es.api_error_types(raw))
        for value in (b'not-json', b'[]', b'null', b'{"error":"secret"}', b'{"error":{"type":"invalid secret","root_cause":null}}'):
            self.assertEqual('', es.api_error_types(value))

    def test_spare_hosts_do_not_create_members_or_relax_host_separation(self):
        for topology in ('standard', 'compact'):
            c = configuration(topology) | {
                'elasticsearch_data_nodes': ['data-a', 'data-b', 'data-c', 'data-spare'],
                'elasticsearch_data_count': 3,
            }
            es.validate(c)
            obj = next(o for o in es.resources(c) if o['kind'] == 'Elasticsearch')
            data = next(s for s in obj['spec']['nodeSets'] if s['name'] == 'data')
            self.assertEqual(3, data['count'])
            self.assertEqual(6 if topology == 'standard' else 3, es.member_count(c))
            affinity = data['podTemplate']['spec']['affinity']
            terms = affinity['nodeAffinity']['requiredDuringSchedulingIgnoredDuringExecution']['nodeSelectorTerms']
            # Kubernetes 1.33.6 ValidateNodeFieldSelectorRequirement permits
            # exactly one value per In/NotIn matchFields requirement. Terms
            # are ORed; retain exact node names without relying on hostname labels.
            for term in terms:
                self.assertNotIn('matchExpressions', term)
                self.assertEqual(1, len(term['matchFields']))
                field = term['matchFields'][0]
                self.assertEqual(('metadata.name', 'In'), (field['key'], field['operator']))
                self.assertEqual(1, len(field['values']))
            for candidate in [*c['elasticsearch_data_nodes'], 'data-foreign', *c['elasticsearch_master_nodes']]:
                allowed = any(candidate in term['matchFields'][0]['values'] for term in terms)
                self.assertEqual(candidate in c['elasticsearch_data_nodes'], allowed)
            self.assertEqual(len(c['elasticsearch_data_nodes']), len(terms))
            rule = affinity['podAntiAffinity']['requiredDuringSchedulingIgnoredDuringExecution'][0]
            self.assertEqual('kubernetes.io/hostname', rule['topologyKey'])
            self.assertEqual('search-es-data', rule['labelSelector']['matchLabels']['elasticsearch.k8s.elastic.co/statefulset-name'])
        for value in (True, '3', -1, 1, 2, 5):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, 'data count'):
                es.validate(c | {'elasticsearch_data_count': value})
        with self.assertRaisesRegex(ValueError, 'exactly three'):
            es.validate(c | {'elasticsearch_data_count': 4})

    def test_member_count_health_and_reload_ignore_spare_hosts(self):
        from contextlib import contextmanager
        from unittest.mock import Mock
        c = configuration('compact') | {'elasticsearch_data_count': 3,
            'elasticsearch_data_nodes': ['data-a', 'data-b', 'data-c', 'data-spare']}
        api = BusinessAPI()
        api.cluster['status'] = {'observedGeneration': 1, 'health': 'green', 'availableNodes': 3, 'phase': 'Ready', 'conditions': [{'type': 'ReconciliationComplete', 'status': 'True'}]}
        api.apply = Mock()
        def request(method, path, body=None):
            if path.startswith('/_cluster/health'): return {'status': 'green', 'number_of_nodes': 3}
            if path == '/': return {'version': {'number': '9.5.6'}, 'cluster_uuid': 'cluster-a'}
            if path == '/_nodes': return {'nodes': {str(i): {'version': '9.5.6'} for i in range(3)}}
            if path == '/_nodes/reload_secure_settings': return {'_nodes': {'successful': 3, 'failed': 0}, 'nodes': {str(i): {} for i in range(3)}}
            raise AssertionError(path)
        @contextmanager
        def connection(*args): yield request
        with patch.object(es, 'connection', connection):
            es.health(c, api)
        es.operation(c, api, request, {'id': 'reload-spares', 'type': 'reload-secure-settings'})
        api.apply.assert_called_once()

    def test_two_topologies_validate_against_the_pinned_official_crd(self):
        source = list(yaml.safe_load_all((FILES / "eck-3.5.0-crds.yaml").read_text()))
        schemas = {o["spec"]["names"]["kind"]: next(v["schema"]["openAPIV3Schema"] for v in o["spec"]["versions"] if v["name"] == "v1") for o in source if any(v["name"] == "v1" for v in o["spec"]["versions"])}
        for topology in ("standard", "compact"):
            c = configuration(topology)
            c["elasticsearch_kibana_enabled"] = True
            c["elasticsearch_s3_secret"] = "s3-credentials"
            c["elasticsearch_s3_ca_configmap"] = "s3-ca"
            es.validate(c)
            objects = es.resources(c)
            for obj in objects:
                if obj["kind"] in schemas:
                    Draft7Validator(schemas[obj["kind"]]).validate(obj)
            database = next(o for o in objects if o["kind"] == "Elasticsearch")
            self.assertEqual(database["spec"]["volumeClaimDeletePolicy"], "DeleteOnScaledownOnly")
            self.assertTrue(all(s["config"]["node.store.allow_mmap"] for s in database["spec"]["nodeSets"]))
            self.assertEqual(objects[2]["spec"]["selector"]["elasticsearch.k8s.elastic.co/node-data"], "true")

    def test_health_waits_for_completed_reconciliation_even_when_generation_and_data_are_ready(self):
        from unittest.mock import Mock
        from contextlib import nullcontext
        c = configuration('compact') | {'elasticsearch_exporter_enabled': False}
        api = BusinessAPI()
        original = api.get
        states = [
            {'conditions': [{'type': 'ReconciliationComplete', 'status': 'True'}]},
            {'phase': 'ApplyingChanges', 'conditions': [{'type': 'ReconciliationComplete', 'status': 'True'}]},
            {'phase': 'Ready', 'conditions': [{'type': 'ReconciliationComplete', 'status': 'False'}]},
            {'phase': 'Ready', 'conditions': []},
            {'phase': 'Ready', 'conditions': [{'type': 'ReconciliationComplete', 'status': 'True'}]},
        ]
        observations = []
        def get(kind, name='', namespace=''):
            if kind != 'elasticsearch': return original(kind, name, namespace)
            status = {'observedGeneration': 1, 'health': 'green', 'availableNodes': 3}
            status.update(states[min(len(observations), len(states)-1)])
            observations.append(status)
            return dict(api.cluster, status=status)
        def request(method, path):
            if path.startswith('/_cluster/health'): return {'timed_out': False, 'status': 'green', 'number_of_nodes': 3}
            if path == '/': return {'version': {'number': '9.5.6'}, 'cluster_uuid': 'cluster-a'}
            if path == '/_nodes': return {'nodes': {str(i): {'version': '9.5.6'} for i in range(3)}}
            raise AssertionError(path)
        api.get = get
        api.apply = Mock()
        with patch.object(es, 'connection', return_value=nullcontext(request)) as connect, patch.object(es.time, 'sleep') as sleep:
            es.health(c, api)
        self.assertEqual(5, len(observations))
        self.assertEqual(4, sleep.call_count)
        connect.assert_called_once()
        api.apply.assert_called_once()

    def test_health_waits_for_stale_status_and_every_member_version(self):
        from unittest.mock import Mock
        from contextlib import contextmanager
        c = configuration('compact') | {'elasticsearch_exporter_enabled': False}
        api = BusinessAPI(); api.cluster['status'] = {'observedGeneration': 1, 'health': 'green', 'availableNodes': 3, 'phase': 'Ready', 'conditions': [{'type': 'ReconciliationComplete', 'status': 'True'}]}
        api.apply = Mock()
        attempts = []
        @contextmanager
        def connection(*args):
            attempt = len(attempts); attempts.append(attempt)
            def request(method, path):
                if path.startswith('/_cluster/health'):
                    return {'timed_out': attempt == 0, 'status': 'yellow' if attempt == 0 else 'green', 'number_of_nodes': 3}
                if path == '/': return {'version': {'number': '9.5.6'}, 'cluster_uuid': 'cluster-a'}
                if path == '/_nodes': return {'nodes': {str(i): {'version': '9.5.5' if attempt == 1 and i == 2 else '9.5.6'} for i in range(3)}}
                raise AssertionError(path)
            yield request
        with patch.object(es, 'connection', connection), patch.object(es.time, 'sleep') as sleep:
            es.health(c, api)
        self.assertEqual([0, 1, 2], attempts)
        self.assertEqual(2, sleep.call_count)
        api.apply.assert_called_once()
        self.assertEqual('cluster-a', api.apply.call_args.args[0][0]['data']['cluster_uuid'])

    def test_health_reopens_verified_tls_after_certificate_reload_race(self):
        import ssl
        from contextlib import contextmanager
        from unittest.mock import Mock
        c = configuration('compact') | {'elasticsearch_exporter_enabled': False}
        api = BusinessAPI()
        api.cluster['status'] = {'observedGeneration': 1, 'health': 'green', 'availableNodes': 3,
            'phase': 'Ready', 'conditions': [{'type': 'ReconciliationComplete', 'status': 'True'}]}
        api.apply = Mock()
        attempts = []
        @contextmanager
        def connection(*args):
            attempts.append(len(attempts))
            def request(method, path):
                if len(attempts) == 1:
                    raise ssl.SSLCertVerificationError('unable to get local issuer certificate')
                if path.startswith('/_cluster/health'):
                    return {'status': 'green', 'number_of_nodes': 3}
                if path == '/': return {'version': {'number': '9.5.6'}, 'cluster_uuid': 'cluster-a'}
                if path == '/_nodes': return {'nodes': {str(i): {'version': '9.5.6'} for i in range(3)}}
                raise AssertionError(path)
            yield request
        with patch.object(es, 'connection', connection), patch.object(es.time, 'sleep') as sleep:
            es.health(c, api)
        self.assertEqual(2, len(attempts))
        sleep.assert_called_once_with(5)
        api.apply.assert_called_once()

    def test_health_certificate_wait_is_bounded_and_other_failures_propagate(self):
        import ssl
        from unittest.mock import Mock
        c = configuration('compact') | {'elasticsearch_wait_seconds': 10}
        for failure in (ssl.SSLCertVerificationError('wrong CA'), ssl.SSLCertVerificationError('wrong SAN'),
                        ssl.SSLError('TLS protocol failure'), ValueError('Elasticsearch API refused status=401')):
            with self.subTest(failure=failure):
                api = BusinessAPI()
                api.cluster['status'] = {'observedGeneration': 1, 'health': 'green', 'availableNodes': 3,
                    'phase': 'Ready', 'conditions': [{'type': 'ReconciliationComplete', 'status': 'True'}]}
                api.apply = Mock()
                certificate = isinstance(failure, ssl.SSLCertVerificationError)
                expected = ValueError if certificate else type(failure)
                with patch.object(es, 'connection', side_effect=failure) as connect, \
                        patch.object(es.time, 'monotonic', side_effect=[0, 0, 5, 10]), \
                        patch.object(es.time, 'sleep') as sleep, \
                        self.assertRaisesRegex(expected, 'timed out' if certificate else str(failure)):
                    es.health(c, api)
                self.assertEqual(2 if certificate else 1, connect.call_count)
                self.assertEqual(2 if certificate else 0, sleep.call_count)
                api.apply.assert_not_called()

    def test_empty_kubectl_list_output_has_list_semantics_but_missing_names_stay_absent(self):
        # Actual first admission failure: kubectl emits no JSON for an empty
        # list with --ignore-not-found. Authorization errors still propagate.
        for implementation in (es, eck):
            api = implementation.API('/private/config')
            with patch.object(api, 'run', return_value='\n') as run:
                for kind in ('pv', 'pvc', 'crd', 'pods'):
                    self.assertEqual({'items': []}, api.get(kind))
                self.assertIsNone(api.get('namespace', 'missing'))
                self.assertIsNone(api.get('secret', 'missing', 'owned'))
            with patch.object(api, 'run', side_effect=ValueError('Kubernetes authorization failure')):
                with self.assertRaisesRegex(ValueError, 'authorization'): api.get('pvc')

    def test_insecure_or_ambiguous_inputs_are_rejected(self):
        for key, value in (("elasticsearch_topology", "single"), ("elasticsearch_namespace", "logging"),
                           ("elasticsearch_storage_class", ""), ("elasticsearch_ver", "9.5.1"),
                           ("elasticsearch_data_nodes", ["one", "one", "two"]), ("elasticsearch_data_heap", "16g"),
                           ("elasticsearch_exporter_secret", ""), ("elasticsearch_eck_upgrade_from", "3.4.0")):
            with self.subTest(key=key), self.assertRaises(ValueError):
                es.validate(configuration() | {key: value})

    def test_existing_shared_eck_is_reused_without_reapplying_or_rolling(self):
        api = ECKAPI()
        with redirect_stdout(StringIO()):
            eck.ensure(api, "search", api.operator["spec"]["template"]["spec"]["containers"][0]["image"])
        self.assertEqual(api.applied, [])

    def test_shared_eck_rejects_foreign_ownership_wrong_version_and_watch(self):
        for mutation in ("ownership", "version", "watch", "override"):
            api = ECKAPI()
            image = api.operator["spec"]["template"]["spec"]["containers"][0]["image"]
            if mutation == "ownership":
                api.operator["metadata"]["managedFields"] = []
            elif mutation == "version":
                api.operator["spec"]["template"]["spec"]["containers"][0]["image"] = image.replace("3.5.0", "3.4.1")
            elif mutation == "watch":
                api.config["data"]["eck.yaml"] += "\nnamespaces: [logging]\n"
            else:
                api.operator["spec"]["template"]["spec"]["containers"][0]["env"].append({"name": "NAMESPACES", "value": "logging"})
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                eck.preflight(api, "search", image)
            self.assertEqual(api.applied, [])

    def test_shared_operator_cannot_hide_changed_or_foreign_crd_schema(self):
        for defect in ("schema", "ownership"):
            api = ECKAPI()
            if defect == "schema":
                api.source["crds"][0]["spec"]["versions"][0]["schema"]["openAPIV3Schema"]["description"] = "changed schema"
            else:
                original = api.get
                def get(kind, name="", namespace=""):
                    obj = original(kind, name, namespace)
                    if kind == "crd": obj["items"][0]["metadata"]["managedFields"] = []
                    return obj
                api.get = get
            with self.subTest(defect=defect), self.assertRaisesRegex(ValueError, "CRD"):
                eck.ensure(api, "search", "registry.talkschool.cn:5000/brinnatt/eck-operator:3.5.0")
            self.assertEqual(api.applied, [])

    def test_consumer_discovery_protects_terminating_and_non_logging_resources(self):
        api = ECKAPI()
        api.resources = [{"kind": "Elasticsearch", "metadata": {"namespace": "search", "name": "orders", "uid": "business", "deletionTimestamp": "2026-11-03T00:00:00Z"}}]
        self.assertEqual(eck.consumers(api)[0]["uid"], "business")

    def test_restore_refuses_existing_data_and_writer_repository(self):
        c = configuration()
        req = {"id": "restore-1", "type": "restore", "repository": "archive", "snapshot": "before-upgrade", "indices": ["orders"]}
        with self.assertRaisesRegex(ValueError, "isolated target"):
            es.operation(c, None, None, req)
        c["elasticsearch_repository_role"] = "reader"
        with self.assertRaisesRegex(ValueError, "not empty"):
            es.operation(c, None, lambda *args: [{"index": "orders"}], req)

    def test_journal_refuses_changed_input_before_executing_api_operation(self):
        c = configuration()
        c["elasticsearch_requests"] = [{"id": "restore-1", "type": "restore"}]
        class Journal:
            def get(self, *args):
                return {"metadata": {"labels": es.LABELS}, "data": {"fingerprint": "other", "phase": "complete"}}
        from contextlib import nullcontext
        with patch.object(es, "connection", return_value=nullcontext(None)), self.assertRaisesRegex(ValueError, "different input"):
            es.operations(c, Journal())

    def test_request_journal_cannot_be_reused_by_another_data_cluster(self):
        from contextlib import nullcontext
        from unittest.mock import Mock
        req = {'id': 'role-1', 'type': 'role', 'name': 'application', 'definition': {'cluster': ['monitor']}}
        c = configuration() | {'elasticsearch_requests': [req]}
        import hashlib
        fingerprint = hashlib.sha256(json.dumps(req, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        api = Mock()
        api.get.return_value = {'metadata': es.meta(c, 'search-request-role-1'), 'data': {'cluster_uuid': 'previous', 'phase': 'complete', 'fingerprint': fingerprint}}
        request = Mock(return_value={'cluster_uuid': 'replacement'})
        with patch.object(es, 'connection', return_value=nullcontext(request)), self.assertRaisesRegex(ValueError, 'different data cluster'):
            es.operations(c, api)
        api.apply.assert_not_called()
        self.assertEqual([(('GET', '/'), {})], request.call_args_list)

    def test_secure_settings_reload_requires_positive_success_from_every_member(self):
        c = configuration('compact')
        valid = {'nodes': {str(i): {} for i in range(3)}, '_nodes': {'successful': 3, 'failed': 0}}
        req = {'id': 'reload-1', 'type': 'reload-secure-settings'}
        for invalid in ({}, valid | {'nodes': {}}, valid | {'_nodes': {'successful': 2, 'failed': 1}}, valid | {'nodes': {'0': {'reload_exception': {}}, '1': {}, '2': {'reload_exception': {'type': 'exception'}}}}):
            with self.subTest(invalid=invalid), self.assertRaisesRegex(ValueError, 'refused secure-settings'):
                es.operation(c, None, lambda *a: invalid, req)
        es.operation(c, None, lambda *a: valid, req)

    def test_complete_requests_are_validated_before_any_product_action(self):
        good = {"id": "s3", "type": "repository", "name": "archive", "endpoint": "https://s3.example.com", "bucket": "backups", "base_path": "orders/prod", "region": "us-east-1"}
        c = configuration() | {"elasticsearch_s3_secret": "s3-keys"}
        es.validate(c | {"elasticsearch_requests": [good]})
        bad = [good | {"endpoint": "http://s3.example.com"}, good | {"path_style_access": "sometimes"}, good | {"base_path": "../foreign"},
               {"id": "user", "type": "user", "name": "reader", "roles": ["read"]},
               {"id": "role", "type": "role", "name": "reader", "definition": {"metadata": {"password": "inline"}}},
               {"id": "snapshot", "type": "snapshot", "repository": "archive", "snapshot": "backup", "indices": ["*"]},
               {"id": "restore", "type": "restore", "repository": "archive", "snapshot": "backup", "indices": ["orders"]}]
        for req in bad:
            with self.subTest(req=req["id"]), self.assertRaises(ValueError):
                es.validate(c | {"elasticsearch_requests": [req]})
        for value in (0, 1, None, "False", [], {}):
            with self.subTest(boolean=value), self.assertRaises(ValueError):
                es.flag(value)
        self.assertFalse(es.flag("false"))

    def test_snapshot_uses_exact_lookup_and_a_real_receipt(self):
        req = {"id": "backup-1", "type": "snapshot", "repository": "archive", "snapshot": "backup", "indices": ["orders"], "include_global_state": "false"}
        calls = []
        def request(method, path, body=None):
            calls.append((method, path, body))
            if path.endswith("ignore_unavailable=true"):
                return {"snapshots": []}
            return {"snapshots": [{"uuid": "snap-uuid", "state": "SUCCESS", "indices": ["orders"], "shards": {"failed": 0}}]}
        self.assertEqual(es.operation(configuration(), None, request, req), {"snapshot_uuid": "snap-uuid"})
        self.assertFalse(calls[1][2]["include_global_state"])
        self.assertNotIn("_all", calls[0][1])

    def test_restore_uses_its_completion_response_after_primary_relocation(self):
        c = configuration() | {"elasticsearch_repository_role": "reader"}
        req = {"id": "restore-1", "type": "restore", "repository": "archive", "snapshot": "backup", "indices": ["orders"]}
        calls = []
        def request(method, path, body=None, *, timeout=60):
            calls.append((method, path, timeout))
            if path.startswith("/_cat/"): return []
            if method == "POST": return {"snapshot": {"snapshot": "backup", "indices": ["orders"], "shards": {"total": 3, "successful": 3, "failed": 0}}}
            if path.startswith("/_snapshot/"): return {"snapshots": [{"uuid": "backup-uuid", "state": "SUCCESS", "indices": ["orders"]}]}
            # A completed snapshot recovery may already have been replaced by
            # PEER relocation. Recovery history is not a restore receipt.
            if "/_recovery" in path: raise AssertionError("relocation replaced snapshot recovery history")
            if "/_settings" in path: return {"orders": {"settings": {"index": {"uuid": "index-uuid"}}}}
            return {"status": "green", "timed_out": False}
        self.assertEqual(es.operation(c, None, request, req), {"snapshot_uuid": "backup-uuid", "index_uuids": {"orders": "index-uuid"}})
        posts = [(path, timeout) for method, path, timeout in calls if method == "POST"]
        self.assertEqual(1, len(posts))
        self.assertEqual("/_snapshot/archive/backup/_restore?wait_for_completion=true", posts[0][0])
        self.assertGreater(posts[0][1], 60)
        self.assertLessEqual(posts[0][1], c["elasticsearch_wait_seconds"])
        with self.assertRaisesRegex(ValueError, "index identity changed"):
            es.verify_receipt(request, req, {"snapshot_uuid": "backup-uuid", "index_uuids": {"orders": "replaced"}})

    def test_restore_refuses_incomplete_or_wrong_completion_and_never_retries_submission(self):
        c = configuration() | {"elasticsearch_repository_role": "reader"}
        req = {"id": "restore-1", "type": "restore", "repository": "archive", "snapshot": "backup", "indices": ["orders"]}
        complete = {"snapshot": "backup", "indices": ["orders"], "shards": {"total": 3, "successful": 3, "failed": 0}}
        replies = [{"accepted": True}, {}, {"snapshot": complete | {"snapshot": "another"}},
                  {"snapshot": complete | {"indices": []}}, {"snapshot": complete | {"indices": ["orders", "foreign"]}}]
        replies += [{"snapshot": complete | {"shards": shards}} for shards in (
            {}, {"total": 0, "successful": 0, "failed": 0}, {"total": 3, "successful": 2, "failed": 1},
            {"total": 3, "successful": 2, "failed": 0}, {"total": 3, "successful": 3},
            {"total": True, "successful": True, "failed": 0})]
        for reply in replies + [TimeoutError("uncertain restore submission")]:
            calls = []
            def request(method, path, body=None, *, timeout=60):
                calls.append((method, path))
                if path.startswith("/_cat/"): return []
                if method == "POST":
                    if isinstance(reply, Exception): raise reply
                    return reply
                if path.startswith("/_snapshot/"):
                    return {"snapshots": [{"uuid": "backup-uuid", "state": "SUCCESS", "indices": ["orders"]}]}
                raise AssertionError("invalid completion must refuse before health or target reads")
            with self.subTest(reply=reply), self.assertRaises((ValueError, TimeoutError)):
                es.operation(c, None, request, req)
            self.assertEqual(1, sum(method == "POST" for method, _ in calls))

    def test_restore_timeout_preserves_running_journal_and_refuses_replay(self):
        from contextlib import nullcontext
        from unittest.mock import Mock
        req = {"id": "restore-1", "type": "restore", "repository": "archive", "snapshot": "backup", "indices": ["orders"]}
        c = configuration() | {"elasticsearch_repository_role": "reader", "elasticsearch_requests": [req]}
        api = Mock(); records = []; posts = []
        api.get.side_effect = lambda *args: copy.deepcopy(records[-1]) if records else None
        api.apply.side_effect = lambda objects: records.extend(copy.deepcopy(objects))
        def request(method, path, body=None, *, timeout=60):
            if path == "/": return {"cluster_uuid": "target-cluster"}
            if path.startswith("/_cat/"): return []
            if method == "POST":
                posts.append(path)
                raise TimeoutError("restore response was lost")
            return {"snapshots": [{"uuid": "backup-uuid", "state": "SUCCESS", "indices": ["orders"]}]}
        with patch.object(es, "connection", return_value=nullcontext(request)), patch.object(es, "verify_restore_repository"):
            with self.assertRaises(TimeoutError): es.operations(c, api)
            self.assertEqual("running", records[-1]["data"]["phase"])
            self.assertNotIn("receipt", records[-1]["data"])
            with self.assertRaisesRegex(ValueError, "unknown result"): es.operations(c, api)
        self.assertEqual(1, len(posts)); self.assertEqual(1, len(records))

    def test_api_transport_preserves_default_timeout_and_uses_explicit_restore_budget_once(self):
        import base64
        from unittest.mock import Mock
        api = Mock(); api.command = ["kubectl"]
        api.get.side_effect = [{"data": {"tls.crt": base64.b64encode(b"test trust").decode()}},
                               {"data": {"elastic": base64.b64encode(b"test password").decode()}}]
        process = Mock(); process.poll.return_value = None
        def launch(*args, **kwargs):
            kwargs["stdout"].write("Forwarding from 127.0.0.1:12345 -> 9200\n")
            kwargs["stdout"].flush()
            return process
        transport = Mock(); transport.getresponse.return_value.status = 200
        transport.getresponse.return_value.read.return_value = b"{}"
        with patch.object(es.ssl, "create_default_context"), patch.object(es.subprocess, "Popen", side_effect=launch), patch.object(es, "TunnelHTTPS", return_value=transport) as factory:
            with es.connection(configuration(), api) as request:
                request("GET", "/")
                request("POST", "/_snapshot/archive/backup/_restore?wait_for_completion=true", {}, timeout=1800)
                transport.getresponse.side_effect = TimeoutError("unknown response")
                with self.assertRaises(TimeoutError): request("POST", "/_snapshot/archive/backup/_restore?wait_for_completion=true", {}, timeout=1800)
        self.assertEqual([60, 1800, 1800], [call.kwargs["timeout"] for call in factory.call_args_list])
        self.assertEqual(3, transport.request.call_count)
        self.assertEqual(3, transport.close.call_count)
        process.terminate.assert_called_once()

    def test_security_feature_restore_requires_real_file_realm_and_waits_for_every_feature_index(self):
        c = configuration() | {'elasticsearch_repository_role': 'reader', 'elasticsearch_file_realm_secret': 'file-admin', 'elasticsearch_admin_secret': 'recovery-admin'}
        req = {'id': 'features-1', 'type': 'restore', 'repository': 'archive', 'snapshot': 'backup', 'indices': ['orders'], 'feature_states': ['security', 'kibana']}
        state = {'realm': 'file', 'business': False, 'missing_feature': False, 'feature_uid': 'system-id'}
        calls = []
        def request(method, path, body=None, *, timeout=60):
            calls.append((method, path, body))
            if path == '/_security/_authenticate': return {'authentication_realm': {'type': state['realm']}, 'roles': ['superuser']}
            if path.startswith('/_cat/'): return [{'index': 'another-business'}] if state['business'] else []
            if path.startswith('/_snapshot/') and method == 'GET':
                return {'snapshots': [{'uuid': 'backup-id', 'state': 'SUCCESS', 'indices': ['orders'], 'feature_states':
                    [{'feature_name': 'security', 'indices': ['.security-7']}] + ([] if state['missing_feature'] else [{'feature_name': 'kibana', 'indices': ['.kibana_1', '.ds-.workflows-events-000001']}])}]}
            if method == 'POST': return {'snapshot': {'snapshot': 'backup', 'indices': ['orders', '.security-7', '.kibana_1', '.ds-.workflows-events-000001'], 'shards': {'total': 6, 'successful': 6, 'failed': 0}}}
            if '_cluster/health' in path: return {'status': 'green', 'timed_out': False}
            if '_recovery' in path: return {name: {'shards': [{'primary': True, 'stage': 'DONE', 'source': {'repository': 'archive', 'snapshot': 'backup'}}]} for name in ('.security-7', '.kibana_1', '.ds-.workflows-events-000001', 'orders')}
            if path == '/_cluster/state/metadata?filter_path=metadata.indices.*.settings.index.uuid':
                return {'metadata': {'indices': {name: {'settings': {'index': {'uuid': state['feature_uid']+name}}} for name in ('.security-7', '.kibana_1', '.ds-.workflows-events-000001', 'unrelated')}}}
            if '_settings' in path:
                names = path.split('/')[1].split(',')
                if any(name.startswith('.') for name in names): raise ValueError('system data stream access is reserved')
                return {name: {'settings': {'index': {'uuid': 'orders-id' if name == 'orders' else state['feature_uid']+name}}} for name in names}
            raise AssertionError((method, path))
        for defect, message in (('realm', 'independent file realm'), ('business', 'isolated target'), ('missing_feature', 'lacks requested nonempty')):
            state[defect] = 'native' if defect == 'realm' else True
            with self.assertRaisesRegex(ValueError, message): es.operation(c, None, request, req)
            self.assertFalse(any(method == 'POST' and '_restore' in path for method, path, _ in calls))
            state[defect] = 'file' if defect == 'realm' else False
        receipt = es.operation(c, None, request, req)
        self.assertEqual({'.security-7', '.kibana_1', '.ds-.workflows-events-000001'}, set(receipt['feature_index_uuids']))
        self.assertTrue(any('/_cluster/health/.ds-.workflows-events-000001,.kibana_1,.security-7,orders?' in path for _, path, _ in calls))
        es.verify_receipt(request, req, receipt)
        state['feature_uid'] = 'replacement'
        with self.assertRaisesRegex(ValueError, 'feature-state index identity'): es.verify_receipt(request, req, receipt)

    def test_feature_uuid_metadata_requires_every_named_index_and_nonempty_identity(self):
        name = '.ds-.workflows-events-000001'
        for indices, message in (({}, 'index set changed'), ({name: {}}, 'UUID is missing'),
                                 ({name: {'settings': {'index': {'uuid': ''}}}}, 'UUID is missing')):
            with self.subTest(indices=indices), self.assertRaisesRegex(ValueError, message):
                es.index_uuids(lambda *args: {'metadata': {'indices': indices}}, [name])

    def test_disable_and_uninstall_include_monitoring_resources_without_deleting_customer_data(self):
        c = configuration()
        objects = { (kind, name, ns): {"metadata": dict(es.meta(c, name), uid='uid-'+name, resourceVersion='7')}
                    for kind, name, ns in es.auxiliary(c) }
        class API:
            def __init__(self): self.deleted = []
            def get(self, kind, name="", namespace=""):
                if kind == "crd": return {"metadata": {"name": name}}
                return objects.get((kind, name, namespace))
            def run(self, *args, **kwargs): self.deleted.append(args)
        api = API()
        with redirect_stdout(StringIO()): es.absent(c, api)
        self.assertEqual(len(api.deleted), len(objects) * 2)
        self.assertTrue(any('servicemonitor/'+name in a and ns in a for a in api.deleted
                            for kind, name, ns in objects if kind == 'servicemonitor'))
        self.assertFalse(any("namespace" in a or "pvc" in a or "secret" in a for a in api.deleted))
        # A foreign object at the last deletion boundary blocks all deletion.
        objects[next(reversed(objects))]["metadata"]["labels"] = {}
        api = API()
        with self.assertRaisesRegex(ValueError, "foreign product resource"): es.absent(c, api)
        self.assertEqual(api.deleted, [])
        objects[next(reversed(objects))]["metadata"] = dict(es.meta(c, 'missing-identity'))
        api = API()
        with self.assertRaisesRegex(ValueError, "current UID and resourceVersion"): es.absent(c, api)
        self.assertEqual(api.deleted, [])

    def test_retirement_passes_uid_and_resource_version_to_the_api_server(self):
        from unittest.mock import Mock
        api = Mock()
        obj = {'metadata': {'uid': 'original', 'resourceVersion': '17'}}
        es.delete_checked(api, 'elasticsearch', 'search', 'business', obj)
        first = api.run.call_args_list[0]
        self.assertEqual(('delete', '--raw', '/apis/elasticsearch.k8s.elastic.co/v1/namespaces/business/elasticsearches/search', '-f', '-'), first.args)
        self.assertEqual({'uid': 'original', 'resourceVersion': '17'}, json.loads(first.kwargs['body'])['preconditions'])
        api.reset_mock(); api.run.side_effect = ValueError('API conflict: replaced resource')
        with self.assertRaisesRegex(ValueError, 'API conflict'):
            es.delete_checked(api, 'elasticsearch', 'search', 'business', obj)
        self.assertEqual(1, api.run.call_count)

    def test_repository_writer_conflicts_and_concurrent_updates_stop_before_s3_writes(self):
        from unittest.mock import Mock
        c = configuration() | {'elasticsearch_s3_secret': 's3'}
        req = {'id': 'archive', 'type': 'repository', 'name': 'archive', 'endpoint': 'https://s3.example.com',
               'bucket': 'backups', 'base_path': 'business/orders', 'region': 'us-east-1'}
        claim = {'namespace': c['elasticsearch_namespace'], 'namespace_uid': 'namespace-uid', 'name': c['elasticsearch_name'],
                 'repository': 'archive', 'bucket': req['bucket'], 'base_path': req['base_path'], 'cluster_uuid': 'data-cluster'}
        for defect in ('none', 'parent', 'child', 'same-path', 'different-cluster', 'replaced-namespace', 'malformed', 'foreign', 'cas'):
            saved = dict(claim)
            if defect in ('parent', 'child', 'same-path'): saved.update(namespace='other', namespace_uid='other-uid')
            if defect == 'parent': saved['base_path'] = 'business'
            if defect == 'child': saved['base_path'] = req['base_path']+'/nested'
            if defect == 'different-cluster': saved['cluster_uuid'] = 'previous-data-cluster'
            if defect == 'replaced-namespace': saved['namespace_uid'] = 'previous-namespace'
            if defect == 'malformed': saved.pop('cluster_uuid')
            claims = [] if defect == 'cas' else [saved]
            ledger = {'metadata': {'uid': 'ledger', 'resourceVersion': '8', 'labels': dict(eck.LABELS)},
                      'data': {'claims': json.dumps(claims)}}
            if defect == 'foreign': ledger['metadata']['labels'] = {}
            api = Mock()
            api.get.side_effect = lambda kind, name='', namespace='': (
                {'metadata': {'uid': 'namespace-uid'}} if kind == 'namespace' else ledger)
            if defect == 'cas': api.run.side_effect = ValueError('conflict')
            request = Mock(return_value={'cluster_uuid': 'data-cluster'})
            with self.subTest(defect=defect):
                if defect == 'none':
                    es.reserve_repository(c, api, req, commit=True, cluster_uuid='data-cluster')
                    api.run.assert_not_called()
                else:
                    with self.assertRaises(ValueError): es.operation(c, api, request, req)
                    self.assertFalse(any(call.args[0] in ('PUT', 'POST', 'DELETE') for call in request.call_args_list))
            if defect == 'cas':
                stored = json.loads(api.run.call_args.kwargs['body'])
                self.assertEqual('replace', api.run.call_args.args[0])
                self.assertEqual('8', stored['metadata']['resourceVersion'])
                self.assertEqual(1, len(stored['data']['claims']) and len(json.loads(stored['data']['claims'])))
        api = Mock()
        api.get.side_effect = lambda kind, name='', namespace='': {'metadata': {'uid': 'namespace-uid'}} if kind == 'namespace' else None
        es.reserve_repository(c, api, req, commit=False)
        api.run.assert_not_called()
        es.reserve_repository(c, api, req, commit=True, cluster_uuid='data-cluster')
        self.assertEqual('create', api.run.call_args.args[0])
        stored = json.loads(api.run.call_args.kwargs['body'])
        self.assertNotIn('secret', stored['data']['claims'])
        api.reset_mock()
        es.reserve_repository(c | {'elasticsearch_repository_role': 'reader'}, api, req, commit=True)
        api.get.assert_not_called(); api.run.assert_not_called()

    def test_snapshot_entry_rechecks_the_registered_repository_before_writing_its_journal(self):
        from contextlib import nullcontext
        from unittest.mock import Mock
        c = configuration() | {'elasticsearch_s3_secret':'s3','elasticsearch_requests':[
            {'id':'backup','type':'snapshot','repository':'archive','snapshot':'backup','indices':['orders']}]}
        api = Mock(); api.get.return_value = None
        for settings in ({'bucket':'backups','base_path':'elasticsearch/logging','endpoint':'https://s3.example.com','region':'us-east-1'},
                         {'readonly':'true'}):
            request = Mock(side_effect=lambda method,path,*args: {'cluster_uuid':'data-cluster'} if path=='/' else {'archive':{'type':'s3','settings':settings}})
            with self.subTest(settings=settings),patch.object(es,'connection',return_value=nullcontext(request)),self.assertRaises(ValueError):
                es.operations(c,api)
            api.apply.assert_not_called();api.run.assert_not_called()

    def test_restore_rechecks_real_repository_including_completed_journal_before_any_mutation(self):
        from contextlib import nullcontext
        from unittest.mock import Mock
        import hashlib
        req = {'id':'restore','type':'restore','repository':'archive','snapshot':'backup','indices':['orders']}
        c = configuration() | {'elasticsearch_s3_secret':'s3','elasticsearch_repository_role':'reader',
            'elasticsearch_requests':[req]}
        for completed in (False,True):
            for fault in ('writable','efk-tree','bad-endpoint'):
                settings = {'readonly':'true','bucket':'backups','base_path':'orders/prod',
                            'endpoint':'https://s3.example.com','region':'us-east-1'}
                if fault == 'writable': settings['readonly']='false'
                elif fault == 'efk-tree': settings['base_path']='elasticsearch/logging'
                else: settings['endpoint']='http://untrusted.example.com'
                api = Mock()
                api.get.return_value = {'metadata':es.meta(c,'request'),'data':{'cluster_uuid':'cluster',
                    'fingerprint':hashlib.sha256(json.dumps(req,sort_keys=True,separators=(',',':')).encode()).hexdigest(),
                    'phase':'complete','receipt':'{}'}} if completed else None
                request = Mock(side_effect=lambda method,path,*args: {'cluster_uuid':'cluster'} if path=='/' else {
                    'archive':{'type':'s3','settings':settings}})
                with self.subTest(completed=completed,fault=fault),patch.object(es,'connection',return_value=nullcontext(request)), \
                        self.assertRaises(ValueError):
                    es.operations(c,api)
                api.apply.assert_not_called();api.run.assert_not_called()
                self.assertFalse(any(call.args[0] in ('PUT','POST','DELETE') for call in request.call_args_list))

    def test_exporter_boolean_arguments_follow_pinned_kingpin_negation_syntax(self):
        # exporter v1.11.0 main.go declares es.aliases as kingpin Bool(),
        # whose v2.4.0 negative complement is --no-<name>.
        objects=es.monitoring(configuration())
        exporter=objects[0]['spec']['template']['spec']['containers'][0]
        self.assertIn('--no-es.aliases',exporter['args'])
        self.assertNotIn('--es.aliases=false',exporter['args'])
        self.assertTrue(all(not value.endswith(('=true','=false')) for value in exporter['args']))

    def test_exporter_rotation_changes_pod_revision_without_embedding_credentials(self):
        c = configuration()
        from contextlib import nullcontext
        class API:
            def __init__(self, version): self.version, self.objects = version, []
            def get(self, *args):
                import base64
                return {"metadata": {"uid": "credential-uid", "resourceVersion": self.version}, "data": {k: base64.b64encode(v.encode()).decode() for k, v in {"username": "metrics", "password": "private-value"}.items()}}
            def apply(self, objects): self.objects = objects
            def run(self, *args, **kwargs): pass
        pods = []
        for revision in ("1", "2"):
            api = API(revision)
            with patch.object(es, "connection", return_value=nullcontext(lambda *args: {})), redirect_stdout(StringIO()): es.enable_monitoring(c, api)
            pods.append(api.objects[0]["spec"]["template"])
            self.assertNotIn("private-value", json.dumps(api.objects))
        self.assertNotEqual(pods[0]["metadata"]["annotations"], pods[1]["metadata"]["annotations"])

    def test_preflight_proves_owner_graph_and_rejects_label_only_pvcs(self):
        api = BusinessAPI()
        # DeleteOnScaledownOnly intentionally removes CR ownerReferences.
        self.assertNotIn("ownerReferences", api.claim["metadata"])
        es.preflight(api.config, api)
        api.pods = []
        with self.assertRaisesRegex(ValueError, "label-only adoption"): es.preflight(api.config, api)
        api.ledger = {"metadata": es.meta(api.config, "search-identity"), "data": {"version_ceiling": "9.5.6", "topology": "compact", "pvc_uids": json.dumps({api.claim["metadata"]["name"]: "pvc-uid"})}}
        es.preflight(api.config, api)
        api.claim["metadata"]["uid"] = "replacement"
        with self.assertRaisesRegex(ValueError, "ownership"): es.preflight(api.config, api)

    def test_retained_data_version_cannot_be_downgraded_after_cr_deletion(self):
        api = BusinessAPI()
        api.cluster = None
        api.ledger = {"metadata": es.meta(api.config, "search-identity"), "data": {"version_ceiling": "9.5.6", "topology": "compact", "pvc_uids": json.dumps({api.claim["metadata"]["name"]: "pvc-uid"})}}
        api.config["elasticsearch_ver"] = "9.5.5"
        with self.assertRaisesRegex(ValueError, "retained data forbids"): es.preflight(api.config, api)

    def test_foreign_namespace_and_external_monitor_collision_refuse_preflight(self):
        api = BusinessAPI()
        api.namespace = {"metadata": {"labels": {}}}
        with self.assertRaisesRegex(ValueError, "foreign business namespace"): es.preflight(api.config, api)
        api = BusinessAPI()
        original = api.get
        def get(kind, name="", namespace=""):
            if kind == "servicemonitor": return {"metadata": {"labels": {}}}
            return original(kind, name, namespace)
        api.get = get
        with self.assertRaisesRegex(ValueError, "foreign product resource"): es.preflight(api.config, api)

    def test_capacity_accounts_for_existing_workloads_outside_business_namespace(self):
        api = BusinessAPI()
        api.pods.append({"metadata": {"namespace": "another"}, "spec": {"nodeName": "data-a", "containers": [{"resources": {"requests": {"memory": "15Gi"}}}]}})
        with self.assertRaisesRegex(ValueError, "allocatable below"): es.preflight(api.config, api)

    def test_capacity_handles_kubernetes_decimal_si_and_exponents_without_ignoring_other_workloads(self):
        # Kubernetes v1.33.6 Quantity accepts DecimalSI, BinarySI and exponents.
        # The same small foreign Pod must be admitted regardless of notation;
        # a large request must still fail for capacity, not parser rejection.
        for memory,cpu in (("100M","100000000n"),("1e8","100000u"),(".1G",".1"),("1E8","100m")):
            with self.subTest(memory=memory,cpu=cpu):
                api=BusinessAPI()
                api.config.update(elasticsearch_data_memory="4Gi", elasticsearch_data_heap="2g")
                pod={"metadata":{"namespace":"another"},"spec":{"nodeName":"data-a",
                     "containers":[{"resources":{"requests":{"memory":memory,"cpu":cpu}}}]}}
                api.pods.append(pod)
                es.preflight(api.config,api)
                pod["spec"]["containers"][0]["resources"]["requests"]["memory"]="16G"
                with self.assertRaisesRegex(ValueError,"allocatable below"):es.preflight(api.config,api)

    def test_business_versions_do_not_upgrade_logging(self):
        kc = KubeConstant()
        self.assertEqual(kc.v_elasticsearch, "9.5.1")
        self.assertIn("brinnatt/elasticsearch:9.5.6", kc.component_images["elasticsearch"])
        self.assertIn("brinnatt/elasticsearch:9.5.1", kc.component_images["logging-efk"])


if __name__ == "__main__":
    unittest.main()
