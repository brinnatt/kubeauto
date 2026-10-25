"""Pinned full-stack admission and monitoring cleanup refusal contracts."""
import copy
import base64
import gzip
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import shutil
import subprocess
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import yaml

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location('v2_monitoring', ROOT/'tests/helpers/longhorn-v2-monitoring.py')
GATE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(GATE)


def node(name, memory='8Gi', taints=None):
    return {'metadata': {'name': name, 'labels': {'kubernetes.io/hostname': name}},
            'spec': {'taints': taints or []}, 'status': {'allocatable': {'cpu': '8', 'memory': memory}}}


def planned(name, memory, group='', pinned=None):
    return {'name': name, 'group': group, 'pinned': pinned,
            'spec': {'containers': [{'resources': {'requests': {'memory': memory, 'cpu': '1'}}}]}}


class NamespaceGraphContracts(unittest.TestCase):
    def test_qualified_reentry_preserves_both_hook_windows_and_refuses_any_release_drift(self):
        lab,row,objects,release=self.fixture()
        GATE.namespace_graph(lab,row)
        second=copy.deepcopy(objects[5]);second['metadata'].update(name='sh.helm.release.v1.prometheus.v2',uid='helm-2')
        next_release=copy.deepcopy(release);next_release['version']=2
        for hook in next_release['hooks']:
            hook['last_run'].update(started_at='2026-10-21T00:01:01Z',completed_at='2026-10-21T00:01:04Z')
        self.encode_release(second,next_release)
        events=[]
        for obj in objects[6:]:
            obj=copy.deepcopy(obj);obj['metadata']['uid']+='-2';obj['metadata']['name']+='-2'
            obj['metadata']['creationTimestamp']='2026-10-21T00:01:02Z'
            obj['involvedObject']['uid']+='-2'
            events.append(obj)
        objects.extend([second]+events)
        with self.assertRaisesRegex(ValueError,'one fresh'):GATE.namespace_graph(lab,row)
        row['qualified_release_history']=sorted([dict(name=o['metadata']['name'],uid=o['metadata']['uid'],
            secret_release_sha256=hashlib.sha256(o['data']['release'].encode()).hexdigest()) for o in (objects[5],second)],key=lambda o:o['name'])
        GATE.namespace_graph(lab,row)
        self.assertEqual(4,len(row['admission_hooks']))
        self.assertEqual(8,len(row['hook_event_subjects']))
        for defect in ('uid','payload','missing','extra'):
            saved=copy.deepcopy(second)
            if defect=='uid':second['metadata']['uid']='replacement'
            if defect=='payload':second['data']['release']+='changed'
            if defect=='missing':objects.remove(second)
            if defect=='extra':
                other=copy.deepcopy(second);other['metadata'].update(uid='extra',name='sh.helm.release.v1.prometheus.v3')
                objects.append(other)
            with self.subTest(defect=defect),self.assertRaisesRegex(ValueError,'history changed'):
                GATE.namespace_graph(lab,row)
            if defect=='missing':objects.append(second)
            if defect=='extra':objects.pop()
            second.clear();second.update(saved)

    def fixture(self, modern_events=False):
        namespace = 'owned'
        row = dict(namespace=namespace, monitor_namespace_uid='ns', run_id='run')
        ns = {'metadata': {'uid': 'ns', 'creationTimestamp': '2026-10-21T00:00:00Z',
                          'labels': {'kubeauto.io/longhorn-monitoring-run': 'run'}}}
        def obj(kind, name, uid, *, helm=False, **fields):
            meta = dict(name=name, uid=uid, namespace=namespace)
            if helm:
                meta['annotations'] = {'meta.helm.sh/release-name': 'prometheus',
                                       'meta.helm.sh/release-namespace': namespace}
            return dict(apiVersion='v1', kind=kind, metadata=meta, **fields)
        name = 'prometheus-kube-prometheus-admission'
        deployment = obj('Deployment', 'prometheus-kube-prometheus-operator-webhook', 'deployment',
                         helm=True, spec={'template': {'spec': {'volumes': [{'secret': {'secretName': name}}]}}})
        service = obj('Service', 'webhook', 'service', helm=True, spec={'selector': {'app': 'webhook'}})
        service['metadata']['labels'] = {'app': 'webhook'}
        pod = obj('Pod', 'webhook-pod', 'pod')
        pod['metadata'].update(labels={'app': 'webhook'}, ownerReferences=[{'uid': 'deployment'}])
        endpoints = obj('Endpoints', 'webhook', 'endpoints', subsets=[{'addresses': [{'targetRef': {
            'kind': 'Pod', 'namespace': namespace, 'name': 'webhook-pod', 'uid': 'pod'}}]}])
        endpoints['metadata']['labels'] = {'app': 'webhook', 'endpoints.kubernetes.io/managed-by': 'endpoint-controller'}
        secret = obj('Secret', name, 'cert', type='Opaque', data={'ca': 'fixture', 'cert': 'fixture', 'key': 'fixture'})
        secret['metadata']['creationTimestamp'] = '2026-10-21T00:00:02Z'
        release = dict(name='prometheus', namespace=namespace, version=1,
                       chart={'metadata': {'name': 'kube-prometheus-stack', 'version': '88.0.0'}}, hooks=[])
        events = []
        for action in ('create', 'patch'):
            hook_name = name+'-'+action
            manifest = obj('Job', hook_name, 'unused', spec={'template': {'spec': {'containers': [{
                'name': action, 'image': 'registry/brinnatt/prometheus-webhook-certgen:1.8.5',
                'args': [action, '--namespace='+namespace, '--secret-name='+name]}]}}})
            manifest['apiVersion'] = 'batch/v1'
            release['hooks'].append(dict(name=hook_name, kind='Job', manifest=yaml.safe_dump(manifest),
                delete_policies=['hook-succeeded'], last_run={'phase': 'Succeeded',
                'started_at': '2026-10-21T00:00:01.150738812Z', 'completed_at': '2026-10-21T00:00:04.270860179Z'}))
            for kind, reason, source, subject_name, subject_uid, message in [
                ('Job', 'SuccessfulCreate', 'job-controller', hook_name, action+'-job', 'Created pod: '+hook_name+'-abc'),
                ('Job', 'Completed', 'job-controller', hook_name, action+'-job', 'Job completed'),
                ('Pod', 'Scheduled', 'default-scheduler', hook_name+'-abc', action+'-pod', 'Scheduled'),
                ('Pod', 'Started', 'kubelet', hook_name+'-abc', action+'-pod', 'Started')]:
                event = obj('Event', action+'-'+reason, action+'-'+reason,
                    involvedObject=dict(apiVersion='batch/v1' if kind == 'Job' else 'v1', kind=kind,
                                        name=subject_name, namespace=namespace, uid=subject_uid),
                    reason=reason, source={'component': source}, message=message, type='Normal')
                event['metadata']['creationTimestamp'] = '2026-10-21T00:00:02Z'
                if modern_events:
                    event['apiVersion'] = 'events.k8s.io/v1'
                    event['regarding'] = event.pop('involvedObject')
                    event['reportingController'] = event.pop('source')['component']
                    event['note'] = event.pop('message')
                events.append(event)
        helm = obj('Secret', 'sh.helm.release.v1.prometheus.v1', 'helm', type='helm.sh/release.v1')
        helm['metadata']['labels'] = {'owner': 'helm', 'name': 'prometheus'}
        objects = [deployment, service, pod, endpoints, secret, helm]+events
        self.encode_release(helm, release)
        lab = SimpleNamespace(DIRECTORY=Path('/fixture'), write_private=Mock(),
                              kubectl=Mock(return_value=SimpleNamespace(stdout='objects')))
        lab.get = Mock(side_effect=lambda kind,*args,**kw: ns if kind == 'namespace' else {'items': objects})
        return lab, row, objects, release

    @staticmethod
    def encode_release(helm, release):
        helm['data'] = {'release': base64.b64encode(base64.b64encode(gzip.compress(
            json.dumps(release).encode()))).decode()}

    def test_controller_generated_objects_and_deleted_hook_events_have_typed_ownership_in_both_apis(self):
        for modern in (False, True):
            with self.subTest(modern=modern):
                lab,row,objects,_=self.fixture(modern)
                self.assertEqual(len(objects),len(GATE.namespace_graph(lab,row)))
                self.assertEqual('cert',row['admission_secret_uid'])
                self.assertEqual({'create-job','patch-job','create-pod','patch-pod'},set(row['hook_event_subjects']))
                # Helm removes its release/workloads first; captured original
                # Secret and historical events can still be scoped for cleanup.
                objects[:] = [o for o in objects if o['kind'] == 'Event' or o['metadata']['uid'] == 'cert']
                self.assertEqual(len(objects),len(GATE.namespace_graph(lab,row)))

    def test_endpoints_reject_foreign_same_name_wrong_controller_selector_labels_and_owner_uids(self):
        for defect in ('foreign-service','controller','selector','labels','pod-uid','pod-namespace','pod-kind','owner'):
            with self.subTest(defect=defect):
                lab,row,objects,_=self.fixture()
                service=objects[1]; endpoint=objects[3]
                if defect == 'foreign-service': service['metadata'].pop('annotations')
                elif defect == 'controller': endpoint['metadata']['labels']['endpoints.kubernetes.io/managed-by']='foreign'
                elif defect == 'selector': service['spec']['selector']={}
                elif defect == 'labels': endpoint['metadata']['labels']['app']='foreign'
                elif defect == 'owner': endpoint['metadata']['ownerReferences']=[{'uid':'deployment'}]
                else: endpoint['subsets'][0]['addresses'][0]['targetRef'][{'pod-uid':'uid','pod-namespace':'namespace','pod-kind':'kind'}[defect]]='foreign'
                with self.assertRaises(ValueError): GATE.namespace_graph(lab,row)
                lab.write_private.assert_not_called()

    def test_historical_hook_events_refuse_unknown_uid_name_namespace_kind_source_reason_and_time(self):
        for defect in ('uid','name','namespace','kind','apiVersion','source','reason','timestamp','unknown-pod'):
            with self.subTest(defect=defect):
                lab,row,objects,_=self.fixture()
                event=objects[7]
                if defect in ('uid','name','namespace','kind','apiVersion'): event['involvedObject'][defect]='foreign'
                elif defect == 'source': event['source']['component']='foreign'
                elif defect == 'reason': event['reason']='foreign'
                elif defect == 'timestamp': event['metadata']['creationTimestamp']='2026-10-20T00:00:00Z'
                else: objects[8]['involvedObject']['name']='prometheus-kube-prometheus-admission-create-unrecorded'
                with self.assertRaises(ValueError): GATE.namespace_graph(lab,row)
                lab.write_private.assert_not_called()

    def test_admission_secret_requires_exact_shape_run_window_consumer_and_captured_uid(self):
        for defect in ('type','keys','owner','time','consumer','replacement'):
            with self.subTest(defect=defect):
                lab,row,objects,_=self.fixture(); secret=objects[4]
                if defect == 'type': secret['type']='kubernetes.io/tls'
                elif defect == 'keys': secret['data'].pop('ca')
                elif defect == 'owner': secret['metadata']['ownerReferences']=[{'uid':'foreign'}]
                elif defect == 'time': secret['metadata']['creationTimestamp']='2026-10-20T00:00:00Z'
                elif defect == 'consumer': objects[0]['spec']['template']['spec']['volumes']=[]
                else: row['admission_secret_uid']='original'
                with self.assertRaises(ValueError): GATE.namespace_graph(lab,row)
                lab.write_private.assert_not_called()

    def test_release_identity_hook_manifest_command_and_runtime_are_not_name_allowlists(self):
        for defect in ('namespace','chart','hook-namespace','command','phase','time','corrupt','revision','multiple-revisions'):
            with self.subTest(defect=defect):
                lab,row,objects,release=self.fixture()
                if defect == 'namespace': release['namespace']='foreign'
                elif defect == 'chart': release['chart']['metadata']['version']='foreign'
                elif defect in ('hook-namespace','command'):
                    manifest=yaml.safe_load(release['hooks'][0]['manifest'])
                    if defect == 'hook-namespace': manifest['metadata']['namespace']='foreign'
                    else: manifest['spec']['template']['spec']['containers'][0]['args'][0]='foreign'
                    release['hooks'][0]['manifest']=yaml.safe_dump(manifest)
                elif defect == 'phase': release['hooks'][0]['last_run']['phase']='Failed'
                elif defect == 'time': release['hooks'][0]['last_run']['started_at']='2026-10-20T00:00:00Z'
                elif defect == 'revision': release['version']=2
                self.encode_release(objects[5],release)
                if defect == 'corrupt': objects[5]['data']['release']='malformed'
                elif defect == 'multiple-revisions':
                    other=copy.deepcopy(objects[5]);other['metadata'].update(name='sh.helm.release.v1.prometheus.v2',uid='other-release')
                    objects.append(other)
                with self.assertRaises(ValueError): GATE.namespace_graph(lab,row)
                lab.write_private.assert_not_called()


class MonitoringContracts(unittest.TestCase):
    def test_requests_do_not_sum_limits_or_double_count_initialization(self):
        pod = {'containers': [{'resources': {'requests': {'memory': '4Gi', 'cpu': '1'},
                                            'limits': {'memory': '8Gi', 'cpu': '4'}}},
                              {'resources': {'requests': {'memory': '100Mi', 'cpu': '100m'}}}],
               'initContainers': [{'resources': {'requests': {'memory': '1Gi', 'cpu': '2'}}}],
               'overhead': {'memory': '10Mi'}}
        result = GATE.requests(pod)
        self.assertEqual(4*1024**3+110*1024**2, result['memory'])
        self.assertEqual(2, result['cpu'])

    def test_limit_without_request_is_scheduler_default_but_sidecar_init_is_refused(self):
        self.assertEqual(1024**3, GATE.requests({'containers': [{'resources': {'limits': {'memory': '1Gi'}}}]})['memory'])
        with self.assertRaises(ValueError):
            GATE.requests({'initContainers': [{'restartPolicy': 'Always'}]})

    def test_exact_placement_preserves_prometheus_and_alertmanager_anti_affinity(self):
        nodes = [node(str(i), '6Gi') for i in range(3)]
        rows = [planned('prom-'+str(i), '4Gi', 'prom') for i in range(2)]
        rows += [planned('alert-'+str(i), '256Mi', 'alert') for i in range(3)]
        result = GATE.placement(nodes, [], rows)
        self.assertEqual(2, len({result['placement']['prom-'+str(i)] for i in range(2)}))
        self.assertEqual(3, len({result['placement']['alert-'+str(i)] for i in range(3)}))

    def test_aggregate_memory_does_not_prove_anti_affinity_capacity(self):
        with self.assertRaisesRegex(ValueError, 'does not fit'):
            GATE.placement([node('a', '12Gi'), node('b', '3Gi'), node('c', '3Gi')], [],
                           [planned('p0', '4Gi', 'prom'), planned('p1', '4Gi', 'prom')])

    def test_current_requests_and_pending_workloads_are_admission_inputs(self):
        pods = [{'spec': {'nodeName': 'a', 'containers': [{'resources': {'requests': {'memory': '4Gi'}}}]}}]
        with self.assertRaises(ValueError):
            GATE.placement([node('a', '6Gi')], pods, [planned('p', '4Gi')])
        with self.assertRaisesRegex(ValueError, 'pending'):
            GATE.placement([node('a')], [{'spec': {}}], [])
        GATE.placement([node('a')], [{'spec': {}, 'status': {'phase': 'Succeeded'}}], [])

    def test_taints_and_node_selectors_cannot_be_silently_ignored(self):
        nodes = [node('a', taints=[{'key': 'control', 'effect': 'NoSchedule'}])]
        with self.assertRaises(ValueError):
            GATE.placement(nodes, [], [planned('p', '1Gi')])
        row = planned('p', '1Gi'); row['spec']['tolerations'] = [{'key': 'control', 'operator': 'Exists'}]
        self.assertEqual('a', GATE.placement(nodes, [], [row])['placement']['p'])
        row['spec']['nodeSelector'] = {'kubernetes.io/hostname': 'foreign'}
        with self.assertRaises(ValueError):
            GATE.placement(nodes, [], [row])

    def test_pinned_chart_crd_bare_equals_null_annotations_and_test_hooks_parse(self):
        docs = GATE.render_documents('''kind: CustomResourceDefinition
metadata:
  name: rules.example
  annotations: null
spec:
  enum:
    - =
---
kind: Pod
metadata:
  name: test
  annotations:
    helm.sh/hook: test
''')
        self.assertEqual(1, len(docs))
        self.assertEqual(['='], docs[0]['spec']['enum'])
        with self.assertRaises(yaml.constructor.ConstructorError):
            GATE.render_documents('!!python/object:danger {}')

    def test_generated_operator_pods_and_all_three_daemonset_instances_are_budgeted(self):
        docs = [{'kind': kind, 'metadata': {'name': kind}, 'spec': {'replicas': n, 'image': 'pinned',
                 'resources': {'requests': {'memory': mem}}, 'podAntiAffinity': 'hard'}}
                for kind, n, mem in [('Prometheus', 2, '4Gi'), ('Alertmanager', 3, '256Mi')]]
        docs.append({'kind': 'DaemonSet', 'metadata': {'name': 'exporter'},
                     'spec': {'template': {'spec': {'containers': [{}]}}}})
        pods = GATE.planned_pods(docs, [node(str(i)) for i in range(3)])
        self.assertEqual(8, len(pods))
        self.assertEqual(4*1024**3+100*1024**2, GATE.requests(pods[0]['spec'])['memory'])
        docs[0]['spec']['replicas'] = 1
        with self.assertRaisesRegex(ValueError, 'two Prometheus'):
            GATE.planned_pods(docs, [node('a')])

    def test_real_operator_cr_affinity_is_enforced_without_helm_values_only_field(self):
        docs = [{'kind': kind, 'metadata': {'name': kind}, 'spec': {'replicas': n, 'image': 'pinned',
            'resources': {'requests': {'memory': '256Mi'}}, 'affinity': {'podAntiAffinity': {
                'requiredDuringSchedulingIgnoredDuringExecution': [{'topologyKey': 'kubernetes.io/hostname'}]}}}}
            for kind, n in [('Prometheus', 2), ('Alertmanager', 3)]]
        nodes = [node(str(i)) for i in range(3)]
        result = GATE.placement(nodes, [], GATE.planned_pods(docs, nodes))
        self.assertEqual(3, len({result['placement']['Alertmanager-'+str(i)] for i in range(3)}))
        docs[1]['spec']['affinity']['podAntiAffinity']['requiredDuringSchedulingIgnoredDuringExecution'][0]['topologyKey'] = 'region'
        with self.assertRaises(ValueError): GATE.planned_pods(docs, nodes)

    def test_node_exporter_actual_required_linux_affinity_is_evaluated(self):
        spec = {'affinity': {'nodeAffinity': {'requiredDuringSchedulingIgnoredDuringExecution': {
            'nodeSelectorTerms': [{'matchExpressions': [{'key': 'kubernetes.io/os', 'operator': 'In', 'values': ['linux']}]}]}}}}
        current = node('a')
        self.assertFalse(GATE.node_allowed(spec, current))
        current['metadata']['labels']['kubernetes.io/os'] = 'linux'
        self.assertTrue(GATE.node_allowed(spec, current))

    def test_capacity_uses_real_raw_disk_counters_and_preserves_fifty_gib_worst_node_budget(self):
        disk = {'metadata': {'name': 'a'}, 'spec': {'disks': {'raw': {'diskType': 'block', 'allowScheduling': True,
            'storageReserved': 1024**3}}}, 'status': {'diskStatus': {'raw': {'diskUUID': 'uuid',
                'storageMaximum': 100*1024**3, 'storageAvailable': 90*1024**3, 'storageScheduled': 0}}}}
        def get(kind, *a, **kw):
            return {'value': '25'} if kind == 'settings.longhorn.io' else {'items': [dict(copy.deepcopy(disk), metadata={'name': str(i)}) for i in range(3)]}
        lab = SimpleNamespace(get=Mock(side_effect=get))
        self.assertEqual(3, len(GATE.storage_budget(lab)))
        disk['status']['diskStatus']['raw']['storageAvailable'] = 70*1024**3
        with self.assertRaisesRegex(ValueError, 'raw-disk budget'):
            GATE.storage_budget(lab)

    def test_missing_stale_future_and_nonfinite_prometheus_samples_are_not_zero(self):
        good = {'status': 'success', 'data': {'resultType': 'vector', 'result': []}}
        with self.assertRaisesRegex(ValueError, 'missing'):
            GATE.sample_vector(good, 100)
        self.assertEqual([], GATE.sample_vector(good, 100, allow_empty=True))
        for timestamp, value in ((0, '1'), (101, '1'), (100, 'NaN'), (100, '+Inf')):
            row = copy.deepcopy(good); row['data']['result'] = [{'metric': {}, 'value': [timestamp, value]}]
            with self.assertRaises(ValueError):
                GATE.sample_vector(row, 100)
        good['data']['result'] = [{'metric': {'state': 'degraded'}, 'value': [100, '1']}]
        self.assertEqual(1., GATE.sample_vector(good, 100)[0]['value'])

    def test_desired_product_keeps_existing_classes_and_ha_resources(self):
        baseline = yaml.safe_load((ROOT/'conf/config.yml').read_text())
        baseline.update(longhorn_data_engines=['v2'])
        raw = yaml.safe_dump(baseline)
        desired = GATE.desired(raw, 'owned')
        self.assertEqual(baseline['longhorn_storage_classes'], desired['longhorn_storage_classes'][:-1])
        fixture = desired['longhorn_storage_classes'][-1]
        self.assertEqual((1, 'Delete', 'v2'), (fixture['replicas'], fixture['reclaim_policy'], fixture['data_engine']))
        self.assertEqual('88.0.0', desired['prom_chart_ver'])
        self.assertTrue(desired['longhorn_monitoring_enabled'])
        for defect in ('prom_install', 'longhorn_requests', 'longhorn_monitoring_enabled'):
            bad = copy.deepcopy(baseline)
            bad[defect] = {'prom_install': 'yes', 'longhorn_requests': [{}], 'longhorn_monitoring_enabled': True}[defect]
            with self.assertRaises(ValueError):
                GATE.desired(yaml.safe_dump(bad), 'owned')

    def test_namespace_replacement_blocks_helm_and_deletion(self):
        row = {'namespace': 'owned', 'monitor_namespace_uid': 'original', 'run_id': 'run'}
        lab = SimpleNamespace(get=Mock(return_value={'metadata': {'uid': 'replacement',
                  'labels': {'kubeauto.io/longhorn-monitoring-run': 'run'}}}), kubectl=Mock(), execute=Mock())
        with self.assertRaises(ValueError):
            GATE.namespace_graph(lab, row)
        lab.execute.assert_not_called(); lab.kubectl.assert_not_called()

    def test_foreign_namespace_object_prevents_cleanup_graph_acceptance(self):
        row = {'namespace': 'owned', 'monitor_namespace_uid': 'ns', 'run_id': 'run'}
        ns = {'metadata': {'uid': 'ns', 'labels': {'kubeauto.io/longhorn-monitoring-run': 'run'}}}
        lab = SimpleNamespace(kubectl=Mock(return_value=SimpleNamespace(stdout='configmaps')))
        lab.get = Mock(side_effect=lambda kind, *a, **k: ns if kind == 'namespace' else
                       {'items': [{'kind': 'ConfigMap', 'metadata': {'name': 'customer', 'uid': 'foreign'}}]})
        with self.assertRaisesRegex(ValueError, 'foreign object'):
            GATE.namespace_graph(lab, row)

    def test_cache_restores_original_publication_function_after_failure(self):
        evidence = json.loads((ROOT/'tests/evidence/longhorn-prometheus-images-20261021.json').read_text())
        publications = Mock(return_value=([{'key': 'base'}], [{'key': 'support'}]))
        lab = SimpleNamespace(BASE=ROOT, publication_records=publications)
        def fail(**kw):
            production, support = lab.publication_records()
            self.assertEqual(12, len(production)); self.assertEqual([{'key': 'support'}], support)
            self.assertEqual({r['key'] for r in evidence['images']}, kw['keys'])
            raise ValueError('cache outage')
        lab.cache_artifacts = Mock(side_effect=fail)
        with self.assertRaisesRegex(ValueError, 'cache outage'):
            GATE.cache(lab)
        self.assertIs(publications, lab.publication_records)

    def test_cache_shared_artifacts_use_product_root_and_restore_function(self):
        spec = importlib.util.spec_from_file_location('monitoring_cache_lab', ROOT/'tests/helpers/longhorn-lab.py')
        lab = importlib.util.module_from_spec(spec); spec.loader.exec_module(lab)
        lab.BASE = ROOT
        original = lab.publication_records
        production, support = original()
        before = copy.deepcopy((production, support))
        publications = Mock(return_value=(production, support))
        lab.publication_records = publications
        evidence = json.loads((ROOT/'tests/evidence/longhorn-prometheus-images-20261021.json').read_text())
        keys = {row['key'] for row in evidence['images']}
        shared = keys & {row['key'] for row in support}
        self.assertEqual({'brinnatt/prometheus:v3.13.1-distroless', 'brinnatt/kube-state-metrics:v2.18.0'}, shared)
        manifest = {'schemaVersion': 2, 'mediaType': 'application/vnd.oci.image.manifest.v1+json',
                    'config': {'digest': 'sha256:'+'1'*64, 'size': 1}, 'layers': []}
        execute = Mock(return_value=SimpleNamespace(returncode=0, stdout=json.dumps(manifest), stderr=''))
        with patch.object(lab, 'owner'), patch.object(lab, 'execute', execute), patch.object(
                lab, 'published_manifest', return_value=manifest):
            GATE.cache(lab)
        destinations = [call.args[0][-1] for call in execute.call_args_list]
        self.assertEqual({'docker://127.0.0.1:5000/'+key for key in keys}, set(destinations))
        self.assertEqual(11, len(destinations))
        self.assertFalse(any('/longhorn-lab/' in path for path in destinations))
        self.assertIs(publications, lab.publication_records)
        self.assertEqual(before, (production, support))

    def test_cache_shared_artifact_identity_conflict_refused_before_execution(self):
        evidence = json.loads((ROOT/'tests/evidence/longhorn-prometheus-images-20261021.json').read_text())
        pinned = evidence['images'][0]
        changes = {'digest': 'sha256:'+'0'*64, 'source_digest': 'sha256:'+'0'*64,
            'platforms': dict(pinned['platforms'], **{'linux/arm64': 'sha256:'+'0'*64}),
            'talkedu_ref': 'hub.talkedu.cn/kubeauto/foreign:tag', 'dockerhub_ref': 'brinnatt/foreign:tag',
            'source_ref': 'quay.io/foreign:tag', 'fingerprint': '0'*64}
        for field, changed in changes.items():
            for group in ('production', 'support'):
                with self.subTest(field=field, group=group):
                    row = copy.deepcopy(pinned); row[field] = changed
                    publications = Mock(return_value=([row], []) if group == 'production' else ([], [row]))
                    lab = SimpleNamespace(BASE=ROOT, publication_records=publications)
                    executed = Mock()
                    def cache(**kwargs):
                        lab.publication_records(); executed()
                    lab.cache_artifacts = cache
                    with self.assertRaisesRegex(ValueError, 'conflicting monitoring artifact identity'):
                        GATE.cache(lab)
                    executed.assert_not_called()
                    self.assertIs(publications, lab.publication_records)

    def test_cache_shared_artifacts_restore_original_roles_after_cache_outage(self):
        evidence = json.loads((ROOT/'tests/evidence/longhorn-prometheus-images-20261021.json').read_text())
        support = [copy.deepcopy(evidence['images'][0])]
        publications = Mock(return_value=([], support))
        lab = SimpleNamespace(BASE=ROOT, publication_records=publications)
        def fail(**kwargs):
            production, selected_support = lab.publication_records()
            self.assertEqual(11, len(production)); self.assertEqual([], selected_support)
            raise ValueError('scoped cache outage')
        lab.cache_artifacts = fail
        with self.assertRaisesRegex(ValueError, 'scoped cache outage'):
            GATE.cache(lab)
        self.assertIs(publications, lab.publication_records)
        self.assertEqual([evidence['images'][0]], support)

    def test_changed_input_or_symlink_blocks_atomic_product_write(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d); (base/'config.yml').write_text('foreign: true\n')
            lab = SimpleNamespace(DIRECTORY=base, write_private=Mock())
            with self.assertRaises(ValueError):
                GATE.apply(lab, {'allowed_inputs': ['original']}, {})
            lab.write_private.assert_not_called()

    def test_prometheus_pod_replacement_during_api_query_is_rejected(self):
        pod = {'metadata': {'name': 'prom-0', 'uid': 'original'}}
        lab = SimpleNamespace(kubectl=Mock(return_value=SimpleNamespace(stdout='{}')),
                              get=Mock(return_value={'metadata': {'uid': 'replacement'}}))
        prom = GATE.Prometheus(lab, {'namespace': 'owned'})
        with patch.object(prom, 'pods', return_value=[pod]):
            with self.assertRaisesRegex(ValueError, 'changed during observation'):
                prom.api('/api/v1/query', {'query': 'up'})
        self.assertIn('/pods/prom-0:9090/proxy/api/v1/query?query=up', lab.kubectl.call_args.args[1])

    def run_shell(self, mode, fail='none'):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            for name in ('.venv/bin', 'kube-bin', 'tests/helpers'):
                (base/name).mkdir(parents=True)
            script = (ROOT/'tests/helpers/longhorn-v2-monitoring.sh').read_text().replace('/usr/local/kubeauto', str(base))
            script = script.replace('/var/tmp/kubeauto-longhorn-v2-monitoring.', str(base/'run.'))
            (base/'runner.sh').write_text(script)
            shutil.copyfile(ROOT/'tests/helpers/run-durable-gate.sh', base/'tests/helpers/run-durable-gate.sh')
            fake = base/'.venv/bin/python'
            fake.write_text('''#!/usr/bin/env bash
set -eu
echo "${1##*/} ${2:-}" >> "$MON_TRACE"
if [[ "${2:-}" == relay-maintain ]]; then
  exec "$MON_REAL_PY" -c 'import os,signal,time; from pathlib import Path; signal.signal(signal.SIGTERM,lambda *a:exit(0)); Path(os.environ["MON_READY"]).touch(); time.sleep(60)'
fi
if [[ "${2:-}" == setup || "${2:-}" == cleanup ]]; then
  "$MON_REAL_PY" -c 'import os,time; from pathlib import Path; p=Path(os.environ["MON_READY"]); start=time.monotonic()
while not p.exists():
 assert time.monotonic()-start<3
 time.sleep(.01)'
fi
[[ "${2:-}" != "$MON_FAIL" ]] || exit 17
if [[ "${2:-}" == setup ]]; then echo LONGHORN_V2_MONITORING_SETUP_PASS; fi
''')
            fake.chmod(0o755)
            kubectl = base/'kube-bin/kubectl'; kubectl.write_text('#!/usr/bin/env bash\nexit 0\n'); kubectl.chmod(0o755)
            env = dict(os.environ, LONGHORN_PARENT_LEASE_HELD='yes', MON_TRACE=str(base/'trace'),
                       MON_REAL_PY=sys.executable, MON_FAIL=fail, MON_READY=str(base/'relay-ready'))
            result = subprocess.run(['bash', str(base/'runner.sh'), mode], env=env, capture_output=True, text=True, timeout=10)
            trace = (base/'trace').read_text() if (base/'trace').exists() else ''
            return result, trace

    def test_runner_focused_setup_keeps_healthy_owner_without_claiming_clean_or_full_pass(self):
        result, trace = self.run_shell('v2-monitoring-setup')
        self.assertEqual(0, result.returncode, result.stdout+result.stderr)
        self.assertIn('LONGHORN_V2_MONITORING_BASELINE_READY', result.stdout)
        self.assertNotIn('longhorn-v2-monitoring.py cleanup', trace)
        self.assertNotIn('LONGHORN_V2_MONITORING_SCOPED_CLEAN_PASS', result.stdout)

    def test_runner_full_boundary_cleans_monitoring_before_terminal_pass(self):
        result, trace = self.run_shell('v2-monitoring')
        self.assertEqual(0, result.returncode, result.stdout+result.stderr)
        self.assertEqual(1, trace.count('longhorn-v2-monitoring.py cleanup'))
        self.assertIn('LONGHORN_V2_MONITORING_PASS', result.stdout)

    def test_runner_preserves_first_failure_and_cleans_before_relay_teardown(self):
        result, trace = self.run_shell('v2-monitoring-setup', fail='setup')
        self.assertEqual(17, result.returncode)
        self.assertIn('LONGHORN_FIRST_FAILURE phase=monitoring-product-setup rc=17', result.stderr)
        self.assertIn('longhorn-v2-monitoring.py cleanup', trace)
        self.assertNotIn('LONGHORN_V2_MONITORING_BASELINE_READY', result.stdout)

    def test_runner_refuses_unknown_mode_before_any_remote_operation(self):
        result, trace = self.run_shell('unknown')
        self.assertEqual(2, result.returncode)
        self.assertFalse(trace)


if __name__ == '__main__':
    unittest.main()
