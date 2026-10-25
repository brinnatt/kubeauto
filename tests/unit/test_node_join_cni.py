"""Joining nodes must not give existing DaemonSets temporary bridge sandboxes."""
from pathlib import Path
import tomllib
import unittest

from jinja2 import Environment, StrictUndefined
import yaml


ROOT = Path(__file__).resolve().parents[2]


class JoinCNIContracts(unittest.TestCase):
    def test_join_waits_for_external_cni_instead_of_generating_bridge(self):
        template = Environment(undefined=StrictUndefined).from_string(
            (ROOT/'roles/containerd/templates/config.toml.j2').read_text())
        context = {'CONTAINERD_STORAGE_DIR':'/var/lib/containerd', 'SANDBOX_IMAGE':'pause:pinned',
                   'inventory_hostname':'192.168.47.149', 'groups':{'kube_master':['192.168.47.146']}}
        for joining, master in ((False,False), (True,False), (True,True)):
            values = dict(context, **({'NODE_TO_ADD':'192.168.47.149'} if joining else {}))
            if master: values['groups'] = {'kube_master':['192.168.47.149']}
            parsed = tomllib.loads(template.render(**values))
            cni = parsed['plugins']['io.containerd.cri.v1.runtime']['cni']
            self.assertEqual('' if joining and not master else '/etc/cni/net.d/10-default.conf', cni['conf_template'])
            self.assertEqual('/etc/cni/net.d', cni['conf_dir'])
            self.assertEqual(1, cni['max_conf_num'])

    def test_bridge_template_is_skipped_before_join_kubelet_starts(self):
        tasks = yaml.safe_load((ROOT/'roles/kube-node/tasks/main.yml').read_text())
        bridge = next(t for t in tasks if t.get('name') == 'prepare cni configuration file')
        self.assertEqual("NODE_TO_ADD is not defined or inventory_hostname in groups['kube_master']", bridge['when'])
        self.assertLess(tasks.index(bridge), next(i for i,t in enumerate(tasks)
                                                if t.get('name') == 'start kubelet service'))
        inline = next(t for t in tasks if t.get('import_tasks') == 'wait-node-ready.yml')
        self.assertEqual(bridge['when'], inline['when'])
        condition = Environment(undefined=StrictUndefined).compile_expression(bridge['when'])
        base = {'inventory_hostname':'worker', 'groups':{'kube_master':['master']}}
        self.assertTrue(condition(**base))
        self.assertFalse(condition(**dict(base, NODE_TO_ADD='worker')))
        self.assertTrue(condition(**dict(base, NODE_TO_ADD='master', inventory_hostname='master')))

    def test_real_cni_precedes_node_ready_and_longhorn_on_join(self):
        plays = yaml.safe_load((ROOT/'playbooks/22.addnode.yml').read_text())
        roles = [r if isinstance(r,str) else r['role'] for r in plays[0]['roles']]
        for network in ('calico','cilium','flannel','kube-router','kube-ovn'):
            self.assertGreater(roles.index(network), roles.index('kube-node'))
        self.assertIn('wait-node-ready.yml', plays[1]['tasks'][0]['import_tasks'])
        self.assertEqual('09.longhorn.yml', plays[2]['import_playbook'])


if __name__ == '__main__':
    unittest.main()
