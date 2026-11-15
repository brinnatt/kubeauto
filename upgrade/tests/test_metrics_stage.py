"""Exercise actual product template and MetricsStage subprocess/API handoffs."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from jinja2 import Environment, StrictUndefined
import yaml

from upgrade.product.metrics_stage import (API, OBJECTS, SOURCE, TARGET, WORKLOAD,
    MetricsStage, MetricsRefused, MetricsNotReady, MetricsCommandFailed, sample_time, usage)


ROOT = Path(__file__).resolve().parents[2]


def product_objects():
    template = ROOT/'roles/cluster-addon/templates/metrics-server/components.yaml.j2'
    text = Environment(undefined=StrictUndefined).from_string(template.read_text()).render(metricsVer='v0.8.0')
    result = {}
    for obj in yaml.safe_load_all(text):
        ref = obj['kind'].lower()+'/'+obj['metadata']['name']
        obj['metadata'].update(uid=ref+'-uid', resourceVersion='1', generation=1)
        if obj['kind'] == 'Deployment':
            obj['spec']['replicas'] = 1
            obj['status'] = dict(observedGeneration=1, replicas=1, updatedReplicas=1,
                                 readyReplicas=1, availableReplicas=1)
        elif obj['kind'] == 'APIService':
            obj['status'] = {'conditions': [{'type': 'Available', 'status': 'True'}]}
        result[ref] = obj
    return result


class MetricsStageContracts(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        binary = self.root/'kubectl'
        binary.write_text('#!'+sys.executable+'\n'+
            (ROOT/'upgrade/tests/fixtures/metrics_kubectl.py').read_text())
        binary.chmod(0o700)
        (self.root/'kubeconfig').write_text('offline TLS fixture')
        self.source = {'source': 'v1.33.6', 'target': 'v1.34.13', 'mutation': 'none',
            'cluster_identity': {'namespace': 'kube-system', 'uid': 'cluster-uid'},
            'nodes': [{'name': 'node1', 'uid': 'node-uid'}],
            'api_backends': [{'server': 'https://master:6443'}]}
        self.pods = {'business/ledger': {'uid': 'pod-uid', 'node': 'node1', 'containers': ['ledger', 'sidecar']}}
        self.progress = []
        self.stage = MetricsStage(binary, self.root/'kubeconfig', self.source, self.pods,
                                  timeout=30, progress=self.progress.append)
        self.reviewed = product_objects()
        stamp = (datetime.now(timezone.utc)-timedelta(seconds=30)).isoformat().replace('+00:00', 'Z')
        sample = {'timestamp': stamp, 'window': '15s'}
        self.state = {'objects': deepcopy(self.reviewed),
            'nodes': [{'metadata': {'name': 'node1', 'uid': 'node-uid'},
                'status': {'nodeInfo': {'kubeletVersion': 'v1.34.13'},
                           'conditions': [{'type': 'Ready', 'status': 'True'}]}}],
            'pod': {'metadata': {'name': 'ledger', 'namespace': 'business', 'uid': 'pod-uid'},
                    'spec': {'nodeName': 'node1', 'containers': [{'name': 'ledger'}, {'name': 'sidecar'}]},
                    'status': {'phase': 'Running', 'conditions': [{'type': 'Ready', 'status': 'True'}]}},
            'nodes_metrics': {'apiVersion': 'metrics.k8s.io/v1beta1', 'kind': 'NodeMetricsList',
                'items': [{**sample, 'metadata': {'name': 'node1'}, 'usage': {'cpu': '314159n', 'memory': '2Gi'}}]},
            'pods_metrics': {'apiVersion': 'metrics.k8s.io/v1beta1', 'kind': 'PodMetricsList',
                'items': [{**sample, 'metadata': {'namespace': 'business', 'name': 'ledger'},
                           'containers': [{'name': name, 'usage': {'cpu': '0', 'memory': '32Mi'}}
                                          for name in ['ledger', 'sidecar']]}]}}

    def save(self):
        (self.root/'state.json').write_text(json.dumps(self.state))

    def run_stage(self):
        self.save()
        return self.stage.run(self.reviewed)

    def calls(self):
        path = self.root/'calls.jsonl'
        return [json.loads(s) for s in path.read_text().splitlines()] if path.exists() else []

    def writes(self):
        return [c for c in self.calls() if c[0] != 'get' and '--dry-run=server' not in c]

    def test_actual_template_edge_is_image_only_dry_run_first_and_real_node_and_pod_samples_required(self):
        receipt = self.run_stage()
        self.assertEqual(set(self.reviewed), OBJECTS)
        self.assertEqual(len(self.writes()), 1)
        patches = [c for c in self.calls() if c[0] == 'patch']
        self.assertEqual(len(patches), 2)
        self.assertIn('--dry-run=server', patches[0])
        live = json.loads((self.root/'state.json').read_text())['objects']
        live[WORKLOAD]['metadata'].update(resourceVersion='1', generation=1)
        live[WORKLOAD]['status']['observedGeneration'] = 1
        live[WORKLOAD]['spec']['template']['spec']['containers'][0]['image'] = SOURCE
        self.assertEqual(live, self.reviewed)
        self.assertFalse(receipt['parent_acceptance'])
        self.assertEqual(set(receipt['samples']), {'nodes/node1', 'pods/business/ledger'})
        self.assertEqual(receipt['image'], TARGET)
        self.assertEqual(self.progress[-1], 'UPGRADE_METRICS_API_VERIFIED nodes=1 pods=1')

    def test_source_platform_or_cluster_identity_refuses_without_writes(self):
        for key in ['api', 'cluster_uid']:
            with self.subTest(key=key):
                self.state[key] = 'foreign'
                with self.assertRaises(MetricsRefused):
                    self.run_stage()
                self.assertEqual(self.writes(), [])
                del self.state[key]
        self.state['nodes'][0]['spec'] = {'unschedulable': True}
        with self.assertRaises(MetricsRefused):
            self.run_stage()
        self.assertEqual(self.writes(), [])

    def test_reviewed_role_service_uid_annotations_or_source_args_drift_refuses_without_writes(self):
        for ref in ['clusterrole/system:metrics-server', 'service/metrics-server', WORKLOAD]:
            with self.subTest(ref=ref):
                original = deepcopy(self.state['objects'][ref])
                self.state['objects'][ref]['metadata']['annotations'] = {'foreign': 'owner'}
                with self.assertRaises(MetricsRefused):
                    self.run_stage()
                self.assertEqual(self.writes(), [])
                self.state['objects'][ref] = original
        self.reviewed[WORKLOAD]['spec']['template']['spec']['containers'][0]['args'].remove('--metric-resolution=15s')
        with self.assertRaises(MetricsRefused):
            self.run_stage()
        self.assertEqual(self.writes(), [])

    def test_empty_node_or_pod_metrics_is_not_source_readiness(self):
        for resource in ['nodes', 'pods']:
            with self.subTest(resource=resource):
                original = deepcopy(self.state[resource+'_metrics'])
                self.state[resource+'_metrics']['items'] = []
                with self.assertRaises(MetricsNotReady):
                    self.run_stage()
                self.assertEqual(self.writes(), [])
                self.state[resource+'_metrics'] = original

    def test_missing_container_usage_or_incorrect_api_version_refuses_without_patch(self):
        for mutation in ['container', 'usage', 'api']:
            with self.subTest(mutation=mutation):
                original = deepcopy(self.state['pods_metrics'])
                if mutation == 'container':
                    self.state['pods_metrics']['items'][0]['containers'].pop()
                elif mutation == 'usage':
                    del self.state['pods_metrics']['items'][0]['containers'][0]['usage']['memory']
                else:
                    self.state['pods_metrics']['apiVersion'] = 'v1'
                with self.assertRaises(MetricsRefused):
                    self.run_stage()
                self.assertEqual(self.writes(), [])
                self.state['pods_metrics'] = original

    def test_pod_replacement_during_metrics_read_is_detected_before_patch(self):
        self.state['pod_race'] = True
        with self.assertRaisesRegex(MetricsRefused, 'Pod identity'):
            self.run_stage()
        self.assertEqual(self.writes(), [])

    def test_server_dry_run_mutation_is_refused_without_actual_patch(self):
        self.state['dry_run_mutation'] = True
        with self.assertRaisesRegex(MetricsRefused, 'dry-run'):
            self.run_stage()
        self.assertEqual(self.writes(), [])

    def test_conflict_keeps_real_rc_and_has_no_rollback_or_success(self):
        self.state['conflict'] = True
        with self.assertRaises(MetricsCommandFailed) as error:
            self.run_stage()
        self.assertEqual(error.exception.rc, 42)
        self.assertEqual(len(self.writes()), 1)
        self.assertEqual(self.progress, [])

    def test_post_patch_drift_stops_without_repair_or_success(self):
        self.state['post_patch_drift'] = True
        with self.assertRaisesRegex(MetricsRefused, 'configuration changed'):
            self.run_stage()
        self.assertEqual(len(self.writes()), 1)
        self.assertEqual(self.progress, [])

    def test_target_resume_observes_advancing_samples_and_never_repatches(self):
        self.state['objects'][WORKLOAD]['spec']['template']['spec']['containers'][0]['image'] = TARGET
        with patch('upgrade.product.metrics_stage.time.sleep'):
            result = self.run_stage()
        self.assertTrue(result['samples'])
        self.assertEqual(self.writes(), [])
        self.assertGreater(len([c for c in self.calls() if 'get' == c[0] and
            c[1] == '--raw=/apis/metrics.k8s.io/v1beta1/nodes']), 1)

    def test_stalled_rollout_or_unchanging_samples_is_bounded_and_not_pass(self):
        for mode in ['stalled', 'frozen_samples']:
            with self.subTest(mode=mode):
                self.state[mode] = True
                with patch('upgrade.product.metrics_stage.time.monotonic', side_effect=[0, 31]), \
                     patch('upgrade.product.metrics_stage.time.sleep'), \
                     self.assertRaisesRegex(MetricsRefused, 'fixed timeout'):
                    self.run_stage()
                self.assertFalse(any(s.startswith('UPGRADE_METRICS_API_VERIFIED') for s in self.progress))
                del self.state[mode]

    def test_transport_failure_is_redacted_and_preserves_rc(self):
        self.state['command_failure'] = ['get', API, '-o', 'json']
        with self.assertRaises(MetricsCommandFailed) as error:
            self.run_stage()
        self.assertEqual(error.exception.rc, 42)
        self.assertNotIn('SENSITIVE_FIXTURE_SENTINEL_4097', str(error.exception))
        self.assertEqual(self.writes(), [])

    def test_platform_probe_preserves_first_rc_and_redacts_diagnostics(self):
        self.state['command_failure'] = ['get', '--raw=/version']
        with self.assertRaises(MetricsCommandFailed) as error:
            self.run_stage()
        self.assertEqual(error.exception.rc, 42)
        self.assertNotIn('SENSITIVE_FIXTURE_SENTINEL_4097', str(error.exception))
        self.assertEqual(self.writes(), [])

    def test_duplicate_metrics_foreign_node_unavailable_api_or_missing_uid_refuses_before_patch(self):
        for mode in ['duplicate', 'foreign-node', 'api-unavailable', 'no-uid']:
            with self.subTest(mode=mode):
                original = deepcopy(self.state)
                if mode == 'duplicate':
                    self.state['nodes_metrics']['items'] *= 2
                elif mode == 'foreign-node':
                    self.state['nodes_metrics']['items'].append({
                        **deepcopy(self.state['nodes_metrics']['items'][0]), 'metadata': {'name': 'foreign'}})
                elif mode == 'api-unavailable':
                    self.state['objects'][API]['status']['conditions'][0]['status'] = 'False'
                else:
                    del self.state['objects']['service/metrics-server']['metadata']['uid']
                with self.assertRaises(MetricsRefused):
                    self.run_stage()
                self.assertEqual(self.writes(), [])
                self.state = original

    def test_wire_quantities_and_windows_follow_official_types_without_float_or_ready_shortcuts(self):
        for cpu, memory in [('314159n', '2048Ki'), ('1m', '1.5Gi'), ('0', '1e6')]:
            usage({'cpu': cpu, 'memory': memory})
        for raw in ['NaN', 'Infinity', '-1', '1ki', '1.0watts', '']:
            with self.subTest(raw=raw), self.assertRaises(MetricsRefused):
                usage({'cpu': raw, 'memory': '1Ki'})
        now = datetime.now(timezone.utc)
        stamp = now.isoformat().replace('+00:00', 'Z')
        sample_time({'timestamp': stamp, 'window': '1m2.5s'}, now)
        for window in ['0s', '-1s', '121s', '15', 'NaN', '1sBAD']:
            with self.subTest(window=window), self.assertRaises(MetricsRefused):
                sample_time({'timestamp': stamp, 'window': window}, now)
        with self.assertRaises(MetricsNotReady):
            sample_time({'timestamp': (now-timedelta(minutes=3)).isoformat().replace('+00:00', 'Z'), 'window': '15s'}, now)
        with self.assertRaises(MetricsRefused):
            sample_time({'timestamp': (now+timedelta(seconds=11)).isoformat().replace('+00:00', 'Z'), 'window': '15s'}, now)


if __name__ == '__main__':
    unittest.main()
