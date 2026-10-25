"""Review-only bridge contracts; apply alongside the full43 patch."""
import importlib.util
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch

ROOT=Path(__file__).resolve().parents[2]
HELPERS=ROOT/'tests/helpers'
def load(path):
    spec=importlib.util.spec_from_file_location('full51_'+path.stem,path)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);return module
F=load(HELPERS/'longhorn-full-evidence.py')
M=load(ROOT/'tests/helpers/longhorn-v2-maintenance.py')

class BridgeContracts(unittest.TestCase):
    def fixture(self,directory):
        base=Path(directory);work=base/'full';work.mkdir(mode=0o700);F.BASE=base
        def ref(name,text='0\n'):
            path=base/name;path.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
            path.write_text(text);path.chmod(0o600);return F.reference(path)
        parents={}
        modes=('v2-aio-install-qualification','v1-operations-v2-platform-install','v2-interrupt-qualification','v2-disk-lifecycle','v2-failure','v2-middleware')
        for mode in modes:
            parent=ref('accepted/'+mode+'.json','{}')
            row={'status':'pass','rc':0,'failure_markers':0,'version':'1.12.1',
                'frozen_inputs':{'product':'sha'},'receipt_refs':{key:ref('raw/'+mode+'/parent.'+key) for key in ('log','exit','finalized')},
                'nested_receipts':[],'baseline_identity':{'run_id':'run','cluster_uid':'core','node_uids':{n:n+'uid' for n in M.NODES}}}
            parents[mode]=(base/parent['path'],row,[])
        row=parents['v1-operations-v2-platform-install'][1]
        for nested in ('v2-runtime-resources','v2-coexist-qualification','v2-maintenance-drain-qualification'):
            for suffix in ('gate.log','owning.exit','owning.finalized'):
                row['nested_receipts'].append({'original':'/var/tmp/original/'+nested+'/'+suffix,
                    'copy':ref('raw/batch/'+nested+'/'+suffix)})
        row=parents['v2-failure'][1]
        for suffix in ('log','exit','finalized'):
            row['nested_receipts'].append({'original':'/var/tmp/original/worker-reboot.'+suffix,'copy':ref('raw/failure/worker-reboot.'+suffix)})
        parents['v2-disk-lifecycle'][1]['nested_receipts'].append({'original':'/original/completed.json','copy':ref('raw/disk/completed-journal.json','{}')})
        return base,work,parents

    def test_full_uses_same_finite_consumer_and_original_receipts(self):
        with tempfile.TemporaryDirectory() as d:
            base,work,parents=self.fixture(d);validate=Mock(return_value={'signed_scope':'v2-maintenance'})
            gate=SimpleNamespace(SPECS=M.SPECS,validate_maintenance51=validate)
            with patch.object(F,'work_directory',return_value=work),patch.object(F,'accepted_stage',side_effect=lambda mode:parents[mode]),patch.object(F,'load',return_value=gate):
                self.assertEqual(F.maintenance51(),{'signed_scope':'v2-maintenance'})
            validate.assert_called_once();actual_base,materials=validate.call_args.args
            self.assertEqual(actual_base,base);self.assertEqual(set(materials),set(M.SCOPES))
            for scope in M.SCOPES:
                row=json.loads((base/materials[scope]['evidence']['path']).read_text())
                self.assertIn('original_parent',row)
                self.assertEqual(row['baseline_identity']['cluster_uid'],'core')
                self.assertEqual(row['raw_logs'][materials[scope]['log']['path']],materials[scope]['log']['sha256'])
            self.assertTrue(materials['disk']['artifacts']['completed-journal']['path'].endswith('/completed-journal.json'))
            self.assertTrue(materials['worker-reboot']['log']['path'].endswith('/worker-reboot.log'))
            self.assertIn('v2-middleware',F.CASE_STAGES[51]);self.assertIn('v2-middleware',F.MODES)

    def test_missing_middleware_or_original_journal_blocks_same_consumer(self):
        for defect in ('middleware','journal'):
            with self.subTest(defect=defect),tempfile.TemporaryDirectory() as d:
                _,work,parents=self.fixture(d);validate=Mock()
                if defect=='middleware':parents.pop('v2-middleware')
                else:parents['v2-disk-lifecycle'][1]['nested_receipts']=[]
                gate=SimpleNamespace(SPECS=M.SPECS,validate_maintenance51=validate)
                with patch.object(F,'work_directory',return_value=work),patch.object(F,'accepted_stage',side_effect=lambda mode:parents[mode]),patch.object(F,'load',return_value=gate):
                    with self.assertRaises((ValueError,KeyError)):F.maintenance51()
                validate.assert_not_called()

if __name__=='__main__':unittest.main()
