"""Leased lab identity, credentials, immutable cache and safe cleanup contracts."""
import copy
import importlib.util
import json
import hashlib
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from cryptography import x509
from cryptography.hazmat.primitives.asymmetric import padding
import yaml

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('mongodb_lab', ROOT / 'tests/helpers/mongodb-lab.py')
lab = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lab)


def baseline():
    nodes = [{'metadata': {'name': 'mongodb-node-' + ip.rsplit('.', 1)[1]},
              'status': {'addresses': [{'type': 'InternalIP', 'address': ip}],
                         'nodeInfo': {'kubeletVersion': 'v1.33.6'},
                         'conditions': [{'type': 'Ready', 'status': 'True'}]}} for ip in lab.NODES]
    namespaces = [{'metadata': {'name': 'kube-system', 'uid': 'owned-cluster'}}]
    owner = {'cluster': lab.CLUSTER, 'nodes': list(lab.NODES), 'cluster_uid': 'owned-cluster'}
    return nodes, namespaces, owner


class MongoDBLabTests(unittest.TestCase):
    def test_install_failure_cleans_client_before_lab_and_stops_on_identity_rejection(self):
        source = ROOT.joinpath('tests/helpers/mongodb-install-gate.sh').read_text()
        function = source.split('on_exit() {', 1)[1].split('\n}\n', 1)[0]
        for mutated in ('yes', 'no'):
            for rejected in (False, True):
                with self.subTest(mutated=mutated, rejected=rejected), tempfile.TemporaryDirectory() as tmp:
                    work = Path(tmp)
                    python = work / 'python'
                    python.write_text('#!/bin/bash\nprintf "%s\\n" "$2" >> "$work/calls"\n'
                                      'if [[ "$2" == cleanup-install-client ]]; then exit "$rejected"; fi\n')
                    python.chmod(0o700)
                    script = ('set -Eeuo pipefail\non_exit() {' + function + '\n}\n'
                              'work=$1; PY=$work/python; BASE=$work; KC=$work/kubeconfig; touch "$KC"\n'
                              'control_config=$work/control; relay_pid=; phase=product; mutated=$2; rejected=$3\n'
                              'export work rejected\nclean_lab() { echo clean-lab >> "$work/calls"; }\n'
                              'trap on_exit EXIT\nexit 7\n')
                    result = lab.subprocess.run(['bash', '-c', script, 'contract', tmp, mutated,
                                                 '9' if rejected else '0'], capture_output=True, text=True, timeout=10)
                    self.assertEqual(result.returncode, 7, result.stderr)
                    expected = ['diagnostics', 'cleanup-install-client']
                    if mutated == 'yes' and not rejected: expected.append('clean-lab')
                    self.assertEqual((work / 'calls').read_text().splitlines(), expected)
                    self.assertIn('cleanup_rc=' + ('9' if rejected else '0'),
                                  (work / 'first-failure.status').read_text())

    def test_install_client_cold_pull_has_bounded_ten_minute_wait(self):
        source = ROOT.joinpath('tests/helpers/mongodb-install-gate.sh').read_text()
        command = next(line for line in source.splitlines()
                       if line.startswith('kubectl ') and 'condition=Ready pod/mongodb-gate-client' in line)
        result = lab.subprocess.run(['bash', '-c',
            'kubectl() { printf "%s\\n" "$@"; }; ' + command], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0)
        self.assertIn('--timeout=600s', result.stdout.splitlines())

    def test_full_suite_covers_every_runtime_case_and_stops_at_first_unmet_gate(self):
        for failure in (None,'stdin-reader','truncated-plan','artifacts-only','invalid-focus','pending','stale-static','dirty','exit','marker','marker-prefix','embedded-marker','failure-marker','clean-marker'):
            with self.subTest(failure=failure),tempfile.TemporaryDirectory() as tmp:
                base=Path(tmp);work=base/'work';work.mkdir();(base/'.venv').symlink_to(ROOT/'.venv',target_is_directory=True)
                (base/'tests/helpers').mkdir(parents=True);(base/'tests/evidence').mkdir();calls=base/'calls'
                data=yaml.safe_load((ROOT/'tests/mongodb-test-matrix.yaml').read_text())
                for case in data['cases']:
                    case.update(status='pass',evidence='tests/evidence/accepted.json')
                data['cases'][0]['evidence']='tests/evidence/static.json'
                if failure=='pending':data['cases'][2].update(status='pending',evidence='')
                (base/'tests/evidence/accepted.json').write_text('{}')
                (base/'input').write_text('immutable')
                (base/'tests/evidence/static.json').write_text(json.dumps({'dependencies':{'input':
                    'bad' if failure=='stale-static' else hashlib.sha256(b'immutable').hexdigest()}}))
                (base/'tests/mongodb-test-matrix.yaml').write_text(yaml.safe_dump(data))
                if failure=='dirty':(base/'clusters/mongodb-gate').mkdir(parents=True)
                markers={c['group']:c['success_marker'] for c in data['cases']}
                markers.update(clean='MONGODB_CLEAN_VERIFY_PASS',**{'sharding-clean':'MONGODB_SHARDING_CLEAN_PASS'})
                rows=['#!/bin/bash','set -eu','module=${1#--}',
                    'printf "%s\\n" "$module" >> "$MONGODB_CONTRACT_CALLS"',
                    'if [[ "$module" == upgrade && "${MONGODB_CONTRACT_FAILURE:-}" == stdin-reader ]]; then cat >/dev/null; fi',
                    'if [[ "$module" == upgrade && "${MONGODB_CONTRACT_FAILURE:-}" == truncated-plan ]]; then : > "$MONGODB_CONTRACT_PLAN"; fi',
                    'if [[ "$module" == storage && "${MONGODB_CONTRACT_FAILURE:-}" == exit ]]; then exit 7; fi',
                    'if [[ "$module" == storage && "${MONGODB_CONTRACT_FAILURE:-}" == marker ]]; then exit 0; fi',
                    'if [[ "$module" == storage && "${MONGODB_CONTRACT_FAILURE:-}" == marker-prefix ]]; then echo MONGODB_STORAGE_PASS_EXTRA; exit 0; fi',
                    'if [[ "$module" == storage && "${MONGODB_CONTRACT_FAILURE:-}" == embedded-marker ]]; then echo "noise MONGODB_STORAGE_PASS"; exit 0; fi',
                    'if [[ "$module" == storage && "${MONGODB_CONTRACT_FAILURE:-}" == failure-marker ]]; then echo MONGODB_TEST_FAIL; fi',
                    'case "$module" in']
                rows += [name+') echo '+marker+(' images=13 current-talkedu=true dual-registry-publication=verified' if name=='artifacts' else '')+';;'
                         for name,marker in markers.items()]
                rows += ['*) exit 99;;','esac',
                    'if [[ "$module" == clean && "${MONGODB_CONTRACT_FAILURE:-}" != clean-marker ]]; then echo LAB_CLEAN_VERIFY_PASS; fi']
                (base/'tests/helpers/mongodb-install-gate.sh').write_text('\n'.join(rows)+'\n')
                (base/'tests/helpers/lab-wipe-nodes.sh').write_text('printf "verify\\n" >> "$MONGODB_CONTRACT_CALLS"\n')
                script='set -euo pipefail\nsource tests/helpers/mongodb-runner.sh\nmongodb_full_suite "$1" "$2" "$3"'
                focus={'artifacts-only':'artifacts','invalid-focus':'storage'}.get(failure,'')
                result=lab.subprocess.run(['bash','-c',script,'contract',str(base),str(work),focus],cwd=ROOT,
                    env=os.environ|{'MONGODB_CONTRACT_CALLS':str(calls),'MONGODB_CONTRACT_FAILURE':failure or '',
                                    'MONGODB_CONTRACT_PLAN':str(work/'full-plan.tsv')},
                    capture_output=True,text=True,timeout=20)
                visited=calls.read_text().splitlines() if calls.exists() else []
                if failure and failure not in ('artifacts-only','stdin-reader'):
                    self.assertNotEqual(result.returncode,0,result.stdout)
                    self.assertNotIn('MONGODB_FULL_PASS',result.stdout.splitlines())
                    if failure in ('invalid-focus','pending','stale-static','dirty'):self.assertEqual(visited,[])
                    elif failure=='truncated-plan':
                        self.assertEqual(visited,['verify','artifacts','upgrade'])
                        self.assertIn('MONGODB_FULL_FAIL reason=incomplete-runtime-plan',result.stderr)
                    elif failure=='clean-marker':self.assertEqual(visited,['verify','artifacts','upgrade','clean'])
                    else:
                        self.assertEqual(visited[-1],'storage');self.assertNotIn('data',visited)
                        record=json.loads((work/'full-7-storage.status.json').read_text())
                        if failure=='exit':self.assertEqual(record['rc'],7)
                        if failure in ('marker','marker-prefix','embedded-marker'):self.assertFalse(record['marker_present'])
                        if failure=='failure-marker':self.assertEqual(record['failure_markers'],1)
                elif failure=='artifacts-only':
                    self.assertEqual(result.returncode,0,result.stderr)
                    self.assertEqual(visited,['verify','artifacts'])
                    self.assertIn('MONGODB_FULL_ARTIFACTS_PASS',result.stdout.splitlines())
                    self.assertNotIn('MONGODB_FULL_PASS',result.stdout.splitlines())
                    record=json.loads((work/'full-1-artifacts.status.json').read_text())
                    self.assertTrue(record['marker_present']);self.assertEqual(record['rc'],0)
                else:
                    self.assertEqual(result.returncode,0,result.stderr)
                    self.assertEqual(visited[:5],['verify','artifacts','upgrade','clean','install'])
                    self.assertEqual(visited[-2:],['cleanup','clean'])
                    expected=set(markers)-{'static','disabled','validation','documentation','full'}
                    self.assertEqual(set(visited)-{'verify'},expected)
                    self.assertIn('MONGODB_FULL_PASS',result.stdout.splitlines())
                    self.assertEqual(len(list(work.glob('*.status.json'))),len(visited)-1)

    def test_full_artifacts_fixed_dispatch_reaches_only_its_owning_full_wrapper(self):
        script=('source tests/helpers/mongodb-runner.sh\n'
                'mongodb_launch() { printf "%s %s\\n" "$1" "$2"; }\n'
                'mongodb_dispatch --mongodb-full-artifacts')
        result=lab.subprocess.run(['bash','-c',script],cwd=ROOT,capture_output=True,text=True,timeout=10)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(result.stdout.strip(),'full-artifacts MONGODB_FULL_ARTIFACTS_PASS')
        runner=ROOT.joinpath('tests/helpers/mongodb-runner.sh').read_text()
        allowed=next(s for s in runner.splitlines() if s.startswith('case "$mode" in'))
        result=lab.subprocess.run(['bash','-c','mode=--full-artifacts; '+allowed],capture_output=True,text=True,timeout=10)
        self.assertEqual(result.returncode,0,result.stderr)
        installer=ROOT.joinpath('tests/helpers/mongodb-install-gate.sh').read_text()
        parse=installer.split('BASE=',1)[0]
        branch=installer.split('if [[ "$MODE" == --full || "$MODE" == --full-artifacts ]]; then',1)[1].split('if [[ "$MODE" == --registry-recover',1)[0]
        script=(parse+'\nBASE=/fixture; work=/private\nsource() { :; }\n'
                'mongodb_full_suite() { printf "%s %s %s\\n" "$1" "$2" "$3"; }\n'
                'if [[ "$MODE" == --full || "$MODE" == --full-artifacts ]]; then'+branch)
        result=lab.subprocess.run(['bash','-c',script,'contract','--full-artifacts'],capture_output=True,text=True,timeout=10)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(result.stdout.strip(),'/fixture /private artifacts')

    def cleanup_objects(self):
        def obj(kind, name, uid, labels=None, parent=None):
            meta = {'name': name, 'namespace': lab.CLUSTER, 'uid': uid, 'labels': labels or {}}
            if parent:meta['ownerReferences'] = [dict(zip(('kind','name','uid'), parent))]
            return {'kind': kind, 'metadata': meta}
        source = obj('PerconaServerMongoDB', lab.NAME, 'source', lab.LABELS)
        sts = obj('StatefulSet', lab.NAME+'-rs0', 'sts', parent=('PerconaServerMongoDB', lab.NAME, 'source'))
        pod = obj('Pod', lab.NAME+'-rs0-0', 'pod', parent=('StatefulSet', lab.NAME+'-rs0', 'sts'))
        revision = obj('ControllerRevision', lab.NAME+'-rs0-hash', 'revision', parent=('StatefulSet', lab.NAME+'-rs0', 'sts'))
        return [pod, revision, sts, source]

    def test_cleanup_graph_follows_uid_bound_controller_chains_independent_of_order(self):
        objects = self.cleanup_objects()
        self.assertEqual(lab.cleanup_graph(objects, 'source'), {'source', 'sts', 'pod', 'revision'})
        with self.assertRaisesRegex(ValueError, 'source identity'):
            lab.cleanup_graph(objects, 'replaced-source')

    def test_cleanup_graph_refuses_unknown_or_rebound_or_disconnected_resources(self):
        for change in ('foreign-pod', 'foreign-secret', 'foreign-kind', 'rebound-owner', 'missing-owner', 'wrong-namespace'):
            with self.subTest(change=change):
                objects = self.cleanup_objects()
                if change.startswith('foreign-'):
                    objects.append({'kind': {'foreign-pod':'Pod','foreign-secret':'Secret','foreign-kind':'Job'}[change],
                        'metadata': {'name':'foreign','uid':'foreign','namespace':lab.CLUSTER,'labels':lab.LABELS}})
                elif change == 'rebound-owner':objects[0]['metadata']['ownerReferences'][0]['uid']='replaced-sts'
                elif change == 'missing-owner':objects[0]['metadata'].pop('ownerReferences')
                else:objects[0]['metadata']['namespace']='foreign'
                with self.assertRaises(ValueError):lab.cleanup_graph(objects, 'source')

    def test_cleanup_graph_requires_exact_helm_and_operator_roots(self):
        objects=self.cleanup_objects()
        meta={'name':'mongodb-operator','namespace':lab.CLUSTER,'uid':'operator',
            'labels':{'app.kubernetes.io/managed-by':'Helm'},
            'annotations':{'meta.helm.sh/release-name':'mongodb-operator','meta.helm.sh/release-namespace':lab.CLUSTER}}
        objects.append({'kind':'Deployment','metadata':meta})
        self.assertIn('operator',lab.cleanup_graph(objects,'source'))
        self.assertIn('operator',lab.cleanup_graph(objects,'source',root_uids={'Deployment/mongodb-operator':'operator'}))
        with self.assertRaisesRegex(ValueError,'root UID changed'):
            lab.cleanup_graph(objects,'source',root_uids={'Deployment/mongodb-operator':'old-operator'})
        meta['annotations']['meta.helm.sh/release-namespace']='foreign'
        with self.assertRaises(ValueError):lab.cleanup_graph(objects,'source')

    def test_cleanup_graph_pvc_needs_preverified_binding_and_endpoints_need_owned_service(self):
        objects=self.cleanup_objects()
        claim={'kind':'PersistentVolumeClaim','metadata':{'name':'mongod-data-'+lab.NAME+'-rs0-0',
            'namespace':lab.CLUSTER,'uid':'claim','labels':{'app.kubernetes.io/instance':lab.NAME}}}
        objects.append(claim)
        with self.assertRaises(ValueError):lab.cleanup_graph(objects,'source')
        self.assertIn('claim',lab.cleanup_graph(objects,'source',{'claim'}))
        service={'kind':'Service','metadata':{'name':lab.NAME+'-metrics','namespace':lab.CLUSTER,
            'uid':'service','labels':lab.LABELS}}
        endpoints={'kind':'Endpoints','metadata':{'name':lab.NAME+'-metrics','namespace':lab.CLUSTER,
            'uid':'endpoints','labels':{'endpoints.kubernetes.io/managed-by':'endpoint-controller'}}}
        objects.extend((endpoints,service))
        self.assertIn('endpoints',lab.cleanup_graph(objects,'source',{'claim'}))
        endpoints['metadata']['name']='foreign'
        with self.assertRaises(ValueError):lab.cleanup_graph(objects,'source',{'claim'})

    def test_cleanup_graph_interruption_uses_historical_root_and_refuses_unbound_descendants(self):
        objects=self.cleanup_objects()[:-1]
        self.assertIn('pod',lab.cleanup_graph(objects,'source'))
        with self.assertRaises(ValueError):lab.cleanup_graph(objects,None)

    def test_cleanup_graph_recognizes_only_pinned_unowned_pbm_configuration(self):
        secret={'kind':'Secret','type':'Opaque','data':{'pbm_config.yaml':'dW5pdA=='},
            'metadata':{'name':lab.NAME+'-pbm-config','namespace':lab.CLUSTER,'uid':'pbm',
                'labels':{'app.kubernetes.io/instance':lab.NAME,
                    'app.kubernetes.io/managed-by':'percona-server-mongodb-operator',
                    'app.kubernetes.io/name':'percona-server-mongodb',
                    'app.kubernetes.io/part-of':'percona-server-mongodb'}}}
        for change in (None,'name','label','type','data','owner','source','namespace'):
            with self.subTest(change=change):
                obj=copy.deepcopy(secret);objects=self.cleanup_objects();source='source'
                if change=='name':obj['metadata']['name']='customer-pbm-config'
                elif change=='label':obj['metadata']['labels']['app.kubernetes.io/instance']='customer'
                elif change=='type':obj['type']='kubernetes.io/tls'
                elif change=='data':obj['data']['other']='dW5pdA=='
                elif change=='owner':obj['metadata']['ownerReferences']=[{'kind':'Secret','name':'customer','uid':'customer'}]
                elif change=='source':objects=objects[:-1];source=None
                elif change=='namespace':obj['metadata']['namespace']='customer'
                objects.append(obj)
                if change is None:
                    self.assertIn('pbm',lab.cleanup_graph(objects,source))
                    self.assertIn('pbm',lab.cleanup_graph(objects[:-2]+[obj],'source'))
                else:
                    with self.assertRaises(ValueError):lab.cleanup_graph(objects,source)

    def test_cleanup_graph_leader_lease_requires_exact_helm_operator_pod_chain(self):
        dep={'kind':'Deployment','metadata':{'name':'mongodb-operator','namespace':lab.CLUSTER,
            'uid':'operator','labels':{'app.kubernetes.io/managed-by':'Helm'},'annotations':{
                'meta.helm.sh/release-name':'mongodb-operator','meta.helm.sh/release-namespace':lab.CLUSTER}}}
        rs={'kind':'ReplicaSet','metadata':{'name':'mongodb-operator-hash','namespace':lab.CLUSTER,
            'uid':'replicaset','ownerReferences':[{'kind':'Deployment','name':'mongodb-operator','uid':'operator'}]}}
        pod={'kind':'Pod','metadata':{'name':'mongodb-operator-hash-pod','namespace':lab.CLUSTER,
            'uid':'operator-pod','ownerReferences':[{'kind':'ReplicaSet','name':'mongodb-operator-hash','uid':'replicaset'}]}}
        lease={'kind':'Lease','metadata':{'name':'08db0feb.percona.com','namespace':lab.CLUSTER,'uid':'leader'},
            'spec':{'holderIdentity':'mongodb-operator-hash-pod_12345678-1234-1234-1234-123456789abc','leaseDurationSeconds':15}}
        for change in (None,'name','holder','uuid','duration','owner','pod','replicaset','deployment'):
            with self.subTest(change=change):
                items=copy.deepcopy([lease,pod,rs,dep]);l,p,r,d=items
                if change=='name':l['metadata']['name']='customer-leader'
                elif change=='holder':l['spec']['holderIdentity']='customer-pod_12345678-1234-1234-1234-123456789abc'
                elif change=='uuid':l['spec']['holderIdentity']='mongodb-operator-hash-pod_invalid'
                elif change=='duration':l['spec']['leaseDurationSeconds']=30
                elif change=='owner':l['metadata']['ownerReferences']=[{'kind':'Deployment','name':'customer','uid':'customer'}]
                elif change=='pod':p['metadata']['ownerReferences'][0]['uid']='foreign'
                elif change=='replicaset':r['metadata']['ownerReferences'][0]['uid']='foreign'
                elif change=='deployment':d['metadata']['annotations']['meta.helm.sh/release-name']='customer'
                objects=items+self.cleanup_objects()
                if change is None:self.assertIn('leader',lab.cleanup_graph(objects,'source'))
                else:
                    with self.assertRaises(ValueError):lab.cleanup_graph(objects,'source')

    def test_cleanup_inventory_discovers_every_stored_type_and_never_ignores_api_failures(self):
        instance=lab.Lab(ROOT)
        resources='pods\nsecrets\nconfigmaps\nunknown.example.com\nevents\npods.metrics.k8s.io\n'
        with patch.object(instance,'run',return_value=resources), \
                patch.object(instance,'get',return_value={'items':[]}) as get:
            self.assertEqual(instance.namespace_objects(),[])
            self.assertEqual({c.args[0] for c in get.call_args_list},{'pods','secrets','configmaps','unknown.example.com'})
            self.assertTrue(all(c.kwargs=={'ns':lab.CLUSTER} for c in get.call_args_list))
            get.reset_mock()
            self.assertEqual(instance.namespace_objects('independent-namespace'),[])
            self.assertTrue(all(c.kwargs=={'ns':'independent-namespace'} for c in get.call_args_list))
        with patch.object(instance,'run',return_value=resources), \
                patch.object(instance,'get',side_effect=ValueError('API unavailable')):
            with self.assertRaisesRegex(ValueError,'API unavailable'):instance.namespace_objects()

    def test_bind_product_requires_original_namespace_or_independent_source_uid_evidence(self):
        for mode in ('construction','backup','missing','changed'):
            with self.subTest(mode=mode),tempfile.TemporaryDirectory() as tmp:
                instance=lab.Lab(ROOT);instance.directory=Path(tmp);instance.owner_path=Path(tmp,'owner.json')
                owner={'cluster_uid':'core','healthy':False}
                if mode=='construction':owner['namespace_uid']='namespace'
                if mode in ('backup','changed'):
                    Path(tmp,'mongodb-backup-physical.json').write_text(json.dumps({
                        'cluster_uid':'core','source_cluster_uid':'source' if mode=='backup' else 'old-source'}))
                namespace={'metadata':{'uid':'namespace','labels':lab.LABELS}}
                source={'metadata':{'uid':'source','labels':lab.LABELS}}
                def get(kind,*args,**kwargs):
                    return namespace if kind=='namespace' else source if kind=='psmdb' else {'metadata':{}} if kind=='crd' else None
                with patch.object(instance,'verify',return_value=owner),patch.object(instance,'get',side_effect=get):
                    if mode in ('missing','changed'):
                        with self.assertRaisesRegex(ValueError,'source UID evidence'):instance.bind_product()
                        self.assertFalse(instance.owner_path.exists())
                    else:
                        instance.bind_product()
                        self.assertEqual(json.loads(instance.owner_path.read_text())['product_identity'],
                            {'namespace_uid':'namespace','source_uid':'source','crds':{},'roots':{}})

    def test_bind_product_replaces_retired_receipt_only_for_a_new_construction_namespace(self):
        for drift in (None, 'not-retired', 'creation-uid', 'same-namespace', 'foreign'):
            with self.subTest(drift=drift), tempfile.TemporaryDirectory() as tmp:
                instance=lab.Lab(ROOT);instance.directory=Path(tmp);instance.owner_path=Path(tmp,'owner.json')
                retired={'namespace_uid':'old-namespace','source_uid':'old-source','crds':{},'roots':{}}
                owner={'healthy':False,'product_clean':drift!='not-retired',
                       'namespace_uid':'foreign' if drift=='creation-uid' else 'new-namespace',
                       'product_identity':retired}
                namespace={'metadata':{'uid':'old-namespace' if drift=='same-namespace' else 'new-namespace',
                                       'labels':{} if drift=='foreign' else lab.LABELS}}
                source={'metadata':{'uid':'new-source','labels':lab.LABELS}}
                def get(kind,*args,**kwargs):
                    return namespace if kind=='namespace' else source if kind=='psmdb' else {'metadata':{}} if kind=='crd' else None
                with patch.object(instance,'verify',return_value=owner),patch.object(instance,'get',side_effect=get):
                    if drift:
                        with self.assertRaises(ValueError):instance.bind_product()
                        self.assertFalse(instance.owner_path.exists())
                    else:
                        instance.bind_product();saved=json.loads(instance.owner_path.read_text())
                        self.assertEqual(saved['product_identity'],
                            {'namespace_uid':'new-namespace','source_uid':'new-source','crds':{},'roots':{}})
                        self.assertNotIn('product_clean',saved)

    def test_cleanup_qualification_checks_complete_graph_and_does_not_delete_even_when_namespace_absent(self):
        for namespace_present,foreign,changed_uid in ((False,False,False),(True,False,False),
                                                     (True,True,False),(True,False,True)):
            with self.subTest(namespace=namespace_present,foreign=foreign,changed_uid=changed_uid), tempfile.TemporaryDirectory() as tmp:
                instance=lab.Lab(ROOT);instance.directory=Path(tmp)
                owner={'product_identity':{'namespace_uid':'namespace','source_uid':'source'}}
                objects=self.cleanup_objects()
                if foreign:objects.append({'kind':'ConfigMap','metadata':{
                    'name':'customer','namespace':lab.CLUSTER,'uid':'customer','labels':{}}})
                def get(kind,*args,**kwargs):
                    if kind=='namespace':return {'metadata':{'uid':'changed' if changed_uid else 'namespace'}} if namespace_present else None
                    if kind in ('pv','pvc'):return {'items':[]}
                    raise AssertionError(kind)
                with patch.object(instance,'verify',return_value=owner),patch.object(instance,'get',side_effect=get), \
                        patch.object(instance,'namespace_objects',return_value=objects), \
                        patch.object(instance,'delete_uid') as delete,patch.object(instance,'run') as run:
                    if foreign or changed_uid:
                        with self.assertRaises(ValueError):instance.cleanup_product(qualify_only=True)
                    else:instance.cleanup_product(qualify_only=True)
                    delete.assert_not_called();run.assert_not_called()

    def test_cleanup_delete_uses_server_side_uid_precondition(self):
        instance=lab.Lab(ROOT)
        with patch.object(lab.subprocess,'run',return_value=SimpleNamespace(returncode=0)) as submit:
            instance.delete_uid('/api/v1/namespaces/'+lab.CLUSTER,'namespace-uid')
        self.assertEqual(json.loads(submit.call_args.kwargs['input'])['preconditions'],{'uid':'namespace-uid'})
        self.assertIn('--raw',submit.call_args.args[0])

    def test_complete_cleanup_proves_graph_before_each_uid_bound_delete_and_preserves_foreign_graph(self):
        for foreign in (False,True):
            with self.subTest(foreign=foreign),tempfile.TemporaryDirectory() as tmp:
                instance=lab.Lab(ROOT);instance.directory=Path(tmp);instance.owner_path=Path(tmp,'owner.json')
                owner={'product_identity':{'namespace_uid':'namespace','source_uid':'source','roots':{}},'created_crds':[]}
                source=self.cleanup_objects()[-1];deleted=[];graphs=[]
                def get(kind,*args,**kwargs):
                    if kind=='namespace':return {'metadata':{'uid':'namespace'}}
                    if kind in ('pvc','pv'):return {'items':[]}
                    if kind=='crd':return {'metadata':{'name':'perconaservermongodbs.psmdb.percona.com'}}
                    if kind=='psmdb':return None if 'source' in deleted else source
                    raise AssertionError(kind)
                def graph():
                    graphs.append(tuple(deleted))
                    objects=self.cleanup_objects()
                    if deleted:objects=objects[:-1]
                    if foreign:objects.append({'kind':'Job','metadata':{'name':'customer',
                        'namespace':lab.CLUSTER,'uid':'customer','labels':{}}})
                    return objects
                def delete(endpoint,uid):
                    self.assertTrue(graphs)
                    deleted.append(uid)
                with patch.object(instance,'verify',return_value=owner),patch.object(instance,'get',side_effect=get), \
                        patch.object(instance,'namespace_objects',side_effect=graph),patch.object(instance,'delete_uid',side_effect=delete), \
                        patch.object(instance,'run') as run:
                    if foreign:
                        with self.assertRaisesRegex(ValueError,'foreign or disconnected'):instance.cleanup_product()
                        self.assertEqual(deleted,[]);run.assert_not_called()
                    else:
                        instance.cleanup_product()
                        self.assertEqual(deleted,['source','namespace'])
                        self.assertEqual(graphs,[(),('source',)])
                        self.assertTrue(json.loads(instance.owner_path.read_text())['product_clean'])

    def test_cleanup_qualification_routes_only_to_read_only_fixed_gate(self):
        script='''source tests/helpers/mongodb-runner.sh
mongodb_launch() { printf '%s|%s\\n' "$1" "$2"; }
mongodb_dispatch --mongodb-cleanup-qualification
'''
        result=lab.subprocess.run(['bash','-euc',script],cwd=ROOT,capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(result.stdout.strip(),'cleanup-qualification|MONGODB_CLEANUP_QUALIFICATION_PASS')
        source=ROOT.joinpath('tests/helpers/mongodb-install-gate.sh').read_text()
        branch=source.split('  --cleanup-qualification)\n',1)[1].split('    ;;',1)[0]
        self.assertIn('bind-product --base',branch)
        self.assertIn('cleanup-qualification --base',branch)
        self.assertNotIn('clean_lab',branch)

    def test_target_inventory_is_bound_to_target_render_directory(self):
        for deployment in ('mongodb-restore-logical','mongodb-restore-physical','mongodb-ownership-gate'):
            inventory=lab.inventory(deployment)
            self.assertIn('cluster_dir="{{ base_dir }}/clusters/'+deployment+'"',inventory)
            self.assertNotIn('clusters/mongodb-gate"',inventory)
            self.assertEqual(inventory.count('cluster_dir='),1)
        with self.assertRaises(ValueError):lab.inventory('foreign')

    def test_restore_target_uses_independent_ca_san_and_its_own_secret_references(self):
        source_ca = lab.credentials()['items'][0]['stringData']['ca.crt']
        for kind in ('logical', 'physical'):
            target = 'mongodb-gate-' + kind + '-restore'
            objects = lab.credentials(target)['items']
            self.assertEqual({o['metadata']['name'] for o in objects},
                             {target + '-' + s for s in ('tls', 'tls-internal', 'users', 'app', 'key')})
            cert = x509.load_pem_x509_certificate(objects[0]['stringData']['tls.crt'].encode())
            names = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value.get_values_for_type(x509.DNSName)
            self.assertIn('*.' + target + '-rs0.mongodb-gate.svc.cluster.local', names)
            self.assertFalse(any(lab.NAME + '-rs0' in n for n in names))
            self.assertNotEqual(objects[0]['stringData']['ca.crt'], source_ca)
            client = lab.client_object(target)
            self.assertNotIn(lab.NAME, json.dumps(client))
            self.assertIn(target + '-tls', json.dumps(client))
            self.assertNotIn('tlsInsecure', json.dumps(client))
        for invalid in ('foreign', 'redis-gate', '../mongodb-gate-data'):
            with self.assertRaises(ValueError): lab.credentials(invalid)
            with self.assertRaises(ValueError): lab.client_object(invalid)

    def test_first_failure_captures_only_backup_fixture_logs_and_redacts_known_values(self):
        import base64
        with tempfile.TemporaryDirectory(prefix='kubeauto-mongodb-install.') as tmp:
            instance = lab.Lab(Path(tmp))
            def get(kind, **kwargs):
                if kind == 'secrets':
                    return {'items': [{'data': {'password': base64.b64encode(b'fixture-private-value').decode()}}]}
                if kind == 'pods':
                    return {'items': [{'metadata': {'name': 'owned-storage', 'labels': {
                        'kubeauto.io/component': 'mongodb-backup-fixture'}}},
                        {'metadata': {'name': 'foreign', 'labels': {}}}]}
                raise AssertionError(kind)
            with patch.object(instance, 'get', side_effect=get), patch.object(lab.subprocess, 'run', return_value=
                    SimpleNamespace(returncode=0, stdout='fixture-private-value', stderr='')):
                instance.diagnostics(Path(tmp))
            self.assertEqual(Path(tmp, 'fixture-owned-storage.txt').read_text(), '[REDACTED]')
            self.assertFalse(Path(tmp, 'fixture-foreign.txt').exists())

    def test_refusal_namespace_receipt_qualifies_only_the_exact_independent_owner(self):
        nodes,namespaces,owner=baseline();name='mongodb-owner-0123456789abcdef'
        labels={'app.kubernetes.io/managed-by':'independent-resource-owner',
            'kubeauto.io/test-run':'0123456789abcdef','kubernetes.io/metadata.name':name}
        fixture={'metadata':{'name':name,'uid':'independent-uid','labels':labels}}
        for drift in (None,'uid','label','other-namespace','unready','no-receipt','name','empty-uid'):
            with self.subTest(drift=drift),tempfile.TemporaryDirectory() as tmp:
                instance=lab.Lab(Path(tmp));instance.directory.mkdir(parents=True)
                instance.owner_path.write_text(json.dumps(owner))
                ns=copy.deepcopy(namespaces+[fixture]);n=copy.deepcopy(nodes)
                receipt=(name,'independent-uid',labels)
                if drift=='uid':ns[-1]['metadata']['uid']='foreign'
                if drift=='label':ns[-1]['metadata']['labels']={}
                if drift=='other-namespace':ns.append({'metadata':{'name':'foreign'}})
                if drift=='unready':n[0]['status']['conditions'][0]['status']='False'
                if drift=='no-receipt':receipt=None
                if drift=='name':receipt=('foreign','independent-uid',labels)
                if drift=='empty-uid':receipt=(name,None,labels)
                with patch.object(instance,'get',side_effect=lambda k:{'items':n if k=='nodes' else ns}):
                    if drift:
                        with self.assertRaises(ValueError):instance.verify(independent_namespace=receipt)
                    else:
                        self.assertEqual(instance.verify(independent_namespace=receipt)['cluster_uid'],'owned-cluster')
                        # This receipt never grants a mutating cleanup exception.
                        with self.assertRaisesRegex(ValueError,'read-only qualification'):
                            instance.cleanup_product(independent_namespace=receipt)

    def test_unready_core_recovery_retains_strict_identity_and_refuses_existing_workloads(self):
        nodes, namespaces, owner = baseline()
        for node in nodes:
            node['status']['conditions'][0]['status'] = 'False'
        with self.assertRaisesRegex(ValueError, 'not Ready'):
            lab.core_identity(nodes, namespaces, owner)
        self.assertEqual(lab.core_identity(nodes, namespaces, owner, allow_unready=True), 'owned-cluster')
        with tempfile.TemporaryDirectory() as tmp:
            instance = lab.Lab(Path(tmp))
            instance.directory.mkdir(parents=True)
            instance.directory.joinpath('hosts').write_text(lab.inventory())
            owner.update(healthy=False, clean_start_verified=True)
            instance.owner_path.write_text(json.dumps(owner))
            def get(kind):
                return {'items': nodes if kind == 'nodes' else namespaces}
            with patch.object(instance, 'get', side_effect=get):
                self.assertEqual(instance.verify(allow_unready=True)['cluster_uid'], 'owned-cluster')
                for defect in ('healthy', 'unclean', 'inventory', 'workload'):
                    changed = copy.deepcopy(owner)
                    if defect == 'healthy':
                        changed['healthy'] = True
                    elif defect == 'unclean':
                        changed['clean_start_verified'] = False
                    elif defect == 'inventory':
                        instance.directory.joinpath('hosts').write_text('foreign inventory')
                    else:
                        namespaces.append({'metadata': {'name': lab.CLUSTER, 'labels': lab.LABELS}})
                    instance.owner_path.write_text(json.dumps(changed))
                    with self.subTest(defect=defect), self.assertRaises(ValueError):
                        instance.verify(allow_unready=True)
                    instance.directory.joinpath('hosts').write_text(lab.inventory())

    def test_preflight_rejects_pressure_unschedulable_worker_and_duplicate_domains(self):
        nodes = baseline()[0]
        for node in nodes:
            node['spec'] = {}
            node['metadata']['labels'] = {'kubernetes.io/hostname': node['metadata']['name']}
            node['status']['conditions'] += [{'type': key, 'status': 'False'} for key in
                                             ('MemoryPressure', 'DiskPressure', 'PIDPressure')]
        self.assertEqual(len(lab.worker_domains(nodes)), 3)
        for defect in ('pressure', 'unknown-pressure', 'unschedulable', 'taint', 'domain'):
            changed = copy.deepcopy(nodes)
            worker = changed[3]
            if defect == 'pressure':
                worker['status']['conditions'][1]['status'] = 'True'
            elif defect == 'unknown-pressure':
                worker['status']['conditions'].pop()
            elif defect == 'unschedulable':
                worker['spec']['unschedulable'] = True
            elif defect == 'taint':
                worker['spec']['taints'] = [{'key': 'unhealthy', 'effect': 'NoExecute'}]
            else:
                worker['metadata']['labels'] = changed[4]['metadata']['labels']
            with self.subTest(defect=defect), self.assertRaises(ValueError):
                lab.worker_domains(changed)

    def test_diagnostic_redaction_removes_known_values_and_uri_credentials(self):
        output = lab.redact('mongodb://operator:private@database password=unknown token="private"\nkey=private', ['private'])
        self.assertNotIn('private', output)
        self.assertNotIn('unknown', output)
        self.assertNotIn('operator:', output)

    def test_upgrade_install_selects_old_pins_only_in_an_explicit_product_fixture(self):
        from tests.unit.test_mongodb_delivery import configuration
        for core_only in (False,True):
            with self.subTest(core_only=core_only),tempfile.TemporaryDirectory() as tmp:
                directory=Path(tmp,'clusters',lab.CLUSTER);directory.mkdir(parents=True)
                path=directory/'config.yml';original=configuration();path.write_text(yaml.safe_dump(original))
                if core_only:
                    with self.assertRaises(ValueError):lab.configure(Path(tmp),core_only=True,upgrade=True)
                    self.assertEqual(yaml.safe_load(path.read_text()),original)
                else:
                    lab.configure(Path(tmp),upgrade=True);actual=yaml.safe_load(path.read_text())
                    self.assertEqual(actual['mongodb_operator_ver'],'1.23.0')
                    self.assertEqual(actual['mongodb_operator_chart_ver'],'1.23.1')
                    self.assertEqual(actual['mongodb_ver'],'8.0.26-11')
                    self.assertEqual(actual['mongodb_fluentbit_ver'],'5.0.9-1')
                    self.assertEqual(actual['mongodb_install'],'yes');self.assertEqual(actual['mongodb_name'],lab.NAME)

    def test_compute_inventory_matches_storage_fixture_and_excludes_other_clusters(self):
        from tests.unit.test_mongodb_storage import module as storage
        self.assertEqual(set(lab.NODES), storage.NODES)
        self.assertEqual(set(lab.NODES[3:]), storage.WORKERS)
        for ip in lab.NODES:
            self.assertIn(ip, lab.inventory())
        self.assertNotIn('192.168.122.1 ', lab.inventory())
        self.assertNotIn('ceph_devices', lab.inventory())
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp) / 'clusters' / lab.CLUSTER
            directory.mkdir(parents=True)
            from tests.unit.test_mongodb_delivery import configuration
            config = configuration()
            for key in ('mongodb_network_policy_enabled', 'mongodb_client_selector', 'mongodb_client_namespaces'):
                config.pop(key, None)
            directory.joinpath('config.yml').write_text(yaml.safe_dump(config))
            lab.configure(Path(tmp))
            actual = yaml.safe_load(directory.joinpath('config.yml').read_text())
            self.assertFalse(actual['ENABLE_SETTING_HOSTNAME'])
            self.assertEqual(actual['mongodb_name'], lab.NAME)
            self.assertEqual(actual['K8S_VER'], '1.33.6')
            self.assertEqual(actual['SYS_RESERVED_ENABLED'], 'no')
            self.assertIs(actual['mongodb_network_policy_enabled'], True)
            self.assertEqual(actual['mongodb_client_selector'], {'kubeauto.io/mongodb-client': 'true'})
            self.assertEqual(directory.joinpath('hosts').read_text(), lab.inventory())

    def test_tls_fixtures_verify_chain_and_cover_replica_shard_config_and_router_dns(self):
        objects = {o['metadata']['name']: o for o in lab.credentials()['items']}
        external = objects[lab.NAME + '-tls']['stringData']
        internal = objects[lab.NAME + '-tls-internal']['stringData']
        ca = x509.load_pem_x509_certificate(external['ca.crt'].encode())
        self.assertEqual(internal['ca.crt'], external['ca.crt'])
        self.assertNotEqual(internal['tls.key'], external['tls.key'])
        for values in (internal, external):
            leaf = x509.load_pem_x509_certificate(values['tls.crt'].encode())
            ca.public_key().verify(leaf.signature, leaf.tbs_certificate_bytes, padding.PKCS1v15(), leaf.signature_hash_algorithm)
            dns = leaf.extensions.get_extension_for_class(x509.SubjectAlternativeName).value.get_values_for_type(x509.DNSName)
            for rs in ('rs0', 'rs1', 'cfg'):
                self.assertIn('*.' + lab.NAME + '-' + rs + '.' + lab.CLUSTER + '.svc.cluster.local', dns)
            self.assertIn(lab.NAME + '-mongos.' + lab.CLUSTER + '.svc.cluster.local', dns)
        users = objects[lab.NAME + '-users']['stringData']
        self.assertEqual(len(set(users.values())), 10)
        client = lab.client_object()['spec']['volumes'][0]['secret']
        self.assertEqual(client['items'], [{'key': 'ca.crt', 'path': 'ca.crt'}])
        self.assertNotIn('tls.key', json.dumps(lab.client_object()))

    def test_core_reuse_rejects_foreign_namespace_uid_node_version_and_missing_node(self):
        nodes, namespaces, owner = baseline()
        self.assertEqual(lab.core_identity(nodes, namespaces, owner), 'owned-cluster')
        for changed in ('namespace', 'uid', 'node', 'version', 'missing'):
            n, ns, o = copy.deepcopy((nodes, namespaces, owner))
            if changed == 'namespace':
                ns.append({'metadata': {'name': 'foreign-workload'}})
            elif changed == 'uid':
                o['cluster_uid'] = 'foreign-cluster'
            elif changed == 'node':
                n[0]['status']['addresses'][0]['address'] = '192.168.122.217'
            elif changed == 'version':
                n[0]['status']['nodeInfo']['kubeletVersion'] = 'v1.34.2'
            else:
                n.pop()
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                lab.core_identity(n, ns, o)

    def test_cache_refuses_mismatched_existing_tag_without_overwriting_it(self):
        with patch.object(lab, 'publication_records', return_value=[{
                'key': 'brinnatt/percona-server-mongodb:8.0.32-14',
                'talkedu_ref': 'hub.talkedu.cn/kubeauto/percona-server-mongodb:8.0.32-14',
                'platforms': {'linux/amd64': 'sha256:' + '0' * 64}}]), \
                patch.object(lab.subprocess, 'run', return_value=SimpleNamespace(returncode=0, stdout=b'{}')) as run:
            with self.assertRaisesRegex(ValueError, 'cache digest differs'):
                lab.cache_artifacts(ROOT)
        self.assertEqual(run.call_count, 2)
        self.assertFalse(any('copy' in c.args[0] for c in run.call_args_list))

    def test_media_type_translation_requires_exact_config_and_layer_content(self):
        original = {'schemaVersion': 2, 'mediaType': 'application/vnd.oci.image.manifest.v1+json',
                    'config': {'digest': 'sha256:config', 'size': 120},
                    'layers': [{'digest': 'sha256:layer', 'size': 400}]}
        translated = copy.deepcopy(original)
        translated['mediaType'] = 'application/vnd.docker.distribution.manifest.v2+json'
        self.assertTrue(lab.equivalent_content(translated, original))
        for part in ('config', 'layers'):
            bad = copy.deepcopy(translated)
            (bad['config'] if part == 'config' else bad['layers'][0])['digest'] = 'sha256:different'
            self.assertFalse(lab.equivalent_content(bad, original))

    def test_publication_rejects_wrong_image_even_when_count_and_platforms_match(self):
        for support, filename in [(False, 'mongodb-images-20261015.json'), (True, 'mongodb-support-images-20261015.json')]:
            document = json.loads(ROOT.joinpath('tests/evidence', filename).read_text())
            document['images'][0]['key'] = 'brinnatt/foreign-image:1.0'
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / 'tests/evidence'
                path.mkdir(parents=True)
                path.joinpath(filename).write_text(json.dumps(document))
                with self.assertRaisesRegex(ValueError, 'coverage is incomplete'):
                    lab.publication_records(Path(tmp), support=support)

    def test_backup_observability_requires_published_kubernetes_state_collector(self):
        filename = 'mongodb-support-images-20261015.json'
        document = json.loads(ROOT.joinpath('tests/evidence', filename).read_text())
        self.assertIn('brinnatt/kube-state-metrics:v2.18.0',
                      {row['key'] for row in lab.publication_records(ROOT, support=True)})
        document['images'] = [row for row in document['images']
                              if row['key'] != 'brinnatt/kube-state-metrics:v2.18.0']
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'tests/evidence'
            path.mkdir(parents=True)
            path.joinpath(filename).write_text(json.dumps(document))
            with self.assertRaisesRegex(ValueError, 'coverage is incomplete'):
                lab.publication_records(Path(tmp), support=True)

    def test_cleanup_foreign_pvc_stops_before_any_delete(self):
        instance = lab.Lab(ROOT)
        def get(kind, *args, **kwargs):
            if kind == 'namespace':
                return {'metadata': {'labels': lab.LABELS}}
            if kind == 'pvc':
                return {'items': [{'metadata': {'labels': {'app.kubernetes.io/instance': 'foreign'}},
                                   'spec': {'storageClassName': 'mongodb-longhorn'}}]}
            raise AssertionError(kind)
        with patch.object(instance, 'verify', return_value={}), patch.object(instance, 'get', side_effect=get), \
                patch.object(instance, 'run') as run:
            with self.assertRaisesRegex(ValueError, 'foreign PVC'):
                instance.cleanup_product()
        run.assert_not_called()

    def test_cleanup_rebound_pv_stops_before_any_delete(self):
        instance = lab.Lab(ROOT)
        claim = {'metadata': {'name': 'mongod-data-' + lab.NAME + '-rs0-0', 'uid': 'claim-uid',
            'labels': {'app.kubernetes.io/instance': lab.NAME}},
            'spec': {'storageClassName': 'mongodb-longhorn', 'volumeName': 'owned-volume'}}
        def get(kind, name='', **kwargs):
            if kind == 'namespace':return {'metadata': {'uid': 'namespace-uid', 'labels': lab.LABELS}}
            if kind == 'pvc':return {'items': [claim]}
            if kind == 'pv':return {'items': []} if not name else {'metadata': {'uid': 'pv-uid'},
                'spec': {'storageClassName': 'mongodb-longhorn',
                         'claimRef': {'namespace': lab.CLUSTER, 'name': claim['metadata']['name'], 'uid': 'replaced-claim'}}}
            if kind == 'crd':return None
            raise AssertionError(kind)
        with tempfile.TemporaryDirectory() as tmp:
            instance.directory = Path(tmp);instance.owner_path = Path(tmp, 'owner.json')
            with patch.object(instance, 'verify', return_value={}), patch.object(instance, 'get', side_effect=get), \
                    patch.object(instance, 'run') as run:
                with self.assertRaisesRegex(ValueError, 'volume'):
                    instance.cleanup_product()
                run.assert_not_called()

    def test_cleanup_foreign_crd_consumer_in_other_namespace_stops_before_any_delete(self):
        instance = lab.Lab(ROOT)
        crd = 'perconaservermongodbs.psmdb.percona.com'
        owner = {'created_crds': [crd]}
        def get(kind, name='', **kwargs):
            if kind == 'namespace':return None
            if kind == 'pv':return {'items': []}
            if kind == 'crd':return {'metadata': {'name': crd}}
            if kind == crd:
                return {'items': [{'metadata': {'name': 'customer', 'namespace': 'foreign'}}]
                        if kwargs.get('all_namespaces') else []}
            raise AssertionError(kind)
        with tempfile.TemporaryDirectory() as tmp:
            instance.directory = Path(tmp);instance.owner_path = Path(tmp, 'owner.json')
            with patch.object(instance, 'verify', return_value=owner), patch.object(instance, 'get', side_effect=get), \
                    patch.object(instance, 'run') as run:
                with self.assertRaisesRegex(ValueError, 'surviving consumers'):
                    instance.cleanup_product()
                run.assert_not_called()

    def test_cleanup_replaced_crd_or_changed_schema_stops_before_namespace_or_any_delete(self):
        import hashlib
        crd='perconaservermongodbs.psmdb.percona.com'
        expected={'uid':'original','versions_sha256':hashlib.sha256(json.dumps(['v1'],sort_keys=True).encode()).hexdigest()}
        for changed_uid in (True,False):
            with self.subTest(changed_uid=changed_uid),tempfile.TemporaryDirectory() as tmp:
                instance=lab.Lab(ROOT);instance.directory=Path(tmp)
                owner={'created_crds':[crd],'product_identity':{'crds':{crd:expected}}}
                def get(kind,*args,**kwargs):
                    if kind=='crd':return {'metadata':{'uid':'replaced' if changed_uid else 'original'},
                                          'spec':{'versions':['v1'] if changed_uid else ['changed-v1']}}
                    if kind==crd:return {'items':[]}
                    raise AssertionError(kind)
                with patch.object(instance,'verify',return_value=owner),patch.object(instance,'get',side_effect=get), \
                        patch.object(instance,'delete_uid') as delete,patch.object(instance,'run') as run:
                    with self.assertRaisesRegex(ValueError,'CRD identity or schema changed'):instance.cleanup_product()
                    delete.assert_not_called();run.assert_not_called()

    def test_lab_api_all_namespace_scope_is_explicit_and_excludes_named_namespace(self):
        instance = lab.Lab(ROOT)
        with patch.object(instance, 'run', return_value='{"items":[]}') as run:
            self.assertEqual(instance.get('psmdb', all_namespaces=True), {'items': []})
            self.assertEqual(run.call_args.args, ('get', 'psmdb', '--all-namespaces', '-o', 'json'))
            with self.assertRaises(ValueError):instance.get('psmdb', ns=lab.CLUSTER, all_namespaces=True)
            run.assert_called_once()

    def test_incomplete_install_reset_checks_every_resource_before_uid_deletion(self):
        instance = lab.Lab(ROOT)
        namespace = {'metadata': {'uid': 'namespace-uid', 'labels': lab.LABELS}}
        secret = {'kind': 'Secret', 'metadata': {'name': lab.NAME + '-users', 'labels': lab.LABELS}}
        objects = [secret]
        def get(kind, name='', **kwargs):
            if kind == 'namespace': return namespace
            if kind == 'crd': return None
            return {'items': objects}
        with patch.object(instance, 'verify', return_value={'healthy': False}), \
                patch.object(instance, 'get', side_effect=get), patch.object(lab.subprocess, 'run') as submit, \
                patch.object(instance, 'run') as run:
            objects.append({'kind': 'Pod', 'metadata': {'name': 'foreign'}})
            with self.assertRaisesRegex(ValueError, 'foreign or active'):
                instance.reset_incomplete_install()
            submit.assert_not_called();run.assert_not_called()
            objects.pop()
            submit.return_value = SimpleNamespace(returncode=0)
            with self.assertRaisesRegex(ValueError, 'not been removed'):
                instance.reset_incomplete_install()
            self.assertEqual(json.loads(submit.call_args.kwargs['input'])['preconditions'], {'uid': 'namespace-uid'})
            self.assertIn('/api/v1/namespaces/mongodb-gate', submit.call_args.args[0])

    def test_incomplete_install_reset_refuses_a_healthy_baseline(self):
        instance = lab.Lab(ROOT)
        with patch.object(instance, 'verify', return_value={'healthy': True}), patch.object(instance, 'get') as get:
            with self.assertRaisesRegex(ValueError, 'healthy or faulted'):
                instance.reset_incomplete_install()
            get.assert_not_called()

    def test_install_client_cleanup_uses_uid_and_refuses_a_scenario_or_foreign_pod(self):
        instance = lab.Lab(ROOT)
        original = lab.client_object();original['metadata']['uid'] = 'install-client'
        for altered in ('scenario', 'image', None):
            pod = copy.deepcopy(original)
            if altered == 'scenario': pod['metadata']['labels']['kubeauto.io/test-run'] = 'other-scenario'
            elif altered == 'image': pod['spec']['containers'][0]['image'] = 'foreign-image'
            with patch.object(instance, 'verify', return_value={'healthy': False}), \
                    patch.object(instance, 'get', side_effect=[pod, None]), patch.object(instance, 'run') as run, \
                    patch.object(lab.subprocess, 'run', return_value=SimpleNamespace(returncode=0)) as submit:
                if altered:
                    with self.assertRaisesRegex(ValueError, 'changed fixture identity'): instance.cleanup_install_client()
                    submit.assert_not_called();run.assert_not_called()
                else:
                    instance.cleanup_install_client()
                    self.assertEqual(json.loads(submit.call_args.kwargs['input'])['preconditions'], {'uid': 'install-client'})

    def test_install_business_recovery_runs_product_then_own_business_assertions(self):
        script = ROOT.joinpath('tests/helpers/mongodb-install-gate.sh').read_text()
        branch = script.split('if [[ "$MODE" == --install-business ]]; then', 1)[1].split('\nelse\n', 1)[0]
        self.assertLess(branch.index('mongodb.py" health'), branch.index('cleanup-install-client'))
        self.assertIn('kubecli setup "$CLUSTER" 07 --tags mongodb', branch)
        self.assertIn('mongosh --quiet --norc --nodb --file /dev/stdin', script)
        self.assertIn('finally {assert.commandWorked(d.dropDatabase());}', script)

    def test_launcher_has_lease_before_sync_and_no_cancel_of_active_job(self):
        runner = ROOT.joinpath('tests/helpers/mongodb-runner.sh').read_text()
        self.assertLess(runner.index('flock -n 9'), runner.index('sync-kubeauto.sh'))
        self.assertNotIn('cancel_remote_job', runner)
        installer = ROOT.joinpath('tests/helpers/mongodb-install-gate.sh').read_text()
        self.assertIn('kubecli new "$CLUSTER"', installer)
        self.assertIn('kubecli setup "$CLUSTER" 07 --tags mongodb', installer)
        self.assertNotIn('192.168.122.217', installer)
        self.assertIn('trap \'exit 143\' TERM', installer)
        self.assertIn('mongosh --quiet --norc --nodb --file /dev/stdin', installer)

    def test_heartbeat_reports_own_matrix_for_mode_and_monitor_label(self):
        runner = ROOT.joinpath('tests/run_enterprise_regression.sh').read_text()
        function = runner.split('matrix_counts() {', 1)[1].split('\n}\n', 1)[0]
        with tempfile.TemporaryDirectory() as tmp:
            # Execute the actual function against isolated matrix fixtures.
            matrices = Path(tmp) / 'tests'
            matrices.mkdir()
            matrices.joinpath('mongodb-test-matrix.yaml').write_text('cases:\n- id: MONGODB-01\n  status: pending\n')
            matrices.joinpath('enterprise-test-matrix.yaml').write_text('- {id: CORE-01, status: pass}\n')
            script = Path(tmp) / 'counts.sh'
            script.write_text('ROOT=' + tmp + '\nMODE=--mongodb-install\n'
                              'matrix_counts() {' + function + '\n}\nmatrix_counts "$1"\n')
            for surface in ('--mongodb-install', 'mongodb'):
                result = lab.subprocess.run(['bash', str(script), surface], capture_output=True, text=True, check=True)
                self.assertEqual(result.stdout, 'matrix_pass=0 matrix_pending=1 matrix_fail=0')

    def test_durable_summary_counts_mongodb_failures_and_passes_without_counting_retries(self):
        runner = ROOT.joinpath('tests/run_enterprise_regression.sh').read_text()
        function = runner.split('remote_job_summary() {', 1)[1].split('\n}\n', 1)[0]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for suffix, value in [('pid', '123'), ('exit', '1'), ('finalized', '1')]:
                root.joinpath('gate.' + suffix).write_text(value)
            root.joinpath('gate.log').write_text('FAILED - RETRYING: network\nMONGODB_STATIC_PASS\n'
                                                 'MONGODB_FIRST_FAILURE phase=core\nMONGODB_LAB_FAIL reason=unready\nMONGODB_GATE_EXIT rc=1\n')
            script = root / 'summary.sh'
            script.write_text('ssh_fixture() { bash -c "$1"; }\nremote_job_summary() {' + function +
                              '\n}\nremote_job_summary ssh_fixture "" "$1/gate" "$1/gate.log"\n')
            result = lab.subprocess.run(['bash', str(script), tmp], capture_output=True, text=True, check=True)
            self.assertIn('state=exited', result.stdout)
            self.assertIn('pass_markers=1 failure_markers=2', result.stdout)
            self.assertIn('latest=MONGODB_GATE_EXIT rc=1', result.stdout)
