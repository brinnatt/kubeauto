"""Use the shared read-only publisher; transport errors never mean absence."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

import yaml
from upgrade.product.hop import TARGET

ROOT = Path(__file__).resolve().parents[2]
SIBLING = ROOT.parent/'kubeauto-k8s-bin-dockerfile'


class RegistryProbeContracts(unittest.TestCase):
    def test_probe_scope_and_real_failure_classification(self):
        workflow = yaml.load((SIBLING/'.github/workflows/build.yml').read_text(), Loader=yaml.BaseLoader)
        self.assertEqual(workflow['on'], {'push': {'tags': ['artifacts-*', 'verify-artifacts-*']}})
        child = b'{"schemaVersion":2,"layers":[]}'
        digest = 'sha256:'+hashlib.sha256(child).hexdigest()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root/'bin').mkdir()
            docker = root/'bin/docker'
            docker.write_text('''#!/usr/bin/env python3
import json,os,sys
args=sys.argv[1:]
assert args[:3]==['buildx','imagetools','inspect'] and args[-1]=='--raw'
mode=os.environ['PROBE_CASE']
if mode=='network': print('connection refused',file=sys.stderr);sys.exit(1)
if mode=='auth': print('unauthorized: authentication required',file=sys.stderr);sys.exit(1)
if mode=='absent': print(args[3]+': not found',file=sys.stderr);sys.exit(1)
if '@' in args[3]: print('{"schemaVersion":2,"layers":[]}',end='');sys.exit(0)
platforms=['amd64','arm64'] if mode!='incomplete' else ['amd64']
print(json.dumps({'manifests':[{'platform':{'os':'linux','architecture':a},'digest':os.environ['CHILD_DIGEST']} for a in platforms]}))
''')
            docker.chmod(0o700)
            for case in ['exists','absent','network','auth','incomplete']:
                with self.subTest(case=case):
                    work = root/case
                    (work/'upgrade').mkdir(parents=True)
                    for name in ['Dockerfile','multi-platform-download.sh','upgrade/release.py','upgrade/release.json']:
                        shutil.copyfile(SIBLING/name, work/name)
                    # The publisher fingerprints actual Git input, so use an
                    # isolated real repository rather than stubbing git reads.
                    for args in [['init','-q'],['add','.'],['-c','user.name=fixture',
                            '-c','user.email=fixture@invalid','commit','-qm','fixture']]:
                        subprocess.run(['git',*args],cwd=work,check=True,capture_output=True)
                    request = json.dumps({'mode':'verify',
                        'selected':['brinnatt/kubeauto-k8s-bin:'+TARGET]})
                    result = subprocess.run([sys.executable,'-B','upgrade/release.py','plan',
                        '--request',request],cwd=work,capture_output=True,text=True,
                        env={**os.environ,'PATH':str(root/'bin')+os.pathsep+os.environ['PATH'],
                            'PROBE_CASE':case,'CHILD_DIGEST':digest},timeout=15)
                    receipt = work/'upgrade/output/plan.json'
                    if case == 'exists':
                        self.assertEqual(result.returncode,0,result.stderr)
                        plan=json.loads(receipt.read_text())
                        self.assertEqual(plan['mode'],'verify')
                        self.assertEqual(plan['images'],['brinnatt/kubeauto-k8s-bin:'+TARGET])
                        self.assertEqual([item['action'] for item in plan['items']],['reuse'])
                    else:
                        self.assertNotEqual(result.returncode,0)
                        self.assertFalse(receipt.exists())
                        self.assertNotIn('ARTIFACT_RELEASE_PASS',result.stdout)
