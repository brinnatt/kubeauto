"""Execute the actual read-only CI probe; transport errors never mean missing."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT.parent/'kubeauto-k8s-bin-dockerfile/.github/workflows/probe-v1.35.10.yml'


class RegistryProbeContracts(unittest.TestCase):
    def test_probe_scope_and_real_failure_classification(self):
        data = yaml.load(WORKFLOW.read_text(), Loader=yaml.BaseLoader)
        self.assertEqual(data['on'], {'push': {'tags': ['probe-k8s-v1.35.10-*']}})
        self.assertNotIn('build-push-action', WORKFLOW.read_text())
        script = next(s['run'] for s in data['jobs']['probe']['steps'] if 'run' in s)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            binary = root/'docker'
            binary.write_text('''#!/usr/bin/env python3
import json,os,sys
args=sys.argv[1:]
assert args[:3]==['buildx','imagetools','inspect'] and args[-1]=='--raw'
mode=os.environ['PROBE_CASE']
if mode=='network': print('connection refused',file=sys.stderr);sys.exit(1)
if mode=='auth': print('unauthorized: authentication required',file=sys.stderr);sys.exit(1)
if mode=='absent' and args[3].endswith(':v1.35.10'):
 print(args[3]+': not found',file=sys.stderr);sys.exit(1)
platforms=['amd64','arm64'] if mode!='incomplete' else ['amd64']
print(json.dumps({'manifests':[{'platform':{'os':'linux','architecture':a},'digest':'sha256:'+'1'*64} for a in platforms]}))
''')
            binary.chmod(0o700)
            for case in ['exists','absent','network','auth','incomplete']:
                with self.subTest(case=case):
                    work=root/case;work.mkdir()
                    result=subprocess.run(['bash','-c',script],cwd=work,capture_output=True,text=True,
                        env=os.environ|{'PATH':str(root)+os.pathsep+os.environ['PATH'],'PROBE_CASE':case})
                    receipt=work/'evidence/probe.json'
                    if case in ['exists','absent']:
                        self.assertEqual(result.returncode,0,result.stderr)
                        records=json.loads(receipt.read_text())['records']
                        self.assertEqual(len(records),4)
                        self.assertEqual([r['exists'] for r in records],[True,case=='exists',True,case=='exists'])
                    else:
                        self.assertNotEqual(result.returncode,0)
                        self.assertFalse(receipt.exists())
                        self.assertNotIn('K8S_REGISTRY_PROBE_PASS',result.stdout)
