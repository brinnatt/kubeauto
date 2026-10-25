"""PXC cluster-webhook ownership, native retirement and empty-baseline contracts."""
import copy,importlib.util,json
from pathlib import Path
from types import SimpleNamespace
import tempfile,unittest
from unittest.mock import Mock,patch
ROOT=Path(__file__).resolve().parents[2]
BASE=ROOT/'tests/helpers'
PATH=BASE/'longhorn-v1-middleware.py'
spec=importlib.util.spec_from_file_location('global_webhook_candidate',PATH);G=importlib.util.module_from_spec(spec);spec.loader.exec_module(G)
def fixture():
 row={'run_id':'run','cluster_uid':'cluster','namespace_uid':'lh','operator_namespace':'owned-operator',
      'operator_namespace_uid':'op-ns','database_namespace':'owned-db','database_namespace_uid':'db-ns',
      'original_input':'original','allowed_inputs':['original'],'created_crds':{}}
 deployment={'kind':'Deployment','metadata':{'name':'pxc-operator','namespace':row['operator_namespace'],'uid':'deployment','annotations':{
   'meta.helm.sh/release-name':'pxc-operator','meta.helm.sh/release-namespace':row['operator_namespace']}}}
 secret={'kind':'Secret','metadata':{'name':'pxc-webhook-ssl','namespace':row['operator_namespace'],'uid':'cert'},'type':'Opaque',
   'data':{'ca.crt':'Y2E=','tls.crt':'Y2VydA==','tls.key':'a2V5'}}
 hook={'apiVersion':'admissionregistration.k8s.io/v1','kind':'ValidatingWebhookConfiguration','metadata':{
   'name':'percona-xtradbcluster-webhook','uid':'hook','resourceVersion':'123','ownerReferences':[
     {'apiVersion':'apps/v1','kind':'Deployment','name':'pxc-operator','uid':'deployment','controller':True}]},'webhooks':[
   {'name':'validationwebhook.pxc.percona.com','admissionReviewVersions':['v1'],'sideEffects':'None','failurePolicy':'Fail',
    'clientConfig':{'service':{'name':'percona-xtradb-cluster-operator','namespace':'owned-operator',
      'path':'/validate-percona-xtradbcluster','port':443},'caBundle':secret['data']['ca.crt']},
    'rules':[{'apiGroups':['pxc.percona.com'],'apiVersions':['*'],'resources':['perconaxtradbclusters/*'],
      'operations':['CREATE','UPDATE'],'scope':'*'}]}]}
 state={'hook':hook,'deployment':deployment,'secret':secret};trace=[]
 def get(kind,*args,**kw):
  if kind=='validatingwebhookconfiguration':return state['hook']
  if kind=='deployment':return state['deployment']
  if kind=='secret':return state['secret']
  raise AssertionError((kind,args,kw))
 lab=SimpleNamespace(get=get,write_private=lambda p,r:trace.append(('persist',copy.deepcopy(r))))
 return row,state,trace,lab
class WebhookContracts(unittest.TestCase):
 def test_typed_capture_exact_native_shape_and_uid(self):
  row,state,trace,lab=fixture();lab.DIRECTORY=BASE
  self.assertIs(state['hook'],G.cluster_webhook(lab,row))
  self.assertEqual('hook',row['cluster_webhook']['uid']);self.assertEqual('deployment',row['cluster_webhook']['operator_uid'])
  self.assertIs(state['hook'],G.cluster_webhook(lab,row))
 def test_bad_namespace_ca_owner_uid_and_rules_never_authorize_delete(self):
  for defect in ('endpoint','ca','owner','url','rules','replace','secret'):
   row,state,trace,lab=fixture();lab.DIRECTORY=BASE;G.cluster_webhook(lab,row)
   if defect=='endpoint':state['hook']['webhooks'][0]['clientConfig']['service']['namespace']='old-namespace'
   if defect=='ca':state['hook']['webhooks'][0]['clientConfig']['caBundle']='foreign'
   if defect=='owner':state['hook']['metadata']['ownerReferences'][0]['uid']='foreign'
   if defect=='url':state['hook']['webhooks'][0]['clientConfig']['url']='https://foreign'
   if defect=='rules':state['hook']['webhooks'][0]['rules'][0]['resources']=['*']
   if defect=='replace':state['hook']['metadata']['uid']='foreign'
   if defect=='secret':state['secret']['metadata']['uid']='replacement'
   lab.remove=Mock()
   with self.subTest(defect=defect),self.assertRaises(ValueError):G.retire_webhook(lab,row)
   lab.remove.assert_not_called()
 def test_delete_intent_precedes_uid_bound_remove_and_resumes_without_controller(self):
  row,state,trace,lab=fixture();lab.DIRECTORY=BASE
  def remove(obj):
   self.assertTrue(row['webhook_retire_intent']);self.assertEqual('hook',row['cluster_webhook']['uid'])
   trace.append(('remove',obj['metadata']['uid']));state['hook']=None;raise KeyboardInterrupt()
  lab.remove=remove;lab.wait=lambda label,timeout,fn:self.assertTrue(fn())
  with self.assertRaises(KeyboardInterrupt):G.retire_webhook(lab,row)
  state['deployment']=None;state['secret']=None
  G.retire_webhook(lab,row);self.assertTrue(row['webhook_retired'])
  self.assertEqual(1,len([r for r in trace if r[0]=='remove']))
 def test_preflight_detects_stale_webhook_before_expensive_monitoring_or_writes(self):
  with tempfile.TemporaryDirectory() as directory:
   lab=SimpleNamespace(DIRECTORY=Path(directory),get=Mock(return_value={'metadata':{'uid':'stale'}}),write_private=Mock())
   with self.assertRaisesRegex(ValueError,'webhook'):G.database_empty(lab)
   self.assertEqual([('validatingwebhookconfiguration','percona-xtradbcluster-webhook')],
      [c.args for c in lab.get.call_args_list]);lab.write_private.assert_not_called()
 def test_actual_cleanup_deletes_cluster_hook_before_uninstall_and_namespace(self):
  with tempfile.TemporaryDirectory() as directory:
   row,state,trace,lab=fixture();base=Path(directory);lab.DIRECTORY=base;lab.BASE=ROOT
   (base/G.RECORD).write_text('{}');(base/'config.yml').write_text('original')
   ns={'kind':'Namespace','metadata':{'name':row['operator_namespace'],'uid':row['operator_namespace_uid'],
        'labels':{'kubeauto.io/longhorn-middleware-run':row['run_id']}}}
   state['namespace']=ns;state['objects']=[state['deployment'],state['secret']]
   original_get=lab.get
   def get(kind,name=None,*args,**kwargs):
    if kind in ('validatingwebhookconfiguration','deployment','secret'):return original_get(kind,name,*args,**kwargs)
    if kind=='namespace':return state['namespace'] if name==row['operator_namespace'] else None
    if kind=='objects':return {'items':state['objects']}
    if kind=='storageclass':return None
    raise AssertionError((kind,name))
   def remove(obj):
    if obj['kind']=='ValidatingWebhookConfiguration':
     self.assertTrue(row['webhook_retire_intent']);trace.append(('delete-hook',));state['hook']=None
    else:
     self.assertIsNone(state['hook']);self.assertIs(obj,ns);trace.append(('delete-namespace',));state['namespace']=None
   def execute(argv,**kwargs):
    if 'uninstall' in argv:
     self.assertIsNone(state['hook']);trace.append(('helm-uninstall',));state['objects']=[state['secret']];state['deployment']=None
    return SimpleNamespace(returncode=0,stdout='',stderr='')
   lab.get=get;lab.remove=remove;lab.execute=execute
   lab.kubectl=lambda *a,**kw:SimpleNamespace(stdout='objects')
   lab.wait=lambda label,timeout,fn:self.assertTrue(fn());lab.installation_health=lambda:trace.append(('health',))
   monitor=SimpleNamespace(verify=lambda *args:trace.append(('monitor',)))
   with patch.object(G,'receipt',return_value=row),patch.object(G,'monitor',return_value=monitor),patch.object(G,'monitor_identity',return_value={}):
    G.cleanup(lab,123)
   order=[r[0] for r in trace]
   self.assertLess(order.index('delete-hook'),order.index('helm-uninstall'))
   self.assertLess(order.index('helm-uninstall'),order.index('delete-namespace'))
   self.assertFalse((base/G.RECORD).exists())
 def test_real_shell_places_database_preflight_before_monitor_setup(self):
  text=(BASE/'longhorn-v1-middleware.sh').read_text()
  self.assertLess(text.index('"$DRIVER" database-empty'),text.index('stage monitoring-product-setup'))
 def test_fixed_static_gate_runs_the_global_webhook_contracts(self):
  self.assertIn('tests.unit.test_longhorn_middleware_webhook',(BASE/'longhorn-static.sh').read_text())
 def legacy(self,directory):
  row,state,trace,lab=fixture();lab.BASE=Path(directory);lab.DIRECTORY=lab.BASE/'cluster';lab.DIRECTORY.mkdir()
  lab.owner=lambda:{k:row[k] for k in ('run_id','cluster_uid','namespace_uid')}
  hooks=copy.deepcopy(state['hook']['webhooks'])
  import hashlib
  for hook in hooks:
   config=hook['clientConfig'];config['caBundleSHA256']=hashlib.sha256(config.pop('caBundle').encode()).hexdigest()
  proof={'schema':1,'case':'LONGHORN-37','status':'failed','classification':'test-gate','orphan_webhook_recovery':{
   'identity':lab.owner(),'durable':{'remote_exit':1,'remote_finalized':1,'module_exit':1,'module_finalized':0},
   'webhook':{'uid':state['hook']['metadata']['uid'],'owners':copy.deepcopy(state['hook']['metadata']['ownerReferences']),
    'webhooks_sha256':hashlib.sha256(json.dumps(hooks,sort_keys=True).encode()).hexdigest()},
   'absent_namespaces':['longhorn-db-'+('a'*16),'longhorn-db-operator-'+('a'*16),'longhorn-db-operator-'+('b'*16)]}}
  evidence=lab.BASE/'tests/evidence/longhorn-middleware-global-webhook-first-failure-20261022.json'
  evidence.parent.mkdir(parents=True);evidence.write_text(json.dumps(proof))
  original=lab.get
  def get(kind,*args,**kwargs):
   if kind=='validatingwebhookconfiguration':return original(kind,*args,**kwargs)
   if kind=='namespace':return state.get('live_namespace')
   if kind=='crds':return {'items':[]}
   if kind=='deployments':return {'items':[state['deployment']]} if state.get('live_controller') else {'items':[]}
   if kind in ('clusterrole','clusterrolebinding'):return None
   raise AssertionError((kind,args))
  lab.get=get;lab.remove=Mock(side_effect=lambda obj:state.update(hook=None));lab.wait=lambda label,timeout,fn:self.assertTrue(fn())
  return row,state,lab,proof,evidence
 def test_actual_no_receipt_cleanup_recovers_only_archived_empty_orphan(self):
  with tempfile.TemporaryDirectory() as directory:
   row,state,lab,proof,evidence=self.legacy(directory)
   G.cleanup(lab,123);lab.remove.assert_called_once();self.assertIsNone(state['hook'])
   G.cleanup(lab,123);lab.remove.assert_called_once()
 def test_legacy_changed_uid_namespace_owner_ca_source_or_durable_refuses(self):
  for defect in ('uid','namespace','owner','ca','source','durable','controller','missing'):
   with self.subTest(defect=defect),tempfile.TemporaryDirectory() as directory:
    row,state,lab,proof,evidence=self.legacy(directory)
    if defect=='uid':state['hook']['metadata']['uid']='foreign'
    if defect=='namespace':state['live_namespace']={'metadata':{'uid':'live'}}
    if defect=='owner':state['hook']['metadata']['ownerReferences'][0]['uid']='foreign'
    if defect=='ca':state['hook']['webhooks'][0]['clientConfig']['caBundle']='foreign'
    if defect=='source':proof['orphan_webhook_recovery']['identity']['cluster_uid']='foreign'
    if defect=='durable':proof['orphan_webhook_recovery']['durable']['remote_finalized']=0
    if defect=='controller':state['live_controller']=True
    evidence.write_text(json.dumps(proof))
    if defect=='missing':evidence.unlink()
    with self.assertRaises(ValueError):G.cleanup(lab,123)
    lab.remove.assert_not_called()
 def test_existing_native_valid_webhook_fixture_remains_rejected_by_empty_baseline(self):
  row,state,trace,lab=fixture()
  with tempfile.TemporaryDirectory() as directory:
   lab.DIRECTORY=Path(directory)
   with self.assertRaisesRegex(ValueError,'webhook'):G.database_empty(lab)

 def test_actual_failed_parent_wrapper_keeps_failure_rc_in_both_durable_fences(self):
  import subprocess
  with tempfile.TemporaryDirectory() as directory:
   prefix=Path(directory)/'parent'
   result=subprocess.run(['bash',str(ROOT/'tests/helpers/run-durable-gate.sh'),str(prefix),'TEST_PARENT_EXIT','bash','-c','exit 1'],capture_output=True,text=True)
   self.assertEqual(1,result.returncode)
   self.assertEqual('1',Path(str(prefix)+'.exit').read_text().strip())
   self.assertEqual('1',Path(str(prefix)+'.finalized').read_text().strip())
 def test_actual_fixed_shell_preflight_failure_stops_before_monitor_fixture(self):
  from tests.unit.test_longhorn_v1_middleware import MiddlewareContracts
  from tests.unit.test_longhorn_v2_middleware import V2MiddlewareContracts
  v1=MiddlewareContracts();rc,out,err,trace,durable,context=v1.run_shell(fail='longhorn-v1-middleware.py:database-empty')
  self.assertEqual(17,rc);self.assertNotIn('longhorn-v1-observability.py setup',trace)
  self.assertNotIn('LONGHORN_MIDDLEWARE_PASS',out);self.assertEqual('17',durable['exit'])
  v2=V2MiddlewareContracts();rc,out,err,trace,durable,context=v2.run_shell(fail='longhorn-v2-middleware.py:database-empty')
  self.assertEqual(17,rc);self.assertNotIn('longhorn-v2-monitoring.py setup',trace)
  self.assertNotIn('LONGHORN_V2_MIDDLEWARE_PASS',out);self.assertEqual('17',durable['exit'])
if __name__=='__main__':unittest.main(verbosity=2)
