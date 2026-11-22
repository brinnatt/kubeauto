"""Native ARM supplement scope and real product/fixture handoffs; no lab."""
import json
import ast
import inspect
import re
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import yaml

from upgrade.lab import harbor_arm_session as arm
from upgrade.lab import harbor_node as node
from upgrade.product import launcher
from upgrade.tests import test_next_session


class NativeArmHarborContracts(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory(prefix='harbor-arm-contract-')
        self.addCleanup(temporary.cleanup);self.root=Path(temporary.name)
        self.session=object.__new__(arm.ArmHarborSession)
        s=self.session;s.cluster='arm-fixture';s.evidence=self.root/'evidence';s.evidence.mkdir()
        s.run_id='v1366-harbor-arm-fixture';s.plan={'run_id':s.run_id};s.mode='qualify'
        s.cli=Mock();s.node=Mock();s.remote=Mock();s.owner=self.root/'owner.json'
        s.owner.write_text(json.dumps(s.plan))

    def test_real_plan_refuses_pending_and_final_prerequisites_without_mutation(self):
        fixture=test_next_session.NextAdmissionContracts('runTest');fixture.setUp()
        self.addCleanup(fixture.doCleanups);fixture.accept_static()
        matrix=fixture.fixture.matrix
        scope={'enabled':True,'control':arm.CONTROL,'active_hosts':[arm.HOST],
               'phases':arm.PHASES,'runner':'upgrade/lab/harbor_arm_session.py'}
        (matrix.parent/'acceptance-plan.yaml').write_text(yaml.safe_dump({'harbor_native_arm64_supplement':scope}))
        with patch.object(arm,'MATRIX_PATH',matrix):
            result=arm.plan(self.session.run_id,'qualify')
            self.assertEqual(result['active_hosts'],[arm.HOST]);self.assertEqual(result['builds'],[])
            self.assertEqual(result['kubernetes_mutation'],'none')
            with self.assertRaisesRegex(launcher.UpgradeAdmissionRefused,'UPG-05'):
                arm.plan(self.session.run_id,'final')
            scope['active_hosts']=[arm.CONTROL]
            (matrix.parent/'acceptance-plan.yaml').write_text(yaml.safe_dump({'harbor_native_arm64_supplement':scope}))
            with self.assertRaisesRegex(ValueError,'scope must be frozen'):
                arm.plan(self.session.run_id,'qualify')

    def test_remote_rejects_old_vpn_pool_and_all_other_hosts(self):
        for host in ['192.168.122.217','192.168.37.102',arm.CONTROL]:
            with self.assertRaisesRegex(ValueError,'escaped'):
                arm.ArmHarborSession.remote(self.session,host,['true'])

    def test_native_transfers_use_ssh_sftp_without_requiring_remote_rsync(self):
        source=inspect.getsource(arm.ArmHarborSession)
        self.assertNotIn("['rsync'",source)
        self.assertIn("['scp', '-p'",source)
        self.assertIn("['scp', '-rp'",source)
        self.assertIn('not x.is_symlink()',source)
        frontend=(arm.ROOT/'upgrade/lab/harbor-arm-control.sh').read_text()
        programs=re.findall(r"<<'(PY|AUTH|NATIVE)'\n(.*?)\n\1",frontend,re.S)
        self.assertGreaterEqual(len(programs),2)
        for tag,program in programs:ast.parse(program)

    def test_staging_clock_gate_rejects_both_directions_before_node_admission(self):
        from common import ansible_python
        source=(arm.ROOT/'upgrade/lab/harbor-arm-control.sh').read_text()
        program=re.search(r"<<'NATIVE'\n(.*?)\nNATIVE",source,re.S)[1].split('from service.cluster.manager')[0]
        native=SimpleNamespace(executable_path='/usr/bin/ansible',version=(2,16),package_owner='rpm:ansible-core')
        with patch.object(ansible_python,'probe_installed_ansible_core',return_value=native), \
                patch.object(ansible_python,'format_ansible_core_compatibility_failure',return_value=''), \
                patch('subprocess.check_output',return_value='1000'),patch('time.time',return_value=1000):
            exec(program,{})
            for epoch in [879,1121]:
                with patch('time.time',return_value=epoch),self.assertRaisesRegex(AssertionError,'clock delta'):
                    exec(program,{})

    def test_control_inputs_cover_the_real_setup_prerequisite_and_version_reads(self):
        from service.cluster.manager import ClusterManager
        for method in [ClusterManager._require_install_prereqs,ClusterManager._show_component_versions]:
            import textwrap
            tree=ast.parse(textwrap.dedent(inspect.getsource(method)))
            for expression in ast.walk(tree):
                if (isinstance(expression,ast.BinOp) and isinstance(expression.op,ast.Div)
                        and isinstance(expression.left,ast.Attribute)
                        and expression.left.attr in ['kube_bin_dir','extra_bin_dir']
                        and isinstance(expression.right,ast.Constant)):
                    directory='kube-bin' if expression.left.attr=='kube_bin_dir' else 'extra-bin'
                    self.assertIn(expression.right.value,arm.CONTROL_BINARIES[directory])

    def test_archive_transfer_uses_named_directory_and_resumes_restored_boundary(self):
        s=self.session;s.archive_directory=self.root/'recovery';s.archive_directory.mkdir()
        calls=[]
        def transfer(argv):
            self.assertEqual(argv,['scp','-rp','root@'+arm.HOST+':'+arm.NODE_STATE,str(s.archive_directory)+'/'])
            target=s.archive_directory/Path(arm.NODE_STATE).name
            target.mkdir(exist_ok=True);(target/'daemon-fixture.json').write_text('{}')
        s.command=Mock(side_effect=transfer)
        def remote(host,args,**kwargs):
            calls.append(kwargs.get('data',''))
            return 'HARBOR_ARM_SCOPED_CLEAN_VERIFY_PASS'
        s.remote.side_effect=remote
        s.node.return_value={'marker':'HARBOR_ARM_RUNTIME_CONFIG_RESTORED'}
        s.clean_phase()
        self.assertEqual([c.args[0] for c in s.node.call_args_list],
                         ['stop','clean-data','arm-runtime-restore','clean-state'])
        s.node.reset_mock();calls.clear();s.clean_phase()
        self.assertEqual([c.args[0] for c in s.node.call_args_list],['clean-state'])
        self.assertIn('p.read_bytes()==raw',calls[0])
        self.assertIn('"inactive"',calls[0])
        for program in calls:
            if program:ast.parse(program)

    def test_uninstalled_recovery_refuses_completed_business_before_remote_mutation(self):
        s=self.session
        (s.evidence/'harbor-source.status.json').write_text('{"rc":1}')
        (s.evidence/'source-created.json').write_text('{}')
        with patch.object(arm,'LEASE',self.root/'lease'):
            with self.assertRaisesRegex(ValueError,'uninstalled source fixture'):
                s.cleanup_uninstalled()
        s.remote.assert_not_called();s.node.assert_not_called()

    def test_existing_cache_is_reused_but_conflicting_package_tags_are_refused(self):
        expected=arm.expected_images()
        self.assertGreater(len(expected),30)
        tag=next(iter(expected))
        arm.verify_image_cache({tag:expected[tag],'customer/image:stable':'sha256:other'},expected)
        with self.assertRaisesRegex(ValueError,'overwritten'):
            arm.verify_image_cache({tag:'sha256:different'},expected)

    def test_arm_frontend_has_no_legacy_vpn_or_release_and_uses_durable_gate(self):
        text=(arm.ROOT/'upgrade/lab/harbor-arm-control.sh').read_text()
        self.assertNotIn('192.168.122.',text)
        self.assertNotIn('git push',text)
        self.assertIn('tests/helpers/run-durable-gate.sh',text)
        self.assertIn('test ! -e "$state/owner.json"',text)
        self.assertIn('--exclude=\'bootstrap-*.tar\'',text)
        self.assertIn('HARBOR_ARM_NATIVE_CONTROL_PREFLIGHT_PASS',text)
        self.assertNotIn("from ansible.cli.",text)
        self.assertNotIn('$state/recovery/',text)

    def test_install_uses_config_and_public_cli_on_verified_runtime(self):
        directory=self.root/'clusters'/self.session.cluster;directory.mkdir(parents=True)
        (directory/'config.yml').write_text('HARBOR_PATH: /var/data\n')
        with patch.object(arm,'ROOT',self.root):self.session.install('v2.16.0')
        config=yaml.safe_load((directory/'config.yml').read_text())
        self.assertEqual(config['HARBOR_VER'],'v2.16.0')
        args=self.session.cli.call_args.args
        self.assertEqual(args[2:5],('setup',self.session.cluster,'11'))
        self.assertEqual(args[5:7],('--start-at-task',arm.shlex.quote(arm.FIRST_TASK)))

    def test_migration_and_restore_verify_original_ledger_without_recreation(self):
        s=self.session;trace=[]
        s.maintenance=Mock(side_effect=lambda op:trace.append(op))
        s.verify=Mock(side_effect=lambda label:trace.append(label))
        s.migration_phase();s.restore_phase()
        self.assertEqual(trace,['backup','upgrade','migrated','upgrade','migration-idempotent','restore','restored'])
        s.node.assert_not_called()

    def test_new_install_retires_only_after_stop_and_data_cleanup(self):
        s=self.session;trace=[]
        s.node.side_effect=lambda action,**kwargs:trace.append(action) or {'marker':'fixture'}
        s.remote.side_effect=lambda *args,**kwargs:trace.append('restore-owned-tls')
        s.install=Mock(side_effect=lambda version:trace.append(version))
        s.settings=Mock(side_effect=lambda:trace.append('settings'))
        s.verify=Mock(side_effect=lambda label:trace.append(label))
        s.new_install_phase()
        self.assertEqual(trace[:5],['stop','clean-data','retire-fixture','restore-owned-tls','v2.16.0'])
        self.assertEqual(trace[-3:],['new-install','v2.16.0','new-idempotent'])

    def test_public_inventory_cannot_leak_docker_environment_secrets(self):
        qualified=arm.expected_images()
        tag=next(t for t in qualified if t.endswith(':v2.16.0'))
        self.session.node.side_effect=[{'architecture':'aarch64','containers':[
            {'Id':'id','Image':qualified[tag],'State':{'Running':True},
             'Config':{'Image':tag,'Env':['PASSWORD=private-value']}}]},
             {'marker':'HARBOR_BUSINESS_LEDGER_PASS'}]
        self.session.verify('native')
        text=(self.session.evidence/'native-inventory.json').read_text()
        self.assertNotIn('private-value',text);self.assertNotIn('Env',text)

    def test_wrong_runtime_image_identity_refuses_before_business_acceptance(self):
        self.session.node.return_value={'architecture':'aarch64','containers':[
            {'Id':'id','Image':'sha256:foreign','State':{'Running':True},
             'Config':{'Image':'goharbor/harbor-core:v2.16.0'}}]}
        with self.assertRaisesRegex(ValueError,'differs from qualified'):
            self.session.verify('migrated')
        self.assertEqual(self.session.node.call_count,1)

    def test_first_failure_preserves_durable_failure_and_stops_dependent_phases(self):
        s=self.session;s.owner_check=Mock();s.source_phase=Mock(side_effect=ValueError('fixture first failure'))
        s.migration_phase=Mock();s.restore_phase=Mock();s.new_install_phase=Mock();s.clean_phase=Mock()
        with patch.object(arm,'LEASE',self.root/'lease'):
            with self.assertRaisesRegex(ValueError,'first failure'):s.run()
        s.migration_phase.assert_not_called();s.clean_phase.assert_not_called()
        self.assertTrue(s.owner.exists())
        self.assertEqual((s.evidence/'harbor-source.exit').read_text(),'1\n')
        self.assertEqual((s.evidence/'harbor-source.finalized').read_text(),'1\n')


class ArmRuntimeFixtureContracts(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory(prefix='arm-daemon-contract-')
        self.addCleanup(temporary.cleanup);self.root=Path(temporary.name)
        self.state=self.root/'state';self.state.mkdir(mode=0o700)
        self.daemon=self.root/'daemon.json'
        for name,value in [('STATE',self.state),('DAEMON',self.daemon)]:
            override=patch.object(node,name,value);override.start();self.addCleanup(override.stop)
        override=patch.object(node,'command',return_value='inactive\n')
        self.command=override.start();self.addCleanup(override.stop)

    def test_original_invalid_cgroup_and_unrelated_insecure_registry_are_restored_exactly(self):
        raw=b'{"exec-opts":["native.cgroupdriver="],"insecure-registries":["http://easzlab.io.local:5000"]}\n'
        self.daemon.write_bytes(raw);self.daemon.chmod(0o640)
        self.assertEqual(node.arm_runtime_fixture()['marker'],'HARBOR_ARM_RUNTIME_CONFIG_READY')
        self.assertEqual(self.daemon.read_bytes(),b'{}\n')
        self.assertEqual((self.state/'original-daemon.json').read_bytes(),raw)
        self.assertEqual((self.state/'original-daemon.json').stat().st_mode&0o777,0o600)
        self.assertEqual(node.arm_runtime_fixture(restore=True)['marker'],'HARBOR_ARM_RUNTIME_CONFIG_RESTORED')
        self.assertEqual(self.daemon.read_bytes(),raw)
        self.assertEqual(self.daemon.stat().st_mode&0o777,0o640)

    def test_original_absence_is_restored_without_retaining_test_configuration(self):
        node.arm_runtime_fixture();node.arm_runtime_fixture(restore=True)
        self.assertFalse(self.daemon.exists())

    def test_foreign_change_tampered_backup_and_active_runtime_refuse(self):
        self.daemon.write_text('{"log-level":"warn"}')
        node.arm_runtime_fixture();self.daemon.write_text('{"foreign":true}')
        with self.assertRaisesRegex(ValueError,'foreign Docker configuration'):
            node.arm_runtime_fixture(restore=True)
        self.daemon.write_bytes(b'{}\n');(self.state/'original-daemon.json').write_text('tampered')
        with self.assertRaisesRegex(ValueError,'snapshot changed'):
            node.arm_runtime_fixture(restore=True)
        self.command.return_value='active\n'
        with self.assertRaisesRegex(ValueError,'stopped native runtime'):
            node.arm_runtime_fixture(restore=True)

    def test_different_data_root_and_prior_fixture_are_never_adopted(self):
        self.daemon.write_text('{"data-root":"/customer/docker"}')
        with self.assertRaisesRegex(ValueError,'foreign Docker data root'):
            node.arm_runtime_fixture()
        self.assertFalse((self.state/'daemon-fixture.json').exists())
        self.daemon.write_text('{}');node.arm_runtime_fixture()
        with self.assertRaisesRegex(ValueError,'cannot adopt'):
            node.arm_runtime_fixture()


if __name__=='__main__':unittest.main()
