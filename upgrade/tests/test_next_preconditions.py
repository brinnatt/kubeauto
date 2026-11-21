"""Run actual Ansible host admission with read-only command fixtures, no SSH."""
import base64
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import yaml

from upgrade.tests.test_control_plane import NO_CONNECTION
from upgrade.tests.test_containerd_stage import recording_tasks

ROOT=Path(__file__).resolve().parents[2]
ACTION='''from ansible.plugins.action import ActionBase
class ActionModule(ActionBase):
 def run(self,tmp=None,task_vars=None):
  if self._task.args['module']=='slurp':
   import base64
   assert self._task.args['values']=={'src':'/var/lib/kubelet/config.yaml'}
   return {'changed':False,'content':base64.b64encode(task_vars['fixture_config'].encode()).decode()}
  assert self._task.args['module']=='command', 'admission attempted mutation'
  argv=self._task.args['values']['argv']
  if argv==['uname','-r']: out=task_vars['fixture_kernel']
  elif argv==['stat','-fc','%T','/sys/fs/cgroup']: out=task_vars['fixture_cgroup']
  elif argv[:2]==['systemctl','cat']: out=task_vars['fixture_unit']
  else: return {'failed':True,'msg':'unmodelled command'}
  return {'rc':0,'changed':False,'stdout':out,'stdout_lines':out.splitlines()}
'''


class NextPreconditionsContracts(unittest.TestCase):
    def test_136_removed_and_locked_gates_in_service_and_typed_configuration(self):
        removed = ['CSIMigrationPortworx', 'HonorPVReclaimPolicy',
            'InPlacePodVerticalScalingAllocatedStatus', 'InTreePluginPortworxUnregister',
            'MemoryManager', 'ServiceTrafficDistribution', 'TopologyAwareHints']
        locked = {name: True for name in ['CSIServiceAccountTokenSecrets', 'DRAAdminAccess',
            'DisableNodeKubeProxyVersion', 'ExternalServiceAccountTokenSigner', 'ImageVolume',
            'KubeletFineGrainedAuthz', 'KubeletPSI', 'KubeletPodResourcesDynamicResources',
            'KubeletPodResourcesGet', 'MutableCSINodeAllocatableCount', 'NodeLogQuery',
            'ProcMountType', 'SELinuxChangePolicy', 'SELinuxMountReadWriteOncePod',
            'TopologyManagerPolicyOptions', 'UserNamespacesSupport', 'VolumeAttributesClass']}
        locked['GitRepoVolumeDriver'] = False
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root/'actions').mkdir(); (root/'connections').mkdir()
            (root/'actions/runtime_record.py').write_text(ACTION)
            (root/'connections/upgrade_test_forbidden.py').write_text(NO_CONNECTION)
            play = yaml.safe_load((ROOT/'upgrade/product/next-preconditions.yml').read_text())[0]
            play['tasks'] = recording_tasks(play['tasks'])
            (root/'book.yml').write_text(yaml.safe_dump([play]))
            (root/'hosts').write_text('[kube_master]\n[kube_node]\nworker-one\n'
                '[all:vars]\nansible_connection=upgrade_test_forbidden\n')
            cases = [('1.36.6', {}, {}, True),
                ('v1.36.6', locked, locked, True),
                # These are still configurable on the source minor. Never
                # retroactively apply the new target policy to the old path.
                ('1.35.10', {'GitRepoVolumeDriver': True}, {'ImageVolume': False}, True)]
            for name in removed:
                for location in ['cli', 'typed']:
                    cases.append(('1.36.6', {name: True} if location == 'cli' else {},
                        {name: False} if location == 'typed' else {}, False))
            for name, default in locked.items():
                for location in ['cli', 'typed']:
                    cases.append(('1.36.6', {name: not default} if location == 'cli' else {},
                        {name: not default} if location == 'typed' else {}, False))
            for target, cli, typed, expected in cases:
                with self.subTest(target=target, cli=cli, typed=typed):
                    unit = '--feature-gates=' + ','.join(f'{k}={str(v).lower()}' for k,v in cli.items())
                    config = {'kind': 'KubeletConfiguration',
                        'apiVersion': 'kubelet.config.k8s.io/v1beta1', 'featureGates': typed}
                    variables = {'K8S_VER': target, 'fixture_kernel': '5.14.0-611.el9',
                        'fixture_cgroup': 'cgroup2fs', 'fixture_unit': unit,
                        'fixture_config': yaml.safe_dump(config)}
                    (root/'vars.json').write_text(json.dumps(variables))
                    result = subprocess.run([str(Path(sys.executable).parent/'ansible-playbook'),
                        '-i', str(root/'hosts'), str(root/'book.yml'), '-e', '@'+str(root/'vars.json')],
                        capture_output=True, text=True, timeout=30,
                        env={**os.environ, 'ANSIBLE_ACTION_PLUGINS': str(root/'actions'),
                            'ANSIBLE_CONNECTION_PLUGINS': str(root/'connections'),
                            'ANSIBLE_LOCAL_TEMP': str(root/'local')})
                    self.assertEqual(result.returncode == 0, expected, result.stdout+result.stderr)
                    self.assertNotIn('TEST_REMOTE_CONNECTION_REFUSED', result.stdout+result.stderr)
                    self.assertNotIn('unmodelled', result.stdout+result.stderr)

    def test_actual_host_floor_cgroup_opt_in_and_removed_flags(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)
            (root/'actions').mkdir();(root/'connections').mkdir()
            (root/'actions/runtime_record.py').write_text(ACTION)
            (root/'connections/upgrade_test_forbidden.py').write_text(NO_CONNECTION)
            plays=yaml.safe_load((ROOT/'upgrade/product/next-preconditions.yml').read_text())
            for play in plays: play['tasks']=recording_tasks(play['tasks'])
            (root/'book.yml').write_text(yaml.safe_dump(plays))
            (root/'hosts').write_text('[kube_master]\nmaster-one\n[kube_node]\nworker-one\n[all:vars]\nansible_connection=upgrade_test_forbidden\n')
            for kernel,cgroup,policy,unit,ok in [
                ('4.18.0-553.el8','tmpfs',False,'legacy source',False),
                ('5.14.0-611.el9','cgroup2fs',True,'supported source',True),
                ('5.14.0-611.el9','tmpfs',True,'legacy source',False),
                ('5.14.0-611.el9','tmpfs',False,'legacy source',True),
                ('5.14.0-611.el9','tmpfs','false','legacy source',False),
                ('5.14.0-611.el9','cgroup2fs',True,'--pod-infra-container-image=pause',False),
                ('5.14.0-611.el9','cgroup2fs',True,'--feature-gates=StorageVersionMigrator=true',False),
                *[('5.14.0','cgroup2fs',True,'--feature-gates='+gate,False) for gate in
                  ['SizeMemoryBackedVolumes=true','ComponentSLIs=false','UserNamespacesPodSecurityStandards=true',
                   'StrictCostEnforcementForVAP=true','StrictCostEnforcementForWebhooks=true',
                   'DynamicResourceAllocation=false','JobManagedBy=false','ExecProbeTimeout=false',
                   'AggregatedDiscoveryRemoveBetaType=false','SystemdWatchdog=false','DRAPartitionableDevices=true']]]:
                with self.subTest(kernel=kernel,cgroup=cgroup,policy=policy,unit=unit):
                    (root/'vars.json').write_text(json.dumps({'fixture_kernel':kernel,'fixture_cgroup':cgroup,
                        'fixture_unit':unit,'KUBELET_FAIL_CGROUP_V1':policy,
                        'fixture_config':'kind: KubeletConfiguration\napiVersion: kubelet.config.k8s.io/v1beta1\n'}))
                    result=subprocess.run([str(Path(sys.executable).parent/'ansible-playbook'),'-i',str(root/'hosts'),
                        str(root/'book.yml'),'-e','@'+str(root/'vars.json')],capture_output=True,text=True,timeout=30,
                        env={**os.environ,'ANSIBLE_ACTION_PLUGINS':str(root/'actions'),
                            'ANSIBLE_CONNECTION_PLUGINS':str(root/'connections'),'ANSIBLE_LOCAL_TEMP':str(root/'local')})
                    self.assertEqual(result.returncode==0,ok,result.stdout+result.stderr)
                    self.assertNotIn('TEST_REMOTE_CONNECTION_REFUSED',result.stdout+result.stderr)
                    self.assertNotIn('unmodelled',result.stdout+result.stderr)

    def test_actual_etcd_unit_capture_is_private_retained_and_refuses_drift(self):
        # Read-only host commands are adapted; the actual delegated copy/slurp
        # modules exercise controller filesystem ownership and force:false.
        action = '''from ansible.plugins.action import ActionBase
class ActionModule(ActionBase):
 def run(self,tmp=None,task_vars=None):
  module=self._task.args['module'];values=self._task.args['values']
  if module=='command':
   assert values['argv']==['systemctl','show','--property=FragmentPath','--value','etcd']
   return {'rc':0,'changed':False,'stdout':'/etc/systemd/system/etcd.service'}
  assert module=='stat' and values=={'path':'/etc/systemd/system/etcd.service','checksum_algorithm':'sha256','follow':False}
  return {'changed':False,'stat':{'isreg':True,'islnk':False,'checksum':task_vars['fixture_unit_sha']}}
'''
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            (root/'actions').mkdir();(root/'connections').mkdir();(root/'state').mkdir(mode=0o700)
            (root/'actions/runtime_record.py').write_text(action)
            (root/'connections/upgrade_test_forbidden.py').write_text(NO_CONNECTION)
            play=yaml.safe_load((ROOT/'upgrade/product/next-preconditions.yml').read_text())[1]
            for task in play['tasks']:
                for module in ['command','stat']:
                    key='ansible.builtin.'+module
                    if key in task:task['runtime_record']={'module':module,'values':task.pop(key)}
            (root/'book.yml').write_text(yaml.safe_dump([play]))
            (root/'hosts').write_text('[etcd]\nmember-one\n[all:vars]\nansible_connection=upgrade_test_forbidden\n')
            for digest,expected in [('a'*64,True),('a'*64,True),('b'*64,False)]:
                (root/'vars.json').write_text(json.dumps({'fixture_unit_sha':digest,
                    'UPGRADE_STATE_DIRECTORY':str(root/'state')}))
                result=subprocess.run([str(Path(sys.executable).parent/'ansible-playbook'),'-i',str(root/'hosts'),
                    str(root/'book.yml'),'-e','@'+str(root/'vars.json')],capture_output=True,text=True,timeout=30,
                    env={**os.environ,'ANSIBLE_ACTION_PLUGINS':str(root/'actions'),
                        'ANSIBLE_CONNECTION_PLUGINS':str(root/'connections'),'ANSIBLE_LOCAL_TEMP':str(root/'local')})
                self.assertEqual(result.returncode==0,expected,result.stdout+result.stderr)
                self.assertNotIn('TEST_REMOTE_CONNECTION_REFUSED',result.stdout+result.stderr)
                path=root/'state/etcd-unit-member-one.json'
                self.assertEqual(path.stat().st_mode & 0o777,0o600)
                self.assertEqual(json.loads(path.read_text()),{'host':'member-one','sha256':'a'*64})
