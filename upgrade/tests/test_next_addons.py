"""Exercise installed-only addon orchestration through the real kubectl adapters."""
import json
from unittest.mock import patch
import unittest

from upgrade.product.next_addons import AddonStages
from upgrade.product.journal import Journal
from upgrade.tests import test_dns_stage as dns_fixture
from upgrade.tests import test_metrics_stage as metrics_fixture


class NextAddonContracts(unittest.TestCase):
    def execute(self, stage):
        with Journal(stage.state/'journal', {'fixture':'addons'},stage.steps) as journal:
            stage.run(journal)
            return list(journal.data['completed'])

    def test_dns_only_real_upgrade_and_completed_reentry_have_no_extra_mutations(self):
        f=dns_fixture.DNSStageContracts(methodName='runTest');f.setUp()
        self.addCleanup(f.doCleanups)
        state=f.root/'private';state.mkdir(mode=0o700)
        f.state['api']='v1.35.10'
        for node in f.state['nodes']:node['status']['nodeInfo']['kubeletVersion']='v1.35.10'
        f.save()
        source={'target':'v1.35.10','nodes':[{'name':name,'uid':uid} for name,uid in f.stage.nodes.items()],
                'api_backends':[{'server':'https://master:6443'}]}
        checks=[]
        stage=AddonStages(f.root/'kubectl',f.root/'kubeconfig',source,
            {'dns_install':'yes','ENABLE_LOCAL_DNS_CACHE':True,'metricsserver_install':'no'},
            state,lambda:checks.append('continuity'),{})
        self.assertEqual(stage.steps,['addons/dns'])
        self.assertEqual(self.execute(stage),stage.steps)
        self.assertEqual(len(f.mutations()),2)
        self.execute(stage)
        self.assertEqual(len(f.mutations()),2)
        self.assertGreaterEqual(len(checks),4)
        self.assertFalse((state/'metrics-source.json').exists())

    def test_metrics_only_captures_real_business_uid_and_requires_fresh_samples(self):
        f=metrics_fixture.MetricsStageContracts(methodName='runTest');f.setUp()
        self.addCleanup(f.doCleanups)
        state=f.root/'private';state.mkdir(mode=0o700)
        binary=f.root/'kubectl'
        program=binary.read_text().replace("elif args[0] == 'get':", "elif args == ['-n','business','get','pods','-l','app=upgrade-client','-o','json']:\n    out = {'items':[state['pod']]}\nelif args[0] == 'get':")
        binary.write_text(program)
        f.source.update(source='v1.34.13',target='v1.35.10')
        f.state['api']='v1.35.10'
        f.state['nodes'][0]['status']['nodeInfo']['kubeletVersion']='v1.35.10'
        f.save()
        stage=AddonStages(binary,f.root/'kubeconfig',f.source,
            {'dns_install':'no','metricsserver_install':'yes'},state,lambda:None,
            {'namespace':'business','client_selector':'app=upgrade-client'})
        self.assertEqual(stage.steps,['addons/metrics'])
        self.execute(stage)
        self.assertEqual(json.loads((state/'metrics-business-pods.json').read_text()),f.pods)
        count=len(f.writes())
        self.execute(stage)
        self.assertEqual(len(f.writes()),count)
        self.assertFalse((state/'dns-source.json').exists())
        # An accepted snapshot cannot adopt a replacement business Pod.
        live=json.loads((f.root/'state.json').read_text())
        live['pod']['metadata']['uid']='foreign'
        (f.root/'state.json').write_text(json.dumps(live))
        with self.assertRaisesRegex(ValueError,'Pod identity'):self.execute(stage)
