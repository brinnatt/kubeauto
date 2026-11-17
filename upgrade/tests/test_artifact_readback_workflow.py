"""Execute the exact CI readback with transport fixtures; refuse false success."""
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
WORKFLOW = REPO / '.github/workflows/verify-v1.34.13.yml'


@unittest.skipUnless(WORKFLOW.exists(), 'readback workflow checkout required')
class ReadbackWorkflowContracts(unittest.TestCase):
    def test_real_step_checks_both_registries_and_preserves_failure(self):
        workflow = yaml.load(WORKFLOW.read_text(), Loader=yaml.BaseLoader)
        self.assertEqual(set(workflow['jobs']), {'verify'})
        self.assertEqual(workflow['on'], {'push': {'tags': ['verify-k8s-v1.34.13-*']}})
        build_workflow = yaml.load((REPO / '.github/workflows/build.yml').read_text(),
                                   Loader=yaml.BaseLoader)
        self.assertEqual(build_workflow['on'], {'push': {'tags': ['v[0-9]*.[0-9]*.[0-9]*']}})
        self.assertNotIn('docker/build-push-action', WORKFLOW.read_text())
        script = next(s['run'] for s in workflow['jobs']['verify']['steps'] if 'run' in s)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fakebin = root / 'bin'
            fakebin.mkdir()
            docker = fakebin / 'docker'
            docker.write_text('''#!/usr/bin/env python3
import hashlib, json, os, sys
args = sys.argv[1:]
assert args[:3] == ['buildx', 'imagetools', 'inspect'] and args[-1] == '--raw'
image = args[3]
assert any(image.startswith(p) for p in (
    'hub.talkedu.cn/kubeauto/kubeauto-k8s-bin', 'docker.io/brinnatt/kubeauto-k8s-bin'))
with open('commands.jsonl', 'a') as log: log.write(json.dumps(args) + '\\n')
if os.environ.get('FAILURE') == 'transport': sys.exit(7)
child = b'{"schemaVersion":2,"layers":[]}'
digest = 'sha256:' + hashlib.sha256(child).hexdigest()
if '@' in image:
    assert image.endswith('@' + digest)
    sys.stdout.buffer.write(b'{}' if os.environ.get('FAILURE') == 'child' else child)
else:
    manifests = [{'digest': digest, 'platform': {'os':'linux','architecture':a}}
                 for a in ('amd64','arm64')]
    if os.environ.get('FAILURE') == 'missing': manifests.pop()
    if os.environ.get('FAILURE') == 'duplicate': manifests.append(manifests[0])
    sys.stdout.write(json.dumps({'manifests':manifests}))
''')
            docker.chmod(0o755)
            child_digest = 'sha256:' + hashlib.sha256(b'{"schemaVersion":2,"layers":[]}').hexdigest()
            index = json.dumps({'manifests': [
                {'digest': child_digest, 'platform': {'os': 'linux', 'architecture': a}}
                for a in ('amd64', 'arm64')]}).encode()
            expected = 'sha256:' + hashlib.sha256(index).hexdigest()
            env = os.environ | {'PATH': str(fakebin) + os.pathsep + os.environ['PATH'],
                                'EXPECTED_DIGEST': expected}
            for failure in ('', 'transport', 'child', 'digest', 'missing', 'duplicate'):
                with self.subTest(failure=failure):
                    work = root / ('run-' + (failure or 'success'))
                    work.mkdir()
                    result = subprocess.run(['bash', '-euo', 'pipefail', '-c', script],
                                            cwd=work, env=env | {'FAILURE': failure,
                                            'EXPECTED_DIGEST': 'sha256:' + '0' * 64
                                            if failure == 'digest' else expected},
                                            capture_output=True, text=True)
                    status = (work / 'evidence/readback.status').read_text().strip()
                    receipt = work / 'evidence/readback.json'
                    if failure:
                        self.assertNotEqual(result.returncode, 0)
                        self.assertNotEqual(status, 'rc=0')
                        self.assertFalse(receipt.exists())
                        self.assertNotIn('K8S_DUAL_REGISTRY_READBACK_PASS', result.stdout)
                    else:
                        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                        self.assertEqual(status, 'rc=0')
                        data = json.loads(receipt.read_text())
                        self.assertEqual(data['published_images'], [])
                        self.assertEqual(data['built_images'], [])
                        self.assertEqual(len(data['records']), 2)
                        self.assertEqual(len((work / 'commands.jsonl').read_text().splitlines()), 6)


if __name__ == '__main__':
    unittest.main()
