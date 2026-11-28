"""Remote CLI transport must preserve business options without forwarding SSH controls."""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import shlex
import sys
import tempfile
import unittest
from unittest import mock

ROOT=Path(__file__).resolve().parents[2]


class RemoteParameterContracts(unittest.TestCase):
    def test_failed_remote_execution_redacts_quoted_secrets_and_removes_transport(self):
        secret="fixture ' quoted $ value"
        for relative,key in (('tools/starrocks/StarCli.py','root_password'),
                             ('tools/kafka/KafkaCli.py','kafka_password')):
            for failure in ('execute','copy'):
                with self.subTest(tool=relative,failure=failure),tempfile.TemporaryDirectory(prefix='remote-failure-') as directory:
                    with mock.patch.dict(os.environ,STARROCKS_LOG_DIR=directory,KAFKA_LOG_DIR=directory):
                        spec=importlib.util.spec_from_file_location('remote_failure_'+Path(relative).stem,ROOT/relative)
                        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
                    source=Path(directory)/'source.json';source.write_text(json.dumps({key:secret}))
                    config={key:secret,'remote_workdir':directory+'/remote path'}
                    args=argparse.Namespace(config=str(source),ssh_user='root',ssh_port=22,ssh_key=None,
                        disable_ssh_host_check=False,remote_workdir=None,**{key:secret})
                    paths=[]
                    def copy(local,remote):
                        paths.append(Path(local))
                        self.assertEqual(json.loads(Path(local).read_text())[key],secret)
                        return failure!='copy'
                    def execute(command,**kwargs):
                        if '--'+key.replace('_','-') in command:
                            return False,'failure '+secret+' '+shlex.quote(secret)
                        return True,'/usr/local/bin/fixture'
                    ssh=mock.Mock();ssh.copy_file.side_effect=copy;ssh.run_command.side_effect=execute
                    with mock.patch.object(module,'SSHManager',return_value=ssh),mock.patch.object(module,'logger') as logger:
                        with self.assertRaises(SystemExit) as result:
                            module._run_remote_deploy('fixture.invalid',args,config,
                                ['cli','--config',str(source),'--'+key.replace('_','-'),secret])
                    self.assertNotEqual(result.exception.code,0)
                    self.assertEqual(len(paths),1)
                    self.assertFalse(paths[0].exists())
                    messages='\n'.join(str(c.args[0]) for c in logger.mock_calls if c.args)
                    self.assertNotIn(secret,messages)
                    self.assertNotIn(shlex.quote(secret),messages)
                    self.assertEqual(json.loads(source.read_text()),{key:secret})

    def test_main_explicit_ssh_values_override_config_before_forwarding(self):
        for relative in ('tools/starrocks/StarCli.py','tools/kafka/KafkaCli.py'):
            with self.subTest(tool=relative),tempfile.TemporaryDirectory(prefix='remote-main-') as directory:
                with mock.patch.dict(os.environ,STARROCKS_LOG_DIR=directory,KAFKA_LOG_DIR=directory):
                    spec=importlib.util.spec_from_file_location('remote_main_'+Path(relative).stem,ROOT/relative)
                    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
                source=Path(directory)/'config.json'
                source.write_text(json.dumps(dict(target_host='config.invalid',ssh_user='config',ssh_port=2222)))
                with mock.patch.object(sys,'argv',['cli','--config',str(source),
                        '--target-host=cli.invalid','--ssh-user=operator','--ssh-port=2345']), \
                        mock.patch.object(module,'_is_local_host',return_value=False), \
                        mock.patch.object(module,'_run_remote_deploy') as remote:
                    module.main()
                call=remote.call_args
                self.assertEqual(call.args[0],'cli.invalid')
                self.assertEqual(call.args[2]['ssh_user'],'operator')
                self.assertEqual(call.args[2]['ssh_port'],2345)

    def test_remote_config_scope_explicit_ssh_options_and_secrets(self):
        for relative,secret_flag in (('tools/starrocks/StarCli.py','root_password'),
                                    ('tools/kafka/KafkaCli.py','kafka_password')):
            with self.subTest(tool=relative),tempfile.TemporaryDirectory(prefix='remote-parameters-') as directory:
                base=Path(directory)
                with mock.patch.dict(os.environ,STARROCKS_LOG_DIR=directory,KAFKA_LOG_DIR=directory):
                    spec=importlib.util.spec_from_file_location('remote_parameters_'+Path(relative).stem,ROOT/relative)
                    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
                config=dict(target_host='node.invalid',ssh_user='operator',ssh_port=2222,
                    ssh_key=str(base/'key'),disable_ssh_host_check=True,remote_workdir=str(base/'remote'),
                    deploy='fe' if secret_flag=='root_password' else 'broker',**{secret_flag:'private-secret'})
                source=base/'config.json';source.write_text(json.dumps(config))
                args=argparse.Namespace(config=str(source),ssh_user='root',ssh_port=22,ssh_key=None,
                    disable_ssh_host_check=False,remote_workdir=None,**{secret_flag:'private-secret'})
                copied=[]
                def copy_file(local,remote):
                    copied.append((json.loads(Path(local).read_text()),remote,Path(local).stat().st_mode & 0o777))
                    return True
                ssh=mock.Mock();ssh.run_command.return_value=(True,'fixture-ok')
                ssh.copy_file.side_effect=copy_file
                with mock.patch.object(module,'SSHManager',return_value=ssh) as factory, \
                        mock.patch.object(module,'logger') as logger:
                    with self.assertRaises(SystemExit) as result:
                        module._run_remote_deploy('node.invalid',args,config,
                            ['cli','--config',str(source),'--target-host=node.invalid','--ssh-port=2222',
                             '--'+secret_flag.replace('_','-'),'private-secret'])
                self.assertEqual(result.exception.code,0)
                factory.assert_called_once_with(host='node.invalid',port=2222,username='operator',
                    key_path=str(base/'key'),strict_host_key_checking=False)
                self.assertEqual(len(copied),1)
                payload,remote,mode=copied[0]
                for key in ('target_host','ssh_user','ssh_port','ssh_key','remote_workdir','disable_ssh_host_check','nodes','batch'):
                    self.assertNotIn(key,payload)
                self.assertEqual(payload[secret_flag],'private-secret')
                self.assertEqual(mode,0o600)
                command=ssh.run_command.call_args.args[0]
                self.assertNotIn('--target-host',command)
                self.assertNotIn('--ssh-port',command)
                self.assertIn('--config',shlex.split(command))
                self.assertNotIn('private-secret',str(logger.mock_calls))
                self.assertEqual(json.loads(source.read_text()),config)


if __name__=='__main__':
    unittest.main()
