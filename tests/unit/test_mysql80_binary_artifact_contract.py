"""No foreign fallback, wrong ABI, partial archive or premature live launch."""
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location('binary80_artifacts', ROOT/'tests/helpers/mysql80-binary-artifacts.py')
GATE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(GATE)


def records():
    return {name+'/'+version: dict(version=version, os=os_version, architecture='x86_64',
                                   url='https://mirrors.huaweicloud.com/mysql/'+version+'.tar.xz',
                                   sha256='a'*64, checksum_source='fixture upstream checksum',
                                   format='tar.xz', abi='glibc2.28')
            for name, os_version in GATE.PROFILES.items() for version in GATE.VERSIONS}


class BinaryArtifactContracts(unittest.TestCase):
    def test_exact_matrix_versions_and_abi_are_required(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'sources.json'
            data=records()
            def save(): path.write_text(json.dumps(dict(artifacts=data)))
            save(); self.assertEqual(len(GATE.catalog(path)), 4)
            data['rocky8/8.0.46']['abi']='el9'; save()
            with self.assertRaisesRegex(ValueError, 'ABI_REFUSED'):
                GATE.catalog(path)
            data['rocky8/8.0.46']['abi']='glibc2.28'
            del data['rocky9/8.0.33']; save()
            with self.assertRaisesRegex(ValueError, 'SOURCE_SET_INCOMPLETE'):
                GATE.catalog(path)

    def test_foreign_sources_redirects_and_credentials_are_rejected(self):
        for url in ('https://cdn.mysql.com/archive.tar.xz', 'http://mirrors.huaweicloud.com/x',
                    'https://mirrors.huaweicloud.com.evil.example/x',
                    'https://user:secret@mirrors.huaweicloud.com/x',
                    'https://mirrors.huaweicloud.com:8443/x'):
            with self.subTest(url=url), self.assertRaisesRegex(ValueError, 'DOMESTIC_SOURCE_REFUSED'):
                GATE.domestic_url(url)
        redirect=GATE.DomesticRedirect()
        with self.assertRaisesRegex(ValueError, 'DOMESTIC_SOURCE_REFUSED'):
            redirect.redirect_request(None, None, 302, 'redirect', {}, 'https://cdn.mysql.com/x')

    def test_failed_checksum_preserves_existing_archive_and_removes_partial(self):
        with tempfile.TemporaryDirectory() as directory:
            cache=Path(directory); record=next(iter(records().values()))
            target=cache/(record['sha256']+'.tar.xz'); target.write_bytes(b'previous file')
            response=io.BytesIO(b'wrong download')
            response.geturl=lambda: record['url']
            with mock.patch.object(GATE.urllib.request, 'build_opener') as opener:
                opener.return_value.open.return_value=response
                with self.assertRaisesRegex(ValueError, 'DOWNLOAD_CHECKSUM_REFUSED'):
                    GATE.prepare({'fixture': record}, cache)
            self.assertEqual(target.read_bytes(), b'previous file')
            self.assertFalse(list(cache.glob('*.partial')))

    def test_verified_archive_is_reused_without_network(self):
        with tempfile.TemporaryDirectory() as directory:
            record=next(iter(records().values())); record['sha256']=hashlib.sha256(b'verified').hexdigest()
            (Path(directory)/(record['sha256']+'.tar.xz')).write_bytes(b'verified')
            with mock.patch.object(GATE.urllib.request, 'build_opener') as opener:
                GATE.prepare({'fixture': record}, directory)
                opener.return_value.open.assert_not_called()

    def test_real_launcher_refuses_missing_sources_before_ssh(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            for name in ('tests/run_tools_regression.sh', 'tests/helpers/mysql80-binary-artifacts.py'):
                target=root/name; target.parent.mkdir(parents=True, exist_ok=True); shutil.copy2(ROOT/name,target)
            result=subprocess.run(['bash',root/'tests/run_tools_regression.sh','--mysql-backup-live'],
                                  env=dict(os.environ,PYTHON=sys.executable,MYSQL_BACKUP_STAGE='binary80-full'),
                                  capture_output=True,text=True)
            self.assertNotEqual(result.returncode,0)
            self.assertIn('BINARY80_SOURCES_MISSING',result.stderr)
            self.assertNotIn('ssh:',result.stderr)

    def test_explained_example_is_valid_json_with_real_values(self):
        data=json.loads((ROOT/'tools/mysqltools/mybackup.conf.example').read_text())
        self.assertEqual(data['mysql_container'],'')
        self.assertEqual(data['mysql_topology'],'standalone')
        for name in ('mysql_container','mysql_service','mysql_topology','mysql_binlog_dir','mysql_binlog_index'):
            self.assertTrue(data['_comment_'+name])


if __name__=='__main__':
    unittest.main()
