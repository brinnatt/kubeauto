"""LONGHORN-41 routing contracts; no network or lab mutation."""
import ast
import fcntl
import hashlib
from contextlib import contextmanager
import importlib.util
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

HERE = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('longhorn_cleanup_contract', HERE/'helpers/longhorn-cleanup.py')
gate = importlib.util.module_from_spec(SPEC); sys.modules[SPEC.name] = gate; SPEC.loader.exec_module(gate)
PRODUCT = HERE.parent if (HERE.parent/'tests/helpers/longhorn-lab.py').is_file() else Path.cwd()


class Routing(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)

    def write(self, scope, name, row):
        directory = self.base/'clusters'/gate.CLUSTERS[scope]; directory.mkdir(parents=True, exist_ok=True)
        path = directory/name; path.write_text(json.dumps(row)); path.chmod(0o600); return path

    def owner(self, scope='v1'):
        row = {'cluster':gate.CLUSTERS[scope], 'nodes':list(gate.NODES[scope]), 'clean_start_verified':True,
            'run_id':'12345678-1234-4234-8234-123456789012', 'cluster_uid':scope+'-core',
            'namespace_uid':scope+'-namespace', 'node_uids':{}}
        self.write(scope, 'longhorn-lab-owner.json', row); return row

    def business(self, scope='v1', **fields):
        row = {'namespace':'longhorn-business-owned', 'namespace_uid':'owned-ns', 'objects':{}, **fields}
        self.write(scope, gate.BUSINESS, row); return row

    def actions(self): return gate.plan(self.base)[1]

    def test_all_retained_producer_archives_stay_byte_identical(self):
            names = ('longhorn-account-failure-'+'a'*24+'.json',
                     'longhorn-system-recovery-prebusiness-cleaned-'+'b'*32+'.json',
                     'longhorn-v1-upgrade-before-business-'+'c'*32+'.json',
                     'longhorn-v2-resources-ambiguous-11111111-2222-3333-4444-555555555555.json')
            for name in names:
                path = self.write('v1', name, self.owner('v1'))
                before = path.read_bytes()
                self.assertEqual(self.actions(), [])
                self.assertEqual(path.read_bytes(), before)
            path.rename(path.with_suffix('.original'))
            path.symlink_to(path.with_suffix('.original'))
            with self.assertRaisesRegex(ValueError, 'unsafe passive'):
                self.actions()
    def test_only_exact_completed_same_core_upgrade_abort_is_passive(self):
        owner=self.owner('v2');identifier='a'*32
        row={'id':identifier,'stage':'aborted-before-upgrade','old_runtime':{'version':'1.12.0'},
            'source_identity':{key:owner[key] for key in ('run_id','cluster_uid','node_uids')}}
        path=self.write('v2','longhorn-upgrade-aborted-'+identifier+'.json',row)
        before=path.read_bytes();self.assertEqual(self.actions(),[]);self.assertEqual(path.read_bytes(),before)
        for defect in ('id','stage','old-version','new-runtime','legacy','retained','config','run','cluster','nodes'):
            with self.subTest(defect=defect):
                changed=json.loads(before)
                if defect=='id':changed['id']='b'*32
                elif defect=='stage':changed['stage']='source-1.12.0'
                elif defect=='old-version':changed['old_runtime']['version']='1.12.1'
                elif defect in ('new-runtime','legacy','retained','config'):
                    changed[{'new-runtime':'new_runtime','legacy':'legacy','retained':'retained_before','config':'successful_configuration'}[defect]]={}
                else:changed['source_identity'][{'run':'run_id','cluster':'cluster_uid','nodes':'node_uids'}[defect]]='foreign'
                path.write_text(json.dumps(changed))
                with self.assertRaisesRegex(ValueError,'upgrade abort archive'):self.actions()
        path.write_bytes(before);path.chmod(0o644)
        with self.assertRaisesRegex(ValueError,'owner-private'):self.actions()
        path.chmod(0o600);path.rename(path.with_suffix('.original'));path.symlink_to(path.with_suffix('.original'))
        with self.assertRaisesRegex(ValueError,'owner-private'):self.actions()
        path.unlink();path=self.write('v2','longhorn-upgrade-aborted-'+identifier[:-1]+'.json',row)
        with self.assertRaisesRegex(ValueError,'unknown or orphan'):self.actions()

    def test_nearby_unknown_name_is_still_refused(self):
            self.write('v1', 'longhorn-account-failure-'+'a'*23+'.json', self.owner('v1'))
            with self.assertRaisesRegex(ValueError, 'unknown or orphan'):
                self.actions()
    def test_only_completed_owned_cache_receipt_is_passive(self):
            owner = self.owner('v2')
            row = {'phase': 'verified', 'copy_rc': 0,
                   'proof': {'run_id': owner['run_id'], 'key': 'brinnatt/busybox:1.37'}}
            path = self.write('v2', 'longhorn-busybox-cache-reconcile.json', row)
            before = path.read_bytes()
            self.assertEqual(self.actions(), [])
            self.assertEqual(path.read_bytes(), before)
            for phase in ('prepared', 'copy-returned'):
                row['phase'] = phase
                path.write_text(json.dumps(row))
                with self.assertRaisesRegex(ValueError, 'single-key cache'):
                    self.actions()
            row.update(phase='restored', copy_rc=0)
            path.write_text(json.dumps(row))
            self.assertEqual(self.actions(), [])
            row['proof']['run_id'] = 'foreign'
            path.write_text(json.dumps(row))
            with self.assertRaisesRegex(ValueError, 'single-key cache'):
                self.actions()
            row['proof'] = None
            path.write_text(json.dumps(row))
            with self.assertRaisesRegex(ValueError, 'single-key cache'):
                self.actions()
            row.update(proof={'run_id': owner['run_id'], 'key': 'brinnatt/busybox:1.37'}, copy_rc=1)
            path.write_text(json.dumps(row))
            with self.assertRaisesRegex(ValueError, 'single-key cache'):
                self.actions()

    def test_empty_cleanup_is_idempotent(self): self.assertEqual(self.actions(), [])

    def test_healthy_baseline_keeps_its_v2_page_pool(self):
        owner = self.owner('v2'); path = self.write('v2', 'longhorn-v2-resources.json', owner)
        raw = path.read_bytes(); self.assertEqual(self.actions(), []); self.assertEqual(path.read_bytes(), raw)

    def test_every_primary_owner_uses_existing_fixed_cleanup(self):
        for recipe in gate.ROUTES:
            with self.subTest(scope=recipe.scope, name=recipe.name), tempfile.TemporaryDirectory() as directory:
                original = self.base; self.base = Path(directory)
                try:
                    owner = self.owner(recipe.scope)
                    if recipe.target: self.owner('target')
                    self.write(recipe.scope, sorted(recipe.files)[0], owner)
                    result = self.actions(); self.assertEqual([r.name for r in result], [recipe.name])
                    self.assertEqual(result[0].helper, recipe.helper)
                    source = (PRODUCT/'tests/helpers'/recipe.helper).read_text()
                    # The fixed recipe must reach a genuine cleanup branch.
                    self.assertIn(recipe.mode, source)
                    self.assertNotIn('lab-wipe-nodes', recipe.helper)
                finally: self.base = original

    def test_v2_cpu_or_resource_journal_cannot_select_a_foreign_scope(self):
        for scope,name in (('v1','longhorn-v2-cpu-isolation.json'),('target','longhorn-v2-cpu-isolation.json'),
                ('v1','longhorn-v2-resources.json')):
            with self.subTest(scope=scope,name=name),tempfile.TemporaryDirectory() as directory:
                original=self.base;self.base=Path(directory)
                try:
                    owner=self.owner(scope);self.write(scope,name,owner)
                    with self.assertRaisesRegex(ValueError,'foreign scope'):self.actions()
                finally:self.base=original

    def test_completed_cpu_receipt_is_retained_without_repeating_host_recovery(self):
        owner=self.owner('v2');path=self.write('v2','longhorn-v2-cpu-isolation.json',dict(owner,clean=True,
            hosts={node:{'captured':True,'restored':True} for node in gate.NODES['v2']}))
        before=path.read_bytes();self.assertEqual(self.actions(),[]);self.assertEqual(path.read_bytes(),before)

    def test_cpu_clean_flag_does_not_hide_partial_host_recovery(self):
        owner=self.owner('v2');self.write('v2','longhorn-v2-cpu-isolation.json',dict(owner,clean=True,hosts={}))
        with self.assertRaisesRegex(ValueError,'incomplete CPU'):self.actions()

    def test_historical_owned_diagnostic_archive_is_preserved_with_its_original_permissions(self):
        self.owner('v2');path=self.write('v2','longhorn-v2-cpu-isolation-completed-abcdef12.json',{'diagnostic':True})
        path.chmod(0o644);before=(path.read_bytes(),path.stat().st_mode)
        self.assertEqual(self.actions(),[]);self.assertEqual((path.read_bytes(),path.stat().st_mode),before)

    def test_ordinary_business_has_an_explicit_route(self):
        self.owner(); self.business(); self.assertEqual([r.name for r in self.actions()], ['business'])

    def test_all_v2_failure_journal_keys_select_the_owner_not_business(self):
        self.owner('v2')
        for key in ('v2_all_replicas_fault','v2_disk_fault','v2_runtime_fault','v2_manager_loss','v2_replica_im_loss'):
            self.business('v2', **{key:{}})
            self.assertEqual([(r.name,r.mode) for r in self.actions()], [('failure','v2-failure-cleanup')])

    def test_v1_disk_intent_and_proof_are_one_owner(self):
        owner = self.owner(); proof = {'run_id':owner['run_id'], 'case':'disk'}
        self.business(v1_storage_fault=proof, v1_storage_fault_intent=proof)
        self.assertEqual([r.mode for r in self.actions()], ['disk-failure-cleanup'])

    def test_v1_network_uses_network_recovery(self):
        self.owner(); self.business(v1_storage_fault_intent={'case':'network'})
        self.assertEqual([r.mode for r in self.actions()], ['network-failure-cleanup'])

    def test_v1_stability_observability_last_replica_restore_before_business(self):
        self.owner()
        for key, expected in [('v1_storage_faults','stability-cleanup'),('v1_last_replica','last-replica-cleanup'),
                ('v1_observability','observability-cleanup')]:
            self.business(**{key:{}}); self.assertEqual([r.mode for r in self.actions()], [expected])

    def test_multiple_fault_keys_refuse_before_any_subprocess(self):
        self.owner('v2'); self.business('v2', v2_disk_fault={}, v2_runtime_fault={})
        with patch.object(gate.subprocess,'Popen') as child, self.assertRaisesRegex(ValueError,'conflicting business'):
            self.actions()
        child.assert_not_called()

    def test_different_fault_cases_in_one_business_refuse(self):
        self.owner(); self.business(v1_storage_fault={'case':'disk'}, v1_storage_fault_intent={'case':'network'})
        with self.assertRaisesRegex(ValueError,'conflicting'): self.actions()

    def test_unknown_fault_type_cannot_fall_back_to_business_delete(self):
        self.owner(); self.business(new_host_fault={'address':'foreign'})
        with self.assertRaisesRegex(ValueError,'unknown business'): self.actions()

    def test_unknown_field_alongside_known_fault_is_also_refused(self):
        self.owner('v2'); self.business('v2',v2_runtime_fault={},unreviewed_fault={})
        with self.assertRaisesRegex(ValueError,'unknown business'): self.actions()

    def test_fault_on_the_wrong_engine_refuses(self):
        self.owner('v2'); self.business('v2',v1_storage_fault={'case':'disk'})
        with self.assertRaisesRegex(ValueError,'foreign cluster'): self.actions()

    def test_foreign_nested_fault_run_id_refuses(self):
        self.owner(); self.business(v1_storage_faults={'run_id':'another-run'})
        with self.assertRaisesRegex(ValueError,'foreign journal'): self.actions()

    def test_foreign_business_namespace_refuses(self):
        self.owner(); self.write('v1',gate.BUSINESS,{'namespace':'customer-prod','namespace_uid':'foreign'})
        with self.assertRaisesRegex(ValueError,'foreign business'): self.actions()

    def test_unknown_journal_refuses_without_removing_foreign_data(self):
        self.owner(); path = self.write('v1','longhorn-unreviewed.json',{'foreign':True}); raw=path.read_bytes()
        with self.assertRaisesRegex(ValueError,'unknown'): self.actions()
        self.assertEqual(path.read_bytes(),raw)

    def test_one_foreign_scope_blocks_the_entire_plan_before_mutation(self):
        self.owner(); self.business(); owner = self.owner('v2')
        self.write('v2','longhorn-v2-linked-clones.json',dict(owner,run_id='foreign'))
        with patch.object(gate,'recover') as recover, self.assertRaisesRegex(ValueError,'foreign'):
            gate.plan(self.base)
        recover.assert_not_called()

    def test_two_main_scenarios_refuse(self):
        owner=self.owner()
        for name in ('longhorn-capacity.json','longhorn-maintenance.json'): self.write('v1',name,owner)
        with self.assertRaisesRegex(ValueError,'conflicting primary'): self.actions()

    def test_unrelated_business_cannot_overlap_middleware(self):
        owner=self.owner();self.write('v1','longhorn-v1-middleware.json',owner);self.business()
        with self.assertRaisesRegex(ValueError,'unrelated common'):self.actions()

    def test_middleware_precedes_monitoring_pvc_cleanup(self):
        owner=self.owner()
        for name in ('longhorn-v1-middleware.json','longhorn-v1-monitoring.json'):self.write('v1',name,owner)
        self.assertEqual([r.name for r in self.actions()],['middleware','monitor'])

    def test_observability_owns_monitor_and_business(self):
        owner=self.owner('v2'); self.business('v2',v2_observability={})
        self.write('v2','longhorn-v2-monitoring.json',owner)
        self.assertEqual([r.name for r in self.actions()],['observability'])

    def test_cpu_recovery_precedes_observability_and_monitor(self):
        owner=self.owner('v2'); self.business('v2',v2_observability={})
        for name in ('longhorn-v2-cpu-isolation.json','longhorn-v2-monitoring.json'):self.write('v2',name,owner)
        self.assertEqual([r.name for r in self.actions()],['cpu','observability'])

    def test_cpu_profile_cannot_overlap_disk_lifecycle(self):
        owner=self.owner('v2')
        for name in ('longhorn-v2-cpu-isolation.json','longhorn-v2-disk-lifecycle.json'):self.write('v2',name,owner)
        with self.assertRaisesRegex(ValueError,'CPU profile'):self.actions()

    def test_runtime_owner_recovers_its_interrupt_subjournal(self):
        owner=self.owner('v2')
        for name in ('longhorn-v2-runtime-resources.json','longhorn-v2-interrupt.json'):self.write('v2',name,owner)
        self.assertEqual([r.name for r in self.actions()],['runtime-resources'])

    def test_systemrestore_owns_systembackup_subjournal(self):
        owner=self.owner('v2')
        for name in ('longhorn-systemrestore-qualification.json','longhorn-systembackup-qualification.json'):self.write('v2',name,owner)
        self.assertEqual([r.name for r in self.actions()],['systemrestore'])

    def test_reboot_fault_owns_boot_allocation_recovery(self):
        owner=self.owner('v2');self.business('v2',v2_runtime_fault={})
        self.write('v2','longhorn-v2-boot-fixture.json',owner)
        self.assertEqual([r.name for r in self.actions()],['failure'])

    def test_orphan_boot_allocation_stops_with_owner_hint(self):
        owner=self.owner('v2');self.write('v2','longhorn-v2-boot-fixture.json',owner)
        with self.assertRaisesRegex(ValueError,'worker-reboot owner'):self.actions()

    def test_dr_coordinator_cleans_target_consumer_before_shared_store(self):
        source=self.owner('v2');target=self.owner('target')
        self.write('v2','longhorn-v2-dr.json',source)
        self.write('target','longhorn-dr-backup-consumer.json',target);self.business('target')
        self.assertEqual([(r.scope,r.name) for r in self.actions()],[('v2','disaster-recovery')])

    def test_target_conflicting_owner_blocks_source_cleanup(self):
        source=self.owner('v2');target=self.owner('target');self.write('v2','longhorn-v2-dr.json',source)
        self.write('target','longhorn-v2-interrupt.json',target)
        with self.assertRaisesRegex(ValueError,'conflicting journal'):self.actions()

    def test_two_sources_cannot_claim_one_target(self):
        one=self.owner();two=self.owner('v2');self.owner('target')
        self.write('v1','longhorn-system-recovery.json',one);self.write('v2','longhorn-v2-dr.json',two)
        with self.assertRaisesRegex(ValueError,'two independent'):self.actions()

    def test_orphan_target_consumer_refuses(self):
        owner=self.owner('target');self.write('target','longhorn-dr-backup-consumer.json',owner)
        with self.assertRaisesRegex(ValueError,'coordinator'):self.actions()

    def test_all_explicit_retained_paths_name_the_owning_recovery(self):
        for name in gate.RETAIN:
            with self.subTest(name=name),tempfile.TemporaryDirectory() as directory:
                original=self.base;self.base=Path(directory)
                try:
                    owner=self.owner('v2');self.write('v2',name,owner)
                    with self.assertRaises(ValueError):self.actions()
                finally:self.base=original

    def test_completed_environment_recovery_is_preserved(self):
        owner=self.owner('v2')
        for name in ('longhorn-v2-post-reboot-environment.json',
                     'longhorn-v2-installed-post-reboot-environment.json','longhorn-v2-resources-reboot.json'):
            self.write('v2',name,dict(owner,phase='complete'))
        self.assertEqual(self.actions(),[])

    def test_pending_upgrade_preparation_is_not_invented_complete(self):
        owner=self.owner('v2');self.write('v2','longhorn-v2-upgrade-preparation.json',dict(owner,stage='installing-old'))
        with self.assertRaisesRegex(ValueError,'incomplete upgrade'):self.actions()

    def test_completed_upgrade_preparation_is_preserved(self):
        owner=self.owner('v2');path=self.write('v2','longhorn-v2-upgrade-preparation.json',dict(owner,stage='complete'))
        raw=path.read_bytes();self.assertEqual(self.actions(),[]);self.assertEqual(path.read_bytes(),raw)

    def test_adoption_is_never_completed_or_rolled_back_by_cleanup(self):
        for scope in gate.CLUSTERS:
            self.owner(scope)
            engine={'target':'v2-dr-target'}.get(scope,scope)
            for revision in ('','revision-2-'):
                path=self.write(scope,'longhorn-'+engine+'-im-'+revision+'adopt.json',{})
                with self.assertRaisesRegex(ValueError,'image adoption retained'):self.actions()
                path.unlink()

    def test_private_symlink_public_and_foreign_owner_journals_refuse(self):
        self.owner();path=self.write('v1','longhorn-platform.json',{})
        path.chmod(0o644)
        with self.assertRaisesRegex(ValueError,'private'):self.actions()
        path.unlink();path.symlink_to('/etc/passwd')
        with self.assertRaisesRegex(ValueError,'regular'):self.actions()

    def test_unrelated_nonjournal_canary_is_untouched(self):
        self.owner();path=self.base/'clusters/longhorn-gate/customer-canary.bin';path.write_bytes(b'foreign')
        before=(path.stat().st_ino,path.read_bytes());self.assertEqual(self.actions(),[])
        self.assertEqual((path.stat().st_ino,path.read_bytes()),before)

    def test_all_recovery_recipes_avoid_destroy_wipe_and_full_scenarios(self):
        self.assertTrue(all('cleanup' in r.mode or r.mode in ('clean','cleanup') for r in gate.ROUTES))
        for r in gate.ROUTES:self.assertNotIn(r.helper,('lab-wipe-nodes.sh','longhorn-v2-cleanup.sh'))
        tree=ast.parse((HERE/'helpers/longhorn-cleanup.py').read_text())
        calls=[ast.unparse(n) for n in ast.walk(tree) if isinstance(n,ast.Call)]
        self.assertFalse(any('os.system(' in x or 'shell=True' in x for x in calls))




    def test_completed_reboot_archive_is_preserved_and_never_selected_for_cleanup(self):
        owner=self.owner('v2');raw=json.dumps(dict(owner,phase='complete',hosts={})).encode()
        name='longhorn-v2-resources-reboot-complete-'+hashlib.sha256(raw).hexdigest()+'.json'
        path=self.write('v2',name,owner);path.write_bytes(raw)
        self.assertEqual(self.actions(),[]);self.assertEqual(path.read_bytes(),raw)
    def test_similar_unknown_name_is_still_refused(self):
        owner=self.owner('v2');self.write('v2','longhorn-v2-resources-reboot-complete-'+'a'*63+'.json',owner)
        with self.assertRaises(ValueError):self.actions()
    def test_symlink_to_completed_reboot_archive_is_not_adopted(self):
        owner=self.owner('v2');name='longhorn-v2-resources-reboot-complete-'+'a'*64+'.json'
        path=self.write('v2',name,owner);path.rename(path.with_suffix('.original'));path.symlink_to(path.with_suffix('.original'))
        with self.assertRaisesRegex(ValueError,'unsafe passive'):self.actions()

class ProcessContracts(unittest.TestCase):
    @contextmanager
    def descriptors(self):
        saved={}
        for fd in (7,8,9):
            try:saved[fd]=os.dup(fd)
            except OSError:saved[fd]=None
            source=os.open('/dev/null',os.O_RDONLY);os.dup2(source,fd)
            if source!=fd:os.close(source)
        try:yield
        finally:
            for fd,original in saved.items():
                if original is None:os.close(fd)
                else:os.dup2(original,fd);os.close(original)

    def test_v2_generic_cleanup_uses_its_actual_backup_profile(self):
        with patch.object(gate,'load') as loader:
            gate.fixture('v2');loader.assert_called_once_with('longhorn-v2-backups.py')

    def test_normal_owner_exit_preserves_exact_log(self):
        with tempfile.TemporaryDirectory() as directory,self.descriptors():
            log=Path(directory)/'owner.log';gate.run([sys.executable,'-c','print("OWNED_CLEAN_PASS")'],log,'OWNED_CLEAN_PASS')
            self.assertEqual(log.read_text(),'OWNED_CLEAN_PASS\n')

    def test_empty_or_similar_or_duplicate_marker_cannot_sign_success(self):
        for output in ('', 'OWNED_CLEAN_PASS extra', 'OWNED_CLEAN_PASS\nOWNED_CLEAN_PASS'):
            with self.subTest(output=output),tempfile.TemporaryDirectory() as directory,self.descriptors():
                with self.assertRaisesRegex(ValueError,'exactly one success marker'):
                    gate.run([sys.executable,'-c','print('+repr(output)+')'],Path(directory)/'owner.log','OWNED_CLEAN_PASS')

    def test_failed_owner_keeps_first_failure_and_stops(self):
        with tempfile.TemporaryDirectory() as directory,self.descriptors():
            log=Path(directory)/'owner.log'
            with self.assertRaisesRegex(ValueError,'rc=86'):
                gate.run([sys.executable,'-c','print("FIRST_CAUSE",flush=True);raise SystemExit(86)'],log,'OWNED_CLEAN_PASS')
            self.assertEqual(log.read_text(),'FIRST_CAUSE\n')

    def test_failure_marker_with_zero_exit_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory,self.descriptors():
            with self.assertRaisesRegex(ValueError,'failure marker'):
                gate.run([sys.executable,'-c','print("LONGHORN_OWNER_FAIL reason=fixture")'],Path(directory)/'owner.log','OWNED_CLEAN_PASS')

    def test_sigterm_owner_is_not_accepted_as_cleanup_success(self):
        with tempfile.TemporaryDirectory() as directory,self.descriptors():
            log=Path(directory)/'owner.log'
            with self.assertRaisesRegex(ValueError,'rc=-15'):
                gate.run([sys.executable,'-c','import os,signal;print("OWNED_JOURNAL_RETAINED",flush=True);os.kill(os.getpid(),signal.SIGTERM)'],log,'OWNED_CLEAN_PASS')
            self.assertIn('OWNED_JOURNAL_RETAINED',log.read_text())

    def test_cleanup_failure_prevents_next_owner_and_final_marker(self):
        actions=[gate.route('platform','v1','longhorn-platform.json','owner.sh','clean'),
            gate.route('monitor','v1','longhorn-v1-monitoring.json','monitor.sh','cleanup')]
        with tempfile.TemporaryDirectory() as directory,patch.object(gate,'BASE',Path(directory)), \
                patch.object(gate,'plan',return_value=({},actions)),patch.object(gate,'load') as loader, \
                patch.object(gate,'recover',side_effect=ValueError('first cause')) as recover, \
                patch.object(gate,'verify') as verify,patch.dict(os.environ,{'LONGHORN_PARENT_LEASE_HELD':'yes','LONGHORN_CLEANUP_WORK':directory}), \
                patch.object(sys,'argv',['cleanup']):
            Path(directory).chmod(0o700)
            with self.assertRaisesRegex(ValueError,'first cause'):gate.main()
            self.assertEqual(recover.call_count,1);verify.assert_not_called()

    def test_no_parent_lease_blocks_recovery(self):
        with patch.object(gate,'plan',return_value=({},[])),patch.dict(os.environ,{},clear=True), \
                patch.object(sys,'argv',['cleanup']),patch.object(gate,'recover') as recover:
            with self.assertRaisesRegex(ValueError,'parent lease'):gate.main()
            recover.assert_not_called()

    def test_wrapper_has_signal_unwind_and_exact_context_restore(self):
        source=(HERE/'helpers/longhorn-cleanup.sh').read_text()
        subprocess.run(['bash','-n',str(HERE/'helpers/longhorn-cleanup.sh')],check=True)
        self.assertIn('kill -TERM "$helper_pid"',source)
        self.assertIn("trap 'exit 143' TERM",source)
        self.assertIn('cp -a "$LONGHORN_CLEANUP_WORK/control-kubeconfig" "$control_config"',source)
        self.assertIn('[[ "$rc" == 0 ]]',source)
        self.assertNotIn('lab-wipe',source)


class FinalVerification(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.directory=Path(self.temp.name)
        self.owner={'run_id':'run','cluster_uid':'core','node_uids':{},'namespace_uid':'native'}
        self.store=SimpleNamespace(directory=self.directory/'store',record=self.directory/'store.json',
            transport_listener=Mock(return_value=None))
        self.lab=SimpleNamespace(DIRECTORY=self.directory,require_lease=Mock(),core_identity=Mock(),
            owner=Mock(return_value=self.owner.copy()),installation_health=Mock(),get=Mock(return_value={'items':[]}),
            backup_store=Mock(return_value=self.store))
        self.scope={'v2':(self.owner,{})}

    def verify(self):
        with patch.object(gate,'fixture',return_value=self.lab):gate.verify(self.scope)

    def test_healthy_scope_keeps_original_core_identity(self):self.verify()

    def test_all_native_consumer_kinds_are_checked_before_acceptance(self):
        for kind in ('volumes.longhorn.io','engines.longhorn.io','replicas.longhorn.io',
                'snapshots.longhorn.io','backups.longhorn.io','backupvolumes.longhorn.io'):
            with self.subTest(kind=kind):
                self.lab.get.side_effect=lambda current,*a,**k:{'items':[{'metadata':{'uid':'residual'}}] if current==kind else []}
                with self.assertRaisesRegex(ValueError,'consumer'):self.verify()

    def test_owned_backup_credential_cannot_survive_final_verification(self):
        self.lab.get.side_effect=lambda kind,*a,**k:{'items':[{'metadata':{'labels':{'kubeauto.io/longhorn-backup-run':'run'}}}] if kind=='secrets' else []}
        with self.assertRaisesRegex(ValueError,'credential'):self.verify()

    def test_orphan_backend_or_transport_prevents_acceptance(self):
        self.store.transport_listener.return_value={'pid':1}
        with self.assertRaisesRegex(ValueError,'backend or transport'):self.verify()
        self.store.transport_listener.return_value=None;self.store.directory.mkdir()
        with self.assertRaisesRegex(ValueError,'backend or transport'):self.verify()

    def test_original_core_uid_change_is_never_signed_as_clean(self):
        self.lab.owner.return_value=dict(self.owner,cluster_uid='replacement')
        with self.assertRaisesRegex(ValueError,'original core ownership'):self.verify()

    def test_cpu_terminal_receipt_must_match_actual_restored_configuration(self):
        (self.directory/'longhorn-v2-cpu-isolation.json').write_text('{}')
        (self.directory/'config.yml').write_text('changed')
        cpu=SimpleNamespace(SETTING='setting',NS='longhorn-system',receipt=Mock(return_value={
            'clean':True,'original_config':'original','setting':{'uid':'setting-uid','value':'original'}}),
            managers=Mock(return_value={'owned':True}))
        self.lab.get.side_effect=lambda kind,*a,**k:({'metadata':{'uid':'setting-uid'},'value':'original'} if kind=='settings.longhorn.io' else {'items':[]})
        with patch.object(gate,'load',return_value=cpu),self.assertRaisesRegex(ValueError,'CPU isolation recovered receipt'):
            self.verify()


class ShellContracts(unittest.TestCase):
    @contextmanager
    def fixture(self, mode, original=True, wrapper_mode='cleanup'):
        with tempfile.TemporaryDirectory() as directory:
            base=Path(directory);(base/'.venv/bin').mkdir(parents=True)
            (base/'tests/helpers').mkdir(parents=True);(base/'contexts').mkdir()
            context=base/'contexts/config'
            if original:context.write_text('original-context\n');context.chmod(0o640)
            (base/'.venv/bin/python').symlink_to(sys.executable)
            helper=base/'tests/helpers/longhorn-cleanup.py'
            helper.write_text(r"""
import os,signal,time,sys
from pathlib import Path
base=Path(__file__).resolve().parents[2]
(base/'argv').write_text(' '.join(sys.argv[1:]))
(base/'contexts/config').write_text('temporary-context\n')
(base/'child.pid').write_text(str(os.getpid()))
def stop(number, frame):
    (base/'owner-unwound').write_text('restored')
    raise SystemExit(128+number)
signal.signal(signal.SIGTERM,stop)
mode=os.environ['OWNER_TEST_MODE']
(base/'ready').write_text('ready')
if mode=='interrupt':
    while True:time.sleep(.02)
if mode=='failure':
    print('LONGHORN_OWNER_FAIL first-cause',flush=True)
    raise SystemExit(86)
print('LONGHORN_CLEANUP_SCOPED_CLEAN_PASS',flush=True)
""")
            source=(HERE/'helpers/longhorn-cleanup.sh').read_text()
            source=source.replace('BASE=/usr/local/kubeauto','BASE='+str(base))
            source=source.replace('control_config=/root/.kube/config','control_config='+str(context))
            source=source.replace('/var/tmp/kubeauto-longhorn-cleanup.XXXXXX',str(base/'durable.XXXXXX'))
            source=source.replace('/var/lock/',str(base)+'/')
            script=base/'wrapper.sh';script.write_text(source)
            saved={}
            try:
                for fd,name in ((9,'kubeauto-ceph-priority.lock'),(7,'kubeauto-ceph-compute.lock'),(8,'kubeauto-redis-core-pool.lock')):
                    try:saved[fd]=os.dup(fd)
                    except OSError:saved[fd]=None
                    source_fd=os.open(base/name,os.O_CREAT|os.O_RDWR,0o600)
                    os.dup2(source_fd,fd)
                    if source_fd!=fd:os.close(source_fd)
                    fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
                process=subprocess.Popen(['bash',str(script),wrapper_mode],stdout=subprocess.PIPE,stderr=subprocess.STDOUT,
                    env=dict(os.environ,LONGHORN_PARENT_LEASE_HELD='yes',OWNER_TEST_MODE=mode),pass_fds=(7,8,9))
                try:yield base,context,process
                finally:
                    if process.poll() is None:process.terminate();process.communicate(timeout=10)
            finally:
                for fd,original_fd in saved.items():
                    if original_fd is None:os.close(fd)
                    else:os.dup2(original_fd,fd);os.close(original_fd)

    def finish(self, base, process, expected, marker='LONGHORN_CLEANUP_PASS'):
        output=process.communicate(timeout=10)[0].decode()
        self.assertEqual(process.returncode,expected,output)
        work=list(base.glob('durable.*'));self.assertEqual(len(work),1)
        self.assertEqual((work[0]/'exit').read_text().strip(),str(expected))
        self.assertEqual((work[0]/'finalized').read_text().strip(),'0')
        self.assertEqual(output.splitlines().count(marker),int(expected==0))
        return output

    def test_normal_restores_original_context_and_mode(self):
        with self.fixture('normal') as (base,context,process):
            self.finish(base,process,0);self.assertEqual(context.read_text(),'original-context\n')
            self.assertEqual(context.stat().st_mode&0o777,0o640)

    def test_qualification_wrapper_forwards_only_qualify_and_uses_its_own_terminal_marker(self):
        with self.fixture('normal',wrapper_mode='cleanup-qualification') as (base,context,process):
            output=self.finish(base,process,0,'LONGHORN_CLEANUP_QUALIFICATION_PASS')
            self.assertEqual((base/'argv').read_text(),'--qualify')
            self.assertNotIn('LONGHORN_CLEANUP_PASS',output.splitlines())
            self.assertEqual(context.read_text(),'original-context\n')

    def test_absent_context_is_absent_after_success(self):
        with self.fixture('normal',original=False) as (base,context,process):
            self.finish(base,process,0);self.assertFalse(context.exists())

    def test_child_failure_preserves_rc86_context_and_first_failure(self):
        with self.fixture('failure') as (base,context,process):
            output=self.finish(base,process,86);self.assertIn('LONGHORN_OWNER_FAIL first-cause',output)
            self.assertEqual(context.read_text(),'original-context\n')

    def test_sigterm_waits_for_owned_unwind_and_restores_context(self):
        with self.fixture('interrupt') as (base,context,process):
            deadline=time.monotonic()+10
            while not (base/'ready').exists():
                if process.poll() is not None or time.monotonic()>deadline:self.fail('helper did not reach owned wait')
                time.sleep(.02)
            process.terminate();self.finish(base,process,143)
            self.assertEqual((base/'owner-unwound').read_text(),'restored')
            self.assertEqual(context.read_text(),'original-context\n')
            with self.assertRaises(ProcessLookupError):os.kill(int((base/'child.pid').read_text()),0)


class WiringContracts(unittest.TestCase):
    def wired(self, name):
        candidate=HERE/'wiring'/name
        return candidate if candidate.is_file() else PRODUCT/name

    def test_authoritative_dispatch_reaches_only_cleanup_with_exact_marker(self):
        for mode,marker in (('cleanup','LONGHORN_CLEANUP_PASS'),('cleanup-qualification','LONGHORN_CLEANUP_QUALIFICATION_PASS')):
            script='source "$1"\nlonghorn_launch() { printf "%s %s\\n" "$1" "$2"; }\nlonghorn_dispatch --longhorn-'+mode+'\n'
            result=subprocess.run(['bash','-c',script,'contract',str(self.wired('tests/helpers/longhorn-runner.sh'))],
                check=True,capture_output=True,text=True)
            self.assertEqual(result.stdout,mode+' '+marker+'\n')

    def test_cleanup_launch_passes_both_allowlists_before_the_durable_boundary(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);(root/'tests/helpers').mkdir(parents=True)
            (root/'tests/helpers/sync-kubeauto.sh').write_text('exit 0\n')
            script=r"""
set -Eeuo pipefail
source "$1"
ROOT="$2"
longhorn_local_gate() { printf 'LOCAL_GATE %s %s\n' "$1" "$2"; }
ssh_longhorn() {
  if [[ "$1" == 'cat /tmp/kubeauto-longhorn-gate.exit' || "$1" == 'cat /tmp/kubeauto-longhorn-gate.finalized' ]]; then
    printf '0\n'
    return
  fi
  if [[ "$1" == 'cat /tmp/kubeauto-longhorn-live.log' ]]; then
    echo LONGHORN_CLEANUP_PASS
    return
  fi
  [[ "$1" == *'case "$mode" in '* ]] || return 86
  local modes="${1#*'case "$mode" in '}"
  modes="${modes%%)*}"
  [[ "|$modes|" == *'|cleanup|'* && "|$modes|" == *'|cleanup-qualification|'* ]] || return 86
  echo LONGHORN_LEASE_READY
  IFS= read -r mode
  [[ "$mode" == cleanup ]] || return 87
  echo LONGHORN_DURABLE_LAUNCHED
}
monitor_remote_job() { [[ "$6" == LONGHORN_CLEANUP_PASS ]]; }
remote_job_summary() { echo 'exit=0 finalized=0'; }
longhorn_launch cleanup LONGHORN_CLEANUP_PASS
"""
            result=subprocess.run(['bash','-c',script,'contract',str(self.wired('tests/helpers/longhorn-runner.sh')),str(root)],
                capture_output=True,text=True)
            self.assertEqual(result.returncode,0,result.stdout+result.stderr)
            self.assertIn('LOCAL_GATE static LONGHORN_STATIC_PASS',result.stdout)
            self.assertIn('LOCAL_GATE artifacts LONGHORN_ARTIFACTS_PASS',result.stdout)
            self.assertIn('completed=2',result.stdout)
            # This fixture has no Python or legacy V1 owner, so an accidental
            # V1 verify-transport precondition cannot pass this launch.
            records=list((root/'logs/longhorn-cases').glob('cleanup-*/durable-status.txt'))
            self.assertEqual(len(records),1)
            for name in ('durable-exit','durable-finalized'):
                self.assertEqual((records[0].parent/name).read_text(),'0\n')

    def test_live_route_requires_parent_lease_then_execs_the_owned_wrapper(self):
        source=self.wired('tests/helpers/longhorn-live.sh').read_text()
        case=source.index('  cleanup|cleanup-qualification)\n');lease=source.index('for entry in ')
        self.assertLess(lease,case)
        self.assertIn('exec bash "$BASE/tests/helpers/longhorn-cleanup.sh" "$MODE"',source)
        self.assertNotIn('--longhorn-only',source)

    def test_live_cleanup_route_executes_before_any_v1_or_v2_setup(self):
        with tempfile.TemporaryDirectory() as directory,ProcessContracts().descriptors():
            base=Path(directory);(base/'tests/helpers').mkdir(parents=True)
            helper=base/'tests/helpers/longhorn-cleanup.sh'
            helper.write_text('[[ "$#" == 1 && "$1" == cleanup ]] || exit 86\necho ONLY_CLEANUP_OWNER_REACHED\n')
            source=self.wired('tests/helpers/longhorn-live.sh').read_text()
            source=source.replace('BASE=/usr/local/kubeauto','BASE='+str(base))
            # Lease ownership is covered separately with real lockfiles in the
            # wrapper contracts. This fixture changes only fixed paths and
            # stubs flock so dispatch can use inherited local /dev/null FDs.
            source=source.replace('"/var/lock/$filename"','/dev/null')
            script=base/'live.sh';script.write_text(source)
            result=subprocess.run(['bash','-c','flock() { :; }; source "$1" cleanup','contract',str(script)],
                env=dict(os.environ,LONGHORN_PARENT_LEASE_HELD='yes'),pass_fds=(7,8,9),capture_output=True,text=True)
            self.assertEqual(result.returncode,0,result.stdout+result.stderr)
            self.assertEqual(result.stdout,'ONLY_CLEANUP_OWNER_REACHED\n')

    def test_static_gate_checks_the_exact_changed_python_and_contract(self):
        source=self.wired('tests/helpers/longhorn-static.sh').read_text()
        self.assertIn('"$PY" -m py_compile tests/helpers/longhorn-cleanup.py',source)
        self.assertIn('"$PY" -m unittest tests.unit.test_longhorn_cleanup -v',source)
        self.assertLess(source.index('tests.unit.test_longhorn_cleanup'),source.index('echo LONGHORN_STATIC_PASS'))

    def test_all_three_wired_shells_parse(self):
        for name in ('longhorn-runner.sh','longhorn-live.sh','longhorn-static.sh'):
            subprocess.run(['bash','-n',str(self.wired('tests/helpers/'+name))],check=True)


class ForeignCanaryContracts(unittest.TestCase):
    def setUp(self):
        import copy
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.directory=Path(self.temp.name);self.objects={};self.state={'run_id':'run','cluster_uid':'core'}
        def get(kind,name='',namespace=''):
            aliases={'Namespace':'namespace','ConfigMap':'configmap'}
            return self.objects.get((aliases.get(kind,kind),namespace,name))
        def create(*args,stdin):
            self.assertEqual(args,('create','-f','-','-o','json'))
            obj=json.loads(stdin);obj['metadata']['uid']='uid-'+obj['kind']
            self.objects[({'Namespace':'namespace','ConfigMap':'configmap'}[obj['kind']],obj['metadata'].get('namespace',''),obj['metadata']['name'])]=obj
            return SimpleNamespace(stdout=json.dumps(obj))
        def write(path,row):path.write_text(json.dumps(row));path.chmod(0o600)
        def remove(obj):
            self.objects.pop(({'Namespace':'namespace','ConfigMap':'configmap'}[obj['kind']],obj['metadata'].get('namespace',''),obj['metadata']['name']))
        self.lab=SimpleNamespace(owner=lambda:self.state,get=get,kubectl=Mock(side_effect=create),write_private=write,
            namespace_defaults=lambda namespace:['default-sa','default-ca'],wait=lambda label,timeout,check:check(),
            check_business_graph=Mock(),remove=Mock(side_effect=remove))
        self.canary=gate.ForeignCanary(self.lab,self.directory,'v1')

    def test_foreign_data_has_original_uid_hash_and_is_removed_only_by_its_owner(self):
        self.canary.create();proof=self.canary.verify()
        self.assertEqual(proof['namespace_uid'],'uid-Namespace');self.assertEqual(proof['configmap_uid'],'uid-ConfigMap')
        self.assertEqual(len(proof['payload_sha256']),64)
        self.canary.clean();self.assertEqual(self.objects,{})
        self.assertTrue(self.canary.receipt()['clean']);self.assertEqual(self.lab.remove.call_count,2)

    def test_replaced_or_modified_canary_is_refused_before_any_delete(self):
        import copy
        for mutation in ('uid','data'):
            with self.subTest(mutation=mutation):
                self.setUp();self.canary.create()
                cm=next(obj for obj in self.objects.values() if obj['kind']=='ConfigMap')
                if mutation=='uid':cm['metadata']['uid']='replacement'
                else:cm['data']['payload']='modified foreign data'
                with self.assertRaisesRegex(ValueError,'UID or content'):self.canary.clean()
                self.lab.remove.assert_not_called()

    def test_foreign_core_is_refused_without_touching_canary(self):
        self.canary.create();self.state['cluster_uid']='replacement'
        with self.assertRaisesRegex(ValueError,'another core'):self.canary.clean()
        self.lab.remove.assert_not_called()

    def test_new_foreign_namespace_object_blocks_namespace_deletion(self):
        self.canary.create();self.lab.check_business_graph.side_effect=ValueError('foreign object')
        with self.assertRaisesRegex(ValueError,'foreign object'):self.canary.clean()
        self.lab.remove.assert_not_called()

    def test_incomplete_creation_receipt_never_authorizes_delete(self):
        self.lab.write_private(self.canary.record,dict(self.state,namespace='uncertain'))
        with self.assertRaisesRegex(ValueError,'complete original UID'):self.canary.clean()
        self.lab.remove.assert_not_called()


class QualificationContracts(unittest.TestCase):
    def test_fixture_child_requires_qualifier_before_any_api_mutation(self):
        with patch.dict(os.environ,{},clear=True),patch.object(gate,'fixture') as fixture:
            with self.assertRaisesRegex(ValueError,'requires its qualifier'):gate.fixture_child('v1','normal')
            fixture.assert_not_called()

    def test_fixture_child_normal_and_failure_leave_the_real_owner_journal(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'longhorn-ownership-preinstall.json';objects={}
            helper=SimpleNamespace(path=path,desired=lambda case:{'apiVersion':'apiextensions.k8s.io/v1',
                'kind':'CustomResourceDefinition','metadata':{'name':'kubeautoownershipprobes.longhorn.io'}},
                current=lambda row:objects.get('crd'),binding=lambda obj:{'uid':obj['metadata']['uid']} if obj else None)
            def write(path,row):path.write_text(json.dumps(row));path.chmod(0o600)
            def create(*args,stdin):
                obj=json.loads(stdin);obj['metadata']['uid']='original-crd';obj['status']={'conditions':[{'type':'Established','status':'True'}]}
                objects['crd']=obj;return SimpleNamespace(stdout=json.dumps(obj))
            lab=SimpleNamespace(require_lease=Mock(),core_identity=Mock(),installation_health=Mock(),
                owner=lambda:{'run_id':'run','cluster_uid':'core'},write_private=write,kubectl=Mock(side_effect=create),
                wait=lambda label,timeout,check:check())
            for case,expected in (('normal',0),('failure',86)):
                with self.subTest(case=case),patch.dict(os.environ,{'LONGHORN_CLEANUP_FIXTURE_CHILD':'yes'}), \
                        patch.object(gate,'fixture',return_value=lab),patch.object(gate,'load',return_value=SimpleNamespace(PreinstallProbe=lambda lab:helper)):
                    self.assertEqual(gate.fixture_child('v1',case),expected)
                    self.assertEqual(json.loads(path.read_text())['created'],{'uid':'original-crd'})
                    self.assertIn('crd',objects)
                    # No owner cleanup is called by the child on these exits.
                    path.unlink();objects.clear()

    def test_three_real_fixture_process_exits_retain_their_original_uid_journal(self):
        with tempfile.TemporaryDirectory() as directory,ProcessContracts().descriptors():
            base=Path(directory);(base/'tests/helpers').mkdir(parents=True)
            child=base/'tests/helpers/longhorn-cleanup.py'
            child.write_text(r'''
import argparse,json,os,time
from pathlib import Path
p=argparse.ArgumentParser();p.add_argument('--fixture-child');p.add_argument('--scope');a=p.parse_args()
base=Path(__file__).resolve().parents[2]
assert os.environ['LONGHORN_CLEANUP_FIXTURE_CHILD']=='yes'
row={'created':{'uid':'original-crd'}}
(base/'owner.json').write_text(json.dumps(row));(base/'api.json').write_text(json.dumps(row['created']))
print('LONGHORN_CLEANUP_FIXTURE_READY',flush=True)
if a.fixture_child=='normal':raise SystemExit(0)
if a.fixture_child=='failure':raise SystemExit(86)
while True:time.sleep(.02)
''')
            helper=SimpleNamespace(receipt=lambda:json.loads((base/'owner.json').read_text()),
                current=lambda row:json.loads((base/'api.json').read_text()),binding=lambda obj:obj)
            def wait(label,timeout,check):
                deadline=time.monotonic()+5
                while True:
                    result=check()
                    if result:return result
                    if time.monotonic()>deadline:raise TimeoutError(label)
                    time.sleep(.01)
            lab=SimpleNamespace(wait=wait,write_private=lambda path,row:path.write_text(json.dumps(row)))
            with patch.object(gate,'BASE',base),patch.object(gate,'load',return_value=SimpleNamespace(PreinstallProbe=lambda lab:helper)):
                for case,expected in (('normal',0),('failure',86),('interruption',-9)):
                    row=gate.prepare_fixture(lab,'v1',case,base)
                    self.assertEqual(row['created']['uid'],'original-crd')
                    self.assertEqual(json.loads((base/('v1-'+case+'-fixture.json')).read_text())['exit'],expected)
                    self.assertTrue((base/'owner.json').exists());self.assertTrue((base/'api.json').exists())

    def test_public_cleanup_uses_fixed_live_route_and_original_owner_markers(self):
        for missing in ('','owner','durable'):
            with self.subTest(missing=missing),tempfile.TemporaryDirectory() as directory,ProcessContracts().descriptors():
                base=Path(directory);(base/'tests/helpers').mkdir(parents=True)
                durable=PRODUCT/'tests/helpers/run-durable-gate.sh'
                (base/'tests/helpers/run-durable-gate.sh').write_text(durable.read_text())
                live=base/'tests/helpers/longhorn-live.sh'
                source='[[ "$1" == cleanup ]] || exit 86\n'
                if missing!='owner':source+='echo "LONGHORN_CLEANUP_OWNER_START scope=v1 owner=business"\n'
                source+='echo LONGHORN_CLEANUP_SCOPED_CLEAN_PASS\necho LONGHORN_CLEANUP_PASS\n'
                live.write_text(source)
                if missing=='durable':
                    (base/'tests/helpers/run-durable-gate.sh').write_text('shift 2\n"$@"\n')
                previous=os.umask(0o077)
                try:
                    with patch.object(gate,'BASE',base):
                        if not missing:gate.public_cleanup(base,'v1','normal')
                        else:
                            with self.assertRaises((ValueError,FileNotFoundError)):gate.public_cleanup(base,'v1','normal')
                finally:os.umask(previous)

    def test_qualification_cannot_start_with_an_active_owner_or_missing_engine(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError,'active scenario'):gate.qualify({},[Mock()],Path(directory))
            with self.assertRaisesRegex(ValueError,'both installed'):gate.qualify({},[],Path(directory))

    def test_qualification_calls_public_cleanup_for_exactly_three_cases_per_engine(self):
        with tempfile.TemporaryDirectory() as directory:
            base=Path(directory);(base/'config.yml').write_bytes(b'original')
            scopes={scope:({'namespace_uid':'native'}, {}) for scope in ('v1','v2')}
            lab=SimpleNamespace(DIRECTORY=base,require_lease=Mock(),core_identity=Mock(),get=Mock(return_value={'items':[]}),shared_snapshot_identity=lambda:{'uid':'original'})
            canary=SimpleNamespace(create=Mock(),verify=Mock(return_value={'uid':'foreign','hash':'original'}),clean=Mock())
            owner=SimpleNamespace(path=base/'absent-journal',current=lambda row:None)
            with patch.object(gate,'verify') as verify,patch.object(gate,'fixture',return_value=lab), \
                    patch.object(gate,'ForeignCanary',return_value=canary),patch.object(gate,'prepare_fixture',return_value={'created':{'uid':'original'}}) as prepare, \
                    patch.object(gate,'public_cleanup') as public,patch.object(gate,'plan',return_value=(scopes,[])), \
                    patch.object(gate,'load',return_value=SimpleNamespace(PreinstallProbe=lambda lab:owner)):
                gate.qualify(scopes,[],base)
                expected=[(scope,case) for scope in ('v1','v2') for case in ('normal','failure','interruption')]
                self.assertEqual([(c.args[1],c.args[2]) for c in public.call_args_list],expected)
                self.assertEqual(prepare.call_count,6);self.assertEqual(verify.call_count,2)
                self.assertEqual(canary.clean.call_count,2)

    def test_qualification_failure_recovers_only_through_public_cleanup_and_preserves_first_error(self):
        with tempfile.TemporaryDirectory() as directory:
            base=Path(directory);(base/'config.yml').write_bytes(b'original')
            scopes={scope:({'namespace_uid':'native'}, {}) for scope in ('v1','v2')}
            lab=SimpleNamespace(DIRECTORY=base,require_lease=Mock(),core_identity=Mock(),get=Mock(return_value={'items':[]}),shared_snapshot_identity=lambda:{})
            canary=SimpleNamespace(create=Mock(),verify=Mock(return_value={}),clean=Mock(),receipt=lambda:{})
            active=gate.route('business','v1','longhorn-ownership-preinstall.json','','',shell=False)
            with patch.object(gate,'verify'),patch.object(gate,'fixture',return_value=lab), \
                    patch.object(gate,'ForeignCanary',return_value=canary),patch.object(gate,'prepare_fixture',side_effect=ValueError('first cause')), \
                    patch.object(gate,'public_cleanup') as public,patch.object(gate,'plan',return_value=(scopes,[active])):
                with self.assertRaisesRegex(ValueError,'first cause'):gate.qualify(scopes,[],base)
                public.assert_called_once_with(base,'v1','qualification-recovery');canary.clean.assert_called_once()


if __name__=='__main__':unittest.main()
