import copy
from contextlib import nullcontext
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import yaml
from tests.unit.test_longhorn_delivery import configuration, product
from tests.unit.test_longhorn_v2 import v2_configuration

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('dr_install_contract', ROOT/'tests/helpers/longhorn-v2-dr-target-install.py')
gate = importlib.util.module_from_spec(spec); spec.loader.exec_module(gate)
NODE = '192.168.47.149'


def single_config(engine):
    c = v2_configuration() if engine == 'v2' else configuration()
    c.update(longhorn_default_replica_count=1, longhorn_inventory_nodes=[NODE], longhorn_client_nodes=[NODE],
             longhorn_storage_nodes=[NODE] if engine == 'v1' else [])
    if engine == 'v2':
        disk = next(iter(c['longhorn_block_disks'].values()))
        c.update(longhorn_v2_nodes=[NODE], longhorn_block_disks={NODE: disk})
    c['longhorn_storage_classes'][0]['replicas'] = 1
    return c


class SingleReplicaProductContracts(unittest.TestCase):
    def test_explicit_one_replica_is_valid_and_zero_or_insufficient_nodes_still_refuse(self):
        for engine in ('v1','v2'):
            c = single_config(engine)
            with patch.object(product,'run',side_effect=AssertionError('commands forbidden')):
                product.validate(c)
                for key in ('default','class','nodes'):
                    wrong=copy.deepcopy(c)
                    if key=='default':wrong['longhorn_default_replica_count']=0
                    elif key=='class':wrong['longhorn_storage_classes'][0]['replicas']=0
                    else:wrong['longhorn_default_replica_count']=2
                    with self.subTest(engine=engine,key=key),self.assertRaises(ValueError):product.validate(wrong)
            self.assertEqual({'v1':'1','v2':'1'},product.chart_values(c)['defaultSettings']['defaultReplicaCount'])
            sc=product.storage_class(c['longhorn_storage_classes'][0])
            self.assertEqual('1',sc['parameters']['numberOfReplicas'])

    def test_single_node_uses_one_csi_controller_and_three_node_defaults_remain_unchanged(self):
        for engine in ('v1','v2'):
            c=single_config(engine);values=product.chart_values(c)
            self.assertEqual('hard',values['csi']['podAntiAffinityPreset'])
            for key in ('attacherReplicaCount','provisionerReplicaCount','resizerReplicaCount','snapshotterReplicaCount'):
                self.assertEqual(1,values['csi'][key])
            normal=v2_configuration() if engine=='v2' else configuration()
            self.assertEqual({'kubeletRootDir':'/var/lib/kubelet','podAntiAffinityPreset':'hard'},product.chart_values(normal)['csi'])
            self.assertEqual({'v1':'3','v2':'3'},product.chart_values(normal)['defaultSettings']['defaultReplicaCount'])
            self.assertEqual(3,normal['longhorn_default_replica_count'])

    def test_official_chart_renders_one_replica_engine_defaults_and_csi_environment(self):
        helm=shutil.which('helm')
        if not helm:self.skipTest('fixed Helm is required')
        c=single_config('v2');product.validate(c)
        with tempfile.TemporaryDirectory() as directory:
            values=Path(directory)/'values.json';values.write_text(json.dumps(product.chart_values(c)))
            rendered=subprocess.check_output([helm,'template','longhorn',str(product.FILES/'longhorn-1.12.1.tgz'),
                '-n',product.NAMESPACE,'-f',str(values)],text=True)
        objects=[obj for obj in yaml.safe_load_all(rendered) if obj]
        driver=next(o for o in objects if o['kind']=='Deployment' and o['metadata']['name']=='longhorn-driver-deployer')
        env={r['name']:r.get('value') for r in driver['spec']['template']['spec']['containers'][0]['env']}
        for key in ('ATTACHER','PROVISIONER','RESIZER','SNAPSHOTTER'):
            self.assertEqual('1',env['CSI_'+key+'_REPLICA_COUNT'])
        settings=next(o for o in objects if o['kind']=='ConfigMap' and o['metadata']['name']=='longhorn-default-setting')
        self.assertEqual({'v1':'1','v2':'1'},json.loads(yaml.safe_load(settings['data']['default-setting.yaml'])['default-replica-count']))


class TargetInstallationContracts(unittest.TestCase):
    def test_core_current_accepts_fresh_ephemeral_cluster_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            evidence = Path(directory) / 'core.json'
            smoke = hashlib.sha256((ROOT / 'tests/helpers/kubernetes-production-smoke.sh').read_bytes()).hexdigest()
            evidence.write_text(json.dumps({
                'status': 'pass', 'rc': 0, 'failure_markers': 0,
                'baseline_identity': {'run_id': 'historical', 'cluster_uid': 'old',
                                      'node_uids': {NODE: 'old-node'}},
                'frozen_inputs': {},
            }))
            lab = SimpleNamespace(
                core_identity=Mock(),
                owner=lambda: {'run_id': 'current', 'cluster_uid': 'new',
                               'node_uids': {NODE: 'new-node'},
                               'target_core_smoke_sha256': smoke},
            )
            install = SimpleNamespace(qualification_input_current=Mock(return_value=True))
            with patch.object(gate, 'BASE', ROOT), \
                    patch.object(gate, 'evidence_path', return_value=evidence), \
                    patch.object(gate, 'load', return_value=install):
                gate.core_current(lab)
            install.qualification_input_current.assert_not_called()

    def test_remount_deletion_refreshes_resource_version_and_refuses_a_replaced_writer(self):
        for replaced in (False, True):
            with self.subTest(replaced=replaced), tempfile.TemporaryDirectory() as directory:
                objects = {}; deleted = []; writer_reads = 0
                state = {'run_id': 'confirmed-data'}
                def create(journal, record, obj):
                    obj = copy.deepcopy(obj); name = obj['metadata']['name']; kind = obj['kind']
                    obj['metadata'].update(uid=kind+'/'+name, resourceVersion='1')
                    journal['objects'][kind+'/'+name] = obj['metadata']['uid']
                    response = copy.deepcopy(obj)
                    obj['metadata']['resourceVersion'] = '2'
                    obj['status'] = {'phase': 'Bound', 'conditions': [{'type':'Ready','status':'True'}]}
                    objects[name] = obj
                    return response
                def get(kind, name='', namespace=''):
                    nonlocal writer_reads
                    if kind == 'storageclass':
                        return {'provisioner':'driver.longhorn.io','reclaimPolicy':'Retain',
                                'parameters':{'dataEngine':'v2','numberOfReplicas':'1'}}
                    if kind == 'namespace': return None
                    if kind == 'volumes.longhorn.io': return {'status':{'state':'detached'}}
                    if name == 'writer':
                        writer_reads += 1
                        if replaced and writer_reads > 1:
                            obj = copy.deepcopy(objects[name]);obj['metadata']['uid']='replacement'
                            return obj
                    return copy.deepcopy(objects.get(name))
                def remove(obj):
                    current = objects[obj['metadata']['name']]
                    # Model the real API's UID AND resourceVersion preconditions.
                    self.assertEqual(current['metadata'], obj['metadata'])
                    deleted.append(obj); del objects[obj['metadata']['name']]
                def kubectl(*args, **kwargs):
                    if args[0] == 'create': return SimpleNamespace(stdout=json.dumps({'metadata':{'uid':'namespace'}}))
                    import hashlib
                    return SimpleNamespace(stdout=hashlib.sha256(state['run_id'].encode()*262144).hexdigest())
                lab=SimpleNamespace(DIRECTORY=Path(directory),owner=lambda:state,get=get,kubectl=kubectl,
                    write_private=Mock(),scoped_cleanup=lambda cleanup:nullcontext(),cleanup_business=Mock(),
                    namespace_defaults=lambda ns:['default'],create_business_object=create,
                    wait=lambda label,timeout,predicate:self.assertTrue(predicate()),
                    bind_business_volume=lambda journal,record:journal.update(volume='volume',volume_uid='original'),
                    client_pod=lambda ns,name,claim,node:{'kind':'Pod','metadata':{'name':name,'namespace':ns}},
                    payload_program=lambda *a,**kw:'payload',remove=remove)
                with patch.object(gate,'health'),patch.object(gate,'replica_ready',return_value=True):
                    if replaced:
                        with self.assertRaisesRegex(ValueError,'writer.*replaced'):gate.business(lab,42)
                        self.assertFalse(deleted)
                    else:
                        gate.business(lab,42)
                        self.assertEqual('2',deleted[0]['metadata']['resourceVersion'])

    def test_owned_core_admission_keeps_foreign_host_and_used_disk_refusals(self):
        host = {'machine_id': 'approved', 'hostname': 'longhorn-node-149', 'ceph_files': [],
                'hugepages_2mi': '0', 'services': {'docker': {'rc': 3}, 'ceph.target': {'rc': 3}}}
        state = {'raw_disks': {NODE: {'machine_id': 'approved', 'identity': 'raw-id',
                                    'path': '/dev/disk/by-id/wwn-approved', 'size_bytes': 100}}}
        assessment = {'blockers': ['existing-host-state-requires-ownership-check'], 'candidates': []}
        pool = SimpleNamespace(PROBE='read-only', assess=Mock(side_effect=lambda _: copy.deepcopy(assessment)))
        target = SimpleNamespace(NODE=NODE, disk_identity=Mock(return_value=('raw-id', 100, '/dev/disk/by-id/wwn-approved')))
        lab = SimpleNamespace(owner=lambda: state, get=lambda *a: None,
                              remote=Mock(side_effect=lambda *a: SimpleNamespace(stdout=json.dumps(host))))
        with patch.object(gate, 'core_current'), patch.object(gate, 'load', return_value=pool):
            gate.host_inputs(target, lab)
            for field, value in (('machine_id', 'foreign'), ('ceph_files', ['foreign']), ('hugepages_2mi', '1024')):
                original = host[field]; host[field] = value
                with self.subTest(field=field), self.assertRaises(ValueError): gate.host_inputs(target, lab)
                host[field] = original
            assessment['blockers'].append('expected-one-unused-100GiB-disk')
            with self.assertRaises(ValueError): gate.host_inputs(target, lab)

    def test_configuration_exercises_real_product_validator_and_pins_only_target_disk(self):
        with tempfile.TemporaryDirectory() as directory:
            base=Path(directory); c=single_config('v2');c.update(longhorn_install='no')
            (base/'config.yml').write_text(yaml.safe_dump(c))
            state={'raw_disks':{NODE:{'path':'/dev/disk/by-id/wwn-approved','identity':'approved'}}}
            lab=SimpleNamespace(DIRECTORY=base,NODES=(NODE,),owner=lambda:state,get=lambda *a:None,prepare_credentials=Mock())
            with patch.object(gate,'core_current'),patch.object(gate,'verify_resources'):
                gate.configure(lab)
            actual=yaml.safe_load((base/'config.yml').read_text());actual['longhorn_install']='yes'
            product.validate(actual)
            self.assertEqual([NODE],actual['longhorn_v2_nodes']);self.assertEqual(1,actual['longhorn_default_replica_count'])
            self.assertEqual(1,actual['longhorn_storage_classes'][0]['replicas'])
            self.assertEqual('v2',actual['longhorn_storage_classes'][0]['data_engine'])
            self.assertEqual('aio',actual['longhorn_block_disks'][NODE][0]['disk_driver'])
            lab.prepare_credentials.assert_called_once_with()

    def replica_fixture(self):
        volume={'metadata':{'uid':'original'},'spec':{'dataEngine':'v2','numberOfReplicas':1},'status':{'robustness':'healthy'}}
        replica={'spec':{'volumeName':'volume','dataEngine':'v2','nodeID':'longhorn-node-149','diskID':'raw-uuid',
                         'healthyAt':'now','failedAt':''},'status':{'currentState':'running'}}
        native={'status':{'diskStatus':{'kubeauto-block':{'diskUUID':'raw-uuid'}}}}
        replicas=[replica]
        def get(kind,*args,**kwargs):
            return {'items':replicas} if kind=='replicas.longhorn.io' else native if kind=='nodes.longhorn.io' else volume
        return SimpleNamespace(get=get),volume,replica,replicas

    def test_single_replica_requires_original_volume_correct_engine_node_and_raw_disk(self):
        lab,volume,replica,replicas=self.replica_fixture();journal={'volume':'volume','volume_uid':'original'}
        self.assertTrue(gate.replica_ready(lab,journal))
        for field,value in (('dataEngine','v1'),('nodeID','source-node'),('diskID','replaced')):
            original=replica['spec'][field];replica['spec'][field]=value
            with self.subTest(field=field),self.assertRaises(ValueError):gate.replica_ready(lab,journal)
            replica['spec'][field]=original
        volume['metadata']['uid']='replacement'
        with self.assertRaises(ValueError):gate.replica_ready(lab,journal)
        volume['metadata']['uid']='original';replicas.append(copy.deepcopy(replica))
        with self.assertRaises(ValueError):gate.replica_ready(lab,journal)

    def test_resource_recovery_refuses_ambiguous_write_before_resize_or_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'resources.json';path.write_text('{}')
            lab=SimpleNamespace(write_private=Mock())
            resources=SimpleNamespace(no_storage=Mock(),record_path=lambda l:path,
                read_record=lambda l:{'hosts':{NODE:{'phase':'write-intent'}}},probe=Mock(),refresh_kubelet=Mock())
            with patch.object(gate,'load',return_value=resources),self.assertRaisesRegex(ValueError,'ambiguous'):
                gate.restore_resources(lab)
            resources.probe.assert_not_called();resources.refresh_kubelet.assert_not_called();lab.write_private.assert_not_called()

    def test_recovery_accepts_absent_empty_namespace_only_after_native_absence_proof(self):
        with tempfile.TemporaryDirectory() as directory:
            state = {'namespace_uid': 'old', 'host_state': {'nodes': {NODE: {}}}, 'healthy': False}
            Path(directory, 'config.yml').write_text(yaml.safe_dump({'longhorn_storage_classes': [{'name': 'longhorn'}]}))
            lab = SimpleNamespace(
                DIRECTORY=Path(directory), STATE=Path(directory)/'state.json', NODES=(NODE,), owner=lambda: state,
                core_identity=Mock(), get=lambda kind, *args, **kwargs: None if kind == 'namespace' else {'items': []},
                execute=Mock(return_value=SimpleNamespace(stdout='[]')),
                write_private=Mock())
            with patch.object(gate, 'restore_resources') as resources, \
                    patch.object(gate, 'restore_hosts') as hosts:
                gate.recover(lab, 0)
            resources.assert_called_once_with(lab); hosts.assert_called_once_with(lab)
            self.assertNotIn('namespace_uid', state)
            self.assertTrue(state['native_uninstalled'])

    def test_target_factory_preserves_target_supervision_and_recovery_context(self):
        lab=SimpleNamespace();target=SimpleNamespace(fixture=lambda:lab,supervise=Mock())
        with patch.object(gate,'load',return_value=target),patch.object(gate,'core_current') as core,patch.object(gate,'restore_hosts') as hosts:
            actual,got=gate.fixture();self.assertIs(got,lab)
            lab.verify_core_smoke();core.assert_called_once_with(lab)
            lab.restore_hosts();hosts.assert_called_once_with(lab)
            lab.supervise_core(42,'longhorn-setup');target.supervise.assert_called_once_with(lab,42,'longhorn-setup')
            with self.assertRaises(ValueError):lab.supervise_core(42,'longhorn-setup',expected_refusal='unowned')

    def test_fixed_entry_and_static_include_installation_without_changing_source_routes(self):
        runner=(ROOT/'tests/helpers/longhorn-runner.sh').read_text();static=(ROOT/'tests/helpers/longhorn-static.sh').read_text()
        self.assertIn('--longhorn-v2-dr-target-install-qualification) longhorn_launch v2-dr-target-install-qualification LONGHORN_V2_DR_TARGET_INSTALL_QUALIFICATION_PASS',runner)
        self.assertIn('tests.unit.test_longhorn_v2_dr_target_install',static)
        live=(ROOT/'tests/helpers/longhorn-live.sh').read_text()
        self.assertIn('exec bash "$BASE/tests/helpers/longhorn-v2-dr-target-install.sh" "$MODE"',live)
        self.assertIn('--longhorn-v2-aio-install-qualification)',runner)

    def test_core_recovery_has_a_fixed_exit_after_native_install_recovery(self):
        helper=(ROOT/'tests/helpers/longhorn-v2-dr-target-core-recovery.sh').read_text()
        runner=(ROOT/'tests/helpers/longhorn-runner.sh').read_text()
        live=(ROOT/'tests/helpers/longhorn-live.sh').read_text()
        self.assertIn("not state.get('native_uninstalled')",helper)
        self.assertIn('lab=target.fixture(); state=lab.owner()',helper)
        self.assertNotIn('_, lab=target.fixture()',helper)
        self.assertIn('core-cleanup',helper)
        self.assertIn('--longhorn-v2-dr-target-core-recovery) longhorn_launch v2-dr-target-core-recovery LONGHORN_V2_DR_TARGET_CORE_RECOVERY_PASS',runner)
        self.assertIn('exec bash "$BASE/tests/helpers/longhorn-v2-dr-target-core-recovery.sh" "$MODE"',live)


class TargetInstallShellContracts(unittest.TestCase):
    def run_shell(self,directory,fault='',mode='v2-dr-target-install-qualification'):
        base=Path(directory)
        for name in ('.venv/bin','tests/helpers','kube-bin','clusters/longhorn-v2-dr-target'):(base/name).mkdir(parents=True,exist_ok=True)
        (base/'control-config').write_text('original-context')
        driver=base/'.venv/bin/python'
        driver.write_text('''#!/usr/bin/env python3
import os,signal,sys,time
from pathlib import Path
base=Path(__file__).resolve().parents[2];helper,mode=sys.argv[1:3]
with (base/'calls').open('a') as f:f.write(mode+'\\n')
fault=os.environ.get('DR_INSTALL_FAULT','')
if mode==fault:sys.exit(7)
if mode=='source-boundary':
 n=(base/'calls').read_text().splitlines().count(mode)
 print('LONGHORN_V2_DR_SOURCE_BOUNDARY sha256='+('changed' if fault=='source-drift' and n>1 else 'original'))
elif mode=='relay-maintain':
 signal.signal(signal.SIGTERM,lambda *_:sys.exit(0))
 (base/'relay-ready').touch()
 while True:time.sleep(.01)
elif mode=='recover':
 deadline=time.monotonic()+2
 while not (base/'relay-ready').exists() and time.monotonic()<deadline:time.sleep(.01)
 if not (base/'relay-ready').exists():sys.exit(8)
elif mode=='supervise':
 (base/'control-config').write_text('target-context')
 if fault=='product-failure':sys.exit(7)
elif mode=='business':
 print('LONGHORN_V2_DR_TARGET_FSYNC_REMOUNT_VERIFIED bytes=9437184 sha256='+'a'*64+' replicas=1 cross_node=false')
 if fault!='missing-marker':print('LONGHORN_V2_DR_TARGET_INSTALL_BUSINESS_PASS')
 print('LONGHORN_BUSINESS_FIXTURES_CLEAN_PASS')
''');driver.chmod(0o755)
        kubectl=base/'kube-bin/kubectl';kubectl.write_text('#!/bin/sh\nexit 0\n');kubectl.chmod(0o755)
        text=(ROOT/'tests/helpers/longhorn-v2-dr-target-install.sh').read_text().replace('BASE=/usr/local/kubeauto','BASE='+str(base)).replace('control_config=/root/.kube/config','control_config='+str(base/'control-config'))
        shell=base/'run.sh';shell.write_text(text)
        result=subprocess.run(['bash',str(shell),mode],env=dict(os.environ,
            LONGHORN_PARENT_LEASE_HELD='yes',DR_INSTALL_FAULT=fault),text=True,capture_output=True,timeout=20)
        return result,(base/'calls').read_text().splitlines(),(base/'control-config').read_text()

    def test_recovery_uses_only_native_recovery_and_restores_context_and_source_boundary(self):
        for fault in ('', 'recover', 'relay-clean', 'source-drift'):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as directory:
                result,calls,context=self.run_shell(directory,fault,'v2-dr-target-install-recovery')
                self.assertEqual(1,calls.count('recover'))
                self.assertFalse(set(calls) & {'host-inputs','capture-hosts','configure','prepare-resources','business','health'})
                self.assertEqual('original-context',context)
                self.assertEqual(2,calls.count('source-boundary'))
                if fault:
                    self.assertNotEqual(0,result.returncode)
                    self.assertNotIn('LONGHORN_V2_DR_TARGET_INSTALL_RECOVERY_PASS',result.stdout)
                else:
                    self.assertEqual(0,result.returncode,result.stderr)
                    self.assertIn('LONGHORN_V2_DR_TARGET_INSTALL_RECOVERY_SCOPED_CLEAN_PASS',result.stdout)

    def test_success_restores_context_cleans_business_and_relays_and_checks_both_sources(self):
        with tempfile.TemporaryDirectory() as directory:
            result,calls,context=self.run_shell(directory)
            self.assertEqual(0,result.returncode,result.stderr)
            self.assertIn('LONGHORN_V2_DR_TARGET_INSTALL_QUALIFICATION_SCOPED_CLEAN_PASS',result.stdout)
            self.assertEqual('original-context',context);self.assertEqual(2,calls.count('source-boundary'))
            self.assertLess(calls.index('business-clean'),calls.index('relay-clean'))
            self.assertNotIn('recover',calls)

    def test_product_failure_or_missing_business_marker_recovers_before_transport_cleanup(self):
        for fault in ('product-failure','missing-marker'):
            with self.subTest(fault=fault),tempfile.TemporaryDirectory() as directory:
                result,calls,context=self.run_shell(directory,fault)
                self.assertNotEqual(0,result.returncode);self.assertNotIn('LONGHORN_V2_DR_TARGET_INSTALL_QUALIFICATION_PASS',result.stdout)
                self.assertLess(calls.index('business-clean'),calls.index('recover'))
                self.assertLess(calls.index('recover'),calls.index('relay-clean'))
                self.assertEqual('original-context',context)

    def test_source_drift_or_failed_cleanup_cannot_emit_pass(self):
        for fault in ('source-drift','business-clean','relay-clean'):
            with self.subTest(fault=fault),tempfile.TemporaryDirectory() as directory:
                result,calls,context=self.run_shell(directory,fault)
                self.assertNotEqual(0,result.returncode)
                self.assertNotIn('LONGHORN_V2_DR_TARGET_INSTALL_QUALIFICATION_PASS',result.stdout)
                self.assertEqual('original-context',context)
                if fault=='business-clean':self.assertNotIn('recover',calls)


if __name__=='__main__':unittest.main()
