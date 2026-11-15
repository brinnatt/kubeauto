"""Real Chart rendering and exact upstream migration refusal contracts."""
import copy
import hashlib
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import jinja2
import yaml

from upgrade.product.cilium import (CHARTS, CiliumRefused, prepare_values,
                                    validate_preflight, validate_removed_policy_rules)


ROOT = Path(__file__).resolve().parents[2]


def product_values(version='1.19.5', hubble=True, ui=True, local_dns=True):
    template = jinja2.Template((ROOT / 'roles/cilium/templates/values.yaml.j2').read_text(),
                              undefined=jinja2.StrictUndefined)
    return yaml.safe_load(template.render(cilium_ver='v' + version,
        cilium_hubble_ui_ver='v0.13.5' if version == '1.19.5' else 'v0.13.6',
        cilium_hubble_enabled=hubble, cilium_hubble_ui_enabled=ui,
        ENABLE_LOCAL_DNS_CACHE=local_dns, LOCAL_DNS_CACHE='169.254.20.10',
        CLUSTER_CIDR='10.244.0.0/16'))


class CiliumValuesContracts(unittest.TestCase):
    def test_only_official_consecutive_edges_and_reviewed_values(self):
        current = product_values()
        with self.assertRaises(CiliumRefused):
            prepare_values(current, current, '1.19.5', '1.20.4')
        drift = copy.deepcopy(current)
        drift['extraArgs'] = ['--unreviewed=true']
        with self.assertRaises(CiliumRefused):
            prepare_values(drift, current, '1.19.5', '1.19.10')
        for key, value in [('identityAllocationMode', 'kvstore'),
                           ('envoy', {'enabled': True}), ('gatewayAPI', {'enabled': True}),
                           ('kubeProxyReplacement', 'true'),
                           ('cleanState', True), ('cleanBpfState', True),
                           ('cni', {'customConf': True}),
                           ('hubble', {'tls': {'auto': {'method': 'cronJob'}}})]:
            changed = {**current, key: value}
            with self.subTest(key=key), self.assertRaises(CiliumRefused):
                prepare_values(changed, changed, '1.19.5', '1.19.10')

    def test_preserves_values_and_original_input_while_pairing_all_components(self):
        current = product_values()
        original = copy.deepcopy(current)
        bridge = prepare_values(current, current, '1.19.5', '1.19.10')
        target = prepare_values(bridge, bridge, '1.19.10', '1.20.4')
        self.assertEqual(current, original)
        for values in [bridge, target]:
            self.assertEqual(values['ipam'], current['ipam'])
            self.assertEqual(values['extraArgs'], current['extraArgs'])
            self.assertEqual(values['resources'], current['resources'])
            self.assertEqual(values['upgradeCompatibility'], '1.19')
            self.assertEqual(values['hubble']['ui']['backend']['image']['tag'], 'v0.13.6')
            self.assertFalse(values['envoy']['enabled'])
            self.assertFalse(values['etcd']['enabled'])

    def test_charts_match_the_official_index(self):
        for version, digest in CHARTS.items():
            self.assertEqual(hashlib.sha256((ROOT / f'roles/cilium/files/cilium-{version}.tgz')
                                           .read_bytes()).hexdigest(), digest)

    def test_custom_digest_or_override_cannot_be_silently_removed(self):
        for key, value in [('override', 'foreign/image:tag'), ('digest', 'sha256:' + 'a' * 64),
                           ('useDigest', True)]:
            current = product_values()
            current['image'][key] = value
            with self.subTest(key=key), self.assertRaises(CiliumRefused):
                prepare_values(current, current, '1.19.5', '1.19.10')

    @unittest.skipUnless(shutil.which('helm'), 'supply verified Helm for real Chart rendering')
    def test_real_charts_main_and_preflight_images_ownership_and_datapath(self):
        for source, target in [('1.19.5', '1.19.10'), ('1.19.10', '1.20.4')]:
            for hubble, ui, local_dns in [(False, False, False), (True, False, True), (True, True, True)]:
                current = product_values(source, hubble, ui, local_dns)
                for preflight in [False, True]:
                    with self.subTest(target=target, hubble=hubble, ui=ui, preflight=preflight):
                        values = prepare_values(current, current, source, target, preflight=preflight)
                        with tempfile.NamedTemporaryFile(mode='w') as stream:
                            yaml.safe_dump(values, stream); stream.flush()
                            run = subprocess.run(['helm', 'template',
                                'cilium-preflight' if preflight else 'cilium',
                                str(ROOT / f'roles/cilium/files/cilium-{target}.tgz'),
                                '-n', 'kube-system', '--kube-version', '1.34.13', '-f', stream.name],
                                capture_output=True, text=True, timeout=30)
                        self.assertEqual(run.returncode, 0, run.stderr)
                        objects = [o for o in yaml.safe_load_all(run.stdout) if o]
                        workloads = [o for o in objects if o['kind'] in ['DaemonSet', 'Deployment', 'Job', 'CronJob']]
                        images = set()
                        for obj in workloads:
                            pod = obj['spec']['template']['spec']
                            for container in pod.get('containers', []) + pod.get('initContainers', []):
                                images.add(container['image'])
                        prefix = 'registry.talkschool.cn:5000/brinnatt/'
                        if preflight:
                            self.assertEqual({(o['kind'], o['metadata']['name']) for o in workloads},
                                {('DaemonSet', 'cilium-pre-flight-check'), ('Deployment', 'cilium-pre-flight-check')})
                            self.assertEqual(images, {prefix + 'cilium:v' + target})
                            validator = next(o for o in workloads if o['kind'] == 'Deployment')
                            self.assertIn('cilium-dbg preflight validate-cnp',
                                validator['spec']['template']['spec']['containers'][0]['args'][1])
                        else:
                            expected = {prefix + 'cilium:v' + target, prefix + 'cilium-operator-generic:v' + target}
                            if hubble: expected.add(prefix + 'hubble-relay:v' + target)
                            if hubble and ui:
                                expected.update({prefix + 'hubble-ui:v0.13.6', prefix + 'hubble-ui-backend:v0.13.6'})
                            self.assertEqual(images, expected)
                            cm = next(o for o in objects if o['kind'] == 'ConfigMap' and o['metadata']['name'] == 'cilium-config')
                            self.assertEqual(cm['data']['identity-allocation-mode'], 'crd')
                            self.assertEqual(cm['data']['ipam'], 'cluster-pool')
                            self.assertEqual(str(cm['data']['kube-proxy-replacement']).lower(), 'false')
                            ds = next(o for o in workloads if o['kind'] == 'DaemonSet')
                            pod = ds['spec']['template']['spec']
                            env = {e['name']: e for c in pod['initContainers'] for e in c.get('env', [])}
                            for name, key in [('CILIUM_ALL_STATE', 'clean-cilium-state'),
                                              ('CILIUM_BPF_STATE', 'clean-cilium-bpf-state')]:
                                self.assertEqual(env[name]['valueFrom']['configMapKeyRef'],
                                    {'name': 'cilium-config', 'key': key, 'optional': True})
                                self.assertNotIn(key, cm['data'])

    def test_policy_inspection_checks_both_scopes_and_specs_without_rewriting(self):
        policies = [{'kind': kind, 'items': []} for kind in
                    ['CiliumNetworkPolicyList', 'CiliumClusterwideNetworkPolicyList']]
        validate_removed_policy_rules(policies)
        for index in [0, 1]:
            for field in ['spec', 'specs']:
                for removed in ['kafka', 'l7', 'l7proto']:
                    bad = copy.deepcopy(policies)
                    spec = {'egress': [{'toPorts': [{'rules': {removed: []}}]}]}
                    bad[index]['items'] = [{field: spec if field == 'spec' else [spec]}]
                    original = copy.deepcopy(bad)
                    with self.assertRaises(CiliumRefused): validate_removed_policy_rules(bad)
                    self.assertEqual(bad, original)
        with self.assertRaises(CiliumRefused): validate_removed_policy_rules(policies[:1])
        policies[0]['items'] = [{'spec': {'ingress': [{'toPorts': [{'rules': {'http': [{}]}}]}]}}]
        validate_removed_policy_rules(policies)


class CiliumPreflightContracts(unittest.TestCase):
    def fixture(self):
        def obj(kind, name, uid, status):
            return {'kind': kind, 'metadata': {'name': name, 'namespace': 'kube-system', 'uid': uid,
                'generation': 2}, 'spec': {'replicas': 1}, 'status': {'observedGeneration': 2, **status}}
        status = dict(desiredNumberScheduled=2, currentNumberScheduled=2, numberReady=2,
                      updatedNumberScheduled=2, numberAvailable=2, numberMisscheduled=0)
        agent = obj('DaemonSet', 'cilium', 'agent-uid', status)
        preflight = obj('DaemonSet', 'cilium-pre-flight-check', 'preflight-uid', status)
        validator = obj('Deployment', 'cilium-pre-flight-check', 'validator-uid',
                        dict(replicas=1, readyReplicas=1, updatedReplicas=1, availableReplicas=1))
        def pods(uid):
            return {'items': [{'metadata': {'namespace': 'kube-system',
                    'ownerReferences': [{'uid': uid, 'controller': True}]},
                'spec': {'nodeName': node}, 'status': {'phase': 'Running',
                    'conditions': [{'type': 'Ready', 'status': 'True'}]}} for node in ['node-1', 'node-2']]}
        return [agent, preflight, validator, pods('agent-uid'), pods('preflight-uid')]

    def test_ready_counts_generation_and_actual_node_ownership_are_required(self):
        validate_preflight(*self.fixture())
        for case in ['not-ready', 'old-generation', 'no-validator', 'foreign-node', 'foreign-owner', 'duplicate-node']:
            args = self.fixture()
            if case == 'not-ready': args[1]['status']['numberReady'] = 1
            if case == 'old-generation': args[1]['status']['observedGeneration'] = 1
            if case == 'no-validator': args[2]['status']['readyReplicas'] = 0
            if case == 'foreign-node': args[4]['items'][0]['spec']['nodeName'] = 'foreign'
            if case == 'foreign-owner': args[4]['items'][0]['metadata']['ownerReferences'][0]['uid'] = 'foreign'
            if case == 'duplicate-node': args[4]['items'][0]['spec']['nodeName'] = 'node-2'
            with self.subTest(case=case), self.assertRaises(CiliumRefused): validate_preflight(*args)


if __name__ == '__main__':
    unittest.main()
