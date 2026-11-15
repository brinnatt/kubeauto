"""Preserve the delivered network while adopting the target's actual installer."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import jinja2
import yaml


ROOT = Path(__file__).resolve().parents[2]
ROLE = ROOT / 'roles/flannel'


class FlannelContracts(unittest.TestCase):
    def render(self, version, backend='vxlan', direct=False, *, template=None):
        text = (ROLE/'templates/kube-flannel.yaml.j2').read_text() if template is None else template
        return jinja2.Template(text, undefined=jinja2.StrictUndefined, trim_blocks=True).render(
            CLUSTER_CIDR='10.244.0.0/16', FLANNEL_BACKEND=backend, DIRECT_ROUTING=direct,
            flannel_ver=version, flannel_cni_ver='v1.9.1-flannel3' if version=='v0.28.10' else 'v1.8.0-flannel1')

    def test_delivered_source_manifest_is_byte_equal_to_original_template_render(self):
        original = subprocess.check_output(['git','show','940c9020144a88b370cba36793a4d7fad6a347ab:roles/flannel/templates/kube-flannel.yaml.j2'],
                                            cwd=ROOT, text=True)
        for backend, direct in [('vxlan',False),('vxlan',True),('host-gw',False)]:
            self.assertEqual(self.render('v0.28.4',backend,direct),
                             self.render('v0.28.4',backend,direct,template=original))

    def test_target_preserves_resource_identity_rbac_selectors_and_ipam(self):
        for backend, direct in [('vxlan',False),('vxlan',True),('host-gw',False)]:
            source = list(yaml.safe_load_all(self.render('v0.28.4',backend,direct)))
            target = list(yaml.safe_load_all(self.render('v0.28.10',backend,direct)))
            self.assertEqual([(o['kind'],o['metadata']) for o in source],
                             [(o['kind'],o['metadata']) for o in target])
            for before, after in zip(source,target):
                if before['kind'] in ['ServiceAccount','ClusterRole','ClusterRoleBinding']:
                    self.assertEqual(before,after)
            old_ds = next(o for o in source if o['kind']=='DaemonSet')
            ds = next(o for o in target if o['kind']=='DaemonSet')
            self.assertEqual(old_ds['spec']['selector'],ds['spec']['selector'])
            self.assertEqual(old_ds['spec']['template']['metadata'],ds['spec']['template']['metadata'])
            pod = ds['spec']['template']['spec']
            self.assertEqual(pod['volumes'],old_ds['spec']['template']['spec']['volumes'])
            self.assertTrue(pod['hostNetwork'])
            cm = next(o for o in target if o['kind']=='ConfigMap')
            old_cm = next(o for o in source if o['kind']=='ConfigMap')
            self.assertEqual(cm['data']['net-conf.json'],old_cm['data']['net-conf.json'])
            cni = json.loads(cm['data']['cni-conf.json'])
            self.assertEqual(cni['cniVersion'],'1.0.0')
            self.assertEqual(cni['plugins'],json.loads(old_cm['data']['cni-conf.json'])['plugins'])

    def test_target_uses_atomic_upstream_installer_and_local_health_endpoint(self):
        objects = list(yaml.safe_load_all(self.render('v0.28.10')))
        ds = next(o for o in objects if o['kind']=='DaemonSet')
        pod = ds['spec']['template']['spec']
        installer = next(c for c in pod['initContainers'] if c['name']=='install-cni')
        self.assertEqual(installer['command'],['/opt/bin/install-conf'])
        self.assertEqual(installer['args'],['/etc/kube-flannel/cni-conf.json','/etc/cni/net.d/10-flannel.conflist'])
        plugin = next(c for c in pod['initContainers'] if c['name']=='install-cni-plugin')
        self.assertEqual(plugin['image'],'registry.talkschool.cn:5000/brinnatt/flannel-cni-plugin:v1.9.1-flannel3')
        container = pod['containers'][0]
        self.assertIn('--healthz-ip=127.0.0.1',container['args'])
        self.assertIn('--healthz-port=8081',container['args'])
        for probe,path in [('livenessProbe','/healthz'),('readinessProbe','/readyz')]:
            self.assertEqual(container[probe]['httpGet'],{'host':'127.0.0.1','port':8081,'path':path})

    def test_actual_role_refuses_wrong_target_pair_before_any_command_or_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); (root/'bin').mkdir()
            fake = '#!/bin/sh\ntouch "$FLANNEL_TEST_CALLED"\nexit 99\n'
            command = root/'bin/kubectl'; command.write_text(fake); command.chmod(0o700)
            variables = {'flannel_ver':'v0.28.10','flannel_cni_ver':'v1.8.0-flannel1',
                         'cluster_dir':str(root/'untouched'),'CHANGE_CA':False}
            book = root/'fixture.yml'
            book.write_text(yaml.safe_dump([{'hosts':'localhost','connection':'local','gather_facts':False,
                'vars':variables,'tasks':[{'ansible.builtin.include_tasks':str(ROLE/'tasks/main.yml')}]}]))
            run = subprocess.run([str(Path(sys.executable).parent/'ansible-playbook'),'-i','localhost,',str(book)],
                capture_output=True,text=True,timeout=30,env={**os.environ,
                    'PATH':str(root/'bin')+':'+os.environ['PATH'],
                    'ANSIBLE_LOCAL_TEMP':str(root/'ansible-local'),'FLANNEL_TEST_CALLED':str(root/'called')})
            self.assertEqual(run.returncode,2,run.stdout+run.stderr)
            self.assertIn('requires its official',run.stdout)
            self.assertFalse((root/'untouched').exists())
            self.assertFalse((root/'called').exists())


if __name__=='__main__':
    unittest.main()
