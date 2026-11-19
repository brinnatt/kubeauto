"""Execute the exact CI readback with transport fixtures; refuse false success."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[2]
REPO = ROOT.parent / 'kubeauto-k8s-bin-dockerfile'
WORKFLOW = REPO / '.github/workflows/build.yml'


@unittest.skipUnless(WORKFLOW.exists(), 'readback workflow checkout required')
class ReadbackWorkflowContracts(unittest.TestCase):
    def test_real_step_checks_both_registries_and_preserves_failure(self):
        workflow = yaml.load(WORKFLOW.read_text(), Loader=yaml.BaseLoader)
        self.assertIn('release', workflow['jobs'])
        self.assertEqual(workflow['on'], {'push': {'tags': ['artifacts-*', 'verify-artifacts-*']}})
        script=next(s['run'] for s in workflow['jobs']['release']['steps'] if 'PIPESTATUS' in s.get('run',''))
        code="import sys; sys.path.insert(0,"+repr(str(REPO/'upgrade'))+"); import release; catalog=release.load_catalog(); req={'mode':'readback','selected':['brinnatt/kubeauto-k8s-bin:v1.34.13']}; plan=release.resolve(catalog,req,release.select(catalog,req)); release.save(plan); release.execute(catalog,plan)"
        script=script.replace('python3 -B upgrade/release.py run', 'python3 -B -c '+__import__('shlex').quote(code))
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
    data={'manifests':manifests}
    if os.environ.get('FAILURE') == 'digest' and image.startswith('docker.io/'):
        data['annotations']={'changed':'identity'}
    sys.stdout.write(json.dumps(data))
''')
            docker.chmod(0o755)
            env = os.environ | {'PATH': str(fakebin) + os.pathsep + os.environ['PATH']}
            for failure in ('', 'transport', 'child', 'digest', 'missing', 'duplicate'):
                with self.subTest(failure=failure):
                    work = root / ('run-' + (failure or 'success'))
                    work.mkdir()
                    import shutil
                    shutil.copytree(REPO/'upgrade',work/'upgrade',ignore=shutil.ignore_patterns('output','__pycache__'))
                    shutil.copyfile(REPO/'Dockerfile',work/'Dockerfile')
                    shutil.copyfile(REPO/'multi-platform-download.sh',work/'multi-platform-download.sh')
                    subprocess.run(['git','init','-q',str(work)],check=True)
                    subprocess.run(['git','-C',str(work),'-c','user.name=fixture','-c','user.email=fixture@example.invalid','commit','--allow-empty','-qm','fixture'],check=True)
                    result = subprocess.run(['bash', '-euo', 'pipefail', '-c', script],
                                            cwd=work, env=env | {'FAILURE': failure},
                                            capture_output=True, text=True)
                    status = (work / 'evidence/release.rc').read_text().strip()
                    receipt = work / 'upgrade/output/plan.json'
                    if failure:
                        self.assertNotEqual(result.returncode, 0)
                        self.assertNotEqual(status, 'rc=0')
                        if receipt.exists():
                            self.assertNotEqual(json.loads(receipt.read_text()).get('status'), 'pass')
                        self.assertNotIn('ARTIFACT_RELEASE_PASS', result.stdout)
                    else:
                        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                        self.assertEqual(status, 'rc=0')
                        data = json.loads(receipt.read_text())
                        self.assertEqual(data['mode'],'readback')
                        self.assertEqual(data['status'],'pass')
                        self.assertEqual(data['items'][0]['action'],'reuse')
                        self.assertEqual(len(data['items'][0]['identity']['platforms']),2)
                        commands=[json.loads(line) for line in (work/'commands.jsonl').read_text().splitlines()]
                        self.assertTrue(all(c[:3]==['buildx','imagetools','inspect'] for c in commands))


if __name__ == '__main__':
    unittest.main()
