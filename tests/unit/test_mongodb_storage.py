"""Independent MongoDB CSI fixture scope and exact reused artifact contracts."""
import copy
import importlib.util
import json
import os
from pathlib import Path
import subprocess
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('mongodb_storage', ROOT / 'tests/helpers/mongodb-lab-storage.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class MongoDBStorageTests(unittest.TestCase):
    def reclaim_objects(self):
        source = {'metadata': {'name': 'mongodb-gate-data', 'namespace': 'mongodb-gate',
            'uid': 'source', 'labels': {'app.kubernetes.io/managed-by': 'kubeauto'}}}
        claims, pvs, volumes, engines = [], [], [], []
        for i in range(3):
            name = 'mongod-data-mongodb-gate-data-rs0-' + str(i)
            uid, volume = 'claim-' + str(i), 'pvc-' + str(i)
            claims.append({'metadata': {'name': name, 'namespace': 'mongodb-gate', 'uid': uid,
                'labels': {'app.kubernetes.io/instance': 'mongodb-gate-data'}},
                'spec': {'volumeName': volume, 'storageClassName': module.STORAGE_CLASS}, 'status': {'phase': 'Bound'}})
            pvs.append({'metadata': {'name': volume, 'uid': 'pv-' + str(i)},
                'spec': {'storageClassName': module.STORAGE_CLASS,
                    'csi': {'driver': 'driver.longhorn.io', 'volumeHandle': volume},
                    'claimRef': {'namespace': 'mongodb-gate', 'name': name, 'uid': uid}}})
            volumes.append({'metadata': {'name': volume, 'uid': 'volume-' + str(i), 'namespace': module.NAMESPACE},
                'spec': {'nodeID': 'mongodb-node-212', 'size': '10737418240', 'dataEngine': 'v1'},
                'status': {'state': 'attached', 'robustness': 'healthy', 'actualSize': '18000000000'}})
            engines.append({'metadata': {'uid': 'engine-' + str(i)}, 'spec': {'volumeName': volume},
                'status': {'currentState': 'running', 'replicaModeMap': {'a': 'RW', 'b': 'RW'},
                    'snapshots': {name: {'name': name, 'usercreated': False, 'labels': labels}
                        for name, labels in [('volume-head', {}), ('rebuild', {}),
                            ('expand-10737418240', {'replica-expansion': '10737418240'})]}}})
        return [source, claims, pvs, volumes, engines, 'source']

    def test_reclaim_reproduces_internal_snapshot_amplification_and_refuses_foreign_graphs(self):
        args = self.reclaim_objects()
        plan = module.reclaim_plan(*args)
        self.assertEqual(len(plan), 3)
        self.assertEqual(plan[0]['actual_bytes'], 18000000000)
        self.assertEqual(plan[0]['snapshots'], ['expand-10737418240', 'rebuild'])
        for drift in ('source', 'namespace', 'claims', 'claim', 'driver', 'handle', 'volume',
                      'detached', 'degraded', 'rebuilding', 'engine', 'snapshot', 'user', 'label'):
            bad = copy.deepcopy(args)
            if drift == 'source':bad[0]['metadata']['uid'] = 'foreign'
            if drift == 'namespace':bad[1][0]['metadata']['namespace'] = 'foreign'
            if drift == 'claims':bad[1].pop()
            if drift == 'claim':bad[2][0]['spec']['claimRef']['uid'] = 'foreign'
            if drift == 'driver':bad[2][0]['spec']['csi']['driver'] = 'foreign'
            if drift == 'handle':bad[2][0]['spec']['csi']['volumeHandle'] = 'foreign'
            if drift == 'volume':bad[3][0]['spec']['nodeID'] = 'mongodb-node-129'
            if drift == 'detached':bad[3][0]['status']['state'] = 'detached'
            if drift == 'degraded':bad[3][0]['status']['robustness'] = 'degraded'
            if drift == 'rebuilding':bad[4][0]['status']['replicaModeMap']['b'] = 'WO'
            if drift == 'engine':bad[4].append(copy.deepcopy(bad[4][0]))
            if drift == 'snapshot':bad[4][0]['status']['snapshots'].pop('volume-head')
            if drift == 'user':bad[4][0]['status']['snapshots']['rebuild']['usercreated'] = True
            if drift == 'label':bad[4][0]['status']['snapshots']['rebuild']['labels'] = {'customer': 'backup'}
            with self.subTest(drift=drift), self.assertRaises(ValueError):module.reclaim_plan(*bad)

    def test_reclaim_native_actions_verify_all_volumes_and_stop_on_first_failure(self):
        for failure in (None, 'retained-removed', 'identity', 'snapshot', 'response', 'trim', 'purge'):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as tmp:
                fixture = module.Fixture(SimpleNamespace(base=Path(tmp), kubeconfig=Path(tmp)/'kc'))
                fixture.state_file.parent.mkdir(parents=True)
                fixture.state_file.write_text(json.dumps({'cluster_uid': 'core'}))
                fixture.state_file.with_name('mongodb-lab-owner.json').write_text(json.dumps({
                    'cluster_uid': 'core', 'product_identity': {'source_uid': 'source'}}))
                args = self.reclaim_objects(); actions = []; purged = set()
                def k(*argv):
                    kind = argv[argv.index('get') + 1]
                    if kind == 'psmdb':return json.dumps(args[0])
                    index = {'pvc': 1, 'pv': 2, 'volumes.longhorn.io': 3, 'engines.longhorn.io': 4}[kind]
                    result = copy.deepcopy(args[index])
                    if index == 4:
                        for i, e in enumerate(result):
                            if 'pvc-' + str(i) in purged:
                                if failure == 'retained-removed':
                                    for s in e['status']['snapshots'].values():s['removed'] = True
                                else:e['status']['snapshots'] = {'volume-head': e['status']['snapshots']['volume-head']}
                                if failure == 'purge':e['status']['purgeStatus'] = {'a': {'error': 'native purge error'}}
                        if failure == 'snapshot':result[-1]['status']['snapshots']['rebuild']['usercreated'] = True
                    if index == 3 and failure == 'identity' and actions:result[-1]['metadata']['uid'] = 'changed'
                    return json.dumps({'items': result})
                def run(argv, **kw):
                    url = argv[argv.index('--raw') + 1]
                    volume, action = url.rsplit('/', 1)[1].split('?action=')
                    actions.append((volume, action, json.loads(kw['input_text'])))
                    if failure == 'trim' and action == 'trimFilesystem':raise ValueError('native trim failed')
                    if action == 'snapshotPurge':purged.add(volume)
                    return json.dumps({'id': 'foreign' if failure == 'response' else volume})
                with patch.object(fixture, 'health'), patch.object(fixture, 'k', side_effect=k), \
                        patch.object(fixture, 'run', side_effect=run), patch.object(fixture, 'ssh', return_value='68400709632 40000000000'), \
                        patch('builtins.print') as output:
                    if failure not in (None, 'retained-removed'):
                        with self.assertRaises(ValueError):fixture.reclaim()
                        self.assertFalse(any(c.args[0] == 'MONGODB_STORAGE_RECLAIM_PASS' for c in output.call_args_list))
                    else:
                        fixture.reclaim()
                        self.assertEqual(len(actions), 12)
                        self.assertEqual([x[1] for x in actions[:4]], ['snapshotDelete', 'snapshotDelete', 'trimFilesystem', 'snapshotPurge'])
                        output.assert_any_call('MONGODB_STORAGE_RECLAIM_PASS', flush=True)
                if failure == 'snapshot':self.assertEqual(actions, [])
                if failure in ('identity', 'response'):self.assertEqual(len(actions), 1)
                if failure == 'trim':self.assertEqual(actions[-1][1], 'trimFilesystem')

    def test_storage_reclaim_has_fixed_entry_and_follows_capacity_and_monitor_cleanup(self):
        script = 'source tests/helpers/mongodb-runner.sh\nmongodb_launch(){ printf "%s|%s\\n" "$1" "$2"; }\nmongodb_dispatch --mongodb-storage-reclaim\n'
        result = subprocess.run(['bash', '-c', script], cwd=ROOT, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), 'storage-reclaim|MONGODB_STORAGE_RECLAIM_PASS')
        runner = (ROOT/'tests/helpers/mongodb-install-gate.sh').read_text()
        self.assertIn('--storage-reclaim)\n    phase=storage-reclaim', runner)
        self.assertIn('case "$phase" in capacity|capacity-pressure|logging|observability|observability-capacity)', runner)

    def spare_node(self, name='mongodb-node-135'):
        return {'metadata': {'name': name, 'uid': 'uid-' + name, 'resourceVersion': '1', 'labels': {}},
            'spec': {'taints': [{'key': 'node-role.kubernetes.io/control-plane', 'effect': 'NoSchedule'}]},
            'status': {'addresses': [{'type': 'InternalIP', 'address': module.REPLICA_SPARES[name]}],
                'conditions': [{'type': 'Ready', 'status': 'True'}]}}

    def domains(self, tmp):
        fixture=module.Fixture(SimpleNamespace(base=Path(tmp),kubeconfig=Path(tmp)/'unused'))
        fixture.state_file.parent.mkdir(parents=True)
        domains=module.ReplicaDomains(fixture,'0123456789abcdef')
        nodes={name:self.spare_node(name) for name in module.REPLICA_SPARES}
        rows={name:module.replica_spare(node) | {'host': {'installed':'no','active':'inactive','enabled':'not-found'}}
              for name,node in nodes.items()}
        domains.state={'run_id':domains.run_id,'cluster_uid':'core','nodes':rows};domains.save()
        def get(kind,name):
            return {'metadata':{'uid':'core'}} if kind=='namespace' else nodes[name]
        return fixture,domains,nodes,get

    def test_replica_spares_refuse_foreign_nodes_taints_labels_and_unready_domains(self):
        node=self.spare_node()
        self.assertEqual(module.replica_spare(node)['ip'],'192.168.122.135')
        for drift in ('ip','name','uid','storage','owner','taint','ready'):
            bad=copy.deepcopy(node)
            if drift=='ip':bad['status']['addresses'][0]['address']='192.168.47.129'
            if drift=='name':bad['metadata']['name']='mongodb-node-212'
            if drift=='uid':bad['metadata']['uid']=''
            if drift in ('storage','owner'):
                bad['metadata']['labels']['kubeauto.io/mongodb-'+('storage' if drift=='storage' else 'replica-domain')]='foreign'
            if drift=='taint':bad['spec']['taints'].append({'key':'foreign','effect':'NoSchedule'})
            if drift=='ready':bad['status']['conditions'][0]['status']='False'
            with self.subTest(drift=drift),self.assertRaises(ValueError):module.replica_spare(bad)

    def test_replica_domains_only_accept_original_or_exact_promoted_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture,domains,nodes,get=self.domains(tmp)
            with patch.object(fixture,'get',side_effect=get):
                domains.verify()
                original=copy.deepcopy(nodes)
                for name in nodes:
                    nodes[name]['metadata']['labels']={'kubeauto.io/mongodb-storage':'true',
                        'kubeauto.io/mongodb-replica-domain':domains.run_id}
                    nodes[name]['spec']={'taints':[],'unschedulable':False}
                domains.verify()
                first=next(iter(nodes));nodes[first]['spec'].update(unschedulable=True,
                    taints=[{'key':'node.kubernetes.io/unschedulable','effect':'NoSchedule'}])
                domains.verify()
                for drift in ('uid','marker','taint','ip'):
                    bad=copy.deepcopy(original)
                    if drift=='uid':bad[first]['metadata']['uid']='foreign'
                    if drift=='marker':bad[first]['metadata']['labels']['kubeauto.io/mongodb-replica-domain']='foreign'
                    if drift=='taint':bad[first]['spec']['taints'].append({'key':'foreign','effect':'NoExecute'})
                    if drift=='ip':bad[first]['status']['addresses'][0]['address']='192.168.47.129'
                    with self.subTest(drift=drift),patch.object(fixture,'get',side_effect=lambda kind,name:
                            {'metadata':{'uid':'core'}} if kind=='namespace' else bad[name]),self.assertRaises(ValueError):
                        domains.verify()

    def test_replica_domain_prepare_checks_both_hosts_before_any_mutation(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture,domains,nodes,get=self.domains(tmp);domains.path.unlink()
            with patch.object(fixture,'get',side_effect=get),patch.object(fixture,'health'), \
                    patch.object(fixture,'k',return_value=''),patch.object(fixture,'ssh',side_effect=[
                        'installed=no\nactive=inactive\nenabled=not-found',ValueError('second host has foreign path')]) as ssh, \
                    patch.object(domains,'patch') as patch_node:
                with self.assertRaisesRegex(ValueError,'second host'):domains.prepare()
                patch_node.assert_not_called();self.assertFalse(domains.path.exists())
                self.assertTrue(all('dnf' not in call.args[1] and 'start iscsid' not in call.args[1]
                    for call in ssh.call_args_list))

    def test_replica_domain_absent_unit_has_empty_is_enabled_stdout(self):
        # Rocky9 systemctl reports ENOENT on stderr, with empty stdout.
        for installed,active,accepted in (('no','inactive',True),('yes','inactive',False),
                                          ('no','active',False)):
            with self.subTest(installed=installed,active=active),tempfile.TemporaryDirectory() as tmp:
                fixture,domains,_,get=self.domains(tmp)
                for row in domains.state['nodes'].values():
                    row['host']={'installed':installed,'active':active,'enabled':''}
                domains.save()
                with patch.object(fixture,'get',side_effect=get):
                    if accepted:domains.verify()
                    else:
                        with self.assertRaises(ValueError):domains.verify()

    def test_replica_domain_generated_drain_taint_allows_api_time_added_only(self):
        for drift in (None,'value','effect','extra'):
            with self.subTest(drift=drift),tempfile.TemporaryDirectory() as tmp:
                fixture,domains,nodes,get=self.domains(tmp)
                for node in nodes.values():
                    node['metadata']['labels']={'kubeauto.io/mongodb-storage':'true',
                        'kubeauto.io/mongodb-replica-domain':domains.run_id}
                    taint={'key':'node.kubernetes.io/unschedulable','effect':'NoSchedule',
                           'timeAdded':'2026-10-15T11:16:58Z'}
                    if drift=='value':taint['value']='foreign'
                    if drift=='effect':taint['effect']='NoExecute'
                    if drift=='extra':taint['foreign']='value'
                    node['spec']={'unschedulable':True,'taints':[taint]}
                with patch.object(fixture,'get',side_effect=get):
                    if drift is None:domains.verify()
                    else:
                        with self.assertRaises(ValueError):domains.verify()

    def test_replica_domain_recovery_does_not_stop_or_remove_absent_iscsi(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture,domains,_,get=self.domains(tmp)
            for row in domains.state['nodes'].values():row['host']['enabled']=''
            domains.save();bindir=Path(tmp,'bin');bindir.mkdir()
            for name,script in {
                'systemctl':'case "$1" in is-active) exit 3;; *) exit 5;; esac',
                'iscsiadm':'exit 1', 'dnf':'exit 29', 'findmnt':'exit 0'}.items():
                if name=='iscsiadm':continue  # command must genuinely be absent.
                p=bindir/name;p.write_text('#!/bin/sh\n'+script+'\n');p.chmod(0o700)
            def ssh(ip,script):
                if 'rm -rf' not in script:return 'installed=no\nactive=inactive\nenabled='
                script=script.replace('/var/lib/kubeauto-mongodb-longhorn',str(Path(tmp,'data')))
                result=subprocess.run(['bash','-se'],input=script,text=True,capture_output=True,
                    env=os.environ|{'PATH':str(bindir)+':/usr/bin:/bin'})
                self.assertEqual(result.returncode,0,result.stderr)
                return result.stdout
            with patch.object(fixture,'get',side_effect=get),patch.object(fixture,'k',return_value=''), \
                    patch.object(domains,'patch'),patch.object(domains,'wait'),patch.object(fixture,'wait_health'), \
                    patch.object(fixture,'ssh',side_effect=ssh):domains.restore()
            self.assertFalse(domains.path.exists())

    def test_replica_domain_csi_requires_five_healthy_nodes_and_both_plugins(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture,domains,_,_=self.domains(tmp)
            names=set(module.REPLICA_SPARES)|{'mongodb-node-'+ip.rsplit('.',1)[1] for ip in module.WORKERS}
            for drift in (None,'missing-node','manager','plugin'):
                def k(*args):
                    if 'nodes.longhorn.io' in args:
                        return json.dumps({'items':[{'metadata':{'name':name},'status':{'conditions':[
                            {'type':'Ready','status':'True'}]}} for name in names if not
                            (drift=='missing-node' and name=='mongodb-node-40')]})
                    return json.dumps({'items':[{'metadata':{'name':prefix+'test'},'status':{'conditions':[
                        {'type':'Ready','status':'False' if drift==role else 'True'}]}}
                        for role,prefix in [('manager','longhorn-manager-'),('plugin','longhorn-csi-plugin-')]]})
                with self.subTest(drift=drift),patch.object(fixture,'k',side_effect=k):
                    self.assertEqual(domains.ready(),drift is None)

    def test_replica_domain_cleanup_never_removes_label_or_host_before_replica_eviction(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture,domains,_,get=self.domains(tmp)
            native={'metadata':{'uid':'longhorn-node','resourceVersion':'2'},'spec':{'allowScheduling':True}}
            with patch.object(fixture,'get',side_effect=get),patch.object(fixture,'k',return_value=json.dumps(native)) as k, \
                    patch.object(domains,'wait',side_effect=ValueError('replicas remain')), \
                    patch.object(domains,'patch') as patch_node,patch.object(fixture,'ssh') as host:
                with self.assertRaisesRegex(ValueError,'replicas remain'):domains.restore()
                patch_node.assert_not_called();host.assert_not_called();self.assertTrue(domains.path.exists())
                patches=[json.loads(c.args[-1]) for c in k.call_args_list if 'patch' in c.args]
                self.assertEqual(patches[0]['spec'],{'allowScheduling':False,'evictionRequested':True})
                self.assertEqual(patches[0]['metadata']['uid'],'longhorn-node')

    def test_replica_domain_removal_requires_no_engines_pods_or_ready_manager(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture,domains,_,_=self.domains(tmp)
            name='mongodb-node-135'
            for drift in (None,'pod','replica','engine','ready','scheduling'):
                def k(*args):
                    if 'pods' in args:return json.dumps({'items':[{}] if drift=='pod' else []})
                    if 'replicas.longhorn.io' in args or 'engines.longhorn.io' in args:
                        present=drift=='replica' and 'replicas.longhorn.io' in args or drift=='engine' and 'engines.longhorn.io' in args
                        return json.dumps({'items':[{'spec':{'nodeID':name}}] if present else []})
                    return json.dumps({'spec':{'allowScheduling':drift=='scheduling'},'status':{'conditions':[
                        {'type':'Ready','status':'True' if drift=='ready' else 'False','reason':'ManagerPodMissing'}]}})
                with self.subTest(drift=drift),patch.object(fixture,'k',side_effect=k):
                    self.assertEqual(domains.removable(name),drift is None)

    def test_replica_domain_success_restores_both_hosts_and_blocks_pass_on_host_drift(self):
        from contextlib import redirect_stdout
        from io import StringIO
        for drift in (False,True):
            with self.subTest(drift=drift),tempfile.TemporaryDirectory() as tmp:
                fixture,domains,nodes,get=self.domains(tmp);events=[]
                longhorn=set(nodes)
                for node in nodes.values():
                    node['metadata']['labels']={'kubeauto.io/mongodb-storage':'true',
                        'kubeauto.io/mongodb-replica-domain':domains.run_id}
                    node['spec']={'taints':[],'unschedulable':False}
                def k(*args,**kwargs):
                    if args[:2]==('patch','node'):
                        name=args[2];body=json.loads(args[-1]);events.append(('restore-node',name))
                        nodes[name]['spec'].update(body['spec'])
                        for key,value in body['metadata']['labels'].items():
                            if value is None:nodes[name]['metadata']['labels'].pop(key,None)
                            else:nodes[name]['metadata']['labels'][key]=value
                        return ''
                    if 'patch' in args:events.append(('evict',args[4]));return ''
                    if 'drain' in args:events.append(('drain',args[1]));nodes[args[1]]['spec']['unschedulable']=True;return ''
                    if 'delete' in args:longhorn.discard(args[4]);events.append(('delete-native',args[4]));return ''
                    if 'pods' in args or 'replicas.longhorn.io' in args or 'engines.longhorn.io' in args:
                        return '{"items":[]}'
                    name=args[4]
                    return json.dumps({'metadata':{'uid':'native-'+name,'resourceVersion':'2'},
                        'spec':{'allowScheduling':False},'status':{'conditions':[
                            {'type':'Ready','status':'False','reason':'ManagerPodMissing'}]}}) if name in longhorn else ''
                def ssh(ip,script):
                    events.append(('host',ip))
                    if 'rm -rf' in script:return ''
                    return 'installed='+('yes' if drift else 'no')+'\nactive=inactive\nenabled=not-found'
                def wait(stage,predicate,**kwargs):
                    events.append(('wait',stage));self.assertTrue(predicate())
                with patch.object(fixture,'get',side_effect=get),patch.object(fixture,'k',side_effect=k), \
                        patch.object(fixture,'ssh',side_effect=ssh),patch.object(fixture,'wait_health'), \
                        patch.object(domains,'wait',side_effect=wait),redirect_stdout(StringIO()) as output:
                    if drift:
                        with self.assertRaisesRegex(ValueError,'host restoration'):domains.restore()
                        self.assertTrue(domains.path.exists());self.assertNotIn('MONGODB_REPLICA_DOMAINS_CLEAN_PASS',output.getvalue())
                    else:
                        domains.restore();self.assertFalse(domains.path.exists());self.assertEqual(longhorn,set())
                        self.assertIn('MONGODB_REPLICA_DOMAINS_CLEAN_PASS',output.getvalue())
                        for name,ip in module.REPLICA_SPARES.items():
                            self.assertLess(events.index(('evict',name)),events.index(('restore-node',name)))
                            self.assertLess(events.index(('delete-native',name)),events.index(('host',ip)))
                            self.assertEqual(nodes[name]['spec']['taints'],self.spare_node(name)['spec']['taints'])

    def test_convergence_wait_retries_only_readiness_and_has_a_bounded_deadline(self):
        fixture = module.Fixture(SimpleNamespace(base=ROOT, kubeconfig=ROOT / 'unused'))
        with patch.object(fixture, 'health', side_effect=[module.StorageNotReady('CSI pending'), None]) as health, \
                patch.object(module.time, 'sleep'):
            fixture.wait_health(timeout=30)
        self.assertEqual(health.call_count, 2)
        with patch.object(fixture, 'health', side_effect=ValueError('foreign namespace')) as health, \
                patch.object(module.time, 'sleep') as sleep:
            with self.assertRaisesRegex(ValueError, 'foreign namespace'):
                fixture.wait_health()
        health.assert_called_once()
        sleep.assert_not_called()
        with patch.object(fixture, 'health', side_effect=module.StorageNotReady('CSI pending')), \
                patch.object(module.time, 'monotonic', side_effect=[0, 31]):
            with self.assertRaisesRegex(ValueError, 'convergence deadline exceeded'):
                fixture.wait_health(timeout=30)

    def test_chart_ready_deployer_does_not_substitute_for_dynamically_created_csi(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = module.Fixture(SimpleNamespace(base=Path(tmp), kubeconfig=Path(tmp) / 'unused'))
            fixture.state_file.parent.mkdir(parents=True)
            fixture.state_file.write_text(json.dumps({'nodes': {'worker': next(iter(module.NODES))},
                                                      'cluster_uid': 'core', 'namespace': module.NAMESPACE}))
            sc = {'metadata': {'labels': module.LABELS}, 'provisioner': 'driver.longhorn.io',
                  'allowVolumeExpansion': True, 'volumeBindingMode': 'WaitForFirstConsumer'}
            ready = {'metadata': {'generation': 1}, 'spec': {'replicas': 1},
                     'status': {'desiredNumberScheduled': 3, 'numberReady': 3, 'readyReplicas': 1, 'observedGeneration': 1}}
            def get(kind, name=None):
                if kind == 'nodes': return {'items': []}
                if kind == 'namespace' and name == 'kube-system': return {'metadata': {'uid': 'core'}}
                if kind == 'namespace': return {'metadata': {'labels': module.LABELS}}
                if kind == 'storageclass': return sc
                raise AssertionError(kind)
            calls = []
            def k(*command, **kwargs):
                calls.append(command)
                if 'longhorn-csi-plugin' in command: return ''
                return json.dumps(ready)
            with patch.object(fixture, 'get', side_effect=get), patch.object(fixture, 'k', side_effect=k), \
                    patch.object(module, 'node_addresses', return_value={'worker': next(iter(module.NODES))}):
                with self.assertRaisesRegex(module.StorageNotReady, 'not created: longhorn-csi-plugin'):
                    fixture.health()
            self.assertTrue(any('longhorn-csi-plugin' in c for c in calls))

    def test_probe_objects_bind_claim_and_only_a_leased_worker_without_database_credentials(self):
        pvc, pod = module.probe_objects('1234567890abcdef', 'mongodb-node-212')
        self.assertEqual(pvc['metadata']['namespace'], module.NAMESPACE)
        self.assertEqual(pvc['spec']['storageClassName'], module.STORAGE_CLASS)
        self.assertEqual(pod['spec']['volumes'][0]['persistentVolumeClaim']['claimName'], pvc['metadata']['name'])
        self.assertEqual(pod['spec']['nodeSelector'], {'kubernetes.io/hostname': 'mongodb-node-212'})
        self.assertNotIn('secret', json.dumps(pod).lower())
        for run_id, node in [('invalid/name', 'mongodb-node-212'), ('1234567890abcdef', 'redis-node-131')]:
            with self.assertRaises(ValueError):
                module.probe_objects(run_id, node)

    def test_failed_create_response_is_recovered_by_exact_run_label_and_uid_precondition(self):
        with tempfile.TemporaryDirectory() as tmp:
            args = SimpleNamespace(base=Path(tmp), kubeconfig=Path(tmp) / 'kubeconfig')
            fixture = module.Fixture(args)
            fixture.state_file.parent.mkdir(parents=True)
            calls = []
            current = {}
            def api(*command, **kwargs):
                calls.append(command)
                if command[0] == 'create':
                    obj = json.loads(kwargs['input_text'])
                    obj['metadata']['uid'] = 'owned-before-response-lost'
                    current['pvc'] = obj
                    raise ValueError('first-create-response-lost')
                if 'get' in command:
                    i = command.index('get')
                    kind = command[i+1]
                    if command[-1] == 'name':
                        return ''
                    return json.dumps(current[kind]) if kind in current else ''
                if command[:2] == ('delete', '--raw'):
                    self.assertEqual(json.loads(kwargs['input_text'])['preconditions'], {'uid': 'owned-before-response-lost'})
                    current.clear()
                return ''
            with patch.object(fixture, 'health'), patch.object(fixture, 'k', side_effect=api):
                with self.assertRaisesRegex(ValueError, 'first-create-response-lost'):
                    fixture.probe()
            self.assertFalse(current)
            self.assertTrue(any(c[:2] == ('delete', '--raw') for c in calls))
            self.assertFalse(fixture.state_file.with_name('mongodb-storage-probe.json').exists())

    def test_foreign_probe_replacement_blocks_cleanup_and_keeps_recovery_journal(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = module.Fixture(SimpleNamespace(base=Path(tmp), kubeconfig=Path(tmp) / 'kubeconfig'))
            fixture.state_file.parent.mkdir(parents=True)
            created = False
            def api(*command, **kwargs):
                nonlocal created
                if command[0] == 'create':
                    created = True
                    raise ValueError('first-create-failed')
                if 'get' in command:
                    if not created or command[-1] == 'name':
                        return ''
                    return json.dumps({'metadata': {'uid': 'foreign', 'labels': {}}})
                raise AssertionError('foreign fixture must never be deleted')
            with patch.object(fixture, 'health'), patch.object(fixture, 'k', side_effect=api):
                with self.assertRaisesRegex(ValueError, 'first-create-failed'):
                    fixture.probe()
            self.assertTrue(fixture.state_file.with_name('mongodb-storage-probe.json').exists())

    def test_exact_image_records_render_owned_registry_paths(self):
        evidence = json.loads((ROOT / 'tests/evidence/redis-storage-images-20261012.json').read_text())
        values, images = module.image_plan(ROOT / 'tests/fixtures/longhorn-1.10.1.tgz', evidence)
        self.assertEqual(len(images), 13)
        self.assertEqual(values['longhornManager']['nodeSelector'], {'kubeauto.io/mongodb-storage': 'true'})
        self.assertEqual(values['defaultSettings']['defaultDataPath'], '/var/lib/kubeauto-mongodb-longhorn')
        self.assertTrue(all(image['repository'].startswith('mongodb-lab/brinnatt/')
                            for group in values['image'].values() for image in group.values()))
        bad = copy.deepcopy(evidence)
        bad['images'].pop()
        with self.assertRaises(ValueError):
            module.image_plan(ROOT / 'tests/fixtures/longhorn-1.10.1.tgz', bad)

    def test_foreign_node_version_and_incomplete_pool_are_refused(self):
        nodes = [{'metadata': {'name': 'mongodb-' + ip.rsplit('.', 1)[1]},
                  'status': {'addresses': [{'type': 'InternalIP', 'address': ip}],
                             'nodeInfo': {'kubeletVersion': 'v1.33.6'},
                             'conditions': [{'type': 'Ready', 'status': 'True'}]}} for ip in sorted(module.NODES)]
        self.assertEqual(set(module.node_addresses(nodes).values()), module.NODES)
        with self.assertRaises(ValueError):
            module.node_addresses(nodes[:-1])
        nodes[0]['status']['nodeInfo']['kubeletVersion'] = 'v1.34.0'
        with self.assertRaises(ValueError):
            module.node_addresses(nodes)

    def test_cleanup_identity_remains_component_specific(self):
        self.assertEqual(module.NAMESPACE, 'mongodb-lab-storage')
        self.assertEqual(module.STORAGE_CLASS, 'mongodb-longhorn')
        self.assertEqual(module.DATA_PATH, '/var/lib/kubeauto-mongodb-longhorn')
        self.assertEqual(module.LABELS['kubeauto.io/component'], 'mongodb-test-storage')
