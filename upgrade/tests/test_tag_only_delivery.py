"""Prove six-repository workflow routing without publishing tags or images."""
from pathlib import Path
import os
import subprocess
import tempfile
import unittest

import yaml

PROJECTS = Path(__file__).resolve().parents[3]
REPOSITORIES = ['kubeauto', 'kubeauto-dockerfile', 'kubeauto-k8s-bin-dockerfile',
                'kubeauto-ext-bin-dockerfile', 'kubeauto-ext-bin-sp1-dockerfile',
                'kubeauto-ext-images-dockerfile']


class TagOnlyDeliveryContracts(unittest.TestCase):
    def test_every_active_workflow_accepts_only_tag_pushes(self):
        for repository in REPOSITORIES:
            for path in (PROJECTS/repository/'.github/workflows').glob('*.y*ml'):
                with self.subTest(path=path):
                    data = yaml.load(path.read_text(), Loader=yaml.BaseLoader)
                    self.assertEqual(set(data['on']), {'push'})
                    self.assertEqual(set(data['on']['push']), {'tags'})
                    self.assertTrue(data['on']['push']['tags'])
                    for job in data['jobs'].values():
                        for step in job.get('steps', []):
                            if 'run' in step:
                                result = subprocess.run(['bash', '-n'], input=step['run'],
                                    capture_output=True, text=True)
                                self.assertEqual(result.returncode, 0, result.stderr)

    def test_packaging_source_and_published_version_derive_from_same_tag(self):
        path = PROJECTS/'kubeauto-dockerfile/.github/workflows/build_and_push.yml'
        job = yaml.load(path.read_text(), Loader=yaml.BaseLoader)['jobs']['build-and-push-image']
        self.assertEqual(job['strategy']['matrix']['include'],
                         [{'version': '${{ github.ref_name }}'}])
        build = next(s for s in job['steps'] if s.get('uses') == 'docker/build-push-action@v6')
        self.assertEqual(build['with']['build-args'], 'KUBEAUTO_VER=${{ matrix.version }}')

    def test_binary_release_refuses_tags_that_mislabel_dockerfile_versions(self):
        for repository, version in [('kubeauto-ext-bin-dockerfile', '1.16.0'),
                                    ('kubeauto-ext-bin-sp1-dockerfile', '1.3.1')]:
            root = PROJECTS/repository
            data = yaml.load((root/'.github/workflows/build.yml').read_text(), Loader=yaml.BaseLoader)
            step = next(s for s in data['jobs']['docker']['steps'] if s.get('name', '').startswith('Verify tag'))
            with tempfile.TemporaryDirectory() as directory:
                fixture = Path(directory)
                (fixture/'python3').symlink_to('/usr/bin/python3')
                for tag, succeeds in [(version, True), ('9.99.999', False)]:
                    result = subprocess.run(['bash', '-euc', step['run']], cwd=root,
                        env=os.environ | {'RELEASE_TAG': tag, 'PATH': str(fixture)+':'+os.environ['PATH']},
                        capture_output=True, text=True)
                    self.assertEqual(result.returncode == 0, succeeds, result.stderr)
        self.assertIn("github.ref_name != '1.17.0'",
            (PROJECTS/'kubeauto-ext-bin-dockerfile/.github/workflows/build.yml').read_text())
