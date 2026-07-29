"""Docker-on-Harbor must not replace the Kubernetes containerd runtime."""
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


class TestRuntimeBinaryIsolation(unittest.TestCase):
    def test_docker_role_isolates_managed_runtime_bundle(self):
        tasks = (ROOT / "roles/docker/tasks/main.yml").read_text(encoding="utf-8")
        self.assertIn("/usr/local/libexec/docker", tasks)
        for name in ("containerd", "containerd-shim-runc-v2", "ctr", "runc"):
            self.assertIn(name, tasks)
        self.assertIn("require docker install to preserve kubernetes containerd", tasks)
        self.assertIn("installed_kubernetes_containerd.stat.checksum", tasks)

    def test_docker_unit_prefers_private_runtime_for_containerd_clusters(self):
        unit = (ROOT / "roles/docker/templates/docker.service.j2").read_text(encoding="utf-8")
        self.assertIn('CONTAINER_RUNTIME == "containerd"', unit)
        self.assertIn(
            'Environment="PATH=/usr/local/libexec/docker:{{ bin_dir }}:',
            unit,
        )

    def test_containerd_role_has_artifact_and_effective_config_gates(self):
        tasks = (ROOT / "roles/containerd/tasks/main.yml").read_text(encoding="utf-8")
        self.assertIn("require the exact ext-bin containerd artifact", tasks)
        self.assertIn("installed_containerd.stat.checksum == staged_containerd.stat.checksum", tasks)
        self.assertIn("containerd config dump", tasks)
        self.assertIn("SANDBOX_IMAGE", tasks)

    def test_containerd_template_uses_current_21_cni_fields(self):
        config = (ROOT / "roles/containerd/templates/config.toml.j2").read_text(encoding="utf-8")
        self.assertNotIn("plugin_dir =", config)
        self.assertNotIn("\n      bin_dir = '/opt/cni/bin'", config)
        self.assertIn("bin_dirs = ['/opt/cni/bin']", config)


if __name__ == "__main__":
    unittest.main()
