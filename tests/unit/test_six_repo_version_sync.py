"""Enforce six-repo version consistency (kubeauto + 5 dockerfile siblings).

Fail loud when kubeauto pins a tag that auxiliary CI/Dockerfile still publishes
under a different tag — that breaks talkedu/Docker Hub pull order.
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

from common.constants import KubeConstant

ROOT = Path(__file__).resolve().parents[2].parent  # /projects
KA = Path(__file__).resolve().parents[2]  # /projects/kubeauto
EXT_IMAGES = ROOT / "kubeauto-ext-images-dockerfile"
EXT_BIN = ROOT / "kubeauto-ext-bin-dockerfile"
EXT_BIN_SP1 = ROOT / "kubeauto-ext-bin-sp1-dockerfile"


def _sibling_present() -> bool:
    return EXT_IMAGES.is_dir() and EXT_BIN.is_dir() and EXT_BIN_SP1.is_dir()


@unittest.skipUnless(_sibling_present(), "sibling dockerfile repos not checked out beside kubeauto")
class TestSixRepoVersionSync(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.kc = KubeConstant()

    def test_json_mock_ci_tag_matches_constant(self):
        """v_json_mock must match ext-images CI matrix tag (talkedu dual-push)."""
        ci = (EXT_IMAGES / ".github/workflows/build.yml").read_text()
        m = re.search(
            r"dockerfile:\s*\./json-mock/Dockerfile.*?tag:\s*([^\n#]+)",
            ci,
            re.S,
        )
        self.assertIsNotNone(m, "json-mock missing from ext-images CI matrix")
        tag = m.group(1).strip().strip("'\"")
        self.assertEqual(tag, self.kc.v_json_mock)

    def test_json_mock_templates_match_constant(self):
        """No leftover hardcoded json-mock tags in role templates."""
        bad = []
        for p in (KA / "roles").rglob("*.j2"):
            text = p.read_text(errors="ignore")
            for m in re.finditer(r"brinnatt/json-mock:([^\s\"']+)", text):
                if m.group(1) != self.kc.v_json_mock:
                    bad.append(f"{p.relative_to(KA)}:{m.group(1)}")
        self.assertEqual(bad, [], msg=f"stale json-mock tags: {bad}")

    def test_ext_bin_env_matches_constants(self):
        ext = (EXT_BIN / "Dockerfile").read_text()
        sp1 = (EXT_BIN_SP1 / "Dockerfile").read_text()

        def env(text: str, name: str) -> str:
            m = re.search(rf"{name}=([^\s]+)", text)
            self.assertIsNotNone(m, f"{name} missing")
            return m.group(1).lstrip("v")

        self.assertEqual(env(ext, "EXT_BIN_VER"), self.kc.v_extra_bin.lstrip("v"))
        self.assertEqual(env(ext, "DOCKER_COMPOSE_VER"), self.kc.v_docker_compose.lstrip("v"))
        self.assertEqual(env(sp1, "EXT_BIN_SP1_VER"), self.kc.v_extra_bin_sp1.lstrip("v"))
        m = re.search(r"kubeauto-ext-bin-sp1:([^\s]+)", ext)
        self.assertIsNotNone(m)
        self.assertEqual(m.group(1).lstrip("v"), self.kc.v_extra_bin_sp1.lstrip("v"))

    def test_json_mock_dockerfile_pins_node20(self):
        """Dockerfile must stay on Node >=18.13 + pinned json-server (v1.3.1 contract)."""
        df = (EXT_IMAGES / "json-mock" / "Dockerfile").read_text()
        self.assertIn("node:20.", df)
        self.assertIn("json-server@0.17.4", df)


if __name__ == "__main__":
    unittest.main()
