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
            lab.plan('qualify','v1366-fixture')
        with self.assertRaisesRegex(ValueError,'identity'):
            lab.plan('qualify','foreign')

    def accept_static(self):
        f=self.fixture
        for case in f.data['cases']:
            if case['id'] not in ['UPG-03','UPG-04']: continue
            case.update(status='pass',subcases=['isolated-boundary'],
                evidence=str(f.matrix.parent.relative_to(f.root)/(case['id']+'.json')))
            receipt=json.loads((f.root/f.data['cases'][0]['evidence']).read_text())
            receipt['case_ids']=[case['id']]
            (f.root/case['evidence']).write_text(json.dumps(receipt))
        f.data['meta']['runner']='upgrade/lab/next-session.sh'
        f.save()

    def test_qualified_plan_uses_shared_control_and_final_refuses_unaccepted_live_items(self):
        self.accept_static()
        result=lab.plan('qualify','v1366-fixture')
        self.assertEqual(result['control'],'192.168.122.2')
        self.assertEqual(result['builds'],[])
        self.assertEqual(result['masters']+result['workers'],lab.MASTERS+lab.WORKERS)
        with self.assertRaisesRegex(launcher.UpgradeAdmissionRefused,'UPG-05'):
            lab.plan('final','v1366-fixture')
        self.fixture.data['meta']['execution_enabled']=False; self.fixture.save()
        with self.assertRaisesRegex(launcher.UpgradeAdmissionRefused,'enabled'):
            lab.plan('qualify','v1366-fixture')

    def test_active_hop_matches_accepted_maintenance_source_and_shell_transport(self):
        baseline=yaml.safe_load((ROOT/'upgrade/v1.36.6/source-baseline.yaml').read_text())
        self.assertEqual(lab.SOURCE_COMMIT,baseline['source_commit'])
        self.assertEqual(lab.SOURCE,baseline['kubernetes'])
        self.assertEqual(lab.TARGET,'v1.36.6')
        self.assertEqual(lab.STATE.name,'kubeauto-v1366-lab')
        self.assertEqual(lab.cluster_names('v1366-fixture')[0].split('-source')[0],
                         'upgrade-v13510')
        for line in (ROOT/'upgrade/lab/next-control.sh').read_text().splitlines():
            if 'ssh -o BatchMode=yes' in line or line.startswith('SSH='):
                self.assertIn('-o ProxyJump=root@192.168.47.130',line)


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

    def owned_volume(self):
        lab.write(self.root/'ledger-namespace.json',{'metadata':{'uid':'namespace-original'}})
        lab.write(self.root/'prepared-boundary.json',{'continuity':{
            'namespace':'upgrade-ledger','namespace_uid':'namespace-original',
            'pvc':'ledger','pvc_uid':'claim-original','pv':'original-volume','pv_uid':'volume-original'}})
        return {'metadata':{'name':'original-volume','uid':'volume-original'},
            'spec':{'persistentVolumeReclaimPolicy':'Delete','claimRef':{
                'namespace':'upgrade-ledger','name':'ledger','uid':'claim-original'}}}

    def test_namespace_can_disappear_before_owned_provisioner_teardown_finishes(self):
        volume=self.owned_volume()
        self.session.kubectl=Mock(side_effect=[json.dumps({'items':[
            {'metadata':{'name':'upgrade-ledger','uid':'namespace-original'}}]}),'','',
            json.dumps({'items':[volume]}),json.dumps({'items':[]})])
        def waiting(_):
            self.session.cli.assert_not_called()
            self.session.command.assert_not_called()
            self.session.host_operation.assert_called_once_with('owned')
        with patch.object(lab.time,'sleep',side_effect=waiting) as sleep:
            self.session.clean_phase()
        sleep.assert_called_once_with(2)
        self.session.cli.assert_called_once()
        self.assertEqual(self.session.kubectl.call_args_list[-1].args,('get','pv','-o','json'))

    def test_changed_pv_or_claim_identity_and_retain_policy_refuse_without_waiting(self):
        for field in ['pv_uid','claim_uid','namespace','policy','extra_volume']:
            with self.subTest(field=field):
                volume=self.owned_volume()
                if field=='pv_uid':volume['metadata']['uid']='replacement'
                if field=='claim_uid':volume['spec']['claimRef']['uid']='replacement'
                if field=='namespace':volume['spec']['claimRef']['namespace']='foreign'
                if field=='policy':volume['spec']['persistentVolumeReclaimPolicy']='Retain'
                volumes=[volume]+([{'metadata':{'name':'foreign'}}] if field=='extra_volume' else [])
                self.session.kubectl=Mock(return_value=json.dumps({'items':volumes}))
                with patch.object(lab.time,'sleep') as sleep:
                    with self.assertRaisesRegex(ValueError,'foreign identity or reclaim policy'):
                        self.session.wait_volume_reclaim()
                sleep.assert_not_called()
                self.session.cli.assert_not_called()

    def test_owned_volume_timeout_retains_cluster_and_never_removes_finalizers(self):
        volume=self.owned_volume();volume['metadata']['finalizers']=['kubernetes.io/pv-protection']
        self.session.kubectl=Mock(return_value=json.dumps({'items':[volume]}))
        with patch.object(lab.time,'monotonic',side_effect=[0,301]),patch.object(lab.time,'sleep') as sleep:
            with self.assertRaisesRegex(ValueError,'timed out'):
                self.session.wait_volume_reclaim()
        sleep.assert_not_called()
        self.session.kubectl.assert_called_once_with('get','pv','-o','json')
        self.session.cli.assert_not_called()

    def test_target_or_replaced_namespace_never_adopts_a_source_volume(self):
        for target in [False,True]:
            with self.subTest(target=target):
                volume=self.owned_volume()
                if not target:lab.write(self.root/'ledger-namespace.json',{'metadata':{'uid':'replacement'}})
                self.session.kubectl=Mock(return_value=json.dumps({'items':[volume]}))
                with patch.object(lab.time,'sleep') as sleep:
                    with self.assertRaisesRegex(ValueError,'volumes remain'):
                        self.session.wait_volume_reclaim(target=target)
                sleep.assert_not_called()


class NextTransportContracts(unittest.TestCase):
    def test_fetch_keeps_harbor_archive_manifests_without_copying_gigabyte_archives(self):
        with tempfile.TemporaryDirectory() as temporary:
            state=Path(temporary);fake=state/'bin';fake.mkdir()
            rsync=fake/'rsync'
            rsync.write_text('#!'+sys.executable+'\nimport json,os,sys\nfrom pathlib import Path\nPath(os.environ["FIXTURE_ARGUMENTS"]).write_text(json.dumps(sys.argv[1:]))\n')
            rsync.chmod(0o700)
            for run in ['v1366-harbor-fixture','v1366-fixture']:
                result=subprocess.run(['bash',str(ROOT/'upgrade/lab/next-control.sh'),'fetch',run],
                    cwd=state,env={**os.environ,'PATH':str(fake)+':'+os.environ['PATH'],
                    'FIXTURE_ARGUMENTS':str(state/'arguments.json')},capture_output=True,text=True,timeout=10)
                self.assertEqual(result.returncode,0,result.stderr)
                args=json.loads((state/'arguments.json').read_text())
                self.assertEqual('--exclude=*.node-state/***' in args,run.startswith('v1366-harbor-'))
                self.assertFalse(any('manifest' in a for a in args))

    def test_real_staging_permission_step_restores_optional_docker_binaries_only(self):
        script=(ROOT/'upgrade/lab/next-control.sh').read_text()
        verification=script.split("<<'VERIFY'\n",1)[1].split('\nVERIFY',1)[0]
        # Exercise the real shell permission step without SSH or lab mutation.
        permission_step=verification.split('cd "$state/target-product"',1)[0]
        for docker_present in [False,True]:
            with self.subTest(docker_present=docker_present),tempfile.TemporaryDirectory() as temporary:
                state=Path(temporary)
                for product in ['source-product','target-product']:
                    directories=['kube-bin','extra-bin']+(['docker-bin'] if docker_present else [])
                    for directory in directories:
                        root=state/product/directory;root.mkdir(parents=True)
                        (root/'binary').write_text('qualified executable')
                        root.chmod(0o700);(root/'binary').chmod(0o600)
                    private=state/product/'config.yml';private.write_text('private configuration');private.chmod(0o600)
                result=subprocess.run(['bash','-s'],text=True,capture_output=True,
                    input=permission_step.replace('/var/tmp/kubeauto-v1366-lab',str(state)),timeout=10)
                self.assertEqual(result.returncode,0,result.stdout+result.stderr)
                for product in ['source-product','target-product']:
                    for directory in directories:
                        self.assertEqual((state/product/directory/'binary').stat().st_mode&0o777,0o755)
                    self.assertEqual((state/product/'config.yml').stat().st_mode&0o777,0o600)

    def test_real_pack_repair_preserves_bytes_and_status_bounds_error_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            state=Path(temporary);fake=state/'bin';fake.mkdir()
            run='v1366-fixture';evidence=state/'runs'/run;evidence.mkdir(parents=True)
            for product in ['source-product','target-product']:(state/product/'extra-bin').mkdir(parents=True)
            source=state/'source-product/extra-bin/shared';source.write_text('qualified bytes')
            target=state/'target-product/extra-bin/shared';os.link(source,target)
            lab.write(state/'input-manifest.json',{'trees':{'target-product':{'extra-bin/shared':lab.sha(target)}}})
            lab.write(state/'owner.json',{'run_id':'foreign','clusters':['fixture-source']})
            lab.write(evidence/'source.status.json',{'rc':1,'reason':str(list('are the same file '+'x'*10000))})
            for name in ['source-health.json','ledger-namespace.json']:lab.write(evidence/name,{})
            (state/(run+'.log')).write_text('NEXT_SESSION_MODULE_STOP '+'x'*10000+'\n')
            ssh=fake/'ssh';ssh.write_text('#!'+sys.executable+'\n'+'''import os,subprocess,sys
script=sys.stdin.read().replace('/var/tmp/kubeauto-v1366-lab',os.environ['FIXTURE_STATE'])
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
            run='v1366-fixture';evidence=state/'runs'/run;evidence.mkdir(parents=True)
            (state/'target-product').mkdir()
            lab.write(state/'owner.json',{'run_id':run,'clusters':['fixture-source']})
            lab.write(evidence/'source.status.json',{'rc':0})
            (state/(run+'.log')).write_text('NEXT_SESSION_WAIT phase=source failed=false lease=held')
            journal=state/'fixture-journal';journal.mkdir()
            ssh=fake/'ssh'
            ssh.write_text('#!'+sys.executable+'\n'+'''import os,subprocess,sys
script=sys.stdin.read()
script=script.replace('/var/tmp/kubeauto-v1366-lab',os.environ['FIXTURE_STATE'])
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
        self.assertIn('flock -n /var/lock/kubeauto-v1366-lab.lock',source)
        self.assertIn('test "${actual%% *}" = "$1"',source)
        self.assertIn('test ! -e "$state/owner.json" && test ! -L "$state/owner.json"',source)

    def test_frontend_invalid_scope_refuses_without_transport(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary); ssh=root/'ssh'
            ssh.write_text('#!/bin/sh\nprintf invoked > "'+str(root/'called')+'"\nexit 99\n');ssh.chmod(0o700)
            for args in [['launch','full','v1366-fixture'], ['launch','qualify','foreign'],
                         ['signal','v1366-fixture','foreign','cleanup'],
                         ['signal','v1366-fixture','source','delete-all']]:
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
            for name,body in {'unshare':'exit 0','systemctl':'echo inactive','uname':'echo 5.14.0-fixture',
                              'df':'echo "fixture-root 65536 4096 61440 7% /"'}.items():
                file=root/name;file.write_text('#!/bin/sh\n'+body+'\n');file.chmod(0o700)
            result=subprocess.run(['bash',str(ROOT/'upgrade/lab/next-inventory.sh')],
                env={**os.environ,'PATH':str(root)+':'+os.environ['PATH']},capture_output=True,text=True,timeout=30)
            self.assertEqual(result.returncode,0,result.stdout+result.stderr)
            self.assertIn('NEXT_HOP_READONLY_INVENTORY_PASS mutation=none lease=none',result.stdout)
            self.assertEqual(result.stdout.count('HOST=192.168.122.'),6)


class NextDurabilityContracts(unittest.TestCase):
    def test_combined_final_refuses_incomplete_kubernetes_before_constructing_harbor(self):
        from upgrade.lab import harbor_session
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);s=object.__new__(lab.Session)
            s.run_id='v1366-final-fixture';s.evidence=root;s.owner=root/'owner.json'
            s.plan={'phases':['source'],'harbor':{'run_id':'v1366-harbor-final-fixture'}}
            lab.write(root/'source.status.json',{'rc':0,'attempt':1})
            with patch.object(harbor_session,'HarborSession') as child:
                with self.assertRaises(FileNotFoundError):s.finish_final()
                child.assert_not_called()
                for suffix in ['exit','finalized']:(root/('source.attempt-1.'+suffix)).write_text('0\n')
                lab.write(root/'source.failure-1.json',{'rc':1})
                with self.assertRaisesRegex(ValueError,'failed attempt'):s.finish_final()
                child.assert_not_called()
                (root/'source.failure-1.json').unlink();s.owner.write_text('owned')
                with self.assertRaisesRegex(ValueError,'ownership'):s.finish_final()
                child.assert_not_called()

    def test_combined_final_requires_both_real_scope_exit_records_and_child_release(self):
        from upgrade.lab import harbor_session
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);s=object.__new__(lab.Session)
            s.run_id='v1366-final-fixture';s.evidence=root/'parent';s.evidence.mkdir()
            s.owner=root/'owner.json';s.plan={'phases':['source'],'harbor':{'run_id':'v1366-harbor-final-fixture'}}
            child=Mock();child.run_id=s.plan['harbor']['run_id'];child.evidence=root/'child';child.evidence.mkdir()
            child.owner=s.owner;child.plan={'phases':['harbor-source','harbor-clean']}
            def complete(session):
                for phase in session.plan['phases']:
                    lab.write(session.evidence/(phase+'.status.json'),{'rc':0,'attempt':1})
                    for suffix in ['exit','finalized']:
                        (session.evidence/(phase+'.attempt-1.'+suffix)).write_text('0\n')
            complete(s)
            with patch.object(harbor_session,'HarborSession',return_value=child) as factory, \
                    patch.object(lab,'restore_source_clients') as restore:
                s.state=root
                child.run.side_effect=RuntimeError('first Harbor failure')
                with self.assertRaisesRegex(RuntimeError,'first Harbor failure'):s.finish_final()
                self.assertFalse((s.evidence/'combined-final.json').exists())
                child.run.side_effect=lambda:complete(child)
                s.finish_final()
                factory.assert_called_with(child.run_id,'final')
                result=json.loads((s.evidence/'combined-final.json').read_text())
                self.assertEqual(result['harbor_run_id'],child.run_id)
                self.assertEqual(result['marker'],'UPGRADE_NEXT_FINAL_PASS')
                restore.assert_called_with(root,s.run_id)

    def test_actual_control_isolator_admits_current_session_and_refuses_foreign_paths(self):
        # Execute the actual shell, replacing only host paths and privileged
        # commands. The previous one-version allowlist rejected the current
        # Session.cli state before unshare could isolate the control home.
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            script = (ROOT/'upgrade/lab/isolate-control.sh').read_text()
            states = ['/var/tmp/kubeauto-v13510-lab', str(lab.STATE)]
            for index, state in enumerate(states):
                local = root/('state-'+str(index)); local.mkdir(mode=0o700)
                script = script.replace(state, str(local))
            path = root/'isolate.sh'; path.write_text(script)
            for name, body in {'id': 'echo 0', 'stat': 'echo 0:700',
                               'unshare': 'printf "%s\\n" "$@" > "$FIXTURE_CALL"'}.items():
                helper = root/name
                helper.write_text('#!/bin/sh\n'+body+'\n'); helper.chmod(0o700)
            call = root/'call'
            env = {**os.environ, 'PATH': str(root)+':'+os.environ['PATH'], 'FIXTURE_CALL': str(call)}
            for index in range(2):
                result = subprocess.run(['bash', path, root/('state-'+str(index)), 'true'],
                                        env=env, capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn('--mount\n--propagation\nprivate', call.read_text())
                call.unlink()
            result = subprocess.run(['bash', path, root/'foreign', 'true'],
                                    env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 2)
            self.assertFalse(call.exists())

    def test_real_hardlinked_packs_merge_and_separate_runs_keep_exact_source_clients(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);source=root/'source';target=root/'target'
            for product in [source,target]:
                (product/'kube-bin').mkdir(parents=True);(product/'extra-bin').mkdir()
            (source/'kube-bin/kubectl').write_text('1.35.10')
            (target/'kube-bin/kubectl').write_text('1.36.6')
            (source/'extra-bin/shared').write_text('unchanged qualified bytes')
            os.link(source/'extra-bin/shared',target/'extra-bin/shared')
            (source/'extra-bin/runtime').write_text('2.1.4')
            (target/'extra-bin/runtime').write_text('2.3.7')
            with self.assertRaises(shutil.Error):
                shutil.copytree(target/'extra-bin',source/'extra-bin',dirs_exist_ok=True)
            for run in ['v1366-qualify-fixture','v1366-final-fixture']:
                (source/'kube-bin/kubectl').write_text('1.35.10')
                lab.stage_upgrade_clients(source,target,run)
                self.assertEqual((source/('kube-bin-source-'+run)/'kubectl').read_text(),'1.35.10')
                self.assertEqual((source/'kube-bin/kubectl').read_text(),'1.36.6')
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
        first=lab.cluster_names('v1366-qualify-a')
        final=lab.cluster_names('v1366-final-a')
        self.assertFalse(set(first)&set(final))
        self.assertTrue(all(len(name)<=64 for name in first+final))

    def test_malformed_foreign_and_invalid_actions_do_not_escape_owned_wait(self):
        with tempfile.TemporaryDirectory() as temporary:
            session=object.__new__(lab.Session)
            session.evidence=Path(temporary);session.run_id='v1366-fixture'
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
            paths=['/var/tmp/kubeauto-v1366-node-owner','/etc/kubernetes','/var/lib/etcd',
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
            def marker(host):return root/host/'var/tmp/kubeauto-v1366-node-owner'
            def execute(operation, ok=True, ca=None):
                variables={'NEXT_RUN_ID':'v1366-fixture','NEXT_HOST_OPERATION':operation,'fixture_root':str(root)}
                if ca:variables['NEXT_CA_SHA256']=ca
                result=subprocess.run([str(Path(sys.executable).parent/'ansible-playbook'),'-i',str(hosts),str(book),
                    '-e',json.dumps(variables)],capture_output=True,text=True,timeout=60,
                    env={**os.environ,'PATH':str(fake)+':'+os.environ['PATH'],'ANSIBLE_LOCAL_TEMP':str(root/'local')})
                self.assertEqual(result.returncode==0,ok,result.stdout+result.stderr)
            execute('admit')
            foreign=marker(lab.WORKERS[-1]);foreign.write_text('v1366-foreign\n');foreign.chmod(0o600)
            execute('claim',False)
            self.assertFalse(any(marker(h).exists() for h in (lab.MASTERS+lab.WORKERS)[:-1]))
            foreign.unlink();execute('claim')
            for host in lab.MASTERS+lab.WORKERS:
                self.assertEqual(marker(host).read_text(),'v1366-fixture\n')
                self.assertEqual(marker(host).stat().st_mode & 0o777,0o600)
            storage=root/lab.WORKERS[0]/'opt/local-path-provisioner'
            storage.mkdir(parents=True);(storage/'retain').write_text('foreign or uncollected data')
            execute('storage-clean',False,ca='a'*64)
            self.assertTrue((storage/'retain').exists())
            (storage/'retain').unlink();execute('storage-clean',ca='a'*64)
            self.assertFalse(storage.exists())
            execute('release')
            self.assertFalse(any(marker(h).exists() for h in lab.MASTERS+lab.WORKERS))


class ClientHandoffContracts(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name);self.run='v1366-final-original'
        trees={}
        for name,version in [('source-product','1.35.10'),('target-product','1.36.6')]:
            p=self.root/name;(p/'kube-bin').mkdir(parents=True);(p/'extra-bin').mkdir()
            (p/'kube-bin/kubectl').write_text(version)
            (p/'extra-bin/etcd').write_text('same accepted 3.6.16')
            trees[name]={str(f.relative_to(p)):lab.sha(f) for f in p.rglob('*') if f.is_file()}
        lab.write(self.root/'input-manifest.json',{'source_commit':lab.SOURCE_COMMIT,'trees':trees})

    def test_real_upgrade_client_transition_restores_before_actual_harbor_input_admission(self):
        source=self.root/'source-product';target=self.root/'target-product'
        lab.stage_upgrade_clients(source,target,self.run)
        with self.assertRaisesRegex(ValueError,'source-product/kube-bin/kubectl'):
            lab.verify_inputs(self.root,target)
        lab.restore_source_clients(self.root,self.run)
        lab.verify_inputs(self.root,target)
        self.assertEqual((source/'kube-bin/kubectl').read_text(),'1.35.10')
        self.assertEqual((source/('kube-bin-upgraded-'+self.run)/'kubectl').read_text(),'1.36.6')

    def test_changed_backup_or_upgraded_clients_refuse_before_rename(self):
        source=self.root/'source-product';target=self.root/'target-product'
        lab.stage_upgrade_clients(source,target,self.run)
        backup=source/('kube-bin-source-'+self.run)/'kubectl'
        backup.write_text('foreign')
        with self.assertRaisesRegex(ValueError,'original source clients'):
            lab.restore_source_clients(self.root,self.run)
        backup.write_text('1.35.10');(source/'kube-bin/kubectl').write_text('foreign')
        with self.assertRaisesRegex(ValueError,'upgraded source clients'):
            lab.restore_source_clients(self.root,self.run)
        self.assertTrue(backup.exists())
        self.assertFalse((source/('kube-bin-upgraded-'+self.run)).exists())

    def test_symbolic_link_backup_is_never_admitted(self):
        source=self.root/'source-product';target=self.root/'target-product'
        lab.stage_upgrade_clients(source,target,self.run)
        backup=source/('kube-bin-source-'+self.run);saved=source/'original-private'
        backup.rename(saved);backup.symlink_to(saved,target_is_directory=True)
        with self.assertRaisesRegex(ValueError,'unsafe'):
            lab.restore_source_clients(self.root,self.run)


class CompletedKubernetesHandoffContracts(ClientHandoffContracts):
    def setUp(self):
        super().setUp()
        s=object.__new__(lab.Session);self.session=s
        s.state=self.root;s.owner=self.root/'owner.json';s.target=self.root/'target-product'
        s.run_id='v1366-final-completion';s.evidence=self.root/'runs'/s.run_id
        self.previous=self.root/'runs'/self.run;self.previous.mkdir(parents=True)
        phases=['source','control','nodes','source-clean','new-install','ingress','target-clean']
        s.plan={'run_id':s.run_id,'phases':phases,'harbor':{'run_id':'v1366-harbor-final-new'}}
        old={'run_id':self.run,'mode':'final','source_commit':lab.SOURCE_COMMIT,
             'phases':phases,'harbor':{'run_id':'v1366-harbor-final-old'}}
        lab.write(self.previous/'plan.json',old)
        for suffix in ['exit','finalized']:(self.root/(self.run+'.'+suffix)).write_text('1\n')
        self.parent_log=self.root/(self.run+'.log')
        self.parent_log.write_text('UPGRADE_NEXT_KUBERNETES_SCOPE_PASS\nTraceback (most recent call last):\n'
            'ValueError: staged product input differs: source-product/kube-bin/kubectl\nNEXT_SESSION_EXIT rc=1\n')
        manifest=self.previous/'staged-input-manifest-before-handoff.json'
        shutil.copy2(self.root/'input-manifest.json',manifest)
        s.plan['kubernetes_reuse']={'run_id':self.run,'manifest_sha256':lab.sha(manifest)}
        markers=['UPGRADE_NEXT_SOURCE_PASS','UPGRADE_NEXT_CONTROL_PASS','UPGRADE_NEXT_NODES_PASS',
                 'UPGRADE_NEXT_SOURCE_CLEAN_PASS','UPGRADE_NEXT_INSTALL_PASS',
                 'UPGRADE_INGRESS_QUALIFICATION_PASS','UPGRADE_NEXT_TARGET_CLEAN_PASS']
        for phase,marker in zip(phases,markers):
            lab.write(self.previous/(phase+'.status.json'),{'rc':0,'attempt':1})
            for suffix in ['exit','finalized']:(self.previous/(phase+'.attempt-1.'+suffix)).write_text('0\n')
            (self.previous/(phase+'.attempt-1.log')).write_text(marker+'\nNEXT_SESSION_PHASE_SCOPE_VERIFY_PASS phase='+phase+'\n')
        override=patch.object(lab,'verify_leases');override.start();self.addCleanup(override.stop)

    def test_real_completed_scope_reuses_seven_logs_but_preserves_failed_parent(self):
        before=self.parent_log.read_bytes()
        result=self.session.reused_kubernetes_scope()
        self.assertEqual(result.evidence,self.previous)
        self.assertEqual(self.parent_log.read_bytes(),before)
        for suffix in ['exit','finalized']:self.assertEqual((self.root/(self.run+'.'+suffix)).read_text(),'1\n')
        self.assertTrue((self.session.evidence/'kubernetes-reuse.json').is_file())

    def test_narrow_preflight_checks_real_handoff_without_recording_or_mutating(self):
        before=self.parent_log.read_bytes()
        self.session.reused_kubernetes_scope(record=False)
        self.assertFalse(self.session.evidence.exists())
        self.assertEqual(self.parent_log.read_bytes(),before)

    def test_fixed_shell_locks_and_routes_readonly_handoff_without_running_session(self):
        root=self.root/'shell-fixture';root.mkdir();(root/'locks').mkdir()
        python=root/'python';calls=root/'calls'
        python.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$HANDOFF_CALLS"\n');python.chmod(0o700)
        module=root/'target-product/upgrade/lab/next_session.py';module.parent.mkdir(parents=True);module.touch()
        script=(ROOT/'upgrade/lab/next-session.sh').read_text()
        script=script.replace(str(lab.STATE),str(root)).replace('/usr/local/kubeauto/.venv/bin/python',str(python))
        script=script.replace('/var/lock/',str(root/'locks')+'/')
        path=root/'session.sh';path.write_text(script)
        env={**os.environ,'HANDOFF_CALLS':str(calls)}
        r=subprocess.run(['bash',path,'final',self.session.run_id,'check-handoff'],env=env,capture_output=True,text=True)
        self.assertEqual(r.returncode,0,r.stderr)
        lines=calls.read_text().splitlines();self.assertEqual(len(lines),2)
        self.assertIn('next_session plan --mode final',lines[0])
        self.assertIn('next_session check-handoff --mode final',lines[1])
        calls.unlink()
        r=subprocess.run(['bash',path,'qualify',self.session.run_id,'check-handoff'],env=env,capture_output=True,text=True)
        self.assertEqual(r.returncode,2);self.assertFalse(calls.exists())

    def test_real_final_handoff_reaches_child_only_after_all_actual_boundary_checks(self):
        from upgrade.lab import harbor_session
        s=self.session;child=Mock();child.run_id=s.plan['harbor']['run_id']
        child.evidence=self.root/'child';child.evidence.mkdir();child.owner=s.owner
        child.plan={'phases':['harbor-source','harbor-clean']}
        def complete():
            self.assertTrue((s.evidence/'kubernetes-reuse.json').exists())
            for phase in child.plan['phases']:
                lab.write(child.evidence/(phase+'.status.json'),{'rc':0,'attempt':1})
                for suffix in ['exit','finalized']:(child.evidence/(phase+'.attempt-1.'+suffix)).write_text('0\n')
        child.run.side_effect=complete
        with patch.object(harbor_session,'HarborSession',return_value=child):s.finish_final()
        d=json.loads((s.evidence/'combined-final.json').read_text())
        self.assertEqual(d['kubernetes_run_id'],self.run)
        self.assertEqual(d['harbor_run_id'],child.run_id)

    def test_failure_retried_scope_or_missing_success_refuse_before_any_child(self):
        from upgrade.lab import harbor_session
        for mode in ['failure','retried','missing-exit','bad-log','missing-clean-marker']:
            with self.subTest(mode=mode),patch.object(harbor_session,'HarborSession') as child:
                paths=[]
                if mode=='failure':
                    p=self.previous/'control.failure-1.json';p.write_text('{}');paths.append((p,None))
                elif mode=='retried':
                    p=self.previous/'nodes.status.json';paths.append((p,p.read_bytes()));lab.write(p,{'rc':0,'attempt':2})
                elif mode=='missing-exit':
                    p=self.previous/'source.attempt-1.exit';paths.append((p,p.read_bytes()));p.unlink()
                else:
                    p=self.previous/'target-clean.attempt-1.log';paths.append((p,p.read_bytes()))
                    p.write_text('[FAIL]\n' if mode=='bad-log' else 'UPGRADE_NEXT_TARGET_CLEAN_PASS\n')
                try:
                    with self.assertRaises((ValueError,FileNotFoundError)):self.session.finish_final()
                    child.assert_not_called();self.assertFalse(self.session.evidence.exists())
                finally:
                    for p,data in paths:
                        if data is None:p.unlink()
                        else:p.write_bytes(data)

    def test_changed_product_binary_or_foreign_lab_module_refuses_even_after_restage(self):
        manifest=self.root/'input-manifest.json';original=manifest.read_bytes()
        for name,relative in [('source-product','kube-bin/kubectl'),('target-product','common/constants.py'),
                              ('target-product','upgrade/lab/harbor_session.py')]:
            with self.subTest(path=relative):
                p=self.root/name/relative;data=p.read_bytes() if p.exists() else None
                p.parent.mkdir(parents=True,exist_ok=True);p.write_text('new dependency')
                current=json.loads(original);current['trees'][name][relative]=lab.sha(p);lab.write(manifest,current)
                with self.assertRaisesRegex(ValueError,'dependency changed'):self.session.reused_kubernetes_scope()
                self.assertFalse(self.session.evidence.exists())
                manifest.write_bytes(original)
                if data is None:p.unlink()
                else:p.write_bytes(data)

    def test_only_exact_gate_and_formal_evidence_changes_can_be_reused(self):
        manifest=self.root/'input-manifest.json';current=json.loads(manifest.read_text())
        for relative in ['upgrade/lab/next_session.py','upgrade/tests/test_next_session.py',
                         'upgrade/v1.36.6/evidence/handoff-repair.json']:
            p=self.session.target/relative;p.parent.mkdir(parents=True,exist_ok=True);p.write_text('qualified repair')
            current['trees']['target-product'][relative]=lab.sha(p)
        lab.write(manifest,current);self.session.reused_kubernetes_scope()

    def test_prior_harbor_mutation_changed_manifest_or_active_owner_refuses(self):
        child=self.root/'runs'/'v1366-harbor-final-old';child.mkdir()
        with self.assertRaisesRegex(ValueError,'pre-Harbor'):self.session.reused_kubernetes_scope()
        child.rmdir();self.session.owner.write_text('{}')
        with self.assertRaisesRegex(ValueError,'unowned'):self.session.reused_kubernetes_scope()
        self.session.owner.unlink();self.session.plan['kubernetes_reuse']['manifest_sha256']='0'*64
        with self.assertRaisesRegex(ValueError,'manifest differs'):self.session.reused_kubernetes_scope()

    def test_completion_main_never_relaunches_the_accepted_kubernetes_scope(self):
        s=Mock();s.plan={'kubernetes_reuse':{'run_id':self.run}}
        with patch.object(lab,'Session',return_value=s):lab.main(['run','--mode','final','--run-id','v1366-final-completion'])
        s.run.assert_not_called();s.finish_final.assert_called_once_with()

    def test_accepted_kubernetes_evidence_cannot_change_during_child_execution(self):
        from upgrade.lab import harbor_session
        child=Mock();child.plan={'phases':[]};child.evidence=self.root/'child'
        child.evidence.mkdir();child.owner=self.session.owner
        child.run.side_effect=lambda:(self.previous/'source.attempt-1.log').write_text('altered')
        with patch.object(harbor_session,'HarborSession',return_value=child):
            with self.assertRaisesRegex(ValueError,'evidence changed'):self.session.finish_final()
        self.assertFalse((self.session.evidence/'combined-final.json').exists())


if __name__=='__main__':
    unittest.main()
