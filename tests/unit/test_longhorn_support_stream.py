"""Real private-stream/CRC/resource contracts for native diagnostic archives."""
import contextlib
import hashlib
import importlib.util
import io
import os
from pathlib import Path
import subprocess
import tempfile
import tracemalloc
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import zipfile
BUNDLE_PATH='/v1/supportbundles/longhorn-node-238/support-bundle-owned/download'
ROOT=Path(os.environ.get('LONGHORN_SUPPORT_STREAM_TEST_ROOT',Path(__file__).resolve().parents[2]))
s=importlib.util.spec_from_file_location('bundle_stream_contract',ROOT/'tests/helpers/longhorn-v2-observability.py');GATE=importlib.util.module_from_spec(s);s.loader.exec_module(GATE)

class SupportStreamContracts(unittest.TestCase):
    def setUp(self):
        directory=tempfile.TemporaryDirectory();self.addCleanup(directory.cleanup)
        original=tempfile.NamedTemporaryFile
        def private_diagnostic(*args,**kwargs):
            kwargs['dir']=directory.name
            return original(*args,**kwargs)
        patcher=patch.object(GATE.tempfile,'NamedTemporaryFile',private_diagnostic)
        patcher.start();self.addCleanup(patcher.stop)

    def test_real_archive_larger_than_old_ceiling_has_bounded_memory_and_all_crc_checks(self):
        with tempfile.TemporaryFile() as stream:
            with zipfile.ZipFile(stream,'w',compression=zipfile.ZIP_STORED) as archive:
                with archive.open('support/node.log','w') as member:
                    block=b'x'*65536
                    for _ in range(1041):member.write(block)
            stream.seek(0,os.SEEK_END);size=stream.tell();self.assertGreater(size,64*1024**2)
            stream.seek(0);digest=hashlib.file_digest(stream,'sha256').hexdigest()
            tracemalloc.start()
            try:row=GATE.support_archive(stream);peak=tracemalloc.get_traced_memory()[1]
            finally:tracemalloc.stop()
            self.assertEqual(size,row['compressed_bytes']);self.assertEqual(digest,row['sha256'])
            self.assertTrue(row['crc_verified']);self.assertFalse(row['extracted']);self.assertFalse(row['published'])
            self.assertLess(peak,8*1024**2,'archive must not be buffered/base64 encoded as one object')
            # Corrupt actual stored content, leaving the original central CRC intact.
            stream.seek(80);stream.write(b'bad');stream.flush()
            with self.assertRaises((ValueError,zipfile.BadZipFile)):GATE.support_archive(stream)

    def test_expanded_and_compressed_budget_refusals_precede_crc_scan(self):
        stream=io.BytesIO()
        with zipfile.ZipFile(stream,'w',compression=zipfile.ZIP_DEFLATED) as archive:archive.writestr('support/log',b'x'*4096)
        with patch.object(GATE,'SUPPORT_EXPANDED_BYTES',4095), patch.object(zipfile.ZipFile,'testzip') as crc:
            with self.assertRaisesRegex(ValueError,'expanded size'):GATE.support_archive(stream)
            crc.assert_not_called()
        with patch.object(GATE,'SUPPORT_COMPRESSED_BYTES',1),patch.object(zipfile.ZipFile,'testzip') as crc:
            with self.assertRaisesRegex(ValueError,'compressed size'):GATE.support_archive(stream)
            crc.assert_not_called()

    def test_real_subprocess_download_exact_length_rc_disk_refusal_and_private_file_closure(self):
        with tempfile.TemporaryDirectory() as directory:
            base=Path(directory);(base/'kube-bin').mkdir();script=base/'kube-bin/kubectl'
            stream=io.BytesIO()
            with zipfile.ZipFile(stream,'w') as z:z.writestr('support/log',b'private diagnostics')
            data=stream.getvalue();source=base/'source.zip';source.write_bytes(data)
            lab=SimpleNamespace(BASE=base,DIRECTORY=base/'cluster')
            created=[];real_temp=tempfile.TemporaryFile
            def temporary(*a,**kw):
                f=real_temp(*a,**kw);created.append(f);self.assertEqual(0o600,os.fstat(f.fileno()).st_mode&0o777);return f
            for code,expected in ((0,len(data)),(7,len(data)),(0,len(data)+1)):
                script.write_text('#!/usr/bin/env python3\nimport sys\nfrom pathlib import Path\nsys.stdout.buffer.write(Path('+repr(str(source))+').read_bytes())\nraise SystemExit('+str(code)+')\n');script.chmod(0o755)
                with patch.object(GATE.tempfile,'TemporaryFile',temporary),patch.object(GATE.shutil,'disk_usage',return_value=SimpleNamespace(free=2*1024**3)):
                    if code or expected!=len(data):
                        with self.assertRaisesRegex(ValueError,'download failed|received sizes'):GATE.download_support(lab,BUNDLE_PATH,expected)
                    else:
                        row=GATE.download_support(lab,BUNDLE_PATH,expected)
                        self.assertEqual(hashlib.sha256(data).hexdigest(),row['sha256'])
                self.assertTrue(all(f.closed for f in created))
            with patch.object(GATE.shutil,'disk_usage',return_value=SimpleNamespace(free=len(data)+GATE.SUPPORT_DISK_HEADROOM-1)),patch.object(GATE.subprocess,'Popen') as process:
                with self.assertRaisesRegex(ValueError,'disk headroom'):GATE.download_support(lab,BUNDLE_PATH,len(data))
                process.assert_not_called()
            for size in (0,True,GATE.SUPPORT_COMPRESSED_BYTES+1):
                with self.assertRaisesRegex(ValueError,'size exceeds'):GATE.download_support(lab,BUNDLE_PATH,size)

    def test_native_download_uses_authenticated_http_proxy_instead_of_exec_websocket(self):
        import sys
        with tempfile.TemporaryDirectory() as directory:
            base=Path(directory);binpath=base/'kube-bin';binpath.mkdir();source=base/'native.zip'
            with zipfile.ZipFile(source,'w') as archive:archive.writestr('support/private.log',b'private native bundle')
            size=source.stat().st_size;program=binpath/'kubectl'
            expected=['--kubeconfig='+str(base/'kubectl.kubeconfig'),'--request-timeout=330s','get',
                '--raw=/api/v1/namespaces/longhorn-system/services/http:longhorn-frontend:80/proxy'+BUNDLE_PATH]
            program.write_text('#!'+sys.executable+'\nimport sys\nfrom pathlib import Path\nassert sys.argv[1:]=='+repr(expected)+'\nsys.stdout.buffer.write(Path('+repr(str(source))+').read_bytes())\n')
            program.chmod(0o755)
            result=GATE.download_support(SimpleNamespace(BASE=base,DIRECTORY=base),BUNDLE_PATH,size)
            self.assertTrue(result['crc_verified']);self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(),result['sha256'])

    def test_foreign_or_ambiguous_native_path_never_reaches_authenticated_api(self):
        for path in ('http://foreign'+BUNDLE_PATH,BUNDLE_PATH+'?token=x',BUNDLE_PATH+'#x',
                     BUNDLE_PATH.replace('/support-bundle-owned/','/../'),'/v1/volumes'):
            with self.subTest(path=path),patch.object(GATE.shutil,'disk_usage',return_value=SimpleNamespace(free=2*1024**3)),patch.object(GATE.subprocess,'Popen') as process:
                with self.assertRaisesRegex(ValueError,'exact native bundle path'):
                    GATE.download_support(SimpleNamespace(BASE=Path('/fixture'),DIRECTORY=Path('/fixture')),path,128)
                process.assert_not_called()

    def test_download_timeout_kills_child_and_never_validates_partial_archive(self):
        class Child:
            killed=False
            def wait(self,timeout):
                if not self.killed:raise subprocess.TimeoutExpired('fixture',timeout)
                return 137
            def poll(self):return 137 if self.killed else None
            def kill(self):self.killed=True
        child=Child()
        with patch.object(GATE.shutil,'disk_usage',return_value=SimpleNamespace(free=2*1024**3)),patch.object(GATE.subprocess,'Popen',return_value=child),patch.object(GATE.time,'monotonic',side_effect=[0,331]),patch.object(GATE,'support_archive') as crc:
            with self.assertRaisesRegex(ValueError,'time or byte budget'):
                GATE.download_support(SimpleNamespace(BASE=Path('/fixture'),DIRECTORY=Path('/fixture')),BUNDLE_PATH,1)
            self.assertTrue(child.killed);crc.assert_not_called()

    def test_failed_transfer_preserves_exact_private_stderr_without_disclosing_it(self):
        with tempfile.TemporaryDirectory() as directory:
            base=Path(directory);(base/'kube-bin').mkdir();script=base/'kube-bin/kubectl'
            secret='error: transport closed; private signed-token=fixture-secret\n'
            script.write_text('#!/usr/bin/env python3\nimport sys\nsys.stdout.buffer.write(b"partial")\nsys.stderr.write('+repr(secret)+')\nraise SystemExit(1)\n');script.chmod(0o755)
            output=io.StringIO()
            with patch.object(GATE.shutil,'disk_usage',return_value=SimpleNamespace(free=2*1024**3)),contextlib.redirect_stdout(output):
                with self.assertRaisesRegex(ValueError,'download failed'):
                    GATE.download_support(SimpleNamespace(BASE=base,DIRECTORY=base),BUNDLE_PATH,128)
            self.assertNotIn(secret.strip(),output.getvalue())
            row=__import__('json').loads(next(l.split(' ',1)[1] for l in output.getvalue().splitlines() if l.startswith('LONGHORN_SUPPORT_DOWNLOAD_RESULT ')))
            diagnostic=Path(row['private_diagnostic'])
            try:
                self.assertEqual(secret.encode(),diagnostic.read_bytes())
                self.assertEqual(0o600,diagnostic.stat().st_mode&0o777)
                self.assertEqual(hashlib.sha256(secret.encode()).hexdigest(),row['stderr_sha256'])
                self.assertEqual(7,row['received_bytes']);self.assertEqual(1,row['rc'])
            finally:diagnostic.unlink()

if __name__=='__main__':unittest.main()
