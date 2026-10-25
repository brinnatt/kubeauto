"""Linked-clone safety boundaries, mixed-class ownership and guarded cleanup."""
import copy
from contextlib import nullcontext, redirect_stdout
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import Mock, call

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location('linked_contract', ROOT/'tests/helpers/longhorn-v2-linked-clones.py')
LINKED = importlib.util.module_from_spec(SPEC); SPEC.loader.exec_module(LINKED)


class LinkedCloneContracts(unittest.TestCase):
    def graph(self):
        source = {'metadata': {'name': 'source', 'uid': 's'}, 'spec': {'dataEngine': 'v2'}}
        clone = {'metadata': {'name': 'clone', 'uid': 'c', 'labels': {LINKED.LABEL: 'entry'}},
            'spec': {'dataEngine': 'v2', 'cloneMode': 'linked-clone'},
            'status': {'cloneStatus': {'sourceVolume': 'source', 'state': 'completed', 'snapshot': 'entry'}}}
        parents = [{'metadata': {'name': 's'+str(i), 'uid': 'su'+str(i)},
            'spec': {'volumeName': 'source', 'nodeID': 'node'+str(i), 'diskID': 'disk'+str(i)}} for i in range(3)]
        children = [{'metadata': {'name': 'c'+str(i), 'uid': 'cu'+str(i)},
            'spec': {'volumeName': 'clone', 'linkedCloneSrcReplicaName': 's'+str(i),
                     'nodeID': 'node'+str(i), 'diskID': 'disk'+str(i)}} for i in range(3)]
        return source, clone, parents, children

    def test_linked_graph_requires_three_distinct_same_disk_parent_pairs(self):
        rows = LINKED.linked_topology(*self.graph())
        self.assertEqual(3, len(rows)); self.assertEqual('s0', rows['c0']['source'])
        self.assertEqual('disk2', rows['c2']['disk'])

    def test_full_copy_incomplete_wrong_source_or_unprotected_entrypoint_is_rejected(self):
        for defect in ('v1','mode','source','incomplete','snapshot','label'):
            with self.subTest(defect=defect):
                source, clone, parents, children = self.graph()
                if defect == 'v1': clone['spec']['dataEngine'] = 'v1'
                if defect == 'mode': clone['spec']['cloneMode'] = 'full-copy'
                if defect == 'source': clone['status']['cloneStatus']['sourceVolume'] = 'foreign'
                if defect == 'incomplete': clone['status']['cloneStatus']['state'] = 'copy-completed-awaiting-healthy'
                if defect == 'snapshot': clone['status']['cloneStatus']['snapshot'] = ''
                if defect == 'label': clone['metadata']['labels'][LINKED.LABEL] = 'other'
                with self.assertRaises(ValueError): LINKED.linked_topology(source, clone, parents, children)

    def test_missing_duplicated_foreign_or_cross_disk_replica_ancestry_is_rejected(self):
        for defect in ('count','parent-count','same-node','missing-parent','reused-parent','node','disk','empty-disk','same-uid','child-volume','parent-volume'):
            with self.subTest(defect=defect):
                source, clone, parents, children = self.graph()
                if defect == 'count': children.pop()
                if defect == 'parent-count': parents.pop()
                if defect == 'same-node': parents[1]['spec']['nodeID'] = 'node0'
                if defect == 'missing-parent': children[0]['spec']['linkedCloneSrcReplicaName'] = 'unknown'
                if defect == 'reused-parent': children[1] = copy.deepcopy(children[0])
                if defect == 'node': children[0]['spec']['nodeID'] = 'node1'
                if defect == 'disk': children[0]['spec']['diskID'] = 'disk1'
                if defect == 'empty-disk': children[0]['spec']['diskID'] = ''
                if defect == 'same-uid': children[0]['metadata']['uid'] = 'su0'
                if defect == 'child-volume': children[0]['spec']['volumeName'] = 'other'
                if defect == 'parent-volume': parents[0]['spec']['volumeName'] = 'other'
                with self.assertRaises(ValueError): LINKED.linked_topology(source, clone, parents, children)

    def fixture(self, directory):
        lab = SimpleNamespace(DIRECTORY=Path(directory), write_private=Mock(), get=Mock(), remove=Mock(),
            restore_request_input=Mock(), owner=Mock(return_value={'run_id':'r'}),
            kubectl=Mock(), wait=Mock(side_effect=lambda stage, timeout, check: check()))
        return lab, LINKED.Probe(lab)

    def test_admission_refusal_is_server_dry_run_and_checks_exact_message_and_unchanged_spec(self):
        with tempfile.TemporaryDirectory() as directory:
            lab, probe = self.fixture(directory)
            obj = {'metadata': {'name':'source','uid':'u','resourceVersion':'9'}, 'spec': {'numberOfReplicas':3}}
            lab.get.return_value = copy.deepcopy(obj)
            lab.kubectl.return_value = SimpleNamespace(returncode=1, stdout='', stderr='denied: protected-source')
            with redirect_stdout(io.StringIO()):
                probe.refused('volumes.longhorn.io', obj, 'protected-source', replicas=2)
            argv = lab.kubectl.call_args.args
            self.assertIn('--dry-run=server', argv)
            patch = json.loads(lab.kubectl.call_args.kwargs['stdin'])
            self.assertEqual('/metadata/uid', patch[0]['path']); self.assertEqual('u',patch[0]['value'])
            self.assertEqual('/metadata/resourceVersion',patch[1]['path'])
            with redirect_stdout(io.StringIO()): probe.refused('volumes.longhorn.io',obj,'protected-source')
            self.assertIn('--dry-run=server',lab.kubectl.call_args.args)
            lab.remove.assert_not_called()
            for rc, message, changed in ((0,'protected-source',False),(1,'unrelated error',False),(1,'protected-source',True)):
                lab.kubectl.return_value = SimpleNamespace(returncode=rc,stdout='',stderr=message)
                lab.get.return_value = copy.deepcopy(obj)
                if changed: lab.get.return_value['spec']['numberOfReplicas'] = 2
                with self.assertRaises(ValueError): probe.refused('volumes.longhorn.io',obj,'protected-source')

    def test_binding_accepts_explicit_mixed_classes_but_rejects_wrong_uid_driver_or_reclaim(self):
        for defect in ('','class','uid','driver','reclaim','pv-replaced','engine'):
            with self.subTest(defect=defect),tempfile.TemporaryDirectory() as directory:
                lab, probe = self.fixture(directory)
                probe.journal = {'namespace':'business','class_name':'linked-sc','objects':{'PersistentVolumeClaim/clone':'claim'},'claims':{}}
                claim = {'metadata':{'uid':'claim'},'spec':{'storageClassName':'linked-sc'}}
                pv = {'metadata':{'name':'pv','uid':'pvuid'},'spec':{'claimRef':{'uid':'claim','name':'clone','namespace':'business'},
                    'storageClassName':'linked-sc','persistentVolumeReclaimPolicy':'Retain','csi':{'driver':'driver.longhorn.io','volumeHandle':'volume'}}}
                volume = {'metadata':{'uid':'volumeuid'},'spec':{'dataEngine':'v2'}}
                if defect == 'class': pv['spec']['storageClassName'] = 'longhorn'
                if defect == 'uid': claim['metadata']['uid'] = 'foreign'
                if defect == 'driver': pv['spec']['csi']['driver'] = 'other'
                if defect == 'reclaim': pv['spec']['persistentVolumeReclaimPolicy'] = 'Delete'
                if defect == 'pv-replaced': probe.journal['claims']['clone'] = {'pv_uid':'previous'}
                if defect == 'engine': volume['spec']['dataEngine'] = 'v1'
                lab.get.side_effect = lambda kind,*args,**kwargs: claim if kind == 'pvc' else {'items':[pv]} if kind == 'pv' else volume
                if defect:
                    with self.assertRaises(ValueError): probe.bind('clone')
                else:
                    self.assertEqual('volumeuid',probe.bind('clone')['volume_uid']); lab.write_private.assert_called_once()
                lab.remove.assert_not_called()

    def test_cleanup_retires_nested_then_clone_then_source_without_deleting_entrypoint(self):
        with tempfile.TemporaryDirectory() as directory:
            lab, probe = self.fixture(directory)
            journal = {'run_id':'r','namespace':'longhorn-linked-owned','namespace_uid':'ns','objects':{'Pod/source':'p'},
                'claims':{},'class_name':'linked-sc'}
            probe.record.write_text(json.dumps(journal))
            lab.get.side_effect = lambda kind,*args,**kwargs: ({'items':[]} if kind in ('volumes.longhorn.io','pv') else
                {'metadata':{'uid':'ns'}} if kind == 'namespace' and not lab.remove.called else None)
            lab.check_business_graph = Mock(); probe.delete_pod = Mock(); probe.retire = Mock()
            with redirect_stdout(io.StringIO()): probe.cleanup()
            self.assertEqual(['nested','clone','source'],[c.args[0] for c in probe.retire.call_args_list])
            self.assertFalse(probe.record.exists()); lab.check_business_graph.assert_called()
            self.assertEqual(1,lab.remove.call_count)

    def test_replacement_namespace_stops_cleanup_before_any_delete(self):
        with tempfile.TemporaryDirectory() as directory:
            lab, probe = self.fixture(directory)
            probe.record.write_text(json.dumps({'run_id':'r','namespace':'longhorn-linked-owned','namespace_uid':'old'}))
            lab.get.return_value = {'metadata':{'uid':'foreign'}}
            with self.assertRaises(ValueError): probe.cleanup()
            self.assertTrue(probe.record.exists()); lab.remove.assert_not_called()

    def test_journal_symlinks_are_rejected_before_read_or_mutation(self):
        for dangling in (False,True):
            with self.subTest(dangling=dangling),tempfile.TemporaryDirectory() as directory:
                lab, probe = self.fixture(directory); target=Path(directory)/'foreign'
                if not dangling: target.write_text('not a fixture journal')
                probe.record.symlink_to(target)
                with self.assertRaisesRegex(ValueError,'symlink'): probe.cleanup()
                lab.get.assert_not_called(); lab.remove.assert_not_called()

    def test_missing_namespace_with_retained_pv_keeps_journal_and_class(self):
        with tempfile.TemporaryDirectory() as directory:
            lab, probe=self.fixture(directory)
            probe.record.write_text(json.dumps({'run_id':'r','namespace':'longhorn-linked-owned','namespace_uid':'ns',
                'claims':{'source':{'pv':'owned-pv','pv_uid':'pvuid','volume':'volume','volume_uid':'vuid'}}}))
            lab.get.side_effect=lambda kind,*a,**kw: {'metadata':{'uid':'pvuid'}} if kind=='pv' else None
            with self.assertRaisesRegex(ValueError,'retained PVs'): probe.cleanup()
            self.assertTrue(probe.record.exists()); lab.remove.assert_not_called()

    def test_unidentified_native_after_scope_cleanup_retains_journal(self):
        with tempfile.TemporaryDirectory() as directory:
            lab, probe=self.fixture(directory)
            probe.record.write_text(json.dumps({'run_id':'r','namespace':'longhorn-linked-owned','namespace_uid':'ns',
                'claims':{},'class_name':'linked-sc'}))
            lab.get.side_effect=lambda kind,*a,**kw: {'items':[{'metadata':{'name':'unbound-native','uid':'unknown'}}]} if kind=='volumes.longhorn.io' else {'items':[]} if kind=='pv' else None
            with self.assertRaisesRegex(ValueError,'native volumes or PVs remain'): probe.cleanup()
            self.assertTrue(probe.record.exists()); lab.remove.assert_not_called()

    def test_application_digest_mismatch_stops_without_success(self):
        with tempfile.TemporaryDirectory() as directory:
            lab, probe = self.fixture(directory); probe.journal={'namespace':'owned'}
            lab.payload_program = Mock(return_value='read');lab.kubectl.return_value=SimpleNamespace(stdout='0'*64)
            with self.assertRaises(ValueError): probe.io('clone','expected',read=True)

    def test_business_builds_source_clone_nested_pvc_datasource_chain(self):
        with tempfile.TemporaryDirectory() as directory:
            lab, probe = self.fixture(directory)
            lab.yaml = SimpleNamespace(safe_load=Mock(return_value={
                'longhorn_storage_classes': [{'name':'longhorn','data_engine':'v2'}]}))
            (lab.DIRECTORY/'config.yml').write_text('fixture')
            lab.scoped_cleanup = Mock(return_value=nullcontext())
            lab.run_requests = Mock()
            lab.get.side_effect = [None, {'metadata':{'uid':'class'}}]
            lab.kubectl.return_value = SimpleNamespace(stdout=json.dumps({'metadata':{'uid':'ns'}}))
            lab.namespace_defaults = Mock(return_value=['default'])
            probe.io = Mock()
            probe.claim = Mock(side_effect=[{'volume_uid':'source'}, {}, RuntimeError('graph ready')])
            with self.assertRaisesRegex(RuntimeError,'graph ready'):
                probe.business(123)
            self.assertEqual([call('source'), call('clone','source'), call('nested','clone')],
                probe.claim.call_args_list)

    def test_failed_native_clone_stops_before_creating_or_waiting_for_client(self):
        with tempfile.TemporaryDirectory() as directory:
            lab, probe = self.fixture(directory)
            probe.journal = {'namespace': 'n', 'class_name': 'linked'}
            lab.clone_claim = Mock(return_value={'spec': {'dataSource': {'name': 'source'}}})
            row = {'volume': 'native', 'volume_uid': 'u'}
            probe.bind = Mock(return_value=row); probe.create = Mock()
            probe.client = Mock(side_effect=RuntimeError('must not reach client before native clone success'))
            lab.get.side_effect = lambda kind, *args: ({'status': {'phase': 'Bound'}} if kind == 'pvc' else
                {'metadata': {'uid': 'u'}, 'status': {'cloneStatus': {'state': 'failed'}}})
            with self.assertRaisesRegex(ValueError, 'linked clone failed'):
                probe.claim('clone', 'source')
            probe.client.assert_not_called()

    def expansion_fixture(self, directory, defect=''):
        lab, probe = self.fixture(directory)
        probe.journal = {'namespace': 'n', 'objects': {'PersistentVolumeClaim/clone': 'c', 'Pod/clone': 'w'},
            'claims': {'clone': {'pv': 'pv', 'pv_uid': 'p', 'volume': 'v', 'volume_uid': 'vuid'}}}
        objects = {
            'pvc': {'metadata': {'uid': 'c'}, 'spec': {'volumeName': 'pv', 'resources': {'requests': {'storage': '2Gi'}}},
                    'status': {'capacity': {'storage': '2Gi'}, 'conditions': None}},
            'pv': {'metadata': {'uid': 'p'}, 'spec': {'csi': {'volumeHandle': 'v'}, 'capacity': {'storage': '2Gi'}}},
            'volumes.longhorn.io': {'metadata': {'uid': 'vuid'}, 'spec': {'dataEngine': 'v2', 'size': str(2*1024**3)}},
            'pod': {'metadata': {'uid': 'w'}}}
        lab.get.side_effect = lambda kind, *args: copy.deepcopy(objects[kind])
        lab.capacity_bytes = lambda value: int(value[:-2])*1024**3 if value.endswith('Gi') else int(value)
        def resize(claim, size):
            self.assertIn('expansions', probe.journal)
            self.assertEqual(size, '4Gi')
            objects['pvc']['spec']['resources']['requests']['storage'] = size
            objects['pvc']['status']['capacity']['storage'] = size
            objects['pv']['spec']['capacity']['storage'] = size
            objects['volumes.longhorn.io']['spec']['size'] = str(4*1024**3)
            if defect == 'replacement': objects['pv']['metadata']['uid'] = 'foreign'
            if defect == 'binding': objects['pvc']['spec']['volumeName'] = 'foreign'
            if defect == 'unexpanded': objects['pv']['spec']['capacity']['storage'] = '2Gi'
        lab.resize_claim = Mock(side_effect=resize)
        def wait(stage, timeout, check):
            value = check()
            if not value: raise TimeoutError(stage)
            return value
        lab.wait.side_effect = wait
        lab.expansion_probe_program = Mock(return_value='fsync beyond original capacity')
        measured = {'capacity_bytes': 4*1024**3, 'offset': 2*1024**3+1024**2, 'sha256': 'a'*64}
        if defect == 'capacity': measured['capacity_bytes'] = 2*1024**3
        if defect == 'offset': measured['offset'] = 0
        if defect == 'digest': measured['sha256'] = 'missing'
        lab.kubectl.return_value = SimpleNamespace(stdout=json.dumps(measured))
        lab.healthy_replicas = Mock(return_value=True); lab.replica_topology = Mock(return_value={'healthy': True})
        probe.io = Mock()
        return lab, probe

    def test_linked_expansion_preserves_binding_and_requires_real_new_capacity_io(self):
        with tempfile.TemporaryDirectory() as directory:
            lab, probe = self.expansion_fixture(directory)
            with redirect_stdout(io.StringIO()): probe.expand('clone', 'clone', 'original-data')
            probe.io.assert_called_once_with('clone', 'original-data', read=True)
            self.assertEqual(4*1024**3, probe.journal['expansions']['clone']['to_bytes'])
            self.assertEqual('a'*64, probe.journal['expansions']['clone']['sha256'])
            lab.remove.assert_not_called()

    def test_expansion_replacement_unconverged_capacity_and_false_data_proofs_stop(self):
        for defect in ('replacement', 'binding', 'unexpanded', 'capacity', 'offset', 'digest'):
            with self.subTest(defect=defect), tempfile.TemporaryDirectory() as directory:
                lab, probe = self.expansion_fixture(directory, defect)
                with self.assertRaises((ValueError, TimeoutError)):
                    probe.expand('clone', 'clone', 'original-data')
                self.assertNotIn('sha256', probe.journal['expansions']['clone'])
                lab.remove.assert_not_called()

    def test_expanded_bytes_are_independently_read_after_cross_node_remount(self):
        with tempfile.TemporaryDirectory() as directory:
            lab, probe = self.fixture(directory)
            probe.journal = {'namespace': 'n', 'expansions': {'clone': {'offset': 2*1024**3+1024**2, 'sha256': 'a'*64}}}
            lab.kubectl.return_value = SimpleNamespace(stdout='a'*64+'\n')
            with redirect_stdout(io.StringIO()): probe.expanded_readback('clone', 'clone-remount')
            self.assertIn('clone-remount', lab.kubectl.call_args.args)
            lab.kubectl.return_value = SimpleNamespace(stdout='b'*64+'\n')
            with self.assertRaisesRegex(ValueError, 'changed across node remount'):
                probe.expanded_readback('clone', 'clone-remount')

    def test_real_shell_stops_at_business_or_marker_failure_and_preserves_cleanup(self):
        for fault in ('', 'business-rc', 'missing-marker', 'cleanup-rc', 'cleanup-only'):
            with self.subTest(fault=fault),tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                for name in ('.venv/bin','tests/helpers','kube-bin'): (root/name).mkdir(parents=True)
                driver = root/'.venv/bin/python'
                driver.write_text('''#!/usr/bin/env python3
import os,signal,sys,time
from pathlib import Path
root=Path(__file__).resolve().parents[2];mode=sys.argv[2];fault=os.environ.get('LINKED_FAULT','')
with (root/'calls').open('a') as log:log.write(mode+'\\n')
if mode=='relay-maintain':
 signal.signal(signal.SIGTERM,lambda *_:sys.exit(0))
 while True:time.sleep(0.01)
if mode=='business':
 if fault=='business-rc':sys.exit(17)
 print('LONGHORN_V2_LINKED_FIXTURES_CLEAN_PASS')
 if fault!='missing-marker':print('LONGHORN_V2_LINKED_NESTED_BUSINESS_PASS')
if mode=='clean' and fault=='cleanup-rc':sys.exit(19)
'''); driver.chmod(0o755)
                kubectl = root/'kube-bin/kubectl'; kubectl.write_text('#!/bin/sh\nexit 0\n'); kubectl.chmod(0o755)
                script = (ROOT/'tests/helpers/longhorn-v2-linked-clones.sh').read_text().replace('BASE=/usr/local/kubeauto','BASE='+str(root))
                path = root/'run.sh'; path.write_text(script)
                mode = 'v2-linked-clone-cleanup' if fault == 'cleanup-only' else 'v2-linked-clone-qualification'
                result = subprocess.run(['bash',str(path),mode],text=True,capture_output=True,timeout=10,
                    env=dict(os.environ,LONGHORN_PARENT_LEASE_HELD='yes',LINKED_FAULT=fault))
                calls = (root/'calls').read_text().splitlines()
                for mode in ('clean','relay-clean','verify-core'): self.assertIn(mode,calls)
                if fault == 'cleanup-only':
                    self.assertEqual(0,result.returncode,result.stderr)
                    self.assertNotIn('business',calls)
                    self.assertLess(calls.index('clean'),calls.index('volume-baseline'))
                    self.assertIn('LONGHORN_V2_LINKED_CLONE_CLEANUP_PASS',result.stdout)
                    self.assertNotIn('LONGHORN_V2_LINKED_CLONE_QUALIFICATION_PASS',result.stdout)
                elif fault:
                    self.assertNotEqual(0,result.returncode)
                    self.assertNotIn('LONGHORN_V2_LINKED_CLONE_QUALIFICATION_PASS',result.stdout)
                    if fault=='business-rc': self.assertEqual(17,result.returncode)
                    if fault=='cleanup-rc': self.assertEqual(19,result.returncode)
                else:
                    self.assertEqual(0,result.returncode,result.stderr)
                    self.assertTrue(result.stdout.rstrip().endswith('LONGHORN_V2_LINKED_CLONE_QUALIFICATION_PASS'))

    def test_fixed_qualified_and_recovery_routes_keep_their_own_markers(self):
        runner=(ROOT/'tests/helpers/longhorn-runner.sh').read_text()
        live=(ROOT/'tests/helpers/longhorn-live.sh').read_text()
        for mode in ('v2-linked-clone-qualification','v2-linked-clone-cleanup'):
            self.assertGreaterEqual(runner.count(mode),4)
            self.assertIn('--longhorn-'+mode+') longhorn_launch '+mode+' LONGHORN_'+mode.upper().replace('-','_')+'_PASS ;;',runner)
            self.assertIn(mode,live)
        self.assertIn('longhorn-v2-linked-clones.sh',live)
        self.assertIn('tests.unit.test_longhorn_v2_linked_clones',(ROOT/'tests/helpers/longhorn-static.sh').read_text())


if __name__ == '__main__': unittest.main()
