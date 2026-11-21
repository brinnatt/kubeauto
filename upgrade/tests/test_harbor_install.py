"""Real Harbor role conditions with read-only external-operation fixtures."""
import json
import hashlib
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from jinja2 import Environment, StrictUndefined
import yaml
from upgrade.tests.test_control_plane import NO_CONNECTION

ROOT = Path(__file__).resolve().parents[2]
ACTION = '''from ansible.plugins.action import ActionBase
import base64,json,os
from pathlib import Path
class ActionModule(ActionBase):
 def run(self,tmp=None,task_vars=None):
  module=self._task.args['module'];values=self._task.args['values']
  with Path(os.environ['HARBOR_TEST_TRACE']).open('a') as out:
   out.write(json.dumps({'name':self._task.get_name(),'module':module})+'\\n')
  if module=='stat':
   if values['path'].endswith('.tgz'):
    digest={'x86_64':'643c137d8e829c857ef0ba45c46b5d6d8553077340bbd4929ded7e92808e77ff',
     'aarch64':'6525af9c26cc24e48ecf37d3f7d9d139895b39a037a3de697f995171ce65524a'}[task_vars['ansible_architecture']]
    return {'changed':False,'stat':{'isreg':True,'islnk':False,
     'checksum':digest if not task_vars['fixture_corrupt'] else '0'*64}}
   return {'changed':False,'stat':{'exists':task_vars['fixture_existing']}}
  if module=='command':
   return {'changed':False,'rc':0,'stdout':'registry' if task_vars['fixture_running'] else ''}
  if module=='uri':
   if values['validate_certs'] is not True or values['use_proxy'] is not False:
    return {'failed':True,'msg':'unmodelled TLS bypass'}
   if values['url'].endswith('/systeminfo'):
    authenticated=(values.get('url_username')=='admin' and values.get('url_password')=='private-fixture-password' and values.get('force_basic_auth') is True)
    return {'changed':False,'status':200,'json':{'harbor_version':task_vars['fixture_version']} if authenticated else {}}
   return {'changed':False,'status':200,'json':{'status':task_vars['fixture_health']}}
  if module=='slurp':
   return {'changed':False,'content':base64.b64encode(b'harbor_admin_password: private-fixture-password\\n').decode()}
  if module=='shell' and self._task.get_name()=='Ensure Harbor archive tools on a minimal host':
   if task_vars['fixture_archive_failure']:return {'failed':True,'rc':1,'msg':'archive dependency installation failed'}
   Path(os.environ['HARBOR_TEST_TRACE']+'.archive-ready').touch()
   return {'changed':True,'rc':0,'stdout':'HARBOR_ARCHIVE_TOOLS_INSTALLED'}
  if module=='shell' and self._task.get_name()=='uncompress harbor offline package':
   if not Path(os.environ['HARBOR_TEST_TRACE']+'.archive-ready').exists():
    return {'failed':True,'rc':127,'msg':'tar: command not found'}
  if module in ['file','shell','copy','template','wait_for']:
   return {'changed':True,'rc':0,'stdout':'private-fixture-password'}
  return {'failed':True,'msg':'unmodelled external operation'}
'''


class HarborInstallationContracts(unittest.TestCase):
    def test_unsupported_prepare_data_paths_refuse_before_any_remote_operation(self):
        for path in ['/srv/harbor data','/srv/"harbor"','/srv/../data','/']:
            with self.subTest(path=path):
                result,trace=self.run_role(data_path=path)
                self.assertNotEqual(result.returncode,0)
                self.assertIn('official Harbor prepare script requires',result.stdout)
                self.assertEqual(trace,[])

    def test_real_official_prepare_accepts_rendered_volume_and_disproves_quoted_scalar(self):
        fixture=ROOT/'upgrade/tests/fixtures/harbor216-prepare.sh.txt'
        self.assertEqual(hashlib.sha256(fixture.read_bytes()).hexdigest(),
                         '8730100efcea0ca3ee5a210a7dd9947633b99ec1122671217ea26093eda91ae2')
        with tempfile.TemporaryDirectory(prefix='harbor-prepare-') as temporary:
            root=Path(temporary);bundle=root/'harbor';bundle.mkdir();commands=root/'bin';commands.mkdir()
            script=root/'prepare'
            # Isolate only legacy fixed /data lookups. The official parser and
            # actual Docker argv construction remain byte-for-byte unchanged.
            script.write_text(fixture.read_text().replace('previous_secretkey_path=/data/secretkey',
                'previous_secretkey_path='+str(root/'absent-secretkey')).replace(
                'previous_defaultalias_path=/data/defaultalias',
                'previous_defaultalias_path='+str(root/'absent-defaultalias')))
            docker=commands/'docker'
            docker.write_text('#!'+sys.executable+'\nimport json,os,sys\n'
                'from pathlib import Path\n'
                'args=sys.argv[1:]\n'
                'Path(os.environ["PREPARE_ARGV"]).write_text(json.dumps(args))\n'
                'expected=os.environ["PREPARE_DATA"]+":/data"\n'
                'raise SystemExit(0 if expected in args else 17)\n')
            docker.chmod(0o700)
            environment=Environment(undefined=StrictUndefined);environment.filters['to_json']=json.dumps
            variables={'HARBOR_HOSTNAME':'registry.example.invalid','HARBOR_HTTP_PORT':80,
                'HARBOR_TLS_PORT':8443,'HARBOR_PATH':str(root/'persistent-data'),'ca_dir':str(root/'ssl'),
                'harbor_password_gen':{'stdout':'secret: # "value"'},
                'db_password_gen':{'stdout':'db-secret: # "value"'}}
            template=(ROOT/'roles/harbor/templates/harbor-v2.16.yml.j2').read_text()
            rendered=environment.from_string(template).render(**variables)
            env={**os.environ,'PATH':str(commands)+os.pathsep+os.environ['PATH'],
                'HARBOR_BUNDLE_DIR':str(bundle),'PREPARE_ARGV':str(root/'argv.json'),
                'PREPARE_DATA':variables['HARBOR_PATH']}
            for quoted in [True,False]:
                content=rendered.replace('data_volume: '+variables['HARBOR_PATH'],
                    'data_volume: '+json.dumps(variables['HARBOR_PATH'])) if quoted else rendered
                (bundle/'harbor.yml').write_text(content)
                result=subprocess.run(['bash',str(script)],cwd=root,env=env,
                                      capture_output=True,text=True,timeout=5)
                self.assertEqual(result.returncode,17 if quoted else 0,result.stdout+result.stderr)
            args=json.loads((root/'argv.json').read_text())
            self.assertIn('goharbor/prepare:v2.16.0',args)
            self.assertIn(variables['HARBOR_PATH']+':/data',args)
            self.assertFalse((bundle/'input').exists())

    def run_role(self, *, existing=False, running=False, corrupt=False,
                 arch='x86_64', version='v2.16.0-abcdef', health='healthy',data_path='/fixture/data',
                 archive_failure=False,os_family='RedHat'):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root/'actions').mkdir(); (root/'connections').mkdir()
            (root/'actions/runtime_record.py').write_text(ACTION)
            (root/'connections/upgrade_test_forbidden.py').write_text(NO_CONNECTION)
            tasks = yaml.safe_load((ROOT/'roles/harbor/tasks/main.yml').read_text())
            def adapt(tasks):
                for task in tasks:
                    for key in list(task):
                        module = key.removeprefix('ansible.builtin.')
                        if module in ['file','command','shell','copy','template','stat','uri','wait_for','slurp']:
                            values = task.pop(key)
                            if isinstance(values,str): values={'_raw_params':values}
                            task['runtime_record']={'module':module,'values':values}
                    if 'until' in task:
                        # Ansible retries=0 disables the until assertion itself.
                        # Keep it active; remove only the fixture wait interval.
                        task['retries']=1
                        task['delay']=0
                    for nested in ['block','rescue','always']:
                        if nested in task: adapt(task[nested])
            adapt(tasks)
            variables = {'HARBOR_PATH':data_path,'HARBOR_VER':'v2.16.0',
                'HARBOR_VER_MAIN':'v2.16','HARBOR_HOSTNAME':'registry.example.invalid',
                'HARBOR_SELF_SIGNED_CERT':True,'HARBOR_WITH_TRIVY':False,'HARBOR_INST_OPS':'',
                'HARBOR_TLS_PORT':8443,'bin_dir':'/fixture/bin','base_dir':'/fixture/product',
                'cluster_dir':'/fixture/cluster','ca_dir':'/fixture/ssl',
                'ansible_architecture':arch,'fixture_existing':existing,'fixture_running':running,
                'fixture_corrupt':corrupt,'fixture_version':version,'fixture_health':health,
                'fixture_archive_failure':archive_failure,'ansible_os_family':os_family}
            (root/'vars.json').write_text(json.dumps(variables))
            (root/'book.yml').write_text(yaml.safe_dump([{'hosts':'harbor','gather_facts':False,
                'any_errors_fatal':True,'tasks':tasks}]))
            (root/'hosts').write_text('[harbor]\nregistry-one ansible_connection=upgrade_test_forbidden\n')
            result = subprocess.run([str(Path(sys.executable).parent/'ansible-playbook'),
                '-i',str(root/'hosts'),str(root/'book.yml'),'-e','@'+str(root/'vars.json')],
                capture_output=True,text=True,timeout=20,env={**os.environ,
                    'HARBOR_TEST_TRACE':str(root/'trace.jsonl'),
                    'ANSIBLE_ACTION_PLUGINS':str(root/'actions'),
                    'ANSIBLE_CONNECTION_PLUGINS':str(root/'connections'),
                    'ANSIBLE_LOCAL_TEMP':str(root/'local')})
            self.assertNotIn('TEST_REMOTE_CONNECTION_REFUSED',result.stdout+result.stderr)
            self.assertNotIn('unmodelled',result.stdout+result.stderr)
            self.assertNotIn('private-fixture-password',result.stdout+result.stderr)
            trace=[json.loads(line) for line in (root/'trace.jsonl').read_text().splitlines()] if (root/'trace.jsonl').exists() else []
            return result,trace

    def test_clean_dual_architecture_install_checks_hash_before_extract_and_tls_api_afterward(self):
        for arch in ['x86_64','aarch64']:
            with self.subTest(arch=arch):
                result,trace=self.run_role(arch=arch)
                self.assertEqual(result.returncode,0,result.stdout+result.stderr)
                names=[row['name'] for row in trace]
                self.assertLess(names.index('Hash the official installer before extraction'),
                    names.index('uncompress harbor offline package'))
                self.assertLess(names.index('install harbor'),names.index('Require the target Harbor API and complete component health'))

    def test_archive_dependency_failure_stops_before_credentials_or_bundle_mutation(self):
        result,trace=self.run_role(archive_failure=True)
        self.assertNotEqual(result.returncode,0)
        names=[r['name'] for r in trace]
        self.assertIn('Ensure Harbor archive tools on a minimal host',names)
        for name in ['Generate random password for Harbor','distribute harbor offline package','uncompress harbor offline package']:
            self.assertNotIn(name,names)

    def test_archive_tool_install_command_renders_for_supported_os_families(self):
        for family in ['Debian','Suse']:
            result,_=self.run_role(os_family=family)
            self.assertEqual(result.returncode,0,result.stdout+result.stderr)

    def test_stopped_existing_instance_refuses_before_password_or_config_changes(self):
        result,trace=self.run_role(existing=True)
        self.assertNotEqual(result.returncode,0)
        self.assertIn('Existing Harbor state requires',result.stdout)
        self.assertFalse([row for row in trace if row['module'] in ['shell','copy','template']])

    def test_bad_archive_refuses_before_extraction(self):
        result,trace=self.run_role(corrupt=True)
        self.assertNotEqual(result.returncode,0)
        self.assertNotIn('uncompress harbor offline package',[row['name'] for row in trace])

    def test_existing_target_is_read_only_and_wrong_version_or_health_never_passes(self):
        for version,health,expected in [('v2.16.0-abcdef','healthy',True),
                ('v2.13.0','healthy',False),('v2.16.0','unhealthy',False)]:
            result,trace=self.run_role(existing=True,running=True,version=version,health=health)
            self.assertEqual(result.returncode==0,expected,result.stdout+result.stderr)
            self.assertFalse([row for row in trace if row['module'] in ['shell','copy','template']])

    def test_216_template_preserves_official_defaults_and_yaml_escapes_customer_values(self):
        environment=Environment(undefined=StrictUndefined)
        environment.filters['to_json']=json.dumps
        variables={'HARBOR_HOSTNAME':'registry.example.invalid','HARBOR_HTTP_PORT':80,
            'HARBOR_TLS_PORT':8443,'HARBOR_PATH':'/srv/harbor-data','ca_dir':'/fixture/ssl',
            'harbor_password_gen':{'stdout':'password: # "value"'},
            'db_password_gen':{'stdout':'different: # "value"'}}
        rendered=environment.from_string((ROOT/'roles/harbor/templates/harbor-v2.16.yml.j2').read_text()).render(**variables)
        data=yaml.safe_load(rendered)
        self.assertEqual(data['_version'],'2.16.0')
        self.assertEqual(data['harbor_admin_password'],variables['harbor_password_gen']['stdout'])
        self.assertEqual(data['database']['password'],variables['db_password_gen']['stdout'])
        self.assertEqual(data['data_volume'],variables['HARBOR_PATH'])
        self.assertEqual(data['https']['port'],8443)
        self.assertEqual(data['jobservice']['max_job_duration_hours'],24)
