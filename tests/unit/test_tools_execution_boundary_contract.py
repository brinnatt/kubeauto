"""Exercise remote handoffs locally, without SSH or service mutations."""
import argparse
import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]


class ExecutionBoundaryTests(unittest.TestCase):
    def test_netcheck_fixture_requires_real_result_and_refuses_foreign_scope(self):
        spec=importlib.util.spec_from_file_location('netcheck_fixture',ROOT/'tests/helpers/netcheck-live-fixture.py')
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        args=module.probe_arguments(Path('/tmp/owned'),['127.0.0.1-2','localhost'])
        self.assertEqual(args[args.index('-d')+1], '8')
        self.assertGreater(int(args[args.index('-d')+1]), 2*2)
        self.assertEqual(args[args.index('--max-icmp-workers')+1], '2')
        self.assertEqual(args[-2:], ['127.0.0.1-2','localhost'])
        summary={'target_count':1,'overall_ok':False}
        module.verify_result(subprocess.CompletedProcess([],2,''),summary,2,1,False)
        for rc,body,count,ok in ((0,'',1,False),(2,'Traceback',1,False),(2,'',2,False),(2,'',1,True)):
            with self.assertRaisesRegex(ValueError,'RESULT_REFUSED'):
                module.verify_result(subprocess.CompletedProcess([],rc,body),summary,2,count,ok)
        with mock.patch.object(module.Path,'cwd',return_value=Path('/tmp/foreign')),mock.patch.object(module.socket,'socket') as sock:
            with self.assertRaisesRegex(ValueError,'SCOPE_REFUSED'):module.main()
            sock.assert_not_called()

    def test_kdd_crd_inventory_does_not_require_a_nonexistent_profile_crd(self):
        spec=importlib.util.spec_from_file_location('boundary_inventory',ROOT/'tests/helpers/tools-execution-boundary-live.py')
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        docs=[{'metadata':{'name':name+'.crd.projectcalico.org'}}
              for name in ('globalnetworkpolicies','hostendpoints','clusterinformations','tiers')]
        self.assertEqual(module.select_calico_crds(docs),docs)
        with self.assertRaisesRegex(ValueError,'CRD_INVENTORY_REFUSED'):
            module.select_calico_crds(docs[:-1])

    def test_ovpn_live_refusal_requires_expected_error_and_unchanged_pki(self):
        spec = importlib.util.spec_from_file_location('ovpn_fixture', ROOT/'tests/helpers/ovpn-config-fixture.py')
        module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
        module.verify_revoked_certificate(subprocess.CompletedProcess([],2,'','error 23: certificate revoked'))
        for rc,message in ((0,'certificate revoked'),(2,'unable to get issuer certificate'),(1,'')):
            with self.assertRaisesRegex(ValueError,'REVOKED_CERTIFICATE_ACCEPTED_OR_UNRELATED_ERROR'):
                module.verify_revoked_certificate(subprocess.CompletedProcess([],rc,'',message))
        with mock.patch.dict(os.environ, EASYRSA_BATCH='1'):
            self.assertNotIn('EASYRSA_BATCH', module.product_environment('/fixture', '/easy', '/logs'))
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            (base/'serial').write_text('01')
            command = [sys.executable, '-B', '-c', 'import sys; print("expected-refusal"); sys.exit(1)']
            module.refusal_unchanged(command, dict(os.environ), base, 'expected-refusal')
            for code in ('print("expected-refusal")',
                         'import sys; print("wrong-error"); sys.exit(1)',
                         'import pathlib,sys; pathlib.Path('+repr(str(base/'serial'))+').write_text("02"); print("expected-refusal"); sys.exit(1)'):
                with self.assertRaises(ValueError):
                    module.refusal_unchanged([sys.executable, '-B', '-c', code], dict(os.environ), base, 'expected-refusal')

    def test_source_fixture_supplies_a_real_importable_pinned_distro_module(self):
        spec=importlib.util.spec_from_file_location('boundary_gate',ROOT/'tests/helpers/tools-execution-boundary-live.py')
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as directory:
            shutil.copy2(module.source_distro_dependency(),Path(directory)/'distro.py')
            result=subprocess.run([sys.executable,'-B','-c','import distro; print(distro.__version__)'],cwd=directory,
                env=dict(os.environ,PYTHONPATH=directory),capture_output=True,text=True,check=True)
            self.assertEqual(result.stdout.strip(),'1.9.0')

    def test_remote_gate_checks_actual_cli_refusal_and_rejects_interpreter_errors(self):
        spec=importlib.util.spec_from_file_location('boundary_gate',ROOT/'tests/helpers/tools-execution-boundary-live.py')
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        for tool,message in (('KafkaCli','必须指定 --kafka-home 或配置文件中的 kafka_home'),
                             ('StarCli','清理操作必须指定节点类型: --deploy [fe|be|cn]')):
            module.verify_remote_refusal(tool,subprocess.CompletedProcess([],1,message,''))
            for code,output in ((0,message),(1,'SyntaxError '+message),(1,'ModuleNotFoundError'),(1,'connection refused')):
                with self.assertRaises(ValueError):
                    module.verify_remote_refusal(tool,subprocess.CompletedProcess([],code,output,''))

    def test_frozen_remote_bootstrap_executes_the_uploaded_binary(self):
        for relative, executable in (('tools/kafka/KafkaCli.py', 'kafkacli'),
                                     ('tools/starrocks/StarCli.py', 'starcli')):
            with self.subTest(tool=relative), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                with mock.patch.dict(os.environ, KAFKA_LOG_DIR=directory, STARROCKS_LOG_DIR=directory):
                    spec = importlib.util.spec_from_file_location('boundary_' + executable, ROOT / relative)
                    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
                binary = root / executable
                binary.write_text('#!/bin/sh\ntest "$1" = --help || exit 17\nprintf "REMOTE_BINARY_EXECUTED\\n"\n')
                binary.chmod(0o755)
                calls = []
                class LocalSSH:
                    def run_command(self, command, timeout=0):
                        calls.append(command)
                        if command.startswith('command -v '):
                            return False, ''
                        result = subprocess.run(command, shell=True, capture_output=True, text=True)
                        return result.returncode == 0, result.stdout + result.stderr
                    def copy_file(self, source, destination):
                        shutil.copy2(source, destination)
                        return True
                args = argparse.Namespace(ssh_user='fixture', ssh_port=22, ssh_key=None,
                                          disable_ssh_host_check=False, remote_workdir=str(root / 'remote path'), config=None)
                with mock.patch.object(module, '__file__', str(root / '_MEI/main.py')), \
                     mock.patch.object(module.sys, 'frozen', True, create=True), \
                     mock.patch.object(module.sys, 'executable', str(binary)), \
                     mock.patch.object(module.shutil, 'which', return_value=str(binary)), \
                     mock.patch.object(module, 'SSHManager', return_value=LocalSSH()):
                    with self.assertRaises(SystemExit) as exit_state:
                        module._run_remote_deploy('fixture.example', args, {},
                                                  [executable, '--help', '--target-host', 'fixture.example'])
                self.assertEqual(exit_state.exception.code, 0)
                self.assertFalse(calls[-1].startswith('python3 '), calls[-1])


if __name__ == '__main__':
    unittest.main()
