"""Exercise removed APIs, DRA data and streaming grants without live mutation."""
import json
import unittest
from unittest.mock import patch

from upgrade.product import next_api
from upgrade.product.kubernetes_preflight import Refused


class ApiAdmissionContracts(unittest.TestCase):
    def setUp(self):
        self.responses = {'/apis': {'groups': []},
            '/apis/rbac.authorization.k8s.io/v1/clusterroles': {'items': []},
            '/apis/rbac.authorization.k8s.io/v1/roles': {'items': []}}
        self.calls = []

    def admit(self):
        def query(binary, config, server, args):
            self.assertEqual((binary, config, server), ('kubectl', 'config', 'https://source:6443'))
            self.assertEqual(args[0], 'get')
            self.assertTrue(args[1].startswith('--raw=/apis'))
            self.calls.append(args[1][6:])
            return json.dumps(self.responses[args[1][6:]])
        with patch.object(next_api, 'query', side_effect=query):
            return next_api.admit('kubectl', 'config', 'https://source:6443')

    def test_default_profile_is_readonly_and_requires_complete_discovery(self):
        self.admit()
        self.assertEqual(set(self.calls), set(self.responses))
        self.responses['/apis'] = {}
        with self.assertRaisesRegex(Refused, 'complete group list'):
            self.admit()

    def test_both_removed_alpha_apis_refuse_even_without_existing_objects(self):
        for name in ['storagemigration.k8s.io', 'storage.k8s.io']:
            self.responses['/apis'] = {'groups': [{'name': name, 'versions': [{'version': 'v1alpha1'}]}]}
            with self.assertRaisesRegex(Refused, 'v1alpha1 is served'):
                self.admit()

    def test_ga_dra_is_allowed_but_partition_counter_data_refuses(self):
        self.responses['/apis'] = {'groups': [{'name': 'resource.k8s.io', 'versions': [{'version': 'v1'}]}]}
        path = '/apis/resource.k8s.io/v1/resourceslices'
        self.responses[path] = {'items': [{'spec': {'devices': [{'name': 'ordinary-device'}]}}]}
        self.admit()
        for spec in [{'sharedCounters': [{'name': 'counter'}]},
                     {'devices': [{'consumesCounters': [{'counterSet': 'counter'}]}]}]:
            self.responses[path] = {'items': [{'spec': spec}]}
            with self.assertRaisesRegex(Refused, 'partitionable DRA'):
                self.admit()

    def test_removed_alpha_pod_certificate_refuses_but_surviving_trust_bundle_does_not(self):
        self.responses['/apis']={'groups':[{'name':'certificates.k8s.io','versions':[{'version':'v1alpha1'}]}]}
        path='/apis/certificates.k8s.io/v1alpha1'
        self.responses[path]={'resources':[{'name':'clustertrustbundles'}]}
        self.admit()
        self.responses[path]['resources'].append({'name':'podcertificaterequests'})
        with self.assertRaisesRegex(Refused,'podcertificaterequests is served'):self.admit()
        self.assertNotIn('/apis/rbac.authorization.k8s.io/v1/roles',self.calls[-2:])
        self.responses[path]={}
        with self.assertRaisesRegex(Refused,'certificate resource discovery is incomplete'):self.admit()

    def test_get_only_streaming_grant_refuses_without_broadening_permissions(self):
        path = '/apis/rbac.authorization.k8s.io/v1/roles'
        role = {'metadata': {'namespace': 'business', 'name': 'operator'},
                'rules': [{'apiGroups': [''], 'resources': ['pods/exec'], 'verbs': ['get']}]}
        self.responses[path] = {'items': [role]}
        with self.assertRaisesRegex(Refused, 'business/operator: pods/exec'):
            self.admit()
        # Permissions may be supplied by another rule of the same Role.
        role['rules'].append({'apiGroups': [''], 'resources': ['pods/exec'], 'verbs': ['create']})
        self.admit()
        role['rules'] = [{'apiGroups': [''], 'resources': ['pods'], 'verbs': ['get']}]
        self.admit()
        role['rules'] = [{'apiGroups': ['*'], 'resources': ['*'], 'verbs': ['*']}]
        self.admit()

    def test_failed_rbac_observation_is_not_treated_as_an_empty_cluster(self):
        self.responses['/apis/rbac.authorization.k8s.io/v1/roles'] = {'error': 'fixture'}
        with self.assertRaisesRegex(Refused, 'RBAC observation is incomplete'):
            self.admit()

    def test_wildcard_controller_reads_are_not_assumed_to_be_streaming_consumers(self):
        self.responses['/apis/rbac.authorization.k8s.io/v1/roles']={'items':[{
            'metadata':{'namespace':'system','name':'garbage-collector'},
            'rules':[{'apiGroups':['*'],'resources':['*'],'verbs':['get','list','watch','delete']}]}]}
        self.admit()
