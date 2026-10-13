"""Prove whole-lab cleanup refuses foreign ownership before its first mutation."""
import json
import os
from pathlib import Path
import subprocess
import shlex
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]


class LabCleanupTests(unittest.TestCase):
    def test_inherited_lease_uses_file_identity_across_symlinked_directories(self):
        # Rocky resolves /var/lock to /run/lock; pathname equality is not identity.
        for name in ('redis-lab-clean-gate.sh', 'redis-install-gate.sh', 'redis-focused-gate.sh'):
            source = (ROOT / 'tests/helpers' / name).read_text()
            lines = source.splitlines()
            end = next(i for i, line in enumerate(lines) if line.startswith('flock -n 9 '))
            guard = '\n'.join(lines[:end + 1]) + '\necho REDIS_LEASE_IDENTITY_PASS\n'
            for boundary in ('alias', 'foreign', 'replaced'):
                with self.subTest(helper=name, boundary=boundary), tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    (root / 'actual').mkdir()
                    (root / 'alias').symlink_to(root / 'actual', target_is_directory=True)
                    lease = root / 'alias/lease'
                    child = root / 'guard.sh'; child.write_text(guard)
                    expected = lease if boundary != 'foreign' else root / 'foreign'
                    prelude = 'exec 9>"$REDIS_LEASE_TEST_OPEN"\nflock -n 9\n'
                    if boundary == 'replaced':
                        prelude += 'mv "$REDIS_LEASE_TEST_OPEN" "$REDIS_LEASE_TEST_OPEN.old"\n: > "$REDIS_LEASE_TEST_OPEN"\n'
                    mode = ('sentinel-data' if name == 'redis-focused-gate.sh' else
                            '--fresh' if name == 'redis-install-gate.sh' else '--clean')
                    result = subprocess.run(['bash', '-c', prelude + 'bash "$1" "$2"',
                                             'lease-contract', str(child), mode],
                        env={**os.environ, 'KUBEAUTO_BASE':tmp, 'REDIS_PARENT_LEASE_HELD':'yes',
                             'REDIS_LAB_LEASE_FILE':str(expected), 'REDIS_LEASE_TEST_OPEN':str(lease)},
                        capture_output=True, text=True, timeout=10)
                    self.assertEqual(result.returncode == 0, boundary == 'alias', result.stdout + result.stderr)
                    self.assertEqual('REDIS_LEASE_IDENTITY_PASS' in result.stdout, boundary == 'alias')

    def fixture(self, root):
        cluster = root / 'clusters/redis-gate'
        cluster.mkdir(parents=True)
        (cluster / 'redis-lab-owner.json').write_text(json.dumps({
            'cluster':'redis-gate', 'cluster_uid':'owned-core',
            'nodes':[f'192.168.47.{i}' for i in range(131,137)]}))
        (cluster / 'config.yml').write_text('CLUSTER_NAME: redis-gate\nredis_namespace: redis-gate\n')
        namespaces = [{'metadata': {'name':name, 'uid':'owned-core' if name=='kube-system' else name}}
                      for name in ('default','kube-system','kube-public','kube-node-lease')]
        for name, component in [('redis-gate','redis'),('redis-gate-operator','redis'),
                                ('redis-lab-storage','redis-test-storage')]:
            namespaces.append({'metadata':{'name':name,'labels':{
                'kubeauto.io/component':component,'kubeauto.io/managed-by':'kubeauto'}}})
        nodes = [{'status':{'addresses':[{'type':'InternalIP','address':f'192.168.47.{i}'}]}}
                 for i in range(131,137)]
        data = {'namespace':namespaces,'nodes':nodes}
        for directory in ('.venv/bin','kube-bin','tests/helpers'):
            (root / directory).mkdir(parents=True)
        python = root / '.venv/bin/python'
        python.write_text('#!/bin/sh\nexec '+shlex.quote(sys.executable)+' "$@"\n')
        python.chmod(0o755)
        kubectl = root / 'kube-bin/kubectl'
        kubectl.write_text('#!'+sys.executable+'\nimport json,os,sys\n'
                           'data=json.load(open(os.environ["REDIS_CLEAN_TEST_DATA"]))\n'
                           'kind=sys.argv[sys.argv.index("get")+1]\n'
                           'print(json.dumps({"items":data[kind]}))\n')
        kubectl.chmod(0o755)
        # Any entry here proves the ownership guard permitted mutation.
        (root/'tests/helpers/redis-cleanup.sh').write_text(
            'printf mutation > "$REDIS_CLEAN_TEST_MUTATION"\nexit 42\n')
        return cluster, data

    def test_exact_owned_baseline_reaches_cleanup_but_foreign_boundaries_never_do(self):
        for change in ('valid','foreign-node','foreign-namespace','wrong-label','wrong-uid',
                       'fault','cluster-symlink','unowned-cluster-fixture'):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp)
                cluster, data=self.fixture(root)
                if change=='foreign-node':
                    data['nodes'][0]['status']['addresses'][0]['address']='192.168.47.129'
                elif change=='foreign-namespace':
                    data['namespace'].append({'metadata':{'name':'customer'}})
                elif change=='wrong-label':
                    data['namespace'][-1]['metadata']['labels']['kubeauto.io/component']='foreign'
                elif change=='wrong-uid':
                    data['namespace'][1]['metadata']['uid']='foreign-core'
                elif change=='fault':
                    (cluster/'redis-maintenance-fault.json').write_text('{}')
                elif change=='cluster-symlink':
                    cluster.rename(root/'foreign')
                    cluster.symlink_to(root/'foreign',target_is_directory=True)
                elif change=='unowned-cluster-fixture':
                    (root/'clusters/redis-cluster-gate').mkdir()
                fixture=root/'data.json';fixture.write_text(json.dumps(data))
                mutation=root/'mutation'
                result=subprocess.run(['bash',str(ROOT/'tests/helpers/redis-lab-clean-gate.sh'),'--clean'],
                    env={**os.environ,'KUBEAUTO_BASE':str(root), 'REDIS_LAB_LEASE_FILE':str(root/'lease'),
                         'REDIS_CLEAN_TEST_DATA':str(fixture),'REDIS_CLEAN_TEST_MUTATION':str(mutation)},
                    capture_output=True,text=True,timeout=15)
                self.assertNotEqual(result.returncode,0,result.stdout+result.stderr)
                self.assertEqual(mutation.exists(),change=='valid',result.stdout+result.stderr)
                self.assertNotIn('REDIS_LAB_CLEANUP_PASS',result.stdout)

    def test_inherited_lease_must_refer_to_exact_redis_pool(self):
        with tempfile.TemporaryDirectory() as tmp:
            result=subprocess.run(['bash',str(ROOT/'tests/helpers/redis-lab-clean-gate.sh')],
                env={**os.environ,'KUBEAUTO_BASE':tmp,'REDIS_PARENT_LEASE_HELD':'yes',
                     'REDIS_LAB_LEASE_FILE':tmp+'/lease'},capture_output=True,text=True,timeout=10)
            self.assertNotEqual(result.returncode,0)
            self.assertNotIn('REDIS_LAB_CLEANUP_PASS',result.stdout)


if __name__=='__main__':
    unittest.main()
