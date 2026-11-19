"""Focused refusal and file-retention contracts for the nerdctl-only pack."""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

SIBLING = Path(__file__).resolve().parents[3] / 'kubeauto-ext-bin-dockerfile'
SOURCE = SIBLING / 'upgrade/nerdctl_release.py'


@unittest.skipUnless(SOURCE.exists(), 'nerdctl revision is only present on matching dev checkouts')
class TestNerdctlRevision(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location('nerdctl_release_contract', SOURCE)
        cls.release = importlib.util.module_from_spec(spec)
        with patch.object(sys, 'path', [str(SOURCE.parent), *sys.path]):
            spec.loader.exec_module(cls.release)
        cls.plan = json.loads(cls.release.PLAN.read_text())

    def test_existing_tag_and_unproven_absence_refuse_before_download(self):
        module = self.release
        for rc, error in [(0, ''), (1, 'TLS handshake timeout'), (1, 'unauthorized')]:
            with self.subTest(error=error), patch.object(module.subprocess, 'run', return_value=
                    subprocess.CompletedProcess([], rc, '', error)), \
                    patch.object(module.assets, 'download') as download, \
                    patch.object(module.base, 'manifests') as manifests:
                with self.assertRaises(ValueError):
                    module.prepare(self.plan)
                download.assert_not_called()
                manifests.assert_not_called()

    def test_overlay_rejects_other_binary_or_missing_architecture_before_registry_access(self):
        module = self.release
        for mutation in ['other-binary', 'missing-arch']:
            plan = json.loads(json.dumps(self.plan))
            if mutation == 'other-binary':
                plan['assets'][0]['files'] = {'containerd': 'containerd-bin/containerd'}
            else:
                plan['assets'].pop()
            with patch.object(module.subprocess, 'run') as command:
                with self.assertRaises(AssertionError):
                    module.prepare(plan)
                command.assert_not_called()

    def test_unrelated_checksum_change_refuses_readback(self):
        module = self.release
        identities = {'linux/amd64': 'sha256:' + 'a' * 64, 'linux/arm64': 'sha256:' + 'b' * 64}
        before = {'nerdctl': {'sha256': 'old'}, 'docker-runtime-artifacts.sha256': {'sha256': 'retained'}}
        after = {'nerdctl': {'sha256': 'new'}, 'docker-runtime-artifacts.sha256': {'sha256': 'changed'}}
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            (output / 'retained.json').write_text(json.dumps({p: before for p in identities}))
            with patch.object(module, 'OUTPUT', output), \
                    patch.object(module.base, 'manifests', return_value=identities), \
                    patch.object(module.base, 'files', return_value=after), \
                    patch.object(module, 'version') as version:
                with self.assertRaisesRegex(AssertionError, 'unrelated binary or checksum'):
                    module.verify(self.plan, 'sha256:' + 'c' * 64)
                version.assert_not_called()


if __name__ == '__main__':
    unittest.main()
