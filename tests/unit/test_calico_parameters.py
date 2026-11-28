"""Real CLI rendering, staged execution, backup, verification and refusal."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import yaml

ROOT=Path(__file__).resolve().parents[2]
TOOL=ROOT/'tools/k8stools/CalicoPolicyCli.py'


class CalicoParameters(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='calico-parameters-')
        self.addCleanup(self.tmp.cleanup)
        self.base=Path(self.tmp.name)
        self.trace=self.base/'calls.jsonl'
        self.executor=self.base/'executor'
        self.executor.write_text('#!'+sys.executable+'\n'+r'''
import json,os,sys
args=sys.argv[1:]
body=sys.stdin.read() if 'apply' in args else ''
with open(os.environ['CALLS'],'a') as f: f.write(json.dumps(dict(argv=args,body=body))+'\n')
if 'api-resources' in args:
    print(os.environ.get('CALICO_FIXTURE_RESOURCES','globalnetworkpolicies.crd.projectcalico.org\nhostendpoints.crd.projectcalico.org\nprofiles.crd.projectcalico.org'))
elif 'nodes' in args:
    print(json.dumps(dict(items=[dict(metadata=dict(name='node-a'),status=dict(addresses=[dict(type='InternalIP',address='10.1.2.3'),dict(type='ExternalIP',address='198.51.100.3')]))])))
elif 'pods' in args: print(json.dumps(dict(items=[dict(metadata=dict(name='fixture-pod'))])))
elif 'get' in args: print(json.dumps(dict(apiVersion='v1',kind='List',items=[])))
else: print('fixture-operation-ok')
''')
        self.executor.chmod(0o700)

    def cli(self,command,*flags,executor='calicoctl',expected=0,no_log=True,global_flags=()):
        argv=[sys.executable,'-B',str(TOOL),'--context','fixture-context',
              '--kubectl',str(self.executor),'--calicoctl',str(self.executor),'--executor',executor]
        if no_log: argv+=['--no-log-file']
        argv += [*global_flags,command,*flags]
        result=subprocess.run(argv,cwd=self.base,env=dict(os.environ,CALLS=str(self.trace)),
                              capture_output=True,text=True,timeout=15)
        self.assertEqual(result.returncode,expected,(result.stdout+result.stderr)[-4000:])
        self.assertNotIn('Traceback',result.stdout+result.stderr)
        return result

    def calls(self):
        return [json.loads(line) for line in self.trace.read_text().splitlines()] if self.trace.exists() else []

    def flags(self):
        return ['--traffic-layer','both','-n','fixture-ns','--pod-label','app=demo','--pod-label','tier=backend',
                '--k8s-np-name','custom-netpol','-a','10.0.0.0/8','--allow-net','fd00::/8',
                '--port','19100','--policy-name','custom-gnp','--hep-prefix','custom-hep','--interface','eth0',
                '--include-external-ip','--policy-selector',"has(role)",'--policy-order','12.5',
                '--hep-profile','custom-profile','--gnp-tier','security','--gnp-apply-on-forward',
                '--gnp-performance-hint','AssumeNeededOnEveryNode']

    def assert_manifest(self,body):
        docs=[d for d in yaml.safe_load_all(body) if d]
        by_kind={d['kind']:d for d in docs}
        self.assertEqual(set(by_kind),{'GlobalNetworkPolicy','HostEndpoint','NetworkPolicy'})
        gnp=by_kind['GlobalNetworkPolicy']
        self.assertEqual(gnp['metadata']['name'],'custom-gnp')
        for key,value in dict(selector='has(role)',order=12.5,tier='security',applyOnForward=True,
                              performanceHints=['AssumeNeededOnEveryNode']).items():
            self.assertEqual(gnp['spec'][key],value)
        self.assertIn('19100',json.dumps(gnp))
        self.assertIn('fd00::/8',json.dumps(gnp))
        hep=by_kind['HostEndpoint']
        self.assertTrue(hep['metadata']['name'].startswith('custom-hep'))
        self.assertEqual(hep['spec']['interfaceName'],'eth0')
        self.assertEqual(hep['spec']['profiles'],['custom-profile'])
        self.assertIn('198.51.100.3',hep['spec']['expectedIPs'])
        np=by_kind['NetworkPolicy']
        self.assertEqual(np['metadata']['name'],'custom-netpol')
        self.assertEqual(np['metadata']['namespace'],'fixture-ns')
        self.assertEqual(np['spec']['podSelector']['matchLabels'],dict(app='demo',tier='backend'))

    def test_plan_all_manifest_fields_reach_real_yaml(self):
        result=self.cli('plan',*self.flags())
        self.assert_manifest(result.stdout)
        self.assertTrue(all('fixture-context' in c['argv'] for c in self.calls()))
        self.assertFalse(any('apply' in c['argv'] for c in self.calls()))

    def test_kubectl_crd_plan_and_apply_use_discovered_api_version(self):
        result=self.cli('plan',*self.flags(),executor='kubectl')
        docs=[d for d in yaml.safe_load_all(result.stdout) if d]
        for doc in docs:
            self.assertEqual(doc['apiVersion'], 'networking.k8s.io/v1' if doc['kind']=='NetworkPolicy'
                             else 'crd.projectcalico.org/v1')
        self.cli('apply',*self.flags(),'--confirm','--apply-staged',executor='kubectl')
        for call in self.calls():
            if 'apply' in call['argv']:
                for doc in yaml.safe_load_all(call['body']):
                    if doc['kind']!='NetworkPolicy':
                        self.assertEqual(doc['apiVersion'],'crd.projectcalico.org/v1')

    def test_kubectl_aggregated_api_keeps_v3_and_missing_resource_refuses(self):
        from unittest import mock
        with mock.patch.dict(os.environ,CALICO_FIXTURE_RESOURCES='globalnetworkpolicies.projectcalico.org\nhostendpoints.projectcalico.org'):
            result=self.cli('plan',*self.flags(),executor='kubectl')
            for doc in yaml.safe_load_all(result.stdout):
                if doc['kind']!='NetworkPolicy':
                    self.assertEqual(doc['apiVersion'],'projectcalico.org/v3')
        before=len(self.calls())
        with mock.patch.dict(os.environ,CALICO_FIXTURE_RESOURCES=''):
            result=self.cli('apply',*self.flags(),'--confirm',executor='kubectl',expected=1)
        self.assertIn('未注册 Calico 资源',result.stderr)
        self.assertFalse(any('apply' in c['argv'] for c in self.calls()[before:]))

    def test_plan_defaults_profile_omission_and_all_pods(self):
        result=self.cli('plan','--traffic-layer','host','--interface','*','-a','10.0.0.0/8')
        docs=list(yaml.safe_load_all(result.stdout))
        gnp=next(d for d in docs if d['kind']=='GlobalNetworkPolicy')
        hep=next(d for d in docs if d['kind']=='HostEndpoint')
        self.assertEqual(gnp['spec']['order'],500.0)
        self.assertIn('9100',json.dumps(gnp))
        self.assertEqual(hep['spec']['profiles'],['projectcalico-default-allow'])
        result=self.cli('plan','--traffic-layer','host','--interface','*','--no-default-allow-profile','-a','10.0.0.0/8')
        hep=next(d for d in yaml.safe_load_all(result.stdout) if d['kind']=='HostEndpoint')
        self.assertFalse(hep['spec'].get('profiles'))
        result=self.cli('plan','--traffic-layer','both','--no-hostendpoint','--policy-selector','all()',
                        '-a','10.0.0.0/8','--pod-selector-all','-n','fixture-ns')
        docs=list(yaml.safe_load_all(result.stdout))
        self.assertNotIn('HostEndpoint',[d['kind'] for d in docs])
        self.assertEqual(next(d for d in docs if d['kind']=='NetworkPolicy')['spec']['podSelector'],{'matchLabels': {}})

    def test_apply_staged_backup_and_post_verify_follow_real_command_order(self):
        result=self.cli('apply',*self.flags(),'--confirm','--backup','--backup-dir',str(self.base/'backups'),
                        '--apply-staged','--post-verify')
        calls=self.calls()
        writes=[c for c in calls if 'apply' in c['argv']]
        self.assertEqual(len(writes),3)
        self.assert_manifest('\n---\n'.join(c['body'] for c in writes))
        self.assertEqual([next(yaml.safe_load_all(c['body']))['kind'] for c in writes],
                         ['GlobalNetworkPolicy','HostEndpoint','NetworkPolicy'])
        self.assertGreater(len(list((self.base/'backups').iterdir())),0)
        self.assertIn('validate:',result.stderr)
        self.assertIn('pods',calls[-1]['argv'])

    def test_apply_kubectl_dry_run_modes_and_calico_refusal(self):
        for mode in ('client','server'):
            before=len(self.calls())
            self.cli('apply','--traffic-layer','pod','-n','fixture-ns','--pod-selector-all',
                     '-a','10.0.0.0/8','--dry-run',mode,executor='kubectl')
            writes=[c for c in self.calls()[before:] if 'apply' in c['argv']]
            self.assertEqual(len(writes),1)
            self.assertIn('--dry-run='+mode,writes[0]['argv'])
        before=len(self.calls())
        self.cli('apply','--traffic-layer','host','--interface','*','-a','10.0.0.0/8',
                 '--dry-run','server',expected=1)
        self.assertFalse(any('apply' in c['argv'] for c in self.calls()[before:]))
        self.cli('apply','--traffic-layer','pod','-n','fixture-ns','--pod-selector-all',
                 '-a','10.0.0.0/8',expected=1)

    def test_apply_profile_omission_and_no_hostendpoint_are_applied(self):
        self.cli('apply','--confirm','--traffic-layer','host','--interface','eth0',
                 '-a','10.0.0.0/8','--no-default-allow-profile')
        docs=[d for c in self.calls() if 'apply' in c['argv'] for d in yaml.safe_load_all(c['body']) if d]
        hep=next(d for d in docs if d['kind']=='HostEndpoint')
        self.assertFalse(hep['spec'].get('profiles'))
        before=len(self.calls())
        self.cli('apply','--confirm','--traffic-layer','both','--no-hostendpoint','--policy-selector','all()',
                 '-a','10.0.0.0/8','-n','fixture-ns','--pod-selector-all')
        docs=[d for c in self.calls()[before:] if 'apply' in c['argv'] for d in yaml.safe_load_all(c['body']) if d]
        self.assertEqual({d['kind'] for d in docs},{'GlobalNetworkPolicy','NetworkPolicy'})
        self.assertEqual(next(d for d in docs if d['kind']=='NetworkPolicy')['spec']['podSelector'],{'matchLabels':{}})

    def test_validate_host_profile_ip_pod_scope_and_profileless_variant(self):
        flags=['--traffic-layer','both','--interface','eth0','--include-external-ip','--hep-profile','custom-profile',
               '--port','19100','-a','10.0.0.0/8','-n','fixture-ns','--pod-label','app=demo','--k8s-np-name','custom-netpol']
        result=self.cli('validate',*flags)
        self.assertIn('198.51.100.3',result.stderr)
        self.assertIn('custom-netpol',result.stderr)
        self.assertIn('custom-profile',json.dumps(self.calls()))
        self.assertIn('app=demo',self.calls()[-1]['argv'])
        self.cli('validate',*flags,'--no-default-allow-profile')
        self.cli('validate','--traffic-layer','both','--no-hostendpoint','--pod-selector-all','-n','fixture-ns',
                 '-a','10.0.0.0/8',executor='kubectl')

    def test_delete_scope_names_aliases_and_documented_ignored_options(self):
        result=self.cli('delete','--traffic-layer','both','--policy-name','custom-gnp','--delete-hostendpoints',
                        '--port','19100','-n','fixture-ns','--k8s-np-name','custom-netpol',
                        '--pod-label','app=demo','--pod-selector-all','--interface','eth0','-a','10.0.0.0/8','--confirm')
        calls=self.calls()
        deletes=[c for c in calls if 'delete' in c['argv']]
        self.assertTrue(any('custom-gnp' in c['argv'] for c in deletes))
        self.assertTrue(any('custom-netpol' in c['argv'] and 'fixture-ns' in c['argv'] for c in deletes))
        self.assertIn('已忽略 --allow-net',result.stderr)
        self.assertIn('已忽略 --interface',result.stderr)
        self.assertFalse(any('10.0.0.0/8' in c['argv'] for c in deletes))

    def test_invalid_scope_selector_port_cidr_and_names_refuse_without_writes(self):
        for flags in (['--traffic-layer','host','-a','10.0.0.0/8'],
                      ['--traffic-layer','host','--no-hostendpoint','-a','10.0.0.0/8'],
                      ['--traffic-layer','pod','-n','fixture-ns','-a','10.0.0.0/8'],
                      ['--traffic-layer','pod','--pod-selector-all','-a','10.0.0.0/8'],
                      ['--traffic-layer','host','--interface','eth0','-a','invalid'],
                      ['--traffic-layer','pod','--pod-selector-all','-n','fixture-ns','-a','10.0.0.0/8','--port','65536']):
            self.cli('plan',*flags,expected=1)
        self.assertFalse(any('apply' in c['argv'] or 'delete' in c['argv'] for c in self.calls()))

    def test_readonly_preflight_nodes_and_log_modes(self):
        result=self.cli('nodes')
        self.assertIn('node=node-a internal_ip=10.1.2.3 external_ip=198.51.100.3',result.stderr)
        self.assertFalse(list(self.base.rglob('*.log')))
        result=self.cli('preflight',no_log=False,global_flags=['--verbose'])
        self.assertIn('cluster-info: ok',result.stderr)
        logs=list(self.base.rglob('*.log'))
        self.assertEqual(len(logs),1)
        self.assertIn('cluster-info: ok',logs[0].read_text())
        self.assertTrue(any('cluster-info' in c['argv'] for c in self.calls()))
        self.assertFalse(any('apply' in c['argv'] or 'delete' in c['argv'] for c in self.calls()))

    def test_verbose_sets_debug_level_on_actual_cli_dispatch(self):
        wrapper='''import logging,runpy,sys
def trace(frame,event,arg):
    if event=='call' and frame.f_code.co_name=='cmd_nodes':
        print('LOG_LEVEL='+str(logging.getLogger('calico_host_port_lockdown').level),file=sys.stderr)
    return trace
sys.settrace(trace)
sys.argv=sys.argv[1:]
runpy.run_path(sys.argv[0],run_name='__main__')
'''
        for flags,level in (([],20),(['--verbose'],10)):
            result=subprocess.run([sys.executable,'-B','-c',wrapper,str(TOOL),
                '--context','fixture-context','--kubectl',str(self.executor),
                '--no-log-file',*flags,'nodes'],cwd=self.base,
                env=dict(os.environ,CALLS=str(self.trace)),text=True,capture_output=True,timeout=15)
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertIn('LOG_LEVEL='+str(level),result.stderr)
            self.assertIn('node=node-a',result.stderr)

    def test_every_port_command_refuses_out_of_range_before_executor(self):
        for command in ('plan','validate','apply','delete'):
            for port in ('0','65536'):
                self.cli(command,'--traffic-layer','pod','-n','fixture-ns','--port',port,expected=1)
        self.assertEqual(self.calls(),[])


if __name__=='__main__':
    unittest.main()
