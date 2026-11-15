"""Exercise the real stage subprocess path against private offline API fixtures."""
import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from upgrade.product.cilium import CiliumRefused
from upgrade.product.cilium_stage import CiliumStage, CommandFailed
from upgrade.tests.test_cilium import ROOT, product_values


FIXTURE = r'''#!/usr/bin/env python3
import json,sys,os,stat
from pathlib import Path
import yaml
root=Path(__file__).parent
state=json.loads((root/'state.json').read_text())
args=sys.argv[1:];tool=Path(sys.argv[0]).name
with (root/'calls.jsonl').open('a') as f:f.write(json.dumps([tool,*args])+'\n')
args=args[4:]
if tool=='kubectl':args=args[:-2]
mode=state.get('mode');out={};rc=0
owner=state['owner'];release=state['release'];version=state['version']
def save(): (root/'state.json').write_text(json.dumps(state))
def obj(kind,name):
 metadata={'name':name,'namespace':'kube-system','uid':name+'-uid','generation':2,
 'annotations':{'meta.helm.sh/release-name':'cilium','meta.helm.sh/release-namespace':'kube-system'}}
 status={'observedGeneration':2}
 if kind=='DaemonSet':status.update(desiredNumberScheduled=2,currentNumberScheduled=2,
 updatedNumberScheduled=2,numberReady=2,numberAvailable=2,numberMisscheduled=0)
 else:status.update(replicas=1,updatedReplicas=1,readyReplicas=1,availableReplicas=1)
 names={'cilium':{'cilium-agent':'cilium:v'+version},
 'cilium-operator':{'cilium-operator':'cilium-operator-generic:v'+version},
 'hubble-relay':{'hubble-relay':'hubble-relay:v'+version},
 'hubble-ui':{'frontend':'hubble-ui:'+state['values']['hubble']['ui']['frontend']['image']['tag'],
 'backend':'hubble-ui-backend:'+state['values']['hubble']['ui']['backend']['image']['tag']}}
 containers=[{'name':k,'image':'registry.talkschool.cn:5000/brinnatt/'+v} for k,v in names.get(name,{}).items()]
 return {'kind':kind,'metadata':metadata,'spec':{'replicas':1,'template':{'spec':{'containers':containers}}},'status':status}
def preobjects():
 if not state.get('installed') and mode!='foreign-preflight':return []
 return [{'metadata':{'annotations':{'meta.helm.sh/release-name':release,
 'meta.helm.sh/release-namespace':'kube-system'}}}]
def pods(uid):
 return {'items':[{'metadata':{'namespace':'kube-system','ownerReferences':[{'uid':uid,'controller':True}]},
 'spec':{'nodeName':n},'status':{'phase':'Running','conditions':[{'type':'Ready','status':'True'}]}}
 for n in ['node1','node2']]}
if tool=='helm':
 if args[:3]==['get','metadata','cilium']:
  out={'name':'cilium','namespace':'kube-system','chart':'cilium','version':version,
  'appVersion':version,'status':'deployed','revision':state['revision']}
 elif args[:3]==['get','values','cilium']:out=state['values']
 elif args[0]=='install':
  assert args[1]==release and '--labels' in args
  path=Path(args[args.index('-f')+1]);assert stat.S_IMODE(path.stat().st_mode)==0o600
  assert stat.S_IMODE(path.parent.stat().st_mode)==0o700
  v=yaml.safe_load(path.read_text());assert v['agent'] is False and v['operator']['enabled'] is False
  assert v['preflight']['image']['repository']=='registry.talkschool.cn:5000/brinnatt/cilium'
  assert v['preflight']['image']['tag']=='v1.19.10' and v['cleanState'] is False
  state['installed']=True
  if mode in ['install-fail','cleanup-foreign']:rc=42
 elif args[0]=='uninstall':
  assert args[1]==release and state['installed'] and mode!='cleanup-foreign'
  state['installed']=False;state['cleaned']=True
  if mode=='source-drift-after-preflight':state['values']['extraArgs']=['--unexpected=true']
 elif args[:2]==['upgrade','cilium']:
  assert state.get('cleaned') and not state.get('installed')
  assert '--reset-values' in args and '--reuse-values' not in args and '--atomic' not in args
  if mode=='main-fail':rc=43
  else:
   state['values']=yaml.safe_load(Path(args[args.index('-f')+1]).read_text())
   state['version']='1.19.10';state['revision']=2
 else:raise AssertionError(args)
elif args[0]=='get':
 if '--ignore-not-found' in args:out={'items':preobjects()}
 elif args[1]=='secrets':
  out={'items':[{'type':'helm.sh/release.v1','metadata':{'labels':{
  'kubeauto.io/upgrade-owner':'foreign' if mode=='cleanup-foreign' else owner}}}] if state.get('installed') else []}
 elif args[1] in ['ciliumnetworkpolicies.cilium.io','ciliumclusterwidenetworkpolicies.cilium.io']:
  kind='CiliumNetworkPolicyList' if args[1].startswith('ciliumnetworkpolicies') else 'CiliumClusterwideNetworkPolicyList'
  out={'kind':kind,'items':([{'spec':{'egress':[{'toPorts':[{'rules':{'kafka':[]}}]}]}}] if mode=='removed-policy' else [])}
 elif args[1]=='pods':
  assert state.get('installed')
  out=pods('cilium-uid' if args[3]=='k8s-app=cilium' else 'cilium-pre-flight-check-uid')
 else:
  name=args[2] if args[1] in ['daemonset','deployment'] else args[1].split('/')[1]
  kind='DaemonSet' if args[1].startswith('daemonset') else 'Deployment'
  out=obj(kind,name)
  if mode=='source-notready' or (mode=='target-notready' and version=='1.19.10'):
   out['status']['numberReady' if kind=='DaemonSet' else 'readyReplicas']=0
else:raise AssertionError(args)
save();print(json.dumps(out));raise SystemExit(rc)
'''


class CiliumStageContracts(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.python = str(Path(os.sys.executable))
        # Use the same interpreter as the contract suite (PyYAML is pinned there).
        fixture = FIXTURE.replace('#!/usr/bin/env python3', '#!' + self.python)
        for name in ['helm', 'kubectl']:
            path = self.root / name
            path.write_text(fixture); path.chmod(0o700)
        (self.root / 'kubeconfig').write_text('offline fixture only')
        self.markers = []
        self.stage = CiliumStage(self.root/'helm', self.root/'kubectl', self.root/'kubeconfig',
            ROOT/'roles/cilium/files/cilium-1.19.10.tgz', 'offline-run', timeout=30, progress=self.markers.append)
        self.values = product_values()
        self.state = {'version': '1.19.5', 'revision': 1, 'values': copy.deepcopy(self.values),
                      'owner': self.stage.owner, 'release': self.stage.release}

    def run_stage(self, mode=None):
        self.state['mode'] = mode
        (self.root/'state.json').write_text(json.dumps(self.state))
        return self.stage.run(self.values, '1.19.5', '1.19.10')

    def calls(self):
        return [json.loads(line) for line in (self.root/'calls.jsonl').read_text().splitlines()]

    def mutations(self):
        return [call for call in self.calls() if call[0]=='helm' and call[5] in ['install','uninstall','upgrade','rollback']]

    def test_real_stage_orders_preflight_cleanup_reread_then_one_main_upgrade(self):
        receipt = self.run_stage()
        self.assertEqual(receipt['target_revision'], 2)
        self.assertEqual([call[5] for call in self.mutations()], ['install','uninstall','upgrade'])
        self.assertEqual(self.markers, ['UPGRADE_CILIUM_PREFLIGHT_CLEAN_PASS','UPGRADE_CILIUM_STAGE_PASS'])
        main = next(call for call in self.mutations() if call[5]=='upgrade')
        self.assertNotIn('--reuse-values', main)
        self.assertNotIn('--install', main)
        self.assertNotIn('--atomic', main)

    def test_configuration_drift_removed_policy_and_foreign_preflight_refuse_before_mutation(self):
        for mode in ['removed-policy','foreign-preflight','initial-drift']:
            with self.subTest(mode=mode):
                (self.root/'calls.jsonl').unlink(missing_ok=True)
                self.state['values'] = copy.deepcopy(self.values)
                if mode=='initial-drift': self.state['values']['extraArgs']=['--unexpected=true']
                with self.assertRaises(CiliumRefused): self.run_stage(mode)
                self.assertEqual(self.mutations(), [])

    def test_install_failure_keeps_first_rc_and_cleans_only_preflight(self):
        with self.assertRaises(CommandFailed) as error: self.run_stage('install-fail')
        self.assertEqual(error.exception.rc, 42)
        self.assertIn('install', error.exception.argv)
        self.assertEqual([call[5] for call in self.mutations()], ['install','uninstall'])
        self.assertNotIn('UPGRADE_CILIUM_STAGE_PASS', self.markers)

    def test_foreign_cleanup_record_is_retained_and_cannot_mask_first_failure(self):
        with self.assertRaises(CommandFailed) as error: self.run_stage('cleanup-foreign')
        self.assertEqual(error.exception.rc, 42)
        self.assertIn('foreign preflight release record', error.exception.__notes__[0])
        self.assertEqual([call[5] for call in self.mutations()], ['install'])

    def test_source_change_during_preflight_refuses_main_after_scoped_cleanup(self):
        with self.assertRaisesRegex(CiliumRefused, 'source changed'): self.run_stage('source-drift-after-preflight')
        self.assertEqual([call[5] for call in self.mutations()], ['install','uninstall'])

    def test_failed_main_upgrade_is_preserved_without_automatic_rollback(self):
        with self.assertRaises(CommandFailed) as error: self.run_stage('main-fail')
        self.assertEqual(error.exception.rc, 43)
        self.assertEqual([call[5] for call in self.mutations()], ['install','uninstall','upgrade'])
        self.assertNotIn('UPGRADE_CILIUM_STAGE_PASS', self.markers)

    def test_unhealthy_source_refuses_before_any_mutation_with_bounded_wait(self):
        # Isolate the product clock; patching time.sleep itself also intercepts
        # subprocess timeout supervision and makes this assertion nondeterministic.
        with mock.patch('upgrade.product.cilium_stage.time') as clock:
            clock.monotonic.side_effect = [0, 31]
            with self.assertRaisesRegex(CiliumRefused, 'convergence exceeded'):
                self.run_stage('source-notready')
            clock.sleep.assert_not_called()
        self.assertEqual(self.mutations(), [])

    def test_target_readiness_failure_cannot_emit_stage_pass(self):
        with mock.patch('upgrade.product.cilium_stage.time') as clock:
            clock.monotonic.side_effect = [0, 0, 0, 31]
            with self.assertRaisesRegex(CiliumRefused, 'convergence exceeded'):
                self.run_stage('target-notready')
            clock.sleep.assert_not_called()
        self.assertNotIn('UPGRADE_CILIUM_STAGE_PASS', self.markers)


if __name__ == '__main__':
    unittest.main()
