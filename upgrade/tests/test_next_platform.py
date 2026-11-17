"""New orchestration contracts retain real Calico API/subprocess transitions."""
from copy import deepcopy
import base64
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from upgrade.product.journal import Journal
from upgrade.product.next_platform import PlatformPrerequisites, snapshot
from upgrade.product.kubernetes_preflight import Refused
from upgrade.tests import test_calico_stage as fixture

VARIABLES = fixture.VARIABLES
source_objects = fixture.source_objects


class NextPlatformContracts(unittest.TestCase):
    def setUp(self):
        self.fixture = fixture.CalicoStageContracts(methodName='runTest')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        f = self.fixture
        f.binary.write_text(f.binary.read_text().replace("str(root/'kubeconfig')", "str(root/'source/kubectl.kubeconfig')"))
        self.state = f.root/'private'; self.state.mkdir(mode=0o700)
        f.source.update(source='v1.34.13', target='v1.35.10')
        f.source['nodes'][0]['originally_schedulable'] = False
        parameters = dict(base_dir=str(f.root), cluster_dir=str(f.root), bin_dir='/fixture/bin',
            ca_dir='/etc/kubernetes/ssl', CLUSTER_CIDR=VARIABLES['CLUSTER_CIDR'],
            NODE_PORT_RANGE=VARIABLES['NODE_PORT_RANGE'],
            **{key: VARIABLES[key] for key in
               ['IP_AUTODETECTION_METHOD','CALICO_NETWORKING_BACKEND','CALICO_ENABLE_OVERLAY']})
        f.source['effective_inventory'] = {'network': 'calico',
            'nodes': {'master-one': 'node1', 'master-two': 'node2'},
            'hosts': {h: {'parameters': parameters} for h in ['master-one','master-two']},
            'groups': {'etcd':['etcd.test'], 'kube_master':['master-one'], 'kube_node':['master-two']}}
        self.books=[]; self.clients=[]; self.traffic=[]
        manager=SimpleNamespace(kube_bin_dir=f.root, clusters_dir=f.root,
            _run_playbook=lambda cluster, book, **kw: self.books.append((Path(book).name,kw)) or SimpleNamespace(rc=0))
        self.manager=manager
        self.variables={**VARIABLES,'UPGRADE_RUN_ID':'next-platform-contract','UPGRADE_PRECONDITIONS_VERIFIED':True}
        (f.root/'extra-bin').mkdir()
        (f.root/'extra-bin/etcdctl').write_text('#!'+sys.executable+'\n'+
            (Path(__file__).parent/'fixtures/calico_datastore.py').read_text())
        (f.root/'extra-bin/etcdctl').chmod(0o700)
        self.configure('v3.28.4')

    def configure(self, version):
        f=self.fixture
        f.state['objects']=source_objects(version,VARIABLES)
        f.state['platform']='v1.34.13'
        for i,n in enumerate(f.state['nodes']):
            n['spec']={'unschedulable': i==0}
            n['status']['nodeInfo']['kubeletVersion']='v1.34.13'
        f.save()

    def stage(self):
        f=self.fixture
        return PlatformPrerequisites(self.manager,'source',f.source,self.variables,self.state,
            lambda:self.traffic.append('ledger'))

    def run_stage(self, stage, fail_client=False):
        def client(version):
            if fail_client: raise Refused('fixture client first failure')
            self.clients.append(version)
        with patch.object(stage,'install_client',side_effect=client), Journal(
                self.state/'journal', {'fixture':'next-platform'}, stage.steps or ['noop']) as journal:
            stage.run(journal)
            return deepcopy(journal.data)

    def test_both_real_edges_complete_and_resume_preserves_cordon_and_objects(self):
        stage=self.stage()
        result=self.run_stage(stage)
        self.assertEqual(result['completed'],stage.steps)
        self.assertEqual(self.clients,['v3.31.7','v3.32.4'])
        self.assertTrue((self.state/'calico-created-default-bgp.json').is_file())
        mutations=len(self.fixture.mutations())
        # Reconstruct the coordinator boundary after the image has advanced.
        result=self.run_stage(self.stage())
        self.assertEqual(len(self.fixture.mutations()),mutations)
        self.assertGreater(len(self.traffic),2)

    def test_already_target_source_verifies_without_replaying_edges(self):
        self.configure('v3.32.4')
        stage=self.stage()
        self.assertEqual(stage.steps,[])
        self.run_stage(stage)
        self.assertEqual(self.fixture.mutations(),[])
        self.assertEqual(self.clients,[])

    def test_nonempty_datastore_has_same_identity_before_and_after_snapshot_reload(self):
        # Real etcd JSON strings; the empty fixture cannot expose tuple/list drift.
        binary=self.fixture.root/'extra-bin/etcdctl'
        binary.write_text('#!'+sys.executable+'\nimport json\n'
            'print(json.dumps({"kvs":[{"key":"pool-key","value":"pool-value"}]}))\n')
        binary.chmod(0o700)
        first=self.stage()
        saved=json.loads((self.state/'calico-datastore.json').read_text())
        self.assertEqual(first.original_datastore,saved)
        first.verify_datastore()
        reloaded=self.stage()
        self.assertEqual(reloaded.original_datastore,first.original_datastore)
        reloaded.verify_datastore()
        self.assertEqual(self.fixture.mutations(),[])

    def test_existing_nonempty_snapshot_reloads_before_guarded_resume(self):
        binary=self.fixture.root/'extra-bin/etcdctl'
        binary.write_text('#!'+sys.executable+'\nimport json\n'
            'print(json.dumps({"kvs":[{"key":"pool-key","value":"pool-value"}]}))\n')
        binary.chmod(0o700)
        self.stage()  # Persist the original snapshot without rewriting it.
        path=self.state/'calico-datastore.json'
        original=path.read_bytes()
        self.stage().verify_datastore()
        self.assertEqual(path.read_bytes(),original)
        self.assertEqual(self.fixture.mutations(),[])

    def test_reloaded_snapshot_still_refuses_actual_datastore_content_drift(self):
        binary=self.fixture.root/'extra-bin/etcdctl'
        binary.write_text('#!'+sys.executable+'\nimport json\n'
            'print(json.dumps({"kvs":[{"key":"pool-key","value":"original"}]}))\n')
        binary.chmod(0o700)
        self.stage()
        reloaded=self.stage()
        original=(self.state/'calico-datastore.json').read_bytes()
        binary.write_text(binary.read_text().replace('"original"','"changed"'))
        with self.assertRaisesRegex(Refused,'pools/policies/host endpoints changed'):
            reloaded.verify_datastore()
        self.assertEqual((self.state/'calico-datastore.json').read_bytes(),original)
        self.assertEqual(self.fixture.mutations(),[])

    def bgp_pair(self, obj=None):
        if obj is None:
            obj={'kind':'BGPConfiguration','apiVersion':'projectcalico.org/v3',
                 'metadata':{'name':'default','uid':'generated-bgp-uid',
                             'creationTimestamp':'2026-11-16T13:10:17Z'},
                 'spec':{'logSeverityScreen':'Info','serviceLoadBalancerAggregation':'Enabled'}}
        return [base64.b64encode(b'/calico/resources/v3/projectcalico.org/bgpconfigurations/default').decode(),
                base64.b64encode(json.dumps(obj).encode()).decode()]

    def test_only_v332_default_bgp_creation_is_admitted_then_uid_bound(self):
        stage=self.stage()
        actual=deepcopy(stage.original_datastore)
        actual['bgpconfigurations']=[self.bgp_pair()]
        with patch.object(stage,'datastore',return_value=actual):
            with self.assertRaises(Refused):stage.verify_datastore()
            stage.verify_datastore(allow_created_default=True)
            stage.verify_datastore()  # Durable identity is used after completion.
            reloaded=self.stage()
            with patch.object(reloaded,'datastore',return_value=actual):reloaded.verify_datastore()
            original=json.loads((self.state/'calico-datastore.json').read_text())
            self.assertEqual(original['bgpconfigurations'],[])
            altered=json.loads(base64.b64decode(actual['bgpconfigurations'][0][1]))
            altered['metadata']['uid']='replaced-uid'
            actual['bgpconfigurations']=[self.bgp_pair(altered)]
            with self.assertRaises(Refused):stage.verify_datastore(allow_created_default=True)
            actual['bgpconfigurations']=[]
            with self.assertRaises(Refused):stage.verify_datastore()

    def test_bgp_default_exception_refuses_routing_fields_extra_objects_and_other_drift(self):
        for fault in ['routes','mesh','aggregation','annotation','pool','extra-object']:
            with self.subTest(fault=fault):
                stage=self.stage()
                obj=json.loads(base64.b64decode(self.bgp_pair()[1]))
                actual=deepcopy(stage.original_datastore)
                if fault=='routes':obj['spec']['serviceLoadBalancerIPs']=[{'cidr':'10.0.0.0/8'}]
                elif fault=='mesh':obj['spec']['nodeToNodeMeshEnabled']=False
                elif fault=='aggregation':obj['spec']['serviceLoadBalancerAggregation']='Disabled'
                elif fault=='annotation':obj['metadata']['annotations']={'foreign':'true'}
                elif fault=='pool':actual['ippools']=[['foreign','value']]
                actual['bgpconfigurations']=[self.bgp_pair(obj)]
                if fault=='extra-object':actual['bgpconfigurations'].append(['foreign','value'])
                with patch.object(stage,'datastore',return_value=actual), self.assertRaises(Refused):
                    stage.verify_datastore(allow_created_default=True)
                self.assertFalse((self.state/'calico-created-default-bgp.json').exists())

    def test_existing_custom_bgp_object_is_preserved_instead_of_defaulted(self):
        stage=self.stage()
        custom=json.loads(base64.b64decode(self.bgp_pair()[1]))
        custom['spec']['nodeToNodeMeshEnabled']=False
        stage.original_datastore['bgpconfigurations']=[self.bgp_pair(custom)]
        actual=deepcopy(stage.original_datastore)
        with patch.object(stage,'datastore',return_value=actual):
            stage.verify_datastore(allow_created_default=True)
            actual['bgpconfigurations']=[self.bgp_pair()]
            with self.assertRaises(Refused):stage.verify_datastore(allow_created_default=True)
        self.assertFalse((self.state/'calico-created-default-bgp.json').exists())

    def test_first_failure_is_durable_without_invalid_category_masking(self):
        stage=self.stage()
        with self.assertRaisesRegex(Refused,'client first failure'):
            self.run_stage(stage,fail_client=True)
        data=json.loads((self.state/'journal/journal.json').read_text())
        self.assertEqual(data['completed'],[])
        self.assertEqual(data['failures'][0]['category'],'kubernetes')
        self.assertEqual(data['active'],stage.steps[0])

    def test_snapshot_failure_and_existing_identity_never_overwrite_boundary(self):
        path=self.state/'boundary.json'
        with self.assertRaisesRegex(ValueError,'fixture interruption'):
            snapshot(path,lambda: (_ for _ in ()).throw(ValueError('fixture interruption')))
        self.assertFalse(path.exists())
        snapshot(path,lambda:{'original':True})
        self.assertEqual(snapshot(path,lambda:self.fail('recollected')),{'original':True})
        path.chmod(0o644)
        with self.assertRaisesRegex(Refused,'private and owned'): snapshot(path,lambda:{})
