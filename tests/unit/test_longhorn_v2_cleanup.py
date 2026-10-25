"""Retirement ordering and durable host-release proofs without lab mutation."""
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import stat
import subprocess
import unittest
from unittest.mock import patch
import uuid

spec=importlib.util.spec_from_file_location('v2_retirement',Path(__file__).resolve().parents[1]/'helpers/longhorn-v2-cleanup.py')
gate=importlib.util.module_from_spec(spec);spec.loader.exec_module(gate)


def fixture_row(scope='source'):
    nodes=gate.HOSTS[scope]
    disks={n:{'machine_id':'machine-'+n,'path':'/dev/disk/by-id/wwn-'+n,
        'identity':'wwn-'+n,'size_bytes':107374182400,'original_hostname':'ubuntu'} for n in nodes}
    observed={n:{'host':n,'inventory':{'machine_id':d['machine_id'],'hugepages_2mi':'0'},
        'assessment':{'blockers':[],'candidates':[{'identity':d['identity'],'size_bytes':d['size_bytes'],'by_id':[d['path']]}]}}
        for n,d in disks.items()}
    return {'id':str(uuid.uuid4()),'scope':scope,'run_id':str(uuid.uuid4()),'cluster_uid':str(uuid.uuid4()),
        'raw_disks':disks,'released':observed,'checkpoint_clean':['failure','sigterm'],
        'archive':'/var/tmp/kubeauto-longhorn-history/core-test','phase':'released','canaries':{}}


class ReleaseProof(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        self.row=fixture_row();self.journal=self.root/'journal.json';self.write()
        self.prefix=self.root/'durable';self.log=self.root/'retire.log';self.out=self.root/'proofs'
        for suffix in ('exit','finalized'):Path(str(self.prefix)+'.'+suffix).write_text('0\n')
        self.log.write_text('\n'.join(gate.MARKER+' host='+n for n in self.row['raw_disks'])+'\n')

    def write(self):self.journal.write_text(json.dumps(self.row))
    def finalize(self):gate.finalize(self.journal,self.prefix,self.log,self.out)

    def test_exact_hardware_and_durable_log_bound_to_each_release(self):
        self.finalize();paths=list(self.out.glob('*.json'));self.assertEqual(len(paths),3)
        proof=json.loads(next(p for p in paths if '.148.' in p.name).read_text())
        self.assertEqual(proof['signed_scope'],'v2-source-host-release')
        self.assertEqual(proof['original_raw_disk'],self.row['raw_disks']['192.168.47.148'])
        self.assertEqual(proof['durable'],{'exit':0,'finalized':0})
        self.assertEqual(proof['raw_logs'][0]['path'],str(self.log))
        self.assertIn('+00:00',proof['released_at'])

    def test_nonzero_or_missing_fence_never_publishes(self):
        for suffix in ('exit','finalized'):
            path=Path(str(self.prefix)+'.'+suffix);path.write_text('1\n')
            with self.assertRaisesRegex(ValueError,'durable rc'):self.finalize()
            self.assertFalse(self.out.exists());path.write_text('0\n')

    def test_same_host_marker_cannot_sign_missing_other_host(self):
        self.log.write_text(gate.MARKER+' host=192.168.47.148\n')
        with self.assertRaisesRegex(ValueError,'exact host marker'):self.finalize()
        self.assertFalse(self.out.exists())

    def test_checkpoint_cleanup_is_required_for_source_scope(self):
        self.row['checkpoint_clean']=['failure'];self.write()
        with self.assertRaisesRegex(ValueError,'SIGTERM'):self.finalize()

    def test_foreign_canary_or_source_error_cannot_be_hidden_by_success_marker(self):
        with self.log.open('a') as stream:stream.write('LONGHORN_CLEANUP_FAIL foreign-canary\n')
        with self.assertRaisesRegex(ValueError,'failed retirement'):self.finalize()

    def test_replacement_disk_machine_or_page_pool_blocks_release(self):
        original=copy.deepcopy(self.row['released']['192.168.47.148'])
        for field,value in (('machine_id','foreign'),('hugepages_2mi','1024')):
            observed=copy.deepcopy(original);observed['inventory'][field]=value
            with self.assertRaisesRegex(ValueError,'not clean'):gate.validate_release(self.row,'192.168.47.148',observed)
        observed=copy.deepcopy(original);observed['assessment']['candidates'][0]['identity']='replacement'
        with self.assertRaisesRegex(ValueError,'raw identity'):gate.validate_release(self.row,'192.168.47.148',observed)

    def test_target_proof_cannot_release_any_source_host(self):
        self.row=fixture_row('target');self.write()
        self.log.write_text('LONGHORN_V2_TARGET_HOST_RELEASE_PASS host=192.168.47.149\n');self.finalize()
        paths=list(self.out.glob('*.json'));self.assertEqual(len(paths),1)
        proof=json.loads(paths[0].read_text())
        self.assertEqual(proof['signed_scope'],'v2-target-host-release');self.assertEqual(proof['host'],'192.168.47.149')
        with self.assertRaisesRegex(ValueError,'scope'):gate.read(self.journal,'source')


class RetirementSequence(unittest.TestCase):
    def test_installed_reboot_intent_blocks_retirement_before_any_native_operation(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'longhorn-v2-installed-post-reboot-environment.json'
            path.write_text('{"phase":"stop-intent"}'); path.chmod(0o600)
            lab=SimpleNamespace(DIRECTORY=Path(directory),core_identity=lambda:None,
                owner=lambda:{'raw_disks':dict.fromkeys(gate.HOSTS['source'])})
            with self.assertRaisesRegex(ValueError,'finish the owning module'):
                gate.preflight(lab,'source')

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        self.journal=self.root/'retire.json';self.row=fixture_row();self.row.update(phase='prepared',canaries={n:{'driver':'sd'} for n in gate.HOSTS['source']})
        self.row['native_receipt']={'namespace_uid':'ns','nodes':{}}
        directory=self.root/'yml/longhorn';directory.mkdir(parents=True)
        (directory/'block-disks-uninstalled-ns.json').write_text(json.dumps(self.row['native_receipt']))
        self.journal.write_text(json.dumps(self.row));self.events=[]
        self.lab=SimpleNamespace(NODES=gate.HOSTS['source'],owner=lambda:dict(self.row,namespace_uid='ns'),
            DIRECTORY=self.root,get=lambda *a:{'items':[]},shared_snapshot_identity=lambda:{'snapshot':'unchanged'},
            write_private=lambda p,r:p.write_text(json.dumps(r)),recover_installation=lambda pid:self.events.append('native'),
            restore_hosts=lambda:self.events.append('hosts'))
        self.resources=SimpleNamespace(restore=lambda lab:self.events.append('resources'),
            probe=lambda lab,node:{'total':0,'free':0,'reserved':0})
        self.remote=lambda *a,**k:({'driver':'sd'} if not self.events.append('host-check') else None)

    def test_native_uninstall_precedes_page_pool_restore_and_raw_clean_verification(self):
        with patch.object(gate,'guard_business',side_effect=lambda *a:self.events.append('guards')), \
                patch.object(gate,'remote',side_effect=self.remote),patch.object(gate,'load',return_value=self.resources):
            gate.storage(self.lab,SimpleNamespace(), 'source',self.journal,123)
        self.assertLess(self.events.index('guards'),self.events.index('native'))
        self.assertLess(self.events.index('native'),self.events.index('resources'))
        self.assertEqual(gate.read(self.journal)['phase'],'storage-clean')

    def test_failed_native_uninstall_never_restores_resources_or_declares_clean(self):
        def failed(pid):raise ValueError('native failure')
        self.lab.recover_installation=failed
        with patch.object(gate,'guard_business'),patch.object(gate,'remote',side_effect=self.remote),patch.object(gate,'load',return_value=self.resources):
            with self.assertRaisesRegex(ValueError,'native failure'):gate.storage(self.lab,SimpleNamespace(),'source',self.journal,123)
        self.assertNotIn('resources',self.events);self.assertEqual(gate.read(self.journal)['phase'],'native-uninstall')

    def test_checkpoint_requires_the_recorded_fault_and_healthy_empty_baseline(self):
        self.row['fault_intent']='sigterm';self.journal.write_text(json.dumps(self.row))
        self.lab.installation_health=lambda:self.events.append('health')
        with patch.object(gate,'preflight',side_effect=lambda *a:self.events.append('preflight')),patch.object(gate,'remote',side_effect=self.remote):
            with self.assertRaisesRegex(ValueError,'intent differs'):gate.checkpoint(self.lab,'source',self.journal,0,'failure',True)
            gate.checkpoint(self.lab,'source',self.journal,0,'sigterm',True)
        self.assertIn('health',self.events);self.assertNotIn('fault_intent',gate.read(self.journal))

    def test_recovery_never_invents_guard_acceptance(self):
        with patch.object(gate,'remote',side_effect=self.remote):
            with self.assertRaisesRegex(ValueError,'does not invent'):gate.storage(self.lab,SimpleNamespace(),'source',self.journal,123,True)

    def test_replaced_archived_receipt_blocks_page_restore(self):
        (self.root/'yml/longhorn/block-disks-uninstalled-ns.json').write_text('{"namespace_uid":"foreign"}')
        with patch.object(gate,'guard_business'),patch.object(gate,'remote',side_effect=self.remote):
            with self.assertRaisesRegex(ValueError,'exact native raw-disk'):gate.storage(self.lab,SimpleNamespace(),'source',self.journal,123)
        self.assertNotIn('resources',self.events)


class RawHost(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        for name in ('etc','proc','var/lib','dev/disk/by-id','sys/class/block/sdb/holders'):
            (self.root/name).mkdir(parents=True)
        (self.root/'etc/machine-id').write_text('machine')
        (self.root/'dev/sdb').write_bytes(b'private simulated device')
        (self.root/'dev/disk/by-id/wwn-unit').symlink_to('../../sdb')
        self.request={'id':str(uuid.uuid4()),'disk':{'machine_id':'machine','path':'/dev/disk/by-id/wwn-unit',
            'identity':'wwn-unit','size_bytes':107374182400},'action':'create','require_clean':True}
        self.signatures=[];self.identity='wwn-unit'
        def command(args,**kwargs):
            value={'signatures':self.signatures} if args[0]=='wipefs' else {'blockdevices':[{
                'name':'sdb','type':'disk','size':107374182400,'ro':False,'fstype':None,'mountpoints':[None],'wwn':self.identity}]}
            return subprocess.CompletedProcess(args,0,json.dumps(value),'')
        self.runner=patch.object(subprocess,'run',side_effect=command);self.runner.start();self.addCleanup(self.runner.stop)
        # Only a private regular fixture substitutes for a block inode; no real
        # /dev, /sys, /proc or hardware write occurs in these tests.
        self.block=patch.object(stat,'S_ISBLK',return_value=True);self.block.start();self.addCleanup(self.block.stop)

    def act(self):return gate.host_probe(self.request,root=self.root)

    def test_exact_canary_survives_checks_and_is_removed_by_its_receipt(self):
        row=self.act();self.request.update(action='check',canary=row['canary']);self.act()
        self.request['action']='remove';self.act()
        self.assertFalse(list((self.root/'var/lib').glob('*.canary')))

    def test_interrupted_remove_requires_original_intent_and_rechecks_raw_identity(self):
        row=self.act();self.request.update(action='remove',canary=row['canary']);self.act()
        self.request['action']='check'
        with self.assertRaisesRegex(ValueError,'absent or replaced'):self.act()
        self.request['action']='finish-remove';self.assertIsNone(self.act()['canary'])
        self.identity='replacement'
        with self.assertRaisesRegex(ValueError,'identity or usage'):self.act()
        self.identity='wwn-unit';self.request.pop('canary')
        with self.assertRaisesRegex(ValueError,'original durable intent'):self.act()

    def test_stable_identity_signature_and_holders_block_release(self):
        self.identity='replacement'
        with self.assertRaisesRegex(ValueError,'identity or usage'):self.act()
        self.identity='wwn-unit';self.signatures=[{'type':'foreign'}]
        with self.assertRaisesRegex(ValueError,'signatures'):self.act()
        self.signatures=[];(self.root/'sys/class/block/sdb/holders/dm-0').touch()
        with self.assertRaisesRegex(ValueError,'holders'):self.act()
        self.assertFalse(list((self.root/'var/lib').glob('*.canary')))

    def test_replaced_canary_is_preserved_without_unlink(self):
        row=self.act();self.request.update(action='remove',canary=row['canary'])
        path=next((self.root/'var/lib').glob('*.canary'));path.rename(path.with_suffix('.original'));path.write_text(self.request['id'])
        with self.assertRaisesRegex(ValueError,'identity changed'):self.act()
        self.assertTrue(path.is_file())


class CompletedRecovery(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.nodes = gate.HOSTS['source']
        self.state = {'run_id':'run', 'cluster_uid':'cluster', 'namespace_uid':'old-namespace',
                      'raw_disks':{n:{'machine_id':'machine-'+n} for n in self.nodes}, 'host_state':{'nodes':{}}}
        self.row = {'run_id':'run', 'engine':'v2', 'phase':'complete', 'hosts':{}}
        for node in self.nodes:
            before = {'machine_id':'machine-'+node, 'boot_id':'new', 'dm':{}, 'configs':{},
                'units':{n:{'ActiveState':'active', 'UnitFileState':'enabled', 'LoadState':'loaded'}
                         for n in ('multipathd.service','multipathd.socket')}}
            old = copy.deepcopy(before); old['boot_id'] = 'old'
            after = copy.deepcopy(before)
            for unit in after['units'].values(): unit['ActiveState'] = 'inactive'
            self.state['host_state']['nodes'][node] = {'multipath':{'phase':'stopped','current':old}}
            self.row['hosts'][node] = {'phase':'stopped','before':before,'after':after}
        raw = json.dumps(self.state).encode(); sha = hashlib.sha256(raw).hexdigest()
        self.row.update(state_sha256=sha,state_archive='longhorn-v2-host-state-pre-reboot-'+sha[:16]+'.json',
            installation_before={'identity':{k:self.state[k] for k in ('run_id','cluster_uid','namespace_uid','raw_disks')}})
        self.archive = self.directory/self.row['state_archive']; self.archive.write_bytes(raw); self.archive.chmod(0o600)
        self.journal = self.directory/'longhorn-v2-installed-post-reboot-environment.json'; self.save()
        self.lab = SimpleNamespace(DIRECTORY=self.directory,NODES=self.nodes,core_identity=lambda:None,
                                   owner=lambda:self.state,get=lambda *a,**kw:{'items':[]})
    def save(self):
        self.journal.write_text(json.dumps(self.row)); self.journal.chmod(0o600)
    def test_complete_history_is_preserved_and_does_not_block_retirement(self):
        raw=self.journal.read_bytes(); archive=self.archive.read_bytes()
        self.assertIs(gate.preflight(self.lab,'source'),self.state)
        self.assertEqual(self.journal.read_bytes(),raw); self.assertEqual(self.archive.read_bytes(),archive)
    def test_single_reboot_receipt_preserves_unchanged_hosts_and_allows_retirement(self):
        original = json.loads(self.archive.read_bytes())
        for node in self.nodes[1:]:
            unchanged = copy.deepcopy(self.row['hosts'][node]['after'])
            unchanged['boot_id'] = 'old'
            original['host_state']['nodes'][node]['multipath']['current'] = unchanged
            self.row['hosts'][node].update(before=unchanged, after=copy.deepcopy(unchanged))
        raw = json.dumps(original).encode(); sha = hashlib.sha256(raw).hexdigest()
        self.archive.unlink()
        self.row.update(state_sha256=sha, state_archive='longhorn-v2-host-state-pre-reboot-'+sha[:16]+'.json')
        self.archive = self.directory/self.row['state_archive']; self.archive.write_bytes(raw); self.archive.chmod(0o600)
        self.save()
        self.assertIs(gate.preflight(self.lab, 'source'), self.state)
        self.row['hosts'][self.nodes[-1]]['before']['configs'] = {'foreign': 'change'}
        self.save()
        with self.assertRaisesRegex(ValueError, 'host boundary'):
            gate.preflight(self.lab, 'source')
    def test_later_current_namespace_or_boot_does_not_reopen_completed_operation(self):
        self.state=copy.deepcopy(self.state); self.state['namespace_uid']='new-namespace'
        for host in self.state['host_state']['nodes'].values():host['multipath']['current']['boot_id']='later'
        gate.preflight(self.lab,'source')
    def test_incomplete_or_foreign_operation_stops_before_native_queries(self):
        for key,value in (('phase','stop-intent'),('run_id','foreign'),('engine','v2-dr-target')):
            original=self.row[key]; self.row[key]=value; self.save()
            self.lab.get=lambda *a,**kw:self.fail('native query reached before refusal')
            with self.assertRaisesRegex(ValueError,'finish the owning module'):gate.preflight(self.lab,'source')
            self.row[key]=original
    def test_replaced_archive_bytes_are_rejected(self):
        self.archive.write_text('{}')
        with self.assertRaisesRegex(ValueError,'original bytes changed'):gate.preflight(self.lab,'source')
    def test_symlink_or_world_writable_record_is_rejected(self):
        original=self.journal.with_suffix('.original'); self.journal.rename(original); self.journal.symlink_to(original)
        with self.assertRaisesRegex(ValueError,'private and owned'):gate.preflight(self.lab,'source')
        self.journal.unlink(); original.rename(self.journal); self.journal.chmod(0o666)
        with self.assertRaisesRegex(ValueError,'private and owned'):gate.preflight(self.lab,'source')
    def test_path_escape_or_foreign_installation_is_rejected(self):
        self.row['state_archive']='../foreign'; self.save()
        with self.assertRaisesRegex(ValueError,'archive path differs'):gate.preflight(self.lab,'source')
        self.row['state_archive']=self.archive.name; self.row['installation_before']['identity']['run_id']='other'; self.save()
        with self.assertRaisesRegex(ValueError,'installation differs'):gate.preflight(self.lab,'source')
    def test_host_phase_or_unrelated_host_change_is_rejected(self):
        host=self.row['hosts'][self.nodes[-1]]; host['phase']='stop-intent'; self.save()
        with self.assertRaisesRegex(ValueError,'host boundary'):gate.preflight(self.lab,'source')
        host['phase']='stopped'; host['after']['configs']={'foreign':'change'}; self.save()
        with self.assertRaisesRegex(ValueError,'preserve host state'):gate.preflight(self.lab,'source')
    def test_existing_active_business_intent_still_blocks(self):
        (self.directory/'longhorn-install-business.json').write_text('{}')
        with self.assertRaisesRegex(ValueError,'finish the owning module'):gate.preflight(self.lab,'source')
    def test_legacy_adoption_and_installed_history_can_coexist(self):
        row=copy.deepcopy(self.row); row.pop('installation_before')
        adoption={'run_id':'run','engine':'v2','phase':'setup-intent'}
        raw=json.dumps(adoption).encode(); sha=hashlib.sha256(raw).hexdigest()
        row.update(adoption_sha256=sha,adoption_archive='longhorn-v2-im-adopt-rejected-setup-'+sha[:16]+'.json')
        archive=self.directory/row['adoption_archive']; archive.write_bytes(raw); archive.chmod(0o600)
        path=self.directory/'longhorn-v2-post-reboot-environment.json'; path.write_text(json.dumps(row)); path.chmod(0o600)
        gate.preflight(self.lab,'source')
        adoption['new_managers']={}; archive.write_text(json.dumps(adoption))
        with self.assertRaisesRegex(ValueError,'adoption provenance'):gate.preflight(self.lab,'source')
    def test_independent_target_legacy_history_is_bound_only_to_target(self):
        node='192.168.47.149'; old_node=self.nodes[0]
        self.state['raw_disks']={node:{'machine_id':'machine-'+node}}
        host=json.loads(json.dumps(self.state['host_state']['nodes'][old_node]).replace(old_node,node))
        self.state['host_state']['nodes']={node:host}
        recovered=json.loads(json.dumps(self.row['hosts'][old_node]).replace(old_node,node))
        raw=json.dumps(self.state).encode(); sha=hashlib.sha256(raw).hexdigest()
        self.row.update(engine='v2-dr-target',hosts={node:recovered},state_sha256=sha,
            state_archive='longhorn-v2-host-state-pre-reboot-'+sha[:16]+'.json')
        self.row.pop('installation_before')
        archive=self.directory/self.row['state_archive']; archive.write_bytes(raw); archive.chmod(0o600)
        adoption={'run_id':'run','engine':'v2-dr-target','phase':'setup-intent'}
        raw=json.dumps(adoption).encode(); sha=hashlib.sha256(raw).hexdigest()
        self.row.update(adoption_sha256=sha,adoption_archive='longhorn-v2-dr-target-im-adopt-rejected-setup-'+sha[:16]+'.json')
        archive=self.directory/self.row['adoption_archive']; archive.write_bytes(raw); archive.chmod(0o600)
        self.journal.unlink(); self.journal=self.directory/'longhorn-v2-post-reboot-environment.json'; self.save()
        self.lab.NODES=(node,)
        gate.preflight(self.lab,'target')
        with self.assertRaisesRegex(ValueError,'exact original raw-disk'):gate.preflight(self.lab,'source')
    def test_actual_page_owner_accepts_history_and_rejects_corrupt_archive(self):
        import uuid
        record={'id':str(uuid.uuid4()),'run_id':'run','cluster_uid':'cluster','hosts':{}}
        for node in self.nodes:
            original={'machine_id':'machine-'+node,'boot_id':'new','total':0,'free':0,'reserved':0,'kubelet_active':'active'}
            current=dict(original,total=1024,free=1024)
            record['hosts'][node]={'phase':'ready','original':original,'current':current}
        raw=json.dumps(record).encode(); sha=hashlib.sha256(raw).hexdigest()
        transition={'phase':'complete','run_id':'run','cluster_uid':'cluster','resource_id':record['id'],
            'hosts':copy.deepcopy(record['hosts']), 'archive':'longhorn-v2-resources-pre-reboot-'+record['id']+'-'+sha[:12]+'.json',
            'original_sha256':sha}
        record['reboot_recovery']={k:transition[k] for k in ('archive','original_sha256')}
        record['reboot_recovery']['transition']='longhorn-v2-resources-reboot.json'
        archive=self.directory/transition['archive']; archive.write_bytes(raw); archive.chmod(0o600)
        for name,row in (('longhorn-v2-resources.json',record),('longhorn-v2-resources-reboot.json',transition)):
            path=self.directory/name; path.write_text(json.dumps(row)); path.chmod(0o600)
        project=Path(__file__).resolve().parents[2]
        def actual(name):
            spec=importlib.util.spec_from_file_location(name,project/'tests/helpers'/name)
            module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module); return module
        with patch.object(gate,'load',side_effect=actual):
            gate.preflight(self.lab,'source')
            archive.write_text('{}')
            with self.assertRaisesRegex(ValueError,'archive changed'):gate.preflight(self.lab,'source')
    def test_page_recovery_uses_original_owner_verifier_before_native_queries(self):
        (self.directory/'longhorn-v2-resources-reboot.json').write_text('{}'); (self.directory/'longhorn-v2-resources-reboot.json').chmod(0o600)
        path=self.directory/'resources.json'; path.write_text('{}'); path.chmod(0o600)
        resources=SimpleNamespace(record_path=lambda lab:path,read_record=lambda lab:{})
        reboot=SimpleNamespace(completed_transition=lambda *args:(_ for _ in ()).throw(ValueError('incomplete page recovery')))
        with patch.object(gate,'load',side_effect=[resources,reboot]):
            with self.assertRaisesRegex(ValueError,'incomplete page'):gate.preflight(self.lab,'source')



if __name__=='__main__':unittest.main()
