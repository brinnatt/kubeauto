"""Large monitoring artifact staging: checksum, identity and cleanup contracts."""
from contextlib import redirect_stdout
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

ROOT=Path(__file__).resolve().parents[2]
spec=importlib.util.spec_from_file_location('monitoring_cache',ROOT/'tests/helpers/longhorn-v1-monitoring-cache.py')
CACHE=importlib.util.module_from_spec(spec);spec.loader.exec_module(CACHE)


class CacheContracts(unittest.TestCase):
    def node(self, root, *, content=b'verified-image-archive', change=None, existing=None):
        machine=root/'machine';machine.write_text('owned-machine')
        digest=root/'digest';digest.write_text(existing or '')
        calls=root/'calls'
        ctr=root/'ctr'
        ctr.write_text('#!'+sys.executable+'\n'+'''import json,pathlib,sys
root=pathlib.Path(__file__).parent
args=sys.argv[1:]
assert args[:3]==['--namespace','k8s.io','images']
if args[3:]==['list']:
 print('REF TYPE DIGEST SIZE PLATFORMS LABELS')
 digest=(root/'digest').read_text()
 if digest:print('registry.talkschool.cn:5000/brinnatt/grafana:13.1.1 OCI '+digest+' 1 linux/amd64 -')
else:
 assert args[3:9]==['import','--local','--base-name','registry.talkschool.cn:5000/brinnatt/grafana','--label','io.cri-containerd.image=managed']
 assert pathlib.Path(args[9]).read_bytes()==b'verified-image-archive'
 (root/'calls').write_text(json.dumps(args))
 (root/'digest').write_text('sha256:owned-manifest')
''');ctr.chmod(0o700)
        request={'action':'import','ref':CACHE.REFERENCE,'manifest':'sha256:owned-manifest',
            'machine_id':'owned-machine','size':len(b'verified-image-archive'),
            'sha256':hashlib.sha256(b'verified-image-archive').hexdigest()}
        if change:request.update(change)
        script=CACHE.node_script().replace("Path('/etc/machine-id')",'Path('+repr(str(machine))+')').replace("'/usr/local/bin/ctr'",repr(str(ctr))).replace("prefix='kubeauto-longhorn-monitoring-cache-'","dir="+repr(str(root))+",prefix='cache-'")
        result=subprocess.run([sys.executable,'-c',script,json.dumps(request)],input=content,capture_output=True)
        self.assertFalse(list(root.glob('cache-*')),result.stderr)
        return result,calls

    def test_binary_archive_is_verified_before_import_and_exact_cli_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            result,calls=self.node(Path(directory))
            self.assertEqual(result.returncode,0,result.stderr)
            row=json.loads(result.stdout);self.assertTrue(row['temporary_files_removed'])
            self.assertEqual(row['sha256'],hashlib.sha256(b'verified-image-archive').hexdigest())
            self.assertTrue(calls.exists())

    def test_corrupt_short_oversize_foreign_machine_or_tag_never_imports(self):
        for kwargs in ({'content':b'corrupt'},{'content':b''},{'content':b'x'*100},
                       {'change':{'machine_id':'foreign'}},{'existing':'sha256:foreign'}):
            with self.subTest(kwargs=kwargs),tempfile.TemporaryDirectory() as directory:
                result,calls=self.node(Path(directory),**kwargs)
                self.assertNotEqual(result.returncode,0)
                self.assertFalse(calls.exists())

    def test_existing_exact_tag_does_not_reimport_and_foreign_probe_is_read_only(self):
        with tempfile.TemporaryDirectory() as directory:
            result,calls=self.node(Path(directory),existing='sha256:owned-manifest')
            self.assertEqual(result.returncode,0,result.stderr);self.assertFalse(calls.exists())
            result,calls=self.node(Path(directory),existing='sha256:foreign',content=b'',change={'action':'probe'})
            self.assertEqual(json.loads(result.stdout),{'manifest':'sha256:foreign'})
            self.assertFalse(calls.exists())

    def lab(self):
        manifest=json.dumps({'schemaVersion':2,'config':{'digest':'config'},'layers':[{'digest':'layer'}]})
        expected='sha256:'+hashlib.sha256(manifest.encode()).hexdigest()
        def execute(argv,**kwargs):
            if argv[1]=='copy':
                destination=argv[-1];path=Path(destination[len('oci-archive:'):].split(':',1)[0]);path.write_bytes(b'archive')
            return SimpleNamespace(stdout=manifest)
        lab=SimpleNamespace(require_lease=MagicMock(),core_identity=MagicMock(),WORKERS=(
            '192.168.122.212','192.168.122.165','192.168.122.238'),owner=MagicMock(return_value={}),
            get=MagicMock(return_value={'status':{'nodeInfo':{'machineID':'original-machine'}}}),
            execute=MagicMock(side_effect=execute),remote=MagicMock(return_value=SimpleNamespace(stdout=json.dumps({'manifest':None}))))
        return lab,expected

    def test_two_transfers_overlap_and_success_waits_for_all_readbacks(self):
        lab,expected=self.lab();cache=MagicMock();barrier=threading.Barrier(2);calls=[]
        def transfer(lab,node,path,request):
            if node in lab.WORKERS[:2]:barrier.wait(timeout=3)
            self.assertEqual(request['manifest'],expected)
            self.assertEqual(request['sha256'],hashlib.sha256(path.read_bytes()).hexdigest())
            self.assertEqual(request['machine_id'],'original-machine');calls.append(node)
            return {'verified':True}
        with patch.object(CACHE,'transfer',side_effect=transfer),redirect_stdout(io.StringIO()) as output:
            CACHE.stage(lab,cache)
        self.assertEqual(set(calls),set(lab.WORKERS));cache.assert_called_once_with(lab)
        self.assertIn(CACHE.MARKER+'_PASS nodes=3 imported=3',output.getvalue())
        self.assertEqual(lab.core_identity.call_count,2)

    def test_all_cached_reuses_without_export_and_unknown_tag_blocks_before_export(self):
        for value in ('expected','sha256:foreign'):
            lab,expected=self.lab();lab.remote.return_value.stdout=json.dumps({'manifest':expected if value=='expected' else value})
            with patch.object(CACHE,'transfer') as transfer,redirect_stdout(io.StringIO()) as output:
                if value=='expected':CACHE.stage(lab,MagicMock())
                else:
                    with self.assertRaisesRegex(ValueError,'foreign cached'):CACHE.stage(lab,MagicMock())
            transfer.assert_not_called()
            self.assertFalse(any(c.args[0][1]=='copy' for c in lab.execute.call_args_list))
            if value!='expected':self.assertNotIn(CACHE.MARKER+'_PASS',output.getvalue())

    def test_first_failed_transfer_never_claims_success(self):
        lab,_=self.lab()
        with patch.object(CACHE,'transfer',side_effect=ValueError('checksum')),redirect_stdout(io.StringIO()) as output:
            with self.assertRaisesRegex(ValueError,'checksum'):CACHE.stage(lab,MagicMock())
        self.assertNotIn(CACHE.MARKER+'_PASS',output.getvalue())

    def test_fixed_cli_path_is_cache_only_and_precedes_relay_start(self):
        shell=(ROOT/'tests/helpers/longhorn-v1-observability.sh').read_text()
        focus=shell.split('if [[ "$mode" == observability-image-cache ]]; then\n  phase=',1)[1].split('\nfi\n',1)[0]
        self.assertIn('"$DRIVER" image-cache',focus);self.assertIn('exit 0',focus)
        self.assertNotIn('relay-maintain',focus)
        runner=(ROOT/'tests/helpers/longhorn-runner.sh').read_text()
        p=subprocess.run(['bash','-c','source "$1";longhorn_launch() { printf "%s\\n" "$@"; };longhorn_dispatch --longhorn-observability-image-cache','test',str(ROOT/'tests/helpers/longhorn-runner.sh')],text=True,capture_output=True,check=True)
        self.assertEqual(p.stdout.splitlines(),['observability-image-cache','LONGHORN_OBSERVABILITY_IMAGE_CACHE_PASS'])
        self.assertIn('observability-image-cache|',runner)
        self.assertIn('observability-image-cache|',(ROOT/'tests/helpers/longhorn-live.sh').read_text())
        self.assertIn('tests.unit.test_longhorn_v1_monitoring_cache',(ROOT/'tests/helpers/longhorn-static.sh').read_text())


if __name__=='__main__':unittest.main()
