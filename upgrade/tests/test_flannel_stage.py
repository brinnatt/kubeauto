"""Exercise actual Flannel kubectl subprocesses using product-rendered sources."""
from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import jinja2
import yaml

from upgrade.product.flannel_stage import (CM, DS, FlannelStage, FlannelRefused,
                                           FlannelCommandFailed, target_objects)


ROOT = Path(__file__).resolve().parents[2]
TEMPLATE = ROOT/'roles/flannel/templates/kube-flannel.yaml.j2'
FIXTURE = r'''import json,sys
from pathlib import Path
root=Path(__file__).parent
state=json.loads((root/'state.json').read_text())
args=sys.argv[1:]
assert args[:4]==['--kubeconfig',str(root/'kubeconfig'),'--namespace','kube-system']
assert args[4]=='--server' and args[6:8]==['--request-timeout=10s','--insecure-skip-tls-verify=false']
server=args[5]; args=args[8:]
with (root/'calls.jsonl').open('a') as stream:stream.write(json.dumps(args)+'\n')
if args==['get','--raw=/version']:out={'gitVersion':state.get('apis',{}).get(server,'v1.33.6')}
elif args==['get','--raw=/readyz']:print('ok');raise SystemExit(0)
elif args==['get','namespace','kube-system','-o','json']:
 out={'metadata':{'uid':state.get('cluster_uid','cluster-uid')},'status':{'phase':'Active'}}
elif args==['get','nodes','-o','json']:out={'items':state['nodes']}
elif args[0]=='get':out=state['objects'][args[1]]
elif args[0]=='patch':
 ref=args[1]; obj=state['objects'][ref]; ops=json.loads(args[4])
 assert args[2:4]==['--type=json','-p'] and len(ops)==4
 assert [x['op'] for x in ops]==['test','test','test','replace']
 for op in ops[:3]:
  value=obj
  for key in op['path'].strip('/').split('/'):value=value[key]
  assert value==op['value']
 assert ops[3]['path']==ops[2]['path'] and ops[3]['path'] in ['/spec','/data']
 if state.get('conflict')==ref:raise SystemExit(42)
 obj[ops[3]['path'][1:]]=ops[3]['value']; obj['metadata']['resourceVersion']='2'
 if obj['kind']=='DaemonSet':
  obj['metadata']['generation']=2; obj['status']['observedGeneration']=2
  if state.get('stalled'):obj['status']['numberReady']=0
 if state.get('drift_after_cm') and ref.startswith('configmap'):
  state['objects']['clusterrole/flannel']['rules']=[]
 if state.get('advance_api_after_cm') and ref.startswith('configmap'):
  state['apis']={'https://master-two:6443':'v1.34.13'}
 out=obj
else:raise AssertionError(args)
(root/'state.json').write_text(json.dumps(state))
print(json.dumps(out))
'''


def objects(version='v0.28.4', backend='vxlan', direct=False):
    rendered = jinja2.Template(TEMPLATE.read_text(), undefined=jinja2.StrictUndefined,
                              trim_blocks=True).render(CLUSTER_CIDR='10.244.0.0/16',
        FLANNEL_BACKEND=backend, DIRECT_ROUTING=direct, flannel_ver=version,
        flannel_cni_ver='v1.8.0-flannel1' if version=='v0.28.4' else 'v1.9.1-flannel3')
    result = {}
    for obj in yaml.safe_load_all(rendered):
        ref = obj['kind'].lower()+'/'+obj['metadata']['name']
        obj['metadata'].update(uid=ref+'-uid', resourceVersion='1', generation=1)
        if obj['kind']=='DaemonSet':
            obj['status'] = dict(observedGeneration=1, desiredNumberScheduled=2,
                currentNumberScheduled=2, updatedNumberScheduled=2, numberReady=2,
                numberAvailable=2, numberMisscheduled=0)
        result[ref] = obj
    return result


class FlannelStageContracts(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        binary = self.root/'kubectl'; binary.write_text('#!'+sys.executable+'\n'+FIXTURE)
        binary.chmod(0o700); (self.root/'kubeconfig').write_text('offline fixture')
        self.reviewed = objects()
        self.source = {'source':'v1.33.6','target':'v1.34.13','mutation':'none',
            'cluster_identity':{'namespace':'kube-system','uid':'cluster-uid'},
            'api_backends':[{'server':s} for s in ['https://master-one:6443','https://master-two:6443']],
            'nodes':[{'name':'node1','uid':'uid1'},{'name':'node2','uid':'uid2'}]}
        self.state = {'objects':deepcopy(self.reviewed),'nodes':[
            {'metadata':{'name':n['name'],'uid':n['uid']},'status':{
             'nodeInfo':{'kubeletVersion':'v1.33.6'},'conditions':[{'type':'Ready','status':'True'}]}}
            for n in self.source['nodes']]}
        self.markers=[]; self.host_checks=0; self.traffic_checks=0
        self.stage=FlannelStage(binary,self.root/'kubeconfig',self.source,
                                timeout=30,progress=self.markers.append)
        self.save()

    def save(self):
        (self.root/'state.json').write_text(json.dumps(self.state))

    def mutations(self):
        p=self.root/'calls.jsonl'
        return [json.loads(line) for line in p.read_text().splitlines()
                if json.loads(line)[0]!='get'] if p.exists() else []

    def hosts(self):
        self.host_checks+=1

    def traffic(self):
        self.traffic_checks+=1

    def execute(self):
        return self.stage.run(self.reviewed,self.hosts,self.traffic)

    def test_target_delta_matches_actual_product_template_preserving_network_and_rbac(self):
        for backend,direct in [('vxlan',False),('vxlan',True),('host-gw',False)]:
            source=objects(backend=backend,direct=direct)
            actual=target_objects(source); desired=objects('v0.28.10',backend,direct)
            for probe in ['livenessProbe','readinessProbe']:
                desired[DS]['spec']['template']['spec']['containers'][0][probe].update(
                    timeoutSeconds=1,successThreshold=1,failureThreshold=3)
                desired[DS]['spec']['template']['spec']['containers'][0][probe]['httpGet']['scheme']='HTTP'
            desired[CM]['data']['cni-conf.json']=actual[CM]['data']['cni-conf.json']
            self.assertEqual(actual,desired)
            self.assertEqual(source,objects(backend=backend,direct=direct),'saved source was mutated')

    def test_real_patch_order_identity_preservation_and_required_traffic_handoff(self):
        result=self.execute()
        self.assertFalse(result['parent_acceptance'])
        self.assertEqual([c[1] for c in self.mutations()],[CM,DS])
        self.assertEqual(self.host_checks,3); self.assertEqual(self.traffic_checks,3)
        expected=target_objects(self.reviewed)
        for ref in [CM,DS]:expected[ref]['metadata']['resourceVersion']='2'
        expected[DS]['metadata']['generation']=2
        expected[DS]['status']['observedGeneration']=2
        self.assertEqual(json.loads((self.root/'state.json').read_text())['objects'],expected)

    def test_unreviewed_source_image_installer_or_probe_refuses_before_commands(self):
        for fault in ['image','probe','installer','plugin','cni-version']:
            reviewed=deepcopy(self.reviewed); pod=reviewed[DS]['spec']['template']['spec']
            if fault=='image':pod['containers'][0]['image']='unreviewed:latest'
            elif fault=='probe':pod['containers'][0]['readinessProbe']={'exec':{'command':['true']}}
            elif fault=='installer':pod['initContainers'][1]['command']=['sh']
            elif fault=='plugin':pod['initContainers'][0]['image']='wrong:tag'
            else:reviewed[CM]['data']['cni-conf.json']='{"cniVersion":"0.4.0","plugins":[]}'
            with self.subTest(fault=fault),self.assertRaises(FlannelRefused):
                self.stage.run(reviewed,self.hosts,self.traffic)
        self.assertEqual(self.mutations(),[])

    def test_drift_in_config_rbac_or_uid_refuses_without_patch(self):
        for ref,field,value in [(CM,'data',{}),('clusterrole/flannel','rules',[]),
                                (DS,'metadata',{'uid':'replacement'})]:
            self.state['objects']=deepcopy(self.reviewed);self.state['objects'][ref][field]=value;self.save()
            with self.subTest(ref=ref),self.assertRaises(FlannelRefused):self.execute()
        self.assertEqual(self.mutations(),[])

    def test_each_backend_cluster_and_complete_node_identity_barrier(self):
        for fault in ['api','cluster','uid','missing','ready','cordon']:
            state=deepcopy(self.state)
            if fault=='api':state['apis']={'https://master-two:6443':'v1.34.13'}
            elif fault=='cluster':state['cluster_uid']='foreign'
            elif fault=='uid':state['nodes'][1]['metadata']['uid']='replacement'
            elif fault=='missing':state['nodes'].pop()
            elif fault=='ready':state['nodes'][1]['status']['conditions'][0]['status']='False'
            else:state['nodes'][1]['spec']={'unschedulable':True}
            (self.root/'state.json').write_text(json.dumps(state))
            with self.subTest(fault=fault),self.assertRaises(FlannelRefused):self.execute()
        self.assertEqual(self.mutations(),[])

    def test_host_port_or_initial_traffic_failure_refuses_without_mutation(self):
        def refused():raise FlannelRefused('fixture host qualification or traffic failed')
        for hosts,traffic in [(refused,self.traffic),(self.hosts,refused)]:
            with self.assertRaises(FlannelRefused):self.stage.run(self.reviewed,hosts,traffic)
        self.assertEqual(self.mutations(),[])

    def test_partial_node_coverage_or_unhealthy_source_refuses_without_patch(self):
        for field,value in [('desiredNumberScheduled',1),('numberReady',1),
                            ('numberMisscheduled',1),('observedGeneration',0)]:
            self.state['objects']=deepcopy(self.reviewed)
            self.state['objects'][DS]['status'][field]=value;self.save()
            with self.subTest(field=field),self.assertRaises(FlannelRefused):self.execute()
        self.assertEqual(self.mutations(),[])

    def test_config_patch_conflict_retains_first_rc_and_never_changes_daemonset(self):
        self.state['conflict']=CM;self.save()
        with self.assertRaises(FlannelCommandFailed) as error:self.execute()
        self.assertEqual(error.exception.rc,42)
        self.assertEqual([c[1] for c in self.mutations()],[CM])
        self.assertEqual(json.loads((self.root/'state.json').read_text())['objects'],self.reviewed)

    def test_failure_after_config_stops_daemonset_and_resume_only_remaining_boundary(self):
        def traffic():
            self.traffic_checks+=1
            if self.traffic_checks==2:raise FlannelRefused('fixture traffic failed after ConfigMap')
        with self.assertRaises(FlannelRefused):self.stage.run(self.reviewed,self.hosts,traffic)
        self.assertEqual([c[1] for c in self.mutations()],[CM])
        self.execute()
        self.assertEqual([c[1] for c in self.mutations()],[CM,DS])
        self.execute()
        self.assertEqual([c[1] for c in self.mutations()],[CM,DS],'completed resume patched again')

    def test_config_side_effect_or_api_advancement_stops_dependent_daemonset(self):
        for fault in ['drift_after_cm','advance_api_after_cm']:
            self.state['objects']=deepcopy(self.reviewed);self.state.pop('drift_after_cm',None)
            self.state[fault]=True;self.save();(self.root/'calls.jsonl').unlink(missing_ok=True)
            with self.subTest(fault=fault),self.assertRaises(FlannelRefused):self.execute()
            self.assertEqual([c[1] for c in self.mutations()],[CM])

    def test_stalled_target_keeps_first_failure_without_rollback_or_replay(self):
        self.state['stalled']=True;self.save()
        with patch('upgrade.product.flannel_stage.time.monotonic',side_effect=[0,0,31]),\
             self.assertRaises(FlannelRefused):self.execute()
        self.assertEqual([c[1] for c in self.mutations()],[CM,DS])
        with patch('upgrade.product.flannel_stage.time.monotonic',side_effect=[0,31]),\
             self.assertRaises(FlannelRefused):self.execute()
        self.assertEqual([c[1] for c in self.mutations()],[CM,DS])


if __name__=='__main__':
    unittest.main()
