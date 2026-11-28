"""StarCli parameter handoff and real native-client argument contracts."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
TOOL = ROOT/'tools/starrocks/StarCli.py'


class StarCliParameters(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='starcli-parameters-')
        self.addCleanup(self.tmp.cleanup)
        self.base=Path(self.tmp.name)
        spec=importlib.util.spec_from_file_location('starcli_parameters',TOOL)
        with mock.patch.dict(os.environ,STARROCKS_LOG_DIR=str(self.base)):
            self.module=importlib.util.module_from_spec(spec)
            spec.loader.exec_module(self.module)
        self.config=self.base/'config.json'

    def status(self, config, *flags):
        self.config.write_text(json.dumps(config))
        trace=self.base/'mysql.jsonl'
        client=self.base/'mysql'
        client.write_text('#!'+sys.executable+'\nimport json,os,sys\n'
            'with open(os.environ["CALLS"],"a") as f: f.write(json.dumps({"argv":sys.argv[1:],"pwd":os.getenv("MYSQL_PWD")})+"\\n")\n'
            'print("fixture-status")\n')
        client.chmod(0o700)
        result=subprocess.run([sys.executable,'-B',str(TOOL),'--config',str(self.config),'--status',*flags],
            env=dict(os.environ,PATH=str(self.base),STARROCKS_LOG_DIR=str(self.base),CALLS=str(trace)),
            cwd=self.base,capture_output=True,text=True,timeout=12)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        calls=[json.loads(line) for line in trace.read_text().splitlines()]
        self.assertEqual(len(calls),3)
        self.assertEqual({c['argv'][-1] for c in calls},{'SHOW FRONTENDS;','SHOW BACKENDS;','SHOW COMPUTE NODES;'})
        self.assertNotIn('fixture-password',result.stdout+result.stderr)
        return calls

    def test_status_consumes_config_endpoint_and_password(self):
        for call in self.status(dict(fe_host='fe.example.invalid',fe_query_port=19030,root_password='fixture-password')):
            self.assertEqual(call['argv'][call['argv'].index('-h')+1],'fe.example.invalid')
            self.assertEqual(call['argv'][call['argv'].index('-P')+1],'19030')
            self.assertEqual(call['pwd'],'fixture-password')

    def test_status_cli_overrides_config_and_default_endpoint_is_explicit(self):
        for call in self.status(dict(fe_host='other.invalid',fe_query_port=19030,root_password='other'),
                                '--fe-host','cli.invalid','--fe-query-port','19031','--root-password','fixture-password'):
            self.assertEqual(call['argv'][call['argv'].index('-h')+1],'cli.invalid')
            self.assertEqual(call['argv'][call['argv'].index('-P')+1],'19031')
            self.assertEqual(call['pwd'],'fixture-password')

    def routed(self, config, *flags):
        self.config.write_text(json.dumps(config))
        module=self.module
        with mock.patch.object(sys,'argv',['StarCli','--config',str(self.config),*flags]), \
                mock.patch.object(module,'StarRocksDeployer') as factory, \
                contextlib.redirect_stdout(io.StringIO()):
            owner=factory.return_value
            owner.check_environment.return_value=(True,[])
            owner.deploy_fe.return_value=owner.deploy_be.return_value=owner.deploy_cn.return_value=True
            module.main()
            return factory,owner

    def test_deployment_identity_cli_values_override_config(self):
        factory,owner=self.routed(dict(deploy='fe',starrocks_home='/config/home',user='config-user',group='config-group'),
                                   '--deploy','be','--starrocks-home','/cli/home','--user','cli-user','--group','cli-group',
                                   '--fe-host','127.0.0.1','--storage-root-path','/cli/data','--no-systemd')
        factory.assert_called_once_with(starrocks_home='/cli/home',user='cli-user',group='cli-group')
        owner.deploy_be.assert_called_once()
        owner.deploy_fe.assert_not_called()

    def test_verify_failure_is_not_reported_as_success(self):
        for role in ('fe', 'be', 'cn'):
            self.config.write_text('{}')
            with self.subTest(role=role), mock.patch.object(sys,'argv',[
                    'StarCli','--deploy',role,'--starrocks-home','/fixture',
                    '--storage-root-path','/fixture/data','--verify']), \
                    mock.patch.object(self.module,'StarRocksDeployer') as factory, \
                    mock.patch.object(self.module.time,'sleep'), \
                    contextlib.redirect_stdout(io.StringIO()):
                owner=factory.return_value
                owner.check_environment.return_value=(True,[])
                getattr(owner,'deploy_'+role).return_value=True
                owner.verify_fe_started.return_value=False
                owner.verify_be_started.return_value=False
                with self.assertRaises(SystemExit) as error:
                    self.module.main()
                self.assertEqual(error.exception.code,1)

    def test_all_deployment_parameters_reach_owning_operation(self):
        common=['--starrocks-home','/fixture','--priority-networks','10.1.0.0/16',
                '--java-home','/fixture/jdk','--no-systemd','--force','--fe-host','fe.invalid',
                '--fe-query-port','19030','--root-password','fixture-password']
        factory,owner=self.routed({},'--deploy','fe',*common,'--meta-dir','/fixture/meta',
            '--http-port','18030','--rpc-port','19020','--query-port','19031',
            '--edit-log-port','19010','--helper-address','peer.invalid:19010',
            '--use-fqdn','--default-replication-num','2')
        kwargs=owner.deploy_fe.call_args.kwargs
        for name,value in dict(meta_dir='/fixture/meta',http_port=18030,rpc_port=19020,
                query_port=19031,edit_log_port=19010,helper_address='peer.invalid:19010',
                use_fqdn=True,default_replication_num=2,priority_networks='10.1.0.0/16',
                java_home='/fixture/jdk',enable_systemd=False,force=True,password='fixture-password',
                fe_host='fe.invalid',fe_query_port=19030).items():
            self.assertEqual(kwargs[name],value)
        for role in ('be','cn'):
            extra=['--storage-root-path','/fixture/data','--starlet-port','19070'] if role=='be' else []
            factory,owner=self.routed({},'--deploy',role,*common,*extra,'--be-port','19060',
                '--be-http-port','18040','--heartbeat-port','19050','--brpc-port','18060')
            kwargs=getattr(owner,'deploy_'+role).call_args.kwargs
            for name,value in dict(be_port=19060,be_http_port=18040,heartbeat_service_port=19050,
                    brpc_port=18060,enable_systemd=False,force=True,java_home='/fixture/jdk',
                    priority_networks='10.1.0.0/16',fe_host='fe.invalid',fe_query_port=19030,
                    password='fixture-password').items():
                self.assertEqual(kwargs[name],value)
            if role=='be':
                self.assertEqual(kwargs['storage_root_path'],'/fixture/data')
                self.assertEqual(kwargs['starlet_port'],19070)

    def test_fqdn_and_helper_both_reach_systemd_start_command(self):
        text=self.module.SystemdServiceGenerator.generate_fe_service(
            '/fixture', 'operator', 'operators', java_home='/fixture/jdk',
            helper_address='peer.invalid:19010', use_fqdn=True)
        line=next(line for line in text.splitlines() if line.startswith('ExecStart='))
        self.assertIn('--helper peer.invalid:19010',line)
        self.assertIn('--host_type FQDN',line)

    def test_setup_status_and_clean_use_selected_endpoint_and_failures_propagate(self):
        for operation in ('setup','status'):
            factory,owner=self.routed({},'--deploy','be','--starrocks-home','/fixture',
                '--storage-root-path','/fixture/data','--fe-host','fe.invalid','--fe-query-port','19030',
                '--root-password','private','--'+operation)
            call=getattr(owner,'setup_cluster' if operation=='setup' else 'show_cluster_status').call_args.kwargs
            self.assertEqual(call['fe_host'],'fe.invalid')
            self.assertEqual(call['fe_query_port'],19030)
            self.assertEqual(call['root_password' if operation=='setup' else 'password'],'private')
        with mock.patch.object(sys,'argv',['StarCli','--clean','--deploy','be','--starrocks-home','/fixture',
                '--fe-host','fe.invalid','--fe-query-port','19030','--root-password','private']), \
                mock.patch.object(self.module,'StarRocksDeployer') as factory:
            factory.return_value.clean_deployment.return_value=False
            with self.assertRaises(SystemExit) as failure: self.module.main()
            self.assertEqual(failure.exception.code,1)
            factory.return_value.clean_deployment.assert_called_once_with('be',backup_config=True,
                fe_host='fe.invalid',fe_query_port=19030,password='private')


if __name__=='__main__':
    unittest.main()
