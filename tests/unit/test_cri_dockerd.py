"""cri-dockerd is delivered as a verified ext-bin artifact (K8s >=1.24)."""
import unittest
import hashlib
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from common.constants import KubeConstant
from service.cluster.docker import DockerManager


class TestCriDockerdPin(unittest.TestCase):
    def setUp(self):
        self.kc = KubeConstant()

    def test_version_pinned(self):
        self.assertTrue(self.kc.v_cri_dockerd)
        self.assertRegex(self.kc.v_cri_dockerd, r"^\d+\.\d+\.\d+$")

    def test_dual_pushed_ext_bin_is_the_runtime_source(self):
        self.assertEqual(
            self.kc.docker_runtime_artifact_image,
            f"brinnatt/kubeauto-ext-bin:{self.kc.v_extra_bin}",
        )

    def test_no_runtime_github_proxy_download_remains(self):
        root = Path(__file__).resolve().parents[2]
        docker_source = (root / "service/cluster/docker.py").read_text()
        docker_role = (root / "roles/docker/tasks/main.yml").read_text()
        self.assertNotIn("v6.gh-proxy.org", docker_source)
        self.assertNotIn("v6.gh-proxy.org", docker_role)
        self.assertIn("docker-runtime-artifacts.sha256", docker_source)
        self.assertIn("kubecli download -D", docker_role)

    def test_delivery_gate_uses_large_host_and_proves_recovery_sources(self):
        root = Path(__file__).resolve().parents[2]
        gate = (root / "tests/helpers/delivery-docker-gate.sh").read_text()
        self.assertIn('NODE="${DOCKER_GATE_NODE:-192.168.47.137}"', gate)
        self.assertIn("corrupted-by-delivery-gate", gate)
        self.assertIn("corrupted-by-fallback-gate", gate)
        self.assertIn("DOCKERHUB_FALLBACK_OK", gate)
        self.assertIn('docker image tag "$PRIVATE_IMAGE" "$CACHE_IMAGE"', gate)
        self.assertIn("Docker Hub fallback digest missing", gate)
        cache_tag = gate.index('docker image tag "$PRIVATE_IMAGE" "$CACHE_IMAGE"')
        self.assertLess(
            cache_tag,
            gate.index('docker image rm "$EXT_IMAGE" "$PRIVATE_IMAGE"', cache_tag),
        )
        self.assertIn("sha256sum -c docker-runtime-artifacts.sha256", gate)
        self.assertIn("docker compose version", gate)
        self.assertIn("docker buildx version", gate)
        self.assertIn('"$BASE/docker-bin/cri-dockerd" --version', gate)
        self.assertIn("kubernetes-production-smoke.sh", gate)
        self.assertIn("PRODUCTION_SMOKE_SERVER_NODE=master-docker", gate)
        self.assertLess(
            gate.index("kubernetes-production-smoke.sh"),
            gate.index("DOCKER_GATE_PASS"),
        )


class TestDockerRuntimeArtifactManifest(unittest.TestCase):
    def setUp(self):
        self.manager = DockerManager()

    def test_manifest_requires_exactly_the_three_runtime_artifacts(self):
        digest = "a" * 64
        manifest = "\n".join(
            f"{digest}  {name}" for name in ("docker-compose", "docker-buildx", "cri-dockerd")
        )
        with TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "docker-runtime-artifacts.sha256"
            path.write_text(manifest)
            checksums = self.manager._read_docker_runtime_manifest(path)
        self.assertEqual(set(checksums), {"docker-compose", "docker-buildx", "cri-dockerd"})

    def test_manifest_rejects_missing_or_unknown_artifacts(self):
        with TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "docker-runtime-artifacts.sha256"
            path.write_text(f"{'a' * 64}  docker-compose\n")
            with self.assertRaises(ValueError):
                self.manager._read_docker_runtime_manifest(path)

    def test_local_artifacts_must_match_manifest_before_reuse(self):
        with TemporaryDirectory() as temp_dir:
            artifact_dir = Path(temp_dir)
            checksums = []
            for name in ("docker-compose", "docker-buildx", "cri-dockerd"):
                content = f"verified-{name}".encode()
                path = artifact_dir / name
                path.write_bytes(content)
                path.chmod(0o755)
                checksums.append(f"{hashlib.sha256(content).hexdigest()}  {name}")
            (artifact_dir / "docker-runtime-artifacts.sha256").write_text("\n".join(checksums))
            self.manager.docker_bin_dir = artifact_dir
            self.assertTrue(self.manager._docker_runtime_artifacts_valid())
            (artifact_dir / "docker-buildx").write_bytes(b"truncated")
            self.assertFalse(self.manager._docker_runtime_artifacts_valid())

    def test_ensure_restages_corrupted_artifacts(self):
        with TemporaryDirectory() as temp_dir:
            artifact_dir = Path(temp_dir)
            self.manager.docker_bin_dir = artifact_dir

            def stage_verified_artifacts():
                checksums = []
                for name in ("docker-compose", "docker-buildx", "cri-dockerd"):
                    content = name.encode()
                    path = artifact_dir / name
                    path.write_bytes(content)
                    path.chmod(0o755)
                    checksums.append(f"{hashlib.sha256(content).hexdigest()}  {name}")
                (artifact_dir / "docker-runtime-artifacts.sha256").write_text("\n".join(checksums))

            with patch.object(self.manager, "_stage_docker_runtime_artifacts", side_effect=stage_verified_artifacts) as stage, \
                 patch.object(self.manager, "_install_docker_cli_plugins") as install, \
                 patch("service.cluster.docker.run_command") as command:
                self.manager.ensure_docker_runtime_artifacts()
            stage.assert_called_once()
            install.assert_called_once()
            command.assert_called_once_with([str(artifact_dir / "cri-dockerd"), "--version"])


if __name__ == "__main__":
    unittest.main()
