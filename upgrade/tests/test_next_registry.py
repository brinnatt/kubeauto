"""Fixed lab mirror identity and no-overwrite qualification; no network."""
from copy import deepcopy
import hashlib
import io
import json
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch

from upgrade.lab import next_registry as registry


class RegistryContracts(unittest.TestCase):
    def test_exact_lab_scope_contains_only_source_bridge_target_and_ledger_images(self):
        scope=registry.scope()
        self.assertEqual(len(scope),18)
        self.assertIn('brinnatt/calico-node:v3.28.4',scope)
        self.assertIn('brinnatt/calico-node:v3.31.7',scope)
        self.assertIn('brinnatt/calico-node:v3.32.4',scope)
        self.assertFalse(any('longhorn' in name or 'mysql' in name for name in scope))

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
