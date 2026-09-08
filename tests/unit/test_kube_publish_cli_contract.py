"""Deterministic CLI and input-safety contracts for the standalone publisher."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
TOOL = ROOT / "tools" / "k8stools" / "KubePublishCli.py"


def load_tool_module():
    spec = importlib.util.spec_from_file_location("kube_publish_cli", TOOL)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class KubePublishCliContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tool = load_tool_module()

    def run_cli(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(TOOL), *args],
            text=True,
            capture_output=True,
            check=False,
        )

    def test_image_references_reject_shell_metacharacters(self):
        self.assertTrue(self.tool.InputValidator.validate_image_reference("registry.example/demo:v1"))
        self.assertTrue(self.tool.InputValidator.validate_image_reference("repo/demo@sha256:abcd"))
        for value in ("", "demo;touch /tmp/pwn", "demo image", "../demo"):
            self.assertFalse(self.tool.InputValidator.validate_image_reference(value))

    def test_host_expansion_preserves_valid_ranges(self):
        self.assertEqual(
            self.tool.expand_distribution_hosts(["worker-{01..02}:22"]),
            ["worker-01:22", "worker-02:22"],
        )

    def test_invalid_inputs_fail_before_runtime_execution(self):
        result = self.run_cli("--download", "demo;touch /tmp/pwn")
        self.assertEqual(result.returncode, 2, result.stderr + result.stdout)
        self.assertIn("镜像引用无效", result.stdout)

    def test_invalid_remote_host_and_tar_name_fail_nonzero(self):
        with tempfile.TemporaryDirectory() as directory:
            tar_path = Path(directory) / "bad;name.tar"
            tar_path.touch()
            result = self.run_cli(
                "--distribute", "bad;touch /tmp/pwn", "--tar", str(tar_path),
            )
        self.assertEqual(result.returncode, 2, result.stderr + result.stdout)
        self.assertIn("远程主机或端口无效", result.stdout)
        self.assertIn("tar文件名或路径无效", result.stdout)

    def test_missing_operation_fails_nonzero(self):
        result = self.run_cli()
        self.assertEqual(result.returncode, 2, result.stderr + result.stdout)

    def test_partial_remote_distribution_is_failure(self):
        source = TOOL.read_text(encoding="utf-8")
        self.assertIn("if success_hosts == len(hosts):", source)


if __name__ == "__main__":
    unittest.main()
