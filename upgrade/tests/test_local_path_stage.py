"""Image/helper-only migration and refusal through the actual subprocess boundary."""
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from jinja2 import Environment, StrictUndefined
import yaml

from upgrade.product.local_path_stage import (CONFIG, HELPER, OBJECTS, SOURCE, TARGET, WORKLOAD,
    LocalPathCommandFailed, LocalPathRefused, LocalPathStage, helper_template, validate_config)


ROOT = Path(__file__).resolve().parents[2]
TEMPLATE = 'roles/cluster-addon/templates/local-storage/local-path-storage.yaml.j2'
GA_SHA = '940c9020144a88b370cba36793a4d7fad6a347ab'


def source_objects():
    original = subprocess.check_output(['git', 'show', GA_SHA+':'+TEMPLATE], cwd=ROOT, text=True)
    text = Environment(undefined=StrictUndefined).from_string(original).render(
        local_path_provisioner_ver='v0.0.31', local_path_storage_class='local-path',
        local_path_provisioner_dir='/opt/local-path-provisioner')
    result = {}
    for obj in yaml.safe_load_all(text):
        ref = obj['kind'].lower()+'/'+obj['metadata']['name']
        obj['metadata'].update(uid=ref+'-uid', resourceVersion='1', generation=1)
        if obj['kind'] == 'Deployment':
            obj['status'] = dict(observedGeneration=1, replicas=1, updatedReplicas=1,
                                 readyReplicas=1, availableReplicas=1)
        result[ref] = obj
    return result


class LocalPathStageContracts(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        binary = self.root/'kubectl'
        binary.write_text('#!'+sys.executable+'\n'+
            (ROOT/'upgrade/tests/fixtures/local_path_kubectl.py').read_text())
        binary.chmod(0o700)
        (self.root/'kubeconfig').write_text('isolated TLS fixture')
        self.source = {'source': 'v1.33.6', 'target': 'v1.34.13', 'mutation': 'none',
            'cluster_identity': {'namespace': 'kube-system', 'uid': 'cluster-uid'},
            'nodes': [{'name': 'node1', 'uid': 'node-uid'}],
            'api_backends': [{'server': 'https://master:6443'}]}
        self.progress = []
        self.stage = LocalPathStage(binary, self.root/'kubeconfig', self.source,
                                    timeout=30, progress=self.progress.append)
        self.reviewed = source_objects()
        self.volumes = {
            '/api/v1/persistentvolumes/ledger-pv': {'apiVersion': 'v1', 'kind': 'PersistentVolume',
                'metadata': {'name': 'ledger-pv', 'uid': 'pv-uid'},
                'spec': {'storageClassName': 'local-path', 'hostPath': {'path': '/opt/local-path-provisioner/ledger'},
                    'nodeAffinity': {'required': {'nodeSelectorTerms': [{'matchExpressions': [
                        {'key': 'kubernetes.io/hostname', 'operator': 'In', 'values': ['node1']}]}]}},
                    'claimRef': {'namespace': 'business', 'name': 'ledger', 'uid': 'pvc-uid'}},
                'status': {'phase': 'Bound'}},
            '/api/v1/namespaces/business/persistentvolumeclaims/ledger': {
                'apiVersion': 'v1', 'kind': 'PersistentVolumeClaim',
                'metadata': {'name': 'ledger', 'namespace': 'business', 'uid': 'pvc-uid'},
                'spec': {'storageClassName': 'local-path', 'volumeName': 'ledger-pv'},
                'status': {'phase': 'Bound'}}}
        self.state = {'objects': {**deepcopy(self.reviewed), **deepcopy(self.volumes)},
            'nodes': [{'metadata': {'name': 'node1', 'uid': 'node-uid'},
                'status': {'nodeInfo': {'kubeletVersion': 'v1.34.13'},
                           'conditions': [{'type': 'Ready', 'status': 'True'}]}}]}
        self.data_phases = []

    def calls(self):
        p = self.root/'calls.jsonl'
        return [json.loads(s) for s in p.read_text().splitlines()] if p.exists() else []

    def writes(self):
        return [c for c in self.calls() if c[0] != 'get' and '--dry-run=server' not in c]

    def run_stage(self, gate=None):
        (self.root/'state.json').write_text(json.dumps(self.state))
        return self.stage.run(self.reviewed, self.volumes, gate or self.data_phases.append)

    def test_actual_ga_template_helper_first_two_dry_runs_before_any_write_and_volume_identity_preserved(self):
        receipt = self.run_stage()
        self.assertEqual(set(self.reviewed)-{'storageclass/local-path'}, OBJECTS)
        patches = [c for c in self.calls() if c[0] == 'patch']
        self.assertEqual([c[1] for c in patches], [CONFIG, WORKLOAD, CONFIG, WORKLOAD])
        self.assertTrue(all('--dry-run=server' in c for c in patches[:2]))
        self.assertEqual(self.data_phases, ['existing']*3+['target-provision-and-cleanup', 'existing'])
        live = json.loads((self.root/'state.json').read_text())['objects']
        for ref in self.volumes:
            self.assertEqual(live[ref], self.volumes[ref])
        for ref in OBJECTS-{CONFIG, WORKLOAD}:
            self.assertEqual(live[ref], self.reviewed[ref])
        helper_template(live[CONFIG]['data']['helperPod.yaml'], HELPER)
        self.assertEqual(live[WORKLOAD]['spec']['template']['spec']['containers'][0]['image'], TARGET)
        self.assertFalse(receipt['parent_acceptance'])

    def test_full_target_and_interrupted_helper_resume_do_not_repeat_completed_writes(self):
        self.run_stage()
        self.state = json.loads((self.root/'state.json').read_text())
        (self.root/'calls.jsonl').unlink()
        self.run_stage()
        self.assertEqual(self.writes(), [])
        self.assertIn('target-provision-and-cleanup', self.data_phases)
        self.state['objects'][WORKLOAD] = deepcopy(self.reviewed[WORKLOAD])
        (self.root/'calls.jsonl').unlink()
        self.run_stage()
        self.assertEqual([c[1] for c in self.writes()], [WORKLOAD])

    def test_target_controller_before_helper_is_refused(self):
        self.state['objects'][WORKLOAD]['spec']['template']['spec']['containers'][0]['image'] = TARGET
        with self.assertRaises(LocalPathRefused):
            self.run_stage()
        self.assertEqual(self.writes(), [])

    def test_both_dry_run_mutations_refuse_before_first_live_write(self):
        for ref in [CONFIG, WORKLOAD]:
            with self.subTest(ref=ref):
                self.state['dry_mutation'] = ref
                with self.assertRaisesRegex(LocalPathRefused, 'dry-run'):
                    self.run_stage()
                self.assertEqual(self.writes(), [])

    def test_source_and_target_barriers_refuse_foreign_cluster_old_api_or_node(self):
        for key in ['cluster_uid', 'api']:
            self.state[key] = 'foreign'
            with self.assertRaises(LocalPathRefused):
                self.run_stage()
            del self.state[key]
        self.state['nodes'][0]['spec'] = {'unschedulable': True}
        with self.assertRaises(LocalPathRefused):
            self.run_stage()
        self.assertEqual(self.writes(), [])

    def test_source_rbac_config_sc_pv_pvc_drift_and_unbound_are_refused(self):
        for ref in ['role/local-path-provisioner-role', CONFIG, 'storageclass/local-path', *self.volumes]:
            original = deepcopy(self.state['objects'][ref])
            self.state['objects'][ref]['metadata']['annotations'] = {'foreign': 'change'}
            with self.assertRaises(LocalPathRefused):
                self.run_stage()
            self.assertEqual(self.writes(), [])
            self.state['objects'][ref] = original
        next(iter(self.state['objects'][ref]['status'] for ref in self.volumes))['phase'] = 'Released'
        with self.assertRaises(LocalPathRefused):
            self.run_stage()
        self.assertEqual(self.writes(), [])

    def test_unhealthy_source_and_missing_real_gate_refuse(self):
        self.state['objects'][WORKLOAD]['status']['availableReplicas'] = 0
        with self.assertRaisesRegex(LocalPathRefused, 'unhealthy'):
            self.run_stage()
        with self.assertRaises(LocalPathRefused):
            self.stage.run(self.reviewed, self.volumes, None)
        self.assertEqual(self.writes(), [])

    def test_custom_helper_scripts_config_pathpattern_or_binding_refuse_before_write(self):
        original = deepcopy(self.reviewed)
        mutations = [
            lambda: self.reviewed[CONFIG]['data'].update(setup='unsafe custom script'),
            lambda: self.reviewed[CONFIG]['data'].update(**{'config.json': '{"nodePathMap":[{"node":"n","paths":["/"]}]}'}),
            lambda: self.reviewed[CONFIG]['data'].update(**{'helperPod.yaml': self.reviewed[CONFIG]['data']['helperPod.yaml']+'\nextra: true'}),
            lambda: self.reviewed['storageclass/local-path'].update(parameters={'pathPattern': 'legacy'})]
        for mutation in mutations:
            self.reviewed = deepcopy(original)
            mutation()
            with self.assertRaises(LocalPathRefused):
                self.run_stage()
            self.assertEqual(self.writes(), [])
        self.reviewed = original
        next(v for v in self.volumes.values() if v['kind'] == 'PersistentVolume')['spec']['claimRef']['uid'] = 'wrong'
        with self.assertRaises(LocalPathRefused):
            self.run_stage()
        self.assertEqual(self.writes(), [])

    def test_guarded_patch_conflict_preserves_rc_without_controller_write_or_diagnostic_leak(self):
        self.state['conflict'] = CONFIG
        with self.assertRaises(LocalPathCommandFailed) as caught:
            self.run_stage()
        self.assertEqual(caught.exception.rc, 42)
        self.assertNotIn('SENSITIVE_LOCALPATH_SENTINEL_8042', str(caught.exception))
        self.assertEqual([c[1] for c in self.writes()], [CONFIG])

    def test_post_helper_volume_drift_stops_before_controller_write(self):
        self.state['volume_drift'] = True
        with self.assertRaises(LocalPathRefused):
            self.run_stage()
        self.assertEqual([c[1] for c in self.writes()], [CONFIG])

    def test_real_data_gate_failure_stops_writes_or_refuses_final_success(self):
        for failed_phase in ['existing', 'target-provision-and-cleanup']:
            self.state['objects'] = {**deepcopy(self.reviewed), **deepcopy(self.volumes)}
            (self.root/'calls.jsonl').unlink(missing_ok=True)
            def gate(phase):
                if phase == failed_phase:
                    raise LocalPathRefused('business ledger or scratch cleanup failed')
            with self.assertRaises(LocalPathRefused):
                self.run_stage(gate)
            self.assertEqual(len(self.writes()), 0 if failed_phase == 'existing' else 2)
        self.assertNotIn('UPGRADE_LOCAL_PATH_IMAGE_AND_DATA_VERIFIED', self.progress)

    def test_target_rollout_timeout_does_not_delete_or_rollback(self):
        self.state['stalled'] = True
        with patch('upgrade.product.local_path_stage.time.monotonic', side_effect=[0, 31]), \
                self.assertRaisesRegex(LocalPathRefused, 'timeout'):
            self.run_stage()
        self.assertEqual([c[1] for c in self.writes()], [CONFIG, WORKLOAD])

    def test_config_normalization_refuses_root_traversal_duplicates_and_custom_backend(self):
        data = deepcopy(self.reviewed[CONFIG]['data'])
        for config in [{'nodePathMap': [{'node': 'n', 'paths': [p]}]} for p in ['', '/', '/a/../b', '/a//b']]+[
                {'nodePathMap': [{'node': 'n', 'paths': ['/a', '/a']}]},
                {'nodePathMap': [{'node': 'n', 'paths': []}, {'node': 'n', 'paths': []}]},
                {'sharedFileSystemPath': '/a', 'nodePathMap': []}]:
            with self.subTest(config=config):
                data['config.json'] = json.dumps(config)
                with self.assertRaises(LocalPathRefused):
                    validate_config(data)


if __name__ == '__main__':
    unittest.main()
