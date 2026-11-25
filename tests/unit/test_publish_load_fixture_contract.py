"""Do not lose foreign SSH identities or accept a false successful image load."""
import importlib.util
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[2]

def load(name,path):
    spec=importlib.util.spec_from_file_location(name,ROOT/path)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);return module

FIXTURE=load('publish_fixture','tests/helpers/publish-load-fixture.py')
GATE=load('publish_gate','tests/helpers/tools-execution-boundary-live.py')

class PublishFixtureContracts(unittest.TestCase):
    def test_key_handoff_preserves_foreign_bytes_and_concurrent_additions(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'keys'
            original='ssh-ed25519 foreign existing\n\n'
            path.write_text(original)
            own='ssh-ed25519 fixture tools-publish-owned'
            def apply(remove=False):
                return subprocess.run([sys.executable,'-B','-c',GATE.authorized_key_update(own,remove,str(path))],capture_output=True)
            self.assertEqual(apply().returncode,0)
            self.assertEqual(path.read_text(),original+own+'\n')
            self.assertNotEqual(apply().returncode,0)
            with path.open('a') as out:out.write('ssh-ed25519 concurrent third-party\n')
            self.assertEqual(apply(True).returncode,0)
            self.assertEqual(path.read_text(),original+'ssh-ed25519 concurrent third-party\n')
            path.write_text('foreign missing final newline')
            self.assertNotEqual(apply().returncode,0)
            self.assertEqual(path.read_text(),'foreign missing final newline')

    def test_first_failed_load_requires_nonzero_and_owning_error(self):
        FIXTURE.assert_failed_load(subprocess.CompletedProcess([],1,'镜像加载失败',''))
        for rc,text in ((0,'镜像加载失败'),(1,'Permission denied')):
            with self.assertRaises(ValueError):FIXTURE.assert_failed_load(subprocess.CompletedProcess([],rc,text,''))

if __name__=='__main__':unittest.main()
