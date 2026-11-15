"""Render the delivered and target Chart through the actual product values."""
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

import jinja2
import yaml


ROOT = Path(__file__).resolve().parents[2]
ROLE = ROOT / 'roles/cluster-addon'
PAIRS = {'4.13.0': ('v1.13.0', 'v1.6.0'), '4.15.1': ('v1.15.1', 'v1.6.9')}


class IngressChartContracts(unittest.TestCase):
    def values(self, chart, metrics=False):
        env = jinja2.Environment(undefined=jinja2.StrictUndefined)
        template = env.from_string((ROLE / 'templates/ingress-nginx/values.yaml.j2').read_text())
        return template.render(ingress_nginx_ver=chart, ingress_nginx_metrics_enabled=metrics,
                               prom_install='yes' if metrics else 'no')

    def test_target_chart_matches_official_asset_digest(self):
        chart = ROLE / 'files/ingress-nginx-4.15.1.tgz'
        self.assertEqual(hashlib.sha256(chart.read_bytes()).hexdigest(),
                         '3eff0bd18151d6e6b1c441463410571443dda1ac78292cb189346628de784f0c')

    def test_values_keep_exact_official_pairing_and_existing_delivery_settings(self):
        for chart, (controller, certgen) in PAIRS.items():
            value = yaml.safe_load(self.values(chart))
            self.assertEqual(value['controller']['image']['tag'], controller)
            self.assertEqual(value['controller']['admissionWebhooks']['patch']['image']['tag'], certgen)
            self.assertEqual(value['global']['image']['registry'], 'registry.talkschool.cn:5000')
            self.assertEqual(value['controller']['service']['type'], 'NodePort')
            self.assertEqual(value['controller']['service']['externalTrafficPolicy'], 'Local')
            self.assertTrue(value['controller']['admissionWebhooks']['enabled'])
            self.assertFalse(value['controller']['scope']['enabled'])

    def test_unknown_chart_refuses_before_any_mutation(self):
        tasks = yaml.safe_load((ROLE / 'tasks/ingress-nginx.yml').read_text())[0]['block']
        self.assertIn('ansible.builtin.assert', tasks[0])
        self.assertEqual(tasks[0]['ansible.builtin.assert']['that'],
                         ["ingress_nginx_ver in ['4.13.0', '4.15.1']"])
        with self.assertRaises(jinja2.UndefinedError):
            self.values('4.16.0')

    def test_actual_role_refuses_unknown_chart_before_local_or_cluster_mutation(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / 'bin').mkdir()
            fake = ('#!/usr/bin/env python3\nimport os\nfrom pathlib import Path\n'
                    'Path(os.environ["INGRESS_TEST_COMMAND_LOG"]).touch()\nraise SystemExit(99)\n')
            for name in ['kubectl', 'helm']:
                path = root / 'bin' / name
                path.write_text(fake)
                path.chmod(0o700)
            variables = {'ingress_nginx_install': 'yes', 'ingress_nginx_ver': '4.16.0',
                         'ingress_nginx_namespace': 'ingress-fixture',
                         'cluster_dir': str(root / 'untouched'), 'base_dir': str(root)}
            book = root / 'fixture.yml'
            book.write_text(yaml.safe_dump([{'hosts': 'localhost', 'connection': 'local',
                'gather_facts': False, 'vars': variables,
                'tasks': [{'ansible.builtin.include_tasks': str(ROLE / 'tasks/ingress-nginx.yml')}]}]))
            result = subprocess.run([str(Path(sys.executable).parent / 'ansible-playbook'),
                '-i', 'localhost,', str(book)], capture_output=True, text=True, timeout=30,
                env={**os.environ, 'PATH': str(root / 'bin') + ':' + os.environ['PATH'],
                     'ANSIBLE_LOCAL_TEMP': str(root / 'ansible-local'),
                     'INGRESS_TEST_COMMAND_LOG': str(root / 'commands-called')})
            self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
            self.assertIn('Unsupported ingress-nginx Chart', result.stdout)
            self.assertFalse((root / 'untouched').exists())
            self.assertFalse((root / 'commands-called').exists())

    @unittest.skipUnless(shutil.which('helm'), 'Helm 3.19.0 must be explicitly supplied for render contracts')
    def test_real_helm_render_keeps_ownership_webhook_and_only_two_consumed_images(self):
        for chart, pair in PAIRS.items():
            for metrics in [False, True]:
                with self.subTest(chart=chart, metrics=metrics), tempfile.TemporaryDirectory() as temp:
                    values = Path(temp) / 'values.yaml'
                    values.write_text(self.values(chart, metrics))
                    command = ['helm', 'template', 'ingress-nginx',
                        str(ROLE / f'files/ingress-nginx-{chart}.tgz'), '-n', 'ingress-nginx',
                        '--kube-version', '1.33.6' if chart == '4.13.0' else '1.34.13',
                        '-f', str(values), '--api-versions', 'monitoring.coreos.com/v1/ServiceMonitor',
                        '--api-versions', 'monitoring.coreos.com/v1/PrometheusRule']
                    result = subprocess.run(command, capture_output=True, text=True, timeout=30)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    objects = [o for o in yaml.safe_load_all(result.stdout) if o]
                    deployments = [o for o in objects if o['kind'] == 'Deployment']
                    self.assertEqual(len(deployments), 1)
                    deployment = deployments[0]
                    self.assertEqual(deployment['metadata']['name'], 'ingress-nginx-controller')
                    pod = deployment['spec']['template']['spec']
                    self.assertEqual(pod['containers'][0]['image'],
                        'registry.talkschool.cn:5000/brinnatt/ingress-nginx-controller:' + pair[0])
                    self.assertIn('--validating-webhook=:8443', pod['containers'][0]['args'])
                    self.assertFalse(pod.get('hostNetwork', False))
                    self.assertFalse(any(m.get('mountPath') == '/etc/nginx/template' for
                                         m in pod['containers'][0].get('volumeMounts', [])))
                    jobs = [o for o in objects if o['kind'] == 'Job']
                    self.assertEqual({o['metadata']['name'] for o in jobs},
                        {'ingress-nginx-admission-create', 'ingress-nginx-admission-patch'})
                    for job in jobs:
                        self.assertEqual(job['spec']['template']['spec']['containers'][0]['image'],
                            'registry.talkschool.cn:5000/brinnatt/kube-webhook-certgen:' + pair[1])
                    webhook = next(o for o in objects if o['kind'] == 'ValidatingWebhookConfiguration')
                    self.assertEqual(webhook['metadata']['name'], 'ingress-nginx-admission')
                    self.assertTrue(all(w['failurePolicy'] == 'Fail' for w in webhook['webhooks']))


if __name__ == '__main__':
    unittest.main()
