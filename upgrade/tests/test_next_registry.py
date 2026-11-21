"""Fixed lab mirror identity and no-overwrite qualification; no network."""
from copy import deepcopy
import hashlib
import io
import json
import re
import threading
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch

from upgrade.lab import next_registry as registry


class RegistryContracts(unittest.TestCase):
    def test_ingress_scope_is_only_the_two_currently_verified_frozen_images(self):
        self.assertEqual(set(registry.scope(ingress=True)),{
            'brinnatt/ingress-nginx-controller:v1.15.1','brinnatt/kube-webhook-certgen:v1.6.9'})
        for value in registry.scope(ingress=True).values():
            self.assertRegex(value['platforms']['linux/amd64'],r'^sha256:[a-f0-9]{64}$')

    def test_fixed_tunnel_routes_real_http_without_private_headers_and_closes_on_failure(self):
        requests=[]
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                requests.append((self.path,dict(self.headers)))
                self.send_response(200);self.end_headers();self.wfile.write(b'{}')
            def log_message(self,*args):pass
        servers=[]
        process=Mock();process.poll.return_value=None
        def launch(command,**kwargs):
            self.assertIn('root@192.168.47.130',command)
            self.assertEqual(command[-1],'root@192.168.122.2')
            forward=command[command.index('-L')+1].split(':')
            self.assertEqual(forward[0::2],['127.0.0.1','127.0.0.1'])
            self.assertEqual(forward[3],'5000')
            server=ThreadingHTTPServer(('127.0.0.1',int(forward[1])),Handler)
            servers.append(server)
            threading.Thread(target=server.serve_forever,daemon=True).start()
            return process
        try:
            with patch.object(registry.subprocess,'Popen',side_effect=launch):
                with self.assertRaisesRegex(RuntimeError,'intentional'):
                    with registry.lab_tunnel() as opener:
                        client=object.__new__(registry.Registry)
                        client.local_opener=opener;client.opener=Mock();client.tokens={}
                        client.basic='private-must-stay-local'
                        self.assertEqual(client.document('brinnatt/test','manifests','v1',local=True)[0],{})
                        client.opener.open.assert_not_called()
                        with self.assertRaisesRegex(ValueError,'escaped'):
                            opener.open('http://192.168.122.1:5000/v2/')
                        raise RuntimeError('intentional')
            self.assertEqual(requests[0][0],'/v2/brinnatt/test/manifests/v1')
            self.assertEqual(requests[0][1]['Host'],'192.168.122.2:5000')
            self.assertNotIn('Authorization',requests[0][1])
            process.terminate.assert_called_once();process.wait.assert_called_once()
        finally:
            for server in servers:server.shutdown();server.server_close()

    def test_exact_lab_scope_contains_only_current_retained_components_and_ledger_images(self):
        scope=registry.scope()
        self.assertEqual(set(scope),{
            'brinnatt/pause:3.10','brinnatt/busybox:1.37',
            'brinnatt/local-path-provisioner:v0.0.37',
            'brinnatt/calico-node:v3.32.4','brinnatt/calico-cni:v3.32.4',
            'brinnatt/calico-kube-controllers:v3.32.4','brinnatt/coredns:1.15.0',
            'brinnatt/k8s-dns-node-cache:1.26.8','brinnatt/metrics-server:v0.9.1'})
        self.assertTrue(all(re.fullmatch('sha256:[a-f0-9]{64}',
            item['platforms']['linux/amd64']) for item in scope.values()))

    def test_build_metadata_equivalence_keeps_all_runtime_and_rootfs_constraints(self):
        base={'architecture':'amd64','os':'linux','rootfs':{'type':'layers','diff_ids':['sha256:'+'a'*64]},
              'config':{'Env':['PATH=/bin'],'Cmd':['sh'],'User':'','Labels':{'build':'old'}},'created':'old','history':[]}
        changed=deepcopy(base);changed.update(created='new',history=[{}]);changed['config']['Labels']={'build':'new'}
        self.assertTrue(registry.equivalent(base,changed))
        for key,value in [('Cmd',['rm','-rf','/']),('Env',['PATH=/foreign']),('User','1000')]:
            changed=deepcopy(base);changed['config'][key]=value
            self.assertFalse(registry.equivalent(base,changed))
        changed=deepcopy(base);changed['rootfs']['diff_ids']=['foreign']
        self.assertFalse(registry.equivalent(base,changed))

    def test_writes_cannot_escape_fixed_registry_even_to_official_hub(self):
        client=object.__new__(registry.Registry);client.opener=Mock()
        for url in ['https://hub.talkedu.cn/v2/brinnatt/test/manifests/v1',
                    'http://192.168.122.1:5000/v2/brinnatt/test/manifests/v1',
                    registry.LAB+'/v2/foreign/test/manifests/v1']:
            with self.assertRaisesRegex(ValueError,'escaped'):client.write(url,'PUT',b'content')
        client.opener.open.assert_not_called()

    def test_blob_hash_failure_blocks_upload_and_preserves_registry(self):
        client=object.__new__(registry.Registry)
        client.get=Mock(side_effect=[None,io.BytesIO(b'corrupt')]);client.write=Mock()
        with self.assertRaisesRegex(ValueError,'checksum differs'):
            client.blob('brinnatt/test',{'digest':'sha256:'+hashlib.sha256(b'accepted').hexdigest(),'size':8})
        client.write.assert_not_called()

    def test_existing_different_runtime_tag_is_refused_without_overwrite(self):
        client=SimpleNamespace(document=Mock(side_effect=[
            ({'config':{'digest':'sha256:source'}},b'original','sha256:original'),
            ({'config':{'digest':'sha256:local'}},b'local','sha256:local'),
            ({'config':{'Cmd':['expected']}},b'',None),
            ({'config':{'Cmd':['foreign']}},b'',None)]),write=Mock(),blob=Mock())
        with patch.object(registry,'scope',return_value={'brinnatt/test:v1':{'platforms':{'linux/amd64':'sha256:original'}}}):
            with self.assertRaisesRegex(ValueError,'do not overwrite'):
                registry.execute(client,publish=True)
        client.write.assert_not_called();client.blob.assert_not_called()


if __name__=='__main__':unittest.main()
