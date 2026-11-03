"""Execute the Rocky 9 regression's real sequencing and refusal boundaries."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
HOSTS = {'192.168.122.212', '192.168.122.165', '192.168.122.238'}


class Rocky9ProductionGateTests(unittest.TestCase):
    def run_gate(self, failure=''):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            for name in ('conf', 'bin', 'tests/helpers'):
                (base/name).mkdir(parents=True)
            (base/'conf/config.yml').write_bytes((ROOT/'conf/config.yml').read_bytes())
            library = (ROOT/'tests/helpers/delivery-gap-retest.sh').read_text()
            library = library.replace('BASE=/usr/local/kubeauto', f'BASE={base}', 1)
            library = library.replace('PATH=/usr/local/bin:/usr/bin:$PATH', 'PATH="$PATH"', 1)
            (base/'tests/helpers/delivery-gap-retest.sh').write_text(library)
            helper = (ROOT/'tests/helpers/rocky9-production-gate.sh').read_text()
            helper = helper.replace('BASE=/usr/local/kubeauto',f'BASE={base}',1)
            helper = helper.replace('PATH="/usr/local/bin:/usr/bin:/bin:$PATH"','PATH="$PATH"',1)
            helper = helper.replace('/var/tmp/kubeauto-rocky9-production.',str(base/'evidence.'))
            helper = helper.replace('/var/lib/kubeauto-ceph-test', str(base/'relay'))
            (base/'gate.sh').write_text(helper)
            program = '''#!/usr/bin/env python3
import json, locale, os, pathlib, shutil, sys
import yaml
a=sys.argv[1:]; root=pathlib.Path(os.environ['TEST_ROOT']); fail=os.environ['TEST_FAILURE']
tool=pathlib.Path(sys.argv[0]).name; work=root/'clusters/dev-rocky9-production'
with (root/'trace').open('a') as out: out.write(json.dumps([tool]+a)+'\\n')
if tool=='kubecli':
 if a[0]=='new': assert (root/'relay/ssh-relay.json').exists()
 locale.setlocale(locale.LC_ALL, '')
 assert locale.getencoding().lower().replace('-','')=='utf8', 'Ansible requires UTF-8 locale'
 if a[0]=='new':
  work.mkdir(parents=True); (work/'config.yml').write_bytes((root/'conf/config.yml').read_bytes())
 elif a[0]=='setup':
  cfg=yaml.safe_load((work/'config.yml').read_text())
  for k in ('KUBE_RESERVED_ENABLED','SYS_RESERVED_ENABLED','SYS_RESERVED_ENFORCE'): assert cfg[k]=='no'
  for k in ('nacos_install','minio_install','rocketmq_install','prom_install','openebs_install'): assert cfg[k]=='no'
  assert cfg['K8S_VER']=='1.33.6'
  inventory=(work/'hosts').read_text()
  for address in ('192.168.122.212','192.168.122.165','192.168.122.238'): assert address in inventory
  state=root/'stages'; stages=state.read_text().split() if state.exists() else []
  expected=['01','90','01']; assert a[-1]==expected[len(stages)]
  if fail=='setup-'+a[-1]: sys.exit(23)
  state.write_text(' '.join(stages+[a[-1]])); (work/'kubectl.kubeconfig').write_text('fixture-only')
 elif a[0]=='destroy': shutil.rmtree(work)
elif tool=='ssh':
 host=next(v for v in a if v.startswith('root@')).split('@')[1]
 assert host in ('192.168.122.212','192.168.122.165','192.168.122.238')
 assert 'BatchMode=yes' in a
 script=a[-1]
 if 'curl --fail' in script:
  assert (root/'relay/ssh-relay.json').exists()
  assert not work.exists()
  assert 'pause:3.10 json-mock:v1.3.1' in script
  if fail=='registry':sys.exit(1)
  sys.exit(0)
 if 'VERSION_ID' in script:
  assert 'dnf install -y firewalld' in script and 'systemctl enable firewalld.service' in script
  if fail=='wrong-os':sys.exit(1)
  print('ROCKY9_HOST os=rocky version=9.8 kernel=fixture')
 elif 'systemctl unmask' in script:
  assert (root/'stages').read_text()=='01 90'
  assert 'systemctl enable firewalld.service' in script
  sys.exit(0)
 else:
  assert (root/'stages').read_text()=='01 90 01'
  if '--version' in script:
   assert 'masked' in script and 'inactive' in script and '/opt/kubeauto_prepare_tasks' in script
   if fail=='network-tools':sys.exit(1)
 if fail!='removed-package' or 'VERSION_ID' in script:print('firewalld.noarch')
 print('ipset.x86_64')
elif tool=='kubectl':print('rocky9-master Ready\\nrocky9-worker1 Ready\\nrocky9-worker2 Ready')
'''
            for name in ('kubecli', 'ssh', 'kubectl'):
                path=base/'bin'/name
                path.write_text(program)
                path.chmod(0o755)
            (base/'tests/helpers/ceph_ssh_fixture.py').write_text('''import json,os,pathlib,signal,sys,time
root=pathlib.Path(os.environ['TEST_ROOT']);a=sys.argv[1:];path=pathlib.Path(a[a.index('--path')+1])
with (root/'trace').open('a') as f:f.write(json.dumps(['relay']+a)+'\\n')
if a[0]=='setup':
 assert not path.exists() and not (root/'clusters/dev-rocky9-production').exists()
 path.write_text('[]')
 if os.environ['TEST_FAILURE']=='relay-setup':sys.exit(1)
elif a[0]=='maintain':
 signal.signal(signal.SIGTERM,lambda *_:sys.exit(0))
 while True:time.sleep(.02)
else:
 assert not (root/'clusters/dev-rocky9-production').exists()
 path.unlink()
''')
            smoke=base/'tests/helpers/kubernetes-production-smoke.sh'
            smoke.write_text('''#!/usr/bin/env bash
set -eu
test "$PRODUCTION_SMOKE_SERVER_NODE" = rocky9-worker1
test "$PRODUCTION_SMOKE_CLIENT_NODE" = rocky9-worker2
test "$PRODUCTION_SMOKE_IMAGE" = registry.talkschool.cn:5000/brinnatt/json-mock:v1.3.1
test "$(cat "$TEST_ROOT/stages")" = '01 90 01'
[[ "$TEST_FAILURE" != business ]]
echo KUBERNETES_PRODUCTION_SMOKE_PASS
''')
            if failure=='foreign-cluster':(base/'clusters/dev-rocky9-production').mkdir(parents=True)
            result=subprocess.run(['bash',str(base/'gate.sh')],capture_output=True,text=True,timeout=25,
                                  env={**os.environ,'PATH':str(base/'bin')+':'+str(Path(sys.executable).parent)+':'+os.environ['PATH'],
                                       'TEST_ROOT':str(base),'TEST_FAILURE':failure})
            trace=[json.loads(v) for v in (base/'trace').read_text().splitlines()] if (base/'trace').exists() else []
            return result,trace,(base/'clusters/dev-rocky9-production').exists()

    def test_product_prepare_network_and_cleanup_order(self):
        result,trace,exists=self.run_gate()
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertFalse(exists)
        self.assertEqual(result.stdout.count('ROCKY9_PREPARE_PASS'),3)
        self.assertIn('ROCKY9_PRODUCTION_FOCUSED_PASS',result.stdout)
        self.assertIn('ROCKY9_PRODUCTION_SCOPED_CLEAN_PASS',result.stdout)
        self.assertEqual([r[1] for r in trace if r[0]=='relay' and r[1]!='maintain'],['setup','cleanup'])
        self.assertLess(next(i for i,r in enumerate(trace) if r[:2]==['relay','setup']),next(i for i,r in enumerate(trace) if r[:2]==['kubecli','new']))
        self.assertEqual([r[-1] for r in trace if r[:2]==['kubecli','setup']],['01','90','01'])
        self.assertEqual({v.split('@')[1] for r in trace if r[0]=='ssh' for v in r if v.startswith('root@')},HOSTS)

    def test_failed_prerequisite_retention_and_business_never_sign(self):
        for failure in ('wrong-os','setup-01','setup-90','network-tools','removed-package','business','registry','relay-setup'):
            with self.subTest(failure=failure):
                result,trace,exists=self.run_gate(failure)
                self.assertNotEqual(result.returncode,0,result.stdout+result.stderr)
                self.assertNotIn('ROCKY9_PRODUCTION_FOCUSED_PASS',result.stdout)
                self.assertFalse(exists)
                if failure=='setup-01':self.assertFalse(any(r[:3]==['kubecli','setup','dev-rocky9-production'] and r[-1]=='90' for r in trace))

    def test_foreign_cluster_is_preserved(self):
        result,trace,exists=self.run_gate('foreign-cluster')
        self.assertNotEqual(result.returncode,0)
        self.assertTrue(exists)
        self.assertEqual(trace,[])

    def test_sort_locale_does_not_override_ansible_utf8_locale(self):
        source=(ROOT/'tests/helpers/rocky9-production-gate.sh').read_text()
        # Exercise the exact global exports, then Ansible's locale guard, without SSH.
        exports='\n'.join(line for line in source.splitlines() if line.startswith('export '))
        script=exports+'\n'+str(ROOT/'.venv/bin/python')+''' -c '
from ansible.cli import initialize_locale
initialize_locale()
' '''
        result=subprocess.run(['bash','-c',script],capture_output=True,text=True,
                              env={**os.environ,'LC_ALL':'C.UTF-8'})
        self.assertEqual(result.returncode,0,result.stderr)

    def test_outer_runner_uses_ceph_locks_until_scoped_final_clean(self):
        source=(ROOT/'tests/run_enterprise_regression.sh').read_text()
        branch=source.split('if [[ "$MODE" == "--rocky9-production-only" || "$MODE" == "--rocky9-nfs-only" ]]; then',1)[1].split('if [[ "$MODE" == "--nacos-startup-only" ]]',1)[0]
        for lock in ('kubeauto-ceph-priority.lock','kubeauto-ceph-compute.lock'):
            self.assertIn(lock,branch)
        self.assertLess(branch.index('read -r rocky9_lease_marker'),branch.index('sync-kubeauto.sh'))
        self.assertLess(branch.rindex('--verify --rocky9-only'),branch.index('release_rocky9_lease\n'))
        self.assertIn('monitor_remote_job rocky9-production-130 ssh130',branch)
        for forbidden in ('--all-delivery','--ceph-only','wipefs','mkfs'):
            self.assertNotIn(forbidden,branch)

    def test_cleanup_cli_selects_only_owned_hosts_and_leaves_extra_disks(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory=Path(temporary)
            ssh=directory/'ssh'
            ssh.write_text('''#!/usr/bin/env python3
import json,os,sys
with open(os.environ['TRACE'],'a') as f:f.write(json.dumps(sys.argv[1:])+'\\n')
''')
            ssh.chmod(0o755)
            for args in [('--rocky9-only',),('--verify','--rocky9-only')]:
                trace=directory/'trace';trace.unlink(missing_ok=True)
                result=subprocess.run(['bash',str(ROOT/'tests/helpers/lab-wipe-nodes.sh'),*args],capture_output=True,text=True,timeout=10,
                                      env={**os.environ,'PATH':str(directory)+':'+os.environ['PATH'],'TRACE':str(trace)})
                self.assertEqual(result.returncode,0,result.stdout+result.stderr)
                calls=[json.loads(v) for v in trace.read_text().splitlines()]
                self.assertEqual({v.split('@')[1] for r in calls for v in r if v.startswith('root@')},HOSTS)
                for call in calls:
                    self.assertNotRegex(call[-1],r'\b(?:wipefs|mkfs|sgdisk)\b|/dev/vd[b-d]')
                if args==('--rocky9-only',):self.assertIn('STARTUP_CLEAN_REFUSED',calls[0][-1])


if __name__=='__main__':unittest.main()
