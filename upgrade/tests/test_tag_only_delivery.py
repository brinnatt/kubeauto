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

    def test_request_readers_checkout_commit_without_overwriting_annotated_tag(self):
        for repository,job_name in [('kubeauto-ext-bin-dockerfile','docker'),
                                    ('kubeauto-ext-bin-sp1-dockerfile','docker'),
                                    ('kubeauto-k8s-bin-dockerfile','verify')]:
            data = yaml.load((PROJECTS/repository/'.github/workflows/build.yml').read_text(), Loader=yaml.BaseLoader)
            step = next(s for s in data['jobs'][job_name]['steps'] if s.get('uses') == 'actions/checkout@v4')
            self.assertEqual(step['with'], {'ref':'${{ github.sha }}','fetch-depth':'0'})

    def test_kubernetes_readback_cannot_enter_publication_plan(self):
        root = PROJECTS/'kubeauto-k8s-bin-dockerfile'
        data = yaml.load((root/'.github/workflows/build.yml').read_text(), Loader=yaml.BaseLoader)
        self.assertIn("!startsWith(github.ref_name, 'verify-k8s-')", data['jobs']['plan']['if'])
        self.assertIn("startsWith(github.ref_name, 'verify-k8s-')", data['jobs']['verify']['if'])
        self.assertEqual(data['jobs']['docker']['needs'], 'plan')

    def test_binary_release_uses_manifest_and_explicit_request_before_registry_login(self):
        for repository in ['kubeauto-ext-bin-dockerfile', 'kubeauto-ext-bin-sp1-dockerfile']:
            root = PROJECTS/repository
            data = yaml.load((root/'.github/workflows/build.yml').read_text(), Loader=yaml.BaseLoader)
            steps = data['jobs']['docker']['steps']
            admit = next(s for s in steps if 'pack_release.py admit' in s.get('run',''))
            login = next(s for s in steps if s.get('uses') == 'docker/login-action@v3')
            self.assertLess(steps.index(admit), steps.index(login))
            self.assertEqual([p.name for p in (root/'.github/workflows').glob('*.yml')], ['build.yml'])
