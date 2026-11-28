"""CLI dispatch, connection overrides and generated OpenVPN client contents."""
import contextlib
import importlib.util
import io
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]


class OvpnParameters(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='ovpn-parameters-')
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        with mock.patch.dict(os.environ, GENOVPN_OPENVPN_BASE=self.tmp.name, GENOVPN_LOG_DIR=self.tmp.name):
            spec = importlib.util.spec_from_file_location('ovpn_parameters', ROOT/'tools/OvpnUserCli.py')
            self.m = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(self.m)
        Path(self.m.SERVER_CONF).parent.mkdir(parents=True, exist_ok=True)
        key = self.base/'shared.key'
        key.write_text('fixture-shared-key')
        Path(self.m.SERVER_CONF).write_text('proto udp\ndev tun\ncipher AES-256-GCM\ntls-crypt '+str(key)+'\n')
        for name in ('ca.crt','issued/alice.crt','private/alice.key'):
            path=Path(self.m.pki_path(name)); path.parent.mkdir(parents=True,exist_ok=True)
            path.write_text('fixture-'+name)

    def test_create_cli_overrides_reach_generated_bundle_and_defaults_follow_server(self):
        m=self.m
        # PKI signing is separately exercised with real pinned Easy-RSA. This
        # contract preserves main -> cmd_create -> real bundle generation.
        for flags, expected in (([],('61.187.64.38',11940,'udp','tun')),
                (['--rhost','vpn.example','--rport','2443','--proto','tcp','--dev','tap'],
                 ('vpn.example',2443,'tcp','tap'))):
            with mock.patch.object(sys,'argv',['OvpnUserCli','create','-u','alice',*flags]), \
                    mock.patch.object(m,'preflight'), mock.patch.object(m,'assert_create_allowed'), \
                    mock.patch.object(m,'get_index_history',return_value=None), \
                    mock.patch.object(m,'easyrsa') as sign, mock.patch.object(m,'assert_issue_done'), \
                    mock.patch.object(m,'cert_serial',return_value='01'), \
                    mock.patch.object(m,'cert_common_name',return_value='alice'), \
                    contextlib.redirect_stdout(io.StringIO()):
                m.main()
                sign.assert_called_once_with('build-client-full','alice','nopass')
            content=(Path(m.client_bundle_dir('alice'))/'client.ovpn').read_text()
            host,port,proto,dev=expected
            for line in ('remote '+host+' '+str(port),'proto '+proto,'dev '+dev,'tls-crypt ta.key'):
                self.assertIn(line,content)
            self.assertEqual((Path(m.client_bundle_dir('alice'))/'ta.key').read_text(),'fixture-shared-key')

    def test_invalid_create_values_refuse_before_pki_signing(self):
        m=self.m
        for flags in (['--rport','0'],['--rport','65536'],['--rhost',' '],
                      ['--rhost','vpn.example\nscript-security 2'],['--rhost','vpn.example other-host'],
                      ['--proto','invalid'],['--dev','invalid'],['--user','a;touch x']):
            with self.subTest(flags=flags), mock.patch.object(sys,'argv',['OvpnUserCli','create','-u','alice',*flags]), \
                    mock.patch.object(m,'cmd_create') as create, contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaises(SystemExit) as error:
                    m.main()
                self.assertNotEqual(error.exception.code,0)
                create.assert_not_called()

    def test_check_status_and_revoke_dispatch_the_selected_user(self):
        for command,flags in (('check',[]),('status',['--user','alice']),('revoke',['-u','alice'])):
            with mock.patch.object(sys,'argv',['OvpnUserCli',command,*flags]), \
                    mock.patch.object(self.m,'cmd_'+command) as operation:
                self.m.main()
                operation.assert_called_once()
                if flags:
                    self.assertEqual(operation.call_args.args[0].user,'alice')

    def test_missing_pki_and_unsafe_names_refuse_before_signing(self):
        for command in ('create','status','revoke'):
            for user in ('../alice','alice;touch injected','server','ca'):
                with self.subTest(command=command,user=user), \
                        mock.patch.object(sys,'argv',['OvpnUserCli',command,'--user',user]), \
                        mock.patch.object(self.m,'easyrsa') as native, \
                        contextlib.redirect_stdout(io.StringIO()):
                    with self.assertRaises(SystemExit) as error:self.m.main()
                    self.assertNotEqual(error.exception.code,0)
                    native.assert_not_called()
        with mock.patch.object(sys,'argv',['OvpnUserCli','check']), \
                mock.patch.object(self.m,'easyrsa') as native, \
                contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit) as error:self.m.main()
            self.assertNotEqual(error.exception.code,0)
            native.assert_not_called()

    def test_duplicate_issue_preserves_existing_certificate_and_bundle(self):
        m=self.m
        cert=Path(m.issued_cert('alice')); original=cert.read_bytes()
        bundle=Path(m.client_bundle_dir('alice'));bundle.mkdir(parents=True)
        (bundle/'client.ovpn').write_text('existing bundle')
        with mock.patch.object(m,'preflight'),mock.patch.object(m,'easyrsa') as native, \
                mock.patch.object(m,'has_valid_cert',return_value=True), \
                mock.patch.object(m,'cert_common_name',return_value='alice'), \
                mock.patch.object(m,'log_user_status'), \
                contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(m.GenovpnError,'已有有效证书'):
                m.cmd_create(m.build_parser().parse_args(['create','--user','alice']))
            native.assert_not_called()
        self.assertEqual(cert.read_bytes(),original)
        self.assertEqual((bundle/'client.ovpn').read_text(),'existing bundle')

    def test_revocation_retries_crl_without_repeating_ca_revoke(self):
        m=self.m
        index=Path(m.pki_path('index.txt'))
        index.write_text('V\t300101000000Z\t\t01\tunknown\t/CN=alice\n')
        Path(m.private_key('alice')).write_text('private')
        calls=[]
        def native(command,*args):
            calls.append(command)
            if command=='revoke':
                index.write_text('R\t300101000000Z\t261128000000Z\t01\tunknown\t/CN=alice\n')
                archive=Path(m.revoked_archive_paths('01')['cert'])
                archive.parent.mkdir(parents=True,exist_ok=True)
                Path(m.issued_cert('alice')).replace(archive)
                Path(m.private_key('alice')).unlink()
            elif calls.count('gen-crl')==1:
                raise m.GenovpnError('fixture CRL generation failed')
            else:
                Path(m.pki_path('crl.pem')).write_text('regenerated')
        with mock.patch.object(m,'preflight'),mock.patch.object(m,'easyrsa',native), \
                mock.patch.object(m,'cert_serial',return_value='01'), \
                mock.patch.object(m,'cert_common_name',return_value='alice'), \
                mock.patch.object(m,'crl_revoked_count',return_value=1), \
                mock.patch.object(m,'has_valid_cert',side_effect=lambda user:Path(m.issued_cert(user)).exists()), \
                contextlib.redirect_stdout(io.StringIO()):
            args=m.build_parser().parse_args(['revoke','--user','alice'])
            with self.assertRaisesRegex(m.GenovpnError,'CRL 更新失败'):m.cmd_revoke(args)
            m.cmd_revoke(args)
        self.assertEqual(calls,['revoke','gen-crl','gen-crl'])
        self.assertFalse(Path(m.client_bundle_dir('alice')).exists())
        self.assertEqual(Path(m.pki_path('crl.pem')).read_text(),'regenerated')

    def test_crl_retry_refuses_an_archive_with_a_different_identity(self):
        m=self.m
        Path(m.issued_cert('alice')).unlink()
        Path(m.pki_path('index.txt')).write_text('R\t300101000000Z\t261128000000Z\t01\tunknown\t/CN=alice\n')
        cert=Path(m.revoked_archive_paths('01')['cert']);cert.parent.mkdir(parents=True);cert.write_text('fixture')
        for cn,serial in (('bob','01'),('alice','02')):
            with mock.patch.object(m,'preflight'),mock.patch.object(m,'easyrsa') as native, \
                    mock.patch.object(m,'has_valid_cert',return_value=False), \
                    mock.patch.object(m,'cert_common_name',return_value=cn), \
                    mock.patch.object(m,'cert_serial',return_value=serial),contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaisesRegex(m.GenovpnError,'归档与 CA 记录不一致'):
                    m.cmd_revoke(m.build_parser().parse_args(['revoke','--user','alice']))
                native.assert_not_called()


if __name__ == '__main__':
    unittest.main()
