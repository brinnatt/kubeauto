"""Real Ansible variable/secret handoff; module action is an isolated fixture."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[2]
ACTION = '''from ansible.plugins.action import ActionBase
import json,os
from pathlib import Path
class ActionModule(ActionBase):
 def run(self,tmp=None,task_vars=None):
  values=self._task.args
  if values['configuration']['database']['password']!='private-original-password':
   return {'failed':True,'msg':'original configuration handoff differs'}
  if values['configuration']['data_volume']!=values['data_directory']:
   return {'failed':True,'msg':'data owner differs'}
  Path(os.environ['HARBOR_HANDOFF_TRACE']).write_text(json.dumps({
   'operation':values['operation'],'backup':values['backup_directory'],
   'ca':values['ca_file'],'packages':values['packages'],
   'transfer':values.get('transfer_directory')}))
  return {'changed':True,'marker':'HARBOR_ISOLATED_HANDOFF_PASS'}
'''


class HarborAnsibleEntryContracts(unittest.TestCase):
    def run_book(self, operation, *, members=1):
        with tempfile.TemporaryDirectory(prefix='harbor-handoff-') as temporary:
            root=Path(temporary);actions=root/'actions';actions.mkdir()
            (actions/'harbor_migration.py').write_text(ACTION)
            data=root/'data';(data/'harbor').mkdir(parents=True)
            config={'data_volume':str(data),'database':{'password':'private-original-password'}}
            (data/'harbor/harbor.yml').write_text(yaml.safe_dump(config))
            backup=root/'backup';backup.mkdir()
            (backup/'receipt.json').write_text(json.dumps({'original_configuration':config,
                'tls_paths':{'ca':'/fixture/original-ca.pem'}}))
            transfer=root/'transfer';transfer.mkdir()
            (transfer/'portable.json').write_text(json.dumps({'original_configuration':config,
                'tls_paths':{'ca':'/fixture/portable-ca.pem'}}))
            if operation=='import':(backup/'receipt.json').unlink()
            if operation != 'backup':
                # After migration the installed config has changed. Recovery
                # must bind the original checkpoint rather than this file.
                (data/'harbor/harbor.yml').write_text('database: {password: wrong-current-password}\n')
            variables={'HARBOR_PATH':str(data),'bin_dir':'/fixture/bin',
                'UPGRADE_HARBOR_OPERATION':operation,'UPGRADE_HARBOR_BACKUP':str(backup),
                'UPGRADE_HARBOR_TRANSFER':str(transfer) if operation=='import' else '',
                'UPGRADE_HARBOR_CA':'/fixture/backup-ca.pem','UPGRADE_HARBOR_PACKAGES':
                {'v2.16.0':'/fixture/harbor2160.tgz'}}
            (root/'vars.json').write_text(json.dumps(variables))
            (root/'hosts').write_text('[harbor]\n'+''.join(
                'harbor-'+str(i)+' ansible_connection=local ansible_python_interpreter='+sys.executable+'\n'
                for i in range(members)))
            trace=root/'trace.json'
            result=subprocess.run([str(Path(sys.executable).parent/'ansible-playbook'),
                '-i',str(root/'hosts'),str(ROOT/'upgrade/product/harbor.yml'),
                '-e','@'+str(root/'vars.json')],capture_output=True,text=True,timeout=30,
                env={**os.environ,'ANSIBLE_ACTION_PLUGINS':str(actions),
                     'ANSIBLE_LOCAL_TEMP':str(root/'ansible-local'),'HARBOR_HANDOFF_TRACE':str(trace)})
            self.assertNotIn('private-original-password',result.stdout+result.stderr)
            self.assertNotIn('wrong-current-password',result.stdout+result.stderr)
            return result,json.loads(trace.read_text()) if trace.exists() else None

    def test_backup_reads_original_config_and_trust_input_without_secret_output(self):
        result,trace=self.run_book('backup')
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertEqual(trace['operation'],'backup')
        self.assertEqual(trace['ca'],'/fixture/backup-ca.pem')
        self.assertIn('HARBOR_ISOLATED_HANDOFF_PASS',result.stdout)

    def test_upgrade_and_recovery_read_original_private_receipt(self):
        for operation in ['upgrade','restore','export','abort-import']:
            with self.subTest(operation=operation):
                result,trace=self.run_book(operation)
                self.assertEqual(result.returncode,0,result.stdout+result.stderr)
                self.assertEqual(trace['operation'],operation)
                self.assertEqual(trace['ca'],'/fixture/original-ca.pem')
                self.assertEqual(trace['packages'],{'v2.16.0':'/fixture/harbor2160.tgz'})

    def test_import_reads_transferred_manifest_without_current_install_or_target_receipt(self):
        result,trace=self.run_book('import')
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertEqual(trace['operation'],'import')
        self.assertEqual(trace['ca'],'/fixture/portable-ca.pem')
        self.assertTrue(trace['transfer'].endswith('/transfer'))

    def test_multiple_registry_owners_refuse_before_module_or_secret_reads(self):
        result,trace=self.run_book('backup',members=2)
        self.assertNotEqual(result.returncode,0)
        self.assertIsNone(trace)
        self.assertNotIn('Read the original installed configuration',result.stdout)


if __name__ == '__main__':
    unittest.main()
