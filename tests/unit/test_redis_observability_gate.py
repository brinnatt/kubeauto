"""Pinned observability fixture provenance, scrape, alert and ownership contracts."""
import copy
import importlib.util
import json
from pathlib import Path
import unittest
from unittest.mock import Mock
import io
from contextlib import redirect_stdout
import yaml

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('redis_observability', ROOT / 'tests/helpers/redis-observability-gate.py')
observability = importlib.util.module_from_spec(spec)
spec.loader.exec_module(observability)


class RedisObservabilityTests(unittest.TestCase):
    def test_events_distinguish_recovered_startup_from_current_or_unknown_failures(self):
        pod = {'metadata':{'name':'scraper', 'namespace':'redis-gate', 'uid':'new'},
               'status':{'phase':'Running', 'podIP':'172.30.0.10',
                         'conditions':[{'type':'Ready', 'status':'True',
                                        'lastTransitionTime':'2026-10-13T03:00:10Z'}],
                         'containerStatuses':[{'ready':True, 'restartCount':0,
                                               'state':{'running':{}}}]}}
        owner = {'name':'scraper', 'namespace':'redis-gate', 'uid':'new', 'kind':'Pod'}
        events = [{'involvedObject':owner, 'type':'Normal', 'reason':reason}
                  for reason in ('Scheduled', 'Started')]
        transient = {'involvedObject':owner, 'type':'Warning', 'reason':'Unhealthy',
                     'lastTimestamp':'2026-10-13T03:00:09Z',
                     'message':'Readiness probe failed: Get "http://172.30.0.10:9090/-/ready": '
                               'dial tcp 172.30.0.10:9090: connect: connection refused'}
        self.assertEqual(observability.lifecycle_events(pod, events), 0)
        self.assertEqual(observability.lifecycle_events(pod, events + [transient]), 1)
        old = copy.deepcopy(transient); old['involvedObject']['uid'] = 'old'
        self.assertEqual(observability.lifecycle_events(pod, events + [old]), 0)
        for change in ('after-ready', 'series-after-ready', 'unknown-warning', 'wrong-endpoint',
                       'no-time', 'not-ready', 'restarted', 'no-started', 'foreign-owner'):
            with self.subTest(change=change):
                current = copy.deepcopy(pod); rows = copy.deepcopy(events + [transient])
                if change == 'after-ready': rows[-1]['lastTimestamp'] = '2026-10-13T03:00:11Z'
                elif change == 'series-after-ready':
                    rows[-1]['series'] = {'lastObservedTime':'2026-10-13T03:00:11Z'}
                elif change == 'unknown-warning': rows[-1]['reason'] = 'FailedMount'
                elif change == 'wrong-endpoint': rows[-1]['message'] = 'Readiness probe failed: timeout'
                elif change == 'no-time': del rows[-1]['lastTimestamp']
                elif change == 'not-ready': current['status']['conditions'][0]['status'] = 'False'
                elif change == 'restarted': current['status']['containerStatuses'][0]['restartCount'] = 1
                elif change == 'no-started': rows.pop(1)
                elif change == 'foreign-owner': rows[0]['involvedObject']['namespace'] = 'foreign'
                with self.assertRaises(ValueError): observability.lifecycle_events(current, rows)

    def test_scraper_reconnect_preserves_product_pods_and_checks_foreign_ownership(self):
        document = observability.fixture_documents('redis-gate', 'redis-prometheus-test', 'a' * 32,
                                                   [], 'pinned')[1]
        current = {'metadata': dict(document['metadata'], uid='old', resourceVersion='42')}
        gate = Mock(ns='redis-gate')
        gate.get.side_effect = [current, {'metadata': {'uid': 'new'}}]
        gate.forward.return_value = 12345
        with redirect_stdout(io.StringIO()):
            self.assertEqual(observability.restart_scraper(gate, document), 12345)
        deletion = gate.k.call_args_list[0]
        self.assertIn('/pods/redis-prometheus-test', deletion.args[2])
        self.assertEqual(json.loads(deletion.kwargs['input_text'])['preconditions'],
                         {'uid': 'old', 'resourceVersion': '42'})
        gate.forward.assert_called_once_with('redis-prometheus-test', 9090, reuse=False)
        gate.k.reset_mock()
        gate.get.side_effect = None
        gate.get.return_value = {'metadata': {'labels': {'foreign': 'owner'}}}
        with self.assertRaisesRegex(ValueError, 'ownership'):
            observability.restart_scraper(gate, document)
        gate.k.assert_not_called()

    def test_fixture_requires_accepted_exact_dual_architecture_publication(self):
        evidence = json.loads((ROOT / 'tests/evidence/redis-observability-images-20261013.json').read_text())
        self.assertEqual(observability.publication(evidence)['key'], observability.KEY)
        for edit in (lambda e: e.update(workflow_conclusion='failure'), lambda e: e.update(durable_rc=1),
                     lambda e: e['images'][0].update(key='brinnatt/foreign:latest'),
                     lambda e: e['images'][0].update(head='foreign'),
                     lambda e: e['images'][0]['platforms'].pop('linux/arm64')):
            bad = copy.deepcopy(evidence)
            edit(bad)
            with self.assertRaises(ValueError):
                observability.publication(bad)

    def test_scrape_and_alert_are_bound_to_three_exact_product_members(self):
        members = [{'metadata': {'name': f'data-{i}'}, 'status': {'podIP': f'172.30.0.{i+1}'}}
                   for i in range(3)]
        config, pod = observability.fixture_documents('redis-gate', 'redis-prometheus-test',
                                                     'a' * 32, members, 'pinned@sha256:identity')
        self.assertEqual(config['metadata'], pod['metadata'])
        self.assertEqual(config['metadata']['namespace'], 'redis-gate')
        scrape = yaml.safe_load(config['data']['prometheus.yml'])
        rules = yaml.safe_load(config['data']['rules.yml'])
        self.assertEqual(scrape['global']['scrape_interval'], '2s')
        targets = scrape['scrape_configs'][0]['static_configs']
        self.assertEqual([t['targets'][0] for t in targets], [f'172.30.0.{i+1}:9121' for i in range(3)])
        self.assertEqual([t['labels']['redis_member'] for t in targets], [f'data-{i}' for i in range(3)])
        self.assertEqual(rules['groups'][0]['rules'][0]['expr'], 'up{job="redis-gate-exporters"} == 0')
        self.assertFalse(pod['spec']['automountServiceAccountToken'])
        self.assertTrue(any('emptyDir' in v for v in pod['spec']['volumes']))
        self.assertFalse(any('persistentVolumeClaim' in v for v in pod['spec']['volumes']))
        self.assertEqual(pod['spec']['containers'][0]['image'], 'pinned@sha256:identity')


if __name__ == '__main__':
    unittest.main()
