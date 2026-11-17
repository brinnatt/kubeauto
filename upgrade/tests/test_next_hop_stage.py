"""Run the reviewed 1.34 -> 1.35 path through real composed Ansible tasks."""
import hashlib
import json
import unittest
from upgrade.tests import test_kubernetes_stage as fixture
from upgrade.product.kubernetes_stage import StageFailed, CONTROL


class NextHopContracts(unittest.TestCase):
    def setUp(self):
        self.fixture = fixture.KubernetesHandoffContracts(methodName='runTest')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        f = self.fixture
        f.binary.write_text(f.binary.read_text().replace("'v1.34.13'", "'v1.35.10'"))
        f.source = json.loads(json.dumps(f.source).replace('v1.34.13','v1.35.10').replace('v1.33.6','v1.34.13'))
        f.source['kubectl_sha256'] = hashlib.sha256(f.binary.read_bytes()).hexdigest()
        f.variables.update(UPGRADE_SOURCE_VERSION='v1.34.13', K8S_VER='1.35.10')
        f.state = json.loads(json.dumps(f.state).replace('v1.33.6','v1.34.13'))
        for node in f.state['nodes'].values():
            node['config'] = 'kind: KubeletConfiguration\napiVersion: kubelet.config.k8s.io/v1beta1\nmaxPods: 110\n'
        f.save(f.state)
        f.stage = f.make_stage()

    def test_real_next_hop_completes_and_resume_preserves_config(self):
        f = self.fixture
        f.execute()
        state = f.read()
        self.assertEqual(set(state['apis'].values()), {'v1.35.10'})
        self.assertEqual(state['api_auth']['master-one'],'--requestheader-allowed-names=aggregator \\\n')
        for node in state['nodes'].values():
            self.assertEqual(node['kubelet'], 'v1.35.10')
            self.assertIn('maxPods: 110', node['config'])
            self.assertIn('failCgroupV1: true', node['config'])
        before = len(f.trace())
        f.execute()
        self.assertFalse([t for t in f.trace()[before:] if t['module'] in ['copy','lineinfile','systemd_service']])

    def test_foreign_aggregator_certificate_refuses_before_any_api_replacement(self):
        f=self.fixture
        f.state['aggregator_cn']='foreign-client';f.save(f.state)
        with self.assertRaises(StageFailed):f.execute()
        self.assertFalse([t for t in f.trace() if t['module'] in ['copy','replace','systemd_service']])
        self.assertEqual(set(f.read()['apis'].values()),{'v1.34.13'})

    def test_legacy_default_refuses_and_explicit_opt_in_resumes(self):
        f = self.fixture
        for node in f.state['nodes'].values(): node['cgroup'] = 'tmpfs'
        f.save(f.state)
        with self.assertRaises(StageFailed): f.execute()
        state = f.read()
        self.assertEqual(state['nodes']['master-one']['kubelet'], 'v1.34.13')
        self.assertFalse(state['nodes']['master-one']['cordoned'])
        self.assertFalse([t for t in f.trace() if 'drain' in t['args'].get('argv', [])])
        self.assertFalse([t for t in f.trace() if t['module']=='lineinfile'])
        # An input change cannot silently resume an existing journal. A separate
        # reviewed run is required; prove its inputs bind a different journal.
        f.variables['KUBELET_FAIL_CGROUP_V1'] = False
        revised = f.make_stage()
        self.assertNotEqual(revised.binding, f.stage.binding)

    def test_explicit_legacy_path_completes_without_changing_other_options(self):
        f = self.fixture
        for node in f.state['nodes'].values(): node['cgroup'] = 'tmpfs'
        f.variables['KUBELET_FAIL_CGROUP_V1'] = False
        f.save(f.state); f.stage = f.make_stage()
        f.execute()
        for node in f.read()['nodes'].values():
            self.assertIn('failCgroupV1: false', node['config'])
            self.assertIn('maxPods: 110', node['config'])
