"""Outer next-hop composition around real Kubernetes/Ansible stage contracts.

Etcd/CNI/metrics have their own real-adapter contracts. Here their boundary
adapters enforce pre/postconditions while the actual coordinator, Journal,
KubernetesStage and composed Ansible playbooks run. No lab or acceptance claim.
"""
from contextlib import ExitStack
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import yaml

from upgrade.product import coordinator
from upgrade.product.journal import Journal
from upgrade.product.kubernetes_stage import CONTROL, StageFailed
from upgrade.tests import test_next_hop_stage as fixture


class NextCoordinatorContracts(unittest.TestCase):
    def setUp(self):
        outer=fixture.NextHopContracts(methodName='runTest');outer.setUp()
        self.addCleanup(outer.doCleanups)
        self.f=outer.fixture;f=self.f
        self.state=f.root/'private';self.state.mkdir(mode=0o700)
        self.cluster='customer-source'
        directory=f.root/'clusters'/self.cluster;directory.mkdir(parents=True)
        (directory/'config.yml').write_text(yaml.safe_dump({**f.variables,'K8S_VER':'1.34.13','CUSTOM':'retained'}))
        (directory/'kubectl.kubeconfig').write_bytes(f.config.read_bytes())
        self.original=(directory/'config.yml').read_text()
        inventory={'nodes':f.hosts,'groups':{'etcd':['master-one'],'kube_master':['master-one'],'kube_node':['worker-one']}}
        self.observed=deepcopy(f.source);self.observed['effective_inventory']=inventory
        (self.state/'source-observation.json').write_text(json.dumps(self.observed));(self.state/'source-observation.json').chmod(0o600)
        snapshot=self.state/'snapshot.db';snapshot.write_bytes(b'offline-etcd-snapshot')
        receipt={'schema_version':1,'source':'v1.34.13','target':'v1.35.10','cluster':self.cluster,
            'mutation':'none-after-setup','cluster_identity':f.source['cluster_identity'],'nodes':f.source['nodes'],
            'continuity':{'fixture':'ledger'},'source_observation_sha256':coordinator._sha256(self.state/'source-observation.json'),
            'recovery_point':{'path':str(snapshot),'sha256':coordinator._sha256(snapshot)}}
        for path,text in [(Path(str(self.state)+'.lease'),'owned'),(Path(str(self.state)+'.receipt.json'),json.dumps(receipt))]:
            path.write_text(text);path.chmod(0o600)
        calls=self.calls=[]
        def run_book(cluster,playbook,*,inventory=None,extra_vars=None,cmdline=None,controller_only=False,
                     fail_msg=None,fail_exception=Exception):
            # Exact keyword-only callback; wrong positional arity cannot hide.
            self.assertEqual(cluster,self.cluster)
            calls.append(Path(playbook).name)
            if controller_only:
                self.assertEqual(Path(playbook).name,'inventory.yml')
                return SimpleNamespace(rc=0)
            if Path(playbook).name in ['next-preconditions.yml','api-auth-observe.yml']:
                self.assertIn(extra_vars['K8S_VER'],['1.35.10','v1.35.10'])
                return SimpleNamespace(rc=0)
            return f.run_book(playbook,extra_vars=extra_vars)
        self.manager=SimpleNamespace(clusters_dir=f.root/'clusters',kube_bin_dir=f.root,extra_bin_dir=f.root,
            _run_playbook=run_book,_yaml_to_dict=lambda path:deepcopy(f.variables))
        self.order=[];self.platform_fail=False
        test=self
        class EtcdBoundary:
            steps=['platform/etcd-fixture']
            def __init__(self,*args):pass
            def run(self,journal):
                if self.steps[0] in journal.data['completed']:return
                test.assertEqual(set(f.read()['apis'].values()),{'v1.34.13'})
                test.assertFalse(journal.data['completed'])
                journal.begin(self.steps[0]);test.order.append('etcd');journal.complete(self.steps[0])
        class CalicoBoundary:
            steps=['platform/calico-fixture']
            def __init__(self,*args):pass
            def run(self,journal):
                if self.steps[0] in journal.data['completed']:return
                test.assertEqual(journal.data['completed'],EtcdBoundary.steps)
                test.assertEqual(set(f.read()['apis'].values()),{'v1.34.13'})
                journal.begin(self.steps[0])
                if test.platform_fail:
                    journal.failed(self.steps[0],19,'kubernetes');raise StageFailed(19)
                test.order.append('calico');journal.complete(self.steps[0])
        class AddonsBoundary:
            steps=['addons/fixture']
            def __init__(self,*args):pass
            def run(self,journal):
                if self.steps[0] in journal.data['completed']:return
                test.assertTrue(all(n['kubelet']=='v1.35.10' for n in f.read()['nodes'].values()))
                test.assertEqual((directory/'config.yml').read_text(),test.original)
                journal.begin(self.steps[0]);test.order.append('addons');journal.complete(self.steps[0])
        self.stack=ExitStack();self.addCleanup(self.stack.close)
        for module,name,value in [(coordinator,'collect',lambda result:deepcopy(inventory)),
            (coordinator,'_next_runtime_inputs',lambda extra:{}),
            (coordinator,'_business_probe',lambda *a:None),
            (__import__('upgrade.product.next_api',fromlist=['admit']),'admit',lambda *a:None),
            (__import__('upgrade.product.continuity',fromlist=['Continuity']),'Continuity',lambda *a:lambda:None),
            (__import__('upgrade.product.next_etcd',fromlist=['EtcdPatch']),'EtcdPatch',EtcdBoundary),
            (__import__('upgrade.product.next_platform',fromlist=['PlatformPrerequisites']),'PlatformPrerequisites',CalicoBoundary),
            (__import__('upgrade.product.next_addons',fromlist=['AddonStages']),'AddonStages',AddonsBoundary)]:
            self.stack.enter_context(patch.object(module,name,value))

    def execute(self):
        return coordinator._run_hop(self.manager,self.cluster,source='v1.34.13',target='v1.35.10',
            expected_cluster=self.cluster,state=self.state,run_id='handoff-1',next_hop=True)

    def test_actual_control_handoff_then_nodes_addons_atomic_config_and_readonly_reentry(self):
        result=self.execute()
        self.assertEqual(result['success_marker'],'UPGRADE_CONTROL_PLANE_STAGE_PASS')
        self.assertEqual(self.order,['etcd','calico'])
        self.assertEqual(set(self.f.read()['apis'].values()),{'v1.35.10'})
        self.assertEqual({n['kubelet'] for n in self.f.read()['nodes'].values()},{'v1.34.13'})
        result=self.execute()
        self.assertEqual(result['success_marker'],'UPGRADE_NEXT_HOP_STAGE_PASS')
        self.assertEqual(self.order,['etcd','calico','addons'])
        self.assertEqual(result['failures'],[])
        before=len(self.f.trace());self.execute()
        self.assertFalse([t for t in self.f.trace()[before:] if t['module'] in ['copy','replace','systemd_service']])
        self.assertIn('CUSTOM: retained',(self.manager.clusters_dir/self.cluster/'config.yml').read_text())

    def test_platform_first_failure_never_launches_control_or_nodes_and_resumes_original_journal(self):
        self.platform_fail=True
        with self.assertRaises(StageFailed):self.execute()
        data=json.loads((self.state/'journal/journal.json').read_text())
        self.assertEqual(data['completed'],['platform/etcd-fixture'])
        self.assertEqual(data['active'],'platform/calico-fixture')
        self.assertEqual(self.f.launched,[])
        self.assertEqual(set(self.f.read()['apis'].values()),{'v1.34.13'})
        first=deepcopy(data['failures'][0]);self.platform_fail=False
        result=self.execute()
        self.assertEqual(result['success_marker'],'UPGRADE_CONTROL_PLANE_STAGE_PASS')
        self.assertEqual(result['failures'][0],first)
        self.assertEqual(self.order,['etcd','calico'])


if __name__=='__main__':unittest.main()
