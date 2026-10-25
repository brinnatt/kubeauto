"""V2 native allocation, asynchronous samples and snapshot-aware discard contracts."""
import copy
from contextlib import redirect_stdout
import importlib.util
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('v2_trim_contract', ROOT/'tests/helpers/longhorn-v2-trim.py')
trim = importlib.util.module_from_spec(spec)
spec.loader.exec_module(trim)


class TrimContracts(unittest.TestCase):
    def fixture(self, directory):
        topology = {'r'+str(i): {'uid':'uid'+str(i), 'node':'longhorn-node-'+str(i)} for i in (146,147,148)}
        volume = {'metadata':{'name':'volume','uid':'volume-uid'},
            'spec':{'dataEngine':'v2','frontend':'blockdev','accessMode':'rwo','encrypted':False},
            'status':{'currentNodeID':'longhorn-node-146'}}
        engine = {'metadata':{'uid':'engine-uid'}, 'spec':{'volumeName':'volume','nodeID':'longhorn-node-146','dataEngine':'v2'},
                  'status':{'currentState':'running','snapshots':{},'rebuildStatus':{}}}
        replicas = [{'metadata':{'name':name,'uid':row['uid']},
            'spec':{'dataEngine':'v2','nodeID':row['node'],'diskID':'disk-'+row['node']}} for name,row in topology.items()]
        nodes = {row['node']:{'metadata':{'uid':'node-'+row['node']},
            'spec':{'disks':{'kubeauto-block':{'diskType':'block','diskDriver':'aio'}}},
            'status':{'diskStatus':{'kubeauto-block':{'diskUUID':'disk-'+row['node'],
                'conditions':[{'type':k,'status':'True'} for k in ('Ready','Schedulable')],
                'storageMaximum':100*1024**3,'storageAvailable':99*1024**3}}}} for row in topology.values()}
        receipt={'nodes':{name:{'name':name,'longhorn_node_uid':node['metadata']['uid'],
            'disks':{'kubeauto-block':{'disk_uuid':'disk-'+name}}} for name,node in nodes.items()}}
        path=Path(directory)/'yml/longhorn/block-disks.json';path.parent.mkdir(parents=True);path.write_text(json.dumps(receipt))
        objects={'volumes.longhorn.io':{'items':[volume]},'engines.longhorn.io':{'items':[engine]},
                 'replicas.longhorn.io':{'items':replicas},'snapshots.longhorn.io':None}
        def get(kind,name='',namespace=''):
            return copy.deepcopy(nodes[name] if kind=='nodes.longhorn.io' else objects[kind])
        def wait(stage,timeout,check):
            for _ in range(10):
                result=check()
                if result:return result
            raise TimeoutError(stage)
        lab=SimpleNamespace(DIRECTORY=Path(directory),get=MagicMock(side_effect=get),
            replica_topology=MagicMock(return_value=topology),healthy_replicas=MagicMock(return_value=list(nodes)),
            wait=MagicMock(side_effect=wait),write_private=MagicMock(),run_requests=MagicMock(),
            bind_snapshots=MagicMock(),remove=MagicMock(),kubectl=MagicMock(return_value=SimpleNamespace(stdout='digest')),
            payload_program=MagicMock(return_value='read-original'))
        journal={'volume':'volume','volume_uid':'volume-uid','namespace':'business'}
        return trim.Probe(lab,journal,topology),lab,volume,engine,replicas,nodes,objects

    def test_native_measurement_uses_lvstore_used_bytes_for_exact_original_replicas(self):
        for defect in ('none','extra-volume','volume-uid','engine','encrypted','frontend','access','topology',
                       'running','v1-engine','expanding','rebuilding','rebuild-error','foreign-snapshot','disk-health'):
            with self.subTest(defect=defect),tempfile.TemporaryDirectory() as directory:
                probe,lab,volume,engine,replicas,nodes,objects=self.fixture(directory)
                if defect=='extra-volume':objects['volumes.longhorn.io']['items'].append(copy.deepcopy(volume))
                if defect=='volume-uid':volume['metadata']['uid']='foreign'
                if defect=='engine':volume['spec']['dataEngine']='v1'
                if defect=='encrypted':volume['spec']['encrypted']=True
                if defect=='frontend':volume['spec']['frontend']='ublk'
                if defect=='access':volume['spec']['accessMode']='rwx'
                if defect=='topology':lab.replica_topology.return_value=None
                if defect=='running':engine['status']['currentState']='stopped'
                if defect=='v1-engine':engine['spec']['dataEngine']='v1'
                if defect=='expanding':engine['status']['isExpanding']=True
                if defect in ('rebuilding','rebuild-error'):
                    engine['status']['rebuildStatus']={'r':{'isRebuilding':defect=='rebuilding','error':'failed' if defect=='rebuild-error' else ''}}
                if defect=='foreign-snapshot':engine['status']['snapshots']={'foreign':{'usercreated':True}}
                if defect=='disk-health':lab.healthy_replicas.return_value=None
                if defect=='none':
                    with redirect_stdout(io.StringIO()):result=probe.sample('baseline')
                    self.assertEqual(result,{name:1024**3 for name in probe.topology})
                    lab.kubectl.assert_not_called()
                else:
                    with self.assertRaises(ValueError):probe.sample('baseline')

    def test_native_disk_sampling_refuses_identity_drift_foreign_consumers_and_invalid_stats(self):
        for defect in ('extra-replica','replica-uid','replica-node','replica-engine','disk-id','node-uid',
                       'disk-uuid','disk-type','driver','ready','negative','larger-than-total','string','bool'):
            with self.subTest(defect=defect),tempfile.TemporaryDirectory() as directory:
                probe,lab,volume,engine,replicas,nodes,objects=self.fixture(directory)
                replica=replicas[0];node=nodes['longhorn-node-146'];status=node['status']['diskStatus']['kubeauto-block']
                config=node['spec']['disks']['kubeauto-block']
                if defect=='extra-replica':replicas.append({'metadata':{'name':'foreign'}})
                if defect=='replica-uid':replica['metadata']['uid']='foreign'
                if defect=='replica-node':replica['spec']['nodeID']='foreign'
                if defect=='replica-engine':replica['spec']['dataEngine']='v1'
                if defect=='disk-id':replica['spec']['diskID']='foreign'
                if defect=='node-uid':node['metadata']['uid']='foreign'
                if defect=='disk-uuid':status['diskUUID']='foreign'
                if defect=='disk-type':config['diskType']='filesystem'
                if defect=='driver':config['diskDriver']='nvme'
                if defect=='ready':status['conditions'][0]['status']='False'
                if defect=='negative':status['storageAvailable']=-1
                if defect=='larger-than-total':status['storageAvailable']=101*1024**3
                if defect=='string':status['storageAvailable']='100'
                if defect=='bool':status['storageAvailable']=True
                with self.assertRaises(ValueError):probe.sample('baseline')

    def test_rejection_preserves_each_public_predicate_before_business_cleanup(self):
        for defect in ('expansion', 'rebuilding', 'rebuild-error', 'snapshot'):
            with self.subTest(defect=defect), tempfile.TemporaryDirectory() as directory:
                probe,lab,volume,engine,*_=self.fixture(directory)
                if defect=='expansion':engine['status']['isExpanding']=True
                if defect in ('rebuilding','rebuild-error'):
                    engine['status']['rebuildStatus']={'r':{'isRebuilding':defect=='rebuilding',
                                                          'error':'private-native-error' if defect=='rebuild-error' else ''}}
                if defect=='snapshot':engine['status']['snapshots']={'foreign':{'usercreated':True}}
                with redirect_stdout(io.StringIO()) as output, self.assertRaises(ValueError):
                    probe.sample('baseline')
                line=output.getvalue().strip()
                self.assertTrue(line.startswith('LONGHORN_V2_TRIM_REJECT_STATE '))
                row=json.loads(line.split(' ',1)[1])
                self.assertEqual(row['engine_uid'],'engine-uid')
                self.assertEqual(row['is_expanding'],defect=='expansion')
                self.assertEqual(row['unowned_snapshots'],['foreign'] if defect=='snapshot' else [])
                if defect in ('rebuilding','rebuild-error'):
                    self.assertEqual(row['replicas'],{'r':{'rebuilding':defect=='rebuilding',
                                                         'error_present':defect=='rebuild-error'}})
                self.assertNotIn('private-native-error',line)
                self.assertNotIn('ALLOCATION',line)

    def test_writable_volume_head_is_not_an_unowned_snapshot(self):
        for foreign in (False, True):
            with self.subTest(foreign=foreign), tempfile.TemporaryDirectory() as directory:
                probe,lab,volume,engine,*_=self.fixture(directory)
                engine['status']['snapshots']={'volume-head':{'usercreated':True}}
                if foreign:engine['status']['snapshots']['foreign']={'usercreated':True}
                with redirect_stdout(io.StringIO()) as output:
                    if foreign:
                        with self.assertRaises(ValueError):probe.sample('baseline')
                        state=json.loads(output.getvalue().strip().split(' ',1)[1])
                        self.assertEqual(state['unowned_snapshots'],['foreign'])
                    else:
                        self.assertEqual(probe.sample('baseline'),{name:1024**3 for name in probe.topology})
                        self.assertNotIn('REJECT_STATE',output.getvalue())

    def test_stability_window_spans_two_native_monitor_periods_and_resets_on_change(self):
        with tempfile.TemporaryDirectory() as directory:
            probe,lab,*_=self.fixture(directory)
            probe.sample=MagicMock(side_effect=[{'r':1},{'r':1},{'r':2},{'r':2},{'r':2},{'r':2}])
            with patch.object(trim.time,'monotonic',side_effect=[0,0,0,30,60,64,65,95,125]):
                self.assertEqual(probe.settled('baseline'),{'r':2})
            self.assertEqual(probe.sample.call_count,6)
            self.assertEqual(lab.wait.call_args.args[:2],('v2-trim-settled-baseline',300))

    def test_prepare_does_not_use_v1_flag_or_purge_and_waits_for_native_snapshot_removal(self):
        for defect in ('none','no-allocation','no-guest-free','lost-retention','snapshot-uid','snapshot-not-ready','replacement'):
            with self.subTest(defect=defect),tempfile.TemporaryDirectory() as directory:
                probe,lab,volume,engine,replicas,nodes,objects=self.fixture(directory)
                base={name:1024**2 for name in probe.topology};written={name:value+trim.SIZE for name,value in base.items()}
                probe.action=MagicMock();probe.settled=MagicMock(side_effect=[base,base if defect=='lost-retention' else written])
                probe.sample=MagicMock(return_value=base if defect=='no-allocation' else written)
                probe.file=MagicMock(side_effect=[{'free_bytes':0,'sha256':'filler'},
                    {'free_bytes':0 if defect=='no-guest-free' else trim.SIZE}])
                def bind(journal):
                    journal['native_snapshots'][probe.snapshot]['uid']='snapshot-uid'
                    objects['snapshots.longhorn.io']={'metadata':{'uid':'foreign' if defect=='snapshot-uid' else 'snapshot-uid'},
                        'status':{'readyToUse':defect!='snapshot-not-ready'}}
                lab.bind_snapshots.side_effect=bind
                def remove(snapshot):
                    objects['snapshots.longhorn.io']=None if defect!='replacement' else {'metadata':{'uid':'replacement'}}
                lab.remove.side_effect=remove
                if defect=='none':
                    proof=probe.prepare(12)
                    self.assertEqual(proof['snapshot']['uid'],'snapshot-uid')
                    self.assertEqual(proof['guest_freed_bytes'],trim.SIZE)
                    self.assertEqual(probe.action.call_args_list,[unittest.mock.call('trimFilesystem')]*2)
                    lab.kubectl.assert_not_called()
                else:
                    with self.assertRaises((ValueError,TimeoutError)):probe.prepare(12)

    def test_finish_requires_original_deleted_snapshot_actual_reclaim_and_confirmed_data(self):
        for defect in ('none','uid','request','still-exists','no-reclaim','data'):
            with self.subTest(defect=defect),tempfile.TemporaryDirectory() as directory:
                probe,lab,volume,engine,replicas,nodes,objects=self.fixture(directory)
                probe.snapshot='trim-owned';probe.journal['native_snapshots']={'trim-owned':{'uid':'snapshot-uid',
                    'request':{'kind':'snapshot','name':'trim-owned','volume':'volume'}}}
                proof={'snapshot':{'name':'trim-owned','uid':'foreign' if defect=='uid' else 'snapshot-uid'},
                    'retained':{name:2*trim.SIZE for name in probe.topology}}
                if defect=='request':probe.journal['native_snapshots']['trim-owned']['request']['volume']='foreign'
                if defect=='still-exists':objects['snapshots.longhorn.io']={'metadata':{'uid':'snapshot-uid'}}
                probe.action=MagicMock();probe.sample=MagicMock(return_value={name:(2*trim.SIZE if defect=='no-reclaim' else trim.SIZE) for name in probe.topology})
                if defect=='data':lab.kubectl.return_value.stdout='corrupt'
                with redirect_stdout(io.StringIO()) as output:
                    if defect=='none':
                        probe.finish(proof,'payload','digest')
                        self.assertIn('LONGHORN_V2_TRIM_RESULT ',output.getvalue())
                        self.assertEqual(proof['confirmed_sha256'],'digest')
                    else:
                        with self.assertRaises((ValueError,TimeoutError)):probe.finish(proof,'payload','digest')
                        self.assertNotIn('LONGHORN_V2_TRIM_RESULT ',output.getvalue())
                if defect in ('uid','request','still-exists'):probe.action.assert_not_called()


if __name__=='__main__':unittest.main()
