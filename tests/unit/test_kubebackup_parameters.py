"""Exercise CLI parsing, real backups/restores and files; replace only the API."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest
from unittest import mock

ROOT=Path(__file__).resolve().parents[2]


class KubeBackupParameters(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='kubebackup-parameters-')
        self.addCleanup(self.tmp.cleanup)
        self.base=Path(self.tmp.name)
        with mock.patch.dict(os.environ,KUBEAUTO_LOG_DIR=self.tmp.name):
            spec=importlib.util.spec_from_file_location('kubebackup_parameters',ROOT/'tools/k8stools/KubeBackupCli.py')
            self.m=importlib.util.module_from_spec(spec);sys.modules[spec.name]=self.m
            spec.loader.exec_module(self.m)
        self.kubeconfig=self.base/'kubeconfig';self.kubeconfig.write_text('{}')

    def invoke(self, *flags, api=None, expected=0):
        m=self.m
        with mock.patch.object(sys,'argv',['KubeBackupCli',*map(str,flags)]), \
                mock.patch.object(m,'KubernetesClient',return_value=api) as factory, \
                contextlib.redirect_stdout(io.StringIO()):
            try:
                m.main(); rc=0
            except SystemExit as error:
                rc=error.code
        self.assertEqual(rc,expected)
        return factory

    def api(self):
        api=mock.Mock()
        api.list_namespaces.return_value=['fixture-ns','other-ns']
        api.is_cluster_scoped.return_value=False
        api.api_client.call_api.return_value=({'gitVersion':'v1.36.6','major':'1','minor':'36'},)
        api.version_api.get_code.return_value={'gitVersion':'v1.36.6','major':'1','minor':'36'}
        api.list_resources.side_effect=lambda resource,ns,*selectors: [dict(apiVersion='v1',kind='ConfigMap',
            metadata=dict(name=name,namespace=ns),data=dict(key='value')) for name in ('keep','omit')]
        return api

    def test_backup_filters_selectors_workers_metadata_archive_and_dry_run(self):
        m=self.m;api=self.api(); output=self.base/'output'
        with mock.patch.object(m,'ThreadPoolExecutor',wraps=m.ThreadPoolExecutor) as pool:
            factory=self.invoke('backup','-n','fixture-ns','-r','configmaps','-o',output,
                '--backup-name','fixture','--max-workers','2','--label-selector','app=demo',
                '--field-selector','metadata.name=keep','--include-names','keep','--tar',
                '--kubeconfig',self.kubeconfig,'--context','fixture-context','--debug',api=api)
        factory.assert_called_once_with(str(self.kubeconfig),'fixture-context')
        api.list_resources.assert_called_once_with('configmaps','fixture-ns','app=demo','metadata.name=keep')
        pool.assert_called_once_with(max_workers=2)
        files=list(output.rglob('*.yaml'))
        self.assertEqual(len(files),1)
        self.assertIn('keep',files[0].read_text())
        metadata=json.loads((output/'fixture/backup-metadata.json').read_text())
        self.assertEqual(metadata['include_names'],['keep'])
        self.assertEqual(metadata['field_selector'],'metadata.name=keep')
        archive=next(output.glob('*.tar.gz'))
        with tarfile.open(archive) as bundle:
            self.assertTrue(any(name.endswith('.yaml') for name in bundle.getnames()))
        api=self.api(); before=set(output.rglob('*'))
        self.invoke('backup','--all-namespaces','--resources','configmaps','--output-dir',output,
            '--backup-name','dry','--exclude-names','omit','--dry-run','--tar',api=api)
        self.assertEqual(set(output.rglob('*')),before)
        self.assertEqual({call.args[1] for call in api.list_resources.call_args_list},{'fixture-ns','other-ns'})

    def restore_fixture(self):
        root=self.base/'restore';root.mkdir()
        (root/'backup-metadata.json').write_text('{}')
        for folder,kind,namespace in (('old-ns/ConfigMap','ConfigMap','old-ns'),
                ('cluster-scoped/customresourcedefinition','CustomResourceDefinition',None),
                ('cluster-scoped/ClusterRole','ClusterRole',None)):
            p=root/folder;p.mkdir(parents=True)
            obj=dict(apiVersion='v1',kind=kind,metadata=dict(name='fixture'))
            if namespace: obj['metadata']['namespace']=namespace
            (p/'fixture.yaml').write_text(json.dumps(obj))
        return root

    def test_restore_scope_skip_create_namespace_dry_run_and_serial_workers(self):
        root=self.restore_fixture()
        for flags,kinds in (([],{'ConfigMap','CustomResourceDefinition','ClusterRole'}),
                (['--skip-crds'],{'ConfigMap','ClusterRole'}),
                (['--skip-cluster-scoped'],{'ConfigMap'}),(['--dry-run'],set())):
            api=self.api();api.list_namespaces.return_value=[]
            self.invoke('restore','--backup-dir',root,'--namespace-mapping','old-ns=new-ns',
                '--create-namespaces','--max-workers','1','--backup-name','fixture',
                '--kubeconfig',self.kubeconfig,'--context','restore-context','--debug',*flags,api=api).assert_called_once_with(
                    str(self.kubeconfig),'restore-context')
            self.assertEqual({c.args[0]['kind'] for c in api.apply_resource.call_args_list},kinds)
            if '--dry-run' in flags:
                api.apply_namespace.assert_not_called()
            else:
                api.apply_namespace.assert_called_once_with('new-ns')
                obj=next(c.args[0] for c in api.apply_resource.call_args_list if c.args[0]['kind']=='ConfigMap')
                self.assertEqual(obj['metadata']['namespace'],'new-ns')

    def test_invalid_backup_options_refuse_before_api(self):
        for flags in (['--max-workers','0'],['--max-workers','257'],['--context','x'*(self.m.MAX_CONTEXT_LEN+1)],
                ['--label-selector','bad\x00selector'],['--field-selector','bad\x00selector'],
                ['--include-names','keep','--exclude-names','omit'],['--backup-name','../escape']):
            factory=self.invoke('backup','-n','fixture-ns','--output-dir',self.base/'output',*flags,expected=1)
            factory.assert_not_called()

    def test_restore_namespace_text_modes_and_merge_patch_have_readback(self):
        from tests.unit.test_kube_backup_cli import KubeBackupCliTests
        fixture=KubeBackupCliTests()
        for mode,expected in (('off','app.old-ns'),('auto','app.new-ns'),('all','app.new-ns')):
            rc,api=fixture.run_restore_cli('--env-namespace-substitute',mode)
            self.assertEqual(rc,0)
            obj=api.return_value.apply_resource.call_args.args[0]
            self.assertEqual(obj['metadata']['namespace'],'new-ns')
            spec=obj['spec']['template']['spec']
            self.assertNotIn('nodeSelector',spec)
            env={x['name']:x.get('value') for x in spec['containers'][0]['env']}
            self.assertEqual(env['DOMAIN_NAME'],expected)


if __name__=='__main__':
    unittest.main()
