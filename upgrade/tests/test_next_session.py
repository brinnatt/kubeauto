"""Fixed lab refusal, ownership and shell transport contracts; no SSH or cluster."""
import ast
import inspect
import json
import os
from pathlib import Path
import subprocess
import shutil
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import yaml

from service.cluster.manager import ClusterManager
from upgrade.lab import next_session as lab
from upgrade.product import launcher
from upgrade.tests import test_launcher as launcher_fixture

ROOT = Path(__file__).resolve().parents[2]


class ProductCallbackContracts(unittest.TestCase):
    def test_all_upgrade_calls_bind_the_real_manager_signature(self):
        # A permissive Mock previously missed a third positional playbook
        # argument in the next-hop coordinator. Bind every direct call to the
        # actual public method signature, including its keyword-only boundary.
        signature = inspect.signature(ClusterManager._run_playbook)
        for source in (ROOT/'upgrade/product').glob('*.py'):
            for call in ast.walk(ast.parse(source.read_text())):
                if isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute) and call.func.attr=='_run_playbook':
                    with self.subTest(file=source.name, line=call.lineno):
                        self.assertFalse(any(isinstance(arg, ast.Starred) for arg in call.args))
                        signature.bind(object(), *[object() for _ in call.args],
                            **{kw.arg: object() for kw in call.keywords if kw.arg is not None})


class NextAdmissionContracts(unittest.TestCase):
    def setUp(self):
        fixture = launcher_fixture.LauncherContracts(methodName='runTest')
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.fixture = fixture
        override = patch.object(lab, 'MATRIX', fixture.matrix)
        override.start(); self.addCleanup(override.stop)

    def test_fixed_plan_refuses_pending_before_any_session_ownership(self):
        with self.assertRaisesRegex(launcher.UpgradeAdmissionRefused, 'UPG-03'):
            lab.plan('qualify','v13510-fixture')
        with self.assertRaisesRegex(ValueError,'identity'):
            lab.plan('qualify','foreign')

    def accept_static(self):
        f=self.fixture
        for case in f.data['cases']:
            if case['id'] not in ['UPG-03','UPG-04']: continue
            case.update(status='pass',subcases=['isolated-boundary'],
                evidence='upgrade/v1.35.10/'+case['id']+'.json')
            receipt=json.loads((f.root/f.data['cases'][0]['evidence']).read_text())
            receipt['case_ids']=[case['id']]
            (f.root/case['evidence']).write_text(json.dumps(receipt))
        f.data['meta']['runner']='upgrade/lab/next-session.sh'
        f.save()

    def test_qualified_plan_uses_shared_control_and_final_refuses_unaccepted_live_items(self):
        self.accept_static()
        result=lab.plan('qualify','v13510-fixture')
        self.assertEqual(result['control'],'192.168.122.2')
        self.assertEqual(result['builds'],[])
        self.assertEqual(result['masters']+result['workers'],lab.MASTERS+lab.WORKERS)
        with self.assertRaisesRegex(launcher.UpgradeAdmissionRefused,'UPG-05'):
            lab.plan('final','v13510-fixture')
        self.fixture.data['meta']['execution_enabled']=False; self.fixture.save()
        with self.assertRaisesRegex(launcher.UpgradeAdmissionRefused,'enabled'):
            lab.plan('qualify','v13510-fixture')


class NextCleanupContracts(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name)
        self.session=object.__new__(lab.Session)
        self.session.source=self.root/'source';self.session.target=self.root/'target'
        self.session.source_cluster=lab.SOURCE_CLUSTER;self.session.target_cluster=lab.TARGET_CLUSTER
        self.session.evidence=self.root
        self.session.host_operation=Mock()
        self.session.cli=Mock()
        self.session.command=Mock()

    def test_replaced_namespace_refuses_before_any_delete_or_destroy(self):
        lab.write(self.root/'ledger-namespace.json',{'metadata':{'uid':'original'}})
        self.session.kubectl=Mock(return_value=json.dumps({'items':[
            {'metadata':{'name':'upgrade-ledger','uid':'foreign'}}]}))
        with self.assertRaisesRegex(ValueError,'replaced'):
            self.session.clean_phase()
        self.assertEqual(self.session.kubectl.call_count,1)
        self.session.cli.assert_not_called()
        self.session.host_operation.assert_called_once_with('owned')

    def test_namespace_delete_binds_uid_then_requires_empty_volumes_before_destroy(self):
        lab.write(self.root/'ledger-namespace.json',{'metadata':{'uid':'original'}})
        self.session.kubectl=Mock(side_effect=[json.dumps({'items':[
            {'metadata':{'name':'upgrade-ledger','uid':'original'}}]}),'','',json.dumps({'items':[]})])
        self.session.clean_phase()
        delete=self.session.kubectl.call_args_list[1]
        self.assertEqual(delete.args,('delete','--raw=/api/v1/namespaces/upgrade-ledger','-f','-'))
        self.assertEqual(json.loads(delete.kwargs['data'])['preconditions'],{'uid':'original'})
        self.assertEqual([c.args[0] for c in self.session.host_operation.call_args_list],
                         ['owned','storage-clean','clean'])
        self.session.cli.assert_called_once_with(self.session.source,self.session.target,'destroy',lab.SOURCE_CLUSTER)
        self.session.command.assert_called_once_with(
            ['bash',lab.ROOT/'tests/helpers/lab-wipe-nodes.sh','--rocky-only',*lab.MASTERS,*lab.WORKERS],
            capture=False)

    def test_runtime_cleanup_failure_blocks_clean_verification(self):
        self.session.kubectl=Mock(side_effect=[json.dumps({'items':[]}),json.dumps({'items':[]})])
        self.session.command.side_effect=RuntimeError('runtime cgroup cleanup failed')
        with self.assertRaisesRegex(RuntimeError,'runtime cgroup cleanup failed'):
            self.session.clean_phase()
        self.assertEqual([c.args[0] for c in self.session.host_operation.call_args_list],
                         ['owned','storage-clean'])
        self.session.cli.assert_called_once_with(self.session.source,self.session.target,'destroy',lab.SOURCE_CLUSTER)

    def test_destroy_failure_never_invokes_lab_wipe(self):
        self.session.kubectl=Mock(side_effect=[json.dumps({'items':[]}),json.dumps({'items':[]})])
        self.session.cli.side_effect=RuntimeError('product destroy failed')
        with self.assertRaisesRegex(RuntimeError,'product destroy failed'):
            self.session.clean_phase()
        self.session.command.assert_not_called()

    def test_remaining_volume_keeps_product_and_storage_for_scoped_recovery(self):
        self.session.kubectl=Mock(side_effect=[json.dumps({'items':[]}),json.dumps({'items':[{'metadata':{'name':'keep'}}]})])
        with self.assertRaisesRegex(ValueError,'volumes remain'):
            self.session.clean_phase()
        self.session.cli.assert_not_called()
        self.session.host_operation.assert_called_once_with('owned')


class NextTransportContracts(unittest.TestCase):
    def test_real_pack_repair_preserves_bytes_and_status_bounds_error_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            state=Path(temporary);fake=state/'bin';fake.mkdir()
            run='v13510-fixture';evidence=state/'runs'/run;evidence.mkdir(parents=True)
            for product in ['source-product','target-product']:(state/product/'extra-bin').mkdir(parents=True)
            source=state/'source-product/extra-bin/shared';source.write_text('qualified bytes')
            target=state/'target-product/extra-bin/shared';os.link(source,target)
            lab.write(state/'input-manifest.json',{'trees':{'target-product':{'extra-bin/shared':lab.sha(target)}}})
            lab.write(state/'owner.json',{'run_id':'foreign','clusters':['fixture-source']})
            lab.write(evidence/'source.status.json',{'rc':1,'reason':str(list('are the same file '+'x'*10000))})
            for name in ['source-health.json','ledger-namespace.json']:lab.write(evidence/name,{})
            (state/(run+'.log')).write_text('NEXT_SESSION_MODULE_STOP '+'x'*10000+'\n')
            ssh=fake/'ssh';ssh.write_text('#!'+sys.executable+'\n'+'''import os,subprocess,sys
script=sys.stdin.read().replace('/var/tmp/kubeauto-v13510-lab',os.environ['FIXTURE_STATE'])
script=script.replace('/usr/local/kubeauto/.venv/bin/python',sys.executable)
script=script.replace("Path('/var/tmp/kubeauto-upgrade-'+plan['clusters'][0])/'journal'",'Path('+repr(os.environ['FIXTURE_STATE']+'/fixture-journal')+')')
raise SystemExit(subprocess.run(['bash','-s','--',sys.argv[-1]],input=script,text=True,env=os.environ).returncode)
''');ssh.chmod(0o700)
            def execute(operation):
                return subprocess.run(['bash',str(ROOT/'upgrade/lab/next-control.sh'),operation,run],
                    env={**os.environ,'PATH':str(fake)+':'+os.environ['PATH'],'FIXTURE_STATE':str(state)},
                    capture_output=True,text=True,timeout=20)
            result=execute('repair-source-pack');self.assertNotEqual(result.returncode,0)
            self.assertTrue(os.path.samefile(source,target))
            lab.write(state/'owner.json',{'run_id':run,'clusters':['fixture-source']})
            result=execute('repair-source-pack');self.assertEqual(result.returncode,0,result.stdout+result.stderr)
            self.assertFalse(os.path.samefile(source,target));self.assertEqual(source.read_bytes(),target.read_bytes())
            self.assertEqual(len(json.loads((evidence/'source-pack-inode-repair.json').read_text())['files']),1)
            result=execute('status');self.assertEqual(result.returncode,0,result.stderr)
            self.assertTrue(all(len(line)<800 for line in result.stdout.splitlines()))

    def test_admission_repair_refuses_after_a_mutation_journal_exists(self):
        self.assert_precontrol_repair_scope('repair-admission',
            ['upgrade/product/next_api.py','upgrade/lab/next-control.sh','upgrade/lab/next_session.py',
             'upgrade/tests/test_next_api.py','upgrade/tests/test_next_session.py'])

    def test_calico_default_repair_refuses_after_mutation_and_keeps_exact_scope(self):
        self.assert_precontrol_repair_scope('repair-calico-defaults',
            ['upgrade/product/calico_stage.py','upgrade/tests/test_calico_stage.py',
             'upgrade/tests/fixtures/calico_kubectl.py','upgrade/lab/next-control.sh',
             'upgrade/tests/test_next_session.py'])

    def assert_precontrol_repair_scope(self, operation, expected):
        with tempfile.TemporaryDirectory() as temporary:
            state=Path(temporary);fake=state/'bin';fake.mkdir()
            run='v13510-fixture';evidence=state/'runs'/run;evidence.mkdir(parents=True)
            (state/'target-product').mkdir()
            lab.write(state/'owner.json',{'run_id':run,'clusters':['fixture-source']})
            lab.write(evidence/'source.status.json',{'rc':0})
            (state/(run+'.log')).write_text('NEXT_SESSION_WAIT phase=source failed=false lease=held')
            journal=state/'fixture-journal';journal.mkdir()
            ssh=fake/'ssh'
            ssh.write_text('#!'+sys.executable+'\n'+'''import os,subprocess,sys
script=sys.stdin.read()
script=script.replace('/var/tmp/kubeauto-v13510-lab',os.environ['FIXTURE_STATE'])
script=script.replace('/usr/local/kubeauto/.venv/bin/python',sys.executable)
script=script.replace('from upgrade.lab.next_session import STATE','STATE=Path('+repr(os.environ['FIXTURE_STATE'])+')')
script=script.replace("Path('/var/tmp/kubeauto-upgrade-'+plan['clusters'][0])/'journal'",'Path('+repr(os.environ['FIXTURE_STATE']+'/fixture-journal')+')')
raise SystemExit(subprocess.run(['bash','-s','--',sys.argv[-1]],input=script,text=True,env=os.environ).returncode)
''');ssh.chmod(0o700)
            rsync=fake/'rsync';rsync.write_text('#!'+sys.executable+'\nimport json,os,sys\nfrom pathlib import Path\nPath(os.environ["FIXTURE_STATE"],"copied").write_text(json.dumps(sys.argv[1:]))\n');rsync.chmod(0o700)
            def execute():
                return subprocess.run(['bash',str(ROOT/'upgrade/lab/next-control.sh'),operation,run],
                    env={**os.environ,'PATH':str(fake)+':'+os.environ['PATH'],'FIXTURE_STATE':str(state)},
                    capture_output=True,text=True,timeout=20)
            result=execute();self.assertNotEqual(result.returncode,0)
            self.assertFalse((state/'copied').exists())
            journal.rmdir()
            for path, value in [(evidence/'control.status.json',{}),
                                (evidence/'control.attempt-1.log',{}),
                                (evidence/'source.status.json',{'rc':1})]:
                lab.write(path,value)
                result=execute();self.assertNotEqual(result.returncode,0)
                self.assertFalse((state/'copied').exists())
                if path.name=='source.status.json':lab.write(path,{'rc':0})
                else:path.unlink()
            result=execute()
            self.assertEqual(result.returncode,0,result.stdout+result.stderr)
            self.assertTrue((state/'copied').exists())
            transferred=json.loads((state/'copied').read_text())
            self.assertEqual([p for p in transferred if p.startswith('upgrade/')],expected)

    def test_staging_rsync_keeps_root_ownership_and_pool_lock(self):
        source=(ROOT/'upgrade/lab/next-control.sh').read_text()
        command=next(line for line in source.splitlines() if line.strip().startswith('rsync -aHz'))
        self.assertIn('--no-owner --no-group',command)
        self.assertIn('flock -n /var/lock/kubeauto-v13510-lab.lock',source)
        self.assertIn('test "${actual%% *}" = "$1"',source)
        self.assertIn('test ! -e "$state/owner.json" && test ! -L "$state/owner.json"',source)

    def test_frontend_invalid_scope_refuses_without_transport(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary); ssh=root/'ssh'
            ssh.write_text('#!/bin/sh\nprintf invoked > "'+str(root/'called')+'"\nexit 99\n');ssh.chmod(0o700)
            for args in [['launch','full','v13510-fixture'], ['launch','qualify','foreign'],
                         ['signal','v13510-fixture','foreign','cleanup'],
                         ['signal','v13510-fixture','source','delete-all']]:
                result=subprocess.run(['bash',str(ROOT/'upgrade/lab/next-control.sh'),*args],
                    capture_output=True,text=True,env={**os.environ,'PATH':str(root)+':'+os.environ['PATH']})
                self.assertNotEqual(result.returncode,0)
                self.assertFalse((root/'called').exists())

    def test_actual_inventory_ssh_payload_survives_shell_quoting(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)
            ssh=root/'ssh'
            # Only transport and the fixed remote Python location are adapted.
            # The exact bash payload (including nested Python quoting) executes
            # locally, with host state commands replaced by read-only fixtures.
            ssh.write_text('#!'+sys.executable+'\n'+'''import os,subprocess,sys
args=sys.argv[1:]
if args[-2:]==['bash','-s']: script=sys.stdin.read()
else: script=args[-1]
script=script.replace('/usr/local/kubeauto/.venv/bin/python',sys.executable)
raise SystemExit(subprocess.run(['bash','-euc',script],env=os.environ).returncode)
''')
            ssh.chmod(0o700)
            for name,body in {'unshare':'exit 0','systemctl':'echo inactive','uname':'echo 5.14.0-fixture'}.items():
                file=root/name;file.write_text('#!/bin/sh\n'+body+'\n');file.chmod(0o700)
            result=subprocess.run(['bash',str(ROOT/'upgrade/lab/next-inventory.sh')],
                env={**os.environ,'PATH':str(root)+':'+os.environ['PATH']},capture_output=True,text=True,timeout=30)
            self.assertEqual(result.returncode,0,result.stdout+result.stderr)
            self.assertIn('NEXT_HOP_READONLY_INVENTORY_PASS mutation=none lease=none',result.stdout)
            self.assertEqual(result.stdout.count('HOST=192.168.122.'),6)


class NextDurabilityContracts(unittest.TestCase):
    def test_real_hardlinked_packs_merge_and_separate_runs_keep_exact_source_clients(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);source=root/'source';target=root/'target'
            for product in [source,target]:
                (product/'kube-bin').mkdir(parents=True);(product/'extra-bin').mkdir()
            (source/'kube-bin/kubectl').write_text('1.34.13')
            (target/'kube-bin/kubectl').write_text('1.35.10')
            (source/'extra-bin/shared').write_text('unchanged qualified bytes')
            os.link(source/'extra-bin/shared',target/'extra-bin/shared')
            (source/'extra-bin/runtime').write_text('2.1.4')
            (target/'extra-bin/runtime').write_text('2.3.7')
            with self.assertRaises(shutil.Error):
                shutil.copytree(target/'extra-bin',source/'extra-bin',dirs_exist_ok=True)
            for run in ['v13510-qualify-fixture','v13510-final-fixture']:
                (source/'kube-bin/kubectl').write_text('1.34.13')
                lab.stage_upgrade_clients(source,target,run)
                self.assertEqual((source/('kube-bin-source-'+run)/'kubectl').read_text(),'1.34.13')
                self.assertEqual((source/'kube-bin/kubectl').read_text(),'1.35.10')
                lab.stage_upgrade_clients(source,target,run)
                self.assertEqual((source/'extra-bin/runtime').read_text(),'2.3.7')
                self.assertEqual((target/'extra-bin/shared').read_text(),'unchanged qualified bytes')

    def test_missing_or_extra_registry_supply_refuses_before_ownership(self):
        from upgrade.lab.next_registry import LAB, scope
        with tempfile.TemporaryDirectory() as temporary:
            path=Path(temporary)/'supply.json'
            records=[{'image':name,'source_child':item['platforms']['linux/amd64'],
                      'action':'copied-missing-amd64-manifest'} for name,item in scope().items()]
            data={'ready':True,'registry':LAB,'builds':[],'existing_tags_overwritten':[],'records':records}
            lab.write(path,data);lab.verify_registry_supply(path)
            data['records']=records[:-1];lab.write(path,data)
            with self.assertRaisesRegex(ValueError,'supply'):lab.verify_registry_supply(path)
            data['records']=records;data['ready']=False;lab.write(path,data)
            with self.assertRaisesRegex(ValueError,'supply'):lab.verify_registry_supply(path)

    def test_runs_have_independent_preparation_and_cluster_identities(self):
        first=lab.cluster_names('v13510-qualify-a')
        final=lab.cluster_names('v13510-final-a')
        self.assertFalse(set(first)&set(final))
        self.assertTrue(all(len(name)<=64 for name in first+final))

    def test_malformed_foreign_and_invalid_actions_do_not_escape_owned_wait(self):
        with tempfile.TemporaryDirectory() as temporary:
            session=object.__new__(lab.Session)
            session.evidence=Path(temporary);session.run_id='v13510-fixture'
            action=session.evidence/'action.json'
            messages=iter(['{', '[]', json.dumps({'run_id':'foreign','phase':'source','action':'cleanup'}),
                json.dumps({'run_id':session.run_id,'phase':'source','action':'retry'}),
                json.dumps({'run_id':session.run_id,'phase':'source','action':'advance'})])
            def supply(_):
                action.write_text(next(messages))
            with patch.object(lab.time,'sleep',side_effect=supply):
                self.assertEqual(session.wait('source'),'advance')
            self.assertFalse(action.exists())

    def test_real_file_descriptors_exclude_other_lock_owners(self):
        with tempfile.TemporaryDirectory() as temporary:
            code='''import os,subprocess,sys
from pathlib import Path
from upgrade.lab import next_session as lab
lab.LOCK_DIRECTORY=Path(sys.argv[1])
for fd,name in lab.LOCKS.items():
 stream=os.open(lab.LOCK_DIRECTORY/name,os.O_RDWR|os.O_CREAT,0o600)
 os.dup2(stream,fd)
 if stream!=fd:os.close(stream)
lab.verify_leases()
for fd,name in lab.LOCKS.items():
 assert subprocess.run(['flock','-n',str(lab.LOCK_DIRECTORY/name),'true']).returncode!=0
foreign=os.open(lab.LOCK_DIRECTORY/'foreign',os.O_RDWR|os.O_CREAT,0o600)
os.dup2(foreign,6)
try:lab.verify_leases()
except ValueError:print('LEASE_IDENTITY_AND_EXCLUSION_PASS')
else:raise AssertionError('foreign descriptor admitted')
'''
            result=subprocess.run([sys.executable,'-B','-c',code,temporary],capture_output=True,text=True)
            self.assertEqual(result.returncode,0,result.stdout+result.stderr)
            self.assertIn('LEASE_IDENTITY_AND_EXCLUSION_PASS',result.stdout)

    def test_child_log_streams_real_subprocess_output_and_preserves_first_attempt(self):
        with tempfile.TemporaryDirectory() as temporary:
            log=Path(temporary)/'phase.log'
            code='''import subprocess,sys
from pathlib import Path
from upgrade.lab.next_session import phase_log
with phase_log(Path(sys.argv[1])):
 print('child product entry',flush=True)
 subprocess.run([sys.executable,'-c','print("real child output")'],check=True)
print('parent outside child boundary',flush=True)
'''
            result=subprocess.run([sys.executable,'-B','-c',code,str(log)],capture_output=True,text=True)
            self.assertEqual(result.returncode,0,result.stdout+result.stderr)
            self.assertEqual(log.read_text(),'child product entry\nreal child output\n')
            self.assertIn('parent outside child boundary',result.stdout)
            before=log.read_bytes()
            result=subprocess.run([sys.executable,'-B','-c',code,str(log)],capture_output=True,text=True)
            self.assertNotEqual(result.returncode,0)
            self.assertEqual(log.read_bytes(),before)

    def test_staged_input_drift_and_source_commit_mismatch_refuse(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);trees={}
            for name in ['source-product','target-product']:
                (root/name).mkdir();(root/name/'input').write_text('qualified')
                trees[name]={'input':lab.sha(root/name/'input')}
            data={'source_commit':lab.SOURCE_COMMIT,'trees':trees}
            lab.write(root/'input-manifest.json',data)
            lab.verify_inputs(root,root/'target-product')
            (root/'source-product/input').write_text('drifted')
            with self.assertRaisesRegex(ValueError,'input differs'):
                lab.verify_inputs(root,root/'target-product')
            data['source_commit']='foreign';lab.write(root/'input-manifest.json',data)
            with self.assertRaisesRegex(ValueError,'source commit'):
                lab.verify_inputs(root,root/'target-product')


class NextHostOwnershipContracts(unittest.TestCase):
    def test_actual_clean_probe_detects_shim_with_truncated_linux_comm(self):
        plays=yaml.safe_load((ROOT/'upgrade/lab/next-hosts.yml').read_text())
        task=next(task for play in plays for task in play['tasks']
                  if task.get('name')=='Refuse foreign services or source residue at initial admission and after cleanup')
        probe=next(line for line in task['ansible.builtin.shell'].splitlines()
                   if '! ps ' in line and 'containerd-shim' in line)
        with tempfile.TemporaryDirectory() as temporary:
            binary=Path(temporary)/'containerd-shim-runc-v2';shutil.copyfile('/bin/sleep',binary);binary.chmod(0o700)
            process=subprocess.Popen([str(binary),'30'])
            try:
                comm=Path('/proc')/str(process.pid)/'comm'
                self.assertNotEqual(comm.read_text().strip(),'containerd-shim-runc-v2')
                self.assertNotEqual(subprocess.run(['bash','-c',probe],capture_output=True).returncode,0)
            finally:
                process.terminate();process.wait(timeout=5)

    def test_real_host_book_refuses_foreign_owner_and_nonempty_storage(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary); fake=root/'bin';fake.mkdir()
            for name,body in {'hostname': 'printf "%s " '+ ' '.join(lab.MASTERS+lab.WORKERS),
                              'uname': 'echo 5.14.0-fixture',
                              'systemctl': 'echo inactive', 'ps':'exit 0'}.items():
                path=fake/name;path.write_text('#!/bin/sh\n'+body+'\n');path.chmod(0o700)
            hosts=root/'hosts';hosts.write_text(lab.inventory(root/'source',lab.SOURCE_CLUSTER)+
                '\nansible_connection=local\nansible_python_interpreter='+sys.executable+'\n')
            # Only absolute node filesystem locations and uid are mapped to
            # per-node local fixtures. Real Ansible assert/shell/copy/file and
            # play barriers execute; no shell command or mutation is stubbed.
            plays=yaml.safe_load((ROOT/'upgrade/lab/next-hosts.yml').read_text())
            paths=['/var/tmp/kubeauto-v13510-node-owner','/etc/kubernetes','/var/lib/etcd',
                   '/var/lib/kubelet','/var/lib/kube-proxy','/var/lib/containerd','/etc/cni',
                   '/etc/containerd','/etc/kube-lb','/run/calico','/opt/local-path-provisioner']
            def adapt(value):
                if isinstance(value,str):
                    for path in paths:
                        value=value.replace(path,'{{ fixture_root }}/{{ inventory_hostname }}'+path)
                    return value.replace('= 0:600','= '+str(os.geteuid())+':600')
                if isinstance(value,list):return [adapt(v) for v in value]
                if isinstance(value,dict):return {k:adapt(v) for k,v in value.items()}
                return value
            book=root/'book.yml';book.write_text(yaml.safe_dump(adapt(plays)))
            for host in lab.MASTERS+lab.WORKERS:
                (root/host/'var/tmp').mkdir(parents=True)
            def marker(host):return root/host/'var/tmp/kubeauto-v13510-node-owner'
            def execute(operation, ok=True, ca=None):
                variables={'NEXT_RUN_ID':'v13510-fixture','NEXT_HOST_OPERATION':operation,'fixture_root':str(root)}
                if ca:variables['NEXT_CA_SHA256']=ca
                result=subprocess.run([str(Path(sys.executable).parent/'ansible-playbook'),'-i',str(hosts),str(book),
                    '-e',json.dumps(variables)],capture_output=True,text=True,timeout=60,
                    env={**os.environ,'PATH':str(fake)+':'+os.environ['PATH'],'ANSIBLE_LOCAL_TEMP':str(root/'local')})
                self.assertEqual(result.returncode==0,ok,result.stdout+result.stderr)
            execute('admit')
            foreign=marker(lab.WORKERS[-1]);foreign.write_text('v13510-foreign\n');foreign.chmod(0o600)
            execute('claim',False)
            self.assertFalse(any(marker(h).exists() for h in (lab.MASTERS+lab.WORKERS)[:-1]))
            foreign.unlink();execute('claim')
            for host in lab.MASTERS+lab.WORKERS:
                self.assertEqual(marker(host).read_text(),'v13510-fixture\n')
                self.assertEqual(marker(host).stat().st_mode & 0o777,0o600)
            storage=root/lab.WORKERS[0]/'opt/local-path-provisioner'
            storage.mkdir(parents=True);(storage/'retain').write_text('foreign or uncollected data')
            execute('storage-clean',False,ca='a'*64)
            self.assertTrue((storage/'retain').exists())
            (storage/'retain').unlink();execute('storage-clean',ca='a'*64)
            self.assertFalse(storage.exists())
            execute('release')
            self.assertFalse(any(marker(h).exists() for h in lab.MASTERS+lab.WORKERS))


if __name__=='__main__':
    unittest.main()
