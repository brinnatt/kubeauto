from contextlib import nullcontext
import copy
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
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location('maintenance', ROOT/'tests/helpers/longhorn-maintenance.py')
M = importlib.util.module_from_spec(SPEC); SPEC.loader.exec_module(M)


def obj(kind, name, uid, **extra):
    return {'kind': kind, 'metadata': {'name': name, 'uid': uid, 'resourceVersion': '7'}, **extra}


def immediate(label, timeout, check):
    value = check()
    if not value: raise RuntimeError('not converged: '+label)
    return value


class MaintenanceContracts(unittest.TestCase):
    def node_fixture(self):
        node = obj('Node', 'worker', 'node-uid', spec={})
        row = {'nodes': {'worker': {'uid': 'node-uid', 'taints': [], 'unschedulable': False}}}
        return node, row

    def test_node_identity_preserves_user_taints_but_accepts_automatic_cordon_taint(self):
        node, row = self.node_fixture(); lab = SimpleNamespace(get=lambda *a: node)
        self.assertIs(M.node(lab, row, 'worker'), node)
        node['spec'].update(unschedulable=True, taints=[{'key': 'node.kubernetes.io/unschedulable', 'effect': 'NoSchedule'}])
        self.assertIs(M.node(lab, row, 'worker'), node)
        for mutation in (lambda: node['metadata'].update(uid='replacement'),
                         lambda: node['spec'].update(taints=[{'key': 'foreign', 'effect': 'NoSchedule'}])):
            node, row = self.node_fixture(); mutation()
            with self.assertRaisesRegex(ValueError, 'identity or taints'):
                M.node(lab, row, 'worker')
        with self.assertRaisesRegex(ValueError, 'outside'):
            M.node(lab, row, 'foreign')

    def test_scheduling_uses_uid_and_resource_version_without_touching_taints(self):
        node, row = self.node_fixture(); calls = []
        def kubectl(*args, stdin):
            calls.append((args, json.loads(stdin))); node['spec']['unschedulable'] = True
        lab = SimpleNamespace(get=lambda *a: node, kubectl=kubectl)
        M.scheduling(lab, row, 'worker', True)
        patch_ops = calls[0][1]
        self.assertEqual(patch_ops[:2], [{'op': 'test', 'path': '/metadata/uid', 'value': 'node-uid'},
                                        {'op': 'test', 'path': '/metadata/resourceVersion', 'value': '7'}])
        self.assertEqual(patch_ops[2], {'op': 'add', 'path': '/spec/unschedulable', 'value': True})
        M.scheduling(lab, row, 'worker', True); self.assertEqual(len(calls), 1)

    def test_scale_requires_original_volume_and_healthy_independent_topology(self):
        volume = obj('Volume', 'volume', 'volume-uid', spec={'numberOfReplicas': 3})
        lab = SimpleNamespace(get=lambda *a: volume, kubectl=MagicMock(), wait=immediate,
                              replica_topology=MagicMock(return_value={'replica': {'node': 'worker'}}))
        business = {'volume': 'volume', 'volume_uid': 'volume-uid'}
        M.scale(lab, business, 1)
        operations = json.loads(lab.kubectl.call_args.kwargs['stdin'])
        self.assertEqual(operations[0]['value'], 'volume-uid')
        self.assertEqual(operations[1]['value'], 3)
        self.assertEqual(operations[2]['value'], 1)
        lab.replica_topology.assert_called_once_with(business, 1)
        volume['metadata']['uid'] = 'foreign'
        with self.assertRaisesRegex(ValueError, 'identity'):
            M.scale(lab, business, 3)

    def balance_fixture(self, base, original='ignored'):
        volume = obj('Volume', 'volume', 'volume-uid', spec={
            'numberOfReplicas': 3, 'replicaAutoBalance': original, 'dataEngine': 'v1'})
        business = {'volume': 'volume', 'volume_uid': 'volume-uid'}
        row = {'run_id': 'owned'}; calls = []
        def kubectl(*args, stdin):
            self.assertEqual(args[:5], ('-n', 'longhorn-system', 'patch', 'volumes.longhorn.io', 'volume'))
            operations = json.loads(stdin); calls.append(operations)
            for operation in operations:
                key = operation['path'].rsplit('/', 1)[1]
                source = volume['metadata'] if key == 'uid' else volume['spec']
                if operation['op'] == 'test': self.assertEqual(source[key], operation['value'])
                else: source[key] = operation['value']
        def topology(journal, count):
            # Fixed upstream cleanup does not remove healthy excess replicas
            # while the resolved policy is disabled.
            if count < 3 and volume['spec']['replicaAutoBalance'] in ('ignored', 'disabled'):
                return None
            return {'replica-'+str(i): {'node': 'worker-'+str(i)} for i in range(count)}
        lab = SimpleNamespace(DIRECTORY=base, get=lambda *a: copy.deepcopy(volume),
            kubectl=kubectl, wait=immediate, replica_topology=topology,
            write_private=lambda path, value: path.write_text(json.dumps(value)))
        return lab, business, row, volume, calls

    def test_native_reduction_needs_temporary_per_volume_policy_then_restores_original(self):
        for original in ('ignored', 'disabled', 'least-effort', 'best-effort'):
            with self.subTest(original=original), tempfile.TemporaryDirectory() as directory:
                lab, business, row, volume, calls = self.balance_fixture(Path(directory), original)
                if original in ('ignored', 'disabled'):
                    with self.assertRaisesRegex(RuntimeError, 'not converged'):
                        M.scale(lab, business, 1)
                    volume['spec']['numberOfReplicas'] = 3
                M.temporary_replica_balance(lab, business, row)
                self.assertEqual(json.loads((lab.DIRECTORY/M.RECORD).read_text())['replica_balance']['original'], original)
                self.assertEqual(len(M.scale(lab, business, 1)), 1)
                self.assertEqual(len(M.scale(lab, business, 3)), 3)
                M.restore_replica_balance(lab, business, row)
                self.assertEqual(volume['spec']['replicaAutoBalance'], original)
                self.assertEqual(volume['spec']['numberOfReplicas'], 3)
                self.assertTrue(all(ops[0] == {'op': 'test', 'path': '/metadata/uid', 'value': 'volume-uid'} for ops in calls))
                self.assertTrue(all(op['path'] in ('/metadata/uid', '/spec/numberOfReplicas', '/spec/replicaAutoBalance') for ops in calls for op in ops))

    def test_policy_intent_precedes_lost_patch_response_and_original_restoration_is_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            lab, business, row, volume, calls = self.balance_fixture(Path(directory))
            execute = lab.kubectl
            def lost(*args, **kwargs):
                self.assertTrue((lab.DIRECTORY/M.RECORD).exists())
                execute(*args, **kwargs)
                raise TimeoutError('response lost after server applied patch')
            lab.kubectl = lost
            with self.assertRaises(TimeoutError): M.temporary_replica_balance(lab, business, row)
            row = json.loads((lab.DIRECTORY/M.RECORD).read_text())
            self.assertEqual(volume['spec']['replicaAutoBalance'], 'least-effort')
            lab.kubectl = execute
            M.restore_replica_balance(lab, business, row)
            self.assertEqual(volume['spec']['replicaAutoBalance'], 'ignored')
            before = len(calls); M.restore_replica_balance(lab, business, row)
            self.assertEqual(len(calls), before)

    def test_balance_refuses_foreign_volume_unknown_policy_and_duplicate_intent_before_patch(self):
        for defect in ('uid', 'engine', 'unknown', 'intent'):
            with self.subTest(defect=defect), tempfile.TemporaryDirectory() as directory:
                lab, business, row, volume, calls = self.balance_fixture(Path(directory))
                if defect == 'uid': volume['metadata']['uid'] = 'replacement'
                if defect == 'engine': volume['spec']['dataEngine'] = 'v2'
                if defect == 'unknown': volume['spec']['replicaAutoBalance'] = 'foreign'
                if defect == 'intent': row['replica_balance'] = {'foreign': True}
                with self.assertRaises(ValueError): M.temporary_replica_balance(lab, business, row)
                self.assertFalse(calls)

    def test_restoration_refuses_replaced_volume_or_external_policy_without_overwriting_it(self):
        for defect in ('uid', 'policy', 'journal'):
            with self.subTest(defect=defect), tempfile.TemporaryDirectory() as directory:
                lab, business, row, volume, calls = self.balance_fixture(Path(directory))
                M.temporary_replica_balance(lab, business, row); before = len(calls)
                if defect == 'uid': volume['metadata']['uid'] = 'replacement'
                if defect == 'policy': volume['spec']['replicaAutoBalance'] = 'best-effort'
                if defect == 'journal': row['replica_balance']['uid'] = 'foreign'
                with self.assertRaisesRegex(ValueError, 'ownership or policy'): M.restore_replica_balance(lab, business, row)
                self.assertEqual(len(calls), before)

    def test_real_business_prepares_reduction_for_each_engine_before_waiting_and_restores_original(self):
        for engine in ('v1', 'v2'):
            with self.subTest(engine=engine), tempfile.TemporaryDirectory() as directory:
                base=Path(directory);(base/'config.yml').write_text('{}')
                lab, fixture, _, volume, calls=self.balance_fixture(base)
                volume['spec']['dataEngine']=engine;fixture['namespace']='owned'
                get=lab.get
                lab.get=lambda kind,*a,**k: obj('Setting','policy','policy-uid',value=M.POLICY) if kind=='settings.longhorn.io' else obj('Node','node','node-uid',spec={}) if kind=='node' else get(kind,*a,**k)
                lab.WORKERS=('ip.1','ip.2','ip.3');lab.owner=lambda:{'run_id':'owned'}
                lab.shared_snapshot_identity=lambda:{};lab.supervise_core=MagicMock();lab.installation_health=MagicMock()
                lab.scoped_cleanup=lambda cleanup:nullcontext()
                with patch.object(M,'DATA_ENGINE',engine), patch.object(M,'empty'), \
                     patch.object(M,'prepare',return_value=(fixture,MagicMock())), \
                     patch.object(M,'replica_manager',side_effect=RuntimeError('handoff-complete')):
                    # Execute real business(), temporary policy and scale(); stop
                    # exactly before PDB work, after its actual scale precondition.
                    with self.assertRaisesRegex(RuntimeError,'handoff-complete'):M.business(lab,123)
                    row=json.loads((base/M.RECORD).read_text())
                    self.assertEqual('ignored',row['replica_balance']['original'])
                    self.assertEqual('least-effort',volume['spec']['replicaAutoBalance'])
                    self.assertEqual(1,volume['spec']['numberOfReplicas'])
                    M.scale(lab,fixture,3);M.restore_replica_balance(lab,fixture,row)
                self.assertEqual('ignored',volume['spec']['replicaAutoBalance'])
                self.assertEqual(3,volume['spec']['numberOfReplicas'])

    def test_cleanup_preserves_business_when_original_policy_cannot_be_restored(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            row = {'run_id': 'owned', 'nodes': {}, 'touched': [], 'digest': 'digest'}
            (base/M.RECORD).write_text(json.dumps(row))
            (base/M.BUSINESS).write_text(json.dumps({'volume': 'vol', 'namespace': 'ns', 'objects': {'Deployment/application': 'dep-uid'}}))
            lab = SimpleNamespace(DIRECTORY=base, owner=lambda: {'run_id': 'owned'}, wait=immediate,
                                  cleanup_business=MagicMock(), installation_health=MagicMock(),
                                  get=lambda *a, **kw: obj('Deployment', 'application', 'dep-uid'))
            with patch.object(M, 'policy'), patch.object(M, 'application', return_value=obj('Pod', 'p', 'p-uid')), \
                 patch.object(M, 'scale'), patch.object(M, 'check_data') as data, \
                 patch.object(M, 'restore_replica_balance', side_effect=ValueError('policy restore failed')), \
                 patch.object(M, 'retire_controllers') as retire:
                with self.assertRaisesRegex(ValueError, 'policy restore failed'): M.clean(lab, 1)
                data.assert_not_called(); retire.assert_not_called()
            lab.cleanup_business.assert_not_called(); self.assertTrue((base/M.RECORD).exists())

    def test_original_journal_without_policy_intent_needs_no_new_mutation_to_restore_three(self):
        with tempfile.TemporaryDirectory() as directory:
            lab, business, row, volume, calls = self.balance_fixture(Path(directory))
            volume['spec']['numberOfReplicas'] = 1
            self.assertEqual(len(M.scale(lab, business, 3)), 3)
            before = len(calls); M.restore_replica_balance(lab, business, row)
            self.assertEqual(len(calls), before)
            self.assertEqual(volume['spec']['replicaAutoBalance'], 'ignored')
            self.assertEqual(calls[-1][-1], {'op': 'replace', 'path': '/spec/numberOfReplicas', 'value': 3})

    def generated_fixture(self, directory):
        rs = obj('ReplicaSet', 'rs', 'rs-uid'); pod = obj('Pod', 'pod', 'pod-uid')
        rs['metadata']['ownerReferences'] = [{'kind': 'Deployment', 'uid': 'deployment-uid', 'controller': True}]
        pod['metadata']['ownerReferences'] = [{'kind': 'ReplicaSet', 'uid': 'rs-uid', 'controller': True}]
        resources = {'replicaset': [rs], 'pod': [pod]}
        lab = SimpleNamespace(DIRECTORY=Path(directory), get=lambda kind, **kw: {'items': resources[kind]},
                              write_private=lambda p, row: p.write_text(json.dumps(row)), check_business_graph=MagicMock())
        business = {'namespace': 'namespace', 'objects': {'Deployment/application': 'deployment-uid'}}
        return lab, business, resources

    def test_generated_objects_bind_the_controller_uid_chain_and_keep_retired_uids(self):
        with tempfile.TemporaryDirectory() as directory:
            lab, business, resources = self.generated_fixture(directory)
            pods = M.generated(lab, business)
            self.assertEqual([p['metadata']['uid'] for p in pods], ['pod-uid'])
            self.assertEqual(business['objects']['ReplicaSet/rs'], 'rs-uid')
            resources['pod'] = []
            M.generated(lab, business)
            self.assertEqual(business['objects']['Pod/pod'], 'pod-uid')
            lab.check_business_graph.assert_called_with(business)

    def test_generated_name_replacement_is_refused_and_foreign_owner_is_not_adopted(self):
        with tempfile.TemporaryDirectory() as directory:
            lab, business, resources = self.generated_fixture(directory)
            M.generated(lab, business)
            resources['pod'][0]['metadata']['uid'] = 'replacement'
            with self.assertRaisesRegex(ValueError, 'replaced'):
                M.generated(lab, business)
            resources['pod'][0]['metadata']['ownerReferences'][0]['uid'] = 'foreign-rs'
            self.assertEqual(M.generated(lab, business), [])
            self.assertEqual(business['objects']['Pod/pod'], 'pod-uid')
            resources['pod'][0]['metadata']['ownerReferences'][0]['uid'] = 'deployment-uid'
            self.assertEqual(M.generated(lab, business), [])

    def test_pdb_requires_matching_selector_observed_generation_and_zero_budget(self):
        pod = obj('Pod', 'im', 'im-uid'); pod['metadata']['labels'] = {'im': 'owned'}
        pdb = obj('PodDisruptionBudget', 'im', 'pdb-uid', spec={'selector': {'matchLabels': {'im': 'owned'}}, 'minAvailable': 1},
                  status={'observedGeneration': 2, 'disruptionsAllowed': 0})
        pdb['metadata']['generation'] = 2
        lab = SimpleNamespace(get=lambda *a, **kw: {'items': [pdb]})
        original = copy.deepcopy(pdb)
        self.assertIs(M.pdb(lab, pod), pdb)
        for mutate in (lambda: pdb['status'].update(disruptionsAllowed=1),
                       lambda: pdb['status'].update(observedGeneration=1),
                       lambda: pdb['spec']['selector']['matchLabels'].update(im='foreign'),
                       lambda: pdb['spec']['selector'].update(matchExpressions=[{'key': 'im', 'operator': 'Exists'}]),
                       lambda: pdb['spec'].update(minAvailable=0)):
            pdb.clear(); pdb.update(copy.deepcopy(original)); mutate()
            self.assertIsNone(M.pdb(lab, pod))

    def test_refusal_requires_real_eviction_budget_error_not_any_nonzero_exit(self):
        for rc, message, accepted in ((1, 'Cannot evict pod as it would violate the pod disruption budget.', True),
                (0, 'Cannot evict pod: disruption budget', False), (1, 'connection timed out', False),
                (1, 'disruption budget exists', False)):
            lab = SimpleNamespace(kubectl=MagicMock(return_value=SimpleNamespace(returncode=rc, stdout='', stderr=message)))
            with self.subTest(rc=rc, message=message):
                if accepted: M.drain(lab, 'worker', pod_selector='im=owned', refusal=True)
                else:
                    with self.assertRaisesRegex(ValueError, 'actual PDB'):
                        M.drain(lab, 'worker', pod_selector='im=owned', refusal=True)
                args = lab.kubectl.call_args.args
                self.assertIn('--pod-selector=im=owned', args)
                self.assertNotIn('--disable-eviction', args); self.assertNotIn('--force', args)
        with self.assertRaisesRegex(ValueError, 'allowed drain failed'):
            M.drain(SimpleNamespace(kubectl=lambda *a, **kw: SimpleNamespace(returncode=1, stdout='', stderr='timeout')), 'worker')

    def drain_fixture(self):
        pod = obj('Pod', 'new', 'new-uid', spec={'nodeName': 'node-b'})
        volume = obj('Volume', 'vol', 'vol-uid', status={'state': 'attached', 'currentNodeID': 'node-b', 'robustness': 'degraded'})
        replicas = [obj('Replica', 'r'+n, 'r-uid'+n, spec={'volumeName': 'vol', 'nodeID': 'node-'+n, 'healthyAt': 'now', 'failedAt': ''},
                        status={'currentState': 'running'}) for n in ('b', 'c')]
        engine = obj('Engine', 'engine', 'engine-uid', spec={'volumeName': 'vol', 'nodeID': 'node-b'},
                     status={'currentState': 'running', 'replicaModeMap': {'rb': 'RW', 'rc': 'RW'}})
        va = obj('VolumeAttachment', 'va', 'va-uid', spec={'source': {'persistentVolumeName': 'pv'}, 'attacher': 'driver.longhorn.io',
                                                         'nodeName': 'node-b'}, status={'attached': True})
        resources = {'volumes.longhorn.io': volume, 'replicas.longhorn.io': [*replicas], 'engines.longhorn.io': [engine],
                     'instancemanagers.longhorn.io': [], 'volumeattachments.storage.k8s.io': [va]}
        def get(kind, *a, **kw): return resources[kind] if kind == 'volumes.longhorn.io' else {'items': resources[kind]}
        # Tests use the same hostname derivation as the real worker lease.
        for resource in (volume, *replicas, engine, va, pod):
            for section in ('spec', 'status'):
                for key in ('nodeID', 'nodeName', 'currentNodeID'):
                    value = resource.get(section, {}).get(key)
                    if value in ('node-a', 'node-b', 'node-c'):
                        resource[section][key] = 'longhorn-node-'+value[-1]
        return SimpleNamespace(get=get, WORKERS=('ip.a', 'ip.b', 'ip.c')), {'volume': 'vol', 'volume_uid': 'vol-uid', 'pv': 'pv'}, pod, resources

    def test_drained_volume_can_be_degraded_with_two_rw_peers_real_new_attachment_and_no_old_instances(self):
        lab, business, pod, resources = self.drain_fixture()
        with patch.object(M, 'application', return_value=pod):
            self.assertIs(M.drained(lab, business, 'longhorn-node-a', 'old-uid'), pod)
            original = copy.deepcopy(resources)
            for mutate in (lambda: resources['replicas.longhorn.io'].pop(),
                           lambda: resources['replicas.longhorn.io'][0]['spec'].update(nodeID='node-a'),
                           lambda: resources['engines.longhorn.io'][0]['status']['replicaModeMap'].update(rb='WO'),
                           lambda: resources['volumeattachments.storage.k8s.io'][0]['spec'].update(nodeName='node-a'),
                           lambda: resources['volumeattachments.storage.k8s.io'][0]['status'].update(attached=False)):
                resources.clear(); resources.update(copy.deepcopy(original)); mutate()
                self.assertIsNone(M.drained(lab, business, 'longhorn-node-a', 'old-uid'))
            resources.clear(); resources.update(copy.deepcopy(original))
            resources['instancemanagers.longhorn.io'].append(obj('InstanceManager', 'im', 'im-uid',
                spec={'nodeID': 'longhorn-node-a'}, status={'instanceReplicas': {'r': {'state': 'running'}}}))
            self.assertIsNone(M.drained(lab, business, 'longhorn-node-a', 'old-uid'))

    def test_clean_restores_cordon_and_full_data_health_before_controller_or_volume_deletion(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory); calls = []
            row = {'run_id': 'owned', 'nodes': {'worker': {'unschedulable': False}}, 'touched': ['worker'], 'digest': 'digest'}
            (base/M.RECORD).write_text(json.dumps(row))
            business = {'volume': 'vol', 'namespace': 'ns', 'objects': {'Deployment/application': 'dep-uid'}}
            (base/M.BUSINESS).write_text(json.dumps(business))
            client = obj('Pod', 'client', 'client-uid')
            lab = SimpleNamespace(DIRECTORY=base, owner=lambda: {'run_id': 'owned'}, wait=immediate,
                cleanup_business=lambda *a, **kw: calls.append('delete-business'), installation_health=lambda: calls.append('health'),
                write_private=lambda p, value: p.write_text(json.dumps(value)),
                get=lambda *a, **kw: obj('Deployment', 'application', 'dep-uid'))
            with patch.object(M, 'policy'), patch.object(M, 'scheduling', side_effect=lambda *a: calls.append('restore-node')), \
                 patch.object(M, 'application', return_value=client), patch.object(M, 'scale', side_effect=lambda *a: calls.append('restore-replicas')), \
                 patch.object(M, 'restore_replica_balance', side_effect=lambda *a: calls.append('restore-policy')), \
                 patch.object(M, 'check_data', side_effect=lambda *a: calls.append('verify-data')), \
                 patch.object(M, 'retire_controllers', side_effect=lambda *a: calls.append('delete-controller')):
                M.clean(lab, 123, failed=True)
            self.assertEqual(calls, ['restore-node', 'restore-replicas', 'restore-policy', 'verify-data', 'delete-controller', 'delete-business', 'health'])
            self.assertFalse((base/M.RECORD).exists())
            recovered = list(base.glob('longhorn-maintenance-recovered-*.json'))
            self.assertEqual(len(recovered), 1)
            self.assertTrue(json.loads(recovered[0].read_text())['recovered'])

    def test_cleanup_does_not_delete_data_when_recovery_verification_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            row = {'run_id': 'owned', 'nodes': {}, 'touched': [], 'digest': 'digest'}
            (base/M.RECORD).write_text(json.dumps(row))
            (base/M.BUSINESS).write_text(json.dumps({'volume': 'vol', 'namespace': 'ns', 'objects': {'Deployment/application': 'dep-uid'}}))
            lab = SimpleNamespace(DIRECTORY=base, owner=lambda: {'run_id': 'owned'}, wait=immediate,
                                  cleanup_business=MagicMock(), installation_health=MagicMock(),
                                  get=lambda *a, **kw: obj('Deployment', 'application', 'dep-uid'))
            with patch.object(M, 'policy'), patch.object(M, 'application', return_value=obj('Pod', 'p', 'p-uid')), \
                 patch.object(M, 'scale'), patch.object(M, 'check_data', side_effect=ValueError('corruption')):
                with self.assertRaisesRegex(ValueError, 'corruption'): M.clean(lab, 1)
            lab.cleanup_business.assert_not_called(); self.assertTrue((base/M.RECORD).exists())

    def retirement_fixture(self, base, *, controller=True, terminating=False, bare=False):
        deployment = obj('Deployment', 'application', 'dep-uid')
        replica_set = obj('ReplicaSet', 'application-rs', 'rs-uid')
        replica_set['metadata']['ownerReferences'] = [
            {'kind': 'Deployment', 'uid': 'dep-uid', 'controller': True}]
        pod = obj('Pod', 'application-pod', 'pod-uid',
                  status={'conditions': [{'type': 'Ready', 'status': 'True'}]})
        pod['metadata']['ownerReferences'] = [
            {'kind': 'ReplicaSet', 'uid': 'rs-uid', 'controller': True}]
        reader = obj('Pod', 'recovery-owned', 'reader-uid',
                     status={'conditions': [{'type': 'Ready', 'status': 'True'}]})
        resources = {'deployment': [deployment] if controller else [],
                     'replicaset': [replica_set], 'pod': [pod, *([reader] if bare else [])]}
        business = {'namespace': 'ns', 'volume': 'volume', 'volume_uid': 'volume-uid',
                    'objects': {'Deployment/application': 'dep-uid',
                                'ReplicaSet/application-rs': 'rs-uid', 'Pod/application-pod': 'pod-uid'}}
        if bare: business['objects']['Pod/recovery-owned'] = 'reader-uid'
        if terminating: deployment['metadata']['deletionTimestamp'] = 'now'
        calls = []
        def get(kind, name=None, namespace=None):
            items = resources[kind]
            return next((p for p in items if p['metadata']['name'] == name), None) if name else {'items': items}
        def graph(journal):
            for items in resources.values():
                for resource in items:
                    key = resource['kind']+'/'+resource['metadata']['name']
                    if journal['objects'].get(key) != resource['metadata']['uid']:
                        raise ValueError('foreign or replaced fixture object')
        def remove(resource):
            self.assertEqual(resource['kind'], 'Deployment')
            calls.append('delete-deployment')
            resources['deployment'].remove(resource)
            # Owner disappearance does not imply its RS or Pod disappeared.
            pod['metadata']['deletionTimestamp'] = 'now'
        def wait(label, timeout, check):
            result = check()
            if not result:
                calls.append('waiting-'+label)
                if label == 'maintenance-controller-retired': resources['deployment'] = []
                elif label == 'maintenance-replicaset-retired': resources['replicaset'] = []
                elif label == 'maintenance-generated-pods-retired':
                    resources['pod'] = [p for p in resources['pod'] if p['metadata']['uid'] != 'pod-uid']
                result = check()
            if not result: raise RuntimeError('not converged: '+label)
            return result
        lab = SimpleNamespace(DIRECTORY=base, get=get, check_business_graph=graph,
            write_private=lambda p, value: p.write_text(json.dumps(value)), remove=remove, wait=wait)
        return lab, business, resources, calls

    def test_background_gc_deletes_only_owner_and_waits_for_original_rs_and_pods(self):
        with tempfile.TemporaryDirectory() as directory:
            lab, business, resources, calls = self.retirement_fixture(Path(directory), bare=True)
            M.retire_controllers(lab, business)
            self.assertEqual(calls, ['delete-deployment', 'waiting-maintenance-replicaset-retired',
                                    'waiting-maintenance-generated-pods-retired'])
            self.assertEqual([p['metadata']['uid'] for p in resources['pod']], ['reader-uid'])
            self.assertEqual(business['objects']['Pod/application-pod'], 'pod-uid')

    def test_business_cleanup_cannot_start_while_generated_pod_gc_is_pending(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory); lab, business, resources, calls = self.retirement_fixture(base)
            row = {'run_id': 'owned', 'nodes': {}, 'digest': 'digest'}
            (base/M.RECORD).write_text(json.dumps(row)); (base/M.BUSINESS).write_text(json.dumps(business))
            lab.owner = lambda: {'run_id': 'owned'}
            lab.installation_health = lambda: calls.append('health')
            def cleanup(*a, **kw):
                self.assertFalse(resources['deployment']); self.assertFalse(resources['replicaset'])
                self.assertFalse(resources['pod']); calls.append('generic-cleanup')
            lab.cleanup_business = cleanup
            with patch.object(M, 'policy'), patch.object(M, 'application', return_value=resources['pod'][0]), \
                 patch.object(M, 'scale'), patch.object(M, 'restore_replica_balance'), patch.object(M, 'check_data'):
                M.clean(lab, 1)
            self.assertLess(calls.index('waiting-maintenance-generated-pods-retired'), calls.index('generic-cleanup'))

    def test_retired_or_terminating_controller_uses_original_recovery_creation_after_gc(self):
        for state in ('retired-with-dependents', 'retired-empty', 'terminating'):
            with self.subTest(state=state), tempfile.TemporaryDirectory() as directory:
                base = Path(directory)
                lab, business, resources, calls = self.retirement_fixture(
                    base, controller=state == 'terminating', terminating=state == 'terminating')
                if state == 'retired-empty':
                    resources['replicaset'] = []; resources['pod'] = []
                row = {'run_id': 'owned', 'nodes': {'worker': {}}, 'digest': 'digest'}
                (base/M.RECORD).write_text(json.dumps(row)); (base/M.BUSINESS).write_text(json.dumps(business))
                lab.owner = lambda: {'run_id': 'owned'}; lab.installation_health = MagicMock()
                lab.client_pod = lambda ns, name, claim, node: obj('Pod', name, 'new-client-uid',
                    spec={'nodeName': node}, status={'conditions': [{'type': 'Ready', 'status': 'True'}]})
                def create(journal, record, pod):
                    self.assertFalse(resources['deployment']); self.assertFalse(resources['replicaset'])
                    self.assertFalse(resources['pod']); calls.append('create-recovery')
                    resources['pod'].append(pod)
                    journal['objects']['Pod/'+pod['metadata']['name']] = pod['metadata']['uid']
                    lab.write_private(record, journal)
                lab.create_business_object = create
                def cleanup(*a, **kw):
                    self.assertEqual([p['metadata']['uid'] for p in resources['pod']], ['new-client-uid'])
                    calls.append('generic-cleanup')
                lab.cleanup_business = cleanup
                with patch.object(M, 'policy'), patch.object(M, 'application') as application, \
                     patch.object(M, 'scale', side_effect=lambda *a: calls.append('restore-three')), \
                     patch.object(M, 'restore_replica_balance', side_effect=lambda *a: calls.append('restore-policy')), \
                     patch.object(M, 'check_data', side_effect=lambda *a: calls.append('verify-data')):
                    M.clean(lab, 1)
                application.assert_not_called()
                if state != 'retired-empty':
                    self.assertLess(calls.index('waiting-maintenance-generated-pods-retired'), calls.index('create-recovery'))
                self.assertEqual(calls[-4:], ['restore-three', 'restore-policy', 'verify-data', 'generic-cleanup'])

    def test_empty_retired_controller_requires_new_reader_digest_and_ledger_before_cleanup(self):
        for corrupt in (False, True):
            with self.subTest(corrupt=corrupt), tempfile.TemporaryDirectory() as directory:
                base = Path(directory)
                lab, business, resources, calls = self.retirement_fixture(base, controller=False)
                resources['replicaset'] = []; resources['pod'] = []
                row = {'run_id': 'owned', 'nodes': {'worker': {}}, 'payload': 'frozen-payload',
                       'digest': 'original-digest', 'ledger': {'token': 'original-ledger'}}
                (base/M.RECORD).write_text(json.dumps(row)); (base/M.BUSINESS).write_text(json.dumps(business))
                lab.owner = lambda: {'run_id': 'owned'}; lab.installation_health = MagicMock()
                lab.cleanup_business = MagicMock()
                lab.client_pod = lambda ns, name, claim, node: obj('Pod', name, 'recovery-uid',
                    status={'conditions': [{'type': 'Ready', 'status': 'True'}]})
                def create(journal, record, pod):
                    resources['pod'].append(pod)
                    journal['objects']['Pod/'+pod['metadata']['name']] = pod['metadata']['uid']
                    lab.write_private(record, journal)
                lab.create_business_object = create
                lab.payload_program = MagicMock(return_value='payload-verifier')
                lab.kubectl = MagicMock(return_value=SimpleNamespace(
                    stdout='corrupt-digest' if corrupt else 'original-digest'))
                faults = SimpleNamespace(verify_client_ledger=MagicMock())
                lab.load_helper = MagicMock(return_value=faults)
                with patch.object(M, 'policy'), patch.object(M, 'application') as application, \
                     patch.object(M, 'scale') as scale, patch.object(M, 'restore_replica_balance'):
                    if corrupt:
                        with self.assertRaisesRegex(ValueError, 'acknowledged business bytes'): M.clean(lab, 1)
                    else: M.clean(lab, 1)
                application.assert_not_called()
                scale.assert_called_once_with(lab, json.loads((base/M.BUSINESS).read_text()), 3)
                client = resources['pod'][0]['metadata']['name']
                lab.payload_program.assert_called_once_with('frozen-payload', read_only=True)
                lab.kubectl.assert_called_once_with('-n', 'ns', 'exec', client, '--', 'python3', '-c', 'payload-verifier')
                if corrupt:
                    lab.cleanup_business.assert_not_called(); faults.verify_client_ledger.assert_not_called()
                    self.assertTrue((base/M.RECORD).exists()); self.assertTrue((base/M.BUSINESS).exists())
                    self.assertEqual(resources['pod'][0]['metadata']['uid'], 'recovery-uid')
                else:
                    faults.verify_client_ledger.assert_called_once_with(
                        lab, json.loads((base/M.BUSINESS).read_text()), client, row['ledger'])
                    lab.cleanup_business.assert_called_once()

    def test_retired_controller_reuses_exact_owned_ready_bare_client(self):
        with tempfile.TemporaryDirectory() as directory:
            lab, business, resources, calls = self.retirement_fixture(Path(directory), controller=False, bare=True)
            expected = resources['pod'][1]
            with patch.object(M, 'application') as application:
                self.assertIs(M.recovery_client(lab, business), expected)
            application.assert_not_called()
            self.assertEqual([p['metadata']['uid'] for p in resources['pod']], ['reader-uid'])

    def test_live_original_controller_keeps_normal_application_recovery_path(self):
        with tempfile.TemporaryDirectory() as directory:
            lab, business, resources, calls = self.retirement_fixture(Path(directory))
            with patch.object(M, 'application', return_value=resources['pod'][0]) as application, \
                 patch.object(M, 'retire_controllers') as retire:
                self.assertIs(M.recovery_client(lab, business), resources['pod'][0])
            application.assert_called_once_with(lab, business); retire.assert_not_called()
            self.assertEqual(calls, [])

    def test_retirement_refuses_foreign_and_replaced_objects_before_any_delete(self):
        for kind in ('deployment', 'replicaset', 'pod', 'foreign-pod', 'namespace'):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                lab, business, resources, calls = self.retirement_fixture(Path(directory))
                if kind == 'foreign-pod': resources['pod'].append(obj('Pod', 'foreign', 'foreign-uid'))
                elif kind == 'namespace':
                    lab.check_business_graph = MagicMock(side_effect=ValueError('namespace identity changed'))
                else: resources[kind][0]['metadata']['uid'] = 'replacement'
                with self.assertRaises(ValueError): M.retire_controllers(lab, business)
                self.assertEqual(calls, [])

    def test_same_name_replacement_during_retirement_wait_fails_immediately(self):
        for kind, label in (('deployment', 'maintenance-controller-retired'),
                            ('replicaset', 'maintenance-replicaset-retired'),
                            ('pod', 'maintenance-generated-pods-retired')):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                lab, business, resources, calls = self.retirement_fixture(Path(directory))
                real_wait = lab.wait
                original = copy.deepcopy(resources[kind][0])
                def replace_at_wait(current_label, timeout, check):
                    if current_label != label: return real_wait(current_label, timeout, check)
                    original['metadata']['uid'] = 'replacement'; resources[kind] = [original]
                    return immediate(current_label, timeout, check)
                lab.wait = replace_at_wait
                with self.assertRaises(ValueError): M.retire_controllers(lab, business)

    def test_gc_timeout_keeps_business_data_and_journals_for_scoped_recovery(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory); lab, business, resources, calls = self.retirement_fixture(base)
            row = {'run_id': 'owned', 'nodes': {}, 'digest': 'digest'}
            (base/M.RECORD).write_text(json.dumps(row)); (base/M.BUSINESS).write_text(json.dumps(business))
            lab.owner = lambda: {'run_id': 'owned'}; lab.cleanup_business = MagicMock()
            lab.installation_health = MagicMock(); real_wait = lab.wait
            lab.wait = lambda label, timeout, check: (immediate(label, timeout, check)
                if label == 'maintenance-generated-pods-retired' else real_wait(label, timeout, check))
            with patch.object(M, 'policy'), patch.object(M, 'application', return_value=resources['pod'][0]), \
                 patch.object(M, 'scale'), patch.object(M, 'restore_replica_balance'), patch.object(M, 'check_data') as data:
                with self.assertRaisesRegex(RuntimeError, 'generated-pods-retired'): M.clean(lab, 1)
            data.assert_called_once(); lab.cleanup_business.assert_not_called()
            self.assertTrue((base/M.RECORD).exists()); self.assertTrue((base/M.BUSINESS).exists())

    def test_lost_namespace_create_response_is_recovered_by_exact_token_and_foreign_token_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory); row = {'run_id': 'owned', 'nodes': {}, 'touched': [],
                                           'namespace_intent': {'name': 'ns', 'token': 'owned-token'}}
            (base/M.RECORD).write_text(json.dumps(row))
            ns = obj('Namespace', 'ns', 'namespace-uid'); ns['metadata']['annotations'] = {'kubeauto.io/maintenance-token': 'foreign'}
            lab = SimpleNamespace(DIRECTORY=base, owner=lambda: {'run_id': 'owned'}, get=lambda *a: ns,
                                  cleanup_business=MagicMock(), installation_health=MagicMock(), wait=immediate,
                                  namespace_defaults=lambda name: ['defaults'], write_private=lambda p, data: p.write_text(json.dumps(data)))
            with patch.object(M, 'policy'):
                with self.assertRaisesRegex(ValueError, 'intent token differs'): M.clean(lab, 1)
                lab.cleanup_business.assert_not_called()
                ns['metadata']['annotations']['kubeauto.io/maintenance-token'] = 'owned-token'
                lab.get = lambda kind, *a, **kw: ns if kind == 'namespace' else {'items': []}
                with patch.object(M, 'retire_controllers'): M.clean(lab, 1)
                self.assertEqual(json.loads((base/M.BUSINESS).read_text())['namespace_uid'], 'namespace-uid')
                lab.cleanup_business.assert_called_once()

    def test_foreign_run_journal_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory); (base/M.RECORD).write_text('{"run_id":"foreign"}')
            with self.assertRaisesRegex(ValueError, 'foreign maintenance journal'):
                M.receipt(SimpleNamespace(DIRECTORY=base, owner=lambda: {'run_id': 'owned'}))

    def test_bounded_ledger_program_produces_real_fsynced_acknowledgements_without_stdin_dependency(self):
        spec = importlib.util.spec_from_file_location('maintenance_faults', ROOT/'tests/helpers/longhorn-faults.py')
        faults = importlib.util.module_from_spec(spec); spec.loader.exec_module(faults)
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory); row = {'run_id': 'owned'}
            def kubectl(*args, **kwargs):
                code = args[-1].replace('/data/fault-ledger', str(base/'fault-ledger'))
                return subprocess.run([sys.executable, '-u', '-c', code], check=True, text=True,
                                      capture_output=True, timeout=8)
            lab = SimpleNamespace(DIRECTORY=base, load_helper=lambda name: faults, kubectl=kubectl,
                                  write_private=lambda p, value: p.write_text(json.dumps(value)))
            with patch.object(M, 'check_data'):
                M.ledger(lab, {'namespace': 'ns'}, 'client', row)
            proof = row['ledger']
            self.assertGreaterEqual(proof['summary']['confirmed'], 4)
            self.assertEqual(faults.verify_ledger(str(base/'fault-ledger'), proof['token'], proof['records']), proof['summary'])

    def test_shell_preserves_first_failure_durable_exit_and_emits_pass_only_after_cleanup(self):
        for fail in ('business', 'finish', 'none'):
            with self.subTest(fail=fail), tempfile.TemporaryDirectory() as directory:
                base = Path(directory); (base/'.venv/bin').mkdir(parents=True)
                os.symlink(sys.executable, base/'.venv/bin/python')
                (base/'tests/helpers').mkdir(parents=True); (base/'kube-bin').mkdir()
                log = base/'calls'
                common = "import os,signal,sys,time\nfrom pathlib import Path\na=sys.argv[1]\nwith open(os.environ['CONTRACT_LOG'],'a') as f:f.write(Path(__file__).name+' '+a+'\\n')\n"
                (base/'tests/helpers/longhorn-lab.py').write_text(common+"if a=='relay-maintain':\n signal.signal(signal.SIGTERM,lambda *x:sys.exit(0))\n while True:time.sleep(.1)\n")
                (base/'tests/helpers/longhorn-maintenance.py').write_text(common+"time.sleep(.15)\nif a==os.environ['CONTRACT_FAIL']:sys.exit(17)\n")
                kubectl = base/'kube-bin/kubectl'; kubectl.write_text('#!/bin/sh\nexit 0\n'); kubectl.chmod(0o755)
                script = base/'run.sh'; script.write_text((ROOT/'tests/helpers/longhorn-maintenance.sh').read_text().replace('BASE=/usr/local/kubeauto', 'BASE='+str(base)))
                result = subprocess.run(['bash', str(script)], text=True, capture_output=True, timeout=15,
                    env=dict(os.environ, LONGHORN_PARENT_LEASE_HELD='yes', CONTRACT_LOG=str(log), CONTRACT_FAIL=fail))
                durable = next(line.split('path=', 1)[1] for line in result.stdout.splitlines()
                               if line.startswith('LONGHORN_MAINTENANCE_DURABLE_DIRECTORY'))
                try:
                    self.assertEqual(result.returncode, 0 if fail == 'none' else 17)
                    self.assertEqual((Path(durable)/'exit').read_text().strip(), str(result.returncode))
                    if fail == 'none':
                        self.assertIn('LONGHORN_MAINTENANCE_SCOPED_CLEAN_PASS', result.stdout)
                        self.assertIn('LONGHORN_MAINTENANCE_PASS', result.stdout)
                    else:
                        self.assertNotIn('LONGHORN_MAINTENANCE_PASS', result.stdout)
                        self.assertIn('LONGHORN_FIRST_FAILURE', result.stderr)
                        lines = log.read_text().splitlines()
                        self.assertLess(lines.index('longhorn-maintenance.py clean'), lines.index('longhorn-lab.py relay-clean'))
                        self.assertEqual(lines[-1], 'longhorn-lab.py verify-core')
                finally: shutil.rmtree(durable)


if __name__ == '__main__': unittest.main()
