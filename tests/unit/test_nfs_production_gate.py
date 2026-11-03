"""Exercise the product NFS handoff, data assertions and export cleanup order."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]


class NfsProductionGateTests(unittest.TestCase):
    def run_gate(self, failure=''):
        with tempfile.TemporaryDirectory() as temporary:
            base=Path(temporary)
            for directory in ('bin','tests/helpers','clusters/owned'):
                (base/directory).mkdir(parents=True)
            work=base/'clusters/owned'
            config=(ROOT/'conf/config.yml').read_text().replace('__nfs_provisioner__','v4.0.2')
            (work/'config.yml').write_text(config)
            for filename in ('delivery-gap-retest.sh','nfs-production-fixture.py'):
                source=(ROOT/'tests/helpers'/filename).read_text()
                source=source.replace('BASE=/usr/local/kubeauto',f'BASE={base}',1)
                source=source.replace('PATH=/usr/local/bin:/usr/bin:$PATH','PATH="$PATH"',1)
                (base/'tests/helpers'/filename).write_text(source)
            source=(ROOT/'tests/helpers/nfs-production-gate.sh').read_text()
            source=source.replace('/var/tmp/kubeauto-nfs-production.',str(base/'evidence.'))
            (base/'gate.sh').write_text(source)
            (base/'template').write_bytes((ROOT/'roles/cluster-addon/templates/nfs-provisioner/nfs-provisioner.yaml.j2').read_bytes())
            program='''#!/usr/bin/env python3
import json, os, pathlib, sys
import yaml
from jinja2 import Environment
root=pathlib.Path(os.environ['TEST_ROOT']);a=sys.argv[1:];fail=os.environ['TEST_FAILURE']
tool=pathlib.Path(sys.argv[0]).name;work=root/'clusters/owned'
with (root/'trace').open('a') as out:out.write(json.dumps([tool]+a)+'\\n')
if tool=='ssh':
 assert 'root@192.168.122.238' in a and 'BatchMode=yes' in a
 assert 'never a test disk' in sys.stdin.read()
 if 'prepare' in a[-1]:
  assert not (root/'server').exists()
  (root/'server').touch()
 else:
  assert not (root/'namespace').exists() and not (root/'deployment').exists()
  (root/'server').unlink(missing_ok=True)
elif tool=='kubecli' and a[0]=='setup':
 assert a==['setup','owned','07']
 assert (root/'server').exists()
 cfg=yaml.safe_load((work/'config.yml').read_text())
 assert cfg['nfs_provisioner_install']=='yes'
 assert cfg['nfs_server']=='192.168.122.238' and cfg['nfs_path']=='/var/tmp/kubeauto-nfs-production-data'
 for name in ('nacos_install','minio_install','prom_install','rocketmq_install'):assert cfg[name]=='no'
 if fail=='setup':sys.exit(19)
 target=work/'yml/nfs-provisioner/nfs-provisioner.yaml';target.parent.mkdir(parents=True)
 target.write_text(Environment().from_string((root/'template').read_text()).render(cfg))
 docs=list(yaml.safe_load_all(target.read_text()))
 deployment=next(d for d in docs if d['kind']=='Deployment')
 container=deployment['spec']['template']['spec']['containers'][0]
 assert container['image']=='registry.talkschool.cn:5000/brinnatt/nfs-subdir-external-provisioner:v4.0.2'
 assert not container.get('args')
 assert next(d for d in docs if d['kind']=='Role')['rules'][0]['resources']==['endpoints']
 (root/'deployment').touch()
elif tool=='kubectl':
 if 'get' in a and 'namespace' in a:sys.exit(0 if fail=='foreign-namespace' or (root/'namespace').exists() else 1)
 if 'get' in a and 'deployment' in a:sys.exit(0 if fail=='foreign-deployment' or (root/'deployment').exists() else 1)
 if 'get' in a and 'pv' in a:
  print(json.dumps({'items':[{'spec':{'claimRef':{'namespace':'kubeauto-nfs-production'}}}] if fail=='pv-residue' else []}))
 elif 'get' in a and ('pods' in a or 'pod' in a):
  replacement=(root/'replaced').exists()
  print(('same-uid' if fail=='same-leader' else 'new-uid' if replacement else 'old-uid') if 'uid' in a[-1] else 'leader-original')
 elif 'create' in a and 'namespace' in a:(root/'namespace').touch()
 elif 'delete' in a:
  if 'namespace' in a:(root/'namespace').unlink(missing_ok=True)
  elif 'deployment' in a:(root/'deployment').unlink(missing_ok=True)
  elif 'pod' in a:(root/'replaced').touch()
 elif 'apply' in a:
  doc=yaml.safe_load(sys.stdin.read())
  name=doc['metadata']['name']
  if doc['kind']=='Pod':
   assert doc['spec']['nodeSelector']['kubernetes.io/hostname']=='rocky9-worker'+name[-1]
   assert doc['spec']['volumes'][0]['persistentVolumeClaim']['claimName']=='rwx-'+name.split('-')[1]
  else:assert doc['spec']['accessModes']==['ReadWriteMany'] and doc['spec']['storageClassName']=='managed-nfs-storage'
 elif 'exec' in a:
  name=a[a.index('exec')+1];suffix=name.split('-')[1];script=a[a.index('-e')+1]
  if 'fsyncSync' in script:
   assert 'writeSync' in script
   (root/('data-'+suffix)).write_text(a[-1])
  else:print('corrupt' if fail=='data-corrupt' else (root/('data-'+suffix)).read_text())
 elif 'logs' in a:print('unrelated controller' if fail=='leader-not-started' else 'Started provisioner controller')
'''
            for name in ('ssh','kubecli','kubectl'):
                executable=base/'bin'/name
                executable.write_text(program)
                executable.chmod(0o755)
            sleep=base/'bin/sleep';sleep.write_text('#!/bin/sh\nexit 0\n');sleep.chmod(0o755)
            launch=f'''set -euo pipefail
BASE={base}
WORK={work}
source "$BASE/tests/helpers/delivery-gap-retest.sh"
source "$BASE/gate.sh"
SSH=(ssh -o BatchMode=yes)
verify_nfs_production owned "$WORK/config.yml" run-owned
'''
            result=subprocess.run(['bash','-c',launch],capture_output=True,text=True,timeout=25,
                                  env={**os.environ,'PATH':str(base/'bin')+':'+str(Path(sys.executable).parent)+':'+os.environ['PATH'],
                                       'TEST_ROOT':str(base),'TEST_FAILURE':failure})
            trace=[json.loads(line) for line in (base/'trace').read_text().splitlines()]
            residue={name:(base/name).exists() for name in ('namespace','deployment','server')}
            return result,trace,residue

    def test_product_render_rwx_fsync_replacement_and_cleanup(self):
        result,trace,residue=self.run_gate()
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertIn('NFS_LEADER_REPLACEMENT_BUSINESS_PASS',result.stdout)
        self.assertEqual(result.stdout.count('NFS_RWX_FSYNC_READBACK_PASS'),3)
        self.assertIn('NFS_PRODUCTION_SCOPED_CLEAN_PASS',result.stdout)
        self.assertFalse(any(residue.values()))
        deleted=next(i for i,row in enumerate(trace) if 'delete' in row and 'namespace' in row)
        server_clean=next(i for i,row in enumerate(trace) if row[0]=='ssh' and 'cleanup' in row[-1])
        self.assertLess(deleted,server_clean)

    def test_failure_cannot_sign_business_or_export_cleanup(self):
        for failure in ('setup','same-leader','data-corrupt','leader-not-started','pv-residue'):
            with self.subTest(failure=failure):
                result,trace,residue=self.run_gate(failure)
                self.assertNotEqual(result.returncode,0,result.stdout+result.stderr)
                self.assertNotIn('NFS_PRODUCTION_SCOPED_CLEAN_PASS',result.stdout)
                if failure!='pv-residue':self.assertFalse(any(residue.values()))
                else:self.assertTrue(residue['server'])

    def test_foreign_namespace_or_deployment_refuses_before_server_mutation(self):
        for failure in ('foreign-namespace','foreign-deployment'):
            result,trace,residue=self.run_gate(failure)
            self.assertNotEqual(result.returncode,0)
            self.assertFalse(any(row[0]=='ssh' for row in trace))
            self.assertFalse(any(residue.values()))


if __name__=='__main__':unittest.main()
