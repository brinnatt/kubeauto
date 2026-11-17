"""Real Ansible eviction admission: exact ownership, PDB and local data safety."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import yaml

from upgrade.tests.test_containerd_stage import recording_tasks
from upgrade.tests.test_control_plane import NO_CONNECTION

ROOT = Path(__file__).resolve().parents[2]
ACTION = '''import json
from pathlib import Path
from ansible.plugins.action import ActionBase
class ActionModule(ActionBase):
 def run(self,tmp=None,task_vars=None):
  assert self._task.args['module']=='command'
  values=self._task.args['values'];args=values['argv'];root=Path(task_vars['fixture_root'])
  state=json.loads((root/'state').read_text())
  with (root/'trace').open('a') as stream:stream.write(json.dumps(values)+'\\n')
  if 'pods' in args:out={'items':state['pods']}
  elif 'replicasets,deployments' in args:out={'items':state['controllers']}
  elif 'cordon' in args:out='cordoned'
  elif 'create' in args:
   body=json.loads(values['stdin']);pod=state['pods'][0]
   assert body=={'apiVersion':'policy/v1','kind':'Eviction','metadata':{'name':pod['metadata']['name'],'namespace':'kube-system'},'deleteOptions':{'preconditions':{'uid':pod['metadata']['uid']}}}
   assert '--raw=/api/v1/namespaces/kube-system/pods/metrics-one/eviction' in args
   if state.get('pdb_denied'):return {'failed':True,'rc':1,'stdout':'','stderr':'TooManyRequests: PDB denial'}
   state['deletion_pending']=True
   (root/'state').write_text(json.dumps(state))
   out='evicted'
  elif 'wait' in args:
   assert 'pod/metrics-one' in args and '--for=delete' in args and '--timeout=300s' in args
   assert state.get('deletion_pending')
   if state.get('wait_failed'):return {'failed':True,'rc':1,'stdout':'','stderr':'timed out waiting for deletion'}
   state['deletion_pending']=False
   (root/'state').write_text(json.dumps(state))
   out='pod/metrics-one condition met'
  elif 'drain' in args:
   assert 'worker-one' in args and '--ignore-daemonsets' in args
   assert '--delete-emptydir-data' not in args and '--disable-eviction' not in args
   if state.get('deletion_pending'):return {'failed':True,'rc':1,'stdout':'','stderr':'cannot delete Pods with local storage: metrics-one'}
   out='drained'
  else:raise AssertionError(args)
  return {'rc':0,'changed':False,'stdout':json.dumps(out) if isinstance(out,dict) else out}
'''


class MetricsEvictionContracts(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name)
        (self.root/'actions').mkdir();(self.root/'connections').mkdir()
        (self.root/'actions/runtime_record.py').write_text(ACTION)
        (self.root/'connections/upgrade_test_forbidden.py').write_text(NO_CONNECTION)
        tasks=recording_tasks(yaml.safe_load((ROOT/'upgrade/product/metrics-eviction.yml').read_text()))
        (self.root/'book.yml').write_text(yaml.safe_dump([{'hosts':'fixture','gather_facts':False,'tasks':tasks}]))
        node=yaml.safe_load((ROOT/'upgrade/product/node.yml').read_text())
        drain=next(t for play in node for t in play['tasks']
                   if t['name']=='Drain with eviction PDB and local-data protection')
        (self.root/'handoff.yml').write_text(yaml.safe_dump([{'hosts':'fixture','gather_facts':False,
            'tasks':[*tasks,*recording_tasks([drain])]}]))
        (self.root/'hosts').write_text('fixture ansible_connection=upgrade_test_forbidden\n')
        (self.root/'vars.json').write_text(json.dumps({'fixture_root':str(self.root),
            'base_dir':'/fixture','cluster_dir':'/fixture/cluster','UPGRADE_NODE_NAME':'worker-one',
            'UPGRADE_DRAIN_TIMEOUT_SECONDS':300,
            'UPGRADE_API_BACKENDS':['https://master-one:6443']}))
        self.pod={'metadata':{'name':'metrics-one','namespace':'kube-system','uid':'pod-uid',
            'ownerReferences':[{'kind':'ReplicaSet','name':'metrics-rs','uid':'rs-uid','controller':True}]},
            'spec':{'nodeName':'worker-one','containers':[{'name':'metrics-server',
                'image':'registry.talkschool.cn:5000/brinnatt/metrics-server:v0.8.0'}],
                'volumes':[{'name':'tmp-dir','emptyDir':{}},{'name':'kube-api-access-test','projected':{}}]}}
        self.state={'pods':[self.pod],'controllers':[
            {'kind':'Deployment','metadata':{'namespace':'kube-system','name':'metrics-server','uid':'deploy-uid'},'spec':{'replicas':1}},
            {'kind':'ReplicaSet','metadata':{'namespace':'kube-system','name':'metrics-rs','uid':'rs-uid',
                'ownerReferences':[{'kind':'Deployment','name':'metrics-server','uid':'deploy-uid','controller':True}]}}]}

    def run_book(self, *, handoff=False):
        (self.root/'state').write_text(json.dumps(self.state))
        result=subprocess.run([str(Path(sys.executable).parent/'ansible-playbook'),'-i',str(self.root/'hosts'),
            str(self.root/('handoff.yml' if handoff else 'book.yml')),'-e','@'+str(self.root/'vars.json')],capture_output=True,text=True,timeout=30,
            env={**os.environ,'ANSIBLE_ACTION_PLUGINS':str(self.root/'actions'),
                 'ANSIBLE_CONNECTION_PLUGINS':str(self.root/'connections'),'ANSIBLE_LOCAL_TEMP':str(self.root/'local')})
        trace=[json.loads(line) for line in (self.root/'trace').read_text().splitlines()]
        self.assertNotIn('TEST_REMOTE_CONNECTION_REFUSED',result.stdout+result.stderr)
        return result,trace

    def test_owned_cache_cordons_then_uses_uid_bound_policy_eviction(self):
        result,trace=self.run_book();self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertEqual(len(trace),5)
        self.assertIn('cordon',trace[2]['argv']);self.assertIn('create',trace[3]['argv'])
        self.assertIn('wait',trace[4]['argv'])
        self.assertFalse(any('delete' in item['argv'] for item in trace))

    def test_actual_drain_waits_for_asynchronous_metrics_deletion(self):
        result,trace=self.run_book(handoff=True)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        commands=[item['argv'] for item in trace]
        self.assertIn('wait',commands[-2]);self.assertIn('drain',commands[-1])
        self.assertFalse(json.loads((self.root/'state').read_text())['deletion_pending'])

    def test_deletion_timeout_stops_before_actual_drain(self):
        self.state['wait_failed']=True
        result,trace=self.run_book(handoff=True)
        self.assertNotEqual(result.returncode,0)
        self.assertIn('timed out waiting for deletion',result.stdout)
        self.assertFalse(any('drain' in item['argv'] for item in trace))

    def test_pdb_refusal_stops_and_never_changes_node_executables(self):
        self.state['pdb_denied']=True
        result,trace=self.run_book();self.assertNotEqual(result.returncode,0)
        self.assertIn('PDB denial',result.stdout)
        self.assertEqual(len(trace),4)

    def test_foreign_data_or_owner_refuses_before_cordon(self):
        for mutation in ['volume','owner','image']:
            with self.subTest(mutation=mutation):
                before=json.loads(json.dumps(self.state))
                if mutation=='volume':self.pod['spec']['volumes'].append({'name':'data','persistentVolumeClaim':{'claimName':'private'}})
                elif mutation=='owner':self.pod['metadata']['ownerReferences'][0]['uid']='foreign'
                else:self.pod['spec']['containers'][0]['image']='foreign/metrics-server:v0.8.0'
                (self.root/'trace').unlink(missing_ok=True)
                result,trace=self.run_book();self.assertNotEqual(result.returncode,0)
                self.assertFalse(any('cordon' in item['argv'] or 'create' in item['argv'] for item in trace))
                self.state=before;self.pod=self.state['pods'][0]

    def test_absent_metrics_on_node_has_no_mutation(self):
        self.state['pods']=[]
        result,trace=self.run_book();self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertEqual(len(trace),1)
