"""Exact runtime UID continuation; all fixtures and locks remain in temp."""
import ast
import copy
from contextlib import redirect_stdout
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[2]
HERE = ROOT/'tests/helpers'

def module(path, name):
    spec=importlib.util.spec_from_file_location(name,path); value=importlib.util.module_from_spec(spec); spec.loader.exec_module(value); return value

M = module(HERE/'longhorn-v2-runtime-continuation.py','continuation_contract')
LEGACY = module(ROOT/'tests/unit/test_longhorn_v2_upgrade_baseline.py','old_baseline_contract')
NEW = module(HERE/'longhorn-v2-upgrade-baseline.py','new_baseline_contract')

REAL_LOAD = NEW.load

def consumer_module(name):
    return M if name == 'longhorn-v2-runtime-continuation.py' else REAL_LOAD(name)

READERS=module(ROOT/'tests/unit/test_longhorn_v2_runtime_reader.py','runtime_readers_fixtures')
M=READERS.M
class RuntimeConsumerContracts(unittest.TestCase):
    fixture=READERS.ContinuationContracts.fixture
    contexts=READERS.ContinuationContracts.contexts
    read=READERS.ContinuationContracts.read
    refresh=READERS.ContinuationContracts.refresh
    def test_explicit_seam_binds_original_adoption_sha_and_default_keeps_strict_uid_guard(self):
        with tempfile.TemporaryDirectory() as d:
            base,row,path,_,_=self.fixture(d)
            with patch.object(NEW,'BASE',base),patch.dict(os.environ,{},clear=True),self.assertRaises(ValueError): NEW.bind_previous(row['baseline'])
            with patch.object(NEW,'BASE',base),patch.object(M,'BASE',base),patch.dict(os.environ,{M.ENV:str(path)}),patch.object(NEW,'load',side_effect=consumer_module),patch.object(M,'load',return_value=SimpleNamespace(verify_log=Mock())):
                self.assertEqual({**row['previous_evidence'],'runtime_continuation':M.reference(path)},NEW.bind_previous(row['baseline']))

    def test_seam_default_keeps_exact_snapshot_while_explicit_path_only_relaxes_ephemeral_uids(self):
        with tempfile.TemporaryDirectory() as d:
            base,row,path,_,_=self.fixture(d);baseline=row['baseline'];current=copy.deepcopy(baseline)
            for manager in current['managers'].values():manager['pod_uid']+='-new'
            with patch.dict(os.environ,{},clear=True),patch.object(NEW,'capture',return_value=current):
                self.assertEqual(current,NEW.current_baseline(SimpleNamespace(),baseline))
            with patch.object(NEW,'BASE',base),patch.dict(os.environ,{M.ENV:str(path)}),patch.object(NEW,'capture',return_value=current),patch.object(NEW,'load',side_effect=consumer_module),patch.object(M,'load',return_value=SimpleNamespace(verify_log=Mock())):
                self.assertEqual(baseline,NEW.current_baseline(SimpleNamespace(),baseline))

    def completed_continuation(self, directory):
        base,row,path,ref,write=self.fixture(directory)
        evidence=json.loads((base/NEW.EVIDENCE).read_text());transition=evidence['transition']
        transition['before']=row['baseline']
        transition['previous_evidence']={**row['previous_evidence'],'runtime_continuation':ref(path)}
        prep_path=base/transition['preparation']['path'];prep=json.loads(prep_path.read_text())
        prep['baseline_before']=transition['before'];prep['previous_evidence']=transition['previous_evidence']
        self.refresh(prep_path,prep);transition['preparation']=ref(prep_path)
        raw_path=base/evidence['receipt_refs']['log']['path'];lines=raw_path.read_text().splitlines()
        prefix='LONGHORN_V2_UPGRADE_BASELINE_TRANSITION '
        lines=[prefix+json.dumps(transition,sort_keys=True) if line.startswith(prefix) else line for line in lines]
        raw_path.write_text('\n'.join(lines)+'\n');evidence['receipt_refs']['log']=ref(raw_path)
        evidence['frozen_inputs']={name:ref(base/name)['sha256'] for name in NEW.INPUTS}
        self.refresh(base/NEW.EVIDENCE,evidence)
        lab_dir=base/'clusters/longhorn-v2-gate';(lab_dir/'yml/longhorn').mkdir(parents=True)
        write('clusters/longhorn-v2-gate/yml/longhorn/block-disks.json',transition['after']['native_block_receipt'])
        after=transition['after'];actual=copy.deepcopy(after)
        for manager in actual['managers'].values():manager['pod_uid']+='-healthy-rebuilt'
        def get(kind,name='',namespace=''):
            if kind=='nodes.longhorn.io':
                node=next(r for r in after['native_block_receipt']['nodes'].values() if r['name']==name)
                return {'metadata':{'uid':node['longhorn_node_uid']},'status':{'diskStatus':{n:{'diskUUID':d['disk_uuid']} for n,d in node['disks'].items()}}}
            return {'items':[]}
        lab=SimpleNamespace(DIRECTORY=lab_dir,owner=Mock(return_value=after['baseline_identity']),get=Mock(side_effect=get))
        return base,row,path,evidence,after,actual,lab,ref

    def test_completed_52_with_all_environment_cleared_remains_accepted_by_original_live_install_and_offline49(self):
        with tempfile.TemporaryDirectory() as d:
            base,row,path,evidence,after,actual,lab,_=self.completed_continuation(d)
            install=module(HERE/'longhorn-v2-install.py','completed_install_contract')
            with patch.dict(os.environ,{},clear=True),patch.object(NEW,'BASE',base),patch.object(NEW,'load',side_effect=consumer_module),patch.object(NEW,'capture',return_value=actual),patch.object(M,'load',return_value=SimpleNamespace(verify_log=Mock())),patch.object(install,'BASE',base),patch.object(install,'load',return_value=NEW),patch.object(install,'health') as health,redirect_stdout(io.StringIO()):
                self.assertEqual(after,NEW.accepted(lab))
                offline,baseline,_=NEW.accepted_evidence();self.assertEqual(evidence,offline);self.assertEqual(after,baseline)
                install.volume_baseline(lab);health.assert_called_once_with(lab)

    def test_completed_transition_rejects_tampered_runtime_ref_or_original_scope_raw_without_any_environment(self):
        for defect in ('ref','bytes','scope-raw'):
            with self.subTest(defect=defect),tempfile.TemporaryDirectory() as d:
                base,row,path,evidence,after,actual,lab,ref=self.completed_continuation(d)
                if defect=='ref':evidence['transition']['previous_evidence']['runtime_continuation']['sha256']='0'*64
                if defect=='bytes':path.write_text(path.read_text()+' ')
                if defect=='scope-raw':(base/'logs/failures/manager.log').write_text('changed original bytes')
                self.refresh(base/NEW.EVIDENCE,evidence)
                with patch.dict(os.environ,{},clear=True),patch.object(NEW,'BASE',base),patch.object(NEW,'load',side_effect=consumer_module),patch.object(NEW,'capture',return_value=actual),patch.object(M,'load',return_value=SimpleNamespace(verify_log=Mock())),self.assertRaises(ValueError):NEW.accepted(lab)

if __name__=='__main__':unittest.main(verbosity=2)
