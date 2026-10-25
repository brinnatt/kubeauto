"""Exact empty-store transition and retained-target failure boundaries."""
from contextlib import redirect_stdout
import copy
import importlib.util
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import yaml

ROOT=Path(__file__).resolve().parents[2]
SPEC=importlib.util.spec_from_file_location('v1_prepare_contract',ROOT/'tests/helpers/longhorn-v1-upgrade-prepare.py')
MODULE=importlib.util.module_from_spec(SPEC);SPEC.loader.exec_module(MODULE)


class PreparationContracts(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.directory=Path(self.tmp.name)
        self.source=self.lab('source',MODULE.impl.SOURCE_NODES,'v1')
        self.target=self.lab('target',('192.168.47.149',),'v2')
        self.backups=SimpleNamespace(BACKUP_RESOURCES=('backups.longhorn.io',))
        self.scenario=MODULE.Preparation(self.source,self.target,self.backups)

    def lab(self,name,nodes,engine):
        path=self.directory/name;path.mkdir();(path/'yml/longhorn').mkdir(parents=True)
        (path/'yml/longhorn/block-disks.json').write_text('{}')
        (path/'config.yml').write_text(yaml.safe_dump({'longhorn_version':'1.12.1','longhorn_data_engines':[engine]}))
        state={'run_id':name,'cluster_uid':name,'node_uids':{'node':'uid'},'namespace_uid':'new-'+name,
               'host_state':{'owned':True},'disks':{'worker':{'phase':'mounted'}}}
        def get(kind,*args,**kwargs):
            if kind=='backuptargets.longhorn.io':return {'spec':{}}
            return {'items':[]}
        base=path/'base';(base/'tests/fixtures').mkdir(parents=True)
        (base/'tests/fixtures/longhorn-v1-qualified-disks.txt').write_bytes((ROOT/'tests/fixtures/longhorn-v1-qualified-disks.txt').read_bytes())
        return SimpleNamespace(BASE=base,DIRECTORY=path,NODES=nodes,WORKERS=('worker',),owner=Mock(return_value=state),
            get=Mock(side_effect=get),core_identity=Mock(),installation_health=Mock(),cache_artifacts=Mock(),
            recover_installation=Mock(),capture_hosts=Mock(),prepare_disks=Mock(),prepare_credentials=Mock(),
            write_private=lambda p,r:p.write_text(json.dumps(r)))

    def identity(self,lab,workloads=None):return {'cluster_uid':lab.owner()['cluster_uid'],'namespace_uid':lab.owner()['namespace_uid']}

    def release(self,lab):return {'app_version':str(yaml.safe_load((lab.DIRECTORY/'config.yml').read_text())['longhorn_version']),'status':'deployed'}

    def preflight(self):
        with patch.object(MODULE.impl.upgrade,'release',side_effect=self.release),patch.object(MODULE.impl.upgrade,'retained_identity',side_effect=self.identity),redirect_stdout(io.StringIO()):
            self.scenario.preflight()
        return json.loads(self.scenario.path.read_text())

    def test_preflight_qualifies_artifacts_before_any_uninstall(self):
        row=self.preflight();self.assertEqual('preflight-complete',row['stage'])
        self.source.cache_artifacts.assert_called_once();self.source.recover_installation.assert_not_called()
        self.target.cache_artifacts.assert_not_called()

    def test_existing_business_or_foreign_engine_never_reaches_artifact_or_uninstall(self):
        for defect in ('journal','pv','namespace','store','engine','version','allowlist','disk'):
            with self.subTest(defect=defect):
                if defect=='journal':(self.source.DIRECTORY/MODULE.JOURNALS[0]).write_text('{}')
                elif defect=='pv':self.source.get.side_effect=lambda kind,*a,**k: {'items':[{'spec':{'csi':{'driver':'driver.longhorn.io'}}}]} if kind=='pv' else {'spec':{}} if kind=='backuptargets.longhorn.io' else {'items':[]}
                elif defect=='namespace':self.source.get.side_effect=lambda kind,*a,**k: {'items':[{'metadata':{'name':'longhorn-business-foreign'}}]} if kind=='namespaces' else {'spec':{}} if kind=='backuptargets.longhorn.io' else {'items':[]}
                elif defect=='store':self.source.get.side_effect=lambda kind,*a,**k: {'spec':{'backupTargetURL':'s3://foreign'}} if kind=='backuptargets.longhorn.io' else {'items':[]}
                elif defect in ('engine','version'):(self.source.DIRECTORY/'config.yml').write_text(yaml.safe_dump({'longhorn_data_engines':['v2'] if defect=='engine' else ['v1'],'longhorn_version':'1.12.0' if defect=='version' else '1.12.1'}))
                elif defect=='allowlist':(self.source.BASE/'tests/fixtures/longhorn-v1-qualified-disks.txt').write_text('bad')
                else:self.source.owner.return_value['disks']['worker']['phase']='planned'
                with self.assertRaises(ValueError):self.preflight()
                self.source.cache_artifacts.assert_not_called();self.source.recover_installation.assert_not_called()
                self.setUp()

    def test_prepare_orders_native_retirement_before_hosts_disks_credentials_and_old_setup(self):
        self.preflight();events=[]
        for method in ('recover_installation','capture_hosts','prepare_disks','prepare_credentials'):
            getattr(self.source,method).side_effect=lambda *args,method=method:events.append(method)
        def configure(source,pid):
            events.append('configure_old');self.assertEqual(123,pid)
            path=source.DIRECTORY/'config.yml';config=yaml.safe_load(path.read_text());config['longhorn_version']='1.12.0';path.write_text(yaml.safe_dump(config))
        with patch.object(MODULE.impl,'configure_old',side_effect=configure),patch.object(MODULE.impl.upgrade,'release',side_effect=self.release),patch.object(MODULE.impl.upgrade,'retained_identity',side_effect=self.identity),redirect_stdout(io.StringIO()):
            self.scenario.prepare(123)
        self.assertEqual(['recover_installation','capture_hosts','prepare_disks','prepare_credentials','configure_old'],events)
        self.assertEqual('old-ready',json.loads(self.scenario.path.read_text())['stage'])

    def test_failed_transition_retains_exact_intent_without_installing_old(self):
        self.preflight();self.source.prepare_disks.side_effect=RuntimeError('disk failed')
        with patch.object(MODULE.impl,'configure_old') as configure,patch.object(MODULE.impl.upgrade,'release',side_effect=self.release),patch.object(MODULE.impl.upgrade,'retained_identity',side_effect=self.identity):
            with self.assertRaisesRegex(RuntimeError,'disk failed'):self.scenario.prepare(123)
            configure.assert_not_called();self.source.prepare_credentials.assert_not_called()
        self.assertEqual('preparing-qualified-disks',json.loads(self.scenario.path.read_text())['stage'])

    def test_core_or_target_raw_identity_drift_blocks_prepare_before_uninstall(self):
        self.preflight()
        with patch.object(MODULE.impl.upgrade,'retained_identity',side_effect=self.identity):
            (self.target.DIRECTORY/'yml/longhorn/block-disks.json').write_text('{"changed":true}')
            with self.assertRaisesRegex(ValueError,'independent target'):self.scenario.prepare(123)
        self.source.recover_installation.assert_not_called()

    def test_finalize_requires_current_namespace_bound_completed_result_and_both_empty(self):
        row=self.preflight();row.update(stage='old-ready',source_namespace_uid='new-source');self.scenario.save(row)
        result={'id':'current','stage':'complete','source_identity':{'cluster_uid':'source','namespace_uid':'old-source'},'target_identity':{'cluster_uid':'target'}}
        result_path=self.source.DIRECTORY/'longhorn-upgrade-result-current.json';result_path.write_text(json.dumps(result))
        with patch.object(MODULE.impl.upgrade,'release',side_effect=self.release),patch.object(MODULE.impl.upgrade,'retained_identity',side_effect=self.identity),redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(ValueError,'completed isolated-recovery'):self.scenario.finalize()
            result['source_identity']['namespace_uid']='new-source';result_path.write_text(json.dumps(result))
            self.scenario.finalize()
        self.assertFalse(self.scenario.path.exists());self.assertTrue((self.source.DIRECTORY/'longhorn-v1-upgrade-preparation-complete-current.json').exists())


if __name__=='__main__':unittest.main()


class OldReadyContinuationContracts(PreparationContracts):
    def initial_failure(self):
        row = self.preflight()
        row.update(stage='old-ready', source_namespace_uid=self.source.owner()['namespace_uid'])
        self.scenario.save(row)
        path = self.source.DIRECTORY/'config.yml'
        config = yaml.safe_load(path.read_text()); config['longhorn_version'] = '1.12.0'
        path.write_text(yaml.safe_dump(config))
        receipt = {'id': 'a'*32, 'source_identity': self.identity(self.source),
                   'target_identity': self.identity(self.target), 'payloads': {}, 'points': {},
                   'stage': 'source-1.12.0'}
        business = self.source.DIRECTORY/'longhorn-v1-upgrade-lifecycle.json'
        business.write_text(json.dumps(receipt, indent=2)+'\n')
        return receipt, business

    def test_real_preflight_prepare_then_business_baseline_reuses_old_installation(self):
        receipt, business = self.initial_failure(); original = business.read_bytes()
        with patch.object(MODULE.impl.upgrade, 'release', side_effect=self.release), \
             patch.object(MODULE.impl.upgrade, 'retained_identity', side_effect=self.identity), \
             patch.object(MODULE.impl.upgrade, 'runtime_identity', return_value={'version':'1.12.0'}) as runtime, \
             patch.object(MODULE.impl, 'configure_old') as configure, redirect_stdout(io.StringIO()):
            self.scenario.preflight(); self.scenario.prepare(123)
            # Execute the actual business handoff refusal/acceptance boundary.
            scenario = SimpleNamespace(source=self.source, target=self.target, backups=self.backups, path=business)
            MODULE.impl.V1Upgrade.validate_baseline(scenario)
        self.assertFalse(business.exists())
        self.assertEqual(original, business.with_name('longhorn-v1-upgrade-before-business-'+receipt['id']+'.json').read_bytes())
        self.source.recover_installation.assert_not_called(); configure.assert_not_called()
        self.source.cache_artifacts.assert_called_once()  # initial preflight only
        self.assertEqual(2, runtime.call_count)

    def test_started_business_changed_identity_or_nonempty_installation_stops_without_archiving(self):
        for defect in ('stage','payload','point','runtime-record','id','source','target','namespace','journal','pv','backup-target','target-version','archive'):
            with self.subTest(defect=defect):
                self.setUp(); receipt, business = self.initial_failure()
                if defect == 'stage': receipt['stage'] = 'business-pass'
                elif defect == 'payload': receipt['payloads'] = {'plain':'data'}
                elif defect == 'point': receipt['points'] = {'backup':{}}
                elif defect == 'runtime-record': receipt['old_runtime'] = {}
                elif defect == 'id': receipt['id'] = '../foreign'
                elif defect in ('source','target'): receipt[defect+'_identity']['namespace_uid'] = 'foreign'
                elif defect == 'namespace': self.source.owner.return_value['namespace_uid'] = 'foreign'
                elif defect == 'journal': (self.source.DIRECTORY/'longhorn-backup-store.json').write_text('{}')
                elif defect == 'pv': self.source.get.side_effect = lambda kind,*a,**k: {'items':[{'spec':{'csi':{'driver':'driver.longhorn.io'}}}]} if kind=='pv' else {'spec':{}} if kind=='backuptargets.longhorn.io' else {'items':[]}
                elif defect == 'backup-target': self.target.get.side_effect = lambda kind,*a,**k: {'spec':{'backupTargetURL':'s3://foreign'}} if kind=='backuptargets.longhorn.io' else {'items':[]}
                elif defect == 'target-version': (self.target.DIRECTORY/'config.yml').write_text('longhorn_version: 1.12.0\nlonghorn_data_engines: [v2]\n')
                elif defect == 'archive': business.with_name('longhorn-v1-upgrade-before-business-'+receipt['id']+'.json').write_text('foreign')
                business.write_text(json.dumps(receipt))
                with patch.object(MODULE.impl.upgrade, 'release', side_effect=self.release), \
                     patch.object(MODULE.impl.upgrade, 'retained_identity', side_effect=self.identity), \
                     patch.object(MODULE.impl.upgrade, 'runtime_identity'):
                    with self.assertRaises(ValueError):
                        self.scenario.preflight(); self.scenario.prepare(123)
                self.assertTrue(business.exists()); self.source.recover_installation.assert_not_called()

    def test_preflight_prepare_preserve_existing_protected_business_after_qualified_resume(self):
        row,business=self.initial_failure()
        row.update(old_runtime={'version':'1.12.0'},old_encrypted_device={},restores={'v1-plain-before':{'remount':True}})
        business.write_text(json.dumps(row));original=business.read_bytes()
        qualified=SimpleNamespace(resume_preflight=Mock())
        protection_fields=MODULE.impl.V1Upgrade.PROTECTION_FIELDS
        with patch.object(MODULE.impl.upgrade,'retained_identity',side_effect=self.identity), \
             patch.object(MODULE.impl.upgrade,'load',return_value=object()), \
             patch.object(MODULE.impl,'V1Upgrade') as scenario,redirect_stdout(io.StringIO()):
            scenario.PROTECTION_FIELDS=protection_fields
            scenario.return_value=qualified
            self.scenario.preflight();self.scenario.prepare(123)
        self.assertEqual(2,qualified.resume_preflight.call_count)
        self.assertEqual(original,business.read_bytes());self.source.recover_installation.assert_not_called()
        self.assertFalse(list(business.parent.glob('longhorn-v1-upgrade-before-business-*')))
