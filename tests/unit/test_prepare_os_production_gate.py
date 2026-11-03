"""Execute only-prepare handoff, refusal and cleanup on two disjoint OS targets."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]


class PrepareOsProductionGateTests(unittest.TestCase):
    def run_gate(self, failure=''):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            for directory in ('conf', 'bin', 'tests/helpers'):
                (base/directory).mkdir(parents=True)
            (base/'.venv').symlink_to(ROOT/'.venv', target_is_directory=True)
            (base/'conf/config.yml').write_bytes((ROOT/'conf/config.yml').read_bytes())
            for name in ('delivery-gap-retest.sh', 'prepare-os-production-gate.sh'):
                source = (ROOT/'tests/helpers'/name).read_text()
                source = source.replace('BASE=/usr/local/kubeauto', f'BASE={base}', 1)
                source = source.replace('PATH=/usr/local/bin:/usr/bin:$PATH', 'PATH="$PATH"', 1)
                source = source.replace('PATH="/usr/local/bin:/usr/bin:/bin:$PATH"', 'PATH="$PATH"', 1)
                source = source.replace('/var/tmp/kubeauto-prepare-compat.', str(base/'evidence.'))
                (base/'tests/helpers'/name).write_text(source)
            program = '''#!/usr/bin/env python3
import json,os,pathlib,shlex,shutil,subprocess,sys
import yaml
root=pathlib.Path(os.environ['TEST_ROOT']);a=sys.argv[1:];fail=os.environ['TEST_FAILURE']
tool=pathlib.Path(sys.argv[0]).name;work=root/'clusters/dev-prepare-compat'
with (root/'trace').open('a') as f:f.write(json.dumps([tool]+a)+'\\n')
if tool=='systemctl':
 print('active' if fail=='cleanup-runtime' else 'inactive');sys.exit(0)
if tool=='kubecli':
 if a[0]=='new':
  work.mkdir(parents=True);(work/'config.yml').write_bytes((root/'conf/config.yml').read_bytes());(work/'kubectl.kubeconfig').write_text(json.dumps({'clusters':[{'name':'owned','cluster':{'certificate-authority-data':'unique-ca','server':'https://fixture'}}],'contexts':[],'current-context':'owned','users':[{'name':'admin','user':{'client-certificate-data':'new-cert'}}]}))
 elif a[0]=='setup':
  assert a==['setup','dev-prepare-compat','01']
  cfg=yaml.safe_load((work/'config.yml').read_text())
  for k in ('KUBE_RESERVED_ENABLED','SYS_RESERVED_ENABLED','SYS_RESERVED_ENFORCE'):assert cfg[k]=='no'
  hosts=(work/'hosts').read_text()
  assert hosts.count('ansible_user=ubuntu ansible_become=true')==2
  assert 'ansible_connection=local' not in hosts
  for host in ('192.168.47.138','192.168.47.141'):
   remote=root/host
   for name in ('etc/kubernetes/ssl','root/.kube','opt'):(remote/name).mkdir(parents=True,exist_ok=True)
   data=json.loads((work/'kubectl.kubeconfig').read_text());data['users'][0]['user']['client-certificate-data']='first-cert'
   if fail=='foreign-config':data['clusters'][0]['cluster']['certificate-authority-data']='foreign-ca'
   (remote/'root/.kube/config').write_text(json.dumps(data))
   (remote/'opt/kubeauto_prepare_tasks').write_text('done')
  if fail=='setup':sys.exit(23)
  with (root/'stages').open('a') as f:f.write('01\\n')
 elif a[0]=='destroy':shutil.rmtree(work)
 else:raise AssertionError(a)
elif tool=='ssh':
 host=next(v for v in a if '@192.168.' in v)
 assert host in ('ubuntu@192.168.47.138','root@192.168.47.141')
 assert 'BatchMode=yes' in a and 'StrictHostKeyChecking=no' in a
 script=a[-1]
 script=subprocess.check_output(['bash','-c','printf \"%s\" '+script.split('bash -c ',1)[1]],text=True)
 if 'cat /root/.kube/config' in script and 'rm -f' not in script:
  path=root/host.split('@')[1]/'root/.kube/config'
  if path.exists():sys.stdout.write(path.read_text())
  sys.exit(0)
 if 'rm -f /opt/kubeauto_prepare_tasks' in script:
  remote=root/host.split('@')[1]
  for name in ('/etc/kubernetes','/etc/containerd','/var/lib/kubelet','/var/lib/containerd','/var/lib/etcd','/root/.kube/config','/opt/kubeauto_prepare_tasks'):
   script=script.replace(name,str(remote/name.lstrip('/')))
  sys.exit(subprocess.run(['bash','-c',script]).returncode)
 if 'systemctl' in script and 'is-active' in script and 'kubepods' in script:
  if fail=='active-host':sys.exit(1)
 elif '--version' in script:
  assert (root/'stages').read_text()=='01\\n01\\n'
  assert 'masked' in script and 'disabled' in script
  if fail=='tools':sys.exit(1)
 else:
  after=(root/'stages').exists()
  if not after:print('OS=fixture VERSION=current')
  if not after or fail!='package-removed':print('firewall-package')
  print('network-package')
'''
            for name in ('ssh', 'kubecli', 'systemctl'):
                executable=base/'bin'/name
                executable.write_text(program)
                executable.chmod(0o755)
            if failure=='foreign-cluster':(base/'clusters/dev-prepare-compat').mkdir(parents=True)
            result=subprocess.run(['bash',str(base/'tests/helpers/prepare-os-production-gate.sh')],
                                  capture_output=True,text=True,timeout=25,
                                  env={**os.environ,'PATH':str(base/'bin')+':'+str(Path(sys.executable).parent)+':'+os.environ['PATH'],
                                       'TEST_ROOT':str(base),'TEST_FAILURE':failure})
            trace=[json.loads(v) for v in (base/'trace').read_text().splitlines()] if (base/'trace').exists() else []
            return result,trace,(base/'clusters/dev-prepare-compat').exists()

    def test_only_prepare_twice_then_scoped_cleanup(self):
        result,trace,exists=self.run_gate()
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertFalse(exists)
        self.assertEqual([r[1:] for r in trace if r[0]=='kubecli'],[
            ['new','dev-prepare-compat'],['setup','dev-prepare-compat','01'],
            ['setup','dev-prepare-compat','01']])
        self.assertEqual(result.stdout.count('PREPARE_OS_HOST_PASS'),2)
        self.assertIn('PREPARE_OS_FOCUSED_PASS',result.stdout)
        self.assertIn('PREPARE_OS_SCOPED_CLEAN_PASS',result.stdout)
        self.assertIn('LAB_CLEAN_VERIFY_PASS',result.stdout)

    def test_failures_never_sign_and_only_owned_cluster_is_destroyed(self):
        for failure in ('active-host','foreign-cluster','setup','tools','package-removed'):
            with self.subTest(failure=failure):
                result,trace,exists=self.run_gate(failure)
                self.assertNotEqual(result.returncode,0,result.stdout+result.stderr)
                self.assertNotIn('PREPARE_OS_FOCUSED_PASS',result.stdout)
                self.assertNotIn('LAB_CLEAN_VERIFY_PASS',result.stdout)
                self.assertEqual(exists,failure=='foreign-cluster')
                if failure in ('active-host','foreign-cluster'):
                    self.assertFalse(any(r[0]=='kubecli' for r in trace))

    def test_cleanup_refuses_active_runtime_or_foreign_kubeconfig(self):
        for failure in ('cleanup-runtime', 'foreign-config'):
            result,trace,exists=self.run_gate(failure)
            self.assertNotEqual(result.returncode,0,result.stdout+result.stderr)
            self.assertTrue(exists)
            self.assertNotIn('PREPARE_OS_SCOPED_CLEAN_PASS',result.stdout)
            self.assertFalse(any(r[:2]==['kubecli','destroy'] for r in trace))

    def test_ufw_masked_or_disabled_is_accepted_and_enabled_is_refused(self):
        source=(ROOT/'tests/helpers/prepare-os-production-gate.sh').read_text()
        body='for service in firewalld ufw; do'+source.split('for service in firewalld ufw; do',1)[1].split("done'",1)[0]+'done'
        for status in ('masked','disabled','enabled'):
            script='set -eu\nsystemctl() {\n case "$1" in\n show) echo loaded;;\n is-active) echo inactive; return 3;;\n is-enabled) if test "$2" = firewalld.service; then echo masked; else echo "$STATUS"; fi;;\n esac\n}\n'+body
            result=subprocess.run(['bash','-c',script],capture_output=True,text=True,
                                  env={**os.environ,'STATUS':status})
            self.assertEqual(result.returncode==0,status!='enabled',result.stderr)

    def test_outer_lease_precedes_sync_and_outlives_product_cleanup(self):
        source=(ROOT/'tests/run_enterprise_regression.sh').read_text()
        branch=source.split('if [[ "$MODE" == "--prepare-os-only" || "$MODE" == "--prepare-os-clean-only" ]]; then',1)[1].split('if [[ "$MODE" == "--rocky9-production-only"',1)[0]
        self.assertIn('/var/lock/kubeauto-core-pool.lock',branch)
        self.assertLess(branch.index('read -r prepare_lease_marker'),branch.index('sync-kubeauto.sh'))
        self.assertLess(branch.index('monitor_remote_job prepare-os-138'),branch.index('release_prepare_lease\n'))
        for forbidden in ('lab-wipe-nodes.sh','--all-delivery','cancel_remote_job'):
            self.assertNotIn(forbidden,branch)


if __name__=='__main__':unittest.main()
