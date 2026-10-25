"""Strict real-upgrade identity edge; original install/adoption guards stay intact."""
import copy
from contextlib import redirect_stdout
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import yaml

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location('v2_upgrade_baseline_contract', ROOT/'tests/helpers/longhorn-v2-upgrade-baseline.py')
MODULE = importlib.util.module_from_spec(SPEC); SPEC.loader.exec_module(MODULE)


class UpgradeBaselineContracts(unittest.TestCase):
    def fixture(self, directory):
        base = Path(directory)
        for name in MODULE.INPUTS:
            path = base/name; path.parent.mkdir(parents=True, exist_ok=True); path.write_text(name)
        (base/'roles/longhorn/files/images.json').write_text(json.dumps({'1.12.1':{'longhorn-instance-manager':MODULE.TAG}}))
        def snapshot(epoch):
            identity = {'run_id': 'run', 'cluster_uid': 'cluster', 'namespace_uid': epoch+'-namespace',
                'node_uids': {'longhorn-node-'+a.rsplit('.', 1)[1]: a+'-kube-uid' for a in MODULE.NODES},
                'raw_disks': {a: {'path': '/dev/disk/by-id/'+a, 'identity': a+'-hardware'} for a in MODULE.NODES}}
            native = {'cluster_uid': 'cluster', 'namespace_uid': identity['namespace_uid'], 'nodes': {a: {
                'name': 'longhorn-node-'+a.rsplit('.', 1)[1], 'kubernetes_node_uid': a+'-kube-uid',
                'longhorn_node_uid': epoch+a+'-native-uid', 'disks': {'raw': {
                    'disk_uuid': a+'-disk-uuid', 'path': '/dev/disk/by-id/'+a, 'hardware_identity': a+'-hardware', 'disk_driver': 'aio'}}}
                for a in MODULE.NODES}}
            managers = {epoch+a: {'uid': epoch+a+'-im-uid', 'pod_uid': epoch+a+'-pod-uid',
                'node': 'longhorn-node-'+a.rsplit('.', 1)[1], 'image': 'registry/longhorn-instance-manager:'+MODULE.TAG,
                'image_id': 'containerd://sha256:'+'a'*64} for a in MODULE.NODES}
            return {'baseline_identity': identity, 'native_block_receipt': native,
                'native_block_receipt_sha256': hashlib.sha256(json.dumps(native).encode()).hexdigest(),
                'config_sha256': 'c'*64, 'managers': managers, 'retained': {'namespace_uid': identity['namespace_uid'], 'cluster_uid': 'cluster'}}
        before, after = snapshot('old'), snapshot('new')
        prior = {'status': 'pass', 'rc': 0, 'failure_markers': 0, 'durable': {'exit': 0, 'finalized': 0},
            'signed_scope': 'v2-im-revision-2-adopt-qualification', 'version': '1.12.1', 'instance_manager_version': MODULE.TAG,
            'remote_input_verification': {'mismatches': 0}, 'baseline_identity': before['baseline_identity']}
        prior['independent_readback'] = {'installation_healthy': True, 'empty_business': True, 'adoption_journal_absent': True,
            'baseline': {'baseline_identity': prior['baseline_identity'], 'native_block_receipt_sha256': before['native_block_receipt_sha256']},
            'managers': {name: {key: row[key] for key in ('uid', 'node', 'image')} for name, row in before['managers'].items()}}
        (base/MODULE.PREVIOUS).parent.mkdir(parents=True, exist_ok=True); (base/MODULE.PREVIOUS).write_text(json.dumps(prior))
        def ref(path): return {'path': str(path.relative_to(base)), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
        target = {'cluster_uid': 'independent', 'namespace_uid': 'target'}
        prep = {'stage': 'complete', 'source_namespace_uid': after['baseline_identity']['namespace_uid'],
            'baseline_before': before, 'baseline_after': after, 'previous_evidence': ref(base/MODULE.PREVIOUS),
            'source_core': {key: after['baseline_identity'][key] for key in ('run_id', 'cluster_uid', 'node_uids', 'raw_disks')},
            'target_identity': {'retained': target}}
        origin = {'created_version': '1.12.0', 'upgraded_version': '1.12.1', 'volume_name': 'legacy', 'volume_uid': 'legacy-uid',
            'source_volume': 'parent', 'source_volume_uid': 'parent-uid'}
        result = {'stage': 'complete', 'source_identity': after['retained'], 'target_identity': target,
            'legacy': {'refusal_verified': True, 'retired': True, 'legacy_origin': origin,
                'source_volume': 'parent', 'source_volume_uid': 'parent-uid', 'volume': {'metadata': {'name': 'legacy', 'uid': 'legacy-uid'}}},
            'restores': {point: {'workload': point, 'volume_uid': point+'-uid', 'confirmed_sha256': '1'*64,
                'independent_write_sha256': '2'*64, 'remount': True} for point in MODULE.POINTS}}
        runtime = base/'clusters/longhorn-v2-gate/upgrade-baseline'; runtime.mkdir(parents=True)
        prep_path = runtime/'preparation.json'; prep_path.write_text(json.dumps(prep))
        result_path = runtime/'result.json'; result_path.write_text(json.dumps(result))
        transition = {'previous_evidence': ref(base/MODULE.PREVIOUS), 'before': before, 'after': after,
            'preparation': ref(prep_path), 'upgrade_result': ref(result_path)}
        log = runtime/'gate.log'
        lines = ['LONGHORN_V2_UPGRADE_PASS', 'LONGHORN_V2_UPGRADE_GATE_PASS', 'LONGHORN_V2_UPGRADE_GATE_SCOPED_CLEAN_PASS',
            'LONGHORN_V2_LEGACY_LINKED_REFUSALS_PASS count=4 real_upgrade_fixture=true',
            'LONGHORN_V2_UPGRADE_BASELINE_TRANSITION '+json.dumps(transition),
            'LONGHORN_V2_UPGRADE_LEGACY_VERIFIED '+json.dumps(origin)]
        lines += ['LONGHORN_V2_UPGRADE_ISOLATED_RESTORE_VERIFIED '+json.dumps({'point': point, **row}) for point, row in result['restores'].items()]
        log.write_text('\n'.join(lines)+'\n'); (runtime/'run.exit').write_text('0\n'); (runtime/'run.finalized').write_text('0\n')
        evidence = {'status': 'pass', 'rc': 0, 'failure_markers': 0, 'durable': {'exit': 0, 'finalized': 0},
            'signed_scope': 'v2-upgrade-lifecycle', 'version': '1.12.1', 'remote_input_verification': {'mismatches': 0},
            'frozen_inputs': {name: ref(base/name)['sha256'] for name in MODULE.INPUTS}, 'transition': transition,
            'independent_readback': {'installation_healthy': True, 'empty_business': True, 'transition_journal_absent': True, 'baseline': after},
            'receipt_refs': {'log': ref(log), 'exit': ref(runtime/'run.exit'), 'finalized': ref(runtime/'run.finalized')}}
        (base/MODULE.EVIDENCE).write_text(json.dumps(evidence))
        return base, evidence, after, ref

    def accept(self, base, evidence, after, actual=None):
        (base/MODULE.EVIDENCE).write_text(json.dumps(evidence))
        with patch.object(MODULE, 'BASE', base), patch.object(MODULE, 'capture', return_value=actual or after), redirect_stdout(io.StringIO()):
            return MODULE.accepted(SimpleNamespace())

    def test_complete_real_transition_and_durable_four_restore_log_accept_exact_new_baseline(self):
        with tempfile.TemporaryDirectory() as directory:
            base, evidence, after, _ = self.fixture(directory)
            self.assertEqual(after, self.accept(base, evidence, after))

    def test_pending_failed_nonfinalized_or_changed_inputs_never_become_a_new_baseline(self):
        for defect in ('status', 'rc', 'failure', 'durable', 'scope', 'input', 'readback'):
            with self.subTest(defect=defect), tempfile.TemporaryDirectory() as directory:
                base, evidence, after, _ = self.fixture(directory)
                if defect == 'status': evidence['status'] = 'pending'
                if defect == 'rc': evidence['rc'] = 1
                if defect == 'failure': evidence['failure_markers'] = 1
                if defect == 'durable': evidence['durable']['finalized'] = 1
                if defect == 'scope': evidence['signed_scope'] = 'v2-volume'
                if defect == 'input': (base/MODULE.INPUTS[0]).write_text('changed')
                if defect == 'readback': evidence['independent_readback']['empty_business'] = False
                with self.assertRaises(ValueError): self.accept(base, evidence, after)

    def test_prior_accepted_adoption_sha_and_before_native_identity_are_mandatory(self):
        for defect in ('prior-sha', 'prior-durable', 'before-namespace', 'before-receipt', 'before-manager'):
            with self.subTest(defect=defect), tempfile.TemporaryDirectory() as directory:
                base, evidence, after, ref = self.fixture(directory)
                if defect.startswith('prior'):
                    path=base/MODULE.PREVIOUS; row=json.loads(path.read_text())
                    if defect == 'prior-sha': row['comment'] = 'changed'
                    else: row['durable']['exit'] = 1
                    path.write_text(json.dumps(row))
                    if defect == 'prior-durable': evidence['transition']['previous_evidence'] = ref(path)
                if defect == 'before-namespace': evidence['transition']['before']['baseline_identity']['namespace_uid'] = 'foreign'
                if defect == 'before-receipt': evidence['transition']['before']['native_block_receipt_sha256'] = '0'*64
                if defect == 'before-manager': next(iter(evidence['transition']['before']['managers'].values()))['uid'] = 'foreign'
                with self.assertRaises(ValueError): self.accept(base, evidence, after)

    def test_old_namespace_reused_or_core_hardware_raw_uuid_native_node_and_manager_changes_are_rejected(self):
        for defect in ('namespace', 'core', 'hardware', 'raw-uuid', 'native-uid', 'im-uid', 'im-image', 'config'):
            with self.subTest(defect=defect), tempfile.TemporaryDirectory() as directory:
                base, evidence, after, _ = self.fixture(directory); transition=evidence['transition']; new=transition['after']; old=transition['before']
                if defect == 'namespace': new['baseline_identity']['namespace_uid'] = old['baseline_identity']['namespace_uid']
                if defect == 'core': new['baseline_identity']['cluster_uid'] = 'foreign'
                if defect == 'hardware': new['baseline_identity']['raw_disks'][MODULE.NODES[0]]['identity'] = 'foreign'
                if defect == 'raw-uuid': new['native_block_receipt']['nodes'][MODULE.NODES[0]]['disks']['raw']['disk_uuid'] = 'foreign'
                if defect == 'native-uid': new['native_block_receipt']['nodes'][MODULE.NODES[0]]['longhorn_node_uid'] = old['native_block_receipt']['nodes'][MODULE.NODES[0]]['longhorn_node_uid']
                if defect == 'im-uid': next(iter(new['managers'].values()))['uid'] = next(iter(old['managers'].values()))['uid']
                if defect == 'im-image': next(iter(new['managers'].values()))['image'] = 'registry/im:v1.12.1'
                if defect == 'config': new['config_sha256'] = '0'*64
                with self.assertRaises(ValueError): self.accept(base, evidence, after)

    def test_incomplete_preparation_wrong_target_legacy_origin_or_restore_data_cannot_sign_transition(self):
        for defect in ('preparation', 'target', 'legacy', 'restore-count', 'restore-uid', 'restore-hash'):
            with self.subTest(defect=defect), tempfile.TemporaryDirectory() as directory:
                base, evidence, after, ref = self.fixture(directory); transition=evidence['transition']
                path=base/transition['preparation' if defect == 'preparation' else 'upgrade_result']['path']; row=json.loads(path.read_text())
                if defect == 'preparation': row['stage'] = 'installing-old'
                if defect == 'target': row['target_identity']['cluster_uid'] = 'source'
                if defect == 'legacy': row['legacy']['legacy_origin']['created_version'] = '1.12.1'
                if defect == 'restore-count': row['restores'].pop(next(iter(row['restores'])))
                if defect == 'restore-uid':
                    values=list(row['restores'].values()); values[0]['volume_uid']=values[1]['volume_uid']
                if defect == 'restore-hash': next(iter(row['restores'].values()))['confirmed_sha256'] = ''
                path.write_text(json.dumps(row)); transition['preparation' if defect == 'preparation' else 'upgrade_result'] = ref(path)
                with self.assertRaises(ValueError): self.accept(base, evidence, after)

    def test_true_zero_exit_finalized_and_exact_raw_markers_are_checked_even_with_fresh_receipt_hashes(self):
        for defect in ('exit', 'finalized', 'failure', 'terminal', 'legacy', 'four-restores', 'restore-bytes', 'transition'):
            with self.subTest(defect=defect), tempfile.TemporaryDirectory() as directory:
                base, evidence, after, ref = self.fixture(directory)
                key = defect if defect in ('exit', 'finalized') else 'log'; path=base/evidence['receipt_refs'][key]['path']
                lines=path.read_text().splitlines()
                if key != 'log': lines=['1']
                if defect == 'failure': lines += ['LONGHORN_FIRST_FAILURE phase=upgrade rc=17']
                if defect == 'terminal': lines.remove('LONGHORN_V2_UPGRADE_GATE_PASS')
                if defect == 'legacy': lines=[line for line in lines if not line.startswith('LONGHORN_V2_LEGACY_LINKED_REFUSALS_PASS')]
                if defect == 'four-restores': lines=[line for line in lines if '"point": "plain-after"' not in line]
                if defect == 'restore-bytes': lines=[line.replace('"confirmed_sha256": "'+'1'*64+'"','"confirmed_sha256": "'+'3'*64+'"') if line.startswith('LONGHORN_V2_UPGRADE_ISOLATED_RESTORE_VERIFIED ') else line for line in lines]
                if defect == 'transition': lines=[line for line in lines if not line.startswith('LONGHORN_V2_UPGRADE_BASELINE_TRANSITION ')]
                path.write_text('\n'.join(lines)+'\n'); evidence['receipt_refs'][key] = ref(path)
                with self.assertRaises(ValueError): self.accept(base, evidence, after)

    def test_current_runtime_cannot_reuse_valid_transition_after_native_drift(self):
        with tempfile.TemporaryDirectory() as directory:
            base, evidence, after, _ = self.fixture(directory); actual=copy.deepcopy(after)
            actual['baseline_identity']['namespace_uid']='foreign'
            with self.assertRaises(ValueError): self.accept(base, evidence, after, actual)

    def capture_fixture(self, directory):
        base, evidence, after, ref = self.fixture(directory); path=base/'clusters/longhorn-v2-gate'
        config={'longhorn_version':'1.12.1','longhorn_data_engines':['v2'],'longhorn_install':'yes','longhorn_requests':[], 'longhorn_image_prefix':'registry'}
        (path/'config.yml').write_text(yaml.safe_dump(config)); (path/'yml/longhorn').mkdir(parents=True)
        (path/'yml/longhorn/block-disks.json').write_text(json.dumps(after['native_block_receipt']))
        (base/'roles/longhorn/files/images.json').write_text(json.dumps({'1.12.1':{'longhorn-instance-manager':MODULE.TAG}}))
        rows={}
        for name, manager in after['managers'].items():
            rows[('pod',name)]={'metadata':{'uid':manager['pod_uid'],'ownerReferences':[{'kind':'InstanceManager','uid':manager['uid'],'controller':True}]},
                'spec':{'nodeName':manager['node'],'containers':[{'name':'instance-manager','image':manager['image']}]},
                'status':{'containerStatuses':[{'ready':True,'state':{'running':{'startedAt':'recorded'}},'imageID':manager['image_id']}]}}
        for address, node in after['native_block_receipt']['nodes'].items():
            disk=node['disks']['raw'];rows[('nodes.longhorn.io',node['name'])]={'metadata':{'uid':node['longhorn_node_uid']},
                'spec':{'disks':{'raw':{'diskType':'block','diskDriver':'aio','path':disk['path']}}},
                'status':{'diskStatus':{'raw':{'diskUUID':disk['disk_uuid']}}}}
        def get(kind,name='',namespace=''):
            if kind=='backuptargets.longhorn.io':return {'spec':{'backupTargetURL':'','credentialSecret':''}}
            if not name:return {'items':[]}
            return rows[(kind,name)]
        lab=SimpleNamespace(NODES=MODULE.NODES,DIRECTORY=path,core_identity=Mock(),installation_health=Mock(),owner=Mock(return_value=after['baseline_identity']),get=Mock(side_effect=get))
        adopter=SimpleNamespace(empty=Mock(),managers=Mock(return_value=copy.deepcopy({name:{k:v for k,v in row.items() if k in ('uid','node','image')} for name,row in after['managers'].items()})))
        upgrade=SimpleNamespace(retained_identity=Mock(return_value=after['retained']))
        return base,lab,rows,adopter,upgrade

    def test_capture_reads_actual_owned_im_pod_image_id_and_native_raw_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            base,lab,_,adopter,upgrade=self.capture_fixture(directory)
            with patch.object(MODULE,'BASE',base),patch.object(MODULE,'load',side_effect=lambda name:adopter if name=='longhorn-v2-im-adopt.py' else upgrade):
                current=MODULE.capture(lab)
            self.assertEqual(3,len(current['managers']));self.assertTrue(all('sha256:' in m['image_id'] for m in current['managers'].values()))
            adopter.empty.assert_called_once_with(lab);lab.installation_health.assert_called_once()

    def test_capture_refuses_changed_pod_owner_image_missing_digest_native_disk_uuid_and_expanded_inventory(self):
        for defect in ('owner','image','digest','raw','inventory'):
            with self.subTest(defect=defect),tempfile.TemporaryDirectory() as directory:
                base,lab,rows,adopter,upgrade=self.capture_fixture(directory);pod=next(row for (kind,_),row in rows.items() if kind=='pod')
                if defect=='owner':pod['metadata']['ownerReferences'][0]['uid']='foreign'
                if defect=='image':pod['spec']['containers'][0]['image']='foreign'
                if defect=='digest':pod['status']['containerStatuses'][0]['imageID']=''
                if defect=='raw':next(row for (kind,_),row in rows.items() if kind=='nodes.longhorn.io')['status']['diskStatus']['raw']['diskUUID']='foreign'
                if defect=='inventory':lab.NODES=(*MODULE.NODES,'192.168.47.149')
                with patch.object(MODULE,'BASE',base),patch.object(MODULE,'load',side_effect=lambda name:adopter if name=='longhorn-v2-im-adopt.py' else upgrade),self.assertRaises(ValueError):MODULE.capture(lab)

    def test_install_consumer_uses_same_strict_transition_validator_then_keeps_native_health_and_empty_guards(self):
        install=MODULE.load('longhorn-v2-install.py')
        with tempfile.TemporaryDirectory() as directory:
            base,lab,_,_,_=self.capture_fixture(directory)
            evidence=json.loads((base/MODULE.EVIDENCE).read_text())
            baseline=evidence['transition']['after']
            baseline['native_block_receipt_sha256']=MODULE.digest(lab.DIRECTORY/'yml/longhorn/block-disks.json')
            validator=SimpleNamespace(accepted=Mock(return_value=baseline))
            with patch.object(install,'BASE',base),patch.object(install,'load',return_value=validator),patch.object(install,'health') as health,redirect_stdout(io.StringIO()):
                install.volume_baseline(lab)
            validator.accepted.assert_called_once_with(lab);health.assert_called_once_with(lab)

    def test_install_pending_transition_cannot_fall_back_to_an_old_pass(self):
        install=MODULE.load('longhorn-v2-install.py')
        with tempfile.TemporaryDirectory() as directory:
            base,lab,_,_,_=self.capture_fixture(directory)
            old=base/'tests/evidence/longhorn-v2-aio-install-qualification-20261020.json'
            old.write_text(json.dumps({'status':'pass','rc':0,'failure_markers':0}))
            validator=SimpleNamespace(accepted=Mock(side_effect=ValueError('52 pending')))
            with patch.object(install,'BASE',base),patch.object(install,'load',return_value=validator),patch.object(install,'health') as health,self.assertRaisesRegex(ValueError,'52 pending'):
                install.volume_baseline(lab)
            health.assert_not_called()


if __name__=='__main__':unittest.main()
