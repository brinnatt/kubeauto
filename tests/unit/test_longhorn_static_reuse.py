import hashlib,importlib.util,json,os,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[2]
spec=importlib.util.spec_from_file_location('static_reuse',ROOT/'tests/helpers/longhorn-static-reuse.py');R=importlib.util.module_from_spec(spec);spec.loader.exec_module(R)
class StaticReuseContracts(unittest.TestCase):
 def test_reuse_requires_complete_original_result_same_inputs_log_and_scoped_cleanup(self):
  for defect in ('none','input','rc','failure','marker','hash','scoped','duplicate','symlink'):
   with self.subTest(defect=defect),tempfile.TemporaryDirectory() as directory:
    root=Path(directory);raw=b'LONGHORN_STATIC_SCOPED_CLEAN_PASS\nLONGHORN_STATIC_PASS\n'
    if defect=='failure':raw+=b'ERROR: original failure\n'
    if defect=='scoped':raw=raw.replace(b'LONGHORN_STATIC_SCOPED_CLEAN_PASS\n',b'')
    if defect=='duplicate':raw+=b'LONGHORN_STATIC_PASS\n'
    (root/'gate.log').write_bytes(raw)
    (root/'static-inputs.json').write_text(json.dumps({'fingerprint':'other' if defect=='input' else 'same'}))
    status={'rc':1 if defect=='rc' else 0,'failure_markers':0,'marker_present':defect!='marker','marker':'LONGHORN_STATIC_PASS','log_sha256':'other' if defect=='hash' else hashlib.sha256(raw).hexdigest()}
    (root/'status.json').write_text(json.dumps(status))
    if defect=='symlink':(root/'status.json').rename(root/'other');(root/'status.json').symlink_to(root/'other')
    if defect=='none':R.verify(root,'same')
    else:
     with self.assertRaises(ValueError):R.verify(root,'same')
 def test_fingerprint_changes_with_sources_tools_packages_and_environment_without_persisting_secret(self):
  from types import SimpleNamespace
  with tempfile.TemporaryDirectory() as directory:
   root=Path(directory)/'kubeauto';root.mkdir();tool=Path(directory)/'tool';tool.write_text('binary')
   for name in ('kubeauto','kubeauto-dockerfile','kubeauto-k8s-bin-dockerfile','kubeauto-ext-bin-dockerfile','kubeauto-ext-bin-sp1-dockerfile','kubeauto-ext-images-dockerfile'):
    repo=Path(directory)/name;repo.mkdir(exist_ok=True);(repo/'input').write_text(name)
   result=SimpleNamespace(stdout=b'input\0');package=SimpleNamespace(metadata={'Name':'dependency'},version='1',read_text=lambda _: 'record')
   with patch.object(R.subprocess,'run',return_value=result),patch.object(R.shutil,'which',return_value=str(tool)),patch.object(R.importlib.metadata,'distributions',return_value=[package]),patch.dict(os.environ,{'TEST_SECRET':'never-record-this'}):
    original=R.fingerprint(root);self.assertEqual(original,R.fingerprint(root));self.assertNotIn('never-record',original)
    for mutate,restore in ((lambda:(root/'input').write_text('changed'),lambda:(root/'input').write_text('kubeauto')),(lambda:tool.write_text('changed'),lambda:tool.write_text('binary')),(lambda:setattr(package,'version','2'),lambda:setattr(package,'version','1'))):
     mutate();self.assertNotEqual(original,R.fingerprint(root));restore();self.assertEqual(original,R.fingerprint(root))
    with patch.dict(os.environ,{'TEST_SECRET':'changed'}):self.assertNotEqual(original,R.fingerprint(root))
 def test_final_full_path_never_consumes_static_reuse(self):
  full=(ROOT/'tests/helpers/longhorn-full.sh').read_text();self.assertIn('static) exec bash "$BASE/tests/helpers/longhorn-static.sh"',full)
  self.assertNotIn('static-reuse',full)
 def test_fixed_launch_reuse_is_scoped_and_standalone_static_still_runs_fresh(self):
  runner=(ROOT/'tests/helpers/longhorn-runner.sh').read_text()
  self.assertIn('local LONGHORN_STATIC_REUSE_ALLOWED=yes',runner)
  self.assertIn('"${LONGHORN_STATIC_REUSE_ALLOWED:-no}" == yes',runner)
  self.assertIn('static-inputs.json', (ROOT/'tests/helpers/longhorn-static-reuse.py').read_text())
  self.assertLess(runner.index('reuse --root'),runner.index('begin --root'))

 def test_real_cli_begin_finish_reuse_rejects_changed_environment_and_failed_original(self):
  import sys
  from contextlib import redirect_stdout,redirect_stderr
  import io
  with tempfile.TemporaryDirectory() as temporary:
   root=Path(temporary);directory=root/'logs/longhorn-cases/static-qualification';directory.mkdir(parents=True)
   def call(action,value='same'):
    argv=['reuse',action,'--root',str(root),'--directory',str(directory)]
    with patch.object(sys,'argv',argv),patch.object(R,'fingerprint',return_value=value),redirect_stdout(io.StringIO()),redirect_stderr(io.StringIO()):R.main()
   call('begin')
   raw=b'LONGHORN_STATIC_SCOPED_CLEAN_PASS\nLONGHORN_STATIC_PASS\n';(directory/'gate.log').write_bytes(raw)
   status={'rc':0,'failure_markers':0,'marker_present':True,'marker':'LONGHORN_STATIC_PASS','log_sha256':hashlib.sha256(raw).hexdigest()}
   (directory/'status.json').write_text(json.dumps(status));call('finish');call('reuse')
   with self.assertRaises(ValueError):call('reuse','changed')
   status['rc']=1;(directory/'status.json').write_text(json.dumps(status))
   with self.assertRaises(ValueError):call('reuse')
