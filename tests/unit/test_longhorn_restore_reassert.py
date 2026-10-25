"""SystemRestore must repair its target profile before the normal health wait."""
from contextlib import redirect_stdout
import copy
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT=Path(__file__).resolve().parents[2]
SOURCE=Path(os.environ.get('LONGHORN_PRODUCT_SOURCE',ROOT/'roles/longhorn/files/longhorn.py'))
SPEC=importlib.util.spec_from_file_location('restore_reassert_product',SOURCE)
MODULE=importlib.util.module_from_spec(SPEC);SPEC.loader.exec_module(MODULE)


class RestoreReassertion(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.values=Path(self.temp.name)/'values.json';self.values.write_text('{"target":"profile"}')
        self.request={'kind':'system-restore','name':'recover-one','source':'system-point'}
        self.restore=MODULE.request_object(self.request)
        self.restore['metadata'].update(uid='restore-uid',resourceVersion='7')
        self.restore['status']={'state':'Completed'}
        self.config={'longhorn_requests':[self.request],'longhorn_ready_timeout':120,
            'longhorn_operation_timeout':600,'longhorn_version':'1.12.1'}
        self.desired={'v1DataEngine':True,'v2DataEngine':True,'dataEngineCPUMask':{'v2':'0x1'},
            'defaultReplicaCount':{'v1':'1','v2':'1'},'storageOverProvisioningPercentage':100}
        self.settings={name:{'apiVersion':'longhorn.io/v1beta2','kind':'Setting','metadata':{
            'name':name,'namespace':'longhorn-system','uid':'uid-'+name,'resourceVersion':'3'},'value':value}
            for name,value in (('v1-data-engine','true'),('v2-data-engine','false'),
                ('data-engine-cpu-mask','{"v2":"0x3"}'),('default-replica-count','{"v2":"1","v1":"1"}'),
                ('storage-over-provisioning-percentage','100'))}
        self.volumes=[];self.events=[]
        self.api=Mock();self.api.get.side_effect=self.get;self.api.patch.side_effect=self.patch

    def get(self,kind,name='',namespace=''):
        if kind=='namespace':return {'metadata':{'uid':'target-namespace'}}
        if kind=='systemrestores.longhorn.io':return self.restore
        if kind=='volumes.longhorn.io':return {'items':self.volumes}
        if kind=='settings.longhorn.io':return self.settings.get(name)
        raise AssertionError((kind,name,namespace))

    def patch(self,kind,name,data,namespace):
        self.events.append(('patch',kind,name))
        if kind=='settings.longhorn.io':
            self.assertEqual(self.settings[name]['metadata']['uid'],data['metadata']['uid'])
            self.assertEqual('3',data['metadata']['resourceVersion']);self.settings[name]['value']=data['value']
        else:
            self.assertEqual('restore-uid',data['metadata']['uid']);self.assertEqual('7',data['metadata']['resourceVersion'])
            self.restore['metadata'].setdefault('annotations',{}).update(data['metadata']['annotations'])

    def invoke(self):
        def run(*args,**kwargs):self.events.append(('helm',args[0]));return ''
        def wait(label,timeout,check):
            self.events.append(('wait',label))
            if not check():raise TimeoutError('all volumes detached wait incomplete')
        with patch.object(MODULE,'chart_values',return_value={'defaultSettings':self.desired}),patch.object(MODULE,'run',side_effect=run),patch.object(MODULE,'wait',side_effect=wait),patch.object(MODULE,'check_block_identity'),patch.object(MODULE,'reconcile_filesystem_disks',return_value=True),patch.object(MODULE,'reconcile_block_disks',return_value=True),patch.object(MODULE,'healthy',return_value=True),redirect_stdout(io.StringIO()):
            MODULE.reassert_completed_restores(self.config,self.api,'helm',self.values,{'node':'identity'})

    def test_completed_restore_reasserts_exact_managed_settings_then_helm_health_and_uid_checkpoint(self):
        self.invoke()
        self.assertEqual('true',self.settings['v2-data-engine']['value'])
        self.assertEqual({'v2':'0x1'},json.loads(self.settings['data-engine-cpu-mask']['value']))
        patched=[e[2] for e in self.events if e[:2]==('patch','settings.longhorn.io')]
        self.assertEqual(['v2-data-engine','data-engine-cpu-mask'],patched)
        self.assertLess(next(i for i,e in enumerate(self.events) if e[0]=='helm'),next(i for i,e in enumerate(self.events) if e==('wait','system-restore-target-profile-ready')))
        self.assertEqual(('patch','systemrestores.longhorn.io','recover-one'),self.events[-1])
        self.assertIn(MODULE.RESTORE_REASSERTION,self.restore['metadata']['annotations'])

    def test_same_completed_uid_and_values_are_noop_but_values_or_uid_change_replays(self):
        self.invoke();self.events.clear();self.api.reset_mock();self.invoke()
        self.assertFalse(self.events);self.api.patch.assert_not_called()
        self.values.write_text('{"target":"changed"}');self.invoke();self.assertTrue(self.events)
        fingerprint=self.restore['metadata']['annotations'][MODULE.RESTORE_REASSERTION]
        self.restore['metadata']['uid']='new-uid'
        pending=MODULE.completed_restore_reassertions(self.config,self.api,self.values)
        self.assertEqual(1,len(pending));self.assertNotEqual(fingerprint,pending[0][2])

    def test_no_requested_restore_does_not_read_live_resources_or_touch_helm(self):
        self.config['longhorn_requests']=[];self.invoke()
        self.api.get.assert_not_called();self.api.patch.assert_not_called();self.assertFalse(self.events)

    def test_nonterminal_request_is_left_to_native_operation(self):
        for state in ('Pending','InProgress','Error'):
            self.restore['status']['state']=state;self.invoke()
            self.api.patch.assert_not_called();self.assertFalse(self.events)

    def test_foreign_or_changed_request_and_error_terminal_cannot_authorize_mutation(self):
        original=copy.deepcopy(self.restore)
        for defect in ('owner','spec','uid','deleting','error','condition'):
            with self.subTest(defect=defect):
                self.restore=copy.deepcopy(original)
                if defect=='owner':self.restore['metadata']['labels']={}
                elif defect=='spec':self.restore['spec']['systemBackup']='foreign'
                elif defect=='uid':self.restore['metadata'].pop('uid')
                elif defect=='deleting':self.restore['metadata']['deletionTimestamp']='now'
                elif defect=='error':self.restore['status']['error']='native error'
                else:self.restore['status']['conditions']=[{'type':'Error','status':'True'}]
                with self.assertRaises(ValueError):self.invoke()
                self.api.patch.assert_not_called();self.assertFalse(self.events)

    def test_attached_or_node_intent_refuses_before_first_mutation(self):
        for spec,status in (({},'attached'),({'nodeID':'node'},'detached')):
            self.volumes=[{'spec':spec,'status':{'state':status}}]
            with self.assertRaisesRegex(TimeoutError,'all volumes detached'):self.invoke()
            self.api.patch.assert_not_called();self.assertFalse(any(e[0] in ('helm','patch') for e in self.events))

    def test_native_restore_attachment_may_finish_after_completed_without_early_mutation(self):
        self.volumes=[{'spec':{'nodeID':'auto-restore-node'},'status':{'state':'attached'}}]
        def wait(label,timeout,check):
            if label=='system-restore-all-volumes-detached':
                self.assertEqual(600,timeout);self.assertFalse(check());self.api.patch.assert_not_called()
                self.volumes[0].update(spec={},status={'state':'detached'})
            self.assertTrue(check())
        with patch.object(MODULE,'chart_values',return_value={'defaultSettings':self.desired}),patch.object(MODULE,'run'),patch.object(MODULE,'wait',side_effect=wait),patch.object(MODULE,'check_block_identity'),patch.object(MODULE,'reconcile_filesystem_disks',return_value=True),patch.object(MODULE,'reconcile_block_disks',return_value=True),patch.object(MODULE,'healthy',return_value=True),redirect_stdout(io.StringIO()):
            MODULE.reassert_completed_restores(self.config,self.api,'helm',self.values,{})
        self.assertIn(MODULE.RESTORE_REASSERTION,self.restore['metadata']['annotations'])

    def test_all_managed_setting_identities_are_checked_before_first_write(self):
        self.settings['storage-over-provisioning-percentage']['metadata'].pop('uid')
        with self.assertRaisesRegex(ValueError,'identity missing'):self.invoke()
        self.api.patch.assert_not_called();self.assertFalse(any(e[0] in ('helm','patch') for e in self.events))

    def test_helm_or_health_failure_never_writes_completion_and_retry_replays(self):
        for fault in ('helm','health'):
            with self.subTest(fault=fault),patch.object(MODULE,'chart_values',return_value={'defaultSettings':self.desired}),patch.object(MODULE,'run',side_effect=RuntimeError('helm failed') if fault=='helm' else None),patch.object(MODULE,'wait',side_effect=RuntimeError('health failed') if fault=='health' else None),redirect_stdout(io.StringIO()):
                with self.assertRaisesRegex(RuntimeError,'failed'):MODULE.reassert_completed_restores(self.config,self.api,'helm',self.values,{})
                self.assertNotIn(MODULE.RESTORE_REASSERTION,self.restore['metadata']['annotations'])
        self.invoke();self.assertIn(MODULE.RESTORE_REASSERTION,self.restore['metadata']['annotations'])

    def test_reconcile_repairs_completed_restore_before_normal_health_and_after_new_operation(self):
        import ast
        tree=ast.parse(SOURCE.read_text());function=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='reconcile')
        calls=[n for n in ast.walk(function) if isinstance(n,ast.Call) and isinstance(n.func,ast.Name)]
        repairs=sorted(n.lineno for n in calls if n.func.id=='reassert_completed_restores')
        ordinary=min(n.lineno for n in calls if n.func.id=='wait' and n.args and isinstance(n.args[0],ast.Constant) and n.args[0].value=='control-and-disks-ready')
        operation=next(n.lineno for n in calls if n.func.id=='operation')
        self.assertEqual(2,len(repairs));self.assertLess(repairs[0],ordinary);self.assertGreater(repairs[1],operation)


if __name__=='__main__':unittest.main()
