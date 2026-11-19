"""Exercise the real sibling downloader and workflow plan without network/push."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[2]
REPO = ROOT.parent / 'kubeauto-k8s-bin-dockerfile'
BINARIES = ('kube-apiserver', 'kube-controller-manager', 'kube-scheduler',
            'kubelet', 'kube-proxy', 'kubectl')


@unittest.skipUnless(REPO.is_dir(), 'sibling k8s-bin checkout required')
class BinaryArtifactContracts(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.fakebin = self.root / 'bin'
        self.fakebin.mkdir()
        # Only the transport and architecture are fixtures. The real shell script,
        # filesystem, hashing, permissions, cleanup and CI selector run unchanged.
        wget = self.fakebin / 'wget'
        wget.write_text('''#!/usr/bin/env python3
import hashlib, os, pathlib, sys
args = sys.argv[1:]
output = pathlib.Path(args[args.index('-O') + 1])
url = args[-1]
name = url.rsplit('/', 1)[-1]
binary = name.removesuffix('.sha256')
expected = 'https://dl.k8s.io/release/v1.34.13/bin/linux/' + os.environ['EXPECTED_ARCH'] + '/' + name
assert url == expected, 'unexpected release or architecture URL'
data = (binary + ':fixture-bytes').encode()
if binary == 'kubelet' and os.environ.get('FAIL_DOWNLOAD'):
    output.write_bytes(b'partial')
    sys.exit(4)
if name.endswith('.sha256'):
    digest = hashlib.sha256(data).hexdigest()
    if binary == 'kubelet' and os.environ.get('BAD_DIGEST'):
        digest = '0' * 64
    if binary == 'kubelet' and os.environ.get('MALFORMED_DIGEST'):
        digest = 'not-a-checksum'
    output.write_text(digest + '\\n')
else:
    output.write_bytes(data)
''')
        wget.chmod(0o755)
        uname = self.fakebin / 'uname'
        uname.write_text('#!/bin/sh\nprintf "%s\\n" "$FIXTURE_MACHINE"\n')
        uname.chmod(0o755)
        self.destination = self.root / 'bundle'
        self.env = os.environ | {
            'PATH': str(self.fakebin) + os.pathsep + os.environ['PATH'],
            'K8S_VER': 'v1.34.13', 'K8S_DESTINATION': str(self.destination),
            'FIXTURE_MACHINE': 'x86_64', 'EXPECTED_ARCH': 'amd64',
        }

    def download(self, **overrides):
        return subprocess.run(['sh', str(REPO / 'multi-platform-download.sh')],
                              env=self.env | overrides, capture_output=True, text=True)

    def assert_no_bundle(self, result):
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(self.destination.exists())
        self.assertEqual(list(self.root.glob('bundle.tmp.*')), [])
        self.assertNotIn('K8S_BINARY_BUNDLE_VERIFIED', result.stdout)

    def test_complete_checked_bundle_on_both_architectures(self):
        for machine, arch in [('x86_64', 'amd64'), ('aarch64', 'arm64')]:
            with self.subTest(arch=arch):
                result = self.download(FIXTURE_MACHINE=machine, EXPECTED_ARCH=arch)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn(f'arch={arch} count=6', result.stdout)
                self.assertEqual({p.name for p in self.destination.iterdir()},
                                 {*BINARIES, 'official.sha256'})
                hashes = []
                for name in BINARIES:
                    path = self.destination / name
                    self.assertEqual(path.stat().st_mode & 0o777, 0o755)
                    hashes.append(f'{hashlib.sha256(path.read_bytes()).hexdigest()}  {name}')
                    path.unlink()
                manifest = self.destination / 'official.sha256'
                self.assertEqual(manifest.read_text().splitlines(), hashes)
                manifest.unlink()
                self.destination.rmdir()

    def test_failed_transport_and_checksums_leave_no_partial_bundle(self):
        for failure in ['FAIL_DOWNLOAD', 'BAD_DIGEST', 'MALFORMED_DIGEST']:
            with self.subTest(failure=failure):
                self.assert_no_bundle(self.download(**{failure: '1'}))

    def test_unsupported_architecture_and_floating_versions_are_rejected(self):
        self.assert_no_bundle(self.download(FIXTURE_MACHINE='riscv64'))
        for version in ['', 'stable', 'v1.34', 'v1.34.13-rc.1', 'v1.34.13/extra']:
            with self.subTest(version=version):
                self.assert_no_bundle(self.download(K8S_VER=version))

    def test_existing_bundle_is_preserved(self):
        self.destination.mkdir()
        sentinel = self.destination / 'old'
        sentinel.write_text('previous verified artifact')
        self.assertNotEqual(self.download().returncode, 0)
        self.assertEqual(sentinel.read_text(), 'previous verified artifact')
        self.assertEqual(list(self.destination.iterdir()), [sentinel])

    def test_real_workflow_selector_limits_selected_publication(self):
        workflow = yaml.load((REPO / '.github/workflows/build.yml').read_text(),
                             Loader=yaml.BaseLoader)
        script = workflow['jobs']['plan']['steps'][0]['run']
        output = self.root / 'github-output'
        for version in ['v1.33.5', 'v1.34.13', 'v1.35.10']:
            output.write_text('')
            result = subprocess.run(['bash', '-euc', script], capture_output=True,
                                    env=os.environ | {'EVENT_NAME': 'push', 'REF_TYPE': 'tag',
                                        'REF_NAME': version, 'GITHUB_OUTPUT': str(output)}, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            matrix = json.loads(output.read_text().removeprefix('matrix='))
            self.assertEqual(matrix, {'include': [{'version': version}]})
        for event, kind, version in [('push', 'branch', 'v1.34.13'),
                                      ('push', 'branch', 'dev'),
                                      ('workflow_dispatch', 'tag', 'v1.34.13'),
                                      ('push', 'tag', 'v1.34.13-rc.1'),
                                      ('push', 'tag', 'v1.34.13\nattack'),
                                      ('push', 'tag', 'verify-k8s-v1.34.13-1')]:
            output.write_text('')
            result = subprocess.run(['bash', '-euc', script], capture_output=True,
                                    env=os.environ | {'EVENT_NAME': event, 'REF_TYPE': kind,
                                        'REF_NAME': version, 'GITHUB_OUTPUT': str(output)}, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(output.read_text(), '')
        docker = workflow['jobs']['docker']
        self.assertEqual(docker['needs'], 'plan')
        self.assertEqual(workflow['on'], {'push': {'tags': ['v[0-9]*.[0-9]*.[0-9]*', 'verify-k8s-*']}})
        self.assertEqual(docker['strategy']['matrix'], '${{ fromJSON(needs.plan.outputs.matrix) }}')
        build = next(s for s in docker['steps'] if s.get('name') == 'Build and push')
        self.assertEqual(build['with']['build-args'], 'K8S_VER=${{ matrix.version }}')
        self.assertEqual(build['with']['platforms'], 'linux/amd64,linux/arm64')
        self.assertEqual(build['with']['push'], 'true')


if __name__ == '__main__':
    unittest.main()
