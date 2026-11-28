"""All publisher CLI operations, runtime scope, SSH and cleanup handoffs."""
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

ROOT=Path(__file__).resolve().parents[2]


class KubePublishParameters(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='publish-parameters-')
        self.addCleanup(self.tmp.cleanup)
        self.base=Path(self.tmp.name)
        spec=importlib.util.spec_from_file_location('publish_parameters',ROOT/'tools/k8stools/KubePublishCli.py')
        self.m=importlib.util.module_from_spec(spec);spec.loader.exec_module(self.m)

    def invoke(self,*flags,expected=0,fail=None):
        m=self.m
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(sys,'argv',['KubePublishCli',*map(str,flags)]))
            stack.enter_context(mock.patch.object(m.RuntimeChecker,'check_runtime',return_value=True))
            operations={name:stack.enter_context(mock.patch.object(m,name)) for name in
                ('download_images','pack_images','delete_images','distribute_images','cleanup_old_auto_files','cleanup_session_files')}
            for name,operation in operations.items():
                operation.return_value=(True,['owned.tar']) if name=='pack_images' else True
            if fail:
                operations[fail].return_value=False
            stack.enter_context(mock.patch.object(m.ImageManager,'save_specific_tar',return_value=True))
            stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
            self.assertEqual(m.main(),expected)
            return operations

    def test_pipeline_all_runtime_ssh_config_and_cleanup_arguments(self):
        config=self.base/'plan.json'
        config.write_text(json.dumps(dict(download=['example/config:v1'],pack=['example/config:v1'],
            distribute=['ignored.invalid:2022'])))
        ops=self.invoke('--config',config,'--download','example/cli:v2','--pack','example/cli:v2',
            '--distribute','node-{01..02}','node-03:2223','--local-runtime','nerdctl',
            '--remote-runtime','docker','--namespace','fixture-ns','--output-dir',self.base,
            '--ssh-user','operator','--ssh-port','2222','--ssh-key',self.base/'id_key',
            '--disable-ssh-host-check','--cleanup')
        ops['download_images'].assert_called_once_with(['example/cli:v2'],'nerdctl','fixture-ns')
        ops['pack_images'].assert_called_once_with(['example/cli:v2'],'nerdctl',str(self.base),'fixture-ns')
        ops['distribute_images'].assert_called_once_with(hosts=[('node-01',2222),('node-02',2222),('node-03',2223)],
            images_dir=str(self.base),remote_runtime='docker',ssh_user='operator',ssh_key=str(self.base/'id_key'),
            namespace='fixture-ns',strict_host_key_checking=False)
        ops['cleanup_old_auto_files'].assert_called_once_with(str(self.base))

    def test_tar_distribution_defaults_and_failure_cleanup(self):
        archive=self.base/'fixture.tar';archive.touch()
        ops=self.invoke('--distribute','node-01','--tar',archive,'--output-dir',self.base)
        call=ops['distribute_images'].call_args.kwargs
        self.assertEqual(call['hosts'],[('node-01',22)])
        self.assertEqual(call['namespace'],'k8s.io')
        self.assertTrue(call['strict_host_key_checking'])
        ops=self.invoke('--distribute','node-01:22','--ssh-port','2222','--tar',archive,'--output-dir',self.base)
        self.assertEqual(ops['distribute_images'].call_args.kwargs['hosts'],[('node-01',22)])
        ops=self.invoke('--pack','example/app:v1','--distribute','node-01','--output-dir',self.base,
            '--cleanup',expected=1,fail='distribute_images')
        ops['cleanup_session_files'].assert_called_once_with(['owned.tar'])
        ops['cleanup_old_auto_files'].assert_not_called()

    def test_delete_local_and_remote_apply_runtime_namespace_and_ssh_port(self):
        ops=self.invoke('--delete','example/app:v1','--namespace','fixture-ns')
        self.assertEqual(ops['delete_images'].call_args.args[:4],(['example/app:v1'],'docker','fixture-ns',None))
        ops=self.invoke('--delete','example/app:v1','--delete-hosts','node-01','node-02:2223','node-03:22',
            '--remote-runtime','nerdctl','--namespace','fixture-ns','--ssh-user','operator',
            '--ssh-port','2222','--ssh-key',self.base/'key','--disable-ssh-host-check')
        ops['delete_images'].assert_called_once_with(['example/app:v1'],'nerdctl','fixture-ns',
            [('node-01',2222),('node-02',2223),('node-03',22)],'operator',str(self.base/'key'),strict_host_key_checking=False)


if __name__=='__main__':
    unittest.main()
