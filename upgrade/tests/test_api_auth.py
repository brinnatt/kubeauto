"""Prove the 1.35 CA validation and the real, narrowly scoped unit repair."""
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
import yaml

ROOT = Path(__file__).resolve().parents[2]
NATIVE = Path('/tmp/kubeauto-k8s13510-qualified/amd64/k8s/kube-apiserver')


def certificate(root, common_name):
    key=rsa.generate_private_key(public_exponent=65537,key_size=2048)
    name=x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,common_name)])
    now=datetime.now(timezone.utc)
    cert=(x509.CertificateBuilder().subject_name(name).issuer_name(name)
        .public_key(key.public_key()).serial_number(1)
        .not_valid_before(now-timedelta(days=1)).not_valid_after(now+timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True,path_length=None),critical=True)
        .sign(key,hashes.SHA256()))
    (root/'aggregator-proxy.pem').write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    (root/'key.pem').write_bytes(key.private_bytes(serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,serialization.NoEncryption()))


class AuthenticationContracts(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name)
        certificate(self.root,'aggregator')
        self.unit=self.root/'kube-apiserver.service'
        self.original='[Service]\nExecStart=/fixture/api \\\n  --requestheader-allowed-names= \\\n  --other-option=retain\n'
        self.unit.write_text(self.original);self.unit.chmod(0o640)
        fakebin=self.root/'bin';fakebin.mkdir()
        systemctl=fakebin/'systemctl'
        systemctl.write_text('#!'+sys.executable+'\nimport sys\nassert sys.argv[1:]=='+
            repr(['show','--property=FragmentPath','--value','kube-apiserver'])+'\nprint('+repr(str(self.unit))+')\n')
        systemctl.chmod(0o700)
        (self.root/'hosts').write_text('fixture ansible_connection=local ansible_python_interpreter='+sys.executable+'\n')
        play=[{'hosts':'fixture','gather_facts':False,'tasks':[
            {'ansible.builtin.import_tasks':str(ROOT/'upgrade/product/api-auth.yml')}]}]
        (self.root/'book.yml').write_text(yaml.safe_dump(play))
        self.env={**os.environ,'PATH':str(fakebin)+os.pathsep+os.environ['PATH'],
                  'ANSIBLE_LOCAL_TEMP':str(self.root/'ansible-local')}

    def repair(self, observe=False):
        variables={'ca_dir':str(self.root),'UPGRADE_API_AUTH_OBSERVE_ONLY':observe}
        (self.root/'vars.json').write_text(json.dumps(variables))
        return subprocess.run([str(Path(sys.executable).parent/'ansible-playbook'),
            '-i',str(self.root/'hosts'),str(self.root/'book.yml'),'-e','@'+str(self.root/'vars.json')],
            env=self.env,text=True,capture_output=True,timeout=30)

    def test_readonly_then_real_repair_is_idempotent_and_changes_one_option(self):
        cert_before=(self.root/'aggregator-proxy.pem').read_bytes()
        result=self.repair(observe=True)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertEqual(self.unit.read_text(),self.original)
        result=self.repair()
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertEqual(self.unit.read_text(),self.original.replace('allowed-names= ','allowed-names=aggregator '))
        self.assertEqual(self.unit.stat().st_mode & 0o777,0o640)
        self.assertEqual((self.root/'aggregator-proxy.pem').read_bytes(),cert_before)
        result=self.repair()
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertIn('changed=0',result.stdout)

    def test_foreign_certificate_or_policy_refuses_without_editing_unit(self):
        certificate(self.root,'foreign')
        result=self.repair()
        self.assertNotEqual(result.returncode,0)
        self.assertEqual(self.unit.read_text(),self.original)
        certificate(self.root,'aggregator')
        before=self.original.replace('allowed-names= ','allowed-names=custom ')
        self.unit.write_text(before)
        result=self.repair()
        self.assertNotEqual(result.returncode,0)
        self.assertEqual(self.unit.read_text(),before)

    @unittest.skipUnless(NATIVE.is_file(),'qualified official 1.35.10 binary unavailable')
    def test_official_binary_proves_shared_ca_requirement_without_starting_a_server(self):
        checksum=(ROOT/'upgrade/official-snapshots/2026-11-16-next-hop/k8s13510-amd64-kube-apiserver.sha256').read_text().strip()
        self.assertEqual(hashlib.sha256(NATIVE.read_bytes()).hexdigest(),checksum)
        cert=str(self.root/'aggregator-proxy.pem');key=str(self.root/'key.pem')
        args=[str(NATIVE),'--etcd-servers=https://127.0.0.1:2379',
            '--client-ca-file='+cert,'--requestheader-client-ca-file='+cert,
            '--tls-cert-file='+cert,'--tls-private-key-file='+key,
            '--service-account-signing-key-file='+key,'--service-account-key-file='+cert,
            '--service-account-issuer=https://offline.example.invalid',
            '--service-cluster-ip-range=10.96.0.0/12',
            # Both variants must stop during validation, before binding sockets,
            # contacting etcd or creating runtime data on the development host.
            '--max-requests-inflight=-1']
        for allowed in ['', 'aggregator']:
            result=subprocess.run([*args,'--requestheader-allowed-names='+allowed],
                                  capture_output=True,text=True,timeout=20)
            self.assertEqual(result.returncode,1)
            self.assertIn('--max-requests-inflight can not be negative value',result.stderr)
            self.assertEqual('contain overlapping certificates' in result.stderr,not bool(allowed))
