import ast
import signal
import sys
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import subprocess
import unittest
from unittest.mock import Mock, patch
from contextlib import contextmanager, redirect_stdout
import io

ROOT=Path(__file__).resolve().parents[2]
spec=importlib.util.spec_from_file_location('probe',ROOT/'tests/helpers/longhorn-ownership.py')
probe=importlib.util.module_from_spec(spec);spec.loader.exec_module(probe)


class ProbeContracts(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory();self.addCleanup(self.directory.cleanup)
        self.objects={};self.state={'run_id':'run','cluster_uid':'cluster','native_uninstalled':True}
        self.product=SimpleNamespace(API=Mock(return_value='live-api'),check_installation_identity=Mock())
        def get(kind,name=''):
            return {'items':[]} if kind=='crds' else self.objects.get((kind,name))
        def create(*args,stdin):
            self.assertEqual(args,('create','-f','-','-o','json'))
            obj=json.loads(stdin);obj['metadata']['uid']='returned-uid'
            self.objects['csidriver','driver.longhorn.io']=obj
            return SimpleNamespace(stdout=json.dumps(obj))
        def remove(obj):
            self.assertEqual(obj['metadata']['uid'],'returned-uid')
            self.objects.pop(('csidriver','driver.longhorn.io'))
        @contextmanager
        def scoped_cleanup(cleanup):
            try:yield
            finally:cleanup()
        self.lab=SimpleNamespace(BASE=Path('/verified-source'),DIRECTORY=Path(self.directory.name),owner=lambda:self.state,
            execute=Mock(return_value=SimpleNamespace(stdout='[]')),
            get=get,core_identity=Mock(),shared_snapshot_identity=lambda:{'shared':'unchanged'},
            kubectl=Mock(side_effect=create),remove=Mock(side_effect=remove),
            wait=lambda name,timeout,check:self.assertTrue(check()),
            write_private=lambda path,row:path.write_text(json.dumps(row)),
            load_helper=lambda name:self.product,scoped_cleanup=scoped_cleanup)
        self.helper=probe.CSIProbe(self.lab)

    def test_fixed_dispatch_selects_only_the_owned_probe_mode(self):
        for mode,marker in (('ownership-request-refusal-qualification','LONGHORN_OWNERSHIP_REQUEST_REFUSAL_QUALIFICATION_PASS'),
                            ('ownership-controller-refusal-qualification','LONGHORN_OWNERSHIP_CONTROLLER_REFUSAL_QUALIFICATION_PASS'),
                            ('ownership-class-refusal-qualification','LONGHORN_OWNERSHIP_CLASS_REFUSAL_QUALIFICATION_PASS'),
                            ('ownership-interruption-qualification','LONGHORN_OWNERSHIP_INTERRUPTION_QUALIFICATION_PASS'),
                            ('ownership-preinstall-refusal-qualification','LONGHORN_OWNERSHIP_PREINSTALL_REFUSAL_QUALIFICATION_PASS'),
                            ('ownership-csi-probe','LONGHORN_OWNERSHIP_CSI_PROBE_PASS'),
                            ('ownership-csi-refusal-qualification','LONGHORN_OWNERSHIP_CSI_REFUSAL_QUALIFICATION_PASS')):
            script='source "$1"\nlonghorn_launch() { printf "%s %s\\n" "$1" "$2"; }\nlonghorn_dispatch --longhorn-'+mode+'\n'
            result=subprocess.run(['bash','-c',script,'contract',str(ROOT/'tests/helpers/longhorn-runner.sh')],text=True,capture_output=True,check=True)
            self.assertEqual(result.stdout.strip(),mode+' '+marker)

    def test_probe_only_reads_product_identity_and_cleans_returned_uid(self):
        with redirect_stdout(io.StringIO()) as output:self.helper.run()
        self.product.check_installation_identity.assert_called_once()
        self.assertIn('LONGHORN_OWNERSHIP_CSI_GAP_OBSERVED ',output.getvalue())
        self.assertEqual(self.objects,{})
        self.assertFalse(self.helper.path.exists())
        self.lab.remove.assert_called_once()

    def test_existing_helm_release_blocks_the_probe_before_canary_creation(self):
        self.lab.execute.return_value.stdout=json.dumps([{'chart':'longhorn-1.12.1'}])
        with self.assertRaisesRegex(ValueError,'no existing Longhorn Helm release'):self.helper.run()
        self.lab.kubectl.assert_not_called();self.lab.remove.assert_not_called()

    def test_fixed_readonly_subcheck_reports_refusal_without_claiming_the_gap(self):
        self.product.check_installation_identity.side_effect=ValueError(probe.CSI_REFUSAL)
        with redirect_stdout(io.StringIO()) as output:self.helper.run()
        self.assertIn('LONGHORN_OWNERSHIP_CSI_READONLY_REFUSAL_OBSERVED',output.getvalue())
        self.assertNotIn('GAP_OBSERVED',output.getvalue());self.assertEqual(self.objects,{})

    def test_product_qualification_uses_config_workflow_and_restores_exact_input_on_every_exit(self):
        from unittest.mock import patch
        spec=importlib.util.spec_from_file_location('ownership_input_recovery',ROOT/'tests/helpers/longhorn-lab.py')
        lab=importlib.util.module_from_spec(spec);spec.loader.exec_module(lab)
        original="longhorn_install: 'no'\nlonghorn_action: present\nlonghorn_requests: []\n"
        path=self.lab.DIRECTORY/'config.yml'
        self.lab.restore_request_input=lab.restore_request_input
        self.lab.write_private=lab.write_private
        for fresh, failure in ((False, False), (False, True), (True, False), (True, True)):
            with self.subTest(fresh=fresh, failure=failure):
                self.state = {'run_id': 'run', 'cluster_uid': 'cluster', **(
                    {'clean_start_verified': True, 'healthy': False} if fresh else {'native_uninstalled': True})}
                path.write_text(original)
                def setup(relay,step,*,expected_refusal):
                    self.assertEqual((relay,step,expected_refusal),(123,'longhorn-setup',probe.CSI_REFUSAL))
                    self.assertEqual(json.loads(path.read_text())['longhorn_install'],'yes')
                    receipt=json.loads((self.lab.DIRECTORY/'longhorn-request-input.json').read_text())
                    self.assertEqual(receipt['original'],original)
                    self.assertEqual(receipt['desired'],path.read_text())
                    if failure:raise RuntimeError('different product failure')
                self.lab.supervise_core=Mock(side_effect=setup)
                with patch.object(lab,'DIRECTORY',self.lab.DIRECTORY),patch.object(lab,'owner',self.lab.owner),redirect_stdout(io.StringIO()) as output:
                    if failure:
                        with self.assertRaisesRegex(RuntimeError,'different product failure'):self.helper.qualify(123)
                    else:self.helper.qualify(123)
                self.assertEqual(path.read_text(),original)
                self.assertFalse((self.lab.DIRECTORY/'longhorn-request-input.json').exists())
                self.assertEqual(self.objects,{})
                self.assertEqual('LONGHORN_OWNERSHIP_CSI_PRODUCT_REFUSAL_VERIFIED' in output.getvalue(),not failure)
                self.product.check_installation_identity.assert_not_called()

    def test_qualification_requires_relay_and_pristine_disabled_product_input(self):
        for invalid in (None,0,-1,True,'123'):
            with self.assertRaisesRegex(ValueError,'live runner relay'):self.helper.qualify(invalid)
        path=self.lab.DIRECTORY/'config.yml';path.write_text("longhorn_install: 'yes'\nlonghorn_action: present\nlonghorn_requests: []\n")
        self.lab.supervise_core=Mock()
        with self.assertRaisesRegex(ValueError,'original disabled'):self.helper.product_refusal(123)
        (self.lab.DIRECTORY/'longhorn-request-input.json').write_text('{}')
        with self.assertRaisesRegex(ValueError,'pending request'):self.helper.product_refusal(123)
        self.lab.supervise_core.assert_not_called();self.lab.kubectl.assert_not_called()

    def test_product_rejection_cleans_canary_but_does_not_claim_gap(self):
        self.product.check_installation_identity.side_effect=ValueError('refused')
        with redirect_stdout(io.StringIO()) as output:
            with self.assertRaisesRegex(ValueError,'refused'):self.helper.run()
        self.assertNotIn('GAP_OBSERVED',output.getvalue());self.assertEqual(self.objects,{})

    def test_existing_driver_never_acquires_fixture_ownership(self):
        self.objects['csidriver','driver.longhorn.io']={'metadata':{'uid':'foreign'}}
        with self.assertRaisesRegex(ValueError,'verified post-uninstall core'):self.helper.run()
        self.lab.kubectl.assert_not_called();self.lab.remove.assert_not_called()

    def test_fresh_core_refuses_unknown_or_previous_installation_before_canary(self):
        for key, value in (('clean_start_verified', False), ('healthy', True),
                ('namespace_uid', 'old'), ('tls_uid', 'old'), ('encryption_uid', 'old'),
                ('native_uninstalled', False)):
            with self.subTest(key=key):
                self.state = {'run_id': 'run', 'cluster_uid': 'cluster',
                              'clean_start_verified': True, 'healthy': False, key: value}
                with self.assertRaisesRegex(ValueError, 'verified post-uninstall core'): self.helper.run()
                self.lab.kubectl.assert_not_called(); self.lab.remove.assert_not_called()

    def test_replacement_change_missing_uid_foreign_journal_and_symlink_refuse_cleanup(self):
        original={'metadata':{'uid':'returned-uid'},'spec':{'podInfoOnMount':True}}
        for fault in ('replacement','spec','uid-missing','foreign-journal','symlink'):
            with self.subTest(fault=fault):
                self.helper.path.unlink(missing_ok=True)
                current=json.loads(json.dumps(original));row={'run_id':'run','cluster_uid':'cluster','created':probe.identity(original)}
                if fault=='replacement':current['metadata']['uid']='foreign'
                if fault=='spec':current['spec']['podInfoOnMount']=False
                if fault=='uid-missing':row.pop('created')
                if fault=='foreign-journal':row['cluster_uid']='foreign'
                if fault=='symlink':self.helper.path.symlink_to('missing')
                else:self.helper.path.write_text(json.dumps(row))
                self.objects['csidriver','driver.longhorn.io']=current
                with self.assertRaises(ValueError):self.helper.cleanup()
                self.lab.remove.assert_not_called()
                self.assertTrue(self.helper.path.exists() or self.helper.path.is_symlink())

    def test_unknown_creation_keeps_intent_even_when_the_driver_is_not_yet_visible(self):
        self.helper.path.write_text(json.dumps({'run_id':'run','cluster_uid':'cluster'}))
        with self.assertRaisesRegex(ValueError,'lacks its returned UID'):self.helper.cleanup()
        self.lab.remove.assert_not_called();self.assertTrue(self.helper.path.exists())

    def test_lost_creation_response_cannot_authorize_canary_removal(self):
        create=self.lab.kubectl.side_effect
        def lost(*args,**kwargs):
            create(*args,**kwargs)
            raise TimeoutError('response lost')
        self.lab.kubectl.side_effect=lost
        with self.assertRaisesRegex(ValueError,'lacks its returned UID'):self.helper.run()
        self.lab.remove.assert_not_called();self.assertTrue(self.helper.path.exists())
        self.product.check_installation_identity.assert_not_called()



class PreinstallContracts(unittest.TestCase):
    def setUp(self):
        from unittest.mock import patch
        import copy
        self.patch = patch
        self.copy = copy.deepcopy
        self.directory = tempfile.TemporaryDirectory(); self.addCleanup(self.directory.cleanup)
        self.base = Path(self.directory.name)
        self.state = {'run_id': 'e48fbabe-963a-44e6-a54c-33990c65cd0c', 'cluster_uid': 'cluster', 'native_uninstalled': True}
        self.objects = {}; self.refusals = []; self.instance = False
        self.original = "longhorn_install: 'no'\nlonghorn_action: present\nlonghorn_requests: []\n"
        (self.base/'config.yml').write_text(self.original)
        def module(name, path):
            spec = importlib.util.spec_from_file_location(name, ROOT/path)
            result = importlib.util.module_from_spec(spec); spec.loader.exec_module(result); return result
        self.real_lab = module('preinstall_input_lab', 'tests/helpers/longhorn-lab.py')
        self.product = module('preinstall_product', 'roles/longhorn/files/longhorn.py')
        def get(kind, name='', namespace=''):
            if kind == 'crds':
                return {'items': [self.copy(o) for (k, _, _), o in self.objects.items() if k == 'customresourcedefinition']}
            if kind == 'kubeautoownershipprobes.longhorn.io':
                self.assertEqual(namespace, '*', 'CRD deletion must inspect instances in every namespace')
                return {'items': [{'metadata': {'uid': 'foreign'}}] if self.instance else []}
            return self.copy(self.objects.get((kind.lower(), name, namespace)))
        def create(*args, stdin):
            self.assertEqual(args, ('create', '-f', '-', '-o', 'json'))
            obj = json.loads(stdin); obj['metadata']['uid'] = 'created-' + obj['kind']
            if obj['kind'] == 'CustomResourceDefinition':
                obj['status'] = {'conditions': [{'type': 'Established', 'status': 'True'}]}
            key = (obj['kind'].lower(), obj['metadata']['name'], obj['metadata'].get('namespace', ''))
            self.assertNotIn(key, self.objects); self.objects[key] = obj
            return SimpleNamespace(stdout=json.dumps(obj))
        def remove(obj):
            key = (obj['kind'].lower(), obj['metadata']['name'], obj['metadata'].get('namespace', ''))
            self.assertEqual(self.objects[key]['metadata']['uid'], obj['metadata']['uid'])
            self.objects.pop(key)
        def wait(name, timeout, check):
            result = check(); self.assertTrue(result, name); return result
        @contextmanager
        def cleanup(function):
            try: yield
            finally: function()
        self.lab = SimpleNamespace(BASE=ROOT, DIRECTORY=self.base, owner=lambda:self.state,
            core_identity=Mock(), shared_snapshot_identity=Mock(return_value={'snapshot':'original'}),
            get=get, kubectl=Mock(side_effect=create), remove=Mock(side_effect=remove), wait=wait,
            scoped_cleanup=cleanup, write_private=self.real_lab.write_private,
            restore_request_input=self.real_lab.restore_request_input,
            namespace_defaults=Mock(return_value=['sa-uid','ca-uid']), check_business_graph=Mock(),
            execute=Mock(side_effect=lambda args:SimpleNamespace(stdout=json.dumps(self.releases()))))
        self.helper = probe.PreinstallProbe(self.lab)
        def supervise(relay, step, *, expected_refusal):
            self.assertEqual((relay, step), (123, 'longhorn-setup'))
            self.assertEqual(json.loads((self.base/'config.yml').read_text())['longhorn_install'], 'yes')
            # Execute the real product preflight, with only the external read
            # interface replaced. Every scenario must refuse before node_map.
            with self.patch.object(self.product, 'run', return_value=json.dumps(self.releases())), \
                    self.patch.object(self.product, 'node_map') as node_map:
                with self.assertRaisesRegex(ValueError, '^' + expected_refusal + '$'):
                    self.product.preflight({}, self.lab, 'helm', self.base/'yml/longhorn/values.json')
                node_map.assert_not_called()
            self.refusals.append(expected_refusal)
        self.lab.supervise_core = Mock(side_effect=supervise)
        self.patches = [self.patch.object(self.real_lab, 'DIRECTORY', self.base), self.patch.object(self.real_lab, 'owner', self.lab.owner)]
        for item in self.patches: item.start(); self.addCleanup(item.stop)

    def releases(self):
        import base64, gzip
        result = []
        for (kind, _, _), obj in self.objects.items():
            if kind != 'secret': continue
            encoded = base64.b64decode(obj['data']['release'])
            release = json.loads(gzip.decompress(base64.b64decode(encoded)))
            self.assertEqual(release['manifest'], '')
            self.assertNotIn('hooks', release)
            self.assertLessEqual(len(release['name']), 53)
            result.append({'name': release['name'], 'namespace': release['namespace'],
                'chart': release['chart']['metadata']['name']+'-'+release['chart']['metadata']['version'],
                'app_version': release['chart']['metadata']['appVersion'], 'status': release['info']['status']})
        return result

    def journal(self, scenario):
        desired = self.helper.desired(scenario)
        obj = json.loads(self.lab.kubectl('create','-f','-','-o','json',stdin=json.dumps(desired)).stdout)
        row = dict(self.state, scenario=scenario, desired=desired, created=self.helper.binding(obj), defaults=['sa-uid','ca-uid'])
        self.lab.write_private(self.helper.path,row)
        return row

    def test_all_three_canaries_reach_the_real_product_guard_and_restore_config(self):
        with redirect_stdout(io.StringIO()) as output: self.helper.qualify(123)
        self.assertEqual(self.refusals, list(self.helper.REFUSALS.values()))
        self.assertEqual(self.objects, {}); self.assertFalse(self.helper.path.exists())
        self.assertEqual((self.base/'config.yml').read_text(), self.original)
        self.assertFalse((self.base/'longhorn-request-input.json').exists())
        self.assertFalse((self.base/'yml/longhorn/installation.json').exists())
        self.lab.check_business_graph.assert_called_once_with({'namespace':'longhorn-system',
            'namespace_uid':'created-Namespace','defaults':['sa-uid','ca-uid'],'objects':{}})
        self.assertIn('LONGHORN_OWNERSHIP_PREINSTALL_REFUSAL_BUSINESS_PASS', output.getvalue())

    def test_full_fresh_core_reaches_real_preinstall_guards_without_fake_uninstall(self):
        # Produce the actual configure() state used before the full ownership
        # stage; no historical uninstall receipt or marker stub is synthesized.
        config = self.base/'config.yml'
        config.write_text('longhorn_version: 1.12.1\n')
        state_path = self.base/'longhorn-lab-owner.json'
        with patch.object(self.real_lab, 'DIRECTORY', self.base), patch.object(self.real_lab, 'STATE', state_path), redirect_stdout(io.StringIO()):
            self.real_lab.configure()
        self.state = json.loads(state_path.read_text())
        self.state['cluster_uid'] = 'cluster'
        config.write_text(self.original)
        with redirect_stdout(io.StringIO()): self.helper.qualify(123)
        self.assertNotIn('native_uninstalled', self.state)
        self.assertEqual(self.refusals, list(self.helper.REFUSALS.values()))
        self.assertEqual(self.objects, {})
        self.assertEqual(config.read_text(), self.original)
        self.assertFalse(self.helper.path.exists())

    def test_product_failure_restores_input_cleans_exact_canary_and_stops_remaining_scenarios(self):
        self.lab.supervise_core.side_effect = RuntimeError('unexpected product failure')
        with redirect_stdout(io.StringIO()) as output:
            with self.assertRaisesRegex(RuntimeError, 'unexpected product failure'): self.helper.qualify(123)
        self.assertEqual(self.lab.kubectl.call_count, 1); self.assertEqual(self.objects, {})
        self.assertEqual((self.base/'config.yml').read_text(), self.original)
        self.assertNotIn('REFUSAL_BUSINESS_PASS', output.getvalue())

    def test_cleanup_refuses_replacement_payload_namespace_graph_and_crd_instances(self):
        for scenario, fault in [('namespace','uid'),('namespace','graph'),('namespace','defaults'),
                                ('release','payload'),('crd','spec'),('crd','instance')]:
            with self.subTest(scenario=scenario,fault=fault):
                self.objects.clear(); self.helper.path.unlink(missing_ok=True)
                self.lab.remove.reset_mock(); self.lab.check_business_graph.reset_mock(side_effect=True)
                row = self.journal(scenario); key = next(iter(self.objects))
                if fault == 'uid': self.objects[key]['metadata']['uid'] = 'replacement'
                if fault == 'payload': self.objects[key]['data']['release'] = 'changed'
                if fault == 'spec': self.objects[key]['spec']['scope'] = 'Cluster'
                if fault == 'instance': self.instance = True
                if fault == 'graph': self.lab.check_business_graph.side_effect = ValueError('foreign namespace resource')
                if fault == 'defaults':
                    row.pop('defaults'); self.lab.write_private(self.helper.path,row)
                with self.assertRaises(ValueError): self.helper.cleanup()
                self.lab.remove.assert_not_called(); self.assertTrue(self.helper.path.exists())
                self.instance = False

    def test_unknown_create_response_keeps_intent_and_never_guesses_ownership(self):
        create = self.lab.kubectl.side_effect
        def lost(*args, **kwargs):
            create(*args, **kwargs); raise TimeoutError('response lost')
        self.lab.kubectl.side_effect = lost
        with self.assertRaisesRegex(ValueError, 'lacks returned UID'): self.helper.qualify(123)
        self.lab.remove.assert_not_called(); self.lab.supervise_core.assert_not_called()
        self.assertTrue(self.helper.path.exists()); self.assertEqual(len(self.objects),1)

    def test_foreign_or_changed_journal_and_symlink_never_authorize_cleanup(self):
        for fault in ('cluster','desired','scenario','symlink'):
            with self.subTest(fault=fault):
                self.objects.clear(); self.helper.path.unlink(missing_ok=True)
                row = self.journal('release')
                if fault == 'cluster': row['cluster_uid'] = 'other-cluster'
                if fault == 'desired': row['desired']['metadata']['namespace'] = 'another'
                if fault == 'scenario': row['scenario'] = 'unknown'
                if fault == 'symlink':
                    self.helper.path.unlink(); self.helper.path.symlink_to('missing')
                else: self.lab.write_private(self.helper.path,row)
                with self.assertRaises(ValueError): self.helper.cleanup()
                self.lab.remove.assert_not_called()

    def test_existing_storage_or_missing_relay_refuses_before_creation(self):
        for invalid in (None,True,'123',0,-1):
            with self.assertRaisesRegex(ValueError,'live runner relay'): self.helper.qualify(invalid)
        self.lab.kubectl.assert_not_called()
        self.state['native_uninstalled'] = False
        with self.assertRaisesRegex(ValueError,'verified post-uninstall core'): self.helper.qualify(123)
        self.lab.kubectl.assert_not_called()

    def test_unrecognized_helm_metadata_cannot_produce_product_or_business_pass(self):
        # Discovery must prove this exact metadata is consumed by Helm; the
        # product's namespace guard alone cannot qualify the release fixture.
        real = self.lab.execute.side_effect
        def execute(args):
            result = real(args)
            return SimpleNamespace(stdout='[]') if self.releases() else result
        self.lab.execute.side_effect = execute
        with redirect_stdout(io.StringIO()) as output:
            with self.assertRaisesRegex(ValueError,'real Helm list did not recognize'): self.helper.qualify(123)
        self.assertEqual(len(self.refusals),1); self.assertEqual(self.objects,{})
        self.assertNotIn('REFUSAL_BUSINESS_PASS',output.getvalue())

    def test_interruption_canary_unwinds_when_its_owner_exits_and_refuses_an_orphan_start(self):
        with self.patch('os.getppid', side_effect=[100, 1]), redirect_stdout(io.StringIO()) as output:
            with self.assertRaises(SystemExit) as stopped: probe.interruption_canary(self.lab)
        self.assertEqual(stopped.exception.code, 143)
        self.assertEqual(self.objects, {}); self.assertFalse(self.helper.path.exists())
        self.assertIn('LONGHORN_OWNERSHIP_INTERRUPTION_CANARY_READY', output.getvalue())
        self.lab.kubectl.reset_mock()
        with self.patch('os.getppid', return_value=1):
            with self.assertRaisesRegex(ValueError, 'lost its owning parent'): probe.interruption_canary(self.lab)
        self.lab.kubectl.assert_not_called()

    def test_interruption_canary_never_overwrites_a_pending_ownership_journal(self):
        self.helper.path.write_text('unconfirmed intent')
        with self.patch('os.getppid', return_value=100):
            with self.assertRaisesRegex(ValueError, 'no pending ownership fixture'): probe.interruption_canary(self.lab)
        self.assertEqual(self.helper.path.read_text(), 'unconfirmed intent')
        self.lab.kubectl.assert_not_called(); self.lab.remove.assert_not_called()

    def test_interruption_qualification_refuses_other_pending_operation_before_spawning(self):
        self.lab.installation_health = Mock()
        names = ('longhorn-install-business.json', 'longhorn-request-input.json',
                 'longhorn-ownership-csi.json', 'longhorn-ownership-preinstall.json', 'longhorn-ownership-class.json',
                 'longhorn-ownership-controller.json', 'longhorn-ownership-request.json',
                 'longhorn-recurring-credentials.json', 'longhorn-recurring-runtime.json',
                 'longhorn-encryption-key.json', 'longhorn-security-tls.json',
                 'longhorn-network-probes.json', 'longhorn-reclaim-class.json',
                 'longhorn-backup-refusal.json')
        for filename in names:
            for symlink in (False, True):
                with self.subTest(filename=filename, symlink=symlink):
                    path = self.base/filename
                    if symlink: path.symlink_to('missing')
                    else: path.write_text('pending operation')
                    with self.patch('subprocess.Popen') as spawn:
                        with self.assertRaisesRegex(ValueError, 'no pending operation'):
                            probe.qualify_interruption(self.lab)
                        spawn.assert_not_called()
                    self.assertTrue(path.exists() or path.is_symlink())
                    path.unlink()


class OwnershipRecoveryContracts(unittest.TestCase):
    def test_sigterm_unwinds_the_real_helper_cli_cleanup(self):
        tree = ast.parse((ROOT/'tests/helpers/longhorn-lab.py').read_text())
        entry = next(node for node in tree.body if isinstance(node, ast.If) and isinstance(node.test, ast.Compare)
                     and isinstance(node.test.left, ast.Name) and node.test.left.id == '__name__')
        script = '''import signal,sys
from contextlib import contextmanager
@contextmanager
def scoped():
 try: yield
 finally: print('OWNED_CLEANUP',flush=True)
def main():
 with scoped():
  print('READY',flush=True)
  while True:signal.pause()
def record_failure(error):raise AssertionError('unexpected exception')
''' + ast.unparse(entry)
        process = subprocess.Popen([sys.executable,'-u','-c',script],stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        try:
            self.assertEqual(process.stdout.readline().strip(),'READY')
            process.send_signal(signal.SIGTERM)
            out,err = process.communicate(timeout=5)
            self.assertEqual(process.returncode,143,err)
            self.assertEqual(out.strip(),'OWNED_CLEANUP')
        finally:
            if process.poll() is None:process.kill();process.communicate(timeout=5)

    def test_fixed_business_cleanup_restores_input_then_dispatches_owned_journals(self):
        spec=importlib.util.spec_from_file_location('ownership_recovery_lab',ROOT/'tests/helpers/longhorn-lab.py')
        lab=importlib.util.module_from_spec(spec);spec.loader.exec_module(lab)
        for filename in ('longhorn-ownership-csi.json','longhorn-ownership-preinstall.json',
                         'longhorn-ownership-class.json','longhorn-ownership-controller.json','longhorn-ownership-request.json'):
            for symlink in (False,True):
                with self.subTest(filename=filename,symlink=symlink),tempfile.TemporaryDirectory() as directory:
                    path=Path(directory);journal=path/filename
                    if symlink:journal.symlink_to('missing')
                    else:journal.write_text('{}')
                    calls=[];helper=SimpleNamespace(cleanup=lambda passed:(self.assertIs(passed,lab),calls.append('ownership')))
                    with patch.object(lab,'DIRECTORY',path),patch.object(lab,'owner',return_value={}), \
                            patch.object(lab,'restore_request_input',side_effect=lambda:calls.append('input')), \
                            patch.object(lab,'cleanup_reclaim_class',side_effect=lambda:calls.append('classes')), \
                            patch.object(lab,'load_helper',return_value=helper),patch.dict(sys.modules,{lab.__name__:lab}):
                        lab.cleanup_business()
                    self.assertEqual(calls,['input','ownership','classes'])

    def test_owned_journal_recovery_checks_core_when_storage_is_absent(self):
        live=(ROOT/'tests/helpers/longhorn-live.sh').read_text()
        selector=next(line for line in live.splitlines() if line.startswith('  business-clean|idempotence|'))
        prefix=live.split(selector+'\n',1)[1].split('    work="$(mktemp',1)[0]
        for filename in ('longhorn-ownership-csi.json','longhorn-ownership-preinstall.json'):
            for symlink in (False,True):
                with self.subTest(filename=filename,symlink=symlink),tempfile.TemporaryDirectory() as directory:
                    base=Path(directory);(base/'.venv/bin').mkdir(parents=True)
                    (base/'.venv/bin/python').symlink_to(sys.executable)
                    (base/'tests/helpers').mkdir(parents=True)
                    (base/'tests/helpers/longhorn-lab.py').write_text("import sys\nprint(sys.argv[1])\nsys.exit(17 if sys.argv[1]=='health' else 0)\n")
                    fixture=base/'clusters/longhorn-gate';fixture.mkdir(parents=True)
                    path=fixture/filename
                    if symlink:path.symlink_to('missing')
                    else:path.write_text('{}')
                    result=subprocess.run(['bash','-c','set -Eeuo pipefail\nBASE="$1"\nMODE=business-clean\n'+prefix,
                        'contract',str(base)],capture_output=True,text=True,timeout=5)
                    self.assertEqual(result.returncode,0,result.stderr)
                    self.assertEqual(result.stdout.strip(),'verify-core')

class InstalledClassContracts(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory();self.addCleanup(self.directory.cleanup)
        self.base=Path(self.directory.name);self.objects={};self.consumers={'pv':[],'pvc':[]}
        self.state={'run_id':'run','cluster_uid':'cluster'}
        from tests.unit.test_longhorn_delivery import configuration
        self.config=configuration();self.original=json.dumps(self.config)+'\n'
        (self.base/'config.yml').write_text(self.original)
        def get(kind,name='',namespace=''):
            if kind in self.consumers:
                self.assertEqual(namespace,'*' if kind=='pvc' else '')
                return {'items':self.consumers[kind]}
            return self.objects.get(name)
        def create(*args,stdin):
            self.assertEqual(args,('create','-f','-','-o','json'))
            obj=json.loads(stdin);obj['metadata']['uid']='original-uid'
            self.objects[obj['metadata']['name']]=obj
            return SimpleNamespace(stdout=json.dumps(obj))
        def remove(obj):
            self.assertEqual(obj['metadata']['uid'],'original-uid')
            del self.objects[obj['metadata']['name']]
        @contextmanager
        def scope(function):
            try:yield
            finally:function()
        self.lab=SimpleNamespace(DIRECTORY=self.base,owner=lambda:self.state,get=get,
            kubectl=Mock(side_effect=create),remove=Mock(side_effect=remove),installation_health=Mock(),
            scoped_cleanup=scope,write_private=lambda path,row:path.write_text(json.dumps(row)),
            wait=lambda label,timeout,check:self.assertTrue(check()),
            idempotence_snapshot=Mock(return_value={'workloads':'unchanged'}),
            shared_snapshot_identity=Mock(return_value={'snapshots':'unchanged'}),
            class_identity=lambda:{name:probe.InstalledClassProbe.binding(obj) for name,obj in self.objects.items()},
            run_requests=Mock())
        self.helper=probe.InstalledClassProbe(self.lab)
        def request(relay, requests, *, storage_classes, expected_refusal):
            from tests.unit.test_longhorn_delivery import FakeAPI, product
            api=FakeAPI();api.listing('storageclasses',list(self.objects.values()))
            with self.assertRaisesRegex(ValueError,'^'+expected_refusal+'$'):
                product.check_classes(dict(self.config,longhorn_storage_classes=storage_classes),api)
            self.assertEqual((relay,requests),(123,[]))
            self.assertFalse(api.writes)
        self.lab.run_requests.side_effect=request

    def journal(self):
        desired=self.helper.desired();obj=json.loads(json.dumps(desired));obj['metadata']['uid']='original-uid'
        self.objects[obj['metadata']['name']]=obj
        row=dict(self.state,desired=desired,created=self.helper.binding(obj))
        self.lab.write_private(self.helper.path,row);return row,obj

    def test_product_entry_refuses_the_foreign_class_and_preserves_original_baseline(self):
        with redirect_stdout(io.StringIO()) as output:self.helper.qualify(123)
        args,kwargs=self.lab.run_requests.call_args
        self.assertEqual(args,(123,[]));self.assertEqual(kwargs['expected_refusal'],'foreign resource: longhorn-owner-run')
        declared=kwargs['storage_classes'];self.assertEqual(declared[:-1],self.config['longhorn_storage_classes'])
        self.assertEqual(declared[-1]['name'],'longhorn-owner-run');self.assertIs(declared[-1]['default'],False)
        self.assertEqual(self.objects,{});self.assertFalse(self.helper.path.exists())
        self.assertEqual((self.base/'config.yml').read_text(),self.original)
        for marker in ('PRODUCT_REFUSAL_VERIFIED','CANARY_CLEAN_PASS','REFUSAL_BUSINESS_PASS'):
            self.assertIn('LONGHORN_OWNERSHIP_CLASS_'+marker,output.getvalue())

    def test_unknown_create_result_is_retained_and_cannot_authorize_cleanup(self):
        create=self.lab.kubectl.side_effect
        def lost(*args,**kwargs):create(*args,**kwargs);raise TimeoutError('lost response')
        self.lab.kubectl.side_effect=lost
        with self.assertRaisesRegex(ValueError,'retain intent'):self.helper.qualify(123)
        self.lab.remove.assert_not_called();self.lab.run_requests.assert_not_called()
        self.assertTrue(self.helper.path.exists());self.assertEqual(len(self.objects),1)

    def test_cleanup_refuses_changed_uid_labels_or_any_class_field(self):
        for field in ('uid','labels','provisioner','parameters','reclaimPolicy','volumeBindingMode',
                      'allowVolumeExpansion','mountOptions','allowedTopologies'):
            with self.subTest(field=field):
                self.objects.clear();row,obj=self.journal();self.lab.remove.reset_mock()
                if field in ('uid','labels'):obj['metadata'][field]='changed' if field=='uid' else {'foreign':'yes'}
                else:obj[field]='changed'
                with self.assertRaisesRegex(ValueError,'replaced or changed'):self.helper.cleanup()
                self.lab.remove.assert_not_called();self.assertTrue(self.helper.path.exists())

    def test_pv_and_all_namespace_pvc_consumers_block_cleanup(self):
        for kind in ('pv','pvc'):
            with self.subTest(kind=kind):
                self.objects.clear();row,obj=self.journal();self.lab.remove.reset_mock()
                self.consumers[kind]=[{'spec':{'storageClassName':obj['metadata']['name']}}]
                with self.assertRaisesRegex(ValueError,'PV or PVC consumer'):self.helper.cleanup()
                self.lab.remove.assert_not_called();self.assertTrue(self.helper.path.exists())
                self.consumers[kind]=[]

    def test_foreign_or_changed_journal_and_symlink_never_authorize_deletion(self):
        for fault in ('cluster_uid','desired','symlink'):
            with self.subTest(fault=fault):
                self.objects.clear();self.helper.path.unlink(missing_ok=True);row,obj=self.journal();self.lab.remove.reset_mock()
                if fault=='symlink':self.helper.path.unlink();self.helper.path.symlink_to('missing')
                else:
                    if fault=='desired':row['desired']['provisioner']='other'
                    else:row[fault]='another-cluster'
                    self.lab.write_private(self.helper.path,row)
                with self.assertRaises(ValueError):self.helper.cleanup()
                self.lab.remove.assert_not_called()

    def test_unexpected_product_failure_cleans_owned_canary_without_business_pass(self):
        self.lab.run_requests.side_effect=RuntimeError('unexpected failure')
        with redirect_stdout(io.StringIO()) as output:
            with self.assertRaisesRegex(RuntimeError,'unexpected failure'):self.helper.qualify(123)
        self.assertEqual(self.objects,{});self.assertFalse(self.helper.path.exists())
        self.assertNotIn('REFUSAL_BUSINESS_PASS',output.getvalue())

    def test_pending_operation_or_invalid_relay_refuses_before_creation(self):
        for invalid in (None,True,0,-1,'123'):
            with self.assertRaisesRegex(ValueError,'live runner relay'):self.helper.qualify(invalid)
        for name in ('longhorn-request-input.json','longhorn-install-business.json','longhorn-ownership-csi.json',
                     'longhorn-ownership-preinstall.json','longhorn-ownership-class.json',
                     'longhorn-ownership-controller.json','longhorn-ownership-request.json'):
            for symlink in (False,True):
                with self.subTest(name=name,symlink=symlink):
                    path=self.base/name
                    if symlink:path.symlink_to('missing')
                    else:path.write_text('pending')
                    with self.assertRaisesRegex(ValueError,'no pending ownership'):self.helper.qualify(123)
                    path.unlink()
        self.lab.kubectl.assert_not_called()


class InstalledControllerContracts(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory();self.addCleanup(self.directory.cleanup)
        self.base=Path(self.directory.name);self.objects={};self.children={'replicaset':[],'pod':[]}
        self.state={'run_id':'run','cluster_uid':'cluster'};self.namespace={'metadata':{'uid':'system-uid'}}
        from tests.unit.test_longhorn_delivery import configuration
        self.config=configuration();self.original=json.dumps(self.config)+'\n'
        (self.base/'config.yml').write_text(self.original)
        def get(kind,name='',namespace=''):
            if kind=='namespace':return self.namespace
            if kind in self.children:
                self.assertEqual(namespace,'kube-system');return {'items':self.children[kind]}
            self.assertEqual((kind,namespace),('deployment','kube-system'))
            return self.objects.get(name)
        def create(*args,stdin):
            self.assertEqual(args,('create','-f','-','-o','json'))
            obj=json.loads(stdin);obj['metadata']['uid']='original-uid'
            self.assertEqual(obj['spec']['replicas'],0);self.assertIs(obj['spec']['paused'],True)
            self.objects[obj['metadata']['name']]=obj
            return SimpleNamespace(stdout=json.dumps(obj))
        def remove(obj):
            self.assertEqual(obj['metadata']['uid'],'original-uid');del self.objects[obj['metadata']['name']]
        @contextmanager
        def scope(function):
            try:yield
            finally:function()
        self.lab=SimpleNamespace(DIRECTORY=self.base,owner=lambda:self.state,get=get,
            kubectl=Mock(side_effect=create),remove=Mock(side_effect=remove),installation_health=Mock(),
            scoped_cleanup=scope,write_private=lambda path,row:path.write_text(json.dumps(row)),
            wait=lambda label,timeout,check:self.assertTrue(check()),
            idempotence_snapshot=Mock(return_value={'workloads':'unchanged'}),
            shared_snapshot_identity=Mock(return_value={'snapshots':'unchanged'}),
            class_identity=Mock(return_value={'classes':'unchanged'}),run_requests=Mock())
        self.helper=probe.InstalledControllerProbe(self.lab)
        self.lab.run_requests.side_effect=self.request

    def request(self,relay,requests,*,expected_refusal):
        from tests.unit.test_longhorn_delivery import FakeAPI, product
        self.assertEqual((relay,requests),(123,[]))
        self.assertEqual(expected_refusal,'refusing a second or foreign snapshot controller')
        managed=next(x for x in product.snapshot_objects(self.config) if x['kind']=='Deployment')
        for controllers in (list(self.objects.values()),[managed]+list(self.objects.values())):
            api=FakeAPI();api.listing('deployments',controllers,namespace='*')
            with self.assertRaisesRegex(ValueError,'^'+expected_refusal+'$'):
                product.snapshot_preflight(self.config,api)
            self.assertFalse(api.writes)

    def journal(self):
        desired=self.helper.desired(self.config);obj=json.loads(json.dumps(desired));obj['metadata']['uid']='original-uid'
        self.objects[obj['metadata']['name']]=obj
        row=dict(self.state,desired=desired,created=probe.identity(obj),namespace_uid='system-uid')
        self.lab.write_private(self.helper.path,row);return row,obj

    def test_product_refuses_foreign_or_second_declaration_without_api_writes(self):
        with redirect_stdout(io.StringIO()) as output:self.helper.qualify(123)
        self.lab.run_requests.assert_called_once()
        self.assertEqual(self.objects,{});self.assertFalse(self.helper.path.exists())
        self.assertEqual((self.base/'config.yml').read_text(),self.original)
        for marker in ('PRODUCT_REFUSAL_VERIFIED','CANARY_CLEAN_PASS','REFUSAL_BUSINESS_PASS'):
            self.assertIn('LONGHORN_OWNERSHIP_CONTROLLER_'+marker,output.getvalue())

    def test_unknown_create_response_retains_intent_and_object(self):
        create=self.lab.kubectl.side_effect
        def lost(*args,**kwargs):create(*args,**kwargs);raise TimeoutError('lost response')
        self.lab.kubectl.side_effect=lost
        with self.assertRaisesRegex(ValueError,'retain intent'):self.helper.qualify(123)
        self.lab.remove.assert_not_called();self.lab.run_requests.assert_not_called()
        self.assertTrue(self.helper.path.exists());self.assertEqual(len(self.objects),1)

    def test_replaced_uid_metadata_spec_or_namespace_blocks_deletion(self):
        for fault in ('uid','labels','annotations','spec','namespace'):
            with self.subTest(fault=fault):
                self.objects.clear();self.namespace={'metadata':{'uid':'system-uid'}}
                row,obj=self.journal();self.lab.remove.reset_mock()
                if fault=='namespace':self.namespace['metadata']['uid']='replaced'
                elif fault=='spec':obj['spec']['replicas']=1
                else:obj['metadata'][fault]='changed' if fault=='uid' else {'foreign':'changed'}
                with self.assertRaises(ValueError):self.helper.cleanup()
                self.lab.remove.assert_not_called();self.assertTrue(self.helper.path.exists())

    def test_owned_or_selector_matching_children_refuse_cleanup(self):
        for kind in ('replicaset','pod'):
            for reference in (True,False):
                with self.subTest(kind=kind,reference=reference):
                    self.objects.clear();row,obj=self.journal();self.lab.remove.reset_mock()
                    metadata={'ownerReferences':[{'uid':'original-uid'}]} if reference else {'labels':obj['spec']['selector']['matchLabels']}
                    self.children[kind]=[{'metadata':metadata}]
                    with self.assertRaisesRegex(ValueError,'ReplicaSet or Pod'):self.helper.cleanup()
                    self.lab.remove.assert_not_called();self.assertTrue(self.helper.path.exists());self.children[kind]=[]

    def test_foreign_or_changed_journal_and_symlink_refuse_cleanup(self):
        for fault in ('run_id','cluster_uid','desired','symlink'):
            with self.subTest(fault=fault):
                self.objects.clear();self.helper.path.unlink(missing_ok=True);row,obj=self.journal();self.lab.remove.reset_mock()
                if fault=='symlink':self.helper.path.unlink();self.helper.path.symlink_to('missing')
                else:
                    if fault=='desired':row['desired']['spec']['paused']=False
                    else:row[fault]='other'
                    self.lab.write_private(self.helper.path,row)
                with self.assertRaises(ValueError):self.helper.cleanup()
                self.lab.remove.assert_not_called()

    def test_unexpected_failure_cleans_canary_without_business_pass(self):
        self.lab.run_requests.side_effect=RuntimeError('unexpected failure')
        with redirect_stdout(io.StringIO()) as output:
            with self.assertRaisesRegex(RuntimeError,'unexpected failure'):self.helper.qualify(123)
        self.assertEqual(self.objects,{});self.assertFalse(self.helper.path.exists())
        self.assertNotIn('REFUSAL_BUSINESS_PASS',output.getvalue())

    def test_pending_operations_and_invalid_relay_block_creation(self):
        for invalid in (None,True,0,-1,'123'):
            with self.assertRaisesRegex(ValueError,'live runner relay'):self.helper.qualify(invalid)
        for name in ('longhorn-request-input.json','longhorn-install-business.json','longhorn-ownership-csi.json',
                     'longhorn-ownership-preinstall.json','longhorn-ownership-class.json','longhorn-ownership-request.json',self.helper.path.name):
            for symlink in (False,True):
                with self.subTest(name=name,symlink=symlink):
                    path=self.base/name
                    if symlink:path.symlink_to('missing')
                    else:path.write_text('pending')
                    with self.assertRaisesRegex(ValueError,'no pending ownership'):self.helper.qualify(123)
                    path.unlink()
        self.lab.kubectl.assert_not_called()

    def test_external_mode_and_baseline_change_never_receive_business_pass(self):
        self.config['longhorn_snapshot_controller']='external';(self.base/'config.yml').write_text(json.dumps(self.config))
        with self.assertRaisesRegex(ValueError,'original managed'):self.helper.qualify(123)
        self.lab.kubectl.assert_not_called();(self.base/'config.yml').write_text(self.original)
        self.config=json.loads(self.original)
        self.lab.shared_snapshot_identity.side_effect=[{'snapshot':'original'},{'snapshot':'changed'}]
        with redirect_stdout(io.StringIO()) as output:
            with self.assertRaisesRegex(ValueError,'installation baseline'):self.helper.qualify(123)
        self.assertNotIn('REFUSAL_BUSINESS_PASS',output.getvalue());self.assertEqual(self.objects,{})


class InstalledRequestContracts(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory();self.addCleanup(self.directory.cleanup)
        self.base=Path(self.directory.name);self.objects={};self.backups=[];self.clones=[];self.contents=[]
        self.state={'run_id':'run','cluster_uid':'cluster','namespace_uid':'installed-namespace'}
        self.journal={'volume':'source-volume','volume_uid':'volume-uid'}
        self.source={'metadata':{'uid':'volume-uid'},'spec':{'dataEngine':'v1'}}
        self.namespace={'metadata':{'uid':'installed-namespace'}}
        self.engine={'metadata':{'uid':'engine-uid'},'spec':{'volumeName':'source-volume'},
                     'status':{'currentState':'running','snapshots':{'volume-head':{}}}}
        from tests.unit.test_longhorn_delivery import configuration,product
        self.config=configuration();self.product=product
        (self.base/'config.yml').write_text(json.dumps(self.config)+'\n')
        (self.base/'longhorn-install-business.json').write_text(json.dumps(self.journal))
        def get(kind,name='',namespace=''):
            if kind=='namespace':return self.namespace
            if kind=='volumes.longhorn.io':return self.source if name else {'items':[self.source]+self.clones}
            if kind=='engines.longhorn.io':return {'items':[self.engine]}
            if kind=='backups.longhorn.io':return {'items':self.backups}
            if kind=='volumesnapshotcontents.snapshot.storage.k8s.io':return {'items':self.contents}
            self.assertEqual(kind,'snapshots.longhorn.io');self.assertEqual(namespace,'longhorn-system')
            return self.objects.get(name)
        def create(*args,stdin):
            self.assertEqual(args,('create','-f','-','-o','json'))
            obj=json.loads(stdin);self.assertIs(obj['spec']['createSnapshot'],True)
            obj['metadata'].update(uid='uid-'+obj['metadata']['name'],
                finalizers=['longhorn.io'],ownerReferences=[{'uid':'volume-uid','kind':'Volume'}])
            obj['metadata']['labels']['longhornvolume']='source-volume';obj['spec']['dataEngine']='v1'
            obj['status']={'readyToUse':True,'creationTime':'2026-10-20T00:00:00Z'}
            self.engine['status']['snapshots'][obj['metadata']['name']]={}
            self.objects[obj['metadata']['name']]=obj
            return SimpleNamespace(stdout=json.dumps(obj))
        def remove(obj):del self.objects[obj['metadata']['name']]
        @contextmanager
        def scope(function):
            try:yield
            finally:function()
        self.lab=SimpleNamespace(DIRECTORY=self.base,owner=lambda:self.state,get=get,
            kubectl=Mock(side_effect=create),remove=Mock(side_effect=remove),load_helper=lambda _:self.product,
            scoped_cleanup=scope,write_private=lambda path,row:path.write_text(json.dumps(row)),
            wait=lambda label,timeout,check:self.assertTrue(check()),
            shared_snapshot_identity=Mock(return_value={'snapshots':'unchanged'}),
            class_identity=Mock(return_value={'classes':'unchanged'}),run_requests=Mock())
        self.helper=probe.InstalledRequestProbe(self.lab);self.lab.run_requests.side_effect=self.request

    def request(self,relay,requests,*,expected_refusal):
        from tests.unit.test_longhorn_delivery import FakeAPI
        self.assertEqual(relay,123);self.assertEqual(len(requests),1)
        api=FakeAPI();api.listing('crds',[{'metadata':{'name':'snapshots.longhorn.io'}}])
        obj=self.objects[requests[0]['name']];api.put('Snapshot.longhorn.io',obj,namespace='longhorn-system')
        with self.assertRaisesRegex(ValueError,'^'+expected_refusal+'$'):
            self.product.check_declarations(dict(self.config,longhorn_requests=requests),api)
        self.assertFalse(api.writes)

    def journal_canary(self):
        request,desired=self.helper.desired(self.journal,'foreign')
        obj=json.loads(self.lab.kubectl('create','-f','-','-o','json',stdin=json.dumps(desired)).stdout)
        obj=self.objects[obj['metadata']['name']]
        row=dict(self.state,scenario='foreign',desired=desired,volume_uid='volume-uid',created=self.helper.binding(obj))
        self.lab.write_private(self.helper.path,row);return row,obj

    def test_actual_product_checks_refuse_ready_snapshot_ids_and_bind_both_for_native_cleanup(self):
        with redirect_stdout(io.StringIO()) as output:self.helper.verify(self.journal,123)
        self.assertEqual(self.lab.run_requests.call_count,2);self.assertEqual(len(self.objects),2)
        self.assertEqual(self.journal['native_snapshots'],{name:{'uid':obj['metadata']['uid']} for name,obj in self.objects.items()})
        self.lab.remove.assert_not_called()
        self.assertFalse(self.helper.path.exists());self.assertEqual(len(self.engine['status']['snapshots']),3)
        for scenario in ('foreign','immutable'):
            for marker in ('PRODUCT_REFUSAL_VERIFIED','SNAPSHOT_BOUND_FOR_CLEANUP'):
                self.assertIn('LONGHORN_OWNERSHIP_REQUEST_'+marker+' scenario='+scenario,output.getvalue())
        self.assertIn('LONGHORN_OWNERSHIP_REQUEST_REFUSAL_VERIFIED',output.getvalue())

    def test_lost_creation_response_retains_intent_and_blocks_deletion(self):
        create=self.lab.kubectl.side_effect
        def lost(*args,**kwargs):create(*args,**kwargs);raise TimeoutError('lost response')
        self.lab.kubectl.side_effect=lost
        with self.assertRaisesRegex(ValueError,'retain intent'):self.helper.verify(self.journal,123)
        self.lab.remove.assert_not_called();self.lab.run_requests.assert_not_called()
        self.assertEqual(len(self.objects),1);self.assertTrue(self.helper.path.exists())

    def test_response_that_changes_creation_intent_stops_and_retains_the_bound_object(self):
        create=self.lab.kubectl.side_effect
        def changed(*args,**kwargs):
            result=create(*args,**kwargs);obj=json.loads(result.stdout)
            self.objects[obj['metadata']['name']]['spec']['createSnapshot']=False
            obj['spec']['createSnapshot']=False
            return SimpleNamespace(stdout=json.dumps(obj))
        self.lab.kubectl.side_effect=changed
        with self.assertRaisesRegex(ValueError,'original snapshot creation'):self.helper.verify(self.journal,123)
        self.lab.remove.assert_not_called();self.lab.run_requests.assert_not_called()
        self.assertTrue(json.loads(self.helper.path.read_text()).get('created'))

    def test_changed_binding_source_or_namespace_refuses_cleanup(self):
        for fault in ('uid','labels','annotations','spec','ownerReferences','finalizers','source','namespace'):
            with self.subTest(fault=fault):
                self.objects.clear();self.source['metadata']['uid']='volume-uid';self.namespace['metadata']['uid']='installed-namespace'
                row,obj=self.journal_canary();self.lab.remove.reset_mock()
                if fault=='source':self.source['metadata']['uid']='other'
                elif fault=='namespace':self.namespace['metadata']['uid']='other'
                elif fault=='spec':obj['spec']['createSnapshot']=False
                else:obj['metadata'][fault]='changed'
                with self.assertRaises(ValueError):self.helper.cleanup()
                self.lab.remove.assert_not_called();self.assertTrue(self.helper.path.exists())

    def test_backup_clone_or_csi_consumers_block_transfer(self):
        for fault in ('backup','clone','csi-static','csi-dynamic'):
            with self.subTest(fault=fault):
                self.objects.clear();row,obj=self.journal_canary();self.lab.remove.reset_mock()
                name=obj['metadata']['name']
                if fault=='backup':self.backups=[{'spec':{'snapshotName':name}}]
                elif fault=='clone':self.clones=[{'spec':{'dataSource':'snap://source-volume/'+name}}]
                elif fault=='csi-static':self.contents=[{'spec':{'source':{'snapshotHandle':'snap://source-volume/'+name}}}]
                else:self.contents=[{'status':{'snapshotHandle':'snap://source-volume/'+name}}]
                with self.assertRaises(ValueError):self.helper.cleanup()
                self.lab.remove.assert_not_called();self.assertTrue(self.helper.path.exists())
                self.engine['status']['snapshots']={'volume-head':{}};self.backups=[];self.clones=[];self.contents=[]

    def test_foreign_journal_changed_desired_and_symlink_refuse_cleanup(self):
        for fault in ('run_id','cluster_uid','namespace_uid','volume_uid','desired','symlink'):
            with self.subTest(fault=fault):
                self.objects.clear();self.helper.path.unlink(missing_ok=True);row,obj=self.journal_canary();self.lab.remove.reset_mock()
                if fault=='symlink':self.helper.path.unlink();self.helper.path.symlink_to('missing')
                else:
                    if fault=='desired':row['desired']['spec']['createSnapshot']=False
                    else:row[fault]='other'
                    self.lab.write_private(self.helper.path,row)
                with self.assertRaises(ValueError):self.helper.cleanup()
                self.lab.remove.assert_not_called()

    def test_unexpected_product_failure_stops_and_transfers_only_the_exact_snapshot(self):
        self.lab.run_requests.side_effect=RuntimeError('different failure')
        with redirect_stdout(io.StringIO()) as output:
            with self.assertRaisesRegex(RuntimeError,'different failure'):self.helper.verify(self.journal,123)
        self.assertEqual(self.lab.kubectl.call_count,1);self.assertEqual(len(self.objects),1)
        self.lab.remove.assert_not_called()
        self.assertFalse(self.helper.path.exists());self.assertNotIn('REQUEST_REFUSAL_VERIFIED',output.getvalue())

    def test_source_engine_or_input_baseline_change_blocks_success(self):
        request=self.request
        def changed(*args,**kwargs):request(*args,**kwargs);(self.base/'config.yml').write_text('changed')
        self.lab.run_requests.side_effect=changed
        with redirect_stdout(io.StringIO()) as output:
            with self.assertRaisesRegex(ValueError,'original configuration'):self.helper.verify(self.journal,123)
        self.assertEqual(self.lab.kubectl.call_count,1);self.assertEqual(len(self.objects),1)
        self.lab.remove.assert_not_called()
        self.assertNotIn('REQUEST_REFUSAL_VERIFIED',output.getvalue())

    def test_invalid_relay_existing_journal_or_stopped_engine_blocks_creation(self):
        for invalid in (None,True,0,-1,'123'):
            with self.assertRaisesRegex(ValueError,'live runner relay'):self.helper.verify(self.journal,invalid)
        for symlink in (False,True):
            if symlink:self.helper.path.symlink_to('missing')
            else:self.helper.path.write_text('{}')
            with self.assertRaisesRegex(ValueError,'no pending canary'):self.helper.verify(self.journal,123)
            self.helper.path.unlink()
        self.engine['status']['currentState']='stopped'
        with self.assertRaisesRegex(ValueError,'source engine'):self.helper.verify(self.journal,123)
        self.lab.kubectl.assert_not_called()


    def test_readiness_precedes_product_request_and_keeps_real_engine_snapshot(self):
        request=self.request
        def verify_ready(*args,**kwargs):
            row=json.loads(self.helper.path.read_text())
            self.assertTrue(self.helper.ready(row,self.journal))
            return request(*args,**kwargs)
        self.lab.run_requests.side_effect=verify_ready
        with redirect_stdout(io.StringIO()):self.helper.verify(self.journal,123)
        self.assertEqual(self.lab.run_requests.call_count,2)

    def test_missing_engine_data_unready_or_replaced_snapshot_cannot_reach_product(self):
        for fault in ('unready','engine','uid','error','deleting'):
            with self.subTest(fault=fault):
                self.setUp()
                create=self.lab.kubectl.side_effect
                def changed(*args,**kwargs):
                    result=create(*args,**kwargs);obj=next(iter(self.objects.values()))
                    if fault=='unready':obj['status']['readyToUse']=False
                    elif fault=='engine':self.engine['status']['snapshots']={'volume-head':{}}
                    elif fault=='uid':obj['metadata']['uid']='replacement'
                    elif fault=='error':obj['status']['error']='failed'
                    else:obj['metadata']['deletionTimestamp']='2026-10-20T00:00:01Z'
                    return result
                self.lab.kubectl.side_effect=changed
                with redirect_stdout(io.StringIO()):
                    with self.assertRaises((ValueError,AssertionError)):self.helper.verify(self.journal,123)
                self.lab.run_requests.assert_not_called();self.lab.remove.assert_not_called()


class ArchivedRequestRecoveryContracts(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory();self.addCleanup(self.directory.cleanup)
        self.base=Path(self.directory.name)
        self.proof=json.loads((ROOT/'tests/evidence/longhorn-ownership-request-fixture-failure-20261020.json').read_text())['recovery']
        self.row=json.loads(json.dumps(self.proof['canary']))
        self.journal=json.loads(json.dumps(self.proof['business']))
        binding=self.proof['current']
        self.current={'metadata':{key:value for key,value in binding.items() if key!='spec'},'spec':binding['spec']}
        self.observation=self.base/self.proof['product_observation']['name']
        self.observation.write_text('{"rc":0}')
        self.product=SimpleNamespace(check_request=Mock(),request_object=lambda request:request)
        import hashlib
        self.sources=self.base/'sources';self.source_bytes={}
        self.source_digests={}
        for name in self.proof['source_sha256']:
            if name.startswith('roles/longhorn/'):
                data=('owned historical source fixture: '+name).encode()
                path=self.sources/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(data)
                self.source_bytes[name]=data;self.source_digests[name]=hashlib.sha256(data).hexdigest()
        self.lab=SimpleNamespace(BASE=self.sources,DIRECTORY=self.base,owner=lambda:self.proof['owner'],
            load_helper=lambda _:self.product,write_private=lambda path,row:path.write_text(json.dumps(row)))
        self.helper=probe.InstalledRequestProbe(self.lab)
        self.helper.path.write_text(json.dumps(self.row))
        self.helper.source=Mock();self.helper.engine_snapshots=Mock(return_value=self.proof['engine'])
        self.lab.get=Mock(return_value={'items':[]})
        # The historical private observation is never copied into unit fixtures.
        # Substitute local observation/source digests. The historical runtime
        # guard stays pinned; this unit fixture must not depend on today's
        # unrelated production source matching the old run's byte identity.
        import hashlib
        self.digest=hashlib.sha256(self.observation.read_bytes()).hexdigest()
        original=probe.json.loads
        def read(raw,*args,**kwargs):
            obj=original(raw,*args,**kwargs)
            if isinstance(obj,dict) and 'recovery' in obj:
                obj['recovery']['product_observation']['sha256']=self.digest
                obj['recovery']['source_sha256'].update(self.source_digests)
            return obj
        self.reader=patch.object(probe.json,'loads',side_effect=read);self.reader.start();self.addCleanup(self.reader.stop)

    def recover(self):self.helper.recover_archived_creation(self.row,self.journal,self.current)

    def test_exact_evidence_transfers_uid_without_deleting_or_rewriting_old_identity(self):
        old=json.loads(json.dumps(self.row))
        with redirect_stdout(io.StringIO()) as output:self.recover()
        self.assertEqual(self.row,old);self.assertFalse(self.helper.path.exists())
        saved=json.loads((self.base/'longhorn-install-business.json').read_text())
        self.assertEqual(saved['native_snapshots'],{self.proof['request']['name']:{
            'request':self.proof['request'],'uid':self.proof['current']['uid']}})
        self.assertIn('TRANSFERRED_TO_BUSINESS_CLEANUP',output.getvalue())
        self.helper.path.write_text(json.dumps(self.row));self.journal=saved
        with redirect_stdout(io.StringIO()):self.recover()

    def test_changed_run_old_receipt_new_uid_declaration_or_business_blocks_transfer(self):
        for field in ('run','old_uid','new_uid','spec','business','engine','observation','source','source-bytes','consumer'):
            with self.subTest(field=field):
                row=json.loads(json.dumps(self.row));journal=json.loads(json.dumps(self.journal))
                current=json.loads(json.dumps(self.current))
                state=dict(self.proof['owner']);self.lab.owner=lambda:state
                self.helper.engine_snapshots.return_value=self.proof['engine']
                self.observation.write_text('{"rc":0}');self.lab.BASE=self.sources;self.lab.get.return_value={'items':[]}
                for name,data in self.source_bytes.items():(self.sources/name).write_bytes(data)
                if field=='run':state['run_id']='different'
                elif field=='old_uid':row['created']['uid']='different'
                elif field=='new_uid':current['metadata']['uid']='different'
                elif field=='spec':current['spec']['createSnapshot']=False
                elif field=='business':journal['volume_uid']='different'
                elif field=='engine':self.helper.engine_snapshots.return_value={'uid':'different','snapshots':[]}
                elif field=='observation':self.observation.write_text('{"rc":1}')
                elif field=='source':self.lab.BASE=self.base
                elif field=='source-bytes':(self.sources/next(iter(self.source_bytes))).write_bytes(b'foreign source')
                elif field=='consumer':self.lab.get.return_value={'items':[{'spec':{'snapshotName':self.proof['request']['name']}}]}
                with self.assertRaises((ValueError,FileNotFoundError)):
                    self.helper.recover_archived_creation(row,journal,current)
                self.assertTrue(self.helper.path.exists())
                self.assertFalse((self.base/'longhorn-install-business.json').exists())


if __name__=='__main__':unittest.main()

