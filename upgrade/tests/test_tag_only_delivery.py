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

    def test_packaging_source_and_published_version_are_catalog_bound(self):
        import json
        repo = PROJECTS/'kubeauto-dockerfile'
        catalog = json.loads((repo/'upgrade/release.json').read_text())
        self.assertTrue(catalog['ghcr'])
        for artifact in catalog['artifacts']:
            self.assertEqual(artifact['source_tag'], artifact['tag'])
            self.assertEqual(artifact['build_args']['KUBEAUTO_VER'], artifact['tag'])
            self.assertRegex(artifact['source_sha'], r'^[a-f0-9]{40}$')
        self.assertIn('KUBEAUTO_ARCHIVE_SHA256', (repo/'Dockerfile').read_text())

    def test_all_generic_requests_are_admitted_before_login(self):
        sources=[]
        for repository in REPOSITORIES[1:5]:
            root=PROJECTS/repository
            data=yaml.load((root/'.github/workflows/build.yml').read_text(),Loader=yaml.BaseLoader)
            self.assertEqual(data['on'], {'push': {'tags': ['artifacts-*','verify-artifacts-*']}})
            self.assertEqual(data['concurrency']['group'],'artifact-publication')
            steps=data['jobs']['release']['steps']
            checkout=next(s for s in steps if s.get('uses')=='actions/checkout@v4')
            self.assertEqual(checkout['with'], {'ref':'${{ github.sha }}','fetch-depth':'0'})
            admit=next(s for s in steps if 'release.py select' in s.get('run',''))
            login=next(s for s in steps if s.get('uses')=='docker/login-action@v3')
            self.assertLess(steps.index(admit),steps.index(login))
            self.assertTrue(any('release.py plan' in s.get('run','') for s in steps))
            self.assertTrue(any('release.py run' in s.get('run','') for s in steps))
            self.assertEqual([p.name for p in (root/'.github/workflows').glob('*.yml')],['build.yml'])
            sources.append((root/'upgrade/release.py').read_bytes())
        self.assertTrue(all(s==sources[0] for s in sources))
        self.assertEqual((PROJECTS/'kubeauto-ext-bin-dockerfile/upgrade/pack_release.py').read_bytes(),
                         (PROJECTS/'kubeauto-ext-bin-sp1-dockerfile/upgrade/pack_release.py').read_bytes())
