"""Real CLI failure propagation and OpenVPN configuration consumption."""
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]


class CalicoDeleteContracts(unittest.TestCase):
    def test_delete_failure_is_nonzero_and_stops_at_first_failed_resource(self):
        for executor, layer, failure in (('kubectl', 'pod', 'delete'),
                                         ('kubectl', 'both', 'delete'),
                                         ('calicoctl', 'both', 'delete'),
                                         ('calicoctl', 'host', 'get')):
            with self.subTest(executor=executor, layer=layer, failure=failure), tempfile.TemporaryDirectory() as directory:
                root = Path(directory); trace = root / 'trace'
                stub = root / 'executor'
                stub.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$BOUNDARY_TRACE"\n'
                                'case " $* " in *" api-resources "*) exit 0;; esac\n'
                                'case " $* " in *" '+failure+' "*) echo injected-failure >&2; exit 23;; esac\n'
                                'printf \'{"items":[]}\\n\'\n')
                stub.chmod(0o755)
                result = subprocess.run([sys.executable, '-B', ROOT/'tools/k8stools/CalicoPolicyCli.py',
                    '--context', 'fixture', '--executor', executor, '--kubectl', stub,
                    '--calicoctl', stub, '--no-log-file', 'delete', '--traffic-layer', layer,
                    '-n', 'fixture', '--delete-hostendpoints'],
                    env=dict(os.environ, BOUNDARY_TRACE=str(trace), KUBEAUTO_LOG_DIR=directory),
                    capture_output=True, text=True, timeout=10)
                self.assertNotEqual(result.returncode, 0, result.stdout+result.stderr)
                self.assertIn('injected-failure', result.stdout + result.stderr)
                calls = trace.read_text().splitlines()
                if failure == 'delete':
                    self.assertEqual(sum(' delete ' in ' '+line+' ' for line in calls), 1, calls)
                else:
                    self.assertFalse(any('networkpolicy' in line.split() for line in calls), calls)


class OvpnConfigContracts(unittest.TestCase):
    def load(self, directory):
        with mock.patch.dict(os.environ, GENOVPN_OPENVPN_BASE=directory, GENOVPN_LOG_DIR=directory):
            spec = importlib.util.spec_from_file_location('ovpn_config_boundary', ROOT/'tools/OvpnUserCli.py')
            module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
        return module

    def server(self, module, key, mode='tls-crypt'):
        path = Path(module.SERVER_CONF); path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('proto udp\ndev tun\ncipher AES-256-GCM\n'+mode+' "'+str(key)+'"\n')
        return module.read_server_conf_summary()

    def test_custom_server_key_is_the_key_delivered_to_client(self):
        with tempfile.TemporaryDirectory() as directory:
            module = self.load(directory); root=Path(directory)
            key=root/'custom key'; key.write_text('configured-shared-key')
            server=self.server(module,key)
            for name in ('ca.crt','ta.key','issued/alice.crt','private/alice.key'):
                path=Path(module.pki_path(name)); path.parent.mkdir(parents=True,exist_ok=True)
                path.write_text('old-default' if name=='ta.key' else name)
            link=module.resolve_create_link_options(server,None,None)
            output, _, _=module.bundle_client('alice','vpn.example',1194,link,server)
            self.assertEqual((Path(output)/'ta.key').read_text(),'configured-shared-key')

    def test_v2_refused_before_pki_mutation_and_existing_bundle_deletion(self):
        with tempfile.TemporaryDirectory() as directory:
            module=self.load(directory); root=Path(directory)
            key=root/'server-unwrap.key'; key.write_text('server-only-key')
            server=self.server(module,key,'tls-crypt-v2')
            existing=Path(module.client_bundle_dir('alice')); existing.mkdir(parents=True)
            (existing/'keep').write_text('original')
            args=module.build_parser().parse_args(['create','--user','alice','--rhost','vpn.example'])
            with mock.patch.object(module,'preflight'), mock.patch.object(module,'assert_create_allowed'), \
                 mock.patch.object(module,'get_index_history',return_value=None), \
                 mock.patch.object(module,'easyrsa') as issue, \
                 mock.patch.object(module,'assert_issue_done'), \
                 mock.patch.object(module,'cert_serial',return_value='01'), \
                 mock.patch.object(module,'cert_common_name',return_value='alice'):
                with self.assertRaisesRegex(module.GenovpnError,'tls-crypt-v2'):
                    module.cmd_create(args)
                issue.assert_not_called()
            self.assertEqual((existing/'keep').read_text(),'original')
            with self.assertRaisesRegex(module.GenovpnError,'tls-crypt-v2'):
                module.resolve_client_security_lines(server)

    def test_tls_auth_direction_and_explicit_working_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            module=self.load(directory); root=Path(directory)
            key=root/'configured.key'; key.write_text('shared')
            self.server(module,key,'tls-auth')
            for direction, expected in (('', 'tls-auth ta.key'), (' 0','tls-auth ta.key 1'), (' 1','tls-auth ta.key 0')):
                Path(module.SERVER_CONF).write_text('proto udp\ndev tun\ncipher AES-256-GCM\ncd "'+directory+'"\ntls-auth configured.key'+direction+'\n')
                server=module.read_server_conf_summary()
                self.assertEqual(module.resolve_client_tls_key(server),str(key))
                self.assertIn(expected,module.resolve_client_security_lines(server))
            Path(module.SERVER_CONF).write_text('proto udp\ndev tun\ncipher AES-256-GCM\nkey-direction 0\ntls-auth "'+str(key)+'"\n')
            self.assertIn('tls-auth ta.key 1',module.resolve_client_security_lines(module.read_server_conf_summary()))

    def test_real_cli_refuses_unsupported_or_missing_key_before_creating_pki(self):
        for mode,key in (('tls-crypt-v2','server-only.key'),('tls-crypt','missing.key'),('tls-auth','ambiguous-relative.key')):
            with self.subTest(mode=mode,key=key),tempfile.TemporaryDirectory() as directory:
                module=self.load(directory); self.server(module,key,mode)
                result=subprocess.run([sys.executable,'-B',ROOT/'tools/OvpnUserCli.py','create','--user','alice','--rhost','vpn.example'],
                    env=dict(os.environ,GENOVPN_OPENVPN_BASE=directory,GENOVPN_LOG_DIR=directory),
                    capture_output=True,text=True,timeout=10)
                self.assertNotEqual(result.returncode,0)
                self.assertFalse(Path(module.EASYRSA_DIR).exists())
                self.assertFalse(Path(module.CLIENT_DIR).exists())
                self.assertNotIn('Easy-RSA 工作目录不存在',result.stdout+result.stderr)


if __name__ == '__main__':
    unittest.main()
