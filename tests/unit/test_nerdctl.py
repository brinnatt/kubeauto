"""nerdctl pin: shipped via ext-bin, installed on containerd master/worker nodes."""
from __future__ import annotations

import re
import json
import unittest
from pathlib import Path

from common.constants import KubeConstant

KA = Path(__file__).resolve().parents[2]
ROOT = KA.parent
EXT_BIN = ROOT / "kubeauto-ext-bin-dockerfile"
CONTAINERD_ROLE = KA / "roles" / "containerd" / "tasks" / "main.yml"
NERDCTL_GATE = KA / "tests" / "helpers" / "nerdctl-gate.sh"


class TestNerdctlPin(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.kc = KubeConstant()

    def test_version_pinned_semver(self):
        self.assertRegex(self.kc.v_nerdctl, r"^\d+\.\d+\.\d+$")

    def test_download_url_minimal_tarball(self):
        url = self.kc.nerdctl_bin_url()
        self.assertIn("containerd/nerdctl/releases/download/", url)
        self.assertIn(f"v{self.kc.v_nerdctl}/", url)
        self.assertTrue(url.endswith(f"nerdctl-{self.kc.v_nerdctl}-linux-amd64.tar.gz")
                        or url.endswith(f"nerdctl-{self.kc.v_nerdctl}-linux-arm64.tar.gz"))
        self.assertNotIn("nerdctl-full", url)

    def test_containerd_role_distributes_nerdctl(self):
        text = CONTAINERD_ROLE.read_text()
        self.assertIn("extra-bin/nerdctl", text)
        self.assertIn("dest={{ bin_dir }}/nerdctl", text)
        self.assertIn("nerdctl completion", text)

    def test_runtime_playbook_covers_master_and_node(self):
        """Master and worker both get roles/containerd when CONTAINER_RUNTIME=containerd."""
        pb = (KA / "playbooks" / "03.runtime.yml").read_text()
        self.assertIn("kube_master", pb)
        self.assertIn("kube_node", pb)
        self.assertIn("role: containerd", pb)

    def test_multi_node_gate_disables_reserved_on_small_worker(self):
        """Worker 133 is below the 16C/32Gi reserved-resource contract floor."""
        text = NERDCTL_GATE.read_text()
        self.assertIn('WORKER="${NERDCTL_WORKER:-192.168.47.133}"', text)
        self.assertIn(
            "sed -i 's/^KUBE_RESERVED_ENABLED: \"yes\"/KUBE_RESERVED_ENABLED: \"no\"/'",
            text,
        )
        self.assertIn(
            "sed -i 's/^SYS_RESERVED_ENABLED: \"yes\"/SYS_RESERVED_ENABLED: \"no\"/'",
            text,
        )
        created = text.index("kubecli new nerdctl-mw")
        config_override = text.index("sed -i 's/^KUBE_RESERVED_ENABLED", created)
        setup = text.index("kubecli setup nerdctl-mw 90", config_override)
        self.assertLess(created, config_override)
        self.assertLess(config_override, setup)

    def test_live_gate_executes_cross_node_application_read_write(self):
        text = NERDCTL_GATE.read_text()
        self.assertIn("kubernetes-production-smoke.sh", text)
        self.assertIn("PRODUCTION_SMOKE_SERVER_NODE=worker-133", text)
        self.assertIn("PRODUCTION_SMOKE_CLIENT_NODE=master-aio", text)
        self.assertLess(
            text.index("kubernetes-production-smoke.sh"),
            text.index("NERDCTL_GATE_PASS"),
        )

    @unittest.skipUnless(EXT_BIN.is_dir(), "ext-bin sibling not checked out")
    def test_ext_bin_dockerfile_pins_match_constants(self):
        df = (EXT_BIN / "Dockerfile").read_text()
        sh = (EXT_BIN / "multi-platform-download.sh").read_text()
        current_manifest = EXT_BIN / 'upgrade/release.json'
        if current_manifest.exists():
            current = json.loads(current_manifest.read_text())
            targets = [a for a in current['artifacts']
                       if a['tag'] == self.kc.v_extra_bin.lstrip('v')]
            self.assertEqual(len(targets), 1, 'consumed ext-bin tag must be unique in catalog')
            values = dict(targets[0]['env'])
            for recipe in targets[0]['components'].values():
                values.update(recipe.get('env', {}))
            df += '\n' + '\n'.join('ENV ' + k + '=' + v for k, v in values.items())

        versions = re.findall(r"NERDCTL_VER=([^\s]+)", df)
        self.assertTrue(versions, "NERDCTL_VER missing from ext-bin Dockerfile")
        self.assertEqual(versions[-1].lstrip("v"), self.kc.v_nerdctl.lstrip("v"))

        m_pack = re.findall(r"EXT_BIN_VER=([^\s]+)", df)
        self.assertIsNotNone(m_pack)
        self.assertEqual(m_pack[-1].lstrip("v"), self.kc.v_extra_bin.lstrip("v"))

        # Must download minimal package only (avoid overwriting project containerd/runc).
        self.assertIn("nerdctl-${NERDCTL_VER}-linux-${ARCH}.tar.gz", sh)
        self.assertNotRegex(sh, r"wget[^\n]*nerdctl-full")
        self.assertIn("mv /tmp/nerdctl /ext-bin/nerdctl", sh)

        # The minimal CLI must match the consumed pack, including its overlay.
        m_ct = re.findall(r"CONTAINERD_VER=([^\s]+)", df)
        self.assertIsNotNone(m_ct)
        self.assertEqual(m_ct[-1], self.kc.v_containerd)


if __name__ == "__main__":
    unittest.main()
