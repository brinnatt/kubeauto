"""Run the real DNS subprocess boundary with product-rendered private fixtures."""
from copy import deepcopy
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from jinja2 import Environment, StrictUndefined
import yaml

from upgrade.product.dns_stage import (CORE, CACHE, EDGES, DNSStage, DNSRefused,
                                       DNSCommandFailed, healthy)

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = r'''import json,sys
from pathlib import Path
root=Path(__file__).parent
state=json.loads((root/'state.json').read_text())
args=sys.argv[1:]
assert args[:4]==['--kubeconfig',str(root/'kubeconfig'),'--namespace','kube-system']
assert args[4]=='--server' and args[6:8]==['--request-timeout=10s','--insecure-skip-tls-verify=false']
with (root/'calls.jsonl').open('a') as stream:stream.write(json.dumps(args)+'\n')
args=args[8:];rc=0
if args==['get','--raw=/version']:out={'gitVersion':state.get('api','v1.34.13')}
elif args==['get','--raw=/readyz']:print('ok');raise SystemExit(0)
elif args==['get','nodes','-o','json']:out={'items':state['nodes']}
elif args[0]=='get':out=state['objects'][args[1]]
elif args[0]=='patch':
 assert args[2]=='--type=json' and args[3]=='-p'
 ref=args[1];obj=state['objects'][ref];ops=json.loads(args[4])
 assert len(ops)==4 and [x['op'] for x in ops]==['test','test','test','replace']
 assert ops[0]=={'op':'test','path':'/metadata/uid','value':obj['metadata']['uid']}
 assert ops[1]=={'op':'test','path':'/metadata/resourceVersion','value':obj['metadata']['resourceVersion']}
 assert ops[2]=={'op':'test','path':'/spec/template/spec/containers/0/image','value':obj['spec']['template']['spec']['containers'][0]['image']}
 assert ops[3]['path']=='/spec/template/spec/containers/0/image'
 if state.get('conflict'):raise SystemExit(42)
 obj['spec']['template']['spec']['containers'][0]['image']=ops[3]['value']
 obj['metadata']['resourceVersion']='2';obj['metadata']['generation']=2
 obj['status']['observedGeneration']=2
 if state.get('stalled')==ref:obj['status']['readyReplicas' if ref.startswith('deployment') else 'numberReady']=0
 if state.get('drift_after_patch'):state['objects']['configmap/coredns']['data']['Corefile']='foreign'
 out=obj
else:raise AssertionError(args)
(root/'state.json').write_text(json.dumps(state))
print(json.dumps(out));raise SystemExit(rc)
'''


def product_objects(mode, cache):
    env = Environment(undefined=StrictUndefined)
    variables = dict(CLUSTER_DNS_DOMAIN='cluster.local', CLUSTER_DNS_SVC_IP='10.68.0.2',
                     LOCAL_DNS_CACHE='169.254.20.10', corednsVer='1.12.4', dnsNodeCacheVer='1.26.4')
    names = ['coredns.yaml.j2'] + ([f'nodelocaldns-{mode}.yaml.j2'] if cache else [])
    result = {}
    kinds = {'Deployment': 'deployment', 'DaemonSet': 'daemonset', 'ConfigMap': 'configmap',
             'Service': 'service', 'ServiceAccount': 'serviceaccount', 'ClusterRole': 'clusterrole',
             'ClusterRoleBinding': 'clusterrolebinding'}
    for name in names:
        path = ROOT / 'roles/cluster-addon/templates/dns' / name
        for obj in yaml.safe_load_all(env.from_string(path.read_text()).render(**variables)):
            ref = kinds[obj['kind']] + '/' + obj['metadata']['name']
            obj['metadata'].update(uid=ref + '-uid', resourceVersion='1', generation=1)
            if obj['kind'] == 'Deployment':
                obj['status'] = dict(observedGeneration=1, replicas=1, updatedReplicas=1,
                                     readyReplicas=1, availableReplicas=1)
            elif obj['kind'] == 'DaemonSet':
                obj['status'] = dict(observedGeneration=1, desiredNumberScheduled=2,
                                     currentNumberScheduled=2, updatedNumberScheduled=2,
                                     numberReady=2, numberAvailable=2, numberMisscheduled=0)
            result[ref] = obj
    return result


class DNSStageContracts(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        binary = self.root / 'kubectl'
        binary.write_text('#!' + sys.executable + '\n' + FIXTURE)
        binary.chmod(0o700)
        (self.root/'kubeconfig').write_text('offline-only')
        self.markers, self.queries = [], []
        self.stage = DNSStage(binary, self.root/'kubeconfig', ['https://master:6443'],
                              {'node1': 'uid1', 'node2': 'uid2'}, timeout=30,
                              progress=self.markers.append)
        self.reset()

    def reset(self, mode='ipvs', cache=True):
        self.reviewed = product_objects(mode, cache)
        self.state = {'objects': deepcopy(self.reviewed), 'nodes': [
            {'metadata': {'name': n, 'uid': u}, 'status': {'nodeInfo': {'kubeletVersion': 'v1.34.13'},
             'conditions': [{'type': 'Ready', 'status': 'True'}]}}
            for n, u in self.stage.nodes.items()]}
        (self.root/'calls.jsonl').unlink(missing_ok=True)

    def save(self):
        (self.root/'state.json').write_text(json.dumps(self.state))

    def calls(self):
        p = self.root/'calls.jsonl'
        return [json.loads(x)[8:] for x in p.read_text().splitlines()] if p.exists() else []

    def mutations(self):
        return [x for x in self.calls() if x[0] != 'get']

    def query(self, ref):
        live = json.loads((self.root/'state.json').read_text())['objects']
        self.assertEqual(live[CORE]['spec']['template']['spec']['containers'][0]['image'], EDGES[CORE][2])
        if ref == CORE and CACHE in live and not self.state.get('resume'):
            self.assertEqual(live[CACHE]['spec']['template']['spec']['containers'][0]['image'], EDGES[CACHE][1])
        self.queries.append(ref)

    def run_stage(self, query=None):
        self.save()
        return self.stage.run(self.reviewed, query or self.query)

    def test_real_product_ipvs_iptables_and_core_only_preserve_every_object_except_images(self):
        for mode, cache in [('ipvs', True), ('iptables', True), ('ipvs', False)]:
            with self.subTest(mode=mode, cache=cache):
                self.reset(mode, cache)
                receipt = self.run_stage()
                refs = [CORE, CACHE] if cache else [CORE]
                self.assertEqual([x[1] for x in self.mutations()], refs)
                self.assertFalse(receipt['parent_acceptance'])
                live = json.loads((self.root/'state.json').read_text())['objects']
                for ref in refs:
                    self.assertEqual(live[ref]['metadata']['uid'], self.reviewed[ref]['metadata']['uid'])
                    live[ref]['spec']['template']['spec']['containers'][0]['image'] = EDGES[ref][1]
                    live[ref]['metadata'].update(resourceVersion='1', generation=1)
                    live[ref]['status'] = self.reviewed[ref]['status']
                self.assertEqual(live, self.reviewed)

    def test_source_api_node_uid_version_readiness_or_cordon_refuses_before_patch(self):
        for mode in ['api', 'node-uid', 'node-version', 'node-ready', 'cordon', 'duplicate']:
            with self.subTest(mode=mode):
                self.reset()
                node = self.state['nodes'][0]
                if mode == 'api': self.state['api'] = 'v1.33.6'
                elif mode == 'node-uid': node['metadata']['uid'] = 'foreign'
                elif mode == 'node-version': node['status']['nodeInfo']['kubeletVersion'] = 'v1.33.6'
                elif mode == 'node-ready': node['status']['conditions'][0]['status'] = 'False'
                elif mode == 'cordon': node['spec'] = {'unschedulable': True}
                else: self.state['nodes'][1] = deepcopy(node)
                with self.assertRaises(DNSRefused): self.run_stage()
                self.assertEqual(self.mutations(), [])

    def test_corefile_rbac_service_workload_or_uid_drift_refuses_before_patch(self):
        for ref in ['configmap/coredns', 'clusterrole/system:coredns', 'service/kube-dns', CORE, CACHE]:
            self.reset()
            self.state['objects'][ref]['metadata']['uid'] = 'foreign'
            with self.assertRaises(DNSRefused): self.run_stage()
            self.assertEqual(self.mutations(), [])
        self.reset()
        self.state['objects']['configmap/coredns']['data']['Corefile'] = 'unreviewed'
        with self.assertRaises(DNSRefused): self.run_stage()
        self.assertEqual(self.mutations(), [])

    def test_unhealthy_cache_source_blocks_first_core_patch(self):
        self.state['objects'][CACHE]['status']['numberReady'] = 0
        with self.assertRaises(DNSRefused): self.run_stage()
        self.assertEqual(self.mutations(), [])

    def test_patch_conflict_preserves_first_rc_and_does_not_retry_or_advance_cache(self):
        self.state['conflict'] = True
        with self.assertRaises(DNSCommandFailed) as error: self.run_stage()
        self.assertEqual(error.exception.rc, 42)
        self.assertEqual([x[1] for x in self.mutations()], [CORE])
        self.assertEqual(self.queries, [])

    def test_query_failure_stops_cache_without_automatic_rollback(self):
        def refused(ref):
            self.query(ref)
            raise DNSRefused('real query fixture refused')
        with self.assertRaisesRegex(DNSRefused, 'query fixture'): self.run_stage(refused)
        self.assertEqual([x[1] for x in self.mutations()], [CORE])

    def test_post_patch_configuration_drift_stops_cache(self):
        self.state['drift_after_patch'] = True
        with self.assertRaisesRegex(DNSRefused, 'configuration drift'): self.run_stage()
        self.assertEqual([x[1] for x in self.mutations()], [CORE])

    def test_target_resume_rechecks_queries_without_repatching(self):
        for ref in [CORE, CACHE]:
            self.state['objects'][ref]['spec']['template']['spec']['containers'][0]['image'] = EDGES[ref][2]
        self.state['resume'] = True
        self.run_stage()
        self.assertEqual(self.mutations(), [])
        self.assertEqual(self.queries, [CORE, CACHE])

    def test_cache_premature_advancement_refuses(self):
        self.state['objects'][CACHE]['spec']['template']['spec']['containers'][0]['image'] = EDGES[CACHE][2]
        with self.assertRaisesRegex(DNSRefused, 'before CoreDNS'): self.run_stage()
        self.assertEqual(self.mutations(), [])

    def test_stalled_rollout_is_bounded_and_stops_before_query_or_cache(self):
        self.state['stalled'] = CORE
        with patch('upgrade.product.dns_stage.time.monotonic', side_effect=[0, 31]):
            with self.assertRaisesRegex(DNSRefused, 'fixed timeout'): self.run_stage()
        self.assertEqual([x[1] for x in self.mutations()], [CORE])
        self.assertEqual(self.queries, [])

    def test_health_rejects_extra_old_replicas_and_stale_generation(self):
        obj = deepcopy(self.reviewed[CORE])
        obj['status']['replicas'] = 2
        self.assertFalse(healthy(obj))
        obj['status'] = deepcopy(self.reviewed[CORE]['status'])
        obj['metadata']['generation'] = 2
        self.assertFalse(healthy(obj))


if __name__ == '__main__':
    unittest.main()
