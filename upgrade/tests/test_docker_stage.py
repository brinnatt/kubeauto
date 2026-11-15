"""Docker adapter-only migration contracts; never start Docker or connect to CRI."""
from pathlib import Path
import subprocess
import unittest

import yaml

from upgrade.tests import test_containerd_stage as fixtures

ROOT = Path(__file__).resolve().parents[2]


class DockerStageContracts(unittest.TestCase):
    def test_actual_process_identity_shells_have_valid_bash_syntax(self):
        tasks = yaml.safe_load((ROOT/'upgrade/product/docker.yml').read_text())
        bodies = [t['ansible.builtin.shell'] for t in tasks if 'ansible.builtin.shell' in t]
        self.assertEqual(len(bodies), 2)
        for body in bodies:
            result = subprocess.run(['bash', '-n'], input=body, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)

    def setUp(self):
        self.fixture = fixtures.ContainerdStageContracts(methodName='runTest')
        self.fixture.setUp(); self.addCleanup(self.fixture.doCleanups)
        self.fixture.variables.update(CONTAINER_RUNTIME='docker', CGROUP_DRIVER='systemd',
            DOCKER_STORAGE_DIR='/fixture/docker-data',
            UPGRADE_CRI_DOCKERD={'path': '/fixture/source/cri-dockerd', 'sha256': fixtures.digest('cri0410')},
            UPGRADE_DOCKER_PRESERVED_FILES={p: fixtures.digest(p) for p in [
                '/etc/docker/daemon.json', '/etc/systemd/system/docker.service', '/etc/systemd/system/cri-dockerd.service']})
        self.fixture.state = dict(adapter='0.3.26', adapter_hash=fixtures.digest('cri0326'), engine_pid=1234,
                                  preserved=self.fixture.variables['UPGRADE_DOCKER_PRESERVED_FILES'].copy())
        (self.fixture.root/'actions/runtime_record.py').write_bytes((ROOT/'upgrade/tests/fixtures/docker_action.py').read_bytes())

    def run_stage(self, **kwargs):
        return self.fixture.run_stage('docker.yml', **kwargs)

    def test_actual_adapter_only_path_preserves_engine_process_configuration_and_sandboxes(self):
        result = self.run_stage()
        self.assertEqual(result.returncode, 0, result.stdout+result.stderr)
        self.assertIn('UPGRADE_DOCKER_STAGE_PASS', result.stdout)
        calls = self.fixture.trace
        services = [c['args'] for c in calls if c['module'] == 'systemd_service']
        self.assertEqual(services, [{'name': 'cri-dockerd', 'state': 'restarted'}])
        self.assertEqual([c['args']['dest'] for c in calls if c['module'] == 'copy'], ['/fixture/bin/cri-dockerd'])
        self.assertEqual(self.fixture.final['engine_pid'], 1234)
        self.assertEqual(self.fixture.final['preserved'], self.fixture.state['preserved'])

    def test_owned_completed_resume_rechecks_hashes_and_skips_service_restart(self):
        self.fixture.variables['UPGRADE_NODE_RESUME'] = True
        self.fixture.state.update(adapter='0.4.10', adapter_hash=fixtures.digest('cri0410'))
        result = self.run_stage()
        self.assertEqual(result.returncode, 0, result.stdout+result.stderr)
        self.assertFalse([c for c in self.fixture.trace if c['module'] == 'systemd_service'])
        self.assertTrue([c for c in self.fixture.trace if c['name'] == 'Observe the installed Docker CRI adapter digest'])

    def test_unknown_or_unowned_adapter_refuses_before_copy(self):
        for version in ['0.3.25', '0.4.10', '0.5.0']:
            with self.subTest(version=version):
                self.fixture.state['adapter'] = version
                result = self.run_stage()
                self.assertEqual(result.returncode, 2, result.stdout+result.stderr)
                self.assertIn('TASK [Refuse an unknown or unowned partial CRI adapter transition]', result.stdout)
                self.assertFalse([c for c in self.fixture.trace if c['module'] == 'copy'])

    def test_engine_or_driver_mismatch_refuses_before_copy(self):
        for key, value in [('engine', '29.8.0'), ('driver', 'cgroupfs')]:
            with self.subTest(key=key):
                old = self.fixture.state.copy(); self.fixture.state[key] = value
                result = self.run_stage()
                self.assertEqual(result.returncode, 2, result.stdout+result.stderr)
                self.assertIn('TASK [Require the reviewed unchanged Engine and driver]', result.stdout)
                self.assertFalse([c for c in self.fixture.trace if c['module'] == 'copy'])
                self.fixture.state = old

    def test_unit_or_daemon_config_drift_refuses_before_engine_query(self):
        self.fixture.state['preserved']['/etc/docker/daemon.json'] = fixtures.digest('drift')
        result = self.run_stage()
        self.assertEqual(result.returncode, 2, result.stdout+result.stderr)
        self.assertIn('TASK [Refuse unreviewed Engine or adapter service configuration]', result.stdout)
        self.assertFalse([c for c in self.fixture.trace if c['module'] in ['copy', 'shell', 'systemd_service']])

    def test_lost_sandbox_or_restarted_engine_cannot_emit_pass(self):
        for fault, refusal in [('lose_sandbox', 'Refuse lost Docker sandboxes'),
                                ('restart_engine', 'Verify the Engine process was preserved')]:
            with self.subTest(fault=fault):
                self.fixture.state = dict(adapter='0.3.26', adapter_hash=fixtures.digest('cri0326'), engine_pid=1234,
                    preserved=self.fixture.variables['UPGRADE_DOCKER_PRESERVED_FILES'].copy(), **{fault: True})
                result = self.run_stage()
                self.assertEqual(result.returncode, 2, result.stdout+result.stderr)
                self.assertIn('TASK ['+refusal, result.stdout)
                self.assertNotIn('UPGRADE_DOCKER_STAGE_PASS', result.stdout)

    def test_real_node_handoff_retains_cordon_and_stops_before_kubelet_on_adapter_failure(self):
        self.fixture.variables['UPGRADE_DRAIN_TIMEOUT_SECONDS'] = 300
        self.fixture.state['fail'] = 'Restart only the changed Docker CRI adapter'
        result = self.run_stage(node=True, start='Drain with eviction PDB and local-data protection')
        self.assertEqual(result.returncode, 2, result.stdout+result.stderr)
        self.assertIn('fixture first failure', result.stdout)
        self.assertTrue(self.fixture.final['cordoned'])
        self.assertNotIn('TASK [Replace only the drained node executables]', result.stdout)
        self.assertNotIn('UPGRADE_NODE_STAGE_PASS', result.stdout)
        self.assertFalse([c for c in self.fixture.trace if 'uncordon' in c['args'].get('argv', [])])

    def test_admission_refuses_missing_config_identity_before_commands(self):
        self.fixture.variables['UPGRADE_DOCKER_PRESERVED_FILES'].pop('/etc/docker/daemon.json')
        result = self.fixture.run_stage('docker-admission.yml')
        self.assertEqual(result.returncode, 2, result.stdout+result.stderr)
        self.assertEqual(self.fixture.trace, [])


if __name__ == '__main__':
    unittest.main()
