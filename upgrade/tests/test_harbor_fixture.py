"""Business ledger contracts: no repair writes can hide lost identities/data."""
from copy import deepcopy
import io
import json
import os
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import patch

from upgrade.lab.harbor_fixture import CONTENT, Ledger, private_json, read_private


def image_archive(content=CONTENT):
    layer = io.BytesIO()
    with tarfile.open(fileobj=layer, mode='w') as archive:
        member = tarfile.TarInfo('confirmed.txt'); member.size = len(content)
        archive.addfile(member, io.BytesIO(content))
    result = io.BytesIO()
    with tarfile.open(fileobj=result, mode='w') as archive:
        for name, data in [('manifest.json', json.dumps([{'Layers': ['layer.tar']}]).encode()),
                           ('layer.tar', layer.getvalue())]:
            member = tarfile.TarInfo(name); member.size = len(data)
            archive.addfile(member, io.BytesIO(data))
    return result.getvalue()


class Fixture(Ledger):
    def __init__(self, state):
        super().__init__({'hostname': 'registry.example.invalid', 'port': 8443,
                         'password': 'original-private-admin', 'ca': 'unused', 'docker': '/fixture/docker'}, state)
        self.methods=[]; self.commands=[]; self.data=CONTENT
        self.members=[{'id': 31, 'entity_id': 8, 'role_id': 3, 'entity_name': 'upgrade-reader'}]
        self.project_id=21; self.artifact_id=41; self.image_id='sha256:'+'1'*64
        self.digest='sha256:'+'2'*64
        private_json(self.state/'ledger.json', {'project_id': 21, 'members': deepcopy(self.members),
            'artifact_id': 41, 'digest': self.digest, 'image_id': self.image_id,
            'payload_sha256': __import__('hashlib').sha256(CONTENT).hexdigest(),
            'reader': ['upgrade-reader', 'original-reader-secret'],
            'robot': ['robot$upgrade-private+upgrade-pull', 'original-robot-secret'], 'robot_id': 51})

    def api(self, method, path, *args, **kwargs):
        self.methods.append(method)
        if kwargs.get('identity', True) is None:return None
        if path=='projects/upgrade-private':return {'project_id': self.project_id, 'metadata': {'public': 'false'}}
        if '/members?' in path:return self.members
        if path=='robots/51':return {'name': 'robot$upgrade-private+upgrade-pull', 'disable': False}
        if '/artifacts/confirmed' in path:return {'id': self.artifact_id, 'digest': self.digest}
        raise AssertionError((method,path))

    def docker(self, *args, **kwargs):
        self.commands.append((args, kwargs))
        if args[:2]==('image','ls'):return b''
        if args[:2]==('image','save'):return image_archive(self.data)
        return b''


class HarborLedgerContracts(unittest.TestCase):
    def test_verify_is_read_only_at_server_and_repulled_for_each_original_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger=Fixture(Path(directory)); before=(ledger.state/'ledger.json').read_bytes()
            result=ledger.verify()
            self.assertEqual(result['marker'],'HARBOR_BUSINESS_LEDGER_PASS')
            self.assertEqual(set(ledger.methods),{'GET'})
            self.assertEqual((ledger.state/'ledger.json').read_bytes(),before)
            pulls=[(a,k) for a,k in ledger.commands if a[0]=='pull']
            self.assertEqual([k['identity'][0] for a,k in pulls],
                             ['upgrade-reader','robot$upgrade-private+upgrade-pull'])
            self.assertTrue(all('@sha256:' in a[1] for a,k in pulls))
            for index,(args,kwargs) in enumerate(ledger.commands):
                if args[0]=='pull':self.assertEqual(ledger.commands[index-1][0][:3],('image','ls','--all'))
            denied=[k for a,k in ledger.commands if a[0]=='push']
            self.assertEqual(len(denied),2)
            self.assertTrue(all(k['expected_success'] is False for k in denied))

    def test_changed_identity_permission_or_payload_refuses_without_recreating_state(self):
        for changed in ['project', 'artifact', 'role', 'payload']:
            with self.subTest(changed=changed), tempfile.TemporaryDirectory() as directory:
                ledger=Fixture(Path(directory))
                if changed=='project':ledger.project_id=999
                if changed=='artifact':ledger.artifact_id=999
                if changed=='role':ledger.members[0]['role_id']=1
                if changed=='payload':ledger.data=b'wrong restored bytes'
                original=(ledger.state/'ledger.json').read_bytes()
                with self.assertRaises(ValueError):ledger.verify()
                self.assertEqual(set(ledger.methods),{'GET'})
                self.assertEqual((ledger.state/'ledger.json').read_bytes(),original)

    def test_local_foreign_tag_refuses_deletion_and_cached_fixture_is_removed(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger=Fixture(Path(directory)); record=read_private(ledger.state/'ledger.json')
            row={'Repository':ledger.authority+'/upgrade-private/confirmed','Tag':'confirmed',
                 'ID':'sha256:foreign'}
            with patch.object(ledger,'docker',return_value=json.dumps(row).encode()) as docker:
                with self.assertRaisesRegex(ValueError,'replaced'):ledger.remove_local_fixture(record)
                self.assertEqual(docker.call_count,1)
            row['ID']=record['image_id']
            with patch.object(ledger,'docker',side_effect=[json.dumps(row).encode(),b'',b'',b'']) as docker:
                ledger.remove_local_fixture(record)
                self.assertEqual(docker.call_args_list[1].args,('image','rm',ledger.image))
            with patch.object(ledger,'docker',side_effect=[b'',b'',json.dumps(row).encode()]):
                with self.assertRaisesRegex(ValueError,'remains cached'):ledger.remove_local_fixture(record)

    def test_negative_push_requires_auth_refusal_and_never_emits_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger=Fixture(Path(directory))
            for message in [b'network unreachable',b'x509: certificate expired',b'denied: requested access']:
                result=__import__('subprocess').CompletedProcess([],1,stdout=b'',stderr=message)
                with patch('upgrade.lab.harbor_fixture.subprocess.run',return_value=result):
                    if message.startswith(b'denied'):
                        Ledger.docker(ledger,'push',ledger.image,expected_success=False)
                    else:
                        with self.assertRaisesRegex(ValueError,'authorization refusal'):
                            Ledger.docker(ledger,'push',ledger.image,expected_success=False)

    def test_credentials_and_original_ledger_are_private_and_cannot_be_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'credentials.json';private_json(path,{'secret':'private'})
            self.assertEqual(path.stat().st_mode & 0o777,0o600)
            with self.assertRaises(FileExistsError):private_json(path,{'secret':'changed'})
            path.chmod(0o644)
            with self.assertRaises(ValueError):read_private(path)
            path.chmod(0o600);link=Path(directory)/'link';link.symlink_to(path)
            with self.assertRaises(ValueError):read_private(link)


if __name__=='__main__':unittest.main()
