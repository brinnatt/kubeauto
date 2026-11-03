"""Exercise the focused runner's real product handoff in a disposable tree."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
HELPER = ROOT/'tests/helpers/nacos-startup-gate.sh'
LIBRARY = ROOT/'tests/helpers/delivery-gap-retest.sh'


class NacosStartupGateContracts(unittest.TestCase):
    def run_gate(self, failure=''):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            (base/'tests/helpers').mkdir(parents=True)
            (base/'conf').mkdir()
            (base/'bin').mkdir()
            (base/'conf/config.yml').write_bytes((ROOT/'conf/config.yml').read_bytes())
            library = LIBRARY.read_text().replace('BASE=/usr/local/kubeauto', f'BASE={base}', 1)
            library = library.replace('PATH=/usr/local/bin:/usr/bin:$PATH', 'PATH="$PATH"', 1)
            library = library.replace('/var/tmp/kubeauto-nacos-', str(base/'kubeauto-nacos-'))
            (base/'tests/helpers/delivery-gap-retest.sh').write_text(library)
            tls = (ROOT/'tests/helpers/nacos-tls-refusal-gate.sh').read_text()
            tls = tls.replace('/var/tmp/kubeauto-nacos-tls-refusal.',str(base/'tls-refusal.'))
            (base/'tests/helpers/nacos-tls-refusal-gate.sh').write_text(tls)
            import yaml
            tasks=yaml.safe_load((ROOT/'roles/cluster-addon/tasks/main.yml').read_text())
            guard=next(t['when'] for t in tasks if t.get('import_tasks')=='nacos.yml')
            subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-days','1',
                            '-subj','/CN=fixture-ca','-keyout',str(base/'ca.key'),'-out',str(base/'ca.crt')],
                           check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            script = HELPER.read_text().replace('BASE=/usr/local/kubeauto', f'BASE={base}', 1)
            script = script.replace('PATH="/usr/local/bin:/usr/bin:/bin:$PATH"', 'PATH="$PATH"', 1)
            (base/'gate.sh').write_text(script)
            program = '''#!/usr/bin/env python3
import base64, json, os, pathlib, sys
import yaml
from jinja2 import Environment
a=sys.argv[1:]; root=pathlib.Path(os.environ['TEST_ROOT']); fail=os.environ['TEST_FAILURE']
tool=pathlib.Path(sys.argv[0]).name
with (root/'trace').open('a') as out: out.write(json.dumps([tool]+a)+'\\n')
if tool=='curl':
    assert any('/v2/' in v for v in a)
    print(json.dumps({'tags':['8.0.46']})); sys.exit(0)
if tool=='kubecli':
    work=root/'clusters/dev-nacos-startup'
    if a[0]=='new':
        work.mkdir(parents=True); (work/'config.yml').write_bytes((root/'conf/config.yml').read_bytes())
    elif a[0]=='setup':
        cfg=yaml.safe_load((work/'config.yml').read_text())
        assert cfg['KUBE_RESERVED_ENABLED']=='no' and cfg['SYS_RESERVED_ENABLED']=='no'
        assert cfg['SYS_RESERVED_ENFORCE']=='no'
        if a[-1] in ('90','01'): assert not cfg['nacos_install']=='yes'
        if fail=='setup-'+a[-1]: sys.exit(23)
        (work/'kubectl.kubeconfig').write_text('fixture-only')
        if a[-1]=='07' and cfg['nacos_install']=='yes':
            # Reproduce the real import guard with a healthy Pod already present
            # in the original namespace. It must not skip either reconciliation.
            assert Environment().compile_expression(os.environ['NACOS_IMPORT_GUARD'])(
                nacos_install=cfg['nacos_install'],pod_info={'stdout':'nacos nacos-0 1/1 Running'})
        if cfg['nacos_namespace']=='nacos-tls-refusal':
            assert a[-1]=='07' and cfg['nacos_install']=='yes'
            assert cfg['nacos_ready_timeout']==90 and cfg['nacos_mysql_ca_secret']=='nacos-mysql-ca'
            (work/'yml').mkdir(exist_ok=True)
            (work/'yml/nacos-sts.yaml').write_text('kind: StatefulSet\\nmetadata: {namespace: nacos-tls-refusal}')
            sys.exit(0 if fail=='tls-unexpected-success' else 17)
    elif a[0]=='destroy':
        cfg=yaml.safe_load((work/'config.yml').read_text()); assert cfg['nacos_namespace']=='nacos'
        assert cfg['nacos_ready_timeout']==900
        import shutil; shutil.rmtree(work)
    sys.exit(0)
if tool=='kubectl':
    if 'get' in a and ('namespace' in a or 'ns' in a):
        sys.exit(0 if fail=='foreign-namespace' or (fail=='tls-foreign-namespace' and 'nacos-tls-refusal' in a) else 1)
    if 'get' in a:
        if 'service' in a and 'nacos-mysql-wrong' in a: sys.exit(1)
        if 'secret' in a and 'nacos-mysql-tls' in a:
            print(base64.b64encode((root/'ca.crt').read_bytes()).decode())
        elif 'pv' in a:
            print(json.dumps({'items':[{'spec':{'claimRef':{'namespace':'nacos-tls-refusal'}}}] if fail=='tls-pv-residue' else []}))
        elif 'nodes' in a:
            count=2 if fail=='few-workers' else 4
            print('\\n'.join('node-%d Ready'%i for i in range(count)))
        elif 'statefulset' in a: print('mysql')
        elif 'pods' in a: print('nacos-0 1/1 Running\\nnacos-1 1/1 Running\\nnacos-2 1/1 Running')
        elif 'nacos-0' in a: print('10.70.1.4')
        else: print('mysql-fixture')
    elif 'exec' in a:
        if 'nacos-tls-refusal' in a:
            cfg=yaml.safe_load((root/'clusters/dev-nacos-startup/config.yml').read_text())
            print('unrelated database timeout' if fail=='tls-unrelated' else 'Server identity verification failed' if 'wrong' in cfg['nacos_mysql_host'] else 'Path does not chain with any of the trust anchors')
            sys.exit(0)
        if 'mysql' in a:
            print('12'); sys.exit(0)
        if 'cat' in a: print('CREATE TABLE fixture (id int);'); sys.exit(0)
        url=next((v for v in a if v.startswith('http://')), '')
        method=a[a.index('-X')+1] if '-X' in a else 'GET'
        if url.endswith('/readiness'): print('OK')
        elif url.endswith('/admin'): print(json.dumps({'username':'nacos'}))
        elif url.endswith('/login'):
            counter=root/'login-count'; count=int(counter.read_text())+1 if counter.exists() else 1
            counter.write_text(str(count))
            cache_miss=fail=='cache-exhausted' or (fail=='cache-once' and count==1) or (fail=='existing-cache' and count==2)
            if cache_miss: print('caused: User nacos not found;\\n500')
            else:
                body={'status':500,'message':'private-error-detail'} if fail=='login-500' else {'accessToken':'private-fixture-token'}
                print(json.dumps(body)+'\\n'+('500' if fail in ('login-500','error-with-token') else '200'))
        elif url.endswith('/configs'):
            print('nacos-dev-business-ok' if method=='GET' else ('false' if fail=='publish' else 'true'))
        elif url.endswith('/instance/list'): print(json.dumps({'hosts':[{'ip':'10.70.1.4','port':8848}]}))
        elif url.endswith('/instance'): print('ok')
    elif 'logs' in a: print('private-log-detail')
    elif 'apply' in a:
        documents=list(yaml.safe_load_all(sys.stdin.read()))
        for doc in documents:
            if doc and doc.get('kind')=='Deployment':
                c=doc['spec']['template']['spec']['containers'][0]
                assert '--require-secure-transport=ON' in c['args']
    elif 'create' in a and 'secret' in a: print('apiVersion: v1\\nkind: Secret')
    elif 'create' in a and '-f' in a:
        doc=yaml.safe_load(sys.stdin.read()); assert doc['kind']=='Service'
        assert doc['metadata']=={'name':'nacos-mysql-wrong','namespace':'nacos'}
        assert doc['spec']['selector']=={'app':'nacos-mysql'}
'''
            for name in ('kubecli', 'kubectl', 'curl'):
                path = base/'bin'/name
                path.write_text(program)
                path.chmod(0o755)
            sleep = base/'bin/sleep'; sleep.write_text('#!/bin/sh\nexit 0\n'); sleep.chmod(0o755)
            result = subprocess.run(['bash', str(base/'gate.sh')], capture_output=True, text=True,
                timeout=45, env={**os.environ, 'PATH': str(base/'bin')+':'+str(ROOT/'.venv/bin')+':'+os.environ['PATH'],
                                'TEST_ROOT': str(base), 'TEST_FAILURE': failure,
                                'NACOS_IMPORT_GUARD':guard})
            trace = [json.loads(row) for row in (base/'trace').read_text().splitlines()]
            if failure == 'login-500':
                response = next(base.glob('kubeauto-nacos-login.*.response'))
                self.assertEqual(response.stat().st_mode & 0o777, 0o600)
                self.assertEqual(json.loads(response.read_text())['status'], 500)
                diagnostics = next(base.glob('kubeauto-nacos-failure.*'))
                self.assertEqual(diagnostics.stat().st_mode & 0o777, 0o700)
                self.assertTrue((diagnostics/'SHA256SUMS').is_file())
                self.assertTrue(all(p.stat().st_mode & 0o077 == 0 for p in diagnostics.iterdir()))
            return result, trace, (base/'clusters/dev-nacos-startup').exists()

    def test_http_failure_is_private_and_diagnosed_before_namespace_deletion(self):
        result, trace, residue = self.run_gate('login-500')
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(residue)
        self.assertNotIn('private-error-detail', result.stdout + result.stderr)
        self.assertNotIn('NACOS_STARTUP_FOCUSED_PASS', result.stdout)
        self.assertIn('NACOS_LOGIN_FAIL private_response=', result.stdout)
        self.assertIn('NACOS_FAILURE_DIAGNOSTICS directory=', result.stdout)
        deletion = next(i for i, row in enumerate(trace) if 'delete' in row and 'namespace' in row)
        for pod in ('nacos-0','nacos-1','nacos-2'):
            current = next(i for i,row in enumerate(trace) if 'logs' in row and pod in row and '--all-containers' in row)
            application = next(i for i,row in enumerate(trace) if 'exec' in row and pod in row and any('nacos-auth.log' in arg for arg in row))
            self.assertLess(current, deletion)
            self.assertLess(application, deletion)

    def test_actual_product_path_checks_capacity_repeats_and_cleans(self):
        result, trace, residue = self.run_gate()
        self.assertEqual(0, result.returncode, result.stdout+result.stderr)
        self.assertFalse(residue)
        setup = [row[-1] for row in trace if row[:2]==['kubecli','setup']]
        self.assertEqual(['01','90','01','07','07','07','07','07'], setup)
        self.assertEqual(1, sum('/nacos/v1/auth/users/admin' in ' '.join(row) for row in trace))
        self.assertIn('NACOS_RESTART_REPEAT_BUSINESS_PASS', result.stdout)
        self.assertIn('NACOS_TLS_REFUSALS_PASS', result.stdout)
        self.assertIn('NACOS_TLS_REFUSALS_SCOPED_CLEAN_PASS', result.stdout)
        self.assertIn('NACOS_STARTUP_SCOPED_CLEAN_PASS', result.stdout)
        self.assertEqual(['kubecli','destroy','dev-nacos-startup'], trace[-1])
        self.assertNotIn('private-fixture-token', result.stdout+result.stderr)
        self.assertNotIn('private-log-detail', result.stdout+result.stderr)
        # Configuration and persistent registration are seeded once, then read
        # unchanged after replacement and repeated product reconciliation.
        for endpoint in ('/nacos/v1/cs/configs','/nacos/v1/ns/instance'):
            writes = [row for row in trace if endpoint in ' '.join(row) and '-X' in row
                      and row[row.index('-X')+1] == 'POST']
            self.assertEqual(len(writes), 1)
        self.assertFalse(any('DELETE' in row for row in trace))

    def test_only_fresh_admin_cache_miss_may_wait_and_wait_is_bounded(self):
        for failure, expected_calls, success in (('cache-once',5,True), ('cache-exhausted',7,False),
                                                ('existing-cache',2,False), ('error-with-token',1,False)):
            with self.subTest(failure=failure):
                result, trace, residue = self.run_gate(failure)
                self.assertEqual(result.returncode == 0, success, result.stdout+result.stderr)
                self.assertFalse(residue)
                self.assertEqual(sum(any(arg.endswith('/nacos/v1/auth/users/login') for arg in row)
                                     for row in trace), expected_calls)
                self.assertNotIn('private-fixture-token', result.stdout+result.stderr)
                self.assertNotIn('private-log-detail', result.stdout+result.stderr)
                if failure in ('existing-cache','error-with-token'):
                    self.assertNotIn('Nacos initialized-user cache', result.stdout)

    def test_tls_refusal_requires_real_application_cause_and_scoped_cleanup(self):
        for failure in ('tls-unrelated','tls-unexpected-success','tls-foreign-namespace','tls-pv-residue'):
            with self.subTest(failure=failure):
                result, trace, residue = self.run_gate(failure)
                self.assertNotEqual(result.returncode,0,result.stdout+result.stderr)
                self.assertFalse(residue)
                self.assertNotIn('NACOS_STARTUP_FOCUSED_PASS',result.stdout)
                self.assertNotIn('NACOS_TLS_REFUSALS_SCOPED_CLEAN_PASS',result.stdout)
                if failure=='tls-foreign-namespace':
                    self.assertFalse(any('delete' in row and 'namespace' in row and 'nacos-tls-refusal' in row for row in trace))

    def test_failed_prerequisite_or_business_never_signs_and_destroys_only_owned_cluster(self):
        for failure in ('setup-90','setup-01','foreign-namespace','few-workers','publish'):
            with self.subTest(failure=failure):
                result, trace, residue = self.run_gate(failure)
                self.assertNotEqual(0, result.returncode, result.stdout+result.stderr)
                self.assertNotIn('NACOS_STARTUP_FOCUSED_PASS', result.stdout)
                self.assertFalse(residue)
                if failure in ('setup-90','setup-01','foreign-namespace','few-workers'):
                    self.assertFalse(any(row[:2]==['kubectl','apply'] for row in trace))
                if failure == 'setup-01':
                    self.assertFalse(any(row[:2] == ['kubecli','setup'] and row[-1] == '90' for row in trace))

    def test_source_reuse_has_no_main_or_logging_side_effect(self):
        result = subprocess.run(['bash','-c','source "$1"; printf "SOURCE_ONLY\\n"', 'contract', str(LIBRARY)],
                                capture_output=True, text=True, timeout=5)
        self.assertEqual((0,'SOURCE_ONLY\n',''), (result.returncode,result.stdout,result.stderr))

    def test_lease_checks_active_jobs_before_sync_and_holds_until_verification(self):
        runner = (ROOT/'tests/run_enterprise_regression.sh').read_text()
        branch = runner.split('if [[ "$MODE" == "--nacos-startup-only" ]]; then',1)[1].split('\nfi\n',1)[0]
        self.assertLess(branch.index('flock -n 9'),branch.index('sync-kubeauto.sh'))
        self.assertLess(branch.index('kill -0'),branch.index('sync-kubeauto.sh'))
        self.assertLess(branch.index('monitor_remote_job'),branch.rindex('release_startup_lease\n'))
        self.assertIn('trap release_startup_lease EXIT', branch)

    def test_scoped_cleanup_and_verification_never_reach_occupied_pools(self):
        with tempfile.TemporaryDirectory() as directory:
            base=Path(directory)
            ssh=base/'ssh'
            ssh.write_text('#!/bin/bash\nfor arg in "$@"; do case "$arg" in *@192.168.*) printf "%s\\n" "$arg" >> "$TRACE";; esac; done\n')
            ssh.chmod(0o755)
            expected={'root@192.168.47.137','brinnatt@192.168.47.128',
                      'root@192.168.47.142','root@192.168.47.143'}
            for options in (['--startup-only'],['--verify','--startup-only']):
                trace=base/'trace';trace.unlink(missing_ok=True)
                result=subprocess.run(['bash',str(ROOT/'tests/helpers/lab-wipe-nodes.sh'),*options],
                    capture_output=True,text=True,timeout=5,
                    env={**os.environ,'PATH':str(base)+':'+os.environ['PATH'],'TRACE':str(trace)})
                self.assertEqual(0,result.returncode,result.stdout+result.stderr)
                self.assertEqual(expected,set(trace.read_text().splitlines()))
            for options in (['--startup-only','unexpected'],['--verify','--startup-only','unexpected']):
                trace.unlink(missing_ok=True)
                result=subprocess.run(['bash',str(ROOT/'tests/helpers/lab-wipe-nodes.sh'),*options],
                    capture_output=True,text=True,timeout=5,
                    env={**os.environ,'PATH':str(base)+':'+os.environ['PATH'],'TRACE':str(trace)})
                self.assertEqual(2,result.returncode,result.stdout+result.stderr)
                self.assertFalse(trace.exists())

    def test_baseline_clean_refuses_active_service_job_or_pod_before_mutation(self):
        source=(ROOT/'tests/helpers/lab-wipe-nodes.sh').read_text()
        assignment='startup_idle_cmd='+source.split('  startup_idle_cmd=',1)[1].split('  for ip in',1)[0]
        for failure in ('','service','job','pod'):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory:
                base=Path(directory)
                systemctl=base/'systemctl'
                systemctl.write_text('#!/bin/sh\nif [ "$FAILURE" = service ]; then echo active; else echo inactive; fi\n')
                systemctl.chmod(0o755)
                ps=base/'ps'
                ps.write_text('#!/bin/sh\nif [ "$FAILURE" = pod ]; then echo "0::/kubepods.slice/pod-owned"; fi\n')
                ps.chmod(0o755)
                if failure=='job': (base/'active.pid').write_text(str(os.getpid()))
                script=assignment.replace('/tmp/kubeauto-*.pid',str(base/'*.pid'))+'\nbash -c "$startup_idle_cmd"\n'
                result=subprocess.run(['bash','-c',script],capture_output=True,text=True,timeout=5,
                    env={**os.environ,'PATH':str(base)+':'+os.environ['PATH'],'FAILURE':failure})
                self.assertEqual(0 if not failure else 1,result.returncode,result.stdout+result.stderr)


if __name__=='__main__':
    unittest.main()
