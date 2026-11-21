"""Owned handoff, real CLI scope and refusal/cleanup contracts; no lab/network."""
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch,call

import yaml

from upgrade.lab import ingress_qualification as lab


class IngressQualificationContracts(unittest.TestCase):
    def setUp(self):
        t=tempfile.TemporaryDirectory();self.addCleanup(t.cleanup);self.root=Path(t.name)
        self.evidence=self.root/'runs/v1366-fixture';self.evidence.mkdir(parents=True)
        self.session=SimpleNamespace(run_id='v1366-fixture',evidence=self.evidence,
            select=Mock(),host_operation=Mock(),target=self.root,cluster='owned-target',cli=Mock(),kubectl=Mock(),command=Mock())
        self.scope={'enabled':True,'runner':'upgrade/lab/ingress_qualification.py','phase':'new-install',
            'images':['brinnatt/ingress-nginx-controller:v1.15.1','brinnatt/kube-webhook-certgen:v1.6.9']}
        (self.root/'acceptance-plan.yaml').write_text(yaml.safe_dump({'ingress_qualification':self.scope}))
        (self.root/'evidence').mkdir()
        lab.write(self.root/'evidence/ingress-focused-contracts.json',{
            'passed':True,'inputs':{'fixture':'fixture'},'references':[{'path':'fixture','sha256':'fixture'}]})
        hashes=patch.object(lab,'check_hash');hashes.start();self.addCleanup(hashes.stop)
        lab.write(self.root/'owner.json',{'run_id':self.session.run_id,'mode':'qualify'})
        lab.write(self.evidence/'new-install.status.json',{'rc':0,'run_id':self.session.run_id,'attempt':1})
        for suffix in ['exit','finalized']:(self.evidence/('new-install.attempt-1.'+suffix)).write_text('0\n')
        (self.root/'v1366-fixture.log').write_text('NEXT_SESSION_WAIT phase=new-install failed=false lease=held\n')
        config=self.root/'config.yml';config.write_text('frozen config')
        lab.write(self.evidence/'target-config.json',{'path':str(config),'sha256':lab.sha(config)})
        self.state=patch.object(lab,'STATE',self.root);self.state.start();self.addCleanup(self.state.stop)
        self.matrix=patch.object(lab,'MATRIX_PATH',self.root/'test-matrix.yaml');self.matrix.start();self.addCleanup(self.matrix.stop)

    def test_healthy_handoff_binds_exact_config_before_host_observation(self):
        self.assertEqual(lab.boundary(self.session)['sha256'],lab.sha(self.root/'config.yml'))
        self.session.select.assert_called_once_with(target=True)
        self.session.host_operation.assert_called_once_with('marked')

    def test_foreign_owner_failed_or_nondurable_install_and_cleanup_refuse_before_host_operation(self):
        cases=[('owner.json',{'run_id':'foreign','mode':'qualify'}),
            ('runs/v1366-fixture/new-install.status.json',{'run_id':'v1366-fixture','rc':1,'attempt':1}),
            ('runs/v1366-fixture/new-install.attempt-1.exit','1\n'),
            ('runs/v1366-fixture/target-clean.attempt-1.log','started'),
            ('config.yml','drift'),('v1366-fixture.log','NEXT_SESSION_WAIT phase=target-clean failed=false lease=held\n')]
        for relative,value in cases:
            with self.subTest(relative=relative):
                p=self.root/relative;original=p.read_bytes() if p.exists() else None
                p.write_text(json.dumps(value) if isinstance(value,dict) else value)
                with self.assertRaises(ValueError):lab.boundary(self.session)
                self.session.host_operation.assert_not_called()
                if original is None:p.unlink()
                else:p.write_bytes(original)

    def test_disabled_scope_refuses_before_any_host_check(self):
        self.scope['enabled']=False
        (self.root/'acceptance-plan.yaml').write_text(yaml.safe_dump({'ingress_qualification':self.scope}))
        with self.assertRaisesRegex(ValueError,'scope'):lab.boundary(self.session)
        self.session.host_operation.assert_not_called()

    def test_final_owner_uses_same_healthy_durable_handoff_without_qualification_wait(self):
        lab.write(self.root/'owner.json',{'run_id':self.session.run_id,'mode':'final'})
        (self.root/(self.session.run_id+'.log')).write_text('final is running without manual handoffs\n')
        lab.boundary(self.session)
        self.session.host_operation.assert_called_once_with('marked')
        self.session.host_operation.reset_mock()
        (self.evidence/'new-install.attempt-1.finalized').write_text('1\n')
        with self.assertRaisesRegex(ValueError,'durable'):lab.boundary(self.session)
        self.session.host_operation.assert_not_called()

    def configuration(self):
        path=self.root/'clusters/owned-target/config.yml';path.parent.mkdir(parents=True)
        original=b'# preserve exact original formatting\ningress_nginx_install: "no"\ningress_nginx_metrics_enabled: true\nother: retained\n'
        path.write_bytes(original)
        return path,original

    def test_product_install_uses_config_and_restores_exact_bytes(self):
        path,original=self.configuration()
        def product(*args):
            configuration=yaml.safe_load(path.read_bytes())
            self.assertEqual(configuration['ingress_nginx_install'],'yes')
            self.assertFalse(configuration['ingress_nginx_metrics_enabled'])
            self.assertEqual(configuration['other'],'retained')
        self.session.cli.side_effect=product
        probe=lab.Probe(self.session);probe.install()
        args=self.session.cli.call_args.args
        self.assertEqual(args,(self.root,self.root,'setup','owned-target','07'))
        self.assertEqual(path.read_bytes(),original)
        self.assertEqual((probe.directory/'frozen-config.yml').read_bytes(),original)
        self.assertEqual((self.root/'config.yml').read_text(),'frozen config')

    def test_failed_setup_restores_configuration_before_cleanup_or_retry(self):
        path,original=self.configuration()
        self.session.cli.side_effect=RuntimeError('first product failure')
        probe=lab.Probe(self.session)
        with self.assertRaisesRegex(RuntimeError,'first product failure'):probe.install()
        self.assertEqual(path.read_bytes(),original)

    def test_foreign_config_change_is_not_overwritten_by_restore(self):
        path,original=self.configuration()
        self.session.cli.side_effect=lambda *args:path.write_text('foreign change')
        probe=lab.Probe(self.session)
        with self.assertRaisesRegex(ValueError,'outside the owned'):probe.install()
        self.assertEqual(path.read_text(),'foreign change')
        self.assertEqual((probe.directory/'frozen-config.yml').read_bytes(),original)

    def test_actual_setup_handoff_delivers_overlay_to_real_ansible(self):
        import ansible_runner
        from service.cluster.manager import ClusterManager
        path,original=self.configuration()
        playbook=self.root/'contract.yml'
        playbook.write_text('''- hosts: localhost
  gather_facts: false
  tasks:
    - ansible.builtin.assert:
        that:
          - ingress_nginx_install == 'yes'
          - ingress_nginx_metrics_enabled == false
          - ingress_nginx_namespace == 'ingress-nginx'
          - other == 'retained'
''')
        manager=object.__new__(ClusterManager);manager.clusters_dir=self.root/'clusters'
        manager._validate_for_setup=Mock();manager._show_component_versions=Mock()
        (self.root/'ansible').mkdir()
        def play(cluster,name,**kwargs):
            self.assertEqual(kwargs['cmdline'],'')
            result=ansible_runner.run(private_data_dir=str(self.root/'ansible'),
                playbook=str(playbook),inventory='localhost,',
                extravars=manager._yaml_to_dict(path),quiet=True,
                envvars={'PATH':str(lab.ROOT/'.venv/bin')+':'+os.environ.get('PATH','')})
            self.assertEqual(result.rc,0,result.stdout.read())
        manager._run_playbook=play
        self.session.cli.side_effect=lambda base,code,command,cluster,stage:manager.setup_cluster(cluster,stage)
        with patch('service.cluster.manager.confirm_action',return_value=True):
            lab.Probe(self.session).install()
        self.assertEqual(path.read_bytes(),original)

    def test_retry_keeps_original_failure_and_requires_durable_cleanup(self):
        with self.assertRaises(FileNotFoundError):lab.retry_boundary(self.session,2)
        for suffix in ['exit','finalized']:(self.evidence/('ingress.'+suffix)).write_text('1\n')
        log=self.evidence/'ingress.log';log.write_text('original failed attempt\n')
        with self.assertRaisesRegex(ValueError,'prior scoped cleanup'):lab.retry_boundary(self.session,2)
        log.write_text('original failed attempt\nUPGRADE_INGRESS_SCOPED_CLEAN_VERIFY_PASS\n')
        lab.retry_boundary(self.session,2)
        self.assertEqual(lab.attempt_prefix(2),'ingress-attempt-2')
        self.assertIn('original failed attempt',log.read_text())
        with self.assertRaisesRegex(ValueError,'bounded'):lab.attempt_prefix(4)

    def test_frontend_transfers_changed_contract_inputs_before_boundary(self):
        source=(lab.ROOT/'upgrade/lab/next-control.sh').read_text()
        branch=source.split('  qualify-ingress)',1)[1].split('  signal)',1)[0]
        transfer=branch.split("<<'INGRESS'",1)[0]
        for path in ['upgrade/lab/ingress_qualification.py','upgrade/tests/test_ingress_qualification.py',
                     'upgrade/v1.36.6/evidence/ingress-focused-contracts.json',
                     'upgrade/v1.36.6/evidence/raw/local-qualification/ingress-config-handoff-contracts.log']:
            self.assertIn(path,transfer)
        self.assertNotIn('source-product/',transfer)
        self.assertNotIn('upgrade/lab/next_session.py',transfer)
        self.assertLess(branch.index('retry_boundary(session'),branch.index('nohup bash'))
        self.assertIn('prefix="ingress-attempt-$2"',branch)

    def test_foreign_object_refuses_before_namespace_creation_or_install(self):
        probe=lab.Probe(self.session);self.session.kubectl.return_value='namespace/ingress-nginx\n'
        with self.assertRaisesRegex(ValueError,'foreign'):probe.initialize()
        self.assertEqual(self.session.kubectl.call_count,1)
        self.session.cli.assert_not_called();self.assertEqual(probe.namespaces,{})

    def test_replaced_namespace_prevents_helm_or_deletion(self):
        probe=lab.Probe(self.session);probe.namespaces={'ingress-nginx':'owned'};probe.installed=True
        self.session.kubectl.return_value=json.dumps({'metadata':{'uid':'foreign'}})
        with self.assertRaisesRegex(ValueError,'ownership'):probe.clean()
        self.session.command.assert_not_called();self.assertEqual(self.session.kubectl.call_count,1)

    def test_cleanup_uses_uid_precondition_and_restores_exact_node_label(self):
        probe=lab.Probe(self.session);probe.namespaces={'ingress-nginx':'owned'};probe.installed=True
        probe.labels={'node':{'uid':'node-uid','original':None}}
        def kube(*args,**kwargs):
            if args[:2]==('get','namespace'):return json.dumps({'metadata':{'uid':'owned'}})
            if args[:2]==('get','node'):return json.dumps({'metadata':{'uid':'node-uid',
                'labels':{'ingress-controller/provider':'ingress-nginx'}}})
            return ''
        self.session.kubectl.side_effect=kube;probe.clean()
        args=self.session.command.call_args.args[0]
        self.assertEqual(args[1:3],['uninstall','ingress-nginx'])
        deletes=[c for c in self.session.kubectl.call_args_list if c.args[0]=='delete']
        self.assertEqual(json.loads(deletes[0].kwargs['data'])['preconditions'],{'uid':'owned'})
        patches=[c for c in self.session.kubectl.call_args_list if c.args[0]=='patch']
        operations=json.loads(patches[0].args[-1])
        self.assertEqual(operations[0],{'op':'test','path':'/metadata/uid','value':'node-uid'})
        self.assertEqual(operations[-1],{'op':'remove','path':'/metadata/labels/ingress-controller~1provider'})

    def test_product_failure_still_cleans_owned_scope_and_never_runs_business_assertion(self):
        probe=Mock();probe.install.side_effect=RuntimeError('product failed')
        probe.directory=self.root
        with patch.object(lab,'Session',return_value=self.session),patch.object(lab,'admit_static'), \
                patch.object(lab,'Probe',return_value=probe):
            with self.assertRaisesRegex(RuntimeError,'product failed'):lab.run(self.session.run_id)
        probe.exercise.assert_not_called();probe.clean.assert_called_once_with()

    def exercise_fixture(self,*,admission=True,wrong_ca_rc=60):
        probe=lab.Probe(self.session)
        self.session.kubectl.side_effect=lambda *args,**kwargs: (
            json.dumps({'items':[{'spec':{'nodeName':'node'},'status':{'conditions':[{'type':'Ready','status':'True'}]}}]})
            if 'pods' in args else json.dumps({'status':{'addresses':[{'type':'InternalIP','address':lab.WORKERS[0]}]}})
            if args[:2]==('get','node') else json.dumps({'spec':{'ports':[{'name':'http','nodePort':30080},
                {'name':'https','nodePort':30443}]}}) if 'service' in args else '')
        def command(args,**kwargs):
            if args[0]=='openssl':
                Path(args[args.index('-keyout')+1]).write_text('fixture key')
                Path(args[args.index('-out')+1]).write_text('fixture cert')
                return SimpleNamespace(returncode=0)
            if 'create' in args:
                return SimpleNamespace(returncode=1 if admission else 0,stdout='',
                    stderr='admission webhook denied: host already defined' if admission else '')
            self.assertEqual(args[0],'curl');self.assertNotIn('--insecure',args);self.assertNotIn('-k',args)
            if str(self.root/'clusters/owned-target/ssl/ca.pem') in args:
                return SimpleNamespace(returncode=wrong_ca_rc,stdout=b'',stderr=b'fixture CA refusal')
            return SimpleNamespace(returncode=0,stdout=b'v1366-fixture\n',stderr=b'')
        return probe,command

    def test_real_probe_requires_payload_tls_ca_and_admission_before_success(self):
        probe,command=self.exercise_fixture()
        with patch.object(lab.subprocess,'run',side_effect=command) as process:
            probe.exercise()
        result=json.loads((probe.directory/'result.json').read_text())
        self.assertTrue(result['verified_https_payload']);self.assertTrue(result['http_payload'])
        self.assertEqual(result['wrong_ca_rc'],60)
        curls=[c.args[0] for c in process.call_args_list if c.args[0][0]=='curl']
        self.assertEqual(len(curls),3)
        self.assertIn('--cacert',curls[1])

    def test_missing_webhook_refusal_stops_before_route_and_cannot_sign_success(self):
        probe,command=self.exercise_fixture(admission=False)
        with patch.object(lab.subprocess,'run',side_effect=command) as process:
            with self.assertRaisesRegex(ValueError,'webhook'):probe.exercise()
        self.assertFalse(any(c.args[0][0]=='curl' for c in process.call_args_list))
        self.assertFalse((probe.directory/'result.json').exists())

    def test_transport_failure_cannot_substitute_for_wrong_ca_refusal(self):
        probe,command=self.exercise_fixture(wrong_ca_rc=7)
        with patch.object(lab.subprocess,'run',side_effect=command):
            with self.assertRaisesRegex(ValueError,'TLS verification refusal'):probe.exercise()
        self.assertFalse((probe.directory/'result.json').exists())


if __name__=='__main__':unittest.main()
