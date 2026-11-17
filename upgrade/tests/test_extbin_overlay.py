"""Exercise the real increment-only downloader with deterministic asset streams."""
from copy import deepcopy
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import struct
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[2]
SIBLING = ROOT.parent/'kubeauto-ext-bin-dockerfile'
SPEC = importlib.util.spec_from_file_location('extbin_overlay', SIBLING/'upgrade/prepare_overlay.py')
overlay = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(overlay)
READBACK_SPEC = importlib.util.spec_from_file_location('extbin_overlay_verify', SIBLING/'upgrade/verify_overlay.py')
readback = importlib.util.module_from_spec(READBACK_SPEC)
with patch.object(sys, 'path', [str(SIBLING/'upgrade'), *sys.path]):
    READBACK_SPEC.loader.exec_module(readback)


def elf(architecture):
    return b'\x7fELF\x02\x01'+b'\0'*12+struct.pack('<H', {'amd64':62,'arm64':183}[architecture])


class OverlayContracts(unittest.TestCase):
    def setUp(self):
        self.plan = json.loads((SIBLING/'upgrade/overlay-v1.34.13.json').read_text())
        self.payloads = {}
        for item in self.plan['assets']:
            raw = elf(item['architecture'])
            if set(item['files']) != {''}:
                stream = io.BytesIO()
                with tarfile.open(fileobj=stream, mode='w:gz') as archive:
                    for name in item['files']:
                        member = tarfile.TarInfo(name);member.size=len(raw)
                        archive.addfile(member, io.BytesIO(raw))
                raw = stream.getvalue()
            self.payloads[item['url']] = raw
            item.update(sha256=hashlib.sha256(raw).hexdigest(), bytes=len(raw))

    def response(self, url, **kwargs):
        return io.BytesIO(self.payloads[url])

    def test_real_prepare_checks_both_architectures_bridges_and_docker_checksums(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(overlay.urllib.request,'urlopen',self.response):
            out = Path(temporary)/'output';overlay.prepare(self.plan,out)
            for arch in ['amd64','arm64']:
                root=out/arch/'extra'
                for name in ['etcd','etcdctl','etcdutl','cri-dockerd','calicoctl',
                             'containerd-bin/containerd','containerd-bin/ctr','containerd-bin/runc',
                             'upgrade/containerd-2.2.10/containerd','upgrade/containerd-2.2.10/runc',
                             'upgrade/calicoctl-v3.31.7']:
                    self.assertEqual((root/name).read_bytes(),elf(arch))
                    self.assertEqual((root/name).stat().st_mode&0o777,0o755)
                docker = (root/'docker-runtime-artifacts.sha256').read_text()
                self.assertIn(self.plan['retained_docker_checksums'][arch]['docker-compose']+'  docker-compose\n',docker)
                self.assertIn(hashlib.sha256(elf(arch)).hexdigest()+'  cri-dockerd\n',docker)
                provenance=json.loads((out/arch/'provenance.json').read_text())
                self.assertEqual(provenance['started_binaries'],[])
                self.assertTrue(all((root/n).read_bytes() for n in provenance['files']))
                self.assertFalse((root/'cfssl').exists())
                self.assertFalse((root/'cephadm').exists())

    def test_missing_architecture_bridge_duplicate_or_noncanonical_path_refuses_before_network(self):
        plans=[]
        p=deepcopy(self.plan);p['assets'].pop();plans.append(p)
        p=deepcopy(self.plan);p['assets'].append(deepcopy(p['assets'][0]));plans.append(p)
        for destination in ['../foreign','/foreign','a/../foreign','a//foreign']:
            p=deepcopy(self.plan);p['assets'][0]['files']={'binary':destination};plans.append(p)
        for plan in plans:
            with tempfile.TemporaryDirectory() as temporary, patch.object(overlay.urllib.request,'urlopen') as network:
                out=Path(temporary)/'output'
                with self.assertRaises(ValueError):overlay.prepare(plan,out)
                self.assertFalse(out.exists());network.assert_not_called()

    def test_download_checksum_or_truncation_never_publishes_an_accepted_archive(self):
        item=self.plan['assets'][0]
        for raw in [b'wrong', self.payloads[item['url']][:-1], self.payloads[item['url']]+b'overflow']:
            with tempfile.TemporaryDirectory() as temporary, patch.object(overlay.urllib.request,'urlopen',return_value=io.BytesIO(raw)):
                root=Path(temporary)
                with self.assertRaises(ValueError):overlay.download(item,root)
                self.assertEqual(list(root.iterdir()),[])

    def test_linked_duplicate_missing_or_wrong_arch_member_does_not_write_target(self):
        item=self.plan['assets'][0];name=next(iter(item['files']))
        for kind in ['symlink','hardlink','duplicate','missing','wrong-arch']:
            with self.subTest(kind=kind),tempfile.TemporaryDirectory() as temporary:
                root=Path(temporary);archive=root/'archive.tgz'
                with tarfile.open(archive,'w:gz') as tar:
                    member=tarfile.TarInfo('wrong' if kind=='missing' else name)
                    if kind in ['symlink','hardlink']:
                        member.type=tarfile.SYMTYPE if kind=='symlink' else tarfile.LNKTYPE
                        member.linkname='/foreign';tar.addfile(member)
                    else:
                        raw=elf('arm64' if item['architecture']=='amd64' else 'amd64')
                        member.size=len(raw);tar.addfile(member,io.BytesIO(raw))
                        if kind=='duplicate':tar.addfile(member,io.BytesIO(raw))
                with self.assertRaises(ValueError):overlay.extract(item,archive,root/'output')
                self.assertFalse((root/'output').exists())

    def test_existing_output_refuses_without_network_or_modifying_foreign_files(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(overlay.urllib.request,'urlopen') as network:
            out=Path(temporary);(out/'foreign').write_text('keep')
            with self.assertRaises(FileExistsError):overlay.prepare(self.plan,out)
            self.assertEqual((out/'foreign').read_text(),'keep');network.assert_not_called()

    def test_dockerfile_can_only_overlay_the_verified_base_and_has_no_compile_stage(self):
        text=(SIBLING/'upgrade/Dockerfile.v1.34.13').read_text()
        self.assertEqual(text.count('\nFROM '),1)
        self.assertIn('FROM brinnatt/kubeauto-ext-bin@'+self.plan['base']['digest'],text)
        self.assertIn('COPY upgrade/output/${TARGETARCH}/extra/ /extra/',text)
        self.assertNotIn('\nRUN ',text)
        self.assertIn('ENV EXT_BIN_VER=1.17.0',text)

    def test_real_version_probe_requires_container_exit_zero_and_cleans_on_failure(self):
        for exit_code, text in [('0',b'Client Version:    v3.32.4'),
                                ('3',b'Client Version:    v3.32.4'),('0',b'wrong version')]:
            with self.subTest(exit_code=exit_code, text=text):
                commands=[]
                def docker(argv):
                    commands.append(argv)
                    if argv[1]=='create':
                        self.assertIn('--network',argv);self.assertIn('none',argv)
                        return b'a'*64
                    if argv[1]=='start':return text
                    if argv[1]=='inspect':return exit_code.encode()
                    if argv[1]=='rm':return b''
                    self.fail(str(argv))
                with patch.object(readback.base,'run',docker):
                    if exit_code=='0' and text.startswith(b'Client'):
                        result=readback.version('fixture@sha256:'+'a'*64,'linux/arm64',
                            'calicoctl',['version','--client'],'v3.32.4')
                        self.assertEqual(result['rc'],0)
                    else:
                        with self.assertRaises(ValueError):
                            readback.version('fixture@sha256:'+'a'*64,'linux/arm64',
                                'calicoctl',['version','--client'],'v3.32.4')
                self.assertEqual(commands[-1],['docker','rm','--force','a'*64])

    def test_real_readback_checks_every_retained_file_and_both_architecture_cli_sets(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)
            identities={'linux/amd64':'sha256:'+'a'*64,'linux/arm64':'sha256:'+'b'*64}
            expected={}
            for arch in ['amd64','arm64']:
                directory=root/arch/'extra';directory.mkdir(parents=True)
                (directory/'etcd').write_bytes(elf(arch));(directory/'etcd').chmod(0o755)
                expected['linux/'+arch]=deepcopy(readback.expected_files(self.plan,root,arch))
            with patch.object(readback.base,'manifests',return_value=identities) as manifests, \
                 patch.object(readback.base,'files',side_effect=lambda r,d,p,f:expected[p]) as files, \
                 patch.object(readback,'version',return_value={'rc':0}) as version:
                receipt=readback.verify(self.plan,'sha256:'+'c'*64,root)
                self.assertEqual(receipt['marker'],'EXT_BIN_INCREMENTAL_DUAL_READBACK_PASS')
                self.assertEqual(manifests.call_count,2);self.assertEqual(files.call_count,2)
                self.assertEqual(version.call_count,22)
                self.assertEqual({c.args[1] for c in version.call_args_list},set(identities))
                version.reset_mock()
                expected['linux/amd64']['nginx']['sha256']='f'*64
                with self.assertRaisesRegex(ValueError,'retained files differ'):
                    readback.verify(self.plan,'sha256:'+'c'*64,root)
                version.assert_not_called()

    def test_actual_workflow_is_manual_selected_one_image_and_qualifies_before_publish(self):
        import ast
        import subprocess
        import yaml
        path=SIBLING/'.github/workflows/upgrade-v1.34.13.yml'
        data=yaml.safe_load(path.read_text());event=data.get('on',data.get(True))
        self.assertEqual(event, {'push': {'tags': ['1.17.0']}})
        self.assertEqual(data['jobs']['selected']['if'], 'github.event.deleted == false')
        steps=data['jobs']['selected']['steps']
        build=next(s for s in steps if s.get('uses')=='docker/build-push-action@v6')
        self.assertNotIn('if', build)
        self.assertEqual(build['with']['file'],'upgrade/Dockerfile.v1.34.13')
        self.assertEqual(set(build['with']['tags'].split()),{
            'hub.talkedu.cn/kubeauto/kubeauto-ext-bin:1.17.0','brinnatt/kubeauto-ext-bin:1.17.0'})
        prepare=next(s for s in steps if 'upgrade/prepare_overlay.py --output' in s.get('run',''))
        self.assertLess(steps.index(prepare),steps.index(build))
        verify=next(s for s in steps if 'upgrade/verify_overlay.py --digest' in s.get('run',''))
        self.assertGreater(steps.index(verify),steps.index(build))
        self.assertIn('readback.status',verify['run'])
        for step in steps:
            script=step.get('run')
            if script:
                result=subprocess.run(['bash','-n'],input=script,text=True,capture_output=True)
                self.assertEqual(result.returncode,0,result.stderr)
                if "python3 - <<'PY'\n" in script:
                    ast.parse(script.split("python3 - <<'PY'\n",1)[1].rsplit('\nPY',1)[0])


if __name__=='__main__':
    unittest.main()
