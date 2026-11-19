"""Qualify the actual sibling readback script without contacting Docker."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch

import yaml

ROOT = Path(__file__).resolve().parents[2]
SIBLING = ROOT.parent/'kubeauto-ext-bin-dockerfile'
SPEC = importlib.util.spec_from_file_location('extbin_base_readback', SIBLING/'upgrade/verify_base.py')
readback = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(readback)


def digest(raw):
    return 'sha256:'+hashlib.sha256(raw).hexdigest()


class RetainedBaseReadbackContracts(unittest.TestCase):
    def setUp(self):
        self.commands = []
        self.children = {a: json.dumps({'architecture': a}).encode() for a in ['amd64', 'arm64']}
        self.index = json.dumps({'manifests': [{'digest': digest(raw),
            'platform': {'os': 'linux', 'architecture': arch}} for arch, raw in self.children.items()]}).encode()
        self.bases = {name: (tag, digest(self.index), directory)
                      for name, (tag, _, directory) in readback.BASES.items()}
        self.platforms = {}
        self.next_id = 0
        self.bad_elf = False
        self.copy_error = False
        self.nonexecutable_runc = False

    def docker(self, argv):
        self.commands.append(argv)
        if argv[:4] == ['docker', 'buildx', 'imagetools', 'inspect']:
            image = argv[4]
            if '@' not in image:
                return self.index
            return next(raw for raw in self.children.values() if digest(raw) == image.split('@')[1])
        if argv[:2] == ['docker', 'pull']:
            return b''
        if argv[:2] == ['docker', 'create']:
            self.next_id += 1
            identity = str(self.next_id).zfill(64)
            self.platforms[identity] = argv[3]
            return identity.encode()
        if argv[:2] == ['docker', 'rm']:
            return b''
        if argv[:2] == ['docker', 'cp']:
            if self.copy_error:
                raise RuntimeError('fixture copy failure')
            container, source = argv[2].split(':')
            platform = self.platforms[container]
            machine = 62 if platform == 'linux/amd64' or self.bad_elf else 183
            raw = b'\x7fELF'+b'\x02\x01'+b'\0'*12+struct.pack('<H', machine)
            names = ['nginx', 'chronyd', 'keepalived']
            if source == '/extra/.':
                names += ['etcd','etcdctl','etcdutl','cfssl','cfssljson','cfssl-certinfo','helm',
                    'crictl','nerdctl','cri-dockerd','calicoctl','docker-compose','docker-buildx','cilium','hubble',
                    'containerd-bin/containerd','containerd-bin/containerd-shim-runc-v2',
                    'containerd-bin/ctr','containerd-bin/runc']
                for name in ['cephadm','cephadm.sha256','docker-runtime-artifacts.sha256']:
                    (Path(argv[3])/name).write_text('fixture\n')
            for name in names:
                p = Path(argv[3])/name;p.parent.mkdir(parents=True, exist_ok=True)
                p.write_bytes(raw);p.chmod(0o755)
                if name == 'containerd-bin/runc' and self.nonexecutable_runc:
                    p.chmod(0o644)
            return b''
        self.fail('unmodelled Docker operation: '+str(argv))

    def test_real_main_only_inspects_pulls_and_extracts_stopped_exact_architecture_packs(self):
        before = Path.cwd()
        with tempfile.TemporaryDirectory() as directory, patch.object(readback, 'run', self.docker), patch.object(readback, 'BASES', self.bases):
            try:
                os.chdir(directory)
                readback.main()
                receipt = json.loads(Path('evidence/readback.json').read_text())
            finally:
                os.chdir(before)
        self.assertEqual(receipt['marker'], 'EXT_BIN_RETAINED_BASE_READBACK_PASS')
        self.assertEqual(len(receipt['records']), 4)
        self.assertEqual(receipt['built_images'], [])
        self.assertEqual(receipt['published_images'], [])
        self.assertEqual(receipt['started_containers'], [])
        self.assertEqual([c[3] for c in self.commands if c[:2] == ['docker', 'create']],
                         ['linux/amd64','linux/arm64']*2)
        self.assertEqual(len([c for c in self.commands if c[:2] == ['docker','rm']]), 4)
        self.assertFalse(any(c[1] in ['build','run','start','push','exec'] for c in self.commands))

    def test_stale_index_refuses_before_pulling_any_layers(self):
        with patch.object(readback,'run',self.docker), self.assertRaisesRegex(ValueError,'tag differs'):
            readback.manifests('fixture/base','1.16.0','sha256:'+'f'*64)
        self.assertTrue(all(c[1:4] == ['buildx','imagetools','inspect'] for c in self.commands))

    def test_inventory_preserves_nonexecutable_asset_mode_without_chmod_or_start(self):
        # The released downloader downloads runc without chmod; the product's
        # containerd role installs its assets with mode=0755. Readback inventories
        # package bytes/modes, rather than asserting installed-node permissions.
        self.nonexecutable_runc = True
        with patch.object(readback, 'run', self.docker):
            files = readback.files('fixture/base', 'sha256:'+'a'*64,
                                   'linux/amd64', '/extra')
        self.assertEqual(files['containerd-bin/runc']['mode'], '0o644')
        self.assertEqual(files['containerd-bin/runc']['elf_machine'], 62)
        self.assertEqual(self.commands[-1][:2], ['docker', 'rm'])
        self.assertFalse(any(c[1] in ['run','start','exec','chmod'] for c in self.commands))

    def test_missing_or_duplicate_architecture_is_not_accepted(self):
        data = json.loads(self.index)
        for rows in [data['manifests'][:1], data['manifests']+data['manifests'][:1]]:
            with self.subTest(rows=len(rows)):
                self.index = json.dumps({'manifests':rows}).encode()
                with patch.object(readback,'run',self.docker), self.assertRaises(ValueError):
                    readback.manifests('fixture/base','1.16.0',digest(self.index))

    def test_child_content_digest_is_actually_checked(self):
        original = self.docker
        def changed(argv):
            raw = original(argv)
            return b'changed child' if '@' in argv[4] else raw
        with patch.object(readback,'run',changed), self.assertRaisesRegex(ValueError,'manifest digest differs'):
            readback.manifests('fixture/base','1.16.0',digest(self.index))

    def test_wrong_elf_or_failed_copy_always_removes_only_the_created_container(self):
        for key, error in [('bad_elf',ValueError),('copy_error',RuntimeError)]:
            with self.subTest(key=key):
                setattr(self,key,True);self.commands=[]
                with patch.object(readback,'run',self.docker),self.assertRaises(error):
                    readback.files('fixture/base','sha256:'+'a'*64,'linux/arm64','/extra')
                self.assertEqual(self.commands[-1][:2],['docker','rm'])
                setattr(self,key,False)

    def test_current_readback_uses_generic_plan_without_publication(self):
        data = yaml.load((SIBLING/'.github/workflows/build.yml').read_text(), Loader=yaml.BaseLoader)
        self.assertIn('verify-pack-*', data['on']['push']['tags'])
        steps = data['jobs']['docker']['steps']
        build = next(s for s in steps if 'build-push' in s.get('uses',''))
        repair = next(s for s in steps if 'pack_release.py copy' in s.get('run',''))
        self.assertEqual(build['if'], "steps.plan.outputs.action == 'build'")
        self.assertEqual(repair['if'], "steps.plan.outputs.action == 'copy'")
        self.assertTrue(any('pack_release.py verify' in s.get('run','') for s in steps))



if __name__ == '__main__':
    unittest.main()
